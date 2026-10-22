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
import time
from collections.abc import AsyncIterator

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
from .test_restore_off_the_loop import HELD, _ticking

SESSION = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
CLAUDE_CODE = {
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": SESSION,
}
ASKED = {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "write the fixtures"}]}
ANTHROPIC = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
# The longest a turn of the event loop may take while the gateway reads what the model wrote in an answer as large as it
# reads of one, in seconds, or twice what Python's own parse of the input alone (``json.loads``, 30 to 45 ms here) takes
# on a slower machine: with every step read on the loop, its parse and its strings held it 0.15 to 0.23 s, four to six
# times that parse; on the engine's thread the parse holds Python's lock all the same, and the loop waited 0.046 to
# 0.077 s.
HELD_HERE = 0.1


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
async def test_what_the_model_wrote_in_an_answer_as_large_as_is_read_is_read_while_the_event_loop_runs(shape):
    items = _items(WRITTEN_BYTES - 2**20, 15 if shape == "pieces" else 12)  # in pieces, each quote escaped again
    content_type, body = _answers(shape, items)
    assert len(body) < WRITTEN_BYTES
    raw = json.dumps({"items": items})
    started = time.perf_counter()
    json.loads(raw)
    loads = time.perf_counter() - started  # what Python's own parse of the input takes on this machine, alone
    provider = _Provider()
    provider.then = httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body))
    async with _gateway(provider) as (client, mediator):
        async with _ticking() as gaps:
            started = time.perf_counter()
            answered = await client.post("/anthropic/v1/messages", json=ASKED, headers=CLAUDE_CODE)
            took = time.perf_counter() - started
        assert answered.status_code == 200 and answered.content == body  # passed on as it came
        # Each recorded (the newest are kept in this process's memory: ``WRITTEN_HERE``).
        assert all(mediator.verifier.written(f"cc:{SESSION}", item) for item in (items[-40_000], items[-1]))
    held = min(HELD, max(HELD_HERE, 2 * loads), took / 4)
    assert max(gaps) < held, f"the loop was held {max(gaps):.3f} s of {took:.3f} s (parse {loads:.3f} s)"


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
