"""Finite actual STORE invocation-tail and failure ownership checks."""

import asyncio
import threading
from concurrent.futures import Future
from functools import partial
from types import SimpleNamespace
from typing import cast

import pytest

from latet.server import launch_mutations, machines
from latet.server.launch_binding import BindingCodec
from latet.server.launch_controls import LaunchHoldAdmission
from latet.server.launch_lifecycle import _ProtectedHold, _result
from latet.server.launch_mutations import (
    MUTATION_BYTES,
    LaunchMutations,
    MutationAdmission,
    OpenOutcome,
    OpenResultCell,
    TerminalIntent,
)
from latet.server.launch_sweep import LaunchSweep, SweepState, _pending, _Work
from latet.server.launches import LaunchAuthority, TerminalTarget
from latet.server.spaces import Spaces

_ISSUER = "u_aaaaaaaaaa"
_TARGET = b"i" * 16


def _ticket(ledger):
    ticket = ledger.reserve(lambda: True)
    assert ticket is not None
    assert ticket.attach(_ISSUER, lambda _added: None) and ticket.identify(_TARGET)
    return ticket


async def _turn():
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def _join(work):
    for actual in work:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(actual)), 3)


@pytest.mark.parametrize("mode", ["setup", "rejected", "lost", "registration", "inline", "missing"])
async def test_submit_failure_uses_actual_invocation_end(monkeypatch, mode):
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    ticket = _ticket(ledger)
    intent = TerminalIntent(_ISSUER, _TARGET, generation=1)
    fallback = OpenOutcome("unavailable", retained=None if mode == "setup" else intent)
    result = OpenResultCell()
    work = []
    notifications = []
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_submit = machines.submit_store
    original_notify = MutationAdmission._tail_done
    original_callback = Future.add_done_callback

    def notified(self, owner_loop, marker):
        invocation = marker()
        assert invocation is not None
        notifications.append(
            invocation.fn is None
            and invocation.argument is None
            and invocation.result is None
            and invocation.notify is None
            and invocation.marker is None
        )
        del invocation
        original_notify(self, owner_loop, marker)

    def worker(cell):
        loop.call_soon_threadsafe(entered.set)
        if mode != "inline":
            assert release.wait(3)
        if mode != "missing":
            cell.outcome = OpenOutcome("ended")

    def submit(fn, argument):
        assert not work
        if mode == "rejected":
            raise RuntimeError("fixed prequeue rejection")
        actual = original_submit(fn, argument)
        work.append(actual)
        if mode == "lost":
            raise RuntimeError("fixed after-enqueue failure")
        if mode == "inline":
            actual.result(timeout=3)
        return actual

    def callback(self, fn):
        if isinstance(fn, partial) and getattr(fn.func, "__func__", None) is MutationAdmission._completed:
            raise RuntimeError("fixed registration failure")
        return original_callback(self, fn)

    def setup(*_args, **_kwargs):
        raise MemoryError("fixed invocation setup failure")

    monkeypatch.setattr(machines, "submit_store", submit)
    monkeypatch.setattr(MutationAdmission, "_tail_done", notified)
    if mode == "registration":
        monkeypatch.setattr(Future, "add_done_callback", callback)
    if mode == "setup":
        monkeypatch.setattr(launch_mutations, "_Invocation", setup)
    try:
        async with asyncio.timeout(10):
            ticket.submit(worker, result, result, not_started=fallback)
            assert ticket.pending and not ticket._ready.is_set()
            if mode in {"setup", "rejected", "lost"}:
                assert ticket.actual is None
            if mode not in {"setup", "rejected", "inline"}:
                await entered.wait()
                assert result.started and ticket.pending
                with pytest.raises(RuntimeError, match="have not drained"):
                    ledger.require_drained()
                assert not ticket.claim_detached(_ISSUER, _TARGET, lambda: True)
                view = asyncio.create_task(_result(ticket))
                await _turn()
                assert not view.done()
                view.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await view
            release.set()
            outcome = await asyncio.wait_for(ticket.wait(), 3)
            assert not ticket.pending and ticket.actual is None and result.outcome is None
            if mode in {"setup", "rejected"}:
                assert outcome is fallback and not result.started and not work
            elif mode == "missing":
                assert outcome.uncertain and outcome.retained is intent and result.started
            else:
                assert outcome.status == "ended" and outcome.retained is None and result.started
                assert len(work) == 1 and work[0].done()
            assert notifications == ([] if mode == "setup" else [True])
            ticket.end_ingress()
            expected = MUTATION_BYTES if mode in {"rejected", "missing"} else 0
            assert ledger.retained_bytes == base + expected
            # Legitimate observers still own actual Futures here; they do not own the marker.
            await ledger.drain()
            assert ledger.retained_bytes == base + expected
    finally:
        release.set()
        ticket.end_ingress()
        await _join(work)
        await ledger.drain()


async def test_callback_fault_preserves_authoritative_and_fallback_obligations():
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    ticket = _ticket(ledger)
    intent = TerminalIntent(_ISSUER, _TARGET, generation=1)
    maintenance = SweepState("parked")
    fallback = OpenOutcome("unavailable", maintenance=maintenance)
    result = OpenResultCell()
    observed = []

    def worker(cell):
        cell.outcome = OpenOutcome("ended", retained=intent)

    def consumed(current, started):
        observed.append(started and current.outcome is not None and current.outcome.retained is intent)
        current.outcome = None
        raise RuntimeError("fixed completion callback failure")

    try:
        ticket.submit(worker, result, result, not_started=fallback, consumed=consumed)
        outcome = await asyncio.wait_for(ticket.wait(), 3)
        assert observed == [True] and outcome.uncertain
        assert outcome.retained is intent and outcome.maintenance is maintenance
        assert not ticket.pending and result.outcome is None
        ticket.end_ingress()
        await ledger.drain()
        assert ledger.retained_bytes == base + MUTATION_BYTES and ledger.issuer_bytes(_ISSUER) == MUTATION_BYTES
    finally:
        ticket.end_ingress()
        await ledger.drain()


async def test_resident_rearm_waits_for_tail_with_repeated_cancellation(monkeypatch):
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    ticket = _ticket(ledger)
    holds = LaunchHoldAdmission()
    physical = holds.reserve()
    assert physical is not None and physical.adopt()
    driver = _ProtectedHold(
        cast(LaunchAuthority, object()), BindingCodec(b"r" * 32), _ISSUER, _TARGET, ticket, physical
    )
    ticket.driver = driver
    ticket.end_ingress()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    entered = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    run = [threading.Event(), threading.Event()]
    original_schedule = loop.call_soon_threadsafe
    original_submit = machines.submit_store
    work = []
    schedules = 0
    waiter = None

    def submit(fn, argument):
        assert len(work) < 2
        actual = original_submit(fn, argument)
        work.append(actual)
        return actual

    def schedule(callback, *args, **kwargs):
        nonlocal schedules
        if getattr(callback, "__func__", None) is not MutationAdmission._consume:
            return original_schedule(callback, *args, **kwargs)
        index = schedules
        schedules += 1
        assert index < 2 and threading.get_ident() != main_thread
        handle = original_schedule(callback, *args, **kwargs)
        original_schedule(entered[index].set)
        assert release[index].wait(3)
        return handle

    def worker(argument):
        cell, index = argument
        assert run[index].wait(3)
        cell.outcome = OpenOutcome("unavailable")

    monkeypatch.setattr(machines, "submit_store", submit)
    monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)
    try:
        async with asyncio.timeout(10):
            first = OpenResultCell()
            ticket.submit(worker, (first, 0), first, not_started=OpenOutcome("unavailable"))
            old_marker = ticket._tail
            assert old_marker is not None
            run[0].set()
            await entered[0].wait()
            assert ticket.pending and not ticket._ready.is_set() and first.outcome is not None
            assert ledger.retained_bytes == base + MUTATION_BYTES
            with pytest.raises(RuntimeError, match="not been consumed"):
                ticket.rearm()
            waiter = asyncio.create_task(driver._settled())
            await _turn()
            for _ in range(2):
                waiter.cancel()
                await _turn()
                assert not waiter.done() and ticket.pending and schedules == 1
            release[0].set()
            assert await asyncio.wait_for(waiter, 3) is ticket.outcome
            assert not ticket.pending and old_marker() is None and work[0].done()
            ticket.outcome = None
            second = OpenResultCell()
            ticket.submit(worker, (second, 1), second, not_started=OpenOutcome("unavailable"))
            current_marker = ticket._tail
            ticket._tail_released(old_marker)
            ticket._consume(work[0])
            assert ticket.pending and ticket._tail is current_marker and len(work) == 2
            run[1].set()
            await entered[1].wait()
            assert ticket.pending and not ticket._ready.is_set()
            release[1].set()
            await asyncio.wait_for(ticket.wait(), 3)
            driver.close()
            assert holds.count == 0 and ledger.retained_bytes == base and schedules == 2
            assert len(work) == 2 and all(actual.done() for actual in work)
    finally:
        for event in (*run, *release):
            event.set()
        if waiter is not None:
            await asyncio.wait_for(asyncio.shield(waiter), 3)
        await _join(work)
        if ticket.pending:
            await asyncio.wait_for(ticket.wait(), 3)
        driver.close()
        await ledger.drain()


async def test_sweep_never_started_restores_previous_then_gated_phase_settles(monkeypatch):
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    ticket = _ticket(ledger)
    previous = OpenOutcome("unavailable", retained=TerminalIntent(_ISSUER, _TARGET, generation=1))
    ticket.outcome = previous
    ticket.end_ingress()
    spaces = cast(Spaces, SimpleNamespace(_launch_mutations=ledger, launch_fits=lambda: True))
    sweep = LaunchSweep()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    work = []
    calls = 0
    submissions = 0
    original = machines.submit_store

    def terminal(issuer, launch_id, **options):
        nonlocal calls
        calls += 1
        assert (issuer, launch_id, options["generation"]) == (_ISSUER, _TARGET, 1)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return TerminalTarget(b"l" * 32, 1, False, False)

    authority = cast(LaunchAuthority, SimpleNamespace(terminal_control=terminal))

    def submit(fn, argument):
        nonlocal submissions
        submissions += 1
        assert submissions <= 2
        if submissions == 1:
            raise RuntimeError("fixed never-started sweep")
        actual = original(fn, argument)
        work.append(actual)
        return actual

    monkeypatch.setattr(machines, "submit_store", submit)
    try:
        async with asyncio.timeout(10):
            state = _pending(previous)
            assert state is not None
            sweep._start(ticket, _Work(authority, OpenResultCell(), state), spaces)
            assert sweep._active is ticket and ticket.pending and ticket.actual is None
            for _ in range(3):
                sweep.tick(authority, spaces)
            assert submissions == 1 and calls == 0
            await asyncio.wait_for(ticket.wait(), 3)
            assert ticket.outcome is previous and sweep._active is None and not ticket.pending
            assert ledger.retained_bytes == base + MUTATION_BYTES
            sweep._start(ticket, _Work(authority, OpenResultCell(), state), spaces)
            await entered.wait()
            assert sweep._active is ticket and ticket.pending and calls == 1
            for _ in range(3):
                sweep.tick(authority, spaces)
            assert submissions == 2 and len(work) == 1
            release.set()
            await asyncio.wait_for(ticket.wait(), 3)
            assert sweep._active is None and not ticket.pending
            assert ledger.retained_bytes == base and ledger.issuer_bytes(_ISSUER) == 0
    finally:
        release.set()
        await _join(work)
        await ledger.drain()
