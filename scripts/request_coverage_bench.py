"""The measurements of P2-8 (`notes/server-request-coverage-plan.md`, "P2-8", the measurement bullets): what request
coverage costs a request end to end, the same frozen fixtures measured on the accepted Task 8 head and on RC, on one
worker and with ``--workers 2``; refusals counted apart, never a speed. Beside them the response finalizer's
end-of-answer wait, the ledger a repeated key costs, RC-REC-3's settings read and RC-REC-2's Valkey read and commit.

    uv run python scripts/request_coverage_bench.py fixtures --out FIXTURES.json [--seed 20261104]
    uv run python scripts/request_coverage_bench.py measure --fixtures FIXTURES.json --label rc --out RC.json
        [--source CHECKOUT] [--python PYTHON] [--machines computer server] [--cases ID ...] [--rounds 5]
        [--warm 3] [--requests 20] [--values 1000] [--person-values 36000] [--workers 1|2] [--ocr-models DIR]
    uv run python scripts/request_coverage_bench.py compare --base TASK8.json --rc RC.json [--out VERDICT.json]
    uv run python scripts/request_coverage_bench.py finalizer --out FINALIZER.json [--rounds 20] [--source ...]
    uv run python scripts/request_coverage_bench.py written --out WRITTEN.json [--rounds 5] [--source ...]
    uv run python scripts/request_coverage_bench.py ledger --out LEDGER.json [--source ...]
    uv run python scripts/request_coverage_bench.py micro --out MICRO.json [--calls 500] [--source ...]
    uv run python scripts/request_coverage_bench.py dry-run --out DIR      # every command with tiny counts, RC alone

``fixtures`` freezes the requests (each case's method, target, headers, body or frames, what it changes around it) in
one file, its SHA-256 recorded by every ``measure``: the Task 8 head and RC are measured on the very same bytes.
``measure`` runs the cases on ``--source`` (default: this checkout), in a process of its own whose ``latet`` is that
checkout's ``src`` (``PYTHONPATH``), with ``--python`` (default: this one; a checkout whose dependencies differ needs
its own environment's), in private homes and data folders made for the run (never yours), a test root key, no keyring.
A computer's cases run on a computer's runtime and app; a server's on the server's app (what `latet server run`
serves, its gate, an API key's route), on the test services (``LATET_TEST_DATABASE_URL``, ``LATET_TEST_VALKEY_URL``),
in a schema and a Valkey prefix of the run's own, made by ``scripts/bench_apps_server.py build`` under the same source
and dropped after it. One worker: in this process, the provider an in-process mock (no socket), the WebSocket's a
loopback echo. ``--workers 2``: `latet server run --workers 2` (``bench_apps_server.py``'s program; Linux alone, the
port shared with SO_REUSEPORT), driven over loopback HTTP, the provider a loopback echo.

Each case: ``--rounds`` rounds; a warm case warms its environment with ``--warm`` requests, then times ``--requests``
more; a cold one times the first request of a fresh environment (a computer's runtime, a server's new API key) each
round. Each request: its wall time to the end of its answer, the process's CPU over it, its status, and whether it
reached the provider. Then one probe pass (never timed): the traced peak, the coverage ledger's retained bytes and the
runtime's retained request bytes (RC's; None on a head that has none), and the process's private bytes.

``compare`` takes each case's median over the requests sent on both heads (a request refused on one is counted apart,
and a case refused on RC is not comparable, never faster) and judges the bar: RC's added median at most 5 % of the
Task 8 head's, and at most 2 ms for a bodyless GET; past either, root rules. Task 8's own absolute targets stay as they
were (``TASK8_TARGETS``), judged by its own measurements (``bench_engine.py``, ``bench_apps_server.py``).

The full runs are root's to schedule, one at a time, on a quiet machine; ``dry-run`` proves the harness."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import platform
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = "latet-request-coverage-bench/1"
BAR_RATIO = 0.05  # RC's added median cost, at most this share of the Task 8 head's on the same fixtures
BAR_BODYLESS_MS = 2.0  # and at most this, p50, for a bodyless GET
TASK8_HEAD = "92b45e47"  # P1-0's pinned execution head: accepted Task 8 (and Tasks 9-11 merged) before any RC change
# Task 8's absolute targets (notes/server-performance-task8-benchmark-results.md), unchanged: judged by its own
# measurements, never relaxed by this one.
TASK8_TARGETS = {
    "known/echo lines/s (B alone)": ">= 6,500",
    "real server mask lines/s at 200k and 1M": ">= 1,000",
    "20 KB document mask p50": "<= 30 ms",
    "unmask lines/s at 200k / 1M": ">= 2,114 / >= 2,014",
    "new key mask lines/s": ">= 153",
    "counter read p50": "<= 2 ms",
    "catch-up of 1 and 100 values": "<= 5 ms",
    "retained index": "<= 25 MB/million",
    "opening to background ready": "<= 5 s/million",
}
ROOT_KEY_B64 = base64.b64encode(bytes(range(32))).decode()  # a test key: the runs' vaults are synthetic
SERVER_URL = "https://latet.bench.lan"
EMAILS = [f"bench.person{n:04d}@example.com" for n in range(64)]
PHONES = [f"+39 347 555 {n:04d}" for n in range(64)]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


# --- the frozen fixtures ----------------------------------------------------------------------------------------------


def _chat(texts: Sequence[str], *, stream: bool = False, model: str = "gpt-4.1-mini") -> bytes:
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)]
    if messages and messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "and then?"})
    return json.dumps({"model": model, "messages": messages, **({"stream": True} if stream else {})}).encode()


def _case(
    case_id: str,
    machines: Sequence[str],
    *,
    method: str = "POST",
    path: str = "/llm/v1/chat/completions",
    body: bytes = b"",
    headers: Sequence[tuple[str, str]] = (("authorization", "Bearer sk-bench"), ("content-type", "application/json")),
    mode: str = "warm",
    transport: str = "http",
    frames: Sequence[str] = (),
    prime: Sequence[bytes] = (),
    settings: dict[str, Any] | None = None,
    state: str | None = None,
    bodyless: bool = False,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "machines": list(machines),
        "transport": transport,
        "method": method,
        "path": path,
        "headers": [list(h) for h in headers],
        "body": base64.b64encode(body).decode(),
        "frames": list(frames),
        "mode": mode,
        "prime": [base64.b64encode(p).decode() for p in prime],
        "settings": settings or {},
        "state": state,
        "bodyless": bodyless,
        "note": note,
    }


def fixtures(seed: int = 20261104) -> dict[str, Any]:
    """Every case of the measurement bullet, as frozen requests (deterministic from ``seed``)."""
    rng = random.Random(seed)
    both = ("computer", "server")

    def text(n: int) -> str:
        return f"write to {rng.choice(EMAILS)} or call {rng.choice(PHONES)} about item {n}"

    small = _chat([text(0)])
    big_texts = [" ".join([text(i)] + ["a long paragraph of an ordinary document"] * 120) for i in range(180)]
    many = _chat([f"line {i}: {text(i)}" for i in range(4000)])
    history = [f"turn {i}: {text(i)}" for i in range(500)]
    image = json.dumps(
        {
            "model": "gpt-4.1-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text(1)},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
                    ],
                }
            ],
        }
    ).encode()
    turn = json.dumps(
        {
            "type": "response.create",
            "model": "gpt-5",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text(2)}]}],
        }
    )
    common = " ".join(["the example.com team"] * 40)
    cases = [
        _case(
            "bodyless-get",
            both,
            method="GET",
            path="/llm/v1/models",
            headers=(("authorization", "Bearer sk-bench"),),
            bodyless=True,
            note="a bodyless GET: the 2 ms p50 bar",
        ),
        _case("small-warm", both, body=small, note="a small chat, warm"),
        _case("small-cold", both, body=small, mode="cold", note="a small chat, the first of a fresh runtime or key"),
        _case("small-stream", both, body=_chat([text(3)], stream=True), note="a small chat streamed"),
        _case("large-json", both, body=_chat(big_texts), note="about 1 MiB of JSON"),
        _case("many-slots", both, body=many, note="4,000 text slots"),
        _case(
            "media-withheld",
            both,
            body=image,
            settings={"images": "mask"},
            note="an image withheld: images mask, its text not read",
        ),
        _case(
            "cached-500-plus-1",
            both,
            body=_chat([*history, text(9)]),
            prime=[_chat(history)],
            note="500 texts masked before, then 500 + 1",
        ),
        _case("cached-one-alone", both, body=_chat([text(9)]), note="the 1 alone, Task 8's comparator"),
        _case(
            "signed-retry",
            ("computer",),
            path="/bedrock/us-east-1/profile/default/{profile_key}/model/"
            "anthropic.claude-sonnet-4-5-20250929-v1%3A0/invoke",
            headers=(("content-type", "application/json"),),
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hello there"}],
                }
            ).encode(),
            state="aws-expired",
            note="AWS's expired token: the signed request built and sent again",
        ),
        _case(
            "ws-turn",
            both,
            transport="ws",
            method="GET",
            path="/llm/v1/responses",
            headers=(("authorization", "Bearer sk-bench"),),
            frames=[turn],
            note="a WebSocket: handshake and a turn",
        ),
        _case(
            "stored-key-working0",
            ("server",),
            body=small,
            state="working0",
            note="an API key's vault in its store, none of it in memory (key_working_values 0)",
        ),
        _case("index-on", ("server",), body=small, state="index-on", note="the key's store with its index of prints"),
        _case("index-off", ("server",), body=small, state="index-off", note="the key's store asked as 0.10.0 asked"),
        _case(
            "envelope-key-candidates",
            ("server",),
            body=_chat([common]),
            state="key-candidates",
            headers=(
                ("authorization", "Bearer sk-bench"),
                ("content-type", "application/json"),
                ("user-agent", "bench " + common[:200]),
            ),
            note="D-3: envelope and body slots of a key's call whose words open about CANDIDATES_AT_MOST entries",
        ),
        _case(
            "envelope-person-whole",
            ("server",),
            body=_chat([common]),
            state="person-whole",
            headers=(
                ("authorization", "Bearer sk-bench"),
                ("content-type", "application/json"),
                ("user-agent", "bench " + common[:200]),
            ),
            note="D-3: the same call by a person whose vault is above WHOLE_VALUES",
        ),
    ]
    doc = {"schema": SCHEMA, "seed": seed, "cases": cases}
    doc["sha256"] = _digest(cases)
    return doc


def _digest(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_fixtures(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text("utf-8"))
    if doc.get("schema") != SCHEMA or doc.get("sha256") != _digest(doc["cases"]):
        raise SystemExit(f"{path}: not the frozen fixtures it says it is (its SHA-256 differs)")
    return doc


# --- the provider: an echo, in process (a mock transport) or on loopback ------------------------------------------


def provider_answer(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    """What the fake provider answers: a model list, a chat completion (whole or streamed) saying back the last user
    text, a Bedrock call (ExpiredToken to a first credential named so), anything else an empty JSON object."""
    json_type = {"content-type": "application/json"}
    if method == "GET":
        return 200, json_type, b'{"object":"list","data":[{"id":"gpt-4.1-mini","object":"model"}]}'
    try:
        doc = json.loads(body) if body else {}
    except ValueError:
        doc = {}
    said = ""
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                said = content if isinstance(content, str) else json.dumps(content)[:200]
                break
    if "/invoke" in path:
        if "expired" in headers.get("x-amz-security-token", ""):
            error = {"message": "The security token included in the request is expired"}
            return 403, {**json_type, "x-amzn-errortype": "ExpiredTokenException"}, json.dumps(error).encode()
        answer = {"content": [{"type": "text", "text": "You said: " + said}], "stop_reason": "end_turn"}
        return 200, json_type, json.dumps(answer).encode()
    if isinstance(doc, dict) and doc.get("stream"):
        chunks = [said[i : i + 16] for i in range(0, min(len(said), 512), 16)] or [""]
        events = b"".join(
            b"data: " + json.dumps({"id": "c1", "choices": [{"index": 0, "delta": {"content": c}}]}).encode() + b"\n\n"
            for c in chunks
        )
        return 200, {"content-type": "text/event-stream"}, events + b"data: [DONE]\n\n"
    answer = {
        "id": "c1",
        "object": "chat.completion",
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "You said: " + said[:512]},
                "finish_reason": "stop",
            }
        ],
    }
    return 200, json_type, json.dumps(answer).encode()


def mock_client() -> Any:
    """An httpx client whose provider is ``provider_answer`` in this process (as ``make_client``'s: no redirect, no
    proxy of the environment)."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = provider_answer(
            request.method, request.url.path, dict(request.headers), request.content
        )
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))  # streamed, as a provider's

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)


class LoopbackProvider:
    """The echo on a port of 127.0.0.1, HTTP and WebSocket (a turn said back in two deltas and its end), served by
    uvicorn in a thread of its own."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server: Any = None
        self.thread: threading.Thread | None = None

    async def app(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            await receive()  # connect
            await send({"type": "websocket.accept"})
            while True:
                message = await receive()
                if message["type"] != "websocket.receive":
                    return
                try:
                    doc = json.loads(message.get("text") or "{}")
                    said = json.dumps(doc.get("input"))[:200]
                except ValueError:
                    said = ""
                half = len(said) // 2
                for event in (
                    {"type": "response.created", "response": {"id": "resp_b"}},
                    {"type": "response.output_text.delta", "delta": said[:half]},
                    {"type": "response.output_text.delta", "delta": said[half:]},
                    {"type": "response.completed", "response": {"id": "resp_b", "status": "completed"}},
                ):
                    await send({"type": "websocket.send", "text": json.dumps(event)})
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        status, answer_headers, answer = provider_answer(scope["method"], scope["path"], headers, body)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in answer_headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": answer})

    def start(self) -> LoopbackProvider:
        import uvicorn

        config = uvicorn.Config(self.app, log_config=None, log_level="warning", interface="asgi3", ws="auto")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline:
                raise SystemExit("the loopback provider did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(30)
        self.sock.close()


# --- driving an ASGI app ----------------------------------------------------------------------------------------------


async def http_request(
    app: Any, method: str, target: str, headers: Sequence[tuple[str, str]], body: bytes, host: str, scheme: str
) -> tuple[int | None, bytes, float]:
    """One request through ``app`` as uvicorn hands it over; its status, its answer and when its last byte came
    (``time.perf_counter``)."""
    raw_path, _, query = target.partition("?")
    from urllib.parse import unquote

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": [(b"host", host.encode())] + [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("127.0.0.1", 50999),
        "server": (host.split(":")[0], 443 if scheme == "https" else 80),
        "state": {},
    }
    status: list[int | None] = [None]
    answer = bytearray()
    ended = [0.0]
    done = asyncio.Event()
    asked = [False]

    async def receive() -> dict[str, Any]:
        if not asked[0]:
            asked[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status[0] = message["status"]
        elif message["type"] == "http.response.body":
            answer.extend(message.get("body", b""))
            if not message.get("more_body"):
                ended[0] = time.perf_counter()
                done.set()

    try:
        await asyncio.wait_for(app(scope, receive, send), 300)
    finally:
        done.set()
    return status[0], bytes(answer), ended[0] or time.perf_counter()


async def ws_request(
    app: Any, target: str, headers: Sequence[tuple[str, str]], frames: Sequence[str], host: str, scheme: str
) -> tuple[int | None, bytes, float]:
    """A WebSocket through ``app``: its handshake, each frame answered to its turn's end; 101 when accepted."""
    from urllib.parse import unquote

    raw_path, _, query = target.partition("?")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "wss" if scheme == "https" else "ws",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": [(b"host", host.encode())] + [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("127.0.0.1", 50998),
        "server": (host.split(":")[0], 443),
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    changed = asyncio.Event()
    state: dict[str, Any] = {"accepted": False, "ends": 0, "closed": False, "status": None}
    answer = bytearray()

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            state["accepted"] = True
        elif kind == "websocket.send":
            text = message.get("text") or ""
            answer.extend(text.encode())
            if "response.completed" in text or '"error"' in text:
                state["ends"] += 1
        elif kind in ("websocket.close", "websocket.http.response.body"):
            state["closed"] = True
        elif kind == "websocket.http.response.start":
            state["status"] = message["status"]
        changed.set()

    async def until(check: Callable[[], bool]) -> None:
        async with asyncio.timeout(60):
            while not check():
                changed.clear()
                await changed.wait()

    await inbox.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, receive, send))
    task.add_done_callback(lambda _: (state.__setitem__("closed", True), changed.set()))
    await until(lambda: state["accepted"] or state["closed"])
    for frame in frames if state["accepted"] else ():
        before = state["ends"]
        await inbox.put({"type": "websocket.receive", "text": frame})
        await until(lambda before=before: state["ends"] > before or state["closed"])
    ended = time.perf_counter()
    await inbox.put({"type": "websocket.disconnect", "code": 1000})
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, 30)
    return (101 if state["accepted"] else state["status"]), bytes(answer), ended


@contextlib.asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """``app``'s ASGI lifespan, started and, after, shut down."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, inbox.get, outbox.put)
    )
    await inbox.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(outbox.get(), 120)
    if started["type"] != "lifespan.startup.complete":
        raise RuntimeError(f"the app did not start: {started.get('message', started['type'])}")
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(outbox.get(), 120)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 30)


# --- the probes (never during a timed request) ----------------------------------------------------------------------


def private_bytes() -> int | None:
    _scripts_on_path()
    from terms_bench import memory_counters

    found = memory_counters()
    value = found.get("current_private_bytes") or found.get("current_resident_bytes")
    return value if isinstance(value, int) else None


@contextlib.contextmanager
def ledger_probe() -> Iterator[list[int]]:
    """Each decision's coverage ledger (its arena's ``body_ledger.retained``), on a head that keeps one (RC's
    ``outcome.finalize``); none on a head that has none."""
    found: list[int] = []
    try:
        from latet.gateway import outcome
    except ImportError:
        yield found
        return
    finalize = getattr(outcome, "finalize", None)
    if finalize is None:
        yield found
        return

    def probed(arena: Any, *args: Any, **kwargs: Any) -> Any:
        ledger = getattr(arena, "body_ledger", None)
        retained = getattr(ledger, "retained", None)
        if isinstance(retained, int):
            found.append(retained)
        return finalize(arena, *args, **kwargs)

    outcome.finalize = probed
    patched = []
    for name in ("latet.gateway.forward", "latet.gateway.ws", "latet.gateway.mediate"):
        module = sys.modules.get(name)
        if module is not None and getattr(module, "finalize", None) is finalize:
            module.finalize = probed  # type: ignore[attr-defined]
            patched.append(module)
    try:
        yield found
    finally:
        outcome.finalize = finalize
        for module in patched:
            module.finalize = finalize  # type: ignore[attr-defined]


def retained_of(runtime: Any) -> int | None:
    """The runtime's retained request bytes (RC-R1: provenance, audit, launch keys), on a head that counts them."""
    value = getattr(runtime, "request_retained_bytes", None)
    return value if isinstance(value, int) else None


def _summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "p50": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 4),
        "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
    }


# --- the environments (inside the measuring process) --------------------------------------------------------------


class Environment:
    """Where a case's requests go: an app, its address, the runtime that serves them, and what closes it."""

    def __init__(self, app: Any, prefix: str, host: str, scheme: str, runtime: Any, stack: contextlib.AsyncExitStack):
        self.app, self.prefix, self.host, self.scheme, self.runtime, self.stack = (
            app,
            prefix,
            host,
            scheme,
            runtime,
            stack,
        )
        self.profile_key = "k" * 22
        self.identity_id: str | None = None  # on a server: whose requests these are

    async def close(self) -> None:
        await self.stack.aclose()


async def computer_environment(folder: Path, ws_url: str, settings: dict[str, Any], aws: bool) -> Environment:
    from latet.config import Config, Destination, IdentityConfig, Profile
    from latet.gateway.app import create_app
    from latet.runtime import make_runtime

    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    config = Config(
        profile=Profile.BALANCED,
        ner="off",
        identity=IdentityConfig(enabled=False),
        upstreams=[Destination(name="llm", url=ws_url)],
        **settings,
    )
    runtime = make_runtime(config, data=folder / "data", config_file=folder / "config.yaml")
    stack = contextlib.AsyncExitStack()
    stack.callback(runtime.close)
    runtime.gateway.client = mock_client
    environment = Environment(None, "", "127.0.0.1:5283", "http", runtime, stack)
    if aws:
        environment.profile_key = _aws_expired(runtime, stack)
    app = create_app(runtime.gateway)
    await stack.enter_async_context(lifespan(app))
    environment.app = app
    return environment


def _aws_expired(runtime: Any, stack: contextlib.AsyncExitStack) -> str:
    """The computer's AWS profile ``default``, whose first credentials the provider finds expired, fresh once read
    again (``tests/request_coverage.py``'s ``aws-expired``): the profile route's key."""
    from latet.gateway.aws import profile_tag
    from latet.gateway.sigv4 import AwsCredentials

    aws = runtime.gateway.aws
    reads = [0]

    async def profile(name: str) -> Any:
        reads[0] += 1
        token = "expired-token" if reads[0] % 2 == 1 else "fresh-token"
        return AwsCredentials("AKIABENCHTEST", "bench-secret", token, None)

    saved = aws.profile, aws.invalidate
    aws.profile, aws.invalidate = profile, (lambda name: None)
    stack.callback(lambda: (setattr(aws, "profile", saved[0]), setattr(aws, "invalidate", saved[1])))
    return profile_tag(runtime.gateway.bridge_token or "", "default")


class ServerSide:
    """A server instance of the run's state (``bench_apps_server.py``'s folder), its app served in this process, the
    shared settings' destination ``llm`` at the loopback echo."""

    def __init__(self, folder: Path, working: int | None = None) -> None:
        _scripts_on_path()
        import bench_apps_server as bas

        self.bas = bas
        self.state = bas.State.load(folder)
        self.server = bas.open_bench_server(self.state, url=SERVER_URL)
        if working is not None:
            # Below the setting's floor (1,000): what bench_engine.py and vault_fuzz.py give StoredVault directly
            # (working=0, every value asked of the store), set past the settings' validation for this measurement.
            self.server.settings = self.server.settings.model_copy(update={"key_working_values": working})
        self.stack = contextlib.AsyncExitStack()

    async def start(self, ws_url: str, settings: dict[str, Any]) -> Any:
        from latet.config import Config, Destination, Profile
        from latet.server.operations import server_app

        config = Config(
            profile=Profile.BALANCED, ner="off", upstreams=[Destination(name="llm", url=ws_url)], **settings
        )
        self.server.shared.set(config, actor="bench")
        self.app = server_app(self.server)
        await self.stack.enter_async_context(lifespan(self.app))
        return self.app

    def key(self) -> tuple[str, str]:
        """A new API key: its id and its route prefix."""
        from latet.server.tokens import route_key

        identity, secret = self.server.identities.add_key(f"bench {time.monotonic_ns()}")
        return identity.id, f"/u/{route_key(secret)}"

    def state_key(self) -> str:
        from latet.server.tokens import route_key

        return f"/u/{route_key(self.state.token.read_text('utf-8').strip())}"

    async def close(self) -> None:
        await self.stack.aclose()
        self.server.close()


@contextlib.contextmanager
def server_client() -> Iterator[None]:
    """Each space's provider client the in-process echo (``make_client`` patched where the gateway's app reads it)."""
    from latet.gateway import app as gateway_app

    made = gateway_app.make_client
    gateway_app.make_client = lambda *args, **kwargs: mock_client()  # type: ignore[assignment]
    try:
        yield
    finally:
        gateway_app.make_client = made  # type: ignore[assignment]


# --- measuring (the child process) ----------------------------------------------------------------------------------


async def _send(environment: Environment, case: dict[str, Any], body: bytes) -> tuple[int | None, float, float]:
    """One request of ``case``: its status, wall seconds to its last byte, process CPU seconds over it."""
    headers = [tuple(h) for h in case["headers"]]
    target = environment.prefix + case["path"].replace("{profile_key}", environment.profile_key)
    cpu, began = time.process_time(), time.perf_counter()
    if case["transport"] == "ws":
        status, _, ended = await ws_request(
            environment.app, target, headers, case["frames"], environment.host, environment.scheme
        )
    else:
        status, _, ended = await http_request(
            environment.app, case["method"], target, headers, body, environment.host, environment.scheme
        )
    return status, ended - began, time.process_time() - cpu


async def _environment(
    machine: str, case: dict[str, Any], config: dict[str, Any], folder: Path, ws_url: str, side: ServerSide | None
) -> Environment:
    settings = dict(case["settings"])
    if machine == "computer":
        return await computer_environment(folder, ws_url, settings, case["state"] == "aws-expired")
    assert side is not None
    stack = contextlib.AsyncExitStack()
    host = SERVER_URL.split("://")[1]
    if case["state"] == "person-whole":
        environment = Environment(None, "", host, "https", None, stack)
        environment.identity_id = side.state.key
        environment.app, environment.runtime = await _person_app(side, stack)
        return environment
    if case["state"] in ("working0", "index-on", "index-off", "key-candidates"):
        identity_id, prefix = side.state.key, side.state_key()  # the state's key, its vault filled
    else:
        identity_id, prefix = side.key()  # a new key: its first request builds its space (cold)
    environment = Environment(side.app, prefix, host, "https", None, stack)
    environment.identity_id = identity_id
    if settings:  # an identity's own settings (images, say: a server's shared settings keep no such rule)
        runtime = await _key_runtime(side, identity_id)
        await runtime.mediator.run(runtime.update_config, lambda c: c.model_copy(update=settings))
    return environment


async def _person_app(side: ServerSide, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
    """A person's space (the state's identity, a person's vault), its gateway's app served directly: a person reaches
    the server from a computer, whose connection this measurement does not make."""
    from latet.gateway.app import create_app

    identity = side.server.identities.get(side.state.key)
    if identity is None:
        raise SystemExit("the state's person is gone")
    runtime = await side.server.spaces.runtime(identity)
    app = create_app(runtime.gateway)
    await stack.enter_async_context(lifespan(app))
    return app, runtime


async def measure_case(
    machine: str, case: dict[str, Any], config: dict[str, Any], folder: Path, ws_url: str, side: ServerSide | None
) -> dict[str, Any]:
    """``case`` on ``machine``: its timed requests (wall and CPU milliseconds, refusals apart), then its probe pass."""
    body = base64.b64decode(case["body"])
    primes = [base64.b64decode(p) for p in case["prime"]]
    rounds, warm, requests = config["rounds"], config["warm"], config["requests"]
    walls: list[float] = []
    cpus: list[float] = []
    statuses: Counter[str] = Counter()
    refused = 0
    for round_ in range(rounds):
        environment = await _environment(machine, case, config, folder / f"{case['id']}-{round_}", ws_url, side)
        try:
            for prime in primes:
                await _send(environment, case, prime)
            timed = 1 if case["mode"] == "cold" else requests
            for _ in range(0 if case["mode"] == "cold" else warm):
                await _send(environment, case, body)
            for _ in range(timed):
                status, wall, cpu = await _send(environment, case, body)
                statuses[str(status)] += 1
                if status is None or status >= 400:
                    refused += 1  # counted apart: never a speed
                    continue
                walls.append(wall * 1000)
                cpus.append(cpu * 1000)
        finally:
            await environment.close()
    probes = await probe_case(machine, case, config, folder, ws_url, side, body, primes)
    return {
        "id": case["id"],
        "machine": machine,
        "statuses": dict(statuses),
        "sent": len(walls),
        "refused": refused,
        "wall_ms": _summary(walls),
        "cpu_ms": _summary(cpus),
        "bodyless": case["bodyless"],
        "note": case["note"],
        **probes,
    }


async def probe_case(
    machine: str,
    case: dict[str, Any],
    config: dict[str, Any],
    folder: Path,
    ws_url: str,
    side: ServerSide | None,
    body: bytes,
    primes: Sequence[bytes],
) -> dict[str, Any]:
    """One request of ``case`` again, never timed: the traced peak, the ledger's retained bytes, the runtime's retained
    request bytes and the process's private bytes around it."""
    environment = await _environment(machine, case, config, folder / f"{case['id']}-probe", ws_url, side)
    try:
        for prime in primes:
            await _send(environment, case, prime)
        before = private_bytes()
        with ledger_probe() as ledgers:
            tracemalloc.start()
            try:
                await _send(environment, case, body)
            finally:
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
        after = private_bytes()
        runtime = environment.runtime
        if runtime is None and side is not None and environment.identity_id is not None:
            runtime = await _key_runtime(side, environment.identity_id)
        return {
            "traced_peak_bytes": peak,
            "ledger_peak_bytes": max(ledgers) if ledgers else None,
            "retained_request_bytes": retained_of(runtime) if runtime is not None else None,
            "private_bytes_before": before,
            "private_bytes_after": after,
        }
    finally:
        await environment.close()


async def _key_runtime(side: ServerSide, identity_id: str) -> Any:
    """The runtime of the identity ``identity_id`` on this instance (its space, built already by its requests)."""
    identity = side.server.identities.get(identity_id)
    return None if identity is None else await side.server.spaces.runtime(identity)


async def child_measure(config: dict[str, Any]) -> dict[str, Any]:
    doc = load_fixtures(Path(config["fixtures"]))
    folder = Path(config["folder"])
    machine = config["machine"]
    cases = [
        c for c in doc["cases"] if machine in c["machines"] and (not config["cases"] or c["id"] in config["cases"])
    ]
    provider = LoopbackProvider().start()
    results = []
    try:
        if machine == "computer":
            for case in cases:
                results.append(await measure_case(machine, case, config, folder, provider.url, None))
        else:
            results = await _server_cases(cases, config, folder, provider.url)
    finally:
        provider.stop()
    return {"machine": machine, "fixtures_sha256": doc["sha256"], "cases": results}


async def _server_cases(cases: list[dict[str, Any]], config: dict[str, Any], folder: Path, ws_url: str) -> list[Any]:
    """The server's cases, grouped by the instance each needs (key_working_values 0, the index off, a person's state),
    each group on its own instance of the run's state."""
    _scripts_on_path()
    import bench_apps_server as bas

    results = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        kind = case["state"] if case["state"] in ("working0", "index-off", "person-whole") else "plain"
        groups.setdefault(kind, []).append(case)
    for kind, chosen in groups.items():
        state_folder = Path(config["person_state"] if kind == "person-whole" else config["key_state"])
        with server_client(), bas.indexing(kind != "index-off"):
            side = ServerSide(state_folder, working=0 if kind == "working0" else None)
            try:
                await side.start(ws_url, {})
                for case in chosen:
                    results.append(await measure_case("server", case, config, folder, ws_url, side))
            finally:
                await side.close()
    return results


# --- the finalizer, the ledger, the micro measurements (children too) ---------------------------------------------


async def child_finalizer(config: dict[str, Any]) -> dict[str, Any]:
    """The end-of-answer latency of one identity's small answer (whole JSON and a stream) alone, and while another of
    its requests is masking on its engine thread (a large body started just before): the response finalizer settles
    each answer's capture on that thread before the answer ends (02dbf660)."""
    folder = Path(config["folder"])
    provider = LoopbackProvider().start()
    out: dict[str, Any] = {}
    rng = random.Random(1)
    big = _chat([" ".join(f"write to {rng.choice(EMAILS)} today" for _ in range(400)) for _ in range(60)])
    try:
        environment = await computer_environment(folder / "finalizer", provider.url, {}, False)
        try:
            for name, small in (("whole", _chat(["hello there"])), ("stream", _chat(["hello there"], stream=True))):
                case = {
                    "transport": "http",
                    "method": "POST",
                    "path": "/llm/v1/chat/completions",
                    "headers": [["authorization", "Bearer sk-bench"], ["content-type", "application/json"]],
                }
                alone: list[float] = []
                during: list[float] = []
                for _ in range(config["rounds"]):
                    status, wall, _ = await _send(environment, case, small)
                    if status == 200:
                        alone.append(wall * 1000)
                    masking = asyncio.create_task(_send(environment, case, big))
                    await asyncio.sleep(0.005)  # the large request is masking on the engine thread by now
                    status, wall, _ = await _send(environment, case, small)
                    if status == 200:
                        during.append(wall * 1000)
                    await masking
                a, d = _summary(alone), _summary(during)
                out[name] = {
                    "alone_ms": a,
                    "during_masking_ms": d,
                    "added_p50_ms": None if a["p50"] is None or d["p50"] is None else round(d["p50"] - a["p50"], 3),
                }
        finally:
            await environment.close()
    finally:
        provider.stop()
    return {"machine": "computer", "finalizer": out}


async def child_written(config: dict[str, Any]) -> dict[str, Any]:
    """How long the event loop is held while the gateway reads what the model wrote in an answer it passes on as it came
    (Claude Code's), an answer as large as it reads (``WRITTEN_BYTES``): a whole body, one event giving a tool call's
    input whole, or that input in pieces. Each round: the longest turn of the loop (a task asks for one every 5 ms),
    the answer's whole time, and what Python's own parse of the input alone takes on this machine. A measurement, never
    a test's bound: the unit test pins that the parse runs on the engine's thread (minors after 0.11, item 17)."""
    import httpx

    from latet.config import Config
    from latet.engine.build import build
    from latet.engine.vault import Vault
    from latet.gateway.app import GatewayConfig, create_app
    from latet.gateway.mediate import Mediator
    from latet.gateway.routes import Upstream
    from latet.gateway.written import WRITTEN_BYTES

    session = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
    headers = {
        "content-type": "application/json",
        "user-agent": "claude-cli/2.1.291 (external, cli)",
        "x-claude-code-session-id": session,
    }
    asked = {"model": "claude-sonnet-4-6", "max_tokens": 10, "messages": [{"role": "user", "content": "write"}]}
    upstream = Upstream(
        "anthropic",
        "/anthropic",
        "http://127.0.0.1:1",
        envelope_expected=frozenset({("x-claude-code-session-id", session)}),
    )

    def event(found: dict[str, Any]) -> bytes:
        return f"event: {found['type']}\ndata: {json.dumps(found)}\n\n".encode()

    def answer(shape: str) -> tuple[str, bytes, str]:
        items = [f"v{i:07d}" for i in range((WRITTEN_BYTES - 2**20) // (15 if shape == "pieces" else 12))]
        given = {"items": items}
        raw = json.dumps(given)
        tool = {"type": "tool_use", "id": "t1", "name": "Write", "input": given}
        if shape == "whole":
            body = {"id": "msg_1", "type": "message", "role": "assistant", "content": [tool]}
            return "application/json", json.dumps(body).encode(), raw
        if shape == "event":
            start = {"type": "content_block_start", "index": 0, "content_block": tool}
            return "text/event-stream", event(start) + event({"type": "content_block_stop", "index": 0}), raw
        start = {"type": "content_block_start", "index": 0, "content_block": {**tool, "input": {}}}
        deltas = [
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}}
            for piece in (raw[at : at + 4096] for at in range(0, len(raw), 4096))
        ]
        stop = {"type": "content_block_stop", "index": 0}
        return "text/event-stream", b"".join(event(found) for found in (start, *deltas, stop)), raw

    out: dict[str, Any] = {}
    for shape in ("whole", "event", "pieces"):
        content_type, body, raw = answer(shape)
        longest: list[float] = []
        took: list[float] = []
        parse: list[float] = []
        for _ in range(config["rounds"]):
            started = time.perf_counter()
            json.loads(raw)
            parse.append((time.perf_counter() - started) * 1000)
            built = build(Config(), Vault(bytes(range(32))))
            mediator = Mediator(built.engine, built.verifier)

            def provider(_: httpx.Request, body: bytes = body, content_type: str = content_type) -> httpx.Response:
                return httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body))

            settings = GatewayConfig(
                port=5283,
                upstreams=[upstream],
                mediator=mediator,
                client=lambda provider=provider: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
            )
            app = create_app(settings)
            gaps: list[float] = []
            done = asyncio.Event()

            async def tick(gaps: list[float] = gaps, done: asyncio.Event = done) -> None:
                last = time.perf_counter()
                while not done.is_set():
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
            ):
                ticking = asyncio.create_task(tick())
                await asyncio.sleep(0.02)
                started = time.perf_counter()
                answered = await client.post("/anthropic/v1/messages", json=asked, headers=headers)
                took.append((time.perf_counter() - started) * 1000)
                done.set()
                await ticking
            if answered.status_code != 200:
                raise SystemExit(f"the {shape} answer was answered {answered.status_code}")
            longest.append(max(gaps) * 1000)
        out[shape] = {
            "longest_turn_ms": _summary(longest),
            "answer_ms": _summary(took),
            "parse_alone_ms": _summary(parse),
        }
    return {"machine": "computer", "written": out}


async def child_ledger(config: dict[str, Any]) -> dict[str, Any]:
    """The coverage ledger a repeated key costs (the scattered round's note: about 14 KB for a 55-byte body with one):
    the ledger retained by bodies with 0 to 8 keys given twice, small and with a longer text."""
    folder = Path(config["folder"])
    provider = LoopbackProvider().start()
    rows = []
    try:
        environment = await computer_environment(folder / "ledger", provider.url, {}, False)
        try:
            case = {
                "transport": "http",
                "method": "POST",
                "path": "/llm/v1/completions",
                "headers": [["authorization", "Bearer sk-bench"], ["content-type", "application/json"]],
            }
            for name, text in (("short", "hello"), ("long", "a longer prompt " * 64)):
                for repeated in range(0, 9):
                    # The key given ``repeated`` more times before the copy that is read (the scattered round's body:
                    # {"model":"gpt-4o","prompt":"removed","prompt":"hello"}, one repeat).
                    prompts = ['"prompt":"removed"'] * repeated + ['"prompt":' + json.dumps(text)]
                    body = ('{"model":"gpt-4o",' + ",".join(prompts) + "}").encode()
                    with ledger_probe() as ledgers:
                        status, _, _ = await _send(environment, case, body)
                    rows.append(
                        {
                            "text": name,
                            "body_bytes": len(body),
                            "repeated": repeated,
                            "status": status,
                            "ledger_bytes": max(ledgers) if ledgers else None,
                        }
                    )
        finally:
            await environment.close()
    finally:
        provider.stop()
    slopes: dict[str, Any] = {}
    for name in ("short", "long"):
        mine = [r for r in rows if r["text"] == name and r["ledger_bytes"] is not None]
        base = next((r for r in mine if r["repeated"] == 0), None)
        found = [(r["ledger_bytes"] - base["ledger_bytes"]) / r["repeated"] for r in mine if base and r["repeated"]]
        slopes[name] = _summary(found)
    return {"rows": rows, "bytes_per_repeated_key": slopes}


async def child_micro(config: dict[str, Any]) -> dict[str, Any]:
    """RC-REC-3's settings read (``spaces._counts_and_now``: PostgreSQL's clock and the settings' counts in one
    statement) and RC-REC-2's Valkey read and commit of opaque provenance (``ValkeyEchoes.read`` and ``keep``), each
    ``calls`` times on an API key's space of the run's state: milliseconds a call."""
    from latet.server import spaces as spaces_module

    calls = config["calls"]
    out: dict[str, Any] = {}
    side = ServerSide(Path(config["key_state"]))
    try:
        await side.start("http://127.0.0.1:9", {})
        read = getattr(spaces_module, "_counts_and_now", None)
        owner_of = getattr(spaces_module, "settings_owner", None)
        if read is None or owner_of is None:
            out["settings_read_ms"] = None
        else:
            owner = owner_of(side.state.key)
            times = []
            for _ in range(calls):
                began = time.perf_counter()
                await asyncio.to_thread(read, side.server.db, owner)
                times.append((time.perf_counter() - began) * 1000)
            out["settings_read_ms"] = _summary(times)
        with server_client():
            status, _, _ = await http_request(
                side.app,
                "POST",
                side.state_key() + "/llm/v1/chat/completions",
                [("authorization", "Bearer sk-bench"), ("content-type", "application/json")],
                _chat(["hello"]),
                SERVER_URL.split("://")[1],
                "https",
            )
        runtime = await _key_runtime(side, side.state.key)
        echoes = getattr(getattr(runtime, "mediator", None), "echoes", None)
        if echoes is None or not hasattr(echoes, "keep"):
            out["valkey_read_ms"] = out["valkey_commit_ms"] = None
        else:
            members = tuple(hashlib.sha256(f"bench {i}".encode()).digest() for i in range(4))
            reads, commits = [], []
            for n in range(calls):
                began = time.perf_counter()
                await asyncio.to_thread(echoes.keep, hashlib.sha256(f"conversation {n}".encode()).digest(), members)
                commits.append((time.perf_counter() - began) * 1000)
                began = time.perf_counter()
                await asyncio.to_thread(echoes.read, members)
                reads.append((time.perf_counter() - began) * 1000)
            out["valkey_read_ms"], out["valkey_commit_ms"] = _summary(reads), _summary(commits)
        out["warm_status"] = status
    finally:
        await side.close()
    return out


async def child_shared(config: dict[str, Any]) -> dict[str, Any]:
    """For ``--workers N``: the shared settings of the run's state given the destination ``llm`` at the loopback echo,
    by the measured source (whose schema it is), and the state's API key's route."""
    from latet.config import Config, Destination, Profile
    from latet.server.tokens import route_key

    _scripts_on_path()
    import bench_apps_server as bas

    state = bas.State.load(Path(config["key_state"]))
    opened = bas.open_bench_server(state, url=config["url"])
    try:
        shared = Config(
            profile=Profile.BALANCED, ner="off", upstreams=[Destination(name="llm", url=config["provider"])]
        )
        opened.shared.set(shared, actor="bench")
    finally:
        opened.close()
    # The program `latet server run --workers N` is started by, as the measured source's bench writes it.
    return {"prefix": f"/u/{route_key(state.token.read_text('utf-8').strip())}", "serve": bas._SERVE}


CHILDREN = {
    "measure": child_measure,
    "finalizer": child_finalizer,
    "written": child_written,
    "ledger": child_ledger,
    "micro": child_micro,
    "shared": child_shared,
}


class Forwarder:
    """Inside a Linux container on Docker's bridge: each test service's loopback address (as its variable names it)
    forwarded to where the service answers on the bridge (``--forward-services``), so that the measurement, its
    children and the server's workers reach the services at 127.0.0.1 as everywhere else. A thread of its own."""

    def __init__(self, pairs: Sequence[tuple[int, str, int]]) -> None:
        self.pairs = pairs
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()

        async def serve() -> None:
            for local, host, port in self.pairs:

                async def handle(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str = host, port: int = port
                ) -> None:
                    remote_reader, remote_writer = await asyncio.open_connection(host, port)
                    for side in (writer, remote_writer):  # no Nagle wait on either leg: a round trip stays one
                        sock = side.get_extra_info("socket")
                        if sock is not None:
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

                await asyncio.start_server(handle, "127.0.0.1", local)
            self.ready.set()

        self.loop.run_until_complete(serve())
        self.loop.run_forever()

    def start(self) -> Forwarder:
        self.thread.start()
        if not self.ready.wait(30):
            raise SystemExit("the service forwarders did not start")
        return self


def forward_services(remotes: Sequence[str]) -> Forwarder:
    """``LATET_TEST_DATABASE_URL``'s and ``LATET_TEST_VALKEY_URL``'s loopback ports forwarded to ``remotes`` (each
    ``host:port``, in that order)."""
    from urllib.parse import urlsplit

    pairs = []
    for variable, remote in zip(("LATET_TEST_DATABASE_URL", "LATET_TEST_VALKEY_URL"), remotes, strict=True):
        local = urlsplit(os.environ[variable]).port
        host, _, port = remote.rpartition(":")
        if local is None:
            raise SystemExit(f"{variable} names no port")
        pairs.append((local, host, int(port)))
    return Forwarder(pairs).start()


def child(config_path: Path) -> int:
    # A script's own folder comes first on the path: the measured source's benches (PYTHONPATH) go before it here.
    if sys.path and Path(sys.path[0] or ".").resolve() == HERE:
        sys.path.pop(0)
    _scripts_on_path()
    config = json.loads(config_path.read_text("utf-8"))
    result = asyncio.run(CHILDREN[config["what"]](config))
    Path(config["out"]).write_text(json.dumps(result, sort_keys=True, indent=1), "utf-8")
    return 0


# --- the parent: private homes, the source, the states --------------------------------------------------------------


def _private_environment(work: Path, source: Path) -> dict[str, str]:
    """The child's environment: ``source``'s ``src`` first on the path, homes and data folders of the run's own (never
    yours), a test root key, no keyring, none of your connections; the test services' two addresses kept."""
    homes = work / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATET_") or k.startswith("LATET_TEST_")}
    paths = {
        "HOME": homes,
        "USERPROFILE": homes,
        "APPDATA": homes / "roaming",
        "LOCALAPPDATA": homes / "local",
        "XDG_CONFIG_HOME": homes / ".config",
        "CLAUDE_CONFIG_DIR": homes / ".claude",
        "CODEX_HOME": homes / ".codex",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    env.update({k: str(v) for k, v in paths.items()})
    if os.name == "nt":
        env.update(HOMEDRIVE=homes.drive, HOMEPATH=str(homes)[len(homes.drive) :])
    env["LATET_DATA_DIR"] = str(work / "data")
    env["LATET_CONFIG"] = str(work / "config.yaml")
    env["LATET_ROOT_KEY"] = ROOT_KEY_B64
    env["LATET_KEYRING"] = "off"
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    # The source's own Task 8 benches first (bench_apps_server.py follows its head's server API), then this script's.
    scripts = source / "scripts"
    paths_first = [source / "src", *([scripts] if (scripts / "bench_apps_server.py").exists() else [])]
    env["PYTHONPATH"] = os.pathsep.join([*map(str, paths_first), str(HERE)])
    return env


def _bench_script(source: Path) -> Path:
    """``bench_apps_server.py`` of the measured source (its head's server API), this checkout's when it has none."""
    own = source / "scripts" / "bench_apps_server.py"
    return own if own.exists() else HERE / "bench_apps_server.py"


def _scripts_on_path() -> None:
    """This script's folder importable (its Task 8 benches), after what ``PYTHONPATH`` already puts first: a measured
    source's own."""
    if str(HERE) not in sys.path:
        sys.path.append(str(HERE))


def _pin(source: Path) -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True)
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=source,
            capture_output=True,
            text=True,
            check=True,
        )
        return head.stdout.strip() + (" (dirty)" if dirty.stdout.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _run_child(what: str, config: dict[str, Any], work: Path, source: Path, python: str, seconds: float) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / f"{what}-config.json"
    out = work / f"{what}-out.json"
    config_path.write_text(json.dumps({**config, "what": what, "out": str(out)}), "utf-8")
    env = _private_environment(work, source)
    began = time.monotonic()
    found = subprocess.run(
        [python, str(Path(__file__).resolve()), "_child", str(config_path)], cwd=work, env=env, timeout=seconds
    )
    if found.returncode != 0 or not out.exists():
        raise SystemExit(f"the {what} process failed (exit {found.returncode})")
    result = json.loads(out.read_text("utf-8"))
    result["elapsed_s"] = round(time.monotonic() - began, 1)
    return result


def _build_state(folder: Path, source: Path, python: str, work: Path, values: int, person: bool) -> Path:
    """A state of the run's own (``bench_apps_server.py build``) under ``source``: its schema, master key, identity
    and vault of ``values`` values."""
    command = [
        python,
        str(_bench_script(source)),
        "--folder",
        str(folder),
        "build",
        "--values",
        str(values),
        "--seed",
        "1",
    ] + (["--person"] if person else [])
    found = subprocess.run(command, env=_private_environment(work, source), cwd=work, timeout=7200)
    if found.returncode != 0:
        raise SystemExit(f"the state could not be built (exit {found.returncode})")
    return folder


def _drop_state(folder: Path, source: Path, python: str, work: Path) -> None:
    if (folder / "state.json").exists():
        subprocess.run(
            [python, str(_bench_script(source)), "--folder", str(folder), "drop"],
            env=_private_environment(work, source),
            cwd=work,
            timeout=600,
        )


def _work_root(work: Path | None) -> Path | None:
    """Where the run's private folders go (made when missing); None: the system's temporary folder."""
    if work is not None:
        work.mkdir(parents=True, exist_ok=True)
    return work


def measure(args: argparse.Namespace) -> int:
    source = (args.source or ROOT).resolve()
    python = args.python or sys.executable
    fixture_doc = load_fixtures(args.fixtures)
    if args.workers != 1:
        return measure_workers(args, source, python, fixture_doc)
    work = Path(tempfile.mkdtemp(prefix=f"rcbench-{args.label}-", dir=_work_root(args.work)))
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "label": args.label,
        "source": str(source),
        "source_pin": _pin(source),
        "fixtures_sha256": fixture_doc["sha256"],
        "python": python,
        "platform": platform.platform(),
        "workers": 1,
        "rounds": args.rounds,
        "warm": args.warm,
        "requests": args.requests,
        "machines": {},
    }
    states: list[Path] = []
    try:
        for machine in args.machines:
            config = {
                "fixtures": str(args.fixtures.resolve()),
                "folder": str(work / machine),
                "machine": machine,
                "cases": args.cases or [],
                "rounds": args.rounds,
                "warm": args.warm,
                "requests": args.requests,
            }
            if machine == "server":
                _need_services()
                key_state = _build_state(work / "key-state", source, python, work, args.values, False)
                states.append(key_state)
                config["key_state"] = str(key_state)
                wanted = {c["id"] for c in fixture_doc["cases"] if "server" in c["machines"]}
                if (not args.cases or "envelope-person-whole" in args.cases) and "envelope-person-whole" in wanted:
                    person = _build_state(work / "person-state", source, python, work, args.person_values, True)
                    states.append(person)
                    config["person_state"] = str(person)
                if "person_state" not in config:
                    config["cases"] = [c for c in (args.cases or sorted(wanted)) if c != "envelope-person-whole"]
            result["machines"][machine] = _run_child("measure", config, work / machine, source, python, args.seconds)
    finally:
        for state in states:
            _drop_state(state, source, python, work)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    args.out.write_text(json.dumps(result, sort_keys=True, indent=1), "utf-8")
    _print_measure(result)
    return 0


def _need_services() -> None:
    if not os.environ.get("LATET_TEST_DATABASE_URL") or not os.environ.get("LATET_TEST_VALKEY_URL"):
        raise SystemExit("the server's cases need LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL")


def _print_measure(result: dict[str, Any]) -> None:
    for machine, part in result["machines"].items():
        for case in part["cases"]:
            wall = case["wall_ms"]
            print(
                f"{result['label']} {machine} {case['id']}: sent {case['sent']}, refused {case['refused']}, "
                f"p50 {wall['p50']} ms (cpu {case['cpu_ms']['p50']}), ledger {case['ledger_peak_bytes']}, "
                f"retained {case['retained_request_bytes']}, traced {case['traced_peak_bytes']}"
            )


def measure_workers(args: argparse.Namespace, source: Path, python: str, fixture_doc: dict[str, Any]) -> int:
    """``--workers N`` (N > 1): `latet server run --workers N` (``bench_apps_server.py``'s program, SO_REUSEPORT:
    Linux alone) on the run's state, the shared destination ``llm`` at a loopback echo, the server's HTTP fixtures sent
    over loopback; wall milliseconds per request, refusals apart, and the workers' CPU seconds and memory over each
    case (``/proc``)."""
    if not sys.platform.startswith("linux"):
        raise SystemExit("--workers 2 needs Linux (one port shared by the workers with SO_REUSEPORT)")
    _need_services()
    import httpx

    _scripts_on_path()
    import bench_apps_server as bas

    work = Path(tempfile.mkdtemp(prefix=f"rcbench-{args.label}-w{args.workers}-", dir=_work_root(args.work)))
    state_folder = _build_state(work / "key-state", source, python, work, args.values, False)
    provider = LoopbackProvider().start()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "label": args.label,
        "source": str(source),
        "source_pin": _pin(source),
        "fixtures_sha256": fixture_doc["sha256"],
        "python": python,
        "platform": platform.platform(),
        "workers": args.workers,
        "machines": {"server": {"cases": []}},
    }
    process = None
    try:
        port = _free_port()
        # The shared destination set, and the key's route read, by the measured source itself (its schema is its own).
        shared = {"key_state": str(state_folder), "url": f"http://127.0.0.1:{port}", "provider": provider.url}
        made = _run_child("shared", shared, work / "shared", source, python, 600)
        prefix = made["prefix"]
        env = _private_environment(work, source)
        process = subprocess.Popen(
            [python, "-c", made["serve"], str(state_folder), str(args.workers), str(port)],
            env=env,
            cwd=work,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        url = f"http://127.0.0.1:{port}"
        _wait_ready(url)
        cases = [
            c
            for c in fixture_doc["cases"]
            if "server" in c["machines"]
            and c["transport"] == "http"
            and c["state"] in (None, "index-on")
            and (not args.cases or c["id"] in args.cases)
        ]
        with httpx.Client(base_url=url, timeout=300) as http:
            for case in cases:
                body = base64.b64decode(case["body"])
                headers = dict(tuple(h) for h in case["headers"])
                walls: list[float] = []
                refused = 0
                pids = _children(process.pid)
                cpu_before = sum(_cpu_seconds(p) for p in pids)
                for n in range(args.warm + args.requests * args.rounds):
                    for prime in case["prime"]:
                        http.request(
                            case["method"], prefix + case["path"], headers=headers, content=base64.b64decode(prime)
                        )
                    began = time.perf_counter()
                    answer = http.request(case["method"], prefix + case["path"], headers=headers, content=body)
                    wall = (time.perf_counter() - began) * 1000
                    if n < args.warm:
                        continue
                    if answer.status_code >= 400:
                        refused += 1
                    else:
                        walls.append(wall)
                result["machines"]["server"]["cases"].append(
                    {
                        "id": case["id"],
                        "machine": "server",
                        "sent": len(walls),
                        "refused": refused,
                        "wall_ms": _summary(walls),
                        "cpu_ms": {"p50": None},
                        "workers_cpu_s": round(sum(_cpu_seconds(p) for p in pids) - cpu_before, 3),
                        "workers_private_bytes": [_private_of(p) for p in pids],
                        "bodyless": case["bodyless"],
                        "note": case["note"],
                        "traced_peak_bytes": None,
                        "ledger_peak_bytes": None,
                        "retained_request_bytes": None,
                    }
                )
    finally:
        if process is not None:
            bas._stop_serving(process)
        provider.stop()
        _drop_state(state_folder, source, python, work)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    args.out.write_text(json.dumps(result, sort_keys=True, indent=1), "utf-8")
    _print_measure(result)
    return 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, seconds: float = 180) -> None:
    import httpx

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(url + "/_latet/ready", timeout=5).status_code == 200:
                return
        time.sleep(0.5)
    raise SystemExit("the server's workers were not ready")


def _children(pid: int) -> list[int]:
    found = [pid]
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                with contextlib.suppress(OSError), open(f"/proc/{entry}/stat") as stat:
                    if int(stat.read().rsplit(")", 1)[1].split()[1]) == pid:
                        found.append(int(entry))
    return found


def _cpu_seconds(pid: int) -> float:
    with contextlib.suppress(OSError), open(f"/proc/{pid}/stat") as stat:
        fields = stat.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")  # pyright: ignore[reportAttributeAccessIssue]
    return 0.0


def _private_of(pid: int) -> int | None:
    with contextlib.suppress(OSError), open(f"/proc/{pid}/smaps_rollup") as smaps:
        total = 0
        for line in smaps:
            if line.startswith(("Private_Clean:", "Private_Dirty:")):
                total += int(line.split()[1]) * 1024
        return total
    return None


def compare(args: argparse.Namespace) -> int:
    """The bar, case by case: medians over the requests each head sent; refusals apart."""
    base = json.loads(args.base.read_text("utf-8"))
    rc = json.loads(args.rc.read_text("utf-8"))
    rows = []
    ruling = False
    if base["fixtures_sha256"] != rc["fixtures_sha256"]:
        raise SystemExit("the two measurements ran other fixtures: no comparison")
    for machine, part in rc["machines"].items():
        theirs = {c["id"]: c for c in base["machines"].get(machine, {}).get("cases", [])}
        for case in part["cases"]:
            other = theirs.get(case["id"])
            row: dict[str, Any] = {"machine": machine, "id": case["id"], "rc_refused": case["refused"]}
            if other is None:
                row["verdict"] = "no base measurement"
            elif case["sent"] == 0 or other["sent"] == 0:
                row["verdict"] = "not comparable: refused" + (" on RC" if case["sent"] == 0 else " on the base")
                row["base_refused"] = other["refused"]
                ruling = ruling or case["sent"] == 0
            else:
                b, r = other["wall_ms"]["p50"], case["wall_ms"]["p50"]
                added = r - b
                ratio = added / b if b else None
                within = (ratio is not None and ratio <= BAR_RATIO) and (
                    not case["bodyless"] or added <= BAR_BODYLESS_MS
                )
                row |= {
                    "base_p50_ms": b,
                    "rc_p50_ms": r,
                    "added_ms": round(added, 4),
                    "added_ratio": None if ratio is None else round(ratio, 4),
                    "base_refused": other["refused"],
                    "base_cpu_p50_ms": other["cpu_ms"].get("p50"),
                    "rc_cpu_p50_ms": case["cpu_ms"].get("p50"),
                    "rc_ledger_peak_bytes": case.get("ledger_peak_bytes"),
                    "rc_retained_request_bytes": case.get("retained_request_bytes"),
                    "verdict": "within the bar" if within else "past the bar: root rules",
                }
                ruling = ruling or not within
            rows.append(row)
    verdict = {
        "schema": SCHEMA,
        "base": {"label": base["label"], "pin": base["source_pin"]},
        "rc": {"label": rc["label"], "pin": rc["source_pin"]},
        "fixtures_sha256": rc["fixtures_sha256"],
        "bar": {"added_median_ratio_at_most": BAR_RATIO, "bodyless_get_added_p50_ms_at_most": BAR_BODYLESS_MS},
        "task8_targets_unchanged": TASK8_TARGETS,
        "rows": rows,
        "ruling_needed": ruling,
    }
    if args.out:
        args.out.write_text(json.dumps(verdict, sort_keys=True, indent=1), "utf-8")
    for row in rows:
        print(
            f"{row['machine']} {row['id']}: {row.get('added_ms', '-')} ms added ({row.get('added_ratio', '-')}): "
            f"{row['verdict']}"
        )
    print("ruling needed" if ruling else "every case within the bar")
    return 1 if ruling else 0


def side_command(args: argparse.Namespace, what: str) -> int:
    source = (args.source or ROOT).resolve()
    python = args.python or sys.executable
    work = Path(tempfile.mkdtemp(prefix=f"rcbench-{what}-", dir=_work_root(args.work)))
    states = []
    try:
        config: dict[str, Any] = {
            "folder": str(work / what),
            "rounds": getattr(args, "rounds", 1),
            "calls": getattr(args, "calls", 1),
        }
        if what == "micro":
            _need_services()
            state = _build_state(work / "key-state", source, python, work, 100, False)
            states.append(state)
            config["key_state"] = str(state)
        result = _run_child(what, config, work / what, source, python, args.seconds)
    finally:
        for state in states:
            _drop_state(state, source, python, work)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    result |= {"schema": SCHEMA, "source_pin": _pin(source), "source": str(source)}
    args.out.write_text(json.dumps(result, sort_keys=True, indent=1), "utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1)[:3000])
    return 0


def dry_run(args: argparse.Namespace) -> int:
    """Every command once with tiny counts, on RC alone (its compare against itself): the harness works end to end."""
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    fixture_path = out / "fixtures.json"
    fixture_path.write_text(json.dumps(fixtures(), sort_keys=True), "utf-8")
    machines = ["computer"] + (["server"] if os.environ.get("LATET_TEST_DATABASE_URL") else [])
    common = {"source": None, "python": None, "work": args.work, "keep": False, "seconds": 1800.0}
    measured = argparse.Namespace(
        fixtures=fixture_path,
        label="rc-dry",
        out=out / "rc.json",
        machines=machines,
        cases=None,
        rounds=1,
        warm=1,
        requests=2,
        values=300,
        person_values=300,
        workers=1,
        **common,
    )
    measure(measured)
    again = argparse.Namespace(**{**vars(measured), "label": "rc-dry-again", "out": out / "rc-again.json"})
    measure(again)
    compare(argparse.Namespace(base=out / "rc-again.json", rc=out / "rc.json", out=out / "verdict.json"))
    side_command(argparse.Namespace(out=out / "finalizer.json", rounds=2, **common), "finalizer")
    side_command(argparse.Namespace(out=out / "written.json", rounds=1, **common), "written")
    side_command(argparse.Namespace(out=out / "ledger.json", **common), "ledger")
    if "server" in machines:
        side_command(argparse.Namespace(out=out / "micro.json", calls=5, **common), "micro")
    print(f"dry run written to {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--source", type=Path, help="the checkout whose src is measured (default: this one)")
        sub.add_argument("--python", help="the interpreter of that checkout's environment (default: this one)")
        sub.add_argument("--work", type=Path, default=None, help="where the run's private folders go")
        sub.add_argument("--keep", action="store_true", help="keep the run's private folders")
        sub.add_argument("--seconds", type=float, default=7200.0, help="a measuring process's deadline")
        sub.add_argument("--out", type=Path, required=True)

    made = commands.add_parser("fixtures", help="freeze the fixtures")
    made.add_argument("--out", type=Path, required=True)
    made.add_argument("--seed", type=int, default=20261104)
    measured = commands.add_parser("measure", help="measure the fixtures on a source")
    common(measured)
    measured.add_argument("--fixtures", type=Path, required=True)
    measured.add_argument("--label", required=True)
    measured.add_argument("--machines", nargs="+", default=["computer", "server"], choices=["computer", "server"])
    measured.add_argument("--cases", nargs="+")
    measured.add_argument("--rounds", type=int, default=5)
    measured.add_argument("--warm", type=int, default=3)
    measured.add_argument("--requests", type=int, default=20)
    measured.add_argument("--values", type=int, default=1000, help="values of the API key's vault in its store")
    measured.add_argument("--person-values", type=int, default=36_000, help="a person's vault (above WHOLE_VALUES)")
    measured.add_argument("--workers", type=int, default=1, choices=[1, 2])
    measured.add_argument("--ocr-models", type=Path, help="text recognition models: images read, not withheld")
    measured.add_argument(
        "--forward-services",
        nargs=2,
        metavar=("PG_HOST:PORT", "VALKEY_HOST:PORT"),
        help="inside a container: the test services' loopback ports forwarded to these addresses",
    )
    compared = commands.add_parser("compare", help="judge RC against the Task 8 head")
    compared.add_argument("--base", type=Path, required=True)
    compared.add_argument("--rc", type=Path, required=True)
    compared.add_argument("--out", type=Path)
    final = commands.add_parser("finalizer", help="the end-of-answer wait behind a masking request")
    common(final)
    final.add_argument("--rounds", type=int, default=20)
    written = commands.add_parser("written", help="the event loop's longest turn while a large written answer is read")
    common(written)
    written.add_argument("--rounds", type=int, default=5)
    ledger = commands.add_parser("ledger", help="the ledger a repeated key costs")
    common(ledger)
    micro = commands.add_parser("micro", help="RC-REC-3's settings read, RC-REC-2's Valkey read and commit")
    common(micro)
    micro.add_argument("--calls", type=int, default=500)
    dry = commands.add_parser("dry-run", help="every command with tiny counts, RC alone")
    dry.add_argument("--out", type=Path, required=True)
    dry.add_argument("--work", type=Path, default=None)
    inner = commands.add_parser("_child")
    inner.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "fixtures":
        doc = fixtures(args.seed)
        args.out.write_text(json.dumps(doc, sort_keys=True), "utf-8")
        print(f"{args.out}: {len(doc['cases'])} cases, sha256 {doc['sha256']}")
        return 0
    if args.command == "measure":
        if args.ocr_models is not None:
            raise SystemExit("--ocr-models: the image budget measurement needs the models; not wired in this run")
        if args.forward_services:
            forward_services(args.forward_services)
        return measure(args)
    if args.command == "compare":
        return compare(args)
    if args.command in ("finalizer", "written", "ledger", "micro"):
        return side_command(args, args.command)
    if args.command == "dry-run":
        return dry_run(args)
    return child(args.config)


if __name__ == "__main__":
    sys.exit(main())
