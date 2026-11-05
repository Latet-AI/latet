"""S3 and S4 behind the guarded transport (P1-4 of ``notes/server-request-coverage-plan.md``, RC-REC-9): a WebSocket's
handshake is decided before anything connects (Phase 1: the legacy rule's ``Pass``, nothing of it inspected yet),
authorized once, and opened only by the transport's WebSocket adapter, which sees the handshake websockets builds
(``Request``: its resource name and headers) before a byte of it is written and refuses one that is not what was
authorized; each frame has its own decision, authorized for the connection it goes on; the refusals made before a
socket's content is read are minted ``Block``s. Nothing is sent, refused, recorded or logged differently (P1-1)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.client import connect
from websockets.sync.server import serve

from latet.gateway import outcome, request_policy, request_profiles, request_transport, ws
from latet.gateway.capture import Capture
from latet.gateway.coverage import Envelope, EnvelopeProof, envelope_of, frame_envelope, inventory
from latet.gateway.mediate import Prepared
from latet.gateway.outcome import Block, Forward
from latet.gateway.routes import RouteTable, Upstream
from latet.server.egress import EgressRefused

from ..request_send_guard import SendGuard, UnguardedSend, installed
from .test_request_outcomes import EMAIL, _Client, _mediator, _until, _Upstream


def _handshake(destination: str = "relay") -> Envelope:
    scope = {"type": "websocket", "path": "/v1/responses", "headers": [], "query_string": b""}
    return envelope_of(scope, "ws-handshake", destination, connection=10**9)


def _connect_proof(url: str):
    parts = urlsplit(url)
    upstream = Upstream("relay", "", f"http://{parts.netloc}")
    envelope = _handshake()
    mediator = _mediator()
    session = mediator.engine.session("ws-guard")
    admission = request_profiles.admit(inventory(envelope), upstream)
    arena = mediator.prepare_handshake(upstream, parts.path, {}, admission.checked, session)
    proof = EnvelopeProof(
        envelope,
        len(admission.inventory.entries),
        admission.profile.id,
        admission.treatments,
        admission.headers,
        url,
        admission.checked,
        admission.removed,
    )
    decision = outcome.finalize(
        arena,
        envelope,
        request_policy.current(),
        outcome.response_plan(arena),
        envelope_proof=proof,
    )
    assert isinstance(decision, Forward)
    return envelope, arena, decision


def _frame_proof(payload: str):
    mediator = _mediator()
    session = mediator.engine.session("ws-guard")
    upstream = Upstream("relay", "", "http://127.0.0.1:1")
    sent, arena = mediator.mask_ws_frame(payload, session, upstream, "/v1/responses")
    envelope = frame_envelope(_handshake(), payload)
    decision = outcome.decide(arena, envelope, payload=sent)
    assert isinstance(decision, Forward)
    return envelope, arena, decision


@pytest.mark.parametrize("on_server", [False, True], ids=["a computer", "a server"])
async def test_a_handshake_is_decided_before_it_connects_and_the_adapter_sees_the_built_request(on_server, monkeypatch):
    mediator = _mediator()
    mediator.on_server = on_server
    order: list[Any] = []
    opened: list[Any] = []
    real_legacy, real_finalize, real_connect = ws.decide, ws.finalize, request_transport.connect_ws

    def kept(arena: Prepared | None, envelope: Envelope, **kwargs: Any) -> Any:
        decision = real_legacy(arena, envelope, **kwargs)
        order.append((envelope.transport, decision))
        return decision

    def finalized(arena: Prepared, envelope: Envelope, *args: Any, **kwargs: Any) -> Any:
        decision = real_finalize(arena, envelope, *args, **kwargs)
        order.append((envelope.transport, decision))
        return decision

    async def connecting(authorized: Any, url: str, pinned: Any, *bound: Any) -> Any:
        order.append(("connect", authorized.decision))
        opened.append(await real_connect(authorized, url, pinned, *bound))
        return opened[-1]

    monkeypatch.setattr(ws, "decide", kept)
    monkeypatch.setattr(ws, "finalize", finalized)
    monkeypatch.setattr(request_transport, "connect_ws", connecting)
    upstream, client = _Upstream(), _Client()
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                f"http://127.0.0.1:{upstream.port}",
                envelope_expected=frozenset(),
            )
        ]
    )
    turn = json.dumps({"type": "response.create", "model": "gpt-4o", "input": f"write to {EMAIL}"})
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), mediator))
        client.incoming.put_nowait({"type": "websocket.receive", "text": turn})
        await _until(lambda: len(client.texts()) == 1)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relaying, 10)
    finally:
        upstream.close()
    (first, shook), (second, connected), (third, frame) = order
    assert (first, second, third) == ("ws-handshake", "connect", "ws-frame")  # decided, then connected, then the turn
    assert type(shook) is Forward and connected is shook and shook.envelope_proof is not None and shook.payload == b""
    assert (
        type(frame) is outcome.Forward
        and frame.payload == upstream.got[0]
        and isinstance(frame.payload, str)
        and EMAIL not in frame.payload
    )
    final = opened[0].final  # the handshake websockets built, as the adapter saw it before it was written
    names = [name.decode("latin-1").lower() for name, _ in final.headers]
    assert (
        final.method == "GET"
        and final.url == b"/v1/responses"
        and (b"session-id", b"5f4a3e00-0000-4000-8000-00000000f4a3") in final.headers
    )
    assert set(names) - {"session-id"} <= request_transport.WS_LIBRARY_HEADERS and "user-agent" not in names


async def test_a_handshake_built_otherwise_than_authorized_is_not_written(monkeypatch):
    """RC-REC-9: the library's additions allowed by name alone; a header it adds that is not among them (here, the
    extension it negotiates, struck from the list for the test) refuses the handshake before a byte is written."""
    reached: list[bytes] = []

    async def heard(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reached.append(await reader.read(1024))
        writer.close()

    server = await asyncio.start_server(heard, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        request_transport, "WS_LIBRARY_HEADERS", request_transport.WS_LIBRARY_HEADERS - {"sec-websocket-extensions"}
    )
    handshake, arena, decision = _connect_proof(f"ws://127.0.0.1:{port}/v1/responses")
    authorized = request_transport.validate_connect(decision, handshake, request_policy.current(), [], arena)
    assert isinstance(authorized, request_transport.AuthorizedConnect)
    assert request_transport.validate_connect(decision, handshake, request_policy.current(), [], arena) == (
        request_transport.SendRefusal("spent")  # a handshake's decision authorizes one connection
    )
    try:
        refused = await request_transport.connect_ws(authorized, f"ws://127.0.0.1:{port}/v1/responses", None)
        again = await request_transport.connect_ws(authorized, f"ws://127.0.0.1:{port}/v1/responses", None)
    finally:
        server.close()
        await server.wait_closed()
    assert refused == request_transport.SendRefusal("changed") and again == request_transport.SendRefusal(
        "unauthorized"
    )
    assert reached in ([], [b""])  # nothing of the handshake written


class _Quick:
    """A WebSocket server on 127.0.0.1 that keeps what it is sent, waits briefly for a handshake (``open_timeout``),
    and is shut down before its socket goes."""

    def __init__(self) -> None:
        self.got: list[str | bytes] = []
        self.server = serve(self._answer, "127.0.0.1", 0, open_timeout=1, close_timeout=1)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()
        print(f"P24_PORT_OPEN {self.port}")

    def _serve(self) -> None:
        with contextlib.suppress(OSError):  # Windows: its select meets the socket shutdown closed (WinError 10038)
            self.server.serve_forever()

    def _answer(self, connection: Any) -> None:
        with contextlib.suppress(Exception):
            for message in connection:
                self.got.append(message)

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        print(f"P24_PORT_CLOSED {self.port}")


async def _opened(url: str) -> Any:
    handshake, arena, decision = _connect_proof(url)
    authorized = request_transport.validate_connect(decision, handshake, request_policy.current(), [], arena)
    assert isinstance(authorized, request_transport.AuthorizedConnect)
    connection = await request_transport.connect_ws(authorized, url, None)
    assert not isinstance(connection, request_transport.SendRefusal)
    return connection


async def test_a_frame_is_authorized_once_and_sent_once_on_the_connection_it_was_authorized_for():
    """The review's M-1: a frame's decision authorizes one frame, for one connection the adapter opened: a second
    authorization is spent, a second send unauthorized, a send on another connection refused (``client``)."""
    server = _Quick()
    url = f"ws://127.0.0.1:{server.port}/v1/responses"
    payload = '{"type":"response.cancel"}'
    frame, arena, decision = _frame_proof(payload)
    policy = request_policy.current()
    try:
        first, second = await _opened(url), await _opened(url)
        assert request_transport.validate_frame(decision, arena, frame, policy, object()) == (
            request_transport.SendRefusal("unbuilt")  # not a connection the adapter opened
        )
        authorized = request_transport.validate_frame(decision, arena, frame, policy, first)
        assert isinstance(authorized, request_transport.AuthorizedFrame)
        assert request_transport.validate_frame(decision, arena, frame, policy, first) == (
            request_transport.SendRefusal("spent")
        )
        other_frame, other_arena, other = _frame_proof(payload)
        elsewhere = request_transport.validate_frame(other, other_arena, other_frame, policy, first)
        assert isinstance(elsewhere, request_transport.AuthorizedFrame)
        assert await request_transport.send_frame(second, elsewhere) == request_transport.SendRefusal("client")
        assert await request_transport.send_frame(first, authorized) is None
        assert await request_transport.send_frame(first, authorized) == request_transport.SendRefusal("unauthorized")
        await asyncio.sleep(0.2)
        for connection in (first, second):
            connection.transport.abort()
    finally:
        server.close()
    assert server.got == [payload]  # sent once, and only as authorized
    assert isinstance(outcome.decide(None, _handshake()), Block)


async def test_the_guard_passes_only_the_handshake_the_adapter_opens_with_its_own_token():
    upstream = _Quick()
    url = f"ws://127.0.0.1:{upstream.port}/v1/responses"
    scratch: dict[str, Any] = {"__name__": "latet.gateway.scratch_ws", "connect": connect}
    exec("async def opened(url):\n    return await connect(url, user_agent_header=None)", scratch)
    guard = SendGuard(checking=True)
    try:
        with installed(guard):
            connection = await _opened(url)
            connection.transport.abort()  # no closing handshake to wait for
            with pytest.raises(UnguardedSend):  # Latet's code opening one by itself
                await scratch["opened"](url)
    finally:
        upstream.close()
    assert [line.split(" without")[0] for line in guard.refused] == [
        "ws connection from latet.gateway.scratch_ws.opened"
    ]
    assert "request_send_guard" in sys.modules[SendGuard.__module__].__name__


async def test_each_websocket_refusal_before_content_is_read_is_a_minted_block(monkeypatch):
    """4404 (no route), 4400 (Google's Live API, a destination the server may not reach), 1011 (a conversation that
    could not be read) and a busy turn (an error frame, the socket open): each a ``Block`` minted through ``refuse``,
    whose code and words the client gets, as before."""
    minted: list[Block] = []
    real = ws.refuse

    def kept(*args: Any) -> Block:
        minted.append(real(*args))
        return minted[-1]

    monkeypatch.setattr(ws, "refuse", kept)

    async def closed_with(routes: RouteTable, path: str = "/relay/v1/responses", **kwargs: Any) -> dict[str, Any]:
        client = _Client()
        client.socket.scope["path"] = path
        client.socket.scope["raw_path"] = path.encode()
        await asyncio.wait_for(ws.relay(client.socket, routes, Capture(None), **kwargs), 10)
        return next(m for m in client.sent if m["type"] == "websocket.close")

    assert (await closed_with(RouteTable([])))["code"] == 4404
    live = RouteTable([Upstream("gemini", "/relay", "http://127.0.0.1:1", refuses_unread=True)])
    assert (await closed_with(live))["code"] == 4400

    async def refused_pin(url: str) -> Any:
        raise EgressRefused("not reachable")

    own = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                "http://127.0.0.1:1",
                own=True,
                envelope_expected=frozenset(),
            )
        ]
    )
    assert (await closed_with(own, pin=refused_pin, mediator=_mediator()))["code"] == 4400
    mediator = _mediator()

    def unreadable(key: str) -> Any:
        raise OSError("the store did not answer")

    monkeypatch.setattr(mediator.engine, "session", unreadable)
    relay = RouteTable([Upstream("relay", "/relay", "http://127.0.0.1:1", envelope_expected=frozenset())])
    assert (await closed_with(relay, mediator=mediator))["code"] == 1011
    assert [(b.transport, b.code, b.status) for b in minted] == [
        ("ws-handshake", "no-route", 4404),
        ("ws-handshake", "unread", 4400),
        ("ws-handshake", "egress-refused", 4400),
        ("ws-handshake", "conversation-unread", 1011),
    ]
    assert all(type(b) is Block for b in minted)
    with pytest.raises(outcome.NotSendable, match="block"):
        outcome.sendable(minted[0], None, _handshake())


class _Busy:
    """An identity's turns in flight at their limit (``latet.turns``, the server's gate): each turn asked refused."""

    def begin(self) -> str | None:
        return "too many requests in flight for this identity"

    def end(self) -> None:
        raise AssertionError("a refused turn never ends")


async def test_a_busy_turn_is_a_minted_block_answered_as_before_and_refuse_keeps_its_bounds(monkeypatch):
    """The review's M-2: the busy turn's ``Block`` (429, ``ws-frame``) carries the words its error frame says, as
    before, nothing of the turn sent; ``refuse`` takes a close code for a handshake alone, never 1000."""
    minted: list[Block] = []
    real = ws.refuse

    def kept(*args: Any) -> Block:
        minted.append(real(*args))
        return minted[-1]

    monkeypatch.setattr(ws, "refuse", kept)
    upstream, client = _Quick(), _Client()
    client.socket.scope["latet.turns"] = _Busy()
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                f"http://127.0.0.1:{upstream.port}",
                envelope_expected=frozenset(),
            )
        ]
    )
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), _mediator()))
        client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "response.create"})})
        await _until(lambda: len(client.texts()) == 1)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relaying, 10)
    finally:
        upstream.close()
    [busy] = minted
    assert (type(busy), busy.transport, busy.code, busy.status) == (Block, "ws-frame", "busy", 429)
    words = "too many requests in flight for this identity"
    assert json.loads(client.texts()[0]) == {"type": "error", "error": {"type": "latet_busy", "message": words}}
    assert busy.reason == words and upstream.got == []
    for transport, status in (("ws-frame", 1011), ("ws-frame", 4404), ("http", 4404), ("http", 1011)):
        with pytest.raises(ValueError, match="says why"):
            outcome.refuse(transport, "relay", "busy", "x", status)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="says why"):
        outcome.refuse("ws-handshake", "relay", "no-route", "", 1000)
    assert outcome.refuse("ws-handshake", "relay", "no-route", "", 4404).status == 4404
