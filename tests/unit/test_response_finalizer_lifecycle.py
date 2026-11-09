"""Real executor and HTTP witnesses for the response finalizer lifecycle."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

from latet.gateway import response_finalizer as lifecycle
from latet.gateway.capture import Capture, Exchange
from latet.gateway.forward import Forwarder
from latet.gateway.headers import redacted
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture
from latet.gateway.routes import RouteTable

from .test_request_leaks import _Answer, _renewers, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _REQUEST, _RESPONSE, _TEXT, _TIMEOUT, _detail, _sinks, _wire
from .test_response_sinks import _END, _TOKENS, _delta, _payload, _records

# Past RESTORED_ON_LOOP (64 KiB): an answer the engine's thread screens, where a test gates that screening with a thread
# event (a smaller one, uncompressed over HTTP, is screened where it ends, on the event loop).
_LARGE_TEXT = "ordinary response " * 4000
_LARGE = _delta(_LARGE_TEXT) + _END
assert len(_LARGE) > 64 * 1024


class _Gate:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.expired = False

    def block(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        if not self.release.wait(_TIMEOUT):
            self.expired = True
            raise TimeoutError("test worker gate")


class _CallbackHold:
    """The callback wrapper itself holds only the gate and the original callback."""

    def __init__(self, gate: _Gate) -> None:
        self.gate = gate
        self.callback: Any = None

    def __call__(self, future: Future[bool]) -> None:
        self.callback(future)
        self.gate.block()


class _Submissions:
    def __init__(self) -> None:
        self.rows: list[tuple[Future[bool], list[lifecycle._Work], lifecycle._Work]] = []
        self.second = asyncio.Event()
        self.other = 0

    def watch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        executor: ThreadPoolExecutor,
        hold: _CallbackHold | None = None,
    ) -> None:
        submit = executor.submit

        def observe(function, *args, **kwargs):
            if function is not lifecycle._work:
                self.other += 1
                return submit(function, *args, **kwargs)
            [handoff] = args
            [shell] = handoff
            future = submit(function, handoff)
            self.rows.append((future, handoff, shell))
            if hold is not None and len(self.rows) == 1:
                add = future.add_done_callback

                def register(callback):
                    assert hold is not None
                    hold.callback = callback
                    add(hold)

                monkeypatch.setattr(future, "add_done_callback", register)
            if len(self.rows) == 2:
                self.second.set()
            return future

        monkeypatch.setattr(executor, "submit", observe)


async def _cleanup(task: asyncio.Task[Any], *gates: _Gate) -> None:
    for gate in gates:
        gate.release.set()
    with suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(asyncio.gather(task, return_exceptions=True)), _TIMEOUT)


def _new_finalizer(directory: Path, mediator: Mediator, recorder: Recorder) -> lifecycle.ResponseFinalizer:
    exchange = Capture(directory).exchange("http", "openai", "POST", "http://127.0.0.1/v1/chat/completions")
    assert exchange is not None
    capture = ResponseCapture("chat", "text/event-stream")
    capture.feed(_LARGE)  # the engine's thread screens it: its gates hold that thread, never the event loop
    outcome = Outcome({}, "openai", "/v1/chat/completions", "http", time.time(), status=200)
    finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, exchange, 200)
    finalizer.seal(outcome, complete=True)
    return finalizer


def _screen_gate(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    screen = ResponseCapture.screen

    def blocked(capture: ResponseCapture) -> bool:
        gate.block()
        return screen(capture)

    monkeypatch.setattr(ResponseCapture, "screen", blocked)


class _Masking:
    """Another request of the identity masking: the engine's one thread held until ``release`` (or the timeout)."""

    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self.executor = executor
        self.started, self.held = threading.Event(), threading.Event()
        self.future: Future[None] | None = None

    def begin(self) -> None:
        def masking() -> None:
            self.started.set()
            self.held.wait(_TIMEOUT)

        self.future = self.executor.submit(masking)

    def holding(self) -> bool:
        return self.started.is_set() and not self.held.is_set() and self.future is not None and not self.future.done()

    def release(self) -> None:
        self.held.set()
        if self.future is not None:
            self.future.result(_TIMEOUT)


_WHOLE = (
    b'{"id":"chatcmpl-1","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"'
    + _TEXT.encode()
    + b'"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
)


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_a_small_answer_ends_while_another_request_of_the_identity_masks(tmp_path, stream):
    """The plan's case: a small answer's end, its screening and its history, wait behind no masking of the identity's
    (P2-8's measurement: 108 ms, the length of the other request's preparation, against 3 ms alone)."""
    answer = _Answer(_RESPONSE if stream else _WHOLE)
    with _sinks(tmp_path) as (mediator, recorder, store):
        other = _Masking(mediator.executor)
        began: list[bool] = []

        async def provider(_: httpx.Request) -> httpx.Response:
            other.begin()  # this request was prepared: another one now holds the engine's thread
            # Answered once that masking holds the engine's thread, which it does until the answer is read. Only
            # submitted, it could begin after the answer had ended (public CI on 3.14, 2026-11-09: the end measured
            # at 0.003 s, the masking not begun), and the test then measured an end behind no masking at all.
            began.append(await asyncio.to_thread(other.started.wait, _TIMEOUT))
            kind = "text/event-stream" if stream else "application/json"
            return httpx.Response(200, headers={"content-type": kind}, stream=answer)

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                forwarder = Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator, recorder)
                body = _REQUEST if stream else _REQUEST.replace(b'"stream":true', b'"stream":false')
                request = _request(body)
                started = time.monotonic()
                response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
                status, wire = await asyncio.wait_for(_wire(response, request), _TIMEOUT)  # type: ignore[arg-type]
                waited = time.monotonic() - started
                held_throughout = other.holding()
        finally:
            other.release()
        detail = _detail(recorder, store)
    assert began == [True], "the other masking never held the engine's thread"
    assert status == 200 and wire == (_RESPONSE if stream else _WHOLE)
    assert held_throughout, "the other masking let the engine's thread go before the answer was read"
    assert waited < 1.0, f"the answer's end waited {waited:.3f} s behind the masking"
    assert (detail.get("response") or {}).get("text") == _TEXT  # published, screened


async def test_a_small_answer_holding_a_launch_key_is_omitted_where_it_ends(tmp_path):
    value = "latet_" + "lk_" + "Ab3_" * 10 + "XYw"
    with _sinks(tmp_path) as (mediator, recorder, store):
        other = _Masking(mediator.executor)
        other.begin()
        assert other.started.wait(_TIMEOUT)
        capture = ResponseCapture("chat", "text/event-stream")
        capture.feed(_delta("ordinary text, then " + value) + _END)
        outcome = Outcome({}, "openai", "/v1/chat/completions", "http", time.time(), status=200)
        finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, None, 200)
        finalizer.seal(outcome, complete=True)
        try:
            await asyncio.wait_for(finalizer.complete(), 1.0)
            held_throughout = other.holding()
        finally:
            other.release()
        detail = _detail(recorder, store)
    assert held_throughout and finalizer.state == "DONE"
    assert capture.omission == "response-credential" and not capture.safe and not capture.text and not capture._raw
    assert (detail.get("response") or {}).get("omitted") == "response-credential"
    assert value not in json.dumps(detail)


@pytest.mark.parametrize(
    ("case", "submitted"),
    [("small", 0), ("small-raw", 1), ("large", 1), ("large-raw", 2), ("gzip-raw", 2), ("websocket-raw", 2)],
)
async def test_only_a_small_plain_http_answer_is_screened_where_it_ends(tmp_path, monkeypatch, case, submitted):
    """Screened on the event loop: an HTTP answer, not compressed, of RESTORED_ON_LOOP bytes at most. A larger or a
    compressed one, or a WebSocket's, is screened on the engine's thread, and the raw publication, when raw capture is
    on, is always made there (file writes): each in the order of the same state machine, screening first."""
    directory = tmp_path / "capture"
    raw_on = case.endswith("-raw")
    with _sinks(tmp_path) as (mediator, recorder, store):
        exchange = Capture(directory).exchange("http", "openai", "POST", "http://127.0.0.1/v1/x") if raw_on else None
        if case.startswith("websocket"):
            capture = ResponseCapture("responses")
            events = [
                {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": _TEXT},
                {"type": "response.completed", "response": {"usage": {}}},
            ]
            for event in events:
                capture.frame(json.dumps(event))
            text, wire = _TEXT, None
        elif case.startswith("gzip"):
            capture = ResponseCapture("chat", "text/event-stream", "gzip")
            capture.feed(gzip.compress(_RESPONSE))
            text, wire = _TEXT, _RESPONSE
        else:
            capture = ResponseCapture("chat", "text/event-stream")
            wire = _LARGE if case.startswith("large") else _RESPONSE
            capture.feed(wire)
            text = _LARGE_TEXT if case.startswith("large") else _TEXT
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor)
        outcome = Outcome({}, "openai", "/v1/x", "http", time.time(), status=200)
        finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, exchange, 200)
        finalizer.seal(outcome, complete=True)
        await asyncio.wait_for(finalizer.complete(), _TIMEOUT)
        detail = _detail(recorder, store)
        assert finalizer.state == "DONE" and len(submissions.rows) == submitted, len(submissions.rows)
        assert (detail.get("response") or {}).get("text") == text
        if raw_on and wire is not None:
            assert _payload(_records(directory)) == wire.decode()


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
async def test_an_omitted_answer_settles_without_waiting_for_the_engine(tmp_path, monkeypatch, websocket):
    """A cancelled or incomplete answer has nothing of its payload read: its settlement waits behind no masking on the
    identity's engine thread. A WebSocket's close waited there for a paused worker, until that worker gave up and
    released the stream it owned (the disconnect must leave it to its owner)."""
    with _sinks(tmp_path) as (mediator, recorder, store):
        held, started = threading.Event(), threading.Event()

        def masking() -> None:
            started.set()
            held.wait(_TIMEOUT)

        blocker = mediator.executor.submit(masking)
        assert started.wait(_TIMEOUT)
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor)
        capture = ResponseCapture("responses" if websocket else "chat", "" if websocket else "text/event-stream")
        if websocket:
            capture.frame('{"type":"response.output_text.delta","item_id":"m1","content_index":0,"delta":"half"}')
        else:
            capture.feed(_RESPONSE[: len(_RESPONSE) // 2])
        outcome = Outcome({}, "openai", "/v1/responses", "ws" if websocket else "http", time.time(), status=200)
        finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, None, 200)
        finalizer.seal(outcome, complete=False, error="closed" if websocket else "CancelledError")
        try:
            await asyncio.wait_for(finalizer.complete(propagate=False), 2)
            settled_alone = finalizer.state == "DONE" and not submissions.rows
        finally:
            held.set()
            blocker.result(_TIMEOUT)
        detail = _detail(recorder, store)
        assert settled_alone, "an omitted answer's settlement waited behind the engine thread"
        assert not capture.safe and not capture.text and not capture._raw
        assert (detail.get("response") or {}).get("omitted") == capture.omission
        assert capture.omission == ("response-incomplete" if websocket else "response-cancelled")


async def test_http_response_freezes_before_transferred_lease_release(tmp_path, monkeypatch):
    directory = tmp_path / "capture"
    gate = _Gate()
    answer = _Answer(_LARGE)  # screened on the engine's thread, which the gate holds
    events: list[str] = []
    owner: list[ExchangeLease] = []
    outcome_ref: weakref.ReferenceType[Outcome] | None = None
    frozen_headers: tuple[tuple[str, str], ...] = ()
    frozen_model: str | None = None
    mutated = False
    requests = 0
    freeze, transfer, release = Recorder.freeze_request, ExchangeLease.transfer, ExchangeLease.release
    screen = ResponseCapture.screen

    def freeze_probe(recorder: Recorder, outcome: Outcome):
        nonlocal outcome_ref, frozen_headers, frozen_model
        result = freeze(recorder, outcome)
        assert result is not None
        outcome_ref = weakref.ref(outcome)
        frozen_headers = tuple(redacted(result.request_headers))
        frozen_model = result.model
        events.append("freeze")
        return result

    def transfer_probe(lease: ExchangeLease) -> ExchangeLease:
        moved = transfer(lease)
        owner.append(moved)
        return moved

    def release_probe(lease: ExchangeLease) -> None:
        nonlocal mutated
        release(lease)
        if owner and lease is owner[0]:
            events.append("release")
            outcome = outcome_ref() if outcome_ref is not None else None
            if outcome is not None and outcome.prepared is not None:
                outcome.request_headers[:] = [("x-freeze-marker", "after")]
                outcome.prepared.model = "changed-after-freeze"
                mutated = True

    def screen_probe(capture: ResponseCapture) -> bool:
        events.append("screen")
        gate.block()
        return screen(capture)

    def provider(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

    monkeypatch.setattr(Recorder, "freeze_request", freeze_probe)
    monkeypatch.setattr(ExchangeLease, "transfer", transfer_probe)
    monkeypatch.setattr(ExchangeLease, "release", release_probe)
    monkeypatch.setattr(ResponseCapture, "screen", screen_probe)
    with _sinks(tmp_path) as (mediator, recorder, store):
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            assert isinstance(response, StreamingResponse)
            [renewer] = _renewers()
            task = asyncio.create_task(_wire(response, request))
            try:
                await asyncio.wait_for(gate.entered.wait(), _TIMEOUT)
                ordered = events == ["freeze", "release", "screen"] and mutated
                await asyncio.sleep(0)
                released = len(owner) == 1 and owner[0].released and renewer.cancelled()
                gate.release.set()
                status, wire = await asyncio.wait_for(asyncio.shield(task), _TIMEOUT)
            finally:
                await _cleanup(task, gate)
                if owner:
                    owner[0].cancel_renewer()
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.gather(renewer, return_exceptions=True), _TIMEOUT)

        detail = _detail(recorder, store)
        raw = _records(directory)
        checks = {
            "freeze before actual release and screening": ordered and not gate.expired,
            "actual lease and renewer drained before fallback": released,
            "persisted headers retained original frozen values": bool(frozen_headers)
            and tuple(map(tuple, detail["request"]["headers"])) == frozen_headers,
            "private model retained original frozen value": (
                frozen_model == "m" and (detail.get("sensitive") or {}).get("model") == "m"
            ),
            "public summary omits model": detail["summary"]["model"] is None,
            "one upstream call and original wire/status": requests == 1
            and answer.closed
            and status == 200
            and wire == _LARGE,
            "one safe history and raw response": (detail.get("response") or {}).get("text") == _LARGE_TEXT
            and _payload(raw) == _LARGE.decode()
            and sum(row["t"] == "end" for row in raw) == 1,
            # Its preparation and record_left, and the restore of its one chunk, past RESTORED_ON_LOOP.
            "exactly three other engine submissions": submissions.other == 3,
            "exactly two actual finalizer submissions": len(submissions.rows) == 2,
        }
        assert all(checks.values()), checks


@pytest.mark.parametrize("phase", ["precommit", "postcommit"])
async def test_finalizer_repeated_cancellation(tmp_path, monkeypatch, phase):
    directory = tmp_path / "capture"
    gate = _Gate()
    postcommit = phase == "postcommit"
    if postcommit:
        publish = ResponseCapture.publish

        def blocked(capture: ResponseCapture, exchange: Exchange, status: int, *, terminal: bool) -> None:
            gate.block()
            publish(capture, exchange, status, terminal=terminal)

        monkeypatch.setattr(ResponseCapture, "publish", blocked)
    else:
        _screen_gate(monkeypatch, gate)

    with _sinks(tmp_path) as (mediator, recorder, store):
        finalizer = _new_finalizer(directory, mediator, recorder)
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor)
        acknowledged = asyncio.Event()
        stop_calls = 0
        stop = lifecycle.ResponseFinalizer.stop

        def acknowledge(target: lifecycle.ResponseFinalizer) -> None:
            nonlocal stop_calls
            stop(target)
            stop_calls += 1
            acknowledged.set()

        monkeypatch.setattr(lifecycle.ResponseFinalizer, "stop", acknowledge)
        task = asyncio.create_task(finalizer.complete())
        try:
            await asyncio.wait_for(gate.entered.wait(), _TIMEOUT)
            actual = finalizer._actual
            assert actual is not None
            phase_correct = finalizer.state == ("COMMITTED_SAFE" if postcommit else "SCREENING")
            no_early_raw = not _payload(_records(directory))
            no_early_history = postcommit or not store.list()[0]
            stayed_owned = True
            for _ in range(2):
                acknowledged.clear()
                task.cancel()
                await asyncio.wait_for(acknowledged.wait(), _TIMEOUT)
                stayed_owned &= not task.done() and actual.running() and finalizer._actual is actual
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), _TIMEOUT)
            await asyncio.wait_for(finalizer.complete(), _TIMEOUT)
        finally:
            await _cleanup(task, gate)

        detail = _detail(recorder, store)
        history = detail.get("response") or {}
        raw = _records(directory)
        omissions = [row["reason"] for row in raw if row["t"] == "omitted"]
        checks = {
            "expected phase and no premature payload": phase_correct and no_early_raw and no_early_history,
            "same actual running future survived both cancellations": stayed_owned and stop_calls == 2,
            "both actual operations drained once": finalizer.state == "DONE"
            and not gate.expired
            and submissions.other == 0
            and len(submissions.rows) == 2
            and all(f.done() and f.result() for f, _, _ in submissions.rows),
            "one raw response end": sum(row["t"] == "end" for row in raw) == 1,
            "raw matches selected commit semantics": _payload(raw) == (_LARGE.decode() if postcommit else "")
            and omissions == ([] if postcommit else ["response-cancelled"]),
            "history matches selected commit semantics": history.get("text") == (_LARGE_TEXT if postcommit else "")
            and history.get("omitted") == (None if postcommit else "response-cancelled"),
            "screened usage retained": detail["summary"]["tokens"] == _TOKENS,
        }
        assert all(checks.values()), checks


async def test_finalizer_detaches_payload_from_actual_future_tails(tmp_path, monkeypatch):
    directory = tmp_path / "capture"
    screen_gate, callback_gate = _Gate(), _Gate()
    _screen_gate(monkeypatch, screen_gate)
    with _sinks(tmp_path) as (mediator, recorder, store):
        finalizer = _new_finalizer(directory, mediator, recorder)
        capture_ref = weakref.ref(finalizer.capture)
        assert finalizer._request is not None
        request_ref = weakref.ref(finalizer._request)
        hold = _CallbackHold(callback_gate)
        submissions = _Submissions()
        submissions.watch(monkeypatch, mediator.executor, hold)
        task = asyncio.create_task(finalizer.complete())
        try:
            await asyncio.wait_for(screen_gate.entered.wait(), _TIMEOUT)
            registered = hold.callback is not None
            screen_gate.release.set()
            await asyncio.wait_for(callback_gate.entered.wait(), _TIMEOUT)
            await asyncio.wait_for(submissions.second.wait(), _TIMEOUT)
            first, handoff, shell = submissions.rows[0]
            second = submissions.rows[1][0]
            original = hold.callback
            detached_while_blocked = (
                first.done()
                and first.result() is True
                and not handoff
                and shell.capture is None
                and shell.exchange is None
            )
            cells = tuple(cell.cell_contents for cell in original.__closure__ or ())
            callback_roots = (
                original.__code__.co_freevars == ("loop", "ready")
                and len(cells) == 2
                and cells[0] is asyncio.get_running_loop()
                and isinstance(cells[1], asyncio.Event)
            )
            raw_waited = not second.running() and not second.done() and not _payload(_records(directory))
            callback_gate.release.set()
            await asyncio.wait_for(asyncio.shield(task), _TIMEOUT)
        finally:
            await _cleanup(task, screen_gate, callback_gate)

        detail = _detail(recorder, store)
        raw = _records(directory)
        del finalizer, task
        await asyncio.sleep(0)
        checks = {
            "actual callback registered before releasing screening": registered,
            "worker handoff and shell detached while callback was live": detached_while_blocked,
            "original callback closes over loop and ready event only": callback_roots,
            "same executor prevented publication during callback tail": raw_waited,
            "all actual work completed without gate timeout": not screen_gate.expired
            and not callback_gate.expired
            and submissions.other == 0
            and len(submissions.rows) == 2
            and second.done()
            and second.result() is True,
            "live future and callback retain no staging or request base": first.done()
            and original is hold.callback
            and capture_ref() is None
            and request_ref() is None,
            "ordinary response published once after callback release": _payload(raw) == _LARGE.decode()
            and sum(row["t"] == "end" for row in raw) == 1
            and (detail.get("response") or {}).get("text") == _LARGE_TEXT,
        }
        assert all(checks.values()), checks


# --- F2 of the P2 baseline: an answer relayed whole is recorded whole, whatever its client does next ------------------


def _websocket_turn(mediator: Mediator, recorder: Recorder) -> lifecycle.ResponseFinalizer:
    """A WebSocket turn answered whole, sealed at its last event: screened on the engine's thread, as every WebSocket
    answer is (``_here``), where a test's gate holds it."""
    capture = ResponseCapture("responses")
    for event in (
        {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": _TEXT},
        {"type": "response.completed", "response": {"usage": {}}},
    ):
        capture.frame(json.dumps(event))
    outcome = Outcome({}, "openai", "/v1/responses", "ws", time.time(), status=101)
    finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, None, 101, terminal=False)
    finalizer.seal(outcome, complete=True)
    return finalizer


@pytest.mark.parametrize("relayed", [True, False], ids=["relayed-whole", "not-relayed"])
async def test_a_stop_while_screening_omits_only_an_answer_not_relayed_whole(tmp_path, monkeypatch, relayed):
    """The order F2's race took, forced: the relay ends (its stop, its wait given up) while the engine's thread screens
    the turn. An answer relayed whole settles alone, its wait gone at once, and is recorded whole; one that was not
    keeps the record it had, its answer omitted, its wait drained until the screening ends."""
    gate = _Gate()
    _screen_gate(monkeypatch, gate)
    with _sinks(tmp_path) as (mediator, recorder, store):
        finalizer = _websocket_turn(mediator, recorder)
        if relayed:
            finalizer.relayed()
        waiter = asyncio.create_task(finalizer.complete())
        try:
            await asyncio.wait_for(gate.entered.wait(), _TIMEOUT)
            screening = finalizer.state == "SCREENING"
            finalizer.stop()  # the relay's end
            waiter.cancel()  # and its wait given up
            await asyncio.wait({waiter}, timeout=0.5)
            went = waiter.done()
            alone = finalizer._task in lifecycle._ALONE.get(mediator.executor, ())
            gate.release.set()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(waiter), _TIMEOUT)
            assert finalizer._task is not None
            await asyncio.wait_for(asyncio.shield(finalizer._task), _TIMEOUT)
            await asyncio.sleep(0)  # its end's callbacks
        finally:
            await _cleanup(waiter, gate)
        detail = _detail(recorder, store)
        response = detail.get("response") or {}
        checks = {
            "the stop came while the engine's thread screened": screening and not gate.expired,
            "its wait went at once only when relayed whole": went is relayed and alone is relayed,
            "settled, nothing held after": finalizer.state == "DONE"
            and mediator.executor not in lifecycle._ALONE
            and not finalizer.capture._raw,
            "recorded whole when relayed whole, omitted otherwise": (
                response.get("text") == _TEXT
                and "omitted" not in response
                and "error" not in detail["summary"]["flags"]
                if relayed
                else response.get("omitted") == "response-cancelled" and not response.get("text")
            ),
        }
        assert all(checks.values()), checks


async def test_past_its_engines_bound_an_answer_relayed_whole_is_waited_for_and_recorded_whole(tmp_path, monkeypatch):
    """``ALONE_PER_ENGINE`` settlements at most end alone for one engine: past it, a wait given up waits for its own,
    as before, and never omits it."""
    monkeypatch.setattr(lifecycle, "ALONE_PER_ENGINE", 0)
    gate = _Gate()
    _screen_gate(monkeypatch, gate)
    with _sinks(tmp_path) as (mediator, recorder, store):
        finalizer = _websocket_turn(mediator, recorder)
        finalizer.relayed()
        waiter = asyncio.create_task(finalizer.complete())
        try:
            await asyncio.wait_for(gate.entered.wait(), _TIMEOUT)
            finalizer.stop()
            waiter.cancel()
            await asyncio.wait({waiter}, timeout=0.5)
            waited = not waiter.done() and mediator.executor not in lifecycle._ALONE
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(waiter), _TIMEOUT)
        finally:
            await _cleanup(waiter, gate)
        detail = _detail(recorder, store)
    assert waited and finalizer.state == "DONE"
    assert (detail.get("response") or {}).get("text") == _TEXT and "omitted" not in (detail.get("response") or {})


async def test_an_answer_not_sealed_whole_is_never_marked_relayed(tmp_path):
    """A turn cut off mid-answer keeps the record it had: its stop omits it, and it never settles alone."""
    with _sinks(tmp_path) as (mediator, recorder, store):
        capture = ResponseCapture("responses")
        capture.frame('{"type":"response.output_text.delta","item_id":"m1","content_index":0,"delta":"half"}')
        outcome = Outcome({}, "openai", "/v1/responses", "ws", time.time(), status=101)
        finalizer = lifecycle.ResponseFinalizer(capture, mediator.executor, recorder, None, 101, terminal=False)
        finalizer.seal(outcome, complete=False, error="closed")
        finalizer.relayed()
        finalizer.stop()
        assert not finalizer.leave()
        await asyncio.wait_for(finalizer.complete(propagate=False), _TIMEOUT)
        detail = _detail(recorder, store)
    assert (detail.get("response") or {}).get("omitted") == "response-incomplete"


class _Halves(httpx.AsyncByteStream):
    """A provider's streamed answer in two parts, its text's event then its end; ``between`` set, its end waits for
    it."""

    def __init__(self, between: asyncio.Event | None) -> None:
        self.between = between

    async def __aiter__(self):
        yield _LARGE[: -len(_END)]
        if self.between is not None:
            await self.between.wait()
        yield _END

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("gone", ["after-its-last-chunk", "before-its-last-chunk"])
async def test_a_streamed_answer_relayed_whole_is_recorded_whole_though_its_request_is_cancelled_at_once(
    tmp_path, monkeypatch, gone
):
    """F2's HTTP counterpart (minors after 0.11, item 3): a client that reads a streamed answer to its end and goes at
    once (``claude -p``, a script) has its request's task cancelled by the server while Latet settles the answer's
    record. An answer its client was given whole is recorded whole, as a WebSocket turn is; one cut off before its end
    keeps its record, its answer omitted. The engine's thread screens this answer (past RESTORED_ON_LOOP), held by a
    gate, so that the client goes exactly while the record is settled."""
    gate = _Gate()
    _screen_gate(monkeypatch, gate)
    after = gone == "after-its-last-chunk"
    if not after:
        gate.release.set()  # an answer cut off is omitted where it ends, never screened
    with _sinks(tmp_path) as (mediator, recorder, store):
        answer = _Halves(None if after else asyncio.Event())

        def provider(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            sent: list[bytes] = []
            first = asyncio.Event()

            async def send(message: Any) -> None:
                if message.get("body"):
                    sent.append(message["body"])
                    first.set()

            task = asyncio.create_task(response(dict(request.scope), request.receive, send))  # type: ignore[operator]
            try:
                # Relayed whole, its screening holding the engine's thread; or its text alone relayed, not its end.
                await asyncio.wait_for((gate.entered if after else first).wait(), _TIMEOUT)
                task.cancel()  # its client gone: the server cancels the request's task
                gate.release.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), _TIMEOUT)
                for _ in range(500):  # a settlement left to end alone ends once its screening does
                    if mediator.executor not in lifecycle._ALONE:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await _cleanup(task, gate)
        detail = _detail(recorder, store)
    response_ = detail.get("response") or {}
    if after:
        assert b"".join(sent) == _LARGE and not gate.expired
        assert response_.get("text") == _LARGE_TEXT and "omitted" not in response_, response_.get("omitted")
        assert "error" not in detail["summary"]["flags"]
    else:
        assert _END not in b"".join(sent)
        assert response_.get("omitted") == "response-cancelled" and not response_.get("text")
