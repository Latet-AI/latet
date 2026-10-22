"""The tutorial's pretend model.

It answers each request with the step's next scripted turn, streamed as Anthropic's Messages API streams: text
in deltas of a few characters (a fake is always split across events) and a tool call's input in pieces. What
it says comes from what it received: a turn's `{{received:NAME}}` is the group of the chapter's pattern NAME in
the request, so the model only ever knows the fakes. The gateway reaches it in-process, through the client it
is given: nothing leaves the computer.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from latet.gateway.routes import Upstream
from latet.tutorial.chapters import Capture, NotReceived, Turn, render, render_json

NAME: Final = "Tutorial Model"
MODEL_ID: Final = "tutorial-model"
PRETEND_URL: Final = "http://pretend-model.invalid"  # a name no resolver answers: never reached over the network
TEXT_PIECE: Final = 4  # characters of text per event
ARGS_PIECE: Final = 7  # characters of a tool call's input per event
_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@dataclass(frozen=True, slots=True)
class PretendUpstream(Upstream):
    """The sandbox's only route: `/anthropic/...` goes to the pretend model, never over a WebSocket."""

    websocket: ClassVar[bool] = False
    name: str = "anthropic"
    prefix: str = "/anthropic"
    base_url: str = PRETEND_URL


@dataclass(slots=True)
class Received:
    """A request as the model received it, and how it answered: the turn, None when it could not."""

    body: bytes
    turn: str | None
    answer: list[dict[str, Any]]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class PretendModel:
    """Answers `POST /v1/messages` with the next turn of those it expects; anything else is not found."""

    def __init__(self) -> None:
        self.received: list[Received] = []
        self.captured: dict[str, str] = {}  # what each pattern found last
        self.served: dict[str, str] = {}  # what it said in each turn, as it sent it (with the fakes it received)
        self._turns: list[Turn] = []
        self._captures: dict[str, Capture] = {}
        self._dropped: list[str] = []  # turns a step never used
        self.app = Starlette(routes=[Route("/{path:path}", self._answer, methods=_METHODS)])

    def client(self) -> httpx.AsyncClient:
        """The client the gateway reaches the model with: in-process, no network."""
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), trust_env=False)

    def expect(self, turns: Sequence[Turn], captures: Mapping[str, Capture]) -> None:
        """Answer the next requests with ``turns``, in order; ``captures``: the chapter's `received` patterns."""
        self._dropped += self.queued()
        self._turns = list(turns)
        self._captures = dict(captures)

    def queued(self) -> list[str]:
        """The turns still to answer with."""
        return [turn.id for turn in self._turns]

    def leftover(self) -> list[str]:
        """The turns never used: those still queued, and those a later `expect` dropped."""
        return [*self._dropped, *self.queued()]

    def reset(self) -> None:
        self.received.clear()
        self.captured.clear()
        self.served.clear()
        self._turns, self._captures, self._dropped = [], {}, []

    async def _answer(self, request: Request) -> Response:
        if request.method != "POST" or request.url.path != "/v1/messages":
            return _error(404, "not_found_error", f"{NAME} answers POST /v1/messages only")
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.received.append(Received(raw, None, []))
            return _error(400, "invalid_request_error", "the body is not a JSON object")
        self._capture(body)
        if not self._turns:
            self.received.append(Received(raw, None, []))
            return _error(500, "api_error", f"{NAME} has nothing more to say: no turn is left for this request")
        turn = self._turns[0]
        try:
            answer = _blocks(turn, self.captured)
        except NotReceived as missing:
            self.received.append(Received(raw, None, []))
            message = f"{NAME} never received {missing.name}: nothing matched its pattern"
            return _error(500, "api_error", message)
        self._turns.pop(0)
        self.received.append(Received(raw, turn.id, answer))
        for block in answer:
            if block["type"] == "text":
                self.served[turn.id] = block["text"]
        return StreamingResponse(_stream(turn.id, answer), media_type="text/event-stream")

    def _capture(self, body: dict[str, Any]) -> None:
        """What each pattern finds: its group in the latest text of its kind where it matches with something in
        it (a group left out or empty found nothing)."""
        texts = _texts(body)
        for name, capture in self._captures.items():
            if capture.pattern is None:  # a capture by value: a pattern once its chapter is loaded, never here
                continue
            pattern = re.compile(capture.pattern)
            for kind, text in reversed(texts):
                if capture.where != "any" and capture.where != kind:
                    continue
                found = next((group for m in pattern.finditer(text) if (group := m.group(1))), None)
                if found is not None:
                    self.captured[name] = found
                    break


def _texts(body: dict[str, Any]) -> list[tuple[str, str]]:
    """The texts of a request, oldest first: what the user typed (`user`), what the tools returned
    (`tool_result`), and the rest (the system prompt, the model's earlier answers)."""
    texts: list[tuple[str, str]] = []
    system = body.get("system")
    if isinstance(system, str):
        texts.append(("other", system))
    elif isinstance(system, list):
        texts += [("other", b["text"]) for b in system if isinstance(b, dict) and isinstance(b.get("text"), str)]
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        typed = "user" if message.get("role") == "user" else "other"
        content = message.get("content")
        if isinstance(content, str):
            texts.append((typed, content))
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append((typed, block["text"]))
            elif block.get("type") == "tool_use":
                texts.append(("other", json.dumps(block.get("input"))))
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                parts = (
                    [inner] if isinstance(inner, str) else [b.get("text") for b in inner or [] if isinstance(b, dict)]
                )
                texts += [("tool_result", part) for part in parts if isinstance(part, str)]
    return texts


def _blocks(turn: Turn, received: Mapping[str, str]) -> list[dict[str, Any]]:
    """A turn's answer, with what the model received in place of its placeholders."""
    blocks: list[dict[str, Any]] = []
    if turn.text is not None:
        blocks.append({"type": "text", "text": render(turn.text, received)})
    if turn.tool is not None:
        tool_input = render_json(turn.tool.input, received)
        blocks.append({"type": "tool_use", "id": f"toolu_{turn.id}", "name": turn.tool.name, "input": tool_input})
    return blocks


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def _pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _stream(turn: str, blocks: list[dict[str, Any]]) -> AsyncIterator[bytes]:
    """An answer as Anthropic streams it: text in deltas of TEXT_PIECE characters, a tool call's input in
    `partial_json` pieces of ARGS_PIECE."""
    message = {"id": f"msg_{turn}", "type": "message", "role": "assistant", "model": MODEL_ID, "content": []}
    usage = {"input_tokens": 1, "output_tokens": 1}
    yield _sse({"type": "message_start", "message": {**message, "stop_reason": None, "usage": usage}})
    yield _sse({"type": "ping"})
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            yield _sse({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            for piece in _pieces(block["text"], TEXT_PIECE):
                yield _sse(
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}}
                )
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            yield _sse({"type": "content_block_start", "index": index, "content_block": start})
            for piece in _pieces(json.dumps(block["input"]), ARGS_PIECE):
                delta = {"type": "input_json_delta", "partial_json": piece}
                yield _sse({"type": "content_block_delta", "index": index, "delta": delta})
        yield _sse({"type": "content_block_stop", "index": index})
    stop = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else "end_turn"
    yield _sse({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 1}})
    yield _sse({"type": "message_stop"})


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)
