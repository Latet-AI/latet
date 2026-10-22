"""A WebSocket's relay on a Latet server (F-S1): its work off the event loop, as an HTTP request's is (its conversation
read in the engine's thread, where a server's Valkey answers and the values another instance showed it are loaded from
PostgreSQL), and its two legs ending with it, whatever ends it: a turn that can no longer be read closes it cleanly,
nothing of that turn sent."""

import asyncio
import json
import threading

import pytest
from starlette.websockets import WebSocket
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode
from websockets.sync.server import serve

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway import ws
from latet.gateway.capture import Capture
from latet.gateway.mediate import Mediator
from latet.gateway.routes import RouteTable, Upstream

from ...conftest import ROOT_KEY

TURN = json.dumps({"type": "response.create", "model": "m", "input": "the status page is up"})
_LEGS = ("client_to_upstream", "upstream_to_client")


class _Answering:
    """A WebSocket upstream on 127.0.0.1 that keeps every frame it is sent and answers each with a turn's end."""

    def __init__(self) -> None:
        self.got: list[str | bytes] = []
        self.connections = 0
        self.server = serve(self._answer, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _answer(self, conn) -> None:
        self.connections += 1
        done = json.dumps({"type": "response.completed", "response": {"status": "completed"}})
        try:
            for message in conn:
                self.got.append(message)
                conn.send(done)
        except ConnectionClosed:
            pass

    def close(self) -> None:
        self.server.shutdown()


class _Client:
    """The client's side of a WebSocket the relay serves: what it sends, queued; what it is sent, kept."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.sent: list[dict] = []
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1",
            "server": ("127.0.0.1", 5283), "client": ("127.0.0.1", 50000), "root_path": "",
            "path": "/relay/v1/responses", "raw_path": b"/relay/v1/responses", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:5283"), (b"session-id", b"s-1")], "subprotocols": [],
        }  # fmt: skip
        self.socket = WebSocket(scope, self._receive, self._send)

    async def _receive(self) -> dict:
        return await self.incoming.get()

    async def _send(self, message: dict) -> None:
        self.sent.append(message)

    def texts(self) -> list[str]:
        return [message["text"] for message in self.sent if message["type"] == "websocket.send" and "text" in message]

    def closed(self) -> list[dict]:
        return [message for message in self.sent if message["type"] == "websocket.close"]

    async def turn(self, frame: str) -> None:
        """``frame`` sent, and its answer read."""
        answered = len(self.texts())
        self.incoming.put_nowait({"type": "websocket.receive", "text": frame})
        await _until(lambda: len(self.texts()) > answered)


async def _until(predicate, seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not in time")


def _mediator() -> Mediator:
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.on_server = True
    return mediator


def _routes(upstream: _Answering) -> RouteTable:
    return RouteTable([Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}")])


def _on_a_loop() -> bool:
    """Whether an event loop runs in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _legs() -> list[asyncio.Task]:
    """The relay's legs still running on this loop."""
    return [task for task in asyncio.all_tasks() if task.get_coro().__qualname__.endswith(_LEGS) and not task.done()]


async def test_a_websockets_conversation_is_read_off_the_event_loop(monkeypatch):
    """On a server a conversation is a round trip to Valkey, and the values another instance showed it are loaded from
    PostgreSQL (``spaces._learn_ids``): read in the engine's thread, as an HTTP request's is (``Mediator.prepare``)."""
    upstream, client, mediator = _Answering(), _Client(), _mediator()
    conversations, on_loop = mediator.engine.conversations, []
    read = conversations.session

    def session(key):
        on_loop.append(_on_a_loop())
        return read(key)

    monkeypatch.setattr(conversations, "session", session)
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, _routes(upstream), Capture(None), mediator))
        await client.turn(TURN)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relaying, 5)
    finally:
        upstream.close()
    assert on_loop == [False] and upstream.got == [TURN]


async def test_a_websocket_whose_conversation_cannot_be_read_is_closed_and_reaches_nothing(monkeypatch):
    """A store that does not answer, or the engine's thread gone as its space closes: the socket is closed (1011)
    before anything is connected, raising nothing."""
    upstream, client, mediator = _Answering(), _Client(), _mediator()

    def unanswered(key):
        raise ConnectionError("Valkey did not answer")

    monkeypatch.setattr(mediator.engine.conversations, "session", unanswered)
    try:
        await asyncio.wait_for(ws.relay(client.socket, _routes(upstream), Capture(None), mediator), 5)
    finally:
        upstream.close()
    [closed] = client.closed()
    assert closed["code"] == CloseCode.INTERNAL_ERROR and upstream.connections == 0


async def test_a_relay_cancelled_as_its_space_closes_ends_both_legs_and_reads_its_client_no_more(monkeypatch):
    """S5's other half: ``Space.end_long`` cancels the relay's task. Both legs end with it: the client is read no more,
    and a frame it sends after is masked by no one and reaches nothing."""
    upstream, client, mediator = _Answering(), _Client(), _mediator()
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, _routes(upstream), Capture(None), mediator))
        await client.turn(TURN)
        relaying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relaying
        assert _legs() == []
        masked: list[str] = []
        monkeypatch.setattr(mediator, "mask_ws_frame", lambda text, session: (masked.append(text), (text, None))[1])
        client.incoming.put_nowait({"type": "websocket.receive", "text": TURN})
        await asyncio.sleep(0.3)
    finally:
        upstream.close()
    assert masked == [] and upstream.got == [TURN]


async def test_a_turn_on_a_closed_mediator_ends_with_a_clean_close_and_sends_nothing():
    """The engine's thread gone (its space's runtime closed) while the socket is open: the next turn cannot be masked.
    The relay closes the socket with 1011, its legs ended and nothing of the turn sent, never an error left in a task
    that nobody reads."""
    upstream, client, mediator = _Answering(), _Client(), _mediator()
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, _routes(upstream), Capture(None), mediator))
        await client.turn(TURN)
        mediator.close()
        client.incoming.put_nowait({"type": "websocket.receive", "text": TURN})
        await asyncio.wait_for(relaying, 5)
        assert _legs() == []
    finally:
        upstream.close()
    [closed] = client.closed()
    assert closed["code"] == CloseCode.INTERNAL_ERROR and upstream.got == [TURN]
