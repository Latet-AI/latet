"""Protected launch OPEN selected by server Space construction."""

from __future__ import annotations

import base64
import logging
import re
import secrets
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from sys import getsizeof
from typing import TYPE_CHECKING, Any, Literal

from latet.gateway.launch_keys import LaunchRefused
from latet.projects import project_id
from latet.server.launch_binding import (
    Binding,
    BindingCodec,
    RemoteResult,
    _canonical_depth,
    _relative,
    _text,
    _windows_key,
    _windows_relative,
)
from latet.server.launch_mutations import LAUNCH_MUTATION, MutationAdmission, OpenOutcome, OpenResultCell
from latet.server.launch_records import LaunchUnavailable
from latet.server.launches import LaunchAuthority, LaunchCapacity, ReservationUncertain

if TYPE_CHECKING:
    from collections.abc import Sequence
    from weakref import ReferenceType

    from starlette.types import Scope

    from latet.config import ProjectEntry
    from latet.runtime import Runtime

log = logging.getLogger(__name__)

_DRIVE = re.compile(r"[A-Za-z]:")
_OPEN_FIELDS = frozenset(("folder", "root", "flavor"))
_MAX_PROJECTS = 200


def _flavor(path: str) -> Literal["windows", "posix"]:
    if type(path) is str:
        if path.startswith("\\\\") or _DRIVE.match(path):
            return "windows"
        if path.startswith("/") and not path.startswith("//"):
            return "posix"
    raise LaunchUnavailable()


def _binding(data: dict[str, Any], entries: Sequence[ProjectEntry], *, stored_label: bool = False) -> Binding:
    folder, root, flavor = data["folder"], data["root"], data["flavor"]
    if type(flavor) is not str or flavor not in ("windows", "posix") or len(entries) > _MAX_PROJECTS:
        raise LaunchRefused("launch-invalid")
    try:
        _text(folder, flavor)
        child = _windows_key(folder) if flavor == "windows" else None
        if root is not None:
            _text(root, flavor)
            relative = (
                _windows_relative(child, _windows_key(root)) if child is not None else _relative(folder, root, "/")
            )
            if not relative:
                raise LaunchUnavailable()

        # Fingerprints are bounded scalars. Recheck earlier original paths on collision.
        seen = array("q")
        selected, depth = None, 0
        compared: list[int] = []  # the entries in a form a launch compares; any other is left out, never fatal (I-7)
        for index, entry in enumerate(entries):
            path = entry.path
            try:
                kind = _flavor(path)
                length = _canonical_depth(path, kind)
            except LaunchUnavailable:
                del path
                continue
            compared.append(index)
            normalized = _windows_key(path) if kind == "windows" else path
            del path
            fingerprint = hash((kind, normalized))
            if fingerprint in seen:
                # The child key is only needed for the later ancestry comparison.
                child = None
                for previous in compared[:-1]:
                    other = entries[previous].path
                    if _flavor(other) == kind:
                        _text(other, kind)
                        if (_windows_key(other) if kind == "windows" else other) == normalized:
                            raise LaunchUnavailable()
                    del other
            else:
                seen.append(fingerprint)
            if kind == flavor:
                if isinstance(normalized, bytes):
                    if child is None:
                        child = _windows_key(folder)
                    relative = _windows_relative(child, normalized)
                else:
                    relative = _relative(folder, normalized, "/")
                if relative:
                    if length == depth:
                        raise LaunchUnavailable()
                    if length > depth:
                        selected, depth = index, length
            del normalized

        if (left_out := len(entries) - len(compared)) > 0:  # said by count alone: a folder's name is never logged
            log.warning(
                "%d project folder%s of the settings %s not in a form a launch compares: left out of this launch's "
                "projects",
                left_out,
                "" if left_out == 1 else "s",
                "is" if left_out == 1 else "are",
            )
        target = entries[selected].path if selected is not None else root
        if target is None:
            raise LaunchRefused("launch-unregistered")
        source: Literal["server", "client"] = "server" if selected is not None else "client"
        del child
        label = "0" * 12 if stored_label else project_id(target)
        del seen
        return Binding(folder, target, label, source, flavor)
    except (LaunchUnavailable, UnicodeError):
        raise LaunchRefused("launch-invalid") from None


@dataclass(frozen=True, slots=True, repr=False)
class _OpenPayload:
    authority: LaunchAuthority
    codec: BindingCodec
    issuer: str
    locator: bytes
    launch_id: bytes
    verifier: bytes
    binding: Binding


@dataclass(slots=True, repr=False)
class _OpenHandoff:
    payload: _OpenPayload | None
    result: OpenResultCell


def _run_open(handoff: _OpenHandoff) -> None:
    handoff.result.outcome = _publish_open(handoff)


def _publish_open(handoff: _OpenHandoff) -> OpenOutcome:
    payload, handoff.payload = handoff.payload, None
    if payload is None:
        return OpenOutcome("unavailable", uncertain=True)
    publication = None
    try:
        authority = payload.authority
        publication = authority.reserve_open(
            payload.issuer, payload.locator, payload.launch_id, payload.binding, payload.verifier, payload.codec
        )
        del payload
        completion = authority.submit(publication)
    except LaunchCapacity:
        return OpenOutcome("capacity")
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
        return OpenOutcome("opened", completion.receipt)
    return OpenOutcome("unavailable", retained=completion)


class ProtectedLaunchOpen:
    __slots__ = ("_authority", "_codec", "_issuer", "_runtime")
    fields = _OPEN_FIELDS

    def __init__(
        self,
        authority: LaunchAuthority,
        codec: BindingCodec,
        issuer: str,
        runtime: ReferenceType[Runtime],
    ) -> None:
        self._authority, self._codec, self._issuer, self._runtime = authority, codec, issuer, runtime

    @property
    def retained_bytes(self) -> int:
        return getsizeof(self) + getsizeof(self._runtime) + self._codec.retained_bytes

    async def open(self, scope: Scope, read: Callable[[frozenset[str]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        data = await read(self.fields)
        ticket = scope.get(LAUNCH_MUTATION)
        if not isinstance(ticket, MutationAdmission) or ticket.issuer != self._issuer:
            raise LaunchRefused("launch-unavailable")
        runtime = self._runtime()
        if runtime is None:
            raise LaunchRefused("launch-unavailable")
        binding = _binding(data, runtime.config.projects)
        del runtime
        data.clear()
        credential = b"latet_lk_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=")
        locator = self._authority.locator(credential)
        launch_id = secrets.token_bytes(16)
        if not ticket.identify(launch_id):
            raise LaunchRefused("launch-unavailable")
        result = OpenResultCell()
        handoff = _OpenHandoff(
            _OpenPayload(
                self._authority,
                self._codec,
                self._issuer,
                locator,
                launch_id,
                self._codec.verifier(credential),
                binding,
            ),
            result,
        )
        ticket.submit(_run_open, handoff, result, not_started=OpenOutcome("unavailable"))
        outcome = await ticket.wait()
        if outcome.status == "capacity":
            return {"key": None, "launch_id": None, "reason": "launch-capacity"}
        if outcome.status != "opened" or outcome.receipt is None:
            raise LaunchRefused("launch-unavailable")
        address = outcome.receipt.address
        return {
            "key": credential.decode("ascii"),
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
