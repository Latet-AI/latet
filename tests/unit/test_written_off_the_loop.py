"""What the model wrote in an answer Latet passes on as it came (Claude Code's, a request it only checks) is read
without holding the event loop. An answer as large as Latet reads of it (``WRITTEN_BYTES``) takes a whole body, one
event of a stream or a tool call's input joined from its pieces to parse, and every string of it to record: past a
little (``RESTORED_ON_LOOP``) that is done on the engine's thread (on a server, the identity's own), as a restore is,
and what is small is read on the event loop in the identity's turn (``Mediator.restoring``), one step of one answer at
a time. Each text is still known in its conversation before the bytes that end it are passed on."""

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.gateway.written import WRITTEN_BYTES, Written

from ..conftest import ROOT_KEY

SESSION = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
CLAUDE_CODE = {
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": SESSION,
}
ASKED = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "write the fixtures"}],
}
ANTHROPIC = Upstream(
    "anthropic",
    "/anthropic",
    "http://127.0.0.1:1",
    envelope_expected=frozenset({("x-claude-code-session-id", SESSION)}),
)


def _items(size: int, each: int = 12) -> list[str]:
    """Short strings, all different, about ``size`` bytes of JSON in all (``each`` bytes each, written so): the most a
    parse and a record cost."""
    return [f"v{i:07d}" for i in range(size // each)]


def _event(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def _tool(index: int, given: dict) -> dict:
    return {"type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Write", "input": given}}  # fmt: skip


def _answers(shape: str, items: list[str]) -> tuple[str, bytes]:
    """An answer of about ``WRITTEN_BYTES`` whose one tool call's input holds ``items``: a whole body, a stream whose one
    event gives the input whole, or a stream whose input comes in pieces of a few kilobytes."""
    given = {"items": items}
    if shape == "whole":
        body = {"id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": given}]}  # fmt: skip
        return "application/json", json.dumps(body).encode()
    if shape == "event":
        return "text/event-stream", _event(_tool(0, given)) + _event({"type": "content_block_stop", "index": 0})
    raw = json.dumps(given)
    pieces = [raw[at : at + 4096] for at in range(0, len(raw), 4096)]
    deltas = [{"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}}
              for piece in pieces]  # fmt: skip
    events = [_tool(0, {}), *deltas, {"type": "content_block_stop", "index": 0}]
    return "text/event-stream", b"".join(_event(event) for event in events)


class _Provider:
    def __init__(self) -> None:
        self.then: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert self.then is not None
        return self.then


@contextlib.asynccontextmanager
async def _gateway(provider: _Provider) -> AsyncIterator[tuple[httpx.AsyncClient, Mediator]]:
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    config = GatewayConfig(
        port=5283,
        upstreams=[ANTHROPIC],
        mediator=mediator,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283") as client,
    ):
        yield client, mediator


@pytest.mark.parametrize("shape", ["whole", "event", "pieces"])
async def test_what_the_model_wrote_in_an_answer_as_large_as_is_read_is_read_on_the_engines_thread(shape, monkeypatch):
    """An answer as large as Latet reads of it: a whole body, one event giving a tool call's input whole, or that input
    in pieces joined at its end. Each step past a little (``RESTORED_ON_LOOP``) is parsed and recorded by
    ``Mediator._write_down`` on the engine's thread, never on the event loop's, and what it wrote is known in its
    conversation. Where the work runs is what this pins (minors after 0.11, item 17): how long the loop then waits for
    Python's lock is the machine's, which a loaded one stretched past any bound (0.112 s against 0.104 s in the merge's
    run); `scripts/request_coverage_bench.py written` measures it."""
    items = _items(WRITTEN_BYTES - 2**20, 15 if shape == "pieces" else 12)  # in pieces, each quote escaped again
    content_type, body = _answers(shape, items)
    assert len(body) < WRITTEN_BYTES
    provider = _Provider()
    provider.then = httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body))
    written_down = Mediator._write_down
    threads: list[str] = []

    def where(self: Mediator, *args: Any) -> Any:
        threads.append(threading.current_thread().name)
        return written_down(self, *args)

    monkeypatch.setattr(Mediator, "_write_down", where)
    loop = threading.current_thread().name
    async with _gateway(provider) as (client, mediator):
        answered = await client.post("/anthropic/v1/messages", json=ASKED, headers=CLAUDE_CODE)
        assert answered.status_code == 200 and answered.content == body  # passed on as it came
        # Each recorded (the newest are kept in this process's memory: ``WRITTEN_HERE``).
        assert all(mediator.verifier.written(f"cc:{SESSION}", item) for item in (items[-40_000], items[-1]))
    assert threads, "no step was large enough to be read on the engine's thread"
    assert all(name.startswith("latet-engine") and name != loop for name in threads), threads


async def test_while_the_engines_thread_reads_a_large_step_another_client_is_answered(monkeypatch):
    """The plan's other half of item 17 (the fix round's Q-M2): the large step's wait for the engine's thread holds no
    one else up. With that thread held inside ``_write_down``, another client's call the event loop answers alone
    (a path with no route) is answered meanwhile: a wait that blocked the loop (``future.result()`` in place of
    ``await``) would answer it only once the thread let go. Structural: the order of the two, never a time. What
    decides is how the held thread let go, which it says itself: released once the other client was answered, or at
    the end of its hold (the re-review's nit: the wait for it to be entered never races the hold)."""
    items = _items(WRITTEN_BYTES - 2**20)
    content_type, body = _answers("whole", items)
    provider = _Provider()
    provider.then = httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body))
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    let_go: list[str] = []  # how the engine's thread left its hold, in its own words
    written_down = Mediator._write_down

    def held(self: Mediator, *args: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        let_go.append("released" if release.wait(5) else "at the end of its hold")
        return written_down(self, *args)

    monkeypatch.setattr(Mediator, "_write_down", held)
    async with _gateway(provider) as (client, _):
        answering = asyncio.create_task(client.post("/anthropic/v1/messages", json=ASKED, headers=CLAUDE_CODE))
        try:
            await asyncio.wait_for(entered.wait(), 30)  # longer than the hold: a blocked loop gets here after it
            other = await client.get("/nowhere/at/all")  # another client: answered by the loop alone
        finally:
            release.set()
        answered = await asyncio.wait_for(answering, 30)
    assert other.status_code == 404
    assert let_go[:1] == ["released"], f"the other client waited for the engine's thread: it let go {let_go}"
    assert answered.status_code == 200 and answered.content == body


async def test_a_small_answer_is_read_on_the_event_loop_in_its_identitys_turn():
    """A small step is read where it came, never behind the engine's thread; and in the identity's turn, as a restore
    is: while another answer of the identity holds the turn, it waits, and then it is read."""
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    session = mediator.engine.session(f"cc:{SESSION}")
    content_type, body = _answers("event", ["v1", "v2"])
    capture = Written(content_type, None)
    try:
        async with mediator.restoring:  # another answer of hers, being read
            step = asyncio.create_task(mediator.read_written(capture, session, body))
            await asyncio.sleep(0.1)
            assert not step.done() and not mediator.verifier.written(f"cc:{SESSION}", "v1")
        await asyncio.wait_for(step, 5)
        assert mediator.verifier.written(f"cc:{SESSION}", "v1") and mediator.verifier.written(f"cc:{SESSION}", "v2")
    finally:
        mediator.close()


async def test_a_small_answer_is_not_read_behind_the_engines_thread():
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    session = mediator.engine.session(f"cc:{SESSION}")
    gate = threading.Event()
    mediator.executor.submit(gate.wait, 30)  # masking another request, say
    try:
        for shape in ("whole", "event", "pieces"):
            content_type, body = _answers(shape, [f"{shape}-1", f"{shape}-2"])
            capture = Written(content_type, None)
            await asyncio.wait_for(mediator.read_written(capture, session, body), 5)
            await asyncio.wait_for(mediator.read_written(capture, session, None), 5)
            assert mediator.verifier.written(f"cc:{SESSION}", f"{shape}-2")
    finally:
        gate.set()
        mediator.close()
