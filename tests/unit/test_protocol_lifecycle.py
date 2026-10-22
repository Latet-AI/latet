"""Observable aggregate-cap boundaries, completed-payload retention and real gateway stream cancellation."""

import asyncio
import gc
import json
import threading
import tracemalloc
from contextlib import suppress

import httpx
import pytest

from latet.engine.rehydrate import Restore
from latet.gateway import mediate
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import SSETransform, WSResponseRewriter
from latet.gateway.routes import Upstream
from latet.protocols.rewrite import RestoreContext
from latet.protocols.sse import ResponseError

from .test_protocol_buffering import (
    FAKE,
    ORIGINAL,
    PROTOCOLS,
    _arguments,
    _context,
    _delta,
    _events,
    _finish,
    _start,
    _wire,
)
from .test_what_leaves import _mediator


class _ObservedLimit(int):
    """Observe the byte amounts actually admitted against the existing injected ceiling, without duplicating them."""

    observed: list[int]

    def __new__(cls, value: int, observed: list[int]):
        return super().__new__(cls, value)

    def __init__(self, value: int, observed: list[int]):
        self.observed = observed

    def __lt__(self, other: int):
        self.observed.append(other)
        return super().__lt__(other)

    def __le__(self, other: int):
        self.observed.append(other)
        return super().__le__(other)

    def __gt__(self, other: int):
        self.observed.append(other)
        return super().__gt__(other)

    def __ge__(self, other: int):
        self.observed.append(other)
        return super().__ge__(other)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_aggregate_peak_is_accepted_at_its_byte_cap_and_refused_one_byte_below(monkeypatch, protocol):
    """Observe actual admission charges, then replay exactly at that peak and one byte beyond the allowed cap.

    One channel's measured working peak fits below the low cap; four held documents collectively do not. This also
    detects replacing a response-wide bound with independent channel allowances. The internal arithmetic seam is
    comparison against the existing MAX_DECODED ceiling; it observes charges without supplying their formula or
    accepting a self-calibrated off-by-one boundary. The last two trials use ordinary integer limits.
    """
    raw = json.dumps({"pad": "a" * 192})
    frames = [_wire(protocol, _start(protocol, index)) for index in range(4)]
    frames += [_wire(protocol, _delta(protocol, raw, index)) for index in range(4)]
    frames += [_wire(protocol, _finish(protocol, raw, index)) for index in range(4)]
    high = 32 * 1024

    def relayed(cap, selected=frames):
        monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", cap)
        remembered = []
        transform = SSETransform(protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
        wire = b"".join(transform.feed(frame) for frame in selected) + transform.end()
        return transform, wire, remembered

    def accepted(cap):
        transform, wire, _ = relayed(cap)
        if transform.stopped is not None:
            return False
        assert all(json.loads(_arguments(protocol, wire, index)) == {"pad": "a" * 192} for index in range(4))
        return True

    single_charges = []
    single_frames = [
        _wire(protocol, event) for event in [_start(protocol, 0), _delta(protocol, raw, 0), _finish(protocol, raw, 0)]
    ]
    single, wire, _ = relayed(_ObservedLimit(high, single_charges), single_frames)
    assert single.stopped is None and json.loads(_arguments(protocol, wire, 0)) == {"pad": "a" * 192}
    low = max(single_charges) + 1
    assert not accepted(low), "individually small channels bypassed the aggregate peak bound"
    charges = []
    assert accepted(_ObservedLimit(high, charges)), "a small legitimate aggregate must remain usable"
    peak = max(charges)
    assert low < peak < high, "no aggregate admission charge was observed beyond the individual frames"
    assert accepted(peak), "an aggregate exactly at the byte cap must be admitted"
    refused, wire, remembered = relayed(peak - 1)
    assert refused.stopped is not None and not refused.rewriter.channels
    assert not getattr(refused.rewriter, "held", None)
    errors = [event for event in _events(wire) if "error" in event or event.get("type") == "error"]
    assert len(errors) == 1 and ORIGINAL not in json.dumps(errors)
    assert remembered == []
    assert all(_arguments(protocol, wire, index) == "" for index in range(4))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_completed_arguments_do_not_accumulate_in_a_live_stream(protocol):
    """Keeping every retired channel's argument buffers must fail even if the active-channel map is emptied."""
    raw = json.dumps({"pad": "a" * 8192})
    transform = SSETransform(protocol, _context(), None, lambda rid: None)

    def complete(index):
        wire = b"".join(
            transform.feed(_wire(protocol, event))
            for event in [_start(protocol, index), _delta(protocol, raw, index), _finish(protocol, raw, index)]
        )
        assert json.loads(_arguments(protocol, wire, index)) == {"pad": "a" * 8192}
        assert transform.stopped is None and not transform.rewriter.channels
        assert not getattr(transform.rewriter, "held", None)

    for index in range(8):
        complete(index)
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start(1)
    try:
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        for index in range(8, 104):
            complete(index)
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
        # Allow interpreter/cache noise and several full payloads, while catching 96 retired 8 KiB arguments.
        assert retained < 256 * 1024, f"completed argument retention grew by {retained} bytes"
    finally:
        if not tracing:
            tracemalloc.stop()
    assert transform.end() == b""


class _WaitingProvider(httpx.AsyncByteStream):
    """Two real provider chunks, then a blocked next read until the ASGI request is cancelled."""

    def __init__(self, protocol, masked):
        self.masked = masked
        self.chunks = [
            _wire(protocol, _start(protocol)),
            _wire(protocol, _delta(protocol, json.dumps({"owner": masked}))),
        ]
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed.set()


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_gateway_cancellation_while_waiting_for_provider_discards_held_arguments(monkeypatch, protocol, caplog):
    """The actual app/forwarder relay must release JSON state even between calls to transform.restored()."""
    mediator = _mediator()
    transforms = []
    remembered = []
    streams = []
    available = asyncio.Event()
    original_transform = mediator.response_transform

    def capture(*args, **kwargs):
        transform = original_transform(*args, **kwargs)
        assert isinstance(transform, SSETransform)
        transforms.append(transform)
        ctx = transform.rewriter.ctx
        remember = ctx.remember

        def completed(masked, restored):
            remembered.append((masked, restored))
            remember(masked, restored)

        ctx.remember = completed
        return transform

    monkeypatch.setattr(mediator, "response_transform", capture)

    def model(request):
        body = json.loads(request.content)
        masked = body["input"] if protocol == "responses" else body["messages"][0]["content"]
        stream = _WaitingProvider(protocol, masked)
        streams.append(stream)
        available.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    upstream = Upstream("llm", "/llm", "http://127.0.0.1:1")
    app = create_app(
        GatewayConfig(
            mediator=mediator,
            upstreams=[upstream],
            client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(model), trust_env=False),
        )
    )
    suffix = {"chat": "chat/completions", "anthropic": "messages", "responses": "responses"}[protocol]
    body = {"model": "m", "stream": True}
    body.update(
        {"input": ORIGINAL}
        if protocol == "responses"
        else {"max_tokens": 10, "messages": [{"role": "user", "content": ORIGINAL}]}
    )
    raw = json.dumps(body).encode()
    path = f"/llm/v1/{suffix}"
    authorization = (
        (b"x-api-key", b"sk-ant-api-test") if protocol == "anthropic" else (b"authorization", b"Bearer sk-test")
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 5283),
        "headers": [(b"host", b"127.0.0.1:5283"), (b"content-type", b"application/json"), authorization],
    }
    sent = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope, receive, send))
        try:
            # No sleeps or scheduling race: the source signals only after both chunks were consumed by the relay.
            async with asyncio.timeout(5):
                await available.wait()
                await streams[0].waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert streams[0].closed.is_set()
            assert len(transforms) == 1
            transform = transforms[0]
            assert not transform.rewriter.channels and not getattr(transform.rewriter, "held", None)
            assert remembered == []
            assert ORIGINAL not in caplog.text and streams[0].masked not in caplog.text
            wire = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            assert _arguments(protocol, wire) == ""
            assert not any(
                choice.get("finish_reason") for event in _events(wire) for choice in event.get("choices", [])
            )
            assert transform.feed(_wire(protocol, _finish(protocol, "{}"))) == b"" and transform.end() == b""
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            mediator.close()


@pytest.mark.parametrize("form", ["whole-arguments", "collision-fallback", "collision-serialization"])
def test_expansion_is_admitted_before_allocation_in_whole_and_collision_paths(monkeypatch, form):
    """A post-join-only limit still allocates megabytes for this small refused argument; admission must precede it."""
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 8192)
    expanded = ('"\\\n' * 4096) if form == "collision-serialization" else "x" * 16384
    remembered = []
    ctx = _context(lambda masked, restored: remembered.append((masked, restored)))
    extra = Restore("EXPAND", expanded, "expanded-id", False)
    ctx.text_restores.append(extra)
    raw_doc = {"value": "EXPAND" * 64}
    if form == "whole-arguments":
        ctx.json_restores.append(Restore("EXPAND", json.dumps(expanded)[1:-1], "expanded-id", False))
    else:
        # Deliberately isolate the structural collision fallback: its text-restorer caller must be bounded too.
        raw_doc.update({FAKE: "owner", ORIGINAL: "viewer"})
    raw = json.dumps(raw_doc)
    transform = SSETransform("responses", ctx, None, lambda rid: None)
    item = {"type": "function_call", "id": "call-0", "call_id": "tool-0", "name": "grant", "arguments": raw}
    packet = _wire("responses", {"type": "response.output_item.done", "output_index": 0, "item": item})
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start(1)
    try:
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        wire = transform.feed(packet) + transform.end()
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        if not tracing:
            tracemalloc.stop()
    errors = [event for event in _events(wire) if "error" in event or event.get("type") == "error"]
    assert len(errors) == 1 and transform.stopped is not None and remembered == []
    assert peak < 512 * 1024, f"refused expansion allocated {peak} bytes before admission"


def _final_output(count):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": f"call-{index}",
                    "call_id": f"tool-{index}",
                    "name": "grant",
                    "arguments": '{"value":"EXPAND"}',
                }
                for index in range(count)
            ],
        },
    }


@pytest.fixture(params=["sse", "websocket"])
def final_output_relay(request, monkeypatch):
    """Keep the real restorers and transport serialization; observe completed strings without retaining copies."""
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 32 * 1024)
    remembered = []
    completed_bytes = []
    ctx = _context(lambda masked, restored: remembered.append((masked, restored)))
    expansion = Restore("EXPAND", "x" * 2048, "expanded-id", False)
    ctx.text_restores.append(expansion)
    ctx.json_restores.append(expansion)
    original = RestoreContext.json_text

    def observed(self, value):
        result = original(self, value)
        completed_bytes.append(len(result.encode("utf-8")))
        return result

    monkeypatch.setattr(RestoreContext, "json_text", observed)
    mediator = _mediator(ner="off")
    if request.param == "sse":
        transform = SSETransform("responses", ctx, None, lambda rid: None, mediator=mediator)
        close = transform.close

        async def send_sse(event):
            wire = b"".join([part async for part in transform.restored(_wire("responses", event))])
            return _events(wire), transform.stopped

        def stream_sse():
            return transform.rewriter

        send, stream = send_sse, stream_sse
    else:
        session = mediator.engine.session("final-output-budget")
        session.entity_ids.add("expanded-id")
        monkeypatch.setattr(mediator, "restore_context", lambda session: ctx)
        rewriter = WSResponseRewriter(mediator, session)
        close = rewriter.close

        async def send_ws(event):
            try:
                frames = await rewriter.restored(json.dumps(event))
            except ResponseError as exc:
                return [], str(exc)
            return [json.loads(frame) for frame in frames], None

        def stream_ws():
            assert rewriter.stream is not None
            return rewriter.stream

        send, stream = send_ws, stream_ws
    try:
        yield send, stream, completed_bytes, remembered
    finally:
        close()
        mediator.executor.submit(lambda: None).result(timeout=5)
        mediator.close()


async def test_final_output_refuses_before_cumulative_restored_arguments_exceed_budget(final_output_relay):
    """Twenty individually admitted strings must not all exist before a late serialization-only refusal."""
    send, stream, completed_bytes, remembered = final_output_relay
    events, refused = await send(_final_output(20))
    # Each returned argument is exactly 2060 ASCII bytes. The old path builds all 41,200 bytes before refusing.
    assert sum(completed_bytes) <= 32 * 1024, "final output retained oversized cumulative arguments before refusal"
    assert 0 < len(completed_bytes) < 20, "the witness must reach restoration and stop before the whole list exists"
    assert refused is not None
    assert all(event.get("type") == "error" for event in events)
    assert len(events) <= 1 and remembered == []
    assert "EXPAND" not in str(refused) + json.dumps(events)
    assert "x" * 64 not in str(refused) + json.dumps(events)
    assert stream().budget.held == 0 and not stream().channels
    assert (await send(_final_output(2)))[0] == []


async def test_small_final_output_releases_reconstruction_budget_after_serialization(final_output_relay):
    """A safe multi-item completion can reuse the same budget after every serialized event."""
    send, stream, completed_bytes, remembered = final_output_relay
    for _ in range(3):
        events, refused = await send(_final_output(2))
        assert refused is None and len(events) == 1
        assert events[0]["type"] == "response.completed"
        items = events[0]["response"]["output"]
        assert [item["id"] for item in items] == ["call-0", "call-1"]
        assert [json.loads(item["arguments"]) for item in items] == [{"value": "x" * 2048}] * 2
        assert stream().budget.held == 0 and not stream().channels
    assert completed_bytes == [2060] * 6 and remembered == []


async def test_final_output_keeps_reservation_through_serialization_and_discards_on_error(
    monkeypatch, final_output_relay
):
    """A serialization failure must discard the live reconstruction reservation without publishing provenance."""
    send, stream, completed_bytes, remembered = final_output_relay
    held_at_serialization = []
    original = mediate.dumps_compact

    def refuse_completion(value):
        if isinstance(value, dict) and value.get("type") == "response.completed":
            held_at_serialization.append(stream().budget.held)
            raise ResponseError("synthetic serialization refusal")
        return original(value)

    monkeypatch.setattr(mediate, "dumps_compact", refuse_completion)
    events, refused = await send(_final_output(2))
    assert completed_bytes == [2060, 2060], "serialization must follow actual multi-item restoration"
    assert len(held_at_serialization) == 1 and 0 < held_at_serialization[0] <= 32 * 1024
    assert refused is not None and all(event.get("type") == "error" for event in events)
    assert len(events) <= 1 and remembered == []
    assert stream().budget.held == 0 and not stream().channels
    assert (await send(_final_output(2)))[0] == []


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_cancellation_during_engine_finalization_cannot_repopulate_terminal_state(monkeypatch, protocol):
    """Cancel unfinished real engine work, then let it finish; an additional event must not resurrect its channel."""
    mediator = _mediator()
    remembered = []
    transform = SSETransform(
        protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None, mediator=mediator
    )
    raw = json.dumps({"pad": "x" * (70 * 1024), "owner": FAKE})
    original = RestoreContext.json_text
    entered = asyncio.Event()
    released = threading.Event()
    worker_errors = []
    working_maps = []
    loop = asyncio.get_running_loop()
    original_run = mediator.run

    async def observed_run(fn, *args, **kwargs):
        def checked():
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                worker_errors.append(type(exc).__name__)
                raise

        return await original_run(checked)

    monkeypatch.setattr(mediator, "run", observed_run)

    def unfinished(self, value):
        if len(value) > 64 * 1024:
            working_maps.append(transform.rewriter.channels)
            loop.call_soon_threadsafe(entered.set)
            assert released.wait(5), "owned engine work was not released within its test bound"
        return original(self, value)

    monkeypatch.setattr(RestoreContext, "json_text", unfinished)

    async def consume(packet):
        return b"".join([out async for out in transform.restored(packet)])

    await consume(_wire(protocol, _start(protocol)))
    for start in range(0, len(raw), 1024):
        await consume(_wire(protocol, _delta(protocol, raw[start : start + 1024])))
    await consume(_wire(protocol, _start(protocol, 2)))
    await consume(_wire(protocol, _delta(protocol, '{"held":2}', 2)))
    # A later event in the same batch catches late shared-state resurrection after cancellation.
    packet = b"".join(
        _wire(protocol, event)
        for event in [_finish(protocol, raw), _start(protocol, 1), _delta(protocol, '{"next":1}', 1)]
    )

    async def owned_relay():
        try:
            return await consume(packet)
        finally:
            transform.close()

    task = asyncio.create_task(owned_relay())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert not transform.rewriter.channels and transform.rewriter.budget.held == 0
        assert working_maps and working_maps[0], "abort mutated the paused worker's owned dictionary"
        assert remembered == []
        released.set()
        # The existing single engine worker's next job is an exact completion barrier, not a timing sleep.
        await asyncio.wait_for(mediator.run(lambda: None), timeout=5)
        assert worker_errors == [], f"the worker raised instead of discarding terminal work: {worker_errors}"
        assert not transform.rewriter.channels and not getattr(transform.rewriter, "held", None)
        assert transform.rewriter.budget.held == 0 and remembered == []
        assert transform.feed(_wire(protocol, _finish(protocol, "{}"))) == b"" and transform.end() == b""
    finally:
        released.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(mediator.run(lambda: None), timeout=5)
        mediator.close()
