"""Loop-owned admission and actual STORE completion for short server controls."""

from __future__ import annotations

import asyncio
from array import array
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Protocol
from weakref import ReferenceType, ref

from latet.gateway.launch_keys import DIRECTORY_BYTES

if TYPE_CHECKING:
    from latet.server.launch_binding import Binding
    from latet.server.launch_sweep import SweepState
    from latet.server.launches import Completion, Publication, Receipt, Ticket

MUTATION_BYTES = 131072
MUTATIONS = 1024
LAUNCH_MUTATION = "latet.launch_mutation"


@dataclass(frozen=True, slots=True, repr=False)
class TerminalIntent:
    issuer: str
    launch_id: bytes
    generation: int | None = None
    operation: Ticket | None = None
    exit_confirmed: bool = False
    prior: Publication | Completion | None = None
    uncertain: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class OpenOutcome:
    status: Literal["opened", "capacity", "unavailable", "held", "recovered", "ended", "retired", "absent"]
    receipt: Receipt | None = None
    retained: Publication | Completion | TerminalIntent | None = None
    uncertain: bool = False
    completion: Completion | None = None
    binding: Binding | None = None
    maintenance: SweepState | None = None


class ResidentLaunch(Protocol):
    def stop(self) -> None: ...

    async def wait_closed(self) -> None: ...


@dataclass(slots=True, repr=False)
class OpenResultCell:
    outcome: OpenOutcome | None = None
    started: bool = False

    def take(self) -> OpenOutcome | None:
        outcome, self.outcome = self.outcome, None
        return outcome


@dataclass(slots=True, weakref_slot=True, repr=False)
class _Invocation:
    fn: Callable[[Any], None] | None
    argument: Any
    result: OpenResultCell | None
    notify: Callable[[ReferenceType[_Invocation]], None] | None = None
    marker: ReferenceType[_Invocation] | None = None

    def take(self) -> tuple[Callable[[Any], None], Any, OpenResultCell]:
        fn, argument, result = self.fn, self.argument, self.result
        self.fn = self.argument = self.result = None
        if fn is None or result is None:
            raise RuntimeError("launch invocation was already consumed")
        return fn, argument, result

    def __del__(self) -> None:
        # Weakref callbacks precede slot clearing on the pinned interpreter.
        # Clear explicitly, including when a queued invocation never started.
        self.fn = self.argument = self.result = None
        notify, marker = getattr(self, "notify", None), getattr(self, "marker", None)
        self.notify = self.marker = None
        if notify is not None and marker is not None:
            with suppress(Exception):
                notify(marker)


def _invoke(invocation: _Invocation) -> None:
    fn, argument, result = invocation.take()
    # A Future may retain an exception traceback, but must not retain this marker.
    del invocation
    result.started = True
    fn(argument)


def _uncertain(outcome: OpenOutcome | None, fallback: OpenOutcome | None) -> OpenOutcome:
    selected = outcome or fallback or OpenOutcome("unavailable")
    return replace(
        selected,
        uncertain=True,
        retained=selected.retained or (fallback.retained if fallback is not None else None),
        maintenance=selected.maintenance or (fallback.maintenance if fallback is not None else None),
    )


class LaunchMutations:
    __slots__ = ("_free", "_issuers", "_slots", "_stopped")

    def __init__(self) -> None:
        self._slots: list[MutationAdmission | None] = [None] * MUTATIONS
        self._free = array("H", reversed(range(MUTATIONS)))
        self._issuers: dict[str, int] = {}
        self._stopped = False

    @property
    def retained_bytes(self) -> int:
        return DIRECTORY_BYTES + (MUTATIONS - len(self._free)) * MUTATION_BYTES

    def issuer_bytes(self, issuer: str) -> int:
        return self._issuers.get(issuer, 0) * MUTATION_BYTES

    def reserve(self, fits: Callable[[], bool], *, background: bool = False) -> MutationAdmission | None:
        if self._stopped or not self._free:
            return None
        slot = self._free.pop()
        ticket = MutationAdmission(self, slot)
        self._slots[slot] = ticket
        if fits():
            ticket.ingress = not background
            return ticket
        ticket.end_ingress()
        return None

    def _attach(self, issuer: str) -> None:
        self._issuers[issuer] = self._issuers.get(issuer, 0) + 1

    def _detach(self, issuer: str) -> None:
        remaining = self._issuers[issuer] - 1
        if remaining:
            self._issuers[issuer] = remaining
        else:
            del self._issuers[issuer]
            if not self._issuers:
                self._issuers = {}

    def _release(self, ticket: MutationAdmission) -> None:
        if self._slots[ticket.slot] is not ticket:
            return
        self._slots[ticket.slot] = None
        self._free.append(ticket.slot)
        if ticket.issuer is not None:
            self._detach(ticket.issuer)
        ticket.issuer = ticket.target = ticket.outcome = None
        ticket.claimed = False

    def stop(self) -> None:
        self._stopped = True
        for ticket in self._slots:
            if ticket is not None and ticket.driver is not None:
                ticket.driver.stop()

    def stop_drivers(self, issuer: str, launch_id: bytes) -> None:
        for ticket in self._slots:
            if (
                ticket is not None
                and ticket.issuer == issuer
                and ticket.target == launch_id
                and ticket.driver is not None
            ):
                ticket.driver.stop()

    def require_drained(self) -> None:
        if any(
            ticket is not None and (ticket.ingress or ticket.pending or ticket.driver is not None)
            for ticket in self._slots
        ):
            raise RuntimeError("launch controls have not drained")

    async def drain(self) -> None:
        self.stop()
        async with asyncio.timeout(15):
            for ticket in self._slots:
                if ticket is not None and ticket.driver is not None:
                    await ticket.driver.wait_closed()
                if ticket is not None and ticket.pending:
                    await ticket.wait()
        self.require_drained()


class MutationAdmission:
    __slots__ = (
        "_fallback",
        "_on_consume",
        "_ready",
        "_result",
        "_tail",
        "actual",
        "claimed",
        "driver",
        "ingress",
        "issuer",
        "outcome",
        "owner",
        "slot",
        "target",
    )

    def __init__(self, owner: LaunchMutations, slot: int) -> None:
        self.owner, self.slot = owner, slot
        self.ingress = True
        self.issuer: str | None = None
        self.target: bytes | None = None
        self.claimed = False
        self.driver: ResidentLaunch | None = None
        self.actual: Future[None] | None = None
        self.outcome: OpenOutcome | None = None
        self._result: OpenResultCell | None = None
        self._on_consume: Callable[[MutationAdmission, bool], None] | None = None
        self._tail: ReferenceType[_Invocation] | None = None
        self._fallback: OpenOutcome | None = None
        self._ready = asyncio.Event()

    def attach(self, issuer: str, check: Callable[[int], str | None]) -> bool:
        if self.issuer is not None or not self.ingress:
            return False
        self.owner._attach(issuer)
        try:
            if check(0) is not None:
                return False
            self.issuer = issuer
            return True
        finally:
            if self.issuer is None:
                self.owner._detach(issuer)

    def identify(self, launch_id: bytes, *, draining: bool = False) -> bool:
        if (
            (self.owner._stopped and not draining)
            or (not self.ingress and self.driver is None)
            or self.issuer is None
            or self.claimed
            or (self.target is not None and self.target != launch_id)
        ):
            return False
        if any(
            ticket is not None and ticket.claimed and ticket.issuer == self.issuer and ticket.target == launch_id
            for ticket in self.owner._slots
        ):
            return False
        self.target = launch_id
        self.claimed = True
        return True

    def claim_detached(self, issuer: str, launch_id: bytes, fits: Callable[[], bool]) -> bool:
        owner = self.owner
        if (
            owner._stopped
            or owner._slots[self.slot] is not self
            or self.ingress
            or self.driver is not None
            or self.pending
            or self.issuer not in (None, issuer)
            or self.target not in (None, launch_id)
        ):
            return False
        if self.claimed:
            return self.issuer == issuer and self.target == launch_id
        if any(
            ticket is not None and ticket.claimed and ticket.issuer == issuer and ticket.target == launch_id
            for ticket in owner._slots
        ):
            return False
        if self.issuer is None:
            owner._attach(issuer)
            accepted = False
            try:
                accepted = fits()
            finally:
                if not accepted:
                    owner._detach(issuer)
            if not accepted:
                return False
            self.issuer = issuer
        self.target, self.claimed = launch_id, True
        return True

    def rearm(self) -> None:
        if self.pending or self.outcome is not None:
            raise RuntimeError("launch work has not been consumed")
        self._ready.clear()

    def detach_driver(self) -> None:
        self.driver = None
        self._release_if_settled()

    @property
    def pending(self) -> bool:
        return self._result is not None or self.actual is not None or self._tail is not None

    def submit(
        self,
        fn: Callable[[Any], None],
        argument: Any,
        result: OpenResultCell,
        *,
        not_started: OpenOutcome | None,
        consumed: Callable[[MutationAdmission, bool], None] | None = None,
    ) -> None:
        from latet.server.machines import submit_store

        self.rearm()
        loop = asyncio.get_running_loop()
        self._result, self._fallback, self._on_consume = result, not_started, consumed
        invocation = None
        try:
            invocation = _Invocation(fn, argument, result, partial(self._tail_done, loop))
            self._tail = invocation.marker = ref(invocation)
        except Exception:
            # Nothing was submitted. Settle after this frame drops its argument aliases.
            del invocation
            with suppress(RuntimeError):
                loop.call_soon(self._finish, context=Context())
            return
        try:
            # submit can raise after enqueueing. Only argument release proves its end.
            with suppress(Exception):
                self.actual = submit_store(_invoke, invocation)
            if self.actual is not None:
                # The independent argument-release signal still settles the actual work.
                with suppress(Exception):
                    self.actual.add_done_callback(partial(self._completed, loop))
        finally:
            del invocation

    def _completed(self, loop: asyncio.AbstractEventLoop, actual: Future[None]) -> None:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._consume, actual, context=Context())

    def _tail_done(self, loop: asyncio.AbstractEventLoop, marker: ReferenceType[_Invocation]) -> None:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._tail_released, marker, context=Context())

    def _tail_released(self, marker: ReferenceType[_Invocation]) -> None:
        if self._tail is not marker:
            return
        self._tail = None
        del marker
        self._finish()

    def _consume(self, actual: Future[None]) -> None:
        if self.actual is not actual:
            return
        del actual
        self._finish()

    def _finish(self) -> None:
        if self._tail is not None or self._result is None or (self.actual is not None and not self.actual.done()):
            return
        result, self._result = self._result, None
        fallback, self._fallback = self._fallback, None
        consumed, self._on_consume = self._on_consume, None
        self.actual = None
        started = result.started
        authoritative = result.take()
        if not started:
            self.outcome = fallback if authoritative is None else _uncertain(authoritative, fallback)
        else:
            self.outcome = authoritative if authoritative is not None else _uncertain(None, fallback)
        authoritative = self.outcome
        try:
            if consumed is not None:
                consumed(self, started)
        except Exception:
            # Preserve the actual outcome even if the callback cleared the field first.
            self.outcome = _uncertain(authoritative, fallback)
        del result, fallback, consumed, authoritative
        self._ready.set()
        self._release_if_settled()

    async def wait(self) -> OpenOutcome:
        await self._ready.wait()
        return self.outcome or OpenOutcome("unavailable")

    def end_ingress(self) -> None:
        self.ingress = False
        self._release_if_settled()

    def _release_if_settled(self) -> None:
        if self.ingress or self.pending or self.driver is not None:
            return
        if self.outcome is not None and (
            self.outcome.uncertain or self.outcome.retained is not None or self.outcome.maintenance is not None
        ):
            return
        self.owner._release(self)
