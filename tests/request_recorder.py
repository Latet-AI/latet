"""The transport recorder of the request-coverage characterization (P1-1 of `notes/server-request-coverage-plan.md`):
what Latet sends to a provider, as it is sent, never as a test server parses it.

Over HTTP, ``RecordingTransport`` is the transport under httpx's own client (``RecordingClient``): each request is
recorded as httpx hands it to its transport, which is what httpcore writes on the wire: the method, ``url.raw_path``
(the path and the query, every escape as built), ``headers.raw`` (each occurrence in its order, its name's case kept,
duplicates kept) and the content, read whole. ``RecordingClient`` counts what the forwarder builds (``build_request``)
and sends (``send``): one of each for a request at S1, two for the resend of S2.

Over a WebSocket, ``RecordingWSUpstream`` is an ASGI app served on 127.0.0.1 by uvicorn, which Latet's relay connects to
as to a provider: each handshake is recorded with the ``raw_path``, the ``query_string`` and the ``headers`` of its ASGI
scope (order and duplicates as received), and every frame the relay sends after it (S3, S4).

``tests/e2e/fake_upstream.py``'s ``FakeUpstream.requests`` keeps a dict of headers, which loses their order and their
duplicates: it is never the oracle for headers here; its app may answer behind ``RecordingTransport``."""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn

Answer = Callable[[httpx.Request], "httpx.Response | Awaitable[httpx.Response]"]


@dataclass(frozen=True, slots=True)
class SentRequest:
    """One request as httpx hands it to its transport."""

    method: str
    host: str  # the URL's host, and its port when the URL names one: where it was sent
    raw_path: bytes  # ``url.raw_path``: the path and the query, as built
    headers: tuple[tuple[bytes, bytes], ...]  # ``headers.raw``: every occurrence, in order, as sent
    content: bytes


class RecordingTransport(httpx.AsyncBaseTransport):
    """An httpx transport that records each request (``sent``) and answers it with ``answer`` (a function of the
    request, a coroutine function too), or raises ``failure`` (an ``httpx.HTTPError``: a provider that does not answer)
    when it is set."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.failure: Exception | None = None
        self.sent: list[SentRequest] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        url = request.url
        host = url.host if url.port is None else f"{url.host}:{url.port}"
        self.sent.append(SentRequest(request.method, host, url.raw_path, tuple(request.headers.raw), content))
        if self.failure is not None:
            raise self.failure
        found = self.answer(request)
        if inspect.isawaitable(found):
            found = await found
        assert isinstance(found, httpx.Response)
        return found

    def clear(self) -> None:
        self.sent.clear()
        self.failure = None


class RecordingClient(httpx.AsyncClient):
    """httpx's client over a ``RecordingTransport``, counting the requests built (``builds``) and sent (``sends``). It
    is made with ``latet.gateway.forward.client_options``, the options of Latet's own clients (``make_client``): it
    follows no redirect and keeps no cookie an answer sets as theirs do, so that a test of what a client keeps tests
    Latet's (G2 re-review N-1); and it keeps httpx's default headers (which the forwarder takes away again unless the
    client sent them). Unlike them, it takes no proxy of the environment, which would route around the recording
    transport (the proxy's hop is transport, P1-0 D-10)."""

    def __init__(self, transport: RecordingTransport) -> None:
        from latet.gateway import forward

        super().__init__(transport=transport, **forward.client_options(trust_env=False))
        self.recording = transport
        self.builds = 0
        self.sends = 0

    def build_request(self, *args: Any, **kwargs: Any) -> httpx.Request:
        self.builds += 1
        return super().build_request(*args, **kwargs)

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        self.sends += 1
        return await super().send(request, **kwargs)


@dataclass(slots=True)
class Handshake:
    """One WebSocket the relay opened to the upstream, and every frame it sent there."""

    raw_path: bytes
    query_string: bytes
    headers: list[tuple[bytes, bytes]]  # the ASGI scope's: every occurrence, in order, as received
    frames: list[str | bytes] = field(default_factory=list)
    accepted: bool = False


def echo_frames(data: str | bytes) -> list[str]:
    """What the recording upstream answers a frame with: to a ``response.create`` turn, the text of its last input
    item said back in two deltas and the turn's end; to anything else, one ``ack`` frame (so that each frame the relay
    passes on is answered, and the driver knows when to send the next)."""
    if isinstance(data, str):
        try:
            turn = json.loads(data)
        except ValueError:
            turn = None
        if isinstance(turn, dict) and turn.get("type") == "response.create":
            said = _last_text(turn.get("input"))
            half = len(said) // 2
            events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": "resp_rc"}}]
            events += [
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_rc",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": piece,
                }
                for piece in (said[:half], said[half:])
            ]
            events.append({"type": "response.completed", "response": {"id": "resp_rc", "status": "completed"}})
            return [json.dumps(event) for event in events]
    return [json.dumps({"type": "ack"})]


def _last_text(items: Any) -> str:
    if isinstance(items, str):
        return items
    texts: list[str] = []
    for item in items if isinstance(items, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            found = [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
            if found:
                texts = found
    return " ".join(texts)


class RecordingWSUpstream:
    """A WebSocket provider served on 127.0.0.1 (uvicorn, in a thread of its own), recording each handshake and frame
    (``handshakes``). ``refuse``: a status the next handshakes are refused with (and ``location``, a redirect's), the
    upstream's own refusal; None accepts them. ``reply``: what each frame is answered with (``echo_frames``)."""

    def __init__(self, reply: Callable[[str | bytes], Iterable[str]] = echo_frames) -> None:
        self.reply = reply
        self.refuse: int | None = None
        self.location: str | None = None
        self.handshakes: list[Handshake] = []
        self._lock = threading.Lock()
        # The socket bound here is the one uvicorn serves on: no other process can take its port in between.
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        config = uvicorn.Config(self, host="127.0.0.1", port=self.port, log_level="warning", ws="websockets-sansio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True, name="rc-ws-upstream"
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> RecordingWSUpstream:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("the recording WebSocket upstream did not start")
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()

    def clear(self) -> None:
        with self._lock:
            self.handshakes.clear()
        self.refuse = None
        self.location = None

    def frames(self) -> int:
        with self._lock:
            return sum(len(shake.frames) for shake in self.handshakes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        shake = Handshake(scope.get("raw_path") or b"", scope.get("query_string") or b"", list(scope["headers"]))
        with self._lock:
            self.handshakes.append(shake)
        await receive()  # websocket.connect
        if self.refuse is not None:
            headers = [(b"content-type", b"text/plain")]
            if self.location is not None:
                headers.append((b"location", self.location.encode()))
            await send({"type": "websocket.http.response.start", "status": self.refuse, "headers": headers})
            await send({"type": "websocket.http.response.body", "body": b"refused by the upstream"})
            return
        await send({"type": "websocket.accept"})
        shake.accepted = True
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes") or b""
            with self._lock:
                shake.frames.append(data)
            for out in self.reply(data):
                await send({"type": "websocket.send", "text": out})
                await asyncio.sleep(0)
