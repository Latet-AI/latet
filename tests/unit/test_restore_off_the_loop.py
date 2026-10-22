"""No restore holds the event loop. Restoring an answer, an event of a stream or a WebSocket message as large as Latet
reads takes seconds, and the event loop carries every other agent's streams (on a Latet server, every identity's): one
larger than a little is restored on the engine's thread (on a server, the identity's own), in turn with masking. It still
comes back restored whole, and nothing of it goes out before it is restored. What is small is restored where it is read,
waiting behind no masking."""

import asyncio
import contextlib
import gzip
import json
import socket
import threading
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway import forward
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator, WSResponseRewriter
from latet.gateway.routes import Upstream
from latet.protocols.sse import StreamDecoder

from ..conftest import ROOT_KEY

MIB = 2**20
EMAIL = "emily.carter@acme.com"
CHAT = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
HEADERS = {"authorization": "Bearer sk-test", "session-id": "s-loop"}
HELD = 0.25  # the longest a turn of the event loop may take, in seconds, while the gateway restores megabytes


def _answer(content_type: str, body: bytes, encoding: str | None = None) -> httpx.Response:
    """A provider's answer, read by the gateway as it comes (a body given whole would be read before)."""
    headers = {"content-type": content_type} | ({"content-encoding": encoding} if encoding else {})
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(gzip.compress(body, 6) if encoding else body))


def _said(stand_in: str, size: int) -> tuple[str, str]:
    """A text of about ``size`` bytes that holds ``stand_in`` again and again; and that text restored."""
    times = size // (len(stand_in) + 1)
    return " ".join([stand_in] * times), " ".join([EMAIL] * times)


@contextlib.asynccontextmanager
async def _ticking() -> AsyncIterator[list[float]]:
    """How long each turn of the event loop took, told by a task that asks for one every 5 ms."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def tick() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(tick())
    await asyncio.sleep(0.02)
    try:
        yield gaps
    finally:
        done.set()
        await task


class _Provider:
    """A provider that answers the first request in a word, so that the test learns what the e-mail was sent as, and
    the next with ``then``, built before it is asked: what is measured is the gateway's own work. ``meanwhile``: what it
    does as it answers that one."""

    def __init__(self) -> None:
        self.stand_in = ""
        self.then: httpx.Response | None = None
        self.meanwhile = lambda: None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.then is None:
            self.stand_in = json.loads(request.content)["messages"][0]["content"].removeprefix("write to ")
            word = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
            return _answer("application/json", json.dumps(word).encode())
        self.meanwhile()
        return self.then


@contextlib.asynccontextmanager
async def _gateway(provider: _Provider) -> AsyncIterator[tuple[httpx.AsyncClient, Mediator]]:
    """The gateway on this event loop, over ``provider``, once it answered the first request; and its mediator."""
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("openai", "/openai", "http://127.0.0.1:1")],
        mediator=mediator,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283") as client,
    ):
        await client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS)
        yield client, mediator


def _streamed(text: str) -> str:
    """The text of a stream of chat completion chunks."""
    return "".join(
        json.loads(block[6:])["choices"][0]["delta"].get("content") or ""
        for block in text.split("\n\n")
        if block.startswith("data: {")
    )


@pytest.mark.timing
async def test_a_json_answer_near_the_bound_is_restored_whole_while_the_event_loop_runs():
    provider = _Provider()
    async with _gateway(provider) as (client, _):
        said, restored = _said(provider.stand_in, 28 * MIB)
        answer = {"choices": [{"index": 0, "message": {"role": "assistant", "content": said}}]}
        provider.then = _answer("application/json", json.dumps(answer).encode(), "gzip")
        async with _ticking() as gaps:
            started = time.perf_counter()
            answered = await client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS)
            took = time.perf_counter() - started
    assert answered.status_code == 200
    assert answered.json()["choices"][0]["message"]["content"] == restored
    assert max(gaps) < min(HELD, took / 4)


@pytest.mark.timing
async def test_one_event_of_a_stream_of_megabytes_is_restored_whole_while_the_event_loop_runs():
    provider = _Provider()
    async with _gateway(provider) as (client, _):
        said, restored = _said(provider.stand_in, 12 * MIB)
        event = {"choices": [{"index": 0, "delta": {"content": said}}]}
        stream = b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n"
        provider.then = _answer("text/event-stream", stream, "gzip")
        async with _ticking() as gaps:
            started = time.perf_counter()
            streamed = await client.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
            took = time.perf_counter() - started
    assert streamed.status_code == 200 and _streamed(streamed.text) == restored
    assert max(gaps) < min(HELD, took / 4)


@pytest.mark.timing
async def test_an_event_of_megabytes_the_end_of_a_stream_completes_is_restored_whole_while_the_event_loop_runs(
    monkeypatch,
):
    """A decoder may hold an event's last bytes until the stream ends (deflate's last match): the event they complete
    then is restored off the event loop as well."""
    provider = _Provider()
    async with _gateway(provider) as (client, _):
        said, restored = _said(provider.stand_in, 12 * MIB)
        event = {"choices": [{"index": 0, "delta": {"content": said}}]}
        provider.then = _answer("text/event-stream", b"data: " + json.dumps(event).encode())
        monkeypatch.setattr(StreamDecoder, "flush", lambda self: b"\n\ndata: [DONE]\n\n")  # what it held to the end
        async with _ticking() as gaps:
            started = time.perf_counter()
            streamed = await client.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
            took = time.perf_counter() - started
    assert streamed.status_code == 200 and _streamed(streamed.text) == restored
    assert max(gaps) < min(HELD, took / 4)


class _Turns:
    """The forwarder's ``asyncio``, counting the turns it hands back to the event loop."""

    def __init__(self) -> None:
        self.turns = 0

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.turns += 1
        await asyncio.sleep(delay)


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
async def test_an_answer_is_decoded_in_turns_with_the_event_loops_other_work(content_type, monkeypatch):
    """What one chunk of the wire decodes to, up to the bound, is read a piece at a time, the event loop handed back
    after each: one chunk of a few kilobytes holds no turn of the loop for all it decodes to."""
    provider = _Provider()
    async with _gateway(provider) as (client, _):
        said, _ = _said(provider.stand_in, 16 * MIB)
        if content_type == "application/json":
            body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": said}}]}).encode()
        else:
            body = b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": said}}]}).encode() + b"\n\n"
        provider.then = _answer(content_type, body, "gzip")  # one chunk on the wire
        turns = _Turns()
        monkeypatch.setattr(forward, "asyncio", turns)
        answered = await client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert answered.status_code == 200 and turns.turns >= len(body) // (64 * 2**10)


async def test_a_stream_of_small_events_is_restored_without_waiting_for_the_engines_thread():
    provider = _Provider()
    gate = threading.Event()
    async with _gateway(provider) as (client, mediator):
        chunk = {"choices": [{"index": 0, "delta": {"content": f" {provider.stand_in}"}}]}
        events = (b"data: " + json.dumps(chunk).encode() + b"\n\n") * 50 + b"data: [DONE]\n\n"
        provider.then = _answer("text/event-stream", events)
        provider.meanwhile = lambda: mediator.executor.submit(gate.wait, 30)  # masking another request, say
        try:
            post = client.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
            streamed = await asyncio.wait_for(post, 5)
        finally:
            gate.set()
    assert _streamed(streamed.text) == f" {EMAIL}" * 50


async def test_a_short_answer_is_restored_without_waiting_for_the_engines_thread():
    provider = _Provider()
    gate = threading.Event()
    async with _gateway(provider) as (client, mediator):
        answer = {"choices": [{"index": 0, "message": {"role": "assistant", "content": f"to {provider.stand_in}"}}]}
        provider.then = _answer("application/json", json.dumps(answer).encode())
        provider.meanwhile = lambda: mediator.executor.submit(gate.wait, 30)
        try:
            answered = await asyncio.wait_for(client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS), 5)
        finally:
            gate.set()
    assert answered.json()["choices"][0]["message"]["content"] == f"to {EMAIL}"


# --- a WebSocket ---------------------------------------------------------------------------------------------------


class _Codex:
    """Codex's WebSocket on 127.0.0.1, in a thread of its own: it answers a turn with one delta of about ``size`` bytes
    that holds what the e-mail was sent as again and again, then ends the text and the turn. ``restored``: that delta
    as the agent should read it."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.restored = ""
        app = Starlette(routes=[WebSocketRoute("/backend-api/codex/responses", self.turn)])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def turn(self, websocket: WebSocket) -> None:
        await websocket.accept()
        frame = json.loads(await websocket.receive_text())
        said, self.restored = _said(frame["input"][0]["content"][0]["text"].removeprefix("write to "), self.size)
        where = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "response.output_text.delta", **where, "delta": said}))
            await websocket.send_text(json.dumps({"type": "response.output_text.done", **where}))
            await websocket.send_text(json.dumps({"type": "response.completed", "response": {"id": "resp_1"}}))
            await websocket.receive_text()  # until the gateway closes

    def __enter__(self) -> "_Codex":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


async def _turn(app, frame: dict) -> list[str]:
    """One turn of Codex's WebSocket through the gateway, served on this event loop: what the agent was sent, read once
    the turn is over (reading it is no work of the gateway's)."""
    inbox: asyncio.Queue[dict] = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.connect"})
    inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})
    sent: list[str] = []
    over = asyncio.Event()

    async def send(message: dict) -> None:
        if message["type"] == "websocket.send":
            sent.append(message["text"])
            if "response.completed" in message["text"][:64]:  # passed on as the upstream wrote it: nothing to restore
                over.set()
        elif message["type"] == "websocket.close":
            over.set()

    path = "/backend-api/codex/responses"
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:5283"), (b"session-id", b"s-ws")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 5283),
        "subprotocols": [],
    }
    served = asyncio.create_task(app(scope, inbox.get, send))
    await asyncio.wait_for(over.wait(), 60)
    inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(served, 30)
    return sent


@pytest.mark.timing
async def test_a_websocket_message_of_megabytes_is_restored_whole_while_the_event_loop_runs():
    frame = {
        "type": "response.create",
        "model": "x",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"write to {EMAIL}"}]}
        ],
    }
    with _Codex(12 * MIB) as codex:
        built = build(Config(), Vault(ROOT_KEY))
        upstream = Upstream("chatgpt", "/backend-api/codex", codex.url, keep_prefix=True)
        app = create_app(
            GatewayConfig(port=5283, upstreams=[upstream], mediator=Mediator(built.engine, built.verifier))
        )
        async with app.router.lifespan_context(app), _ticking() as gaps:
            started = time.perf_counter()
            sent = await _turn(app, frame)
            took = time.perf_counter() - started
    events = [json.loads(text) for text in sent]
    assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == codex.restored
    assert events[-1]["type"] == "response.completed"
    assert max(gaps) < min(HELD, took / 4)


async def test_a_small_websocket_message_is_restored_without_waiting_for_the_engines_thread():
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    upstream = Upstream("openai", "/openai", "http://127.0.0.1:1")
    raw = json.dumps(CHAT).encode()
    prepared = mediator.prepare(upstream, "POST", "/openai/v1/chat/completions", {"authorization": "Bearer t"}, raw)
    assert prepared.session is not None
    stand_in = json.loads(prepared.body)["messages"][0]["content"].removeprefix("write to ")
    rewriter = WSResponseRewriter(mediator, prepared.session)
    where = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    gate = threading.Event()
    mediator.executor.submit(gate.wait, 30)
    try:
        held = await asyncio.wait_for(
            rewriter.restored(json.dumps({"type": "response.output_text.delta", **where, "delta": f"to {stand_in}"})), 5
        )
        out = await asyncio.wait_for(rewriter.restored(json.dumps({"type": "response.output_text.done", **where})), 5)
    finally:
        gate.set()
        mediator.close()
    assert held == [] and json.loads(out[0])["delta"] == f"to {EMAIL}"
