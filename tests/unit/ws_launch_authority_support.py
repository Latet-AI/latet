"""Finite real WS protocol/ASGI helper; no listener or provider service."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from starlette.websockets import WebSocket
from websockets.client import ClientProtocol
from websockets.extensions.permessage_deflate import (
    ClientPerMessageDeflateFactory,
    ServerPerMessageDeflateFactory,
)
from websockets.frames import Frame, Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol
from websockets.uri import parse_uri

from latet.engine.pipeline import request_scope
from latet.gateway import request_transport, ws
from latet.gateway.capture import Capture
from latet.gateway.outcome import Forward, Pass
from latet.gateway.routes import RouteTable, Upstream

from .test_response_sink_failures import _TIMEOUT
from .test_response_ws_one_shot import _ANSWER, _CREATE, _BoundClient

_BOUND = 16_384
_CASES = ("verified", "missing-off", "end-after-dial", "end-before-frame")


class ProtocolWire(asyncio.Transport):
    """Actual ClientProtocol/_Checked paired with actual ServerProtocol in memory."""

    def __init__(self, connection):
        self.connection = connection
        self.server = ServerProtocol(extensions=[ServerPerMessageDeflateFactory()], max_size=_BOUND)
        self.closed = False
        self.handshakes = self.frames = self.bytes = self.answers = 0
        self.headers = ()

    def set_write_buffer_limits(self, high=None, low=None):
        pass

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass

    def is_closing(self):
        return self.closed

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            asyncio.get_running_loop().call_soon(self.connection.connection_lost, None)

    def abort(self):
        self.close()

    def write(self, data):
        self.bytes += data.nbytes if isinstance(data, memoryview) else len(data)
        assert not self.closed and self.bytes <= _BOUND
        self.server.receive_data(data.tobytes() if isinstance(data, memoryview) else data)
        for event in self.server.events_received():
            if isinstance(event, Request):
                self.handshakes += 1
                assert self.handshakes == 1
                self.headers = tuple(event.headers.raw_items())
                self.server.send_response(self.server.accept(event))
            elif isinstance(event, Frame) and event.opcode is Opcode.TEXT:
                self.frames += 1
                payload = event.data
                size = payload.nbytes if isinstance(payload, memoryview) else len(payload)
                assert self.frames == 1 and size <= _BOUND
                if isinstance(payload, memoryview):
                    payload = payload.tobytes()
                assert json.loads(payload)["type"] == "response.create"
                for text in _ANSWER:
                    self.server.send_text(text.encode())
        for reply in self.server.data_to_send():
            self.answers += len(reply)
            assert self.answers <= _BOUND
            if reply:
                asyncio.get_running_loop().call_soon(self.connection.data_received, reply)
            else:
                asyncio.get_running_loop().call_soon(self.close)


async def exercise(case, mediator, monkeypatch, app_factory, end, expected_scope, capture_dir=None):
    """One connection, one create, three answer events, two finite end gates; with ``capture_dir``, the relay's
    capture is enabled there (off by default, as before)."""
    assert case in _CASES and len(_CREATE.encode()) <= _BOUND
    assert sum(len(text.encode()) for text in _ANSWER) <= _BOUND
    client = _BoundClient()
    connected = asyncio.Event()
    result = asyncio.Event()
    wires = []
    seen = []
    scopes = []
    endings = []
    legacy = ws.decide
    send_frame = request_transport.send_frame
    submit = mediator.executor.submit
    engine_jobs = 0

    def submitted(function, *args, **kwargs):
        nonlocal engine_jobs
        engine_jobs += 1
        assert engine_jobs <= 10  # existing finite single-turn fixture profile
        return submit(function, *args, **kwargs)

    monkeypatch.setattr(mediator.executor, "submit", submitted)

    async def send(message):
        await client._send(message)
        if message["type"] in ("websocket.accept", "websocket.close"):
            connected.set()
        if message["type"] == "websocket.close":
            result.set()
        if message["type"] == "websocket.send":
            event = json.loads(message.get("text", "{}"))
            if event.get("type") in ("error", "response.completed"):
                result.set()

    def observed(arena, *args, **kwargs):
        decision = legacy(arena, *args, **kwargs)
        if arena is not None and isinstance(decision, (Forward, Pass)):
            selection = request_scope.get()
            seen.append(arena)
            scopes.append(
                selection is not None and selection.session is arena.session and selection.scope is expected_scope
            )
        return decision

    async def dial(url, **options):
        assert request_transport.connecting() is not None and not wires
        protocol = ClientProtocol(parse_uri(url), extensions=[ClientPerMessageDeflateFactory()], max_size=_BOUND)
        connection = options["create_connection"](protocol, ping_interval=None, close_timeout=1, max_queue=4)
        wire = ProtocolWire(connection)
        wires.append(wire)
        connection.connection_made(wire)
        if case == "end-after-dial":
            assert await end()
            endings.append("dial")
        try:
            # Actual _Checked override and real library send_context, never a forged successful open.
            await connection.handshake(options["additional_headers"], options["user_agent_header"])
        except BaseException:
            wire.abort()
            await asyncio.wait_for(connection.wait_closed(), _TIMEOUT)
            raise
        return connection

    async def sending(connection, authorized):
        assert seen and scopes == [True]
        if case == "end-before-frame":
            assert await end()
            endings.append("frame")
        return await send_frame(connection, authorized)

    monkeypatch.setattr(request_transport, "_NoRedirect", dial)
    monkeypatch.setattr(request_transport, "send_frame", sending)
    monkeypatch.setattr(ws, "decide", observed)
    route = Upstream(
        "relay",
        "/relay",
        "http://127.0.0.1:1",
        envelope_expected=frozenset({("model", "m")}),
    )

    async def relay(scope, receive, outbound):
        await ws.relay(WebSocket(scope, receive, outbound), RouteTable([route]), Capture(capture_dir), mediator)

    app = app_factory(relay)
    task = asyncio.create_task(app(client.socket.scope, client._receive, send))
    try:
        await asyncio.wait_for(connected.wait(), _TIMEOUT)
        accepted = any(message["type"] == "websocket.accept" for message in client.sent)
        if accepted:
            client.incoming.put_nowait({"type": "websocket.receive", "text": _CREATE})
        await asyncio.wait_for(result.wait(), _TIMEOUT)
        if not task.done():
            client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, _TIMEOUT)
        writes = sum(wire.handshakes for wire in wires)
        frames = sum(wire.frames for wire in wires)
        if case == "verified":
            assert accepted and writes == frames == 1 and scopes == [True]
            assert tuple(client.texts()) == _ANSWER
        elif case == "missing-off":
            assert not accepted and not wires and scopes == []
        elif case == "end-after-dial":
            assert len(wires) == 1 and writes == frames == 0 and not accepted and endings == ["dial"]
        else:
            assert accepted and writes == 1 and frames == 0 and scopes == [True] and endings == ["frame"]
        assert all(name.lower() != "x-latet-launch-key" for wire in wires for name, _ in wire.headers)
        assert len(seen) <= 1 and all(wire.closed for wire in wires)
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, _TIMEOUT)
        for wire in wires:
            wire.abort()
            await asyncio.wait_for(wire.connection.wait_closed(), _TIMEOUT)
        assert request_scope.get() is None
