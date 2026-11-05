"""Authenticated protected controls and the paid server hold driver."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from latet.gateway.launch_keys import LaunchRefused
from latet.server.launch_binding import Binding, BindingCodec, RemoteResult, canonical_credential
from latet.server.launch_controls import LAUNCH_HOLD_TICKET, LaunchHoldTicket
from latet.server.launch_mutations import (
    LAUNCH_MUTATION,
    MutationAdmission,
    OpenOutcome,
    OpenResultCell,
    TerminalIntent,
)
from latet.server.launch_open import ProtectedLaunchOpen, _binding
from latet.server.launch_records import LaunchUnavailable, uint
from latet.server.launches import Completion, ReservationUncertain, TerminalContended, _TransactionUncertain

if TYPE_CHECKING:
    from starlette.types import Scope

    from latet.server.launches import LaunchAuthority

_HEX_ID = re.compile(r"[0-9a-f]{32}")
_REFRESH_SECONDS = 20


def _id(value: object) -> bytes:
    if type(value) is not str or _HEX_ID.fullmatch(value) is None:
        raise LaunchRefused("launch-invalid")
    return bytes.fromhex(value)


@dataclass(frozen=True, slots=True, repr=False)
class _Control:
    kind: Literal["hold", "refresh", "recover", "terminal"]
    authority: LaunchAuthority
    issuer: str
    launch_id: bytes
    codec: BindingCodec | None = None
    generation: int = 0
    held: Completion | None = None
    locator: bytes = b""
    epoch: bytes = b""
    verifier: bytes = b""
    binding: Binding | None = None
    intent: TerminalIntent | None = None


@dataclass(slots=True, repr=False)
class _ControlHandoff:
    work: _Control | None
    result: OpenResultCell


def _terminal(authority: LaunchAuthority, intent: TerminalIntent) -> OpenOutcome:
    pending = intent
    confirmed = False
    try:
        target = authority.terminal_control(
            intent.issuer,
            intent.launch_id,
            generation=intent.generation,
            operation=intent.operation,
            exit_confirmed=intent.exit_confirmed,
        )
        if target is None:
            if intent.prior is not None or intent.uncertain:
                return OpenOutcome("unavailable", retained=intent)
            return OpenOutcome("absent")
        confirmed = True
        if target.has_operation or intent.prior is not None or intent.uncertain:
            return OpenOutcome("ended", retained=intent)
        if target.binding_reserved:
            publication = authority.reserve_cleanup(intent.issuer, target.locator)
            if publication is None:
                return OpenOutcome("ended", retained=intent)
            pending = replace(intent, prior=publication)
            completed = authority.submit(publication)
            if not completed.settled or completed.wire.cleanup_pending is not None:
                return OpenOutcome("ended", retained=replace(intent, prior=completed))
            if completed.wire.remote != RemoteResult.DELETE:
                return OpenOutcome("ended", retained=intent)
            pending = replace(intent, prior=completed)
        if intent.exit_confirmed:
            if authority.retire(intent.issuer, target.locator, target.generation):
                return OpenOutcome("retired")
            return OpenOutcome("ended", retained=intent)
        return OpenOutcome("ended")
    except ReservationUncertain as error:
        pending = replace(intent, prior=error.publication)
    except TerminalContended:
        return OpenOutcome("unavailable", retained=intent)
    except Exception as error:
        if (
            isinstance(error, LaunchUnavailable)
            and not isinstance(error, _TransactionUncertain)
            and not confirmed
            and intent.prior is None
            and not intent.uncertain
        ):
            return OpenOutcome("unavailable")
        return OpenOutcome(
            "ended" if confirmed and not intent.exit_confirmed else "unavailable", retained=pending, uncertain=True
        )
    return OpenOutcome("ended" if confirmed and not intent.exit_confirmed else "unavailable", retained=pending)


def _control(work: _Control) -> OpenOutcome:
    authority = work.authority
    if work.intent is not None:
        return _terminal(authority, work.intent)
    publication = None
    binding = None
    codec = work.codec
    try:
        if codec is None:
            return OpenOutcome("unavailable")
        if work.kind == "hold":
            publication = authority.reserve_hold(work.issuer, work.launch_id, work.generation, codec)
        elif work.kind == "refresh" and work.held is not None:
            publication = authority.reserve_refresh(work.held, codec)
        elif work.kind == "recover" and work.binding is not None:
            publication, binding = authority.recover_current(
                work.issuer, work.locator, work.launch_id, work.epoch, 0, work.binding, work.verifier, codec
            )
        else:
            return OpenOutcome("unavailable")
        completion = authority.submit(publication)
    except ReservationUncertain as error:
        return OpenOutcome("unavailable", retained=error.publication)
    except LaunchUnavailable:
        return OpenOutcome("unavailable", retained=publication)
    except Exception:
        return OpenOutcome("unavailable", retained=publication, uncertain=True)
    if (
        completion.settled
        and completion.receipt is not None
        and completion.wire.remote == RemoteResult.SET
        and completion.wire.cleanup_pending is None
    ):
        return OpenOutcome(
            "recovered" if work.kind == "recover" else "held",
            completion.receipt,
            completion=completion,
            binding=binding,
        )
    return OpenOutcome("unavailable", retained=completion)


def _run_control(handoff: _ControlHandoff) -> None:
    work, handoff.work = handoff.work, None
    handoff.result.outcome = _control(work) if work is not None else OpenOutcome("unavailable", uncertain=True)


def _start_control(ticket: MutationAdmission, work: _Control) -> None:
    result = OpenResultCell()
    handoff = _ControlHandoff(work, result)
    ticket.submit(
        _run_control,
        handoff,
        result,
        not_started=OpenOutcome("unavailable", retained=work.intent),
    )


async def _result(ticket: MutationAdmission) -> OpenOutcome:
    if not ticket.pending and ticket.outcome is not None:
        return ticket.outcome
    return await ticket.wait()


class _ProtectedHold:
    __slots__ = (
        "_authority",
        "_closed",
        "_codec",
        "_held",
        "_issuer",
        "_launch_id",
        "_next_refresh",
        "_physical",
        "_stop",
        "_ticket",
    )

    def __init__(
        self,
        authority: LaunchAuthority,
        codec: BindingCodec,
        issuer: str,
        launch_id: bytes,
        ticket: MutationAdmission,
        physical: LaunchHoldTicket,
    ) -> None:
        self._authority, self._codec, self._issuer, self._launch_id = authority, codec, issuer, launch_id
        self._ticket, self._physical = ticket, physical
        self._stop, self._closed = asyncio.Event(), asyncio.Event()
        self._held: Completion | None = None
        self._next_refresh = 0.0

    @property
    def generation(self) -> int:
        receipt = self._held.receipt if self._held is not None else None
        if receipt is None:
            raise LaunchRefused("launch-unavailable")
        return receipt.address.generation

    def acknowledge(self) -> int:
        if self._stop.is_set():
            raise LaunchRefused("launch-unavailable")
        return self.generation

    async def ready(self, generation: int) -> None:
        _start_control(
            self._ticket, _Control("hold", self._authority, self._issuer, self._launch_id, self._codec, generation)
        )
        outcome = await _result(self._ticket)
        if not self._adopt_result(outcome) or self._stop.is_set():
            raise LaunchRefused("launch-unavailable")

    def _adopt_result(self, outcome: OpenOutcome) -> bool:
        if outcome.status != "held" or outcome.completion is None:
            return False
        self._held = outcome.completion
        self._next_refresh = asyncio.get_running_loop().time() + _REFRESH_SECONDS
        self._ticket.outcome = None
        self._ticket.claimed = False
        return True

    async def wait(self) -> None:
        while not self._stop.is_set():
            try:
                async with asyncio.timeout_at(self._next_refresh):
                    await self._stop.wait()
            except TimeoutError:
                pass
            if self._stop.is_set() or not self._ticket.identify(self._launch_id):
                self.stop()
                return
            _start_control(
                self._ticket,
                _Control("refresh", self._authority, self._issuer, self._launch_id, self._codec, held=self._held),
            )
            if not self._adopt_result(await _result(self._ticket)):
                self.stop()
                return

    def stop(self) -> None:
        self._stop.set()

    async def _settled(self) -> OpenOutcome | None:
        while self._ticket.pending:
            try:
                await self._ticket.wait()
            except asyncio.CancelledError:
                continue
        return self._ticket.outcome

    async def drain(self) -> None:
        outcome = await self._settled()
        if outcome is not None and self._adopt_result(outcome):
            outcome = None
        prior = outcome.retained if outcome is not None else None
        if isinstance(prior, TerminalIntent):
            return
        uncertain = outcome is not None and outcome.uncertain
        operation = prior.ticket if prior is not None else None
        if self._held is None and operation is None and not uncertain:
            self._ticket.outcome = None
            return
        generation = self.generation if self._held is not None and operation is None else None
        intent = TerminalIntent(self._issuer, self._launch_id, generation, operation, prior=prior, uncertain=uncertain)
        if (generation is None and operation is None) or (
            not self._ticket.claimed and not self._ticket.identify(self._launch_id, draining=True)
        ):
            self._ticket.outcome = OpenOutcome("unavailable", retained=intent)
            return
        self._ticket.outcome = None
        _start_control(
            self._ticket, _Control("terminal", self._authority, self._issuer, self._launch_id, intent=intent)
        )
        await self._settled()

    def close(self) -> None:
        self._held = None
        self._physical.close()
        self._ticket.detach_driver()
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()


class ProtectedLaunchControls(ProtectedLaunchOpen):
    __slots__ = ()
    recovery_fields = frozenset(("folder", "root", "flavor", "key", "launch_id", "epoch", "retirement"))

    def _admission(self, scope: Scope, launch_id: bytes) -> MutationAdmission:
        ticket = scope.get(LAUNCH_MUTATION)
        if not isinstance(ticket, MutationAdmission) or ticket.issuer != self._issuer or not ticket.identify(launch_id):
            raise LaunchRefused("launch-unavailable")
        return ticket

    def hold(self, scope: Scope, launch_id: str, generation: int) -> _ProtectedHold:
        target = _id(launch_id)
        if not uint(generation):
            raise LaunchRefused("launch-invalid")
        ticket = self._admission(scope, target)
        physical = scope.get(LAUNCH_HOLD_TICKET)
        if not isinstance(physical, LaunchHoldTicket):
            raise LaunchRefused("launch-unavailable")
        lease = _ProtectedHold(self._authority, self._codec, self._issuer, target, ticket, physical)
        if not physical.adopt():
            raise LaunchRefused("launch-unavailable")
        ticket.driver = lease
        return lease

    async def recover(
        self, scope: Scope, read: Callable[[frozenset[str]], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        data = await read(self.recovery_fields)
        launch_id, epoch = _id(data["launch_id"]), _id(data["epoch"])
        if type(data["retirement"]) is not int or data["retirement"] != 0:
            raise LaunchRefused("launch-invalid")
        ticket = self._admission(scope, launch_id)
        credential = data.pop("key")
        try:
            if type(credential) is not str or len(credential) != 52:
                raise LaunchUnavailable()
            key = canonical_credential(credential.encode("ascii"))
            locator, verifier = self._authority.locator(key), self._codec.verifier(key)
        except (LaunchUnavailable, UnicodeError):
            raise LaunchRefused("launch-invalid") from None
        del credential, key
        runtime = self._runtime()
        if runtime is None:
            raise LaunchRefused("launch-unavailable")
        selected = _binding(data, runtime.config.projects, stored_label=True)
        del runtime
        data.clear()
        _start_control(
            ticket,
            _Control(
                "recover",
                self._authority,
                self._issuer,
                launch_id,
                self._codec,
                locator=locator,
                epoch=epoch,
                verifier=verifier,
                binding=selected,
            ),
        )
        del selected
        outcome = await _result(ticket)
        if outcome.status != "recovered" or outcome.receipt is None or outcome.binding is None:
            raise LaunchRefused("launch-unavailable")
        address, binding = outcome.receipt.address, outcome.binding
        return {
            "launch_id": address.launch_id.hex(),
            "generation": address.generation,
            "binding": {
                "folder": binding.folder,
                "root": binding.root,
                "flavor": binding.flavor,
                "source": binding.source,
                "project": binding.project,
            },
        }

    async def end(self, scope: Scope, launch_id: str, *, exit_epoch: str | None = None) -> bool:
        target = _id(launch_id)
        child_exit = exit_epoch is not None
        if child_exit and not self._authority.accepts_epoch(_id(exit_epoch)):
            raise LaunchRefused("launch-unavailable")
        ticket = self._admission(scope, target)
        ticket.owner.stop_drivers(self._issuer, target)
        intent = TerminalIntent(self._issuer, target, exit_confirmed=child_exit)
        _start_control(ticket, _Control("terminal", self._authority, self._issuer, target, intent=intent))
        outcome = await _result(ticket)
        if outcome.status == "unavailable":
            raise LaunchRefused("launch-unavailable")
        return outcome.status == ("retired" if child_exit else "ended")
