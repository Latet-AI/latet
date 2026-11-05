"""Deterministic checks of the protected launch sweep's detached ownership."""

import asyncio
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from latet.server import machines
from latet.server.launch_binding import RemoteResult, WireResult, _BorrowedConnection
from latet.server.launch_mutations import (
    MUTATION_BYTES,
    LaunchMutations,
    MutationAdmission,
    OpenOutcome,
    TerminalIntent,
)
from latet.server.launch_records import Entry, LaunchUnavailable, Operation, Reason, State
from latet.server.launch_sweep import LaunchSweep
from latet.server.launches import Completion, LaunchAuthority, TerminalTarget, Ticket
from latet.server.spaces import Spaces


@pytest.mark.parametrize("state", ["replacement", "absent"])
async def test_uncertain_unbound_hold_intent_never_terminalizes_current_launch(monkeypatch, state):
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    issuer, launch_id = "u_aaaaaaaaaa", b"i" * 16
    ticket = ledger.reserve(lambda: True)
    assert ticket is not None and ticket.attach(issuer, lambda _added: None) and ticket.identify(launch_id)
    intent = TerminalIntent(issuer, launch_id, uncertain=True)
    outcome = OpenOutcome("unavailable", retained=intent)
    ticket.outcome = outcome
    ticket.end_ingress()

    terminal_calls = cleanup_calls = 0

    def terminal_control(_issuer, _launch_id, **_options):
        nonlocal terminal_calls
        terminal_calls += 1
        return TerminalTarget(b"l" * 32, 2, True, False) if state == "replacement" else None

    def reserve_cleanup(*_args, **_kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise AssertionError("unexpected binding cleanup")

    authority = cast(
        LaunchAuthority, SimpleNamespace(terminal_control=terminal_control, reserve_cleanup=reserve_cleanup)
    )
    spaces = cast(Spaces, SimpleNamespace(_launch_mutations=ledger, launch_fits=lambda: True))
    sweep = LaunchSweep()
    sweep._pg_next = False  # Select one LOCAL beat without unrelated discovery.

    work = []
    original = machines.submit_store

    def submit(fn, argument):
        assert not work
        actual = original(fn, argument)
        work.append(actual)
        return actual

    monkeypatch.setattr(machines, "submit_store", submit)
    try:
        sweep.tick(authority, spaces)
        if work:
            async with asyncio.timeout(3):
                await asyncio.shield(asyncio.wrap_future(work[0]))
                await ticket.wait()
        assert (terminal_calls, cleanup_calls) == (0, 0)
        assert not work
        assert ticket.outcome is outcome and outcome.retained is intent and intent.uncertain
        assert ledger._slots[ticket.slot] is ticket and ticket.claimed and ticket.target == launch_id
        assert ledger.issuer_bytes(issuer) == MUTATION_BYTES and ledger.retained_bytes == base + MUTATION_BYTES
    finally:
        await ledger.drain()


_ISSUER = "u_aaaaaaaaaa"
_LOCATOR = b"l" * 32
_GENERATION = 2


class _Gate:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = Event()
        self.expired = False

    def block(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        self.expired = not self.release.wait(3)


class _SweepAuthority:
    """Data-only authority with observable errors even when the sweep catches them."""

    def __init__(self, launch_id: bytes, *, discovery: bool = False, gate: _Gate | None = None) -> None:
        self.launch_id = launch_id
        self.discovery = discovery
        self.gate = gate
        self.calls: Counter[str] = Counter()
        self.bad_arguments = 0

    def __getattr__(self, name: str) -> None:
        self.calls[name] += 1
        raise AssertionError(f"unexpected authority call: {name}")

    def expired_locators(self, cursor: bytes | None, limit: int) -> tuple[bytes, ...]:
        self.calls["expired_locators"] += 1
        self.bad_arguments += (cursor, limit) != (None, 16)
        return (_LOCATOR,) if self.discovery else ()

    def _snapshot(self, locator: bytes) -> tuple[Entry, int]:
        self.calls["snapshot"] += 1
        self.bad_arguments += locator != _LOCATOR or not self.discovery
        assert self.gate is not None
        self.gate.block()
        return (
            Entry(
                locator=_LOCATOR,
                launch_id=self.launch_id,
                issuer=_ISSUER,
                epoch=b"e" * 16,
                revision=1,
                generation=_GENERATION,
                op_seq=0,
                state=State.LIVE,
                deadline_us=1,
                binding_reserved=True,
                binding_digest=b"d" * 32,
                published_token=b"p" * 16,
                published_blob_digest=b"b" * 32,
                terminal_reason=Reason.NONE,
                disclosure_possible=True,
                exit_confirmed=False,
                op_kind=Operation.NONE,
                op_id=None,
                op_owner=None,
                op_base_gen=None,
                op_target_gen=None,
                op_payload_digest=None,
            ),
            2,
        )

    def terminal_control(
        self,
        issuer: str,
        launch_id: bytes,
        *,
        generation: int | None,
        operation: Ticket | None,
        exit_confirmed: bool,
    ) -> TerminalTarget:
        self.calls["terminal_control"] += 1
        self.bad_arguments += (issuer, launch_id, generation, operation, exit_confirmed) != (
            _ISSUER,
            self.launch_id,
            _GENERATION,
            None,
            False,
        )
        if self.gate is not None:
            self.gate.block()
            raise LaunchUnavailable()
        return TerminalTarget(_LOCATOR, _GENERATION, False, False)


def _spaces(ledger: LaunchMutations) -> Spaces:
    return cast(
        Spaces,
        SimpleNamespace(
            _launch_mutations=ledger,
            launch_fits=lambda: True,
            launch_background_fits=lambda _issuer: True,
        ),
    )


def _capture_store(monkeypatch: pytest.MonkeyPatch, limit: int) -> list[Future[None]]:
    work: list[Future[None]] = []
    original = machines.submit_store

    def submit(fn: Callable[[object], None], argument: object) -> Future[None]:
        assert len(work) < limit and all(actual.done() for actual in work)
        actual = original(fn, argument)
        work.append(actual)
        return actual

    monkeypatch.setattr(machines, "submit_store", submit)
    return work


def _detach(ledger: LaunchMutations, launch_id: bytes, outcome: OpenOutcome) -> MutationAdmission:
    ticket = ledger.reserve(lambda: True)
    assert ticket is not None and ticket.attach(_ISSUER, lambda _added: None) and ticket.identify(launch_id)
    ticket.outcome = outcome
    ticket.end_ingress()
    return ticket


def _assert_paid(ledger: LaunchMutations, ticket: MutationAdmission, launch_id: bytes) -> None:
    assert ledger._slots[ticket.slot] is ticket and ticket.claimed and not ticket.ingress
    assert ticket.issuer == _ISSUER and ticket.target == launch_id


async def _join_actual(actual: Future[None]) -> None:
    await asyncio.shield(asyncio.wrap_future(actual))


async def _join_store(ledger: LaunchMutations, work: list[Future[None]], drain: asyncio.Task[None] | None) -> None:
    ledger.stop()
    async with asyncio.timeout(15):
        try:
            for actual in work:
                await _join_actual(actual)
        finally:
            if drain is None:
                await ledger.drain()
            else:
                await asyncio.shield(drain)


async def test_full_ledger_two_parked_passes_then_local_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    spaces = _spaces(ledger)
    launch_id = (16).to_bytes(16, "big")
    authority = _SweepAuthority(launch_id)
    typed_authority = cast(LaunchAuthority, authority)
    sweep = LaunchSweep()
    work = _capture_store(monkeypatch, 1)
    dirty = cast(_BorrowedConnection, object())
    evidence: list[tuple[MutationAdmission, bytes, OpenOutcome, Completion, Ticket]] = []
    try:
        async with asyncio.timeout(10):
            for slot in range(1024):
                target = slot.to_bytes(16, "big")
                operation = Ticket(
                    _ISSUER, _LOCATOR, target, b"e" * 16, b"o" * 16, target, Operation.HOLD, 1, 1, 2, None
                )
                wire = WireResult(RemoteResult.SET, dirty) if slot % 2 else WireResult(RemoteResult.UNKNOWN)
                done = Completion(operation, wire, settled=False)
                retained = (
                    TerminalIntent(_ISSUER, target, operation=operation, prior=done, uncertain=True)
                    if slot < 4
                    else done
                )
                outcome = OpenOutcome("unavailable", retained=retained)
                ticket = _detach(ledger, target, outcome)
                assert ticket.slot == slot
                evidence.append((ticket, target, outcome, done, operation))

            def assert_evidence(*, refunded: bool) -> None:
                count = 1023 if refunded else 1024
                assert ledger.retained_bytes == base + count * MUTATION_BYTES
                assert ledger.issuer_bytes(_ISSUER) == count * MUTATION_BYTES
                for ticket, target, outcome, done, operation in evidence:
                    if refunded and ticket.slot == 16:
                        assert ledger._slots[16] is None and not ticket.claimed
                        continue
                    _assert_paid(ledger, ticket, target)
                    assert ticket.actual is None and ticket.outcome is outcome
                    if isinstance(outcome.retained, TerminalIntent):
                        assert outcome.retained.prior is done and outcome.retained.operation is operation
                    else:
                        assert outcome.retained is done
                    assert done.ticket is operation
                    if ticket.slot % 2:
                        assert done.wire.remote == RemoteResult.SET and done.wire.cleanup_pending is dirty
                    else:
                        assert done.wire.remote == RemoteResult.UNKNOWN and done.wire.cleanup_pending is None

            for _ in range(2):
                for turn in range(128):
                    sweep.tick(typed_authority, spaces)
                    assert sweep._local == ((turn + 1) // 2 * 16) % 1024
                    assert sweep._pg_next == (turn % 2 == 1)
                assert sweep._local == 0 and sweep._pg is None and sweep._pg_next and sweep._active is None
                assert not work and not authority.calls and authority.bad_arguments == 0
                assert_evidence(refunded=False)

            ticket = evidence[16][0]
            # Replace this fixture's evidence to make an eligible exact-generation LOCAL phase.
            ticket.outcome = OpenOutcome("unavailable", retained=TerminalIntent(_ISSUER, launch_id, _GENERATION))
            sweep.tick(typed_authority, spaces)
            sweep.tick(typed_authority, spaces)
            assert sweep._local == 16 and not work and not authority.calls
            sweep.tick(typed_authority, spaces)
            assert not work and not authority.calls
            sweep.tick(typed_authority, spaces)
            assert len(work) == 1 and ticket.actual is work[0] and sweep._active is ticket
            _assert_paid(ledger, ticket, launch_id)
            assert ledger.retained_bytes == base + 1024 * MUTATION_BYTES
            assert ledger.issuer_bytes(_ISSUER) == 1024 * MUTATION_BYTES
            await _join_actual(work[0])
            await ticket.wait()
            assert sweep._local == 17 and sweep._active is None and ticket.actual is None
            assert authority.calls == {"terminal_control": 1} and authority.bad_arguments == 0
            assert_evidence(refunded=True)
    finally:
        await _join_store(ledger, work, None)


async def _blocked_drain(monkeypatch: pytest.MonkeyPatch, *, local: bool) -> None:
    ledger = LaunchMutations()
    base = ledger.retained_bytes
    spaces = _spaces(ledger)
    sweep = LaunchSweep()
    launch_id = b"i" * 16
    gate = _Gate()
    authority = _SweepAuthority(launch_id, discovery=not local, gate=gate)
    typed_authority = cast(LaunchAuthority, authority)
    work = _capture_store(monkeypatch, 2 if local else 1)
    intent = TerminalIntent(_ISSUER, launch_id, _GENERATION)
    outcome = OpenOutcome("unavailable", retained=intent)
    local_ticket = _detach(ledger, launch_id, outcome) if local else None
    waiter: asyncio.Task[OpenOutcome] | None = None
    drain: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(10):
            sweep.tick(typed_authority, spaces)
            discovery = sweep._active
            assert discovery is not None and len(work) == 1 and discovery.actual is work[0]
            assert discovery.issuer is None and discovery.target is None and not discovery.claimed
            assert ledger.retained_bytes == base + (2 if local else 1) * MUTATION_BYTES
            if local_ticket is not None:
                await _join_actual(work[0])
                await discovery.wait()
                assert sweep._active is None and ledger._slots[discovery.slot] is None
                assert ledger.retained_bytes == base + MUTATION_BYTES
                sweep.tick(typed_authority, spaces)

            ticket = local_ticket if local_ticket is not None else discovery
            assert sweep._active is ticket and len(work) == (2 if local else 1)
            actual = work[-1]
            await gate.entered.wait()
            assert ticket.actual is actual and not actual.done()
            assert ledger.retained_bytes == base + MUTATION_BYTES
            assert ledger.issuer_bytes(_ISSUER) == (MUTATION_BYTES if local else 0)

            waiter = asyncio.create_task(ticket.wait())
            # A call_soon marker runs after the waiter has reached its first suspension.
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not actual.done() and ticket.actual is actual

            cursors = sweep._pg_next, sweep._pg, sweep._local
            submissions = len(work)
            for _ in range(3):
                sweep.tick(typed_authority, spaces)
                assert (sweep._pg_next, sweep._pg, sweep._local) == cursors
                assert len(work) == submissions and sweep._active is ticket and ticket.actual is actual

            ledger.stop()
            with pytest.raises(RuntimeError, match="launch controls have not drained"):
                ledger.require_drained()
            drain = asyncio.create_task(ledger.drain())
            checkpoint.clear()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            assert not drain.done() and not actual.done()
    finally:
        gate.release.set()
        try:
            if waiter is not None:
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter
        finally:
            await _join_store(ledger, work, drain)
            if local_ticket is not None:
                _assert_paid(ledger, local_ticket, launch_id)
                assert ledger.retained_bytes == base + MUTATION_BYTES
                assert ledger.issuer_bytes(_ISSUER) == MUTATION_BYTES
                parked = local_ticket.outcome
                assert parked is not None
                if parked is not outcome:
                    assert parked.maintenance is not None and parked.maintenance.phase == "parked"
                    assert parked.maintenance.intent is intent

    assert not gate.expired and authority.bad_arguments == 0
    assert sweep._active is None and ticket.actual is None and actual.done() and not actual.cancelled()
    assert drain is not None and drain.done()
    ledger.require_drained()
    if local:
        assert ticket.outcome is not outcome
        assert authority.calls == {"expired_locators": 1, "terminal_control": 1}
    else:
        assert authority.calls == {"expired_locators": 1, "snapshot": 1}
        assert ledger.retained_bytes == base and ledger.issuer_bytes(_ISSUER) == 0
        assert ledger._slots[ticket.slot] is None and ticket.issuer is None and ticket.outcome is None
        assert sweep._pg == _LOCATOR


async def test_blocked_discovery_drain_joins_actual_store_work(monkeypatch: pytest.MonkeyPatch) -> None:
    await _blocked_drain(monkeypatch, local=False)


async def test_blocked_local_drain_preserves_paid_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    await _blocked_drain(monkeypatch, local=True)
