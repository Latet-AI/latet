"""AWS's event-stream framing, in which Bedrock streams its answers: read frame by frame, checksums
checked. Latet reads it for the dashboard only; the answer goes to the agent as Amazon sent it."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

_PRELUDE = 12  # total length, headers' length, the prelude's checksum
_TRAILER = 4  # the message's checksum
_MAX_FRAME = 16 * 1024 * 1024
# A header's value: its type byte → the size of a fixed-size value (bool true and false carry none).
_FIXED = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
_BYTES, _STRING = 6, 7


class EventStreamError(ValueError):
    """Not a frame of AWS's event-stream: a length or a checksum does not hold."""


@dataclass(frozen=True, slots=True)
class Message:
    headers: dict[str, str]  # the text ones (`:event-type`, `:message-type`...)
    payload: bytes


def _crc(data: bytes | bytearray) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _headers(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    at = 0
    while at < len(raw):
        size = raw[at]
        name = raw[at + 1 : at + 1 + size].decode("utf-8", "replace")
        kind = raw[at + 1 + size]
        at += 2 + size
        if kind in (_BYTES, _STRING):
            (length,) = struct.unpack(">H", raw[at : at + 2])
            if kind == _STRING:
                out[name] = raw[at + 2 : at + 2 + length].decode("utf-8", "replace")
            at += 2 + length
        elif kind in _FIXED:
            at += _FIXED[kind]
        else:
            raise EventStreamError(f"unknown header type {kind}")
    return out


class EventStreamDecoder:
    """Frames out of a stream fed in pieces of any size."""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def held(self) -> int:
        """The bytes of a frame begun and not ended: what the next piece may end at once, with its own."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[Message]:
        self._buf += data
        out: list[Message] = []
        while len(self._buf) >= _PRELUDE:
            total, header_size, checksum = struct.unpack(">III", self._buf[:_PRELUDE])
            if _crc(self._buf[:8]) != checksum:
                raise EventStreamError("the prelude's checksum does not match")
            if not _PRELUDE + _TRAILER <= total <= _MAX_FRAME or header_size > total - _PRELUDE - _TRAILER:
                raise EventStreamError("a frame's lengths do not hold")
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            if _crc(frame[:-_TRAILER]) != struct.unpack(">I", frame[-_TRAILER:])[0]:
                raise EventStreamError("a message's checksum does not match")
            body = frame[_PRELUDE:-_TRAILER]
            out.append(Message(_headers(body[:header_size]), body[header_size:]))
        return out


def encode(headers: dict[str, str], payload: bytes) -> bytes:
    """One frame with text headers: what the tests' fake Bedrock streams."""
    raw = b""
    for name, value in headers.items():
        key, text = name.encode(), value.encode()
        raw += bytes([len(key)]) + key + bytes([_STRING]) + struct.pack(">H", len(text)) + text
    total = _PRELUDE + len(raw) + len(payload) + _TRAILER
    prelude = struct.pack(">II", total, len(raw))
    prelude += struct.pack(">I", _crc(prelude))
    message = prelude + raw + payload
    return message + struct.pack(">I", _crc(message))
