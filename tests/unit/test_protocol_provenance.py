"""Finite EPC witnesses for the unwired record-only queue prototype; no production wire-up before guard RED."""

import asyncio
import gc
import json
import threading
import weakref
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from dataclasses import fields

import pytest

from latet.engine.types import SlotKind
from latet.gateway import provenance
from latet.gateway.mediate import SSETransform, WSResponseRewriter, _StreamWork
from latet.gateway.provenance import _AcceptedProvenance, _Records
from latet.protocols.sse import ResponseError

from .test_protocol_buffering import FAKE, ORIGINAL, _context, _delta, _events, _finish, _start, _wire
from .test_what_leaves import _mediator


@contextmanager
def _occupied(monkeypatch):
    """A real existing engine is occupied until the test releases it; no sleeps or replacement executor."""
    mediator = _mediator()
    entered, release = threading.Event(), threading.Event()
    submitted = []
    original_submit = mediator.executor.submit

    def blocked():
        entered.set()
        assert release.wait(5), "the finite witness did not release its existing engine"

    gate = original_submit(blocked)
    assert entered.wait(5)

    def observed(fn, *args, **kwargs):
        if fn is provenance._record:
            submitted.append((fn, args))
        return original_submit(fn, *args, **kwargs)

    monkeypatch.setattr(mediator.executor, "submit", observed)
    owner = _AcceptedProvenance(mediator.executor)
    try:
        yield mediator, owner, submitted, release
    finally:
        release.set()
        gate.result(timeout=5)
        original_submit(lambda: None).result(timeout=5)
        owner.close()
        mediator.close()


def _staged(label, *, text=False, remember=lambda m, r: None):
    """Use actual SSE rewriting/staging, then transfer only its records at the prototype's admission boundary."""
    transform = SSETransform("chat", _context(remember), None, lambda rid: None)
    if text:
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": FAKE + label}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    else:
        raw = json.dumps({"value": FAKE + label})
        chunks = [_start("chat"), _delta("chat", raw), _finish("chat", raw)]
    batch = None
    for chunk in chunks:
        for read in transform._read(_wire("chat", chunk)):
            assert not isinstance(read, bytes), "the finite legitimate staging fixture was refused"
            batch = transform._events(read)
    assert batch is not None and batch.remembered
    work = transform._work
    transform._accept(batch, work)
    # This boundary transfers JSON ownership; a future accepted task must never release this live budget later.
    work.stream.budget.release(batch.json_charge)
    records = _Records(batch.remembered, batch.named, work.remember, lambda rid: None)
    return transform, records, batch.data


def _emit(owner, records, output, sent):
    error = None
    future = None
    try:
        future = owner.submit(records)
    except ResponseError as exc:
        error = str(exc)
    if error is not None:
        assert FAKE not in error and ORIGINAL not in error
        return None
    sent.append(output)
    return future


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
@pytest.mark.parametrize("over", [0, 1], ids=["exact-byte-cap", "one-byte-over"])
def test_epc_admission_bytes_precede_submit_and_output_across_two_transforms(monkeypatch, text, over):
    with _occupied(monkeypatch) as (_, owner, submitted, _):
        first, a, output_a = _staged("-A", text=text)
        second, b, output_b = _staged("-B", text=text)
        assert first is not second
        monkeypatch.setattr(provenance, "MAX_ACCEPTED_BYTES", a.byte_size + b.byte_size - over)
        sent = []
        assert _emit(owner, a, output_a, sent) is not None
        future = _emit(owner, b, output_b, sent)
        if over:
            assert future is None and sent == [output_a], "one excess byte was submitted and emitted"
            assert len(submitted) == 1 and not b.remembered and not b.named
        else:
            assert future is not None and sent == [output_a, output_b] and len(submitted) == 2


def test_epc_batch_limit_is_independent_of_bytes_and_other_mediators(monkeypatch):
    monkeypatch.setattr(provenance, "MAX_ACCEPTED_BATCHES", 2)
    monkeypatch.setattr(provenance, "MAX_ACCEPTED_BYTES", 4096)
    with _occupied(monkeypatch) as (_, owner, submitted, _):
        sent = []
        for index in range(2):
            assert _emit(owner, _Records([], [str(index)], lambda m, r: None, lambda rid: None), b"ok", sent)
        assert len(submitted) == 2
        other = _mediator()
        seen = []
        try:
            independent = _AcceptedProvenance(other.executor)
            future = independent.submit(_Records([], ["independent"], lambda m, r: None, seen.append))
            assert future is not None
            future.result(timeout=5)
            assert seen == ["independent"]
            independent.close()
        finally:
            other.close()
        rejected = _Records([], ["excess"], lambda m, r: None, lambda rid: None)
        assert _emit(owner, rejected, b"bad", sent) is None, "a third tiny batch bypassed the independent batch cap"
        assert len(submitted) == 2 and sent == [b"ok", b"ok"] and not rejected.named


@pytest.mark.parametrize("limit", ["bytes", "batches"])
def test_epc_running_records_occupy_capacity_until_the_worker_finishes(monkeypatch, limit):
    mediator = _mediator()
    entered, release = threading.Event(), threading.Event()
    owner = _AcceptedProvenance(mediator.executor)

    def remember(masked, restored):
        entered.set()
        assert release.wait(5)

    a = _Records([("masked", "restored")], [], remember, lambda rid: None)
    b = _Records([("next", "next-restored")], [], lambda m, r: None, lambda rid: None)
    monkeypatch.setattr(provenance, "MAX_ACCEPTED_BYTES", a.byte_size + b.byte_size - 1 if limit == "bytes" else 4096)
    monkeypatch.setattr(provenance, "MAX_ACCEPTED_BATCHES", 256 if limit == "bytes" else 1)
    try:
        future = owner.submit(a)
        assert future is not None and entered.wait(5)
        sent = []
        assert _emit(owner, b, b"bad", sent) is None and sent == [], "running work was excluded from admission"
    finally:
        release.set()
        mediator.executor.submit(lambda: None).result(timeout=5)
        owner.close()
        mediator.close()


@pytest.mark.parametrize("kind", ["pair", "name"])
def test_epc_byte_admission_counts_utf8_before_output_without_encoded_copies(monkeypatch, kind):
    with _occupied(monkeypatch) as (_, owner, submitted, _):
        if kind == "pair":
            records = _Records(
                [("\N{GREEK SMALL LETTER ALPHA}", "\N{GREEK SMALL LETTER BETA}")],
                [],
                lambda m, r: None,
                lambda rid: None,
            )
            cap = provenance._BATCH_BYTES + provenance._PAIR_BYTES + 3  # the two source strings need four UTF-8 bytes
        else:
            records = _Records([], ["\N{GREEK CAPITAL LETTER OMEGA}"], lambda m, r: None, lambda rid: None)
            cap = provenance._BATCH_BYTES + provenance._NAME_BYTES + 1  # this response ID needs two UTF-8 bytes
        monkeypatch.setattr(provenance, "MAX_ACCEPTED_BYTES", cap)
        sent = []
        assert _emit(owner, records, b"bad", sent) is None and sent == [] and submitted == []


def test_epc_jobs_detach_records_without_output_or_live_stream_and_cannot_clear_next_batch(monkeypatch):
    with _occupied(monkeypatch) as (mediator, owner, submitted, release):
        recorded = []
        a, records, output = _staged("-A", remember=lambda m, r: recorded.append((m, r)))
        weak = weakref.ref(a)
        weak_work, weak_stream = weakref.ref(a._work), weakref.ref(a.rewriter)
        sent = []
        future = _emit(owner, records, output, sent)
        assert future is not None and sent == [output]
        assert len(submitted) == 1 and submitted[0][0] is provenance._record
        assert submitted[0][1] == (records,)
        assert not any(isinstance(getattr(records, field.name), bytes) for field in fields(records))
        a.close()
        del a
        gc.collect()
        assert weak() is None and weak_work() is None and weak_stream() is None, (
            "an accepted job retained the transform, its live work or its stream"
        )
        b, pending_b, _ = _staged("-B")
        b.ctx.restored_ids[:] = ["next-batch-id"]
        b.rewriter.budget.keep(17)
        release.set()
        future.result(timeout=5)
        mediator.executor.submit(lambda: None).result(timeout=5)
        assert recorded and b.ctx.restored_ids == ["next-batch-id"] and b.rewriter.budget.held == 17
        assert pending_b.remembered and not records.remembered and not records.named
        assert owner.held_bytes == 0 and owner.pending_batches == 0 and not owner.pending


def test_epc_enqueue_happens_before_output_and_the_same_executor_next_request(monkeypatch):
    with _occupied(monkeypatch) as (mediator, owner, submitted, release):
        order = []
        _, records, output = _staged("-A", remember=lambda m, r: order.append("record-attempt"))
        sent = []
        future = _emit(owner, records, output, sent)
        assert future is not None and sent == [output] and len(submitted) == 1
        following = mediator.executor.submit(lambda: order.append("next-request"))
        assert order == [] and not following.done()
        release.set()
        following.result(timeout=5)
        assert order and order[-1] == "next-request" and "record-attempt" in order[:-1]


@pytest.mark.parametrize("transport", ["sse", "ws"])
async def test_epc_live_mediation_submits_only_detached_records_before_returning_output(monkeypatch, transport):
    """The actual async callers must submit record-only work, rather than a closure over transform/batch output."""
    mediator = _mediator()
    submitted = []
    original_submit = mediator.executor.submit

    def observed(fn, *args, **kwargs):
        submitted.append((fn, args))
        return original_submit(fn, *args, **kwargs)

    monkeypatch.setattr(mediator.executor, "submit", observed)
    try:
        if transport == "sse":
            transform = SSETransform("chat", _context(), None, lambda rid: None, mediator=mediator)
            wire = b""
            for event in [
                {"choices": [{"index": 0, "delta": {"content": FAKE}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]:
                wire += b"".join([out async for out in transform.restored(_wire("chat", event))])
                if event["choices"][0].get("finish_reason"):
                    assert submitted, "completion output preceded synchronous record submission"
            wire += await transform.restored_end()
            assert ORIGINAL in wire.decode()
        else:
            session = mediator.engine.session("epc-live-ws")
            fake = mediator.engine.mask_text(ORIGINAL, SlotKind.USER_TEXT, session).text
            rewriter = WSResponseRewriter(mediator, session)
            where = {"item_id": "text-1", "content_index": 0}
            first = await rewriter.restored(json.dumps({"type": "response.output_text.delta", **where, "delta": fake}))
            last = await rewriter.restored(json.dumps({"type": "response.output_text.done", **where}))
            assert ORIGINAL in "".join(first + last)
            rewriter.close()
        assert submitted and all(
            fn is provenance._record and len(args) == 1 and isinstance(args[0], _Records) for fn, args in submitted
        ), "live mediation queued a transform/output closure or returned without synchronously submitting its records"
        for _, args in submitted:
            records = args[0]
            for callback in (records.remember, records.name):
                assert not isinstance(
                    getattr(callback, "__self__", None), (SSETransform, WSResponseRewriter, _StreamWork)
                )
                cells = getattr(callback, "__closure__", None) or ()
                assert not any(
                    isinstance(cell.cell_contents, (SSETransform, WSResponseRewriter, _StreamWork, bytes))
                    for cell in cells
                ), "a recording callback retained live stream work or serialized output"
    finally:
        original_submit(lambda: None).result(timeout=5)
        mediator.close()


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_epc_after_acceptance_cancellation_keeps_exact_payload_ownership(monkeypatch, running):
    with _occupied(monkeypatch) as (mediator, owner, _, release_engine):
        entered, finish = threading.Event(), threading.Event()

        def remember(masked, restored):
            entered.set()
            assert finish.wait(5)

        _, records, output = _staged("-A", remember=remember)
        sent = []
        future = _emit(owner, records, output, sent)
        assert future is not None and sent == [output]
        try:
            if running:
                release_engine.set()
                assert entered.wait(5)
                assert not owner.cancel(future) and records.remembered
                monkeypatch.setattr(provenance, "MAX_ACCEPTED_BYTES", records.byte_size)
                sent_later = []
                later = _Records([], ["later"], lambda m, r: None, lambda rid: None)
                assert _emit(owner, later, b"bad", sent_later) is None and sent_later == [], (
                    "losing cancellation released capacity still owned by the running recorder"
                )
            else:
                assert owner.cancel(future) and future.cancelled()
                assert not records.remembered, "confirmed pre-start cancellation retained its work-item payload"
            finish.set()
            release_engine.set()
            mediator.executor.submit(lambda: None).result(timeout=5)
            assert not records.remembered and not records.named
            assert owner.pending_batches == 0 and owner.held_bytes == 0 and not owner.pending
        finally:
            finish.set()


class _ObservedFuture(Future):
    def __init__(self):
        super().__init__()
        self.exception_reads = 0

    def exception(self, timeout=None):
        self.exception_reads += 1
        return super().exception(timeout)


class _ManualExecutor(Executor):
    """Controlled Future callback boundary, including after loop teardown; no thread or replacement pool."""

    def __init__(self):
        self.jobs = []
        self.reject = False

    def submit(self, fn, /, *args, **kwargs):
        if self.reject:
            raise RuntimeError("submission-private-sentinel")
        future = _ObservedFuture()
        self.jobs.append((future, fn, args, kwargs))
        return future

    def run(self):
        future, fn, args, kwargs = self.jobs.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
        return future


def test_epc_repeated_store_errors_are_observed_with_one_fixed_diagnostic(caplog):
    executor = _ManualExecutor()
    owner = _AcceptedProvenance(executor)

    def fail(masked, restored):
        raise RuntimeError("never-log-this-record-sentinel")

    records = [_Records([("masked", "restored")], [], fail, lambda rid: None) for _ in range(2)]
    for record in records:
        owner.submit(record)
    futures = [executor.run(), executor.run()]
    assert all(future.exception_reads for future in futures), "record failures were left unobserved"
    assert all(not record.remembered for record in records) and not owner.pending
    assert len(caplog.records) == 1 and "RuntimeError" in caplog.records[0].getMessage()
    assert "sentinel" not in caplog.text and "Traceback" not in caplog.text


@pytest.mark.parametrize("outcome", ["success", "store-error", "submit-error", "closed", "loop-closed"])
def test_epc_terminal_cleanup_submission_failure_and_fixed_exception_observation(caplog, outcome):
    executor = _ManualExecutor()
    loop = None
    if outcome == "loop-closed":
        loop = asyncio.new_event_loop()

        async def create_owner():
            return _AcceptedProvenance(executor)

        owner = loop.run_until_complete(create_owner())
    else:
        owner = _AcceptedProvenance(executor)

    def remember(masked, restored):
        if outcome == "store-error":
            raise RuntimeError("record-private-sentinel")

    records = _Records([("masked-private", "restored-private")], ["response-private"], remember, lambda rid: None)
    if outcome == "submit-error":
        executor.reject = True
    if outcome == "closed":
        owner.close()
    if outcome in ("submit-error", "closed"):
        refused = False
        refusal = ""
        try:
            owner.submit(records)
        except ResponseError as exc:
            refused = True
            refusal = str(exc)
        except RuntimeError:
            pass  # the final assertion distinguishes this actual unconverted submit failure from safe refusal
        assert refused and not executor.jobs, "closed/failed submission was not a fixed pre-output refusal"
        assert "private" not in refusal
    else:
        future = owner.submit(records)
        assert future is not None
        if loop is not None:
            loop.close()
        observed = executor.run()
        assert observed.exception_reads > 0, "accepted completion's exception/cancellation was not observed"
    assert not records.remembered and not records.named
    assert owner.held_bytes == 0 and owner.pending_batches == 0 and not owner.pending
    assert all(
        value not in caplog.text
        for value in ["private-sentinel", "masked-private", "restored-private", "response-private"]
    )


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_epc_mediator_shutdown_discards_only_confirmed_queued_records(monkeypatch, running):
    with _occupied(monkeypatch) as (mediator, owner, _, release_engine):
        entered, finish = threading.Event(), threading.Event()

        def remember(masked, restored):
            entered.set()
            assert finish.wait(5)

        records = _Records([("masked", "restored")], [], remember, lambda rid: None)
        future = owner.submit(records)
        assert future is not None
        try:
            if running:
                release_engine.set()
                assert entered.wait(5)
            owner.close()
            if running:
                assert records.remembered and not future.cancelled(), "shutdown cleared a running worker's payload"
            else:
                assert future.cancelled() and not records.remembered
            finish.set()
            release_engine.set()
            mediator.executor.submit(lambda: None).result(timeout=5)
            assert not records.remembered and owner.held_bytes == 0 and owner.pending_batches == 0
            later = _Records([], ["late"], lambda m, r: None, lambda rid: None)
            with pytest.raises(ResponseError):
                owner.submit(later)
        finally:
            finish.set()


def test_epc_close_during_completion_registration_clears_once_without_waiting(monkeypatch):
    """Close can cancel a queued Future before its completion observer is registered; late registration is harmless."""
    with _occupied(monkeypatch) as (_, owner, _, _):
        entered, registered = threading.Event(), threading.Event()
        captured, errors = [], []
        original_add = Future.add_done_callback

        def paused(future, callback):
            if getattr(callback, "__self__", None) is owner:
                captured.append(future)
                entered.set()
                assert registered.wait(5), "the finite registration witness was not released"
            return original_add(future, callback)

        monkeypatch.setattr(Future, "add_done_callback", paused)
        records = _Records([("masked", "restored")], [], lambda m, r: None, lambda rid: None)

        def submit():
            try:
                owner.submit(records)
            except BaseException as exc:
                errors.append(type(exc).__name__)

        control = threading.Thread(target=submit, name="epc-registration-control")
        control.start()
        try:
            assert entered.wait(5)
            owner.close()
            assert len(captured) == 1 and captured[0].cancelled()
            assert not records.remembered and not owner.pending and owner.held_bytes == 0 and owner.pending_batches == 0
            registered.set()
            control.join(timeout=5)
            assert not control.is_alive() and errors == []
            assert not owner.pending and owner.held_bytes == 0 and owner.pending_batches == 0
            with pytest.raises(ResponseError):
                owner.submit(_Records([], ["late"], lambda m, r: None, lambda rid: None))
        finally:
            registered.set()
            control.join(timeout=5)
            assert not control.is_alive()


def test_epc_mixed_text_and_json_cannot_take_the_text_only_accounting_exemption(monkeypatch):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 2048)
    transform = SSETransform("chat", _context(), None, lambda rid: None)
    event = _delta("chat", json.dumps({"value": FAKE}))
    event["choices"][0]["delta"]["content"] = "x" * 1500
    wire = transform.feed(_wire("chat", event)) + transform.end()
    events = _events(wire)
    assert sum("error" in item for item in events) == 1
    assert not any(choice.get("delta", {}).get("content") for item in events for choice in item.get("choices", []))
    assert not transform.rewriter.channels and transform.rewriter.budget.held == 0


def test_epc_text_provenance_is_bounded_before_new_staging_not_only_queue_admission(monkeypatch):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 1024)
    remembered = []
    transform = SSETransform(
        "chat", _context(lambda m, r: remembered.append((m, r)), original="x" * 1200), None, lambda rid: None
    )
    prefix = transform.feed(_wire("chat", {"choices": [{"index": 0, "delta": {"content": FAKE}}]}))
    # Ordinary text keeps its existing incremental behavior; the earlier text cannot be withdrawn.
    assert "x" * 1200 in prefix.decode()
    tail = transform.feed(_wire("chat", {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    assert sum("error" in item for item in _events(tail)) == 1 and transform.stopped is not None
    assert not remembered and not transform._work.pending and not transform.rewriter.channels


def test_epc_delayed_record_is_an_eventual_miss_on_the_separate_bridge_executor(monkeypatch):
    with _occupied(monkeypatch) as (mediator, owner, _, release):
        engine = mediator.engine
        session = engine.session("epc-eventual")
        fake = engine.mask_text(ORIGINAL, SlotKind.USER_TEXT, session).text
        masked = f"model wrote to {fake}"
        restored = f"model wrote to {ORIGINAL}"
        key = session.key
        records = _Records(
            [(masked, restored)],
            ["epc-response"],
            engine.remember_model_output,
            lambda rid: engine.conversations.name(rid, key),
        )
        future = owner.submit(records)
        assert future is not None

        def miss():
            assert engine.conversations.recall(engine._key("R", restored)) is None
            assert engine.conversations.named("epc-response") is None
            return engine.mask_text(restored, SlotKind.ASSISTANT_TEXT, session, from_model=True)

        before = mediator.bridge_executor.submit(miss).result(timeout=5)
        assert ORIGINAL not in before.text and before.replacements, (
            "a delayed provenance miss bypassed ordinary masking"
        )
        release.set()
        future.result(timeout=5)
        after = mediator.bridge_executor.submit(
            lambda: engine.mask_text(restored, SlotKind.ASSISTANT_TEXT, session, from_model=True)
        ).result(timeout=5)
        assert after.text == masked and not after.replacements
        assert engine.conversations.named("epc-response") == key
