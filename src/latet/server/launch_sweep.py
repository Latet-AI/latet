"""One prepaid protected-launch maintenance phase per server beat."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Literal

from latet.server.launch_binding import RemoteResult
from latet.server.launch_mutations import MutationAdmission, OpenOutcome, OpenResultCell, TerminalIntent
from latet.server.launch_records import Operation, exact_bytes
from latet.server.launches import Completion, LaunchAuthority, Publication, ReservationUncertain

if TYPE_CHECKING:
    from latet.server.spaces import Spaces

_VISITS = 16
_Phase = Literal["inspect", "abandon", "settle", "terminal", "cleanup", "retire", "parked"]


@dataclass(frozen=True, slots=True, repr=False)
class _Target:
    issuer: str
    launch_id: bytes
    locator: bytes
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class SweepState:
    phase: _Phase
    intent: TerminalIntent | None = None
    evidence: Publication | Completion | None = None
    target: _Target | None = None
    undisclosed: Completion | None = None

    def parked(self) -> SweepState:
        return replace(self, phase="parked")


@dataclass(slots=True, repr=False)
class _Work:
    authority: LaunchAuthority
    result: OpenResultCell
    state: SweepState | None = None
    cursor: bytes | None = None
    next_cursor: bytes | None = None


def _discover(work: _Work) -> SweepState | None:
    authority = work.authority
    work.next_cursor = work.cursor
    page = authority.expired_locators(work.cursor, _VISITS)
    for locator in page:
        if not exact_bytes(locator, 32):
            continue
        work.next_cursor = locator
        entry, now = authority._snapshot(locator)
        if (
            entry is not None
            and entry.binding_reserved
            and entry.op_kind == Operation.NONE
            and entry.deadline_us <= now
        ):
            target = _Target(entry.issuer, entry.launch_id, locator, entry.generation)
            return SweepState("cleanup", target=target)
        return None
    work.next_cursor = None
    return None


def _after_completion(state: SweepState, done: Completion) -> SweepState | None:
    wire, ticket = done.wire, done.ticket
    state = replace(state, evidence=done)
    if wire.remote == RemoteResult.UNKNOWN or wire.cleanup_pending is not None:
        return state.parked()
    if not done.settled:
        return replace(state, phase="settle") if state.phase != "settle" else state.parked()
    if ticket.kind == Operation.DELETE and state.target is not None:
        if wire.remote != RemoteResult.DELETE:
            # A confirmed reservation abandoned without wire leaves the binding for a later discovery.
            return None if state.intent is None and state.undisclosed is None else state.parked()
        if state.undisclosed is not None or (state.intent is not None and state.intent.exit_confirmed):
            return replace(state, phase="retire")
        return None
    if ticket.kind == Operation.OPEN and done.receipt is None:
        target = _Target(ticket.issuer, ticket.launch_id, ticket.locator, ticket.base)
        return replace(state, phase="cleanup", target=target, undisclosed=done)
    if state.intent is not None:
        return replace(state, phase="terminal")
    return None


def _step(authority: LaunchAuthority, state: SweepState) -> SweepState | None:
    evidence, intent, target = state.evidence, state.intent, state.target
    if state.phase == "inspect" and isinstance(evidence, Publication):
        if evidence.attempted:
            return state.parked()
        if authority.reconcile_reservation(evidence):
            return replace(state, phase="abandon")
        return replace(state, phase="terminal", evidence=None) if intent is not None else None
    if state.phase == "abandon" and isinstance(evidence, Publication):
        return _after_completion(state, authority.abandon_unsubmitted(evidence))
    if state.phase == "settle" and isinstance(evidence, Completion):
        return _after_completion(state, authority.reconcile_completion(evidence))
    if state.phase == "terminal" and intent is not None:
        confirmed = authority.terminal_control(
            intent.issuer,
            intent.launch_id,
            generation=intent.generation,
            operation=intent.operation,
            exit_confirmed=intent.exit_confirmed,
        )
        if confirmed is None:
            return None
        target = _Target(intent.issuer, intent.launch_id, confirmed.locator, confirmed.generation)
        state = replace(state, target=target)
        if confirmed.has_operation:
            return state.parked()
        if confirmed.binding_reserved:
            return replace(state, phase="cleanup")
        return replace(state, phase="retire") if intent.exit_confirmed else None
    if state.phase == "cleanup" and target is not None:
        pending = authority.reserve_cleanup(
            target.issuer, target.locator, launch_id=target.launch_id, generation=target.generation
        )
        if pending is None:
            return None if intent is None and state.undisclosed is None else state.parked()
        try:
            done = authority.submit(pending)
        except Exception:
            return replace(state, evidence=pending).parked()
        return _after_completion(state, done)
    if (
        state.phase == "retire"
        and target is not None
        and authority.retire(target.issuer, target.locator, target.generation, undisclosed=state.undisclosed)
    ):
        return None
    return state.parked()


def _run(work: _Work) -> None:
    state = work.state
    try:
        updated = _discover(work) if state is None else _step(work.authority, state)
    except ReservationUncertain as error:
        if state is None:
            updated = None
        elif state.phase == "cleanup":
            updated = replace(state, evidence=error.publication, phase="inspect")
        else:
            updated = state.parked()
    except Exception:
        updated = state.parked() if state is not None else None
    work.state = None
    work.result.outcome = OpenOutcome("unavailable", maintenance=updated)


def _pending(outcome: OpenOutcome) -> SweepState | None:
    retained = outcome.retained
    intent = retained if isinstance(retained, TerminalIntent) else None
    if intent is not None and intent.uncertain and intent.generation is None and intent.operation is None:
        # An unbound failed HOLD cannot target the current launch by ID.
        return None
    evidence = intent.prior if intent is not None else retained
    if isinstance(evidence, Publication):
        return None if evidence.attempted else SweepState("inspect", intent, evidence)
    if isinstance(evidence, Completion):
        state = SweepState("settle", intent, evidence)
        if evidence.wire.remote == RemoteResult.UNKNOWN or evidence.wire.cleanup_pending is not None:
            return None
        if not evidence.settled:
            return state
        # An uncertain explicit retirement already holds its clean DELETE evidence.
        if evidence.ticket.kind == Operation.DELETE and outcome.uncertain:
            return None
        return _after_completion(state, evidence)
    if intent is not None:
        return SweepState("terminal", intent)
    return None


class LaunchSweep:
    """Cursors and one active reference into the existing loop-owned mutation ledger."""

    __slots__ = ("_active", "_local", "_pg", "_pg_next")

    def __init__(self) -> None:
        self._active: MutationAdmission | None = None
        self._local = 0
        self._pg: bytes | None = None
        self._pg_next = True

    def tick(self, authority: LaunchAuthority, spaces: Spaces) -> None:
        mutations = spaces._launch_mutations
        if mutations is None or mutations._stopped or self._active is not None:
            return
        discover, self._pg_next = self._pg_next, not self._pg_next
        if discover:
            ticket = mutations.reserve(spaces.launch_fits, background=True)
            if ticket is not None:
                self._start(ticket, _Work(authority, OpenResultCell(), cursor=self._pg), spaces)
            return
        slots = mutations._slots
        for _ in range(_VISITS):
            ticket = slots[self._local]
            self._local = (self._local + 1) % len(slots)
            if ticket is None or ticket.pending or ticket.driver is not None or ticket.ingress:
                continue
            previous = ticket.outcome
            if previous is None or ticket.issuer is None or ticket.target is None:
                continue
            state = previous.maintenance or _pending(previous)
            if state is None or state.phase == "parked":
                continue
            if not ticket.claim_detached(ticket.issuer, ticket.target, spaces.launch_fits):
                continue
            self._start(ticket, _Work(authority, OpenResultCell(), state), spaces)
            return

    def _start(self, ticket: MutationAdmission, work: _Work, spaces: Spaces) -> None:
        previous, ticket.outcome = ticket.outcome, None
        discover = work.state is None
        self._active = ticket
        ticket.submit(
            _run,
            work,
            work.result,
            not_started=previous,
            consumed=partial(self._consumed, work, spaces, discover),
        )

    def _consumed(self, work: _Work, spaces: Spaces, discover: bool, ticket: MutationAdmission, started: bool) -> None:
        self._active = None
        if not started:
            return
        if not discover:
            if ticket.outcome is not None and ticket.outcome.uncertain and work.state is not None:
                ticket.outcome = OpenOutcome("unavailable", maintenance=work.state.parked())
            return
        self._pg = work.next_cursor
        outcome = ticket.outcome
        target = outcome.maintenance.target if outcome is not None and outcome.maintenance is not None else None
        if target is not None:
            try:
                if ticket.claim_detached(
                    target.issuer, target.launch_id, partial(spaces.launch_background_fits, target.issuer)
                ):
                    return
            except Exception:
                # The attach rolled back. Discovery has no backend mutation to retain.
                ticket.outcome = None
                return
        ticket.outcome = None
