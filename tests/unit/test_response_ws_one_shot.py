"""An ordinary Responses create must reach real guarded WebSocket send."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Response as HandshakeResponse

from latet.gateway import request_transport, ws
from latet.gateway.capture import Capture, Exchange
from latet.gateway.outcome import AlreadyFinal, Forward, Pass
from latet.gateway.recorder import ResponseCapture
from latet.gateway.routes import RouteTable, Upstream

from .test_request_outcomes import _Client
from .test_response_finalizer_lifecycle import _Submissions
from .test_response_sink_failures import _TIMEOUT, _sinks
from .test_response_sinks import _records

_WIRE = 16_384
_TEXT = "ordinary response"


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


_CREATE = _json(
    {
        "type": "response.create",
        "model": "m",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Please reply briefly."}]}],
    }
)
_ANSWER = (
    _json({"type": "response.created", "response": {"id": "resp_one"}}),
    _json({"type": "response.output_text.delta", "item_id": "item_one", "content_index": 0, "delta": _TEXT}),
    _json({"type": "response.completed", "response": {"id": "resp_one", "status": "completed"}}),
)


class _BoundClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        first = self.incoming.get_nowait()
        self.incoming = asyncio.Queue(maxsize=3)
        self.incoming.put_nowait(first)
        self.bytes = 0

    async def _send(self, message: Any) -> None:
        if message["type"] == "websocket.send":
            self.bytes += len(message.get("text", "").encode()) + len(message.get("bytes", b""))
        if len(self.sent) >= 5 or self.bytes > _WIRE:
            pytest.fail("ASGI fixture wire bound", pytrace=False)
        await super()._send(message)


class _OpenedWire:
    """In-memory transport only. The real connect adapter owns its authority."""

    def __init__(self) -> None:
        self.response = HandshakeResponse(101, "Switching Protocols", Headers())
        self.incoming: asyncio.Queue[str] = asyncio.Queue(maxsize=3)
        self.transport = self
        self.close_code: int | None = None
        self.opens = self.sends = self.reads = self.closes = self.aborts = 0
        self.sent_create = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        frame = await self.incoming.get()
        self.reads += 1
        return frame

    async def send(self, frame: str | bytes) -> None:
        self.sends += 1
        if self.sends != 1 or not isinstance(frame, str) or len(frame.encode()) > _WIRE:
            pytest.fail("provider fixture send bound", pytrace=False)
        self.sent_create = json.loads(frame).get("type") == "response.create"
        for answer in _ANSWER:
            self.incoming.put_nowait(answer)

    async def close(self) -> None:
        self.closes += 1
        self.close_code = 1000

    def abort(self) -> None:
        self.aborts += 1


async def test_ordinary_create_reaches_guarded_send(tmp_path, monkeypatch):
    directory = tmp_path / "capture"
    client = _BoundClient()
    wire = _OpenedWire()
    loop = asyncio.get_running_loop()
    published = asyncio.Event()
    allowed = repeats = validations = send_calls = 0
    legacy, validate = ws.decide, request_transport.validate_frame
    send_frame, publish = request_transport.send_frame, ResponseCapture.publish
    if len(_CREATE.encode()) > _WIRE or sum(len(frame.encode()) for frame in _ANSWER) > _WIRE:
        pytest.fail("generated WebSocket fixture bound", pytrace=False)

    async def dial(_url, **_options):
        if request_transport.connecting() is None or wire.opens:
            pytest.fail("fixture requires one guarded connect", pytrace=False)
        wire.opens += 1
        return wire

    def observe_legacy(*args, **kwargs):
        nonlocal allowed, repeats
        try:
            decision = legacy(*args, **kwargs)
        except AlreadyFinal:
            repeats += 1
            raise
        allowed += isinstance(decision, (Forward, Pass))
        return decision

    def observe_validation(*args, **kwargs):
        nonlocal validations
        validations += 1
        return validate(*args, **kwargs)

    async def observe_send(*args, **kwargs):
        nonlocal send_calls
        send_calls += 1
        return await send_frame(*args, **kwargs)

    def observe_publish(capture: ResponseCapture, exchange: Exchange, status: int, *, terminal: bool) -> None:
        publish(capture, exchange, status, terminal=terminal)
        loop.call_soon_threadsafe(published.set)

    monkeypatch.setattr(request_transport, "_NoRedirect", dial)
    monkeypatch.setattr(ws, "decide", observe_legacy)
    monkeypatch.setattr(request_transport, "validate_frame", observe_validation)
    monkeypatch.setattr(request_transport, "send_frame", observe_send)
    monkeypatch.setattr(ResponseCapture, "publish", observe_publish)
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                "http://127.0.0.1:1",
                envelope_expected=frozenset({("model", "m")}),
            )
        ]
    )
    client.incoming.put_nowait({"type": "websocket.receive", "text": _CREATE})
    with _sinks(tmp_path) as (mediator, recorder, store):
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor)
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(directory), mediator, recorder))
        publication = asyncio.create_task(published.wait())
        disconnected = False
        try:
            done, _ = await asyncio.wait((relaying, publication), timeout=_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                pytest.fail("WebSocket relay reached neither completion nor publication", pytrace=False)
            if publication in done and not relaying.done():
                client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
                disconnected = True
            await asyncio.wait_for(asyncio.shield(relaying), _TIMEOUT)
        finally:
            publication.cancel()
            await asyncio.wait_for(asyncio.gather(publication, return_exceptions=True), _TIMEOUT)
            if not relaying.done() and not disconnected and not client.incoming.full():
                client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            if not relaying.done():
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(relaying), _TIMEOUT)
            if not relaying.done():
                relaying.cancel()
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(relaying), _TIMEOUT)

        assert recorder.drain(_TIMEOUT), "WebSocket history drain"
        summaries = store.list()[0]
        detail = store.detail(summaries[0]["id"]) if len(summaries) == 1 else None
        raw = _records(directory) if any(directory.glob("*.jsonl")) else []
        downstream = [row["data"] for row in raw if row["t"] == "ws" and row["dir"] == "down"]
        closes = [message["code"] for message in client.sent if message["type"] == "websocket.close"]
        accepted = sum(message["type"] == "websocket.accept" for message in client.sent) == 1
        causal_double_finalize = (
            accepted
            and wire.opens == 1
            and allowed == repeats == 1
            and validations == send_calls == wire.sends == wire.reads == 0
            and len(submissions.rows) == 0
            and submissions.other == 3
            and not summaries
            and not downstream
            and closes == [1011]
            and wire.closes == 1
            and not wire.aborts
        )
        checks = {
            "actual handshake accepted once": accepted and wire.opens == 1,
            "ordinary create was decided once": allowed == 1 and repeats == 0,
            "real frame validation and transport accepted create once": (
                validations == send_calls == wire.sends == 1 and wire.sent_create
            ),
            "ordinary downstream frames unchanged": wire.reads == 3 and tuple(client.texts()) == _ANSWER,
            "safe turn written once in both sinks": tuple(downstream) == _ANSWER
            and len(summaries) == 1
            and ((detail or {}).get("response") or {}).get("text") == _TEXT
            and len(submissions.rows) == 2
            and 4 <= submissions.other <= 7,
            "normal connection cleanup": closes == [1000] and wire.closes == 1 and not wire.aborts,
        }
        assert all(checks.values()), {
            "positive_contract": checks,
            "causal_double_finalize": causal_double_finalize,
            "allowed": allowed,
            "already_final": repeats,
            "validations": validations,
            "send_calls": send_calls,
            "wire_sends": wire.sends,
            "turn_records": len(summaries),
            "engine_other": submissions.other,
            "finalizer_operations": len(submissions.rows),
            "close_1011": closes == [1011],
        }
