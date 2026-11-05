"""RC-REC-11's three resource-leak fixes (P1-4 of ``notes/server-request-coverage-plan.md``), the only behaviour Phase 1
changes: what an exchange holds of its conversation's vault is released once, by whoever ends the exchange, through its
lease (``gateway/lease.py``), and the provider's answer is closed, however the exchange ends:

- (a) a restored stream whose relay never starts (its client gone before the answer's start): released, its renewer
  ended and the answer closed by the response's own ``finally``, before its ASGI call returns, never by a garbage
  collection finalizer;
- (b) a request cancelled while an executor step that holds runs, the masking or the Live Reviewer's decision applied:
  what that step holds is released when it ends, the handle ``Engine.reacquire`` returned included, nothing sent;
- (c) a WebSocket turn: its hold released after its answer's last event is restored (or as a refusal is answered, or
  as the socket ends), its restoration intact.

Nothing is sent, refused, recorded or logged differently (P1-1's characterization judges that, ``phase_1_judge``)."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.config import LiveReview
from latet.gateway import mediate, request_transport
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.approvals import Decision
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import Mediator
from latet.gateway.request_transport import SendRefusal
from latet.gateway.routes import RouteTable, Upstream

from .test_request_outcomes import CHAT, EMAIL, HEADERS, OPENAI, _chat, _mediator

SSE = b'data: {"choices":[{"index":0,"delta":{"content":"noted"}}]}\n\ndata: [DONE]\n\n'


class _Seam:
    """The engine's ``hold``, ``reacquire`` and ``release`` as the gateway calls them, each handle an object of its
    own: which handles were taken, and which were let go, in order."""

    def __init__(self, mediator: Mediator, monkeypatch: pytest.MonkeyPatch) -> None:
        self.holds: list[object] = []
        self.reacquired: list[object] = []
        self.releases: list[object] = []
        self.gate: threading.Event | None = None  # set: each reacquire waits for it, once it says it started
        self.entered = threading.Event()
        engine = mediator.engine

        def hold(session: Any) -> object:
            self.holds.append(object())
            return self.holds[-1]

        def reacquire(session: Any, held: object) -> object:
            self.entered.set()
            if self.gate is not None:
                self.gate.wait(10)
            self.reacquired.append(object())  # a hold of its own: the earlier one lapsed meanwhile
            return self.reacquired[-1]

        def release(session: Any, held: object) -> None:
            self.releases.append(held)

        monkeypatch.setattr(engine, "hold", hold)
        monkeypatch.setattr(engine, "reacquire", reacquire)
        monkeypatch.setattr(engine, "release", release)


class _Answer(httpx.AsyncByteStream):
    """A provider's answer, which knows whether the gateway closed it."""

    def __init__(self, body: bytes, repeat: int = 1) -> None:
        self.body, self.repeat = body, repeat
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.repeat):
            yield self.body

    async def aclose(self) -> None:
        self.closed = True


class _Provider:
    """Answers each request: a stream of ``chunks`` events when it asks one, else JSON; with ``status`` and the extra
    ``headers`` (bytes, as a provider writes them)."""

    def __init__(self, status: int = 200, headers: list[tuple[bytes, bytes]] | None = None, chunks: int = 1) -> None:
        self.sent: list[httpx.Request] = []
        self.answers: list[_Answer] = []
        self.status, self.headers, self.chunks = status, headers or [], chunks

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        streams = json.loads(request.content).get("stream", False)
        whole = b'{"choices":[{"message":{"content":"noted"}}]}'
        self.answers.append(_Answer(SSE, self.chunks) if streams else _Answer(whole))
        kind = b"text/event-stream" if streams else b"application/json"
        headers = [(b"content-type", kind), *self.headers]
        return httpx.Response(self.status, headers=headers, stream=self.answers[-1])


def _forwarder(mediator: Mediator, provider: _Provider) -> Forwarder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    return Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator)


def _request(body: bytes) -> Request:
    sent = [False]

    async def receive() -> dict[str, Any]:
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "scheme": "http",
        "path": CHAT,
        "raw_path": CHAT.encode(),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in HEADERS.items()],
        "server": ("127.0.0.1", 5283),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope, receive)


def _renewers() -> list[asyncio.Task[Any]]:
    return [task for task in asyncio.all_tasks() if getattr(task.get_coro(), "__name__", "") == "renewing"]


async def test_an_unstarted_stream_releases_its_hold_and_closes_its_response(monkeypatch):
    """(a): the answer handed to a client gone before its start (the server's send raises at the response's start, so
    its body is never iterated and its relay's ``finally`` never runs): one release and the provider's answer closed by
    the response's own ``finally`` before its ASGI call returns, with garbage collection off; its renewer ended; sent
    once, as before."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)
    body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": f"to {EMAIL}"}]})
    request = _request(body.encode())

    async def gone() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def closed(message: dict[str, Any]) -> None:
        raise OSError("the client is gone")

    gc.disable()
    try:
        response = await forwarder.handle(request)
        assert isinstance(response, StreamingResponse) and len(seam.holds) == 1 and seam.releases == []
        with pytest.raises(ClientDisconnect):  # what Starlette makes of the server's failed send
            await response(dict(request.scope), gone, cast(Any, closed))
        assert seam.releases == seam.holds  # its own hold, once, before its ASGI call returned
        assert provider.answers[0].closed
    finally:
        gc.enable()
    for _ in range(4):
        await asyncio.sleep(0)
    assert all(task.done() for task in _renewers())  # its renewer renews no more
    assert len(provider.sent) == 1 and EMAIL.encode() not in provider.sent[0].content
    await forwarder.client.aclose()


def _stream_body() -> bytes:
    return json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": f"to {EMAIL}"}]}).encode()


async def test_a_restored_stream_whose_headers_cannot_be_written_releases_its_hold_and_closes_its_answer(monkeypatch):
    """(a), the review's I-2: a provider's header outside latin-1 ("€", which httpx reads as UTF-8) fails the response
    before it is handed back: the hold released, the renewer ended and the answer closed, with garbage collection off."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider(headers=[(b"x-note", "\N{EURO SIGN}".encode())])
    forwarder = _forwarder(mediator, provider)
    gc.disable()
    try:
        with pytest.raises(UnicodeEncodeError):
            await forwarder.handle(_request(_stream_body()))
        assert len(seam.holds) == 1 and seam.releases == seam.holds and provider.answers[0].closed
    finally:
        gc.enable()
    for _ in range(4):
        await asyncio.sleep(0)
    assert all(task.done() for task in _renewers())
    await forwarder.client.aclose()


async def test_an_unstarted_stream_passed_as_it_came_closes_its_answer(monkeypatch):
    """(a), an answer Latet passes on unrestored (an error's stream): its hold handed on to the response with the
    request's freeze (02dbf660), as a restored stream's is; the response, never started, closes the provider's answer
    and releases the hold, once."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider(status=400)
    forwarder = _forwarder(mediator, provider)
    gc.disable()
    try:
        response = await forwarder.handle(_request(_stream_body()))
        assert isinstance(response, StreamingResponse) and seam.releases == [] and len(seam.holds) == 1
        with pytest.raises(ClientDisconnect):
            await response(dict(_request(b"").scope), _gone, cast(Any, _closed))
        assert provider.answers[0].closed and seam.releases == seam.holds
    finally:
        gc.enable()
    await forwarder.client.aclose()


async def test_a_stream_cut_short_mid_way_releases_its_hold_and_closes_its_answer_at_once(monkeypatch):
    """(a), the review's M-5: the client gone after the first chunk went out (the server's send raises while the relay
    waits at its ``yield``): the relay's ``finally`` runs as the response's call ends, not at garbage collection."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider(chunks=3)
    forwarder = _forwarder(mediator, provider)
    sent: list[dict[str, Any]] = []

    async def first_only(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and any(m["type"] == "http.response.body" for m in sent):
            raise OSError("the client is gone")
        sent.append(message)

    gc.disable()
    try:
        response = await forwarder.handle(_request(_stream_body()))
        with pytest.raises(ClientDisconnect):
            await response(dict(_request(b"").scope), _gone, cast(Any, first_only))
        assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
        assert seam.releases == seam.holds and len(seam.holds) == 1 and provider.answers[0].closed
    finally:
        gc.enable()
    await forwarder.client.aclose()


async def test_an_error_after_the_masking_held_releases_its_hold(monkeypatch):
    """The review's M-7: anything raised in the masking step once it held (here its final check) lets the hold go
    there, since no request will own it; the error goes on as before, nothing sent."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)

    def broken(prepared: Any) -> None:
        raise RuntimeError("the final check broke")

    monkeypatch.setattr(mediator, "_check", broken)
    with pytest.raises(RuntimeError, match="final check"):
        await forwarder.handle(_request(_chat(f"write to {EMAIL}")))
    assert len(seam.holds) == 1 and seam.releases == seam.holds and provider.sent == []
    await forwarder.client.aclose()


async def _gone() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _closed(message: dict[str, Any]) -> None:
    raise OSError("the client is gone")


async def test_a_request_cancelled_while_masking_releases_its_hold_when_the_executor_ends(monkeypatch):
    """(b), the masking: the request given up while the executor masks; what the masking holds is released once the
    executor ends, and nothing is sent."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)
    gate, entered = threading.Event(), threading.Event()
    prepare = mediator.prepare

    def held_prepare(*args: Any, **kwargs: Any) -> Any:
        found = prepare(*args, **kwargs)  # masked: its conversation's hold taken
        entered.set()
        gate.wait(10)
        return found

    monkeypatch.setattr(mediator, "prepare", held_prepare)
    task = asyncio.create_task(forwarder.handle(_request(_chat(f"write to {EMAIL}"))))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(seam.holds) == 1 and seam.releases == []  # still masking: nothing to let go yet
    gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)  # the executor ended
    assert seam.releases == seam.holds and provider.sent == []
    await forwarder.client.aclose()


async def test_a_request_cancelled_while_the_live_decision_applies_releases_the_reacquired_hold(monkeypatch):
    """(b), the Live Reviewer's decision applied (re-review I-1): the request given up while the executor applies it;
    ``handle``'s ``finally`` lets the first handle go, and the handle ``Engine.reacquire`` returns afterwards is
    released once the executor ends: each handle once, none twice, nothing sent."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    seam.gate = threading.Event()
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)

    async def decided(batch: Any, gone: Any, held: Any = None) -> Decision:
        return Decision(True)

    monkeypatch.setattr(mediator, "live_review", LiveReview(enabled=True))
    monkeypatch.setattr(mediator, "await_decision", decided)
    task = asyncio.create_task(forwarder.handle(_request(_chat(f"write to {EMAIL}"))))
    assert await asyncio.to_thread(seam.entered.wait, 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    seam.gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
    assert len(seam.holds) == 1 and len(seam.reacquired) == 1
    assert seam.releases == [seam.holds[0], seam.reacquired[0]] and provider.sent == []
    await forwarder.client.aclose()


class _Turns:
    """A provider's Responses API over WebSocket: each turn answered with its input as one delta, then its end."""

    def __init__(self, ends: bool = True) -> None:
        self.ends = ends  # False: each answer stops before its end, as one the socket's end cuts short
        self.answered = 0
        self.server = serve(self.answer, "127.0.0.1", 0, close_timeout=2)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def answer(self, connection: Any) -> None:
        with contextlib.suppress(ConnectionClosed, TimeoutError):
            for message in connection:
                said = json.loads(message)["input"]
                delta = {"type": "response.output_text.delta", "item_id": "t", "content_index": 0, "delta": said}
                connection.send(json.dumps(delta))
                if self.ends:
                    connection.send(json.dumps({"type": "response.completed", "response": {"status": "completed"}}))
                self.answered += 1

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def _settled(check: Any) -> bool:
    for _ in range(200):
        if check():
            return True
        time.sleep(0.01)
    return False


def test_a_websocket_turn_releases_its_hold_after_its_last_restore(monkeypatch):
    """(c): each turn's hold released once its answer's last event is restored, while the socket stays open; the
    answer restored as before; one release per turn, none at the socket's end for a turn already let go."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Turns()
    app = create_app(
        GatewayConfig(mediator=mediator, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}")])
    )
    try:
        with (
            TestClient(app, base_url="http://127.0.0.1:5283") as client,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            for n in (1, 2):
                socket.send_json({"type": "response.create", "model": "gpt-4o", "input": f"turn {n}: write to {EMAIL}"})
                delta = socket.receive_json()
                assert delta["type"] == "response.output_text.delta" and EMAIL in delta["delta"]  # restored
                assert socket.receive_json()["type"] == "response.completed"
                assert _settled(lambda n=n: len(seam.releases) == n), (n, seam.releases)
                assert seam.releases == seam.holds  # its own, at its end, the socket still open
    finally:
        provider.close()
    assert len(seam.holds) == 2 and seam.releases == seam.holds  # none again as the socket ended


def test_a_websocket_turn_cut_short_releases_its_hold_as_the_socket_ends(monkeypatch):
    """(c), at the socket's end: a turn whose answer never ended (the client gone meanwhile) is released as the socket
    ends, once."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Turns(ends=False)
    app = create_app(
        GatewayConfig(mediator=mediator, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}")])
    )
    try:
        with (
            TestClient(app, base_url="http://127.0.0.1:5283") as client,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            socket.send_json({"type": "response.create", "model": "gpt-4o", "input": f"write to {EMAIL}"})
            assert _settled(lambda: provider.answered == 1)  # its answer begun (held back while it may continue)
            assert seam.releases == [] and len(seam.holds) == 1
        assert _settled(lambda: len(seam.releases) == 1)
    finally:
        provider.close()
    assert seam.releases == seam.holds


def _relay(mediator: Mediator, provider: _Turns) -> Any:
    return create_app(
        GatewayConfig(mediator=mediator, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}")])
    )


@pytest.mark.parametrize("where", ["its final check", "its writing anew"])
def test_an_error_after_a_websocket_turn_held_releases_its_hold(monkeypatch, where):
    """The P1-4 review's RR-3, the WebSocket twin of the test above: anything raised in a turn's masking once it held
    (its final check, or the turn written anew) lets the hold go there, since no turn will own it; the socket closes as
    before (1011), nothing sent."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Turns()

    def broken(*args: Any) -> Any:
        raise RuntimeError("it broke")

    if where == "its final check":
        monkeypatch.setattr(mediator, "_check", broken)
    else:
        monkeypatch.setattr(mediate, "dumps_compact", broken)
    try:
        with (
            TestClient(_relay(mediator, provider), base_url="http://127.0.0.1:5283") as client,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            socket.send_json({"type": "response.create", "model": "gpt-4o", "input": f"write to {EMAIL}"})
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1011
    finally:
        provider.close()
    assert len(seam.holds) == 1 and seam.releases == seam.holds and provider.answered == 0


def test_a_turn_whose_send_is_refused_lets_go_of_its_hold_and_the_socket_goes_on(monkeypatch):
    """The P1-4 review's RR-4 (M-6's path, never taken in Phase 1): a frame the adapter refuses to send has its lease
    taken back out of the turns answered and released, its client told so in an error frame, nothing sent; the socket
    stays open, and the next turn is sent, answered and let go as usual."""
    mediator = _mediator()
    seam = _Seam(mediator, monkeypatch)
    provider = _Turns()
    send_frame = request_transport.send_frame
    refused: list[bool] = []

    async def once(connection: Any, authorized: Any) -> SendRefusal | None:
        if not refused:
            refused.append(True)
            return SendRefusal("client")
        return await send_frame(connection, authorized)

    monkeypatch.setattr(request_transport, "send_frame", once)
    try:
        with (
            TestClient(_relay(mediator, provider), base_url="http://127.0.0.1:5283") as client,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            socket.send_json({"type": "response.create", "model": "gpt-4o", "input": f"write to {EMAIL}"})
            error = socket.receive_json()
            assert error["type"] == "error" and error["error"]["type"] == "latet_error"
            assert "(client)" in error["error"]["message"]
            assert _settled(lambda: len(seam.releases) == 1) and seam.releases == seam.holds
            socket.send_json({"type": "response.create", "model": "gpt-4o", "input": f"again to {EMAIL}"})
            delta = socket.receive_json()
            assert delta["type"] == "response.output_text.delta" and EMAIL in delta["delta"]
            assert socket.receive_json()["type"] == "response.completed"
            assert _settled(lambda: len(seam.releases) == 2)
    finally:
        provider.close()
    assert provider.answered == 1 and len(seam.holds) == 2 and seam.releases == seam.holds
