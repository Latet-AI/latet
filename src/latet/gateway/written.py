"""What the model wrote, in an answer Latet passes on as it came: a request it only checks, Claude Code's.

The final check of such a request passes a text of the model's only when it is exactly what the model wrote
(``Verifier.wrote``): its own words, written from the fakes it was shown, such as a password it makes up for a test,
which a tool's answer may show again and the vault then learn. Each text block, and each string of each tool call's
input, is read as the answer passes, Anthropic's stream, Bedrock's event-stream or a whole body, and is known in its
conversation before the bytes that end it are passed on: the request the client makes after it finds it known. At most
``WRITTEN_BYTES`` of an answer are read, decoded, and ``WRITTEN_EVENTS`` events: past that, or in an answer that does
not read, nothing more is taken, and what the model wrote after is checked as a client's text is.

An answer is read a piece of ``STEP`` bytes at a time (``pieces``, then ``read`` for each and ``end``), and each step
says first what it may parse at once (``load``): a whole body, an event begun megabytes before, a tool call's input
joined from its pieces. The gateway reads a step that loads more than a little on the engine's thread (on a Latet
server, the identity's own), never on the event loop every identity's streams share (``Mediator.read_written``).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from latet.engine import wellformed
from latet.protocols.codec import UnsupportedEncodingError
from latet.protocols.eventstream import EventStreamDecoder
from latet.protocols.sse import SSEParser, StreamDecoder

WRITTEN_BYTES = 8 * 1024 * 1024  # of one answer, decoded, read at most
WRITTEN_EVENTS = 200_000  # of one stream, read at most
STEP = 64 * 1024  # bytes read in one step, at most: the event loop serves others between two


def strings(value: Any) -> list[str]:
    """Every string of a JSON value, its keys left out: what a request carries of a tool call's input, each its own
    text (``protocols.base.string_leaves``)."""
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return out


@dataclass(slots=True)
class _Block:
    """A block begun and not ended: a text and its pieces, or a tool call's input as it began and the pieces of its
    JSON; and the bytes it holds, which its end joins and reads at once."""

    kind: str  # "text" or "tool"
    pieces: list[str] = field(default_factory=list)
    given: Any = None
    size: int = 0


class Written:
    """The texts one answer's model wrote, read as the answer passes (``pieces``, ``read``, ``end``; or ``steps``)."""

    def __init__(self, content_type: str, content_encoding: str | None) -> None:
        self._sse = "text/event-stream" in content_type
        self._frames = EventStreamDecoder() if "vnd.amazon.eventstream" in content_type else None
        self._whole = not self._sse and self._frames is None and "json" in content_type
        self._events = SSEParser(limit=WRITTEN_BYTES, events=WRITTEN_EVENTS)
        self._body = bytearray()
        self._size = 0  # bytes read, decoded
        self._count = 0  # events read (Bedrock's)
        self._blocks: dict[int, _Block] = {}  # the blocks begun and not ended, by their index
        self._open = 0  # the bytes they hold
        self._decoder: StreamDecoder | None = None
        with contextlib.suppress(UnsupportedEncodingError):  # an encoding Latet does not read: nothing is taken
            self._decoder = StreamDecoder(content_encoding, limit=WRITTEN_BYTES)
        self.broken = self._decoder is None or not (self._sse or self._frames is not None or self._whole)

    def steps(self, chunk: bytes | None) -> Iterator[list[str]]:
        """The texts each step of ``chunk`` ends, a step for each ``STEP`` bytes it decodes to; None: the answer's end.
        An answer that does not decode or read, or past what is read of it, ends nothing more (``broken``)."""
        for piece in self.pieces(chunk):
            yield self.read(piece)
        if chunk is None and not self.broken:
            yield self.end()

    def pieces(self, chunk: bytes | None) -> Iterator[bytes]:
        """What ``chunk`` decodes to (None: what the decoder still holds at the answer's end), ``STEP`` bytes at most at
        a time, each for ``read``. Nothing once the answer is ``broken``; one that does not decode is broken."""
        if self.broken or self._decoder is None:
            return
        try:
            decoded = [self._decoder.flush()] if chunk is None else self._decoder.pieces(chunk)
            for piece in decoded:
                for start in range(0, len(piece), STEP):
                    if self.broken:
                        return
                    yield piece[start : start + STEP]
        except Exception:  # ResponseError: nothing more is taken
            self._break()

    def load(self, piece: bytes | None) -> int:
        """The bytes the step of ``piece`` (None: the answer's end, ``end``) may parse and record at once: the piece and
        what it may end with it, an event or a frame begun before and the blocks begun and not ended; a whole body once
        it is all there."""
        if self._whole:
            return len(self._body) if piece is None else 0
        held = self._frames.held if self._frames is not None else self._events.held
        return held + self._open + (len(piece) if piece is not None else 0)

    def read(self, piece: bytes) -> list[str]:
        """The texts ``piece`` ends (one of ``pieces``)."""
        if self.broken:
            return []
        try:
            return self._read(piece)
        except Exception:  # EventStreamError, past what is read of it, a payload that is no JSON: nothing more is taken
            self._break()
            return []

    def end(self) -> list[str]:
        """The texts the answer's end ends, once what ``pieces(None)`` gave was read: a whole body's."""
        if self.broken:
            return []
        try:
            return self._end()
        except Exception:
            self._break()
            return []

    def _break(self) -> None:
        self.broken = True
        self._blocks.clear()
        self._open = 0
        self._body = bytearray()

    def _read(self, piece: bytes) -> list[str]:
        self._size += len(piece)
        if self._size > WRITTEN_BYTES:
            raise ValueError("more than is read of an answer")
        out: list[str] = []
        if self._sse:
            for event in self._events.feed(piece):
                if event.data and event.data != "[DONE]":
                    out += self._event(event.data)
        elif self._frames is not None:
            for message in self._frames.feed(piece):
                if message.headers.get(":message-type") == "event" and message.headers.get(":event-type") == "chunk":
                    self._count += 1
                    if self._count > WRITTEN_EVENTS:
                        raise ValueError("more events than are read")
                    out += self._event(_chunk(message.payload))
        else:
            self._body += piece
        return out

    def _end(self) -> list[str]:
        """A whole body's texts, once it is all there."""
        if not self._whole or not self._body:
            return []
        try:
            answer = wellformed.loads(bytes(self._body))
        except ValueError:
            return []
        content = answer.get("content") if isinstance(answer, dict) else None
        out: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
            elif block.get("type") == "tool_use":
                out += strings(block.get("input"))
        return out

    def _event(self, data: str) -> list[str]:
        """One of Anthropic's stream events: the texts it ends."""
        try:
            event = wellformed.loads(data)
        except ValueError:
            return []
        if not isinstance(event, dict) or not isinstance(index := event.get("index"), int):
            return []
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block")
            if not isinstance(block, dict):
                return []
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                self._begin(index, _Block("text", [block["text"]], size=len(block["text"])))
            elif block.get("type") == "tool_use":
                self._begin(index, _Block("tool", given=block.get("input"), size=len(data)))
        elif etype == "content_block_delta":
            found, delta = self._blocks.get(index), event.get("delta")
            if found is None or not isinstance(delta, dict):
                return []
            wanted, name = {"text": ("text_delta", "text"), "tool": ("input_json_delta", "partial_json")}[found.kind]
            if delta.get("type") == wanted and isinstance(piece := delta.get(name), str):
                found.pieces.append(piece)
                found.size += len(piece)
                self._open += len(piece)
        elif etype == "content_block_stop" and (found := self._blocks.pop(index, None)) is not None:
            self._open -= found.size
            return _ended(found)
        return []

    def _begin(self, index: int, block: _Block) -> None:
        """A block begun at ``index``: one begun there before and never ended is forgotten."""
        if (before := self._blocks.pop(index, None)) is not None:
            self._open -= before.size
        self._blocks[index] = block
        self._open += block.size


def _chunk(payload: bytes) -> str:
    """The stream event a frame of Bedrock's carries, in base64."""
    try:
        given = json.loads(payload)
        return base64.b64decode(given["bytes"], validate=True).decode("utf-8", "replace")
    except (ValueError, KeyError, TypeError, binascii.Error):
        return ""


def _ended(block: _Block) -> list[str]:
    """A block's texts once it ended: a text whole, or each string of a tool call's input."""
    if block.kind == "text":
        return ["".join(block.pieces)]
    raw = "".join(block.pieces)
    if not raw:
        return strings(block.given)
    try:
        return strings(wellformed.loads(raw))
    except ValueError:
        return []
