"""Incremental server-sent events parsing and streaming Content-Encoding decoding.

An answer never inflates past what Latet reads: decoded, it holds at most ``MAX_DECODED`` bytes (a request's own bound),
one event of a stream as much, a stream at most ``MAX_EVENTS`` events, and past that it is not read further
(``ResponseTooLarge``, ``TooManyEvents``). A few kilobytes of gzip, brotli or zstd can hold gigabytes, and 32 MiB hold
600,000 events of a few bytes, each restored apart: on a server, a provider an identity runs could send them to the
instance every identity shares."""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, cast

import brotli
import zstandard

from latet.engine import wellformed
from latet.protocols.codec import MEMBERS_KEPT, UnsupportedEncodingError

MAX_DECODED = 32 * 1024 * 1024  # bytes of an answer Latet reads at most, once decoded: a request's own bound
# Events of one stream Latet reads at most. From real streams: a provider sends about one event for each token it writes
# (OpenAI's chat and Responses APIs one a token, Anthropic's one for a few), and a model writes 128,000 tokens at most
# (the largest outputs of GPT-5 and of Claude), with a few events around each block: a long answer has thousands of
# events, the longest a little over a hundred thousand. Past this many the answer is stopped with a clear error.
MAX_EVENTS = 200_000
# Bytes a decoder gives at a time, at most (brotli: about twice that), and is handed at a time, at most: what a stream
# restores between two turns of the event loop is that much, and a chunk of any size costs time in proportion to it.
_STEP = 64 * 1024
_ZSTD_WINDOW = 8 * 1024 * 1024  # the most a zstd frame of an HTTP answer may ask a decoder to keep (RFC 9659)
_GZIP = 16 + zlib.MAX_WBITS  # zlib reading a gzip member, header and trailer
_EVENT_END = re.compile(b"\r\n\r\n|\n\n")  # what ends an event, whichever comes first


class ResponseError(ValueError):
    """An answer Latet does not pass on: it does not decode as its content-encoding says, or it is larger than Latet
    reads. Its words are fixed: they hold nothing of the answer."""


class ResponseTooLarge(ResponseError):
    """Decoded, an answer (or one event of it) is larger than Latet reads."""


def too_large(limit: int) -> ResponseTooLarge:
    return ResponseTooLarge(f"the provider's answer, decoded, was larger than Latet reads ({limit / 2**20:g} MiB)")


class TooManyEvents(ResponseError):
    """A stream that carries more events than Latet reads."""


def too_many(limit: int) -> TooManyEvents:
    return TooManyEvents(f"the provider's answer carried more events than Latet reads ({limit:,})")


@dataclass(slots=True)
class SSEEvent:
    raw: bytes  # the event exactly as received, terminator included
    event: str | None
    data: str | None
    newline: bytes

    def json(self) -> Any | None:
        if self.data is None:
            return None
        try:
            return wellformed.loads(self.data)
        except ValueError:
            return None


def serialize(event: str | None, data: str, newline: bytes = b"\n") -> bytes:
    lines = []
    if event is not None:
        lines.append(f"event: {event}".encode())
    for line in data.split("\n"):
        lines.append(f"data: {line}".encode())
    return newline.join(lines) + newline + newline


class SSEParser:
    """Feeds bytes, yields complete events. Comments and unknown fields stay in ``raw``. An event that has not ended
    once it holds ``limit`` bytes (``MAX_DECODED``) is not read further: ``ResponseTooLarge``; nor a stream past
    ``events`` events (``MAX_EVENTS``): ``TooManyEvents``. Each byte is looked at once, however the events come cut, and
    however many a piece holds."""

    def __init__(self, limit: int | None = None, events: int | None = None) -> None:
        self._buf = bytearray()
        self._seen = 0  # how far the buffer holds no end of an event
        self.limit = MAX_DECODED if limit is None else limit
        self.events = MAX_EVENTS if events is None else events
        self._count = 0  # events read so far

    @property
    def held(self) -> int:
        """The bytes of an event begun and not ended: what the next piece may end at once, with its own."""
        return len(self._buf)

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buf += chunk
        events = []
        start = 0
        while True:
            cut, sep = self._boundary(start)
            if cut < 0:
                break
            self._count += 1
            if self._count > self.events:
                raise too_many(self.events)
            end = cut + len(sep)
            events.append(_parse(bytes(self._buf[start:end]), b"\r\n" if sep == b"\r\n\r\n" else b"\n"))
            start = self._seen = end
        del self._buf[:start]
        self._seen -= start
        if len(self._buf) > self.limit:
            raise too_large(self.limit)
        return events

    def _boundary(self, start: int) -> tuple[int, bytes]:
        """Where the next event ends, and with what: the first of its two ends after ``start``, looked for in one pass
        that stops there (two searches would each read on to the buffer's end at every event)."""
        at = max(start, self._seen - 3)  # an end cut between two feeds is found whole
        found = _EVENT_END.search(self._buf, at)
        if found is None:
            self._seen = len(self._buf)
            return -1, b""
        return found.start(), found.group()

    def rest(self) -> bytes:
        rest, self._buf, self._seen = bytes(self._buf), bytearray(), 0
        return rest


def _parse(raw: bytes, newline: bytes) -> SSEEvent:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    return SSEEvent(raw, event_name, "\n".join(data_lines) if data_lines else None, newline)


class _Kept:
    """Where zstandard's writer puts what it decodes, a piece at a time: kept for the decoder, counted as it comes."""

    def __init__(self, decoder: StreamDecoder) -> None:
        self.decoder = decoder
        self.pieces: list[bytes] = []

    def write(self, data: Any) -> int:
        self.pieces.append(self.decoder._counted(bytes(data)))
        return len(data)


class StreamDecoder:
    """Undo a response Content-Encoding chunk by chunk, a piece of at most ``_STEP`` bytes or so at a time, the decoder
    handed a piece of the chunk at most, and never more than ``limit`` bytes in all (``MAX_DECODED``): past that
    ``ResponseTooLarge``, before any more is decoded. A gzip answer is read member after member, as Python's gzip reads
    it, ``MEMBERS_KEPT`` at most. An answer that does not decode as it says (what follows a gzip member and is no
    member) is a ``ResponseError``, never cut short silently. One in no encoding goes through as it comes: it costs its
    sender every byte, and whoever buffers it bounds it."""

    def __init__(self, content_encoding: str | None, limit: int | None = None) -> None:
        coding = (content_encoding or "identity").strip().lower()
        self.limit = MAX_DECODED if limit is None else limit
        self.total = 0  # bytes decoded so far
        self._obj: Any = None
        self._kept: _Kept | None = None
        self._gzip = coding in ("gzip", "x-gzip")
        self._members = 1  # gzip members begun
        if coding in ("", "identity"):
            self._obj = None
        elif self._gzip:
            self._obj = zlib.decompressobj(_GZIP)
        elif coding == "deflate":
            self._obj = zlib.decompressobj()
        elif coding == "br":
            self._obj = brotli.Decompressor()
        elif coding == "zstd":
            # Its writer hands what it decodes on a piece at a time, and decodes every frame of the answer.
            self._kept = _Kept(self)
            dctx = zstandard.ZstdDecompressor(max_window_size=_ZSTD_WINDOW)
            sink = cast("IO[bytes]", self._kept)  # it writes there alone
            self._obj = dctx.stream_writer(sink, write_size=_STEP, write_return_read=True)
        else:
            raise UnsupportedEncodingError(coding)
        self._brotli = coding == "br"

    def _counted(self, piece: bytes) -> bytes:
        self.total += len(piece)
        if self.total > self.limit:
            raise too_large(self.limit)
        return piece

    def pieces(self, chunk: bytes) -> Iterator[bytes]:
        """What ``chunk`` decodes to, a piece at a time."""
        if self._obj is None:
            if chunk:
                yield chunk
            return
        try:
            yield from self._decoded(chunk)
        except ResponseError:
            raise
        except (zlib.error, brotli.error, zstandard.ZstdError):
            # Its words name what the library found, nothing of the answer; the answer is not passed on all the same.
            raise ResponseError("the provider's answer did not decode as its content-encoding says") from None

    def _decoded(self, chunk: bytes) -> Iterator[bytes]:
        """What ``chunk`` decodes to, its decoder handed a piece of it at most at each call (zlib hands back what it
        could not read yet: handed a whole chunk of megabytes, it copied the rest of it at every step)."""
        view = memoryview(chunk)
        for start in range(0, len(view), _STEP):
            yield from self._decoded_piece(view[start : start + _STEP])

    def _decoded_piece(self, piece: memoryview) -> Iterator[bytes]:
        if self._kept is not None:
            failed: BaseException | None = None
            try:
                self._obj.write(piece)
            except Exception as exc:  # handed on below, after what was decoded within the bound
                failed = exc
            pieces, self._kept.pieces = self._kept.pieces, []
            yield from pieces
            if failed is not None:
                raise failed
        elif self._brotli:
            # It keeps what it decoded past a step, even once it took all its input, and hands it on when asked again
            # with no more: asked until it hands on nothing (then it takes more input).
            out = self._obj.process(piece, output_buffer_limit=_STEP)
            while out:
                yield self._counted(out)
                out = self._obj.process(b"", output_buffer_limit=_STEP)
        else:
            data: bytes | memoryview = piece
            while data:
                if self._obj.eof:
                    if not self._gzip:
                        return  # what follows a zlib stream's end is no part of it
                    self._next_member()  # what follows a gzip member is the next one's, or the answer is refused
                yield self._counted(self._obj.decompress(data, _STEP))
                data = self._obj.unconsumed_tail or (self._obj.unused_data if self._obj.eof else b"")

    def _next_member(self) -> None:
        self._members += 1
        if self._members > MEMBERS_KEPT:
            raise ResponseError(f"the provider's answer holds more gzip members than Latet reads ({MEMBERS_KEPT})")
        self._obj = zlib.decompressobj(_GZIP)

    def feed(self, chunk: bytes) -> bytes:
        return b"".join(self.pieces(chunk))

    def flush(self) -> bytes:
        if self._obj is None or self._brotli or self._kept is not None:
            return b""
        try:
            return self._counted(self._obj.flush())  # what the window still holds: its input was all decoded
        except zlib.error:
            raise ResponseError("the provider's answer did not decode as its content-encoding says") from None
