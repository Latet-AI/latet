"""The floor of the settings: what a company sets that nobody goes below (Enterprise, E2), as the Community code sees
it. ``Edition.floor`` answers one, or None; the Community Edition never has one."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from latet.config import Config


class Floor(Protocol):
    def below(self, config: Config) -> list[str]: ...  # why ``config`` goes below it: empty when it does not
    def raise_to(self, config: Config) -> Config: ...  # ``config`` raised to it, never lowered

    # Whether ``raise_to`` carries terms to the build as locked (``Config.locked_terms``).
    @property
    def locks_terms(self) -> bool: ...

    # Whether ``raise_to`` has the engine cut every span of yours left in clear (your terms' and your identity's) around
    # what is masked without them (``Config.carves_terms``).
    @property
    def carves_terms(self) -> bool: ...

    # Whether the company decides on request exceptions: then one is approved by its security team alone (central
    # approvals), or by the administration that keeps an API key's settings, never by whoever asks for it.
    @property
    def decides_exceptions(self) -> bool: ...


# Why an approval of a request exception is refused where the company decides on them (``Floor.decides_exceptions``):
# by its destination alone, as ``protection.loosened`` names one.
COMPANY_DECIDES = "a request exception for {destination}: your company's security team decides on it"


def company_decides(floor: Floor | None) -> bool:
    """Whether ``floor`` has the company decide on request exceptions (no floor: each person decides on theirs)."""
    return floor is not None and floor.decides_exceptions


class BelowFloor(Exception):
    """A change refused because it goes below what the company set. Not held for approval: the person cannot
    approve it."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(refusal(reasons))


# What the dashboard and a server's administration answer (503) while the floor cannot be said: fixed words, never the
# error's own.
FLOOR_UNAVAILABLE = "the company's policy cannot be read on this server now; try again"


class FloorUnavailable(RuntimeError):
    """The company's floor cannot be said now: on a server, a policy is stored that this instance cannot read, or it
    has never read the license (Latet Enterprise: ``latet.ee.policy_server.PolicyUnavailable``). Nothing is built or
    changed below it: a space is refused, a change is refused (``FLOOR_UNAVAILABLE``, 503), and both are tried again."""


# What they answer (503) while the person's account on the server cannot be read for the name they are masked under
# (Latet Enterprise: ``Edition.provider_name``): fixed words, never the error's own.
ACCOUNT_UNAVAILABLE = "your account on this server cannot be read now: try again"


class AccountUnavailable(FloorUnavailable):
    """The person's account on the server cannot be read now, for the name their company's identity provider gives
    them (Latet Enterprise: ``Edition.provider_name``): as with the floor, nothing is built or changed for them and both
    are tried again, the refusal naming the account (``ACCOUNT_UNAVAILABLE``), never the policy."""


# What they answer (503) while a server cannot read the terms its company syncs (Latet Enterprise): no space is built
# without them, and both are tried again.
TERMS_UNAVAILABLE = "your company's synced terms cannot be read on this server now; try again"


class TermsUnavailable(FloorUnavailable):
    """The terms the company syncs cannot be said now (Latet Enterprise, on a server: never read on this instance, or
    the license that switches them on never read): as with the floor, nothing is built without them and both are tried
    again, the refusal naming them (``TERMS_UNAVAILABLE``)."""


def unavailable(exc: FloorUnavailable) -> str:
    """The fixed words of the 503 that answers ``exc``: the synced terms', the person's account's, or the company's
    policy's."""
    if isinstance(exc, TermsUnavailable):
        return TERMS_UNAVAILABLE
    return ACCOUNT_UNAVAILABLE if isinstance(exc, AccountUnavailable) else FLOOR_UNAVAILABLE


def refusal(reasons: list[str]) -> str:
    return "Set by your company, not changed: " + "; ".join(reasons)


# The reason a change of a person's own name is refused while their name comes from the company's identity provider
# (Latet Enterprise: ``Edition.provider_name``): it names the setting, never a value.
PROVIDER_NAME = "identity.full_name: your name comes from your company's identity provider"


# A change is weighed folder by folder, the settings raised twice for each folder whose own entry it lowers: one that
# lowers more folders than this at once is refused in fixed words rather than weighed for minutes in a person's worker.
FOLDERS_WEIGHED = 20
TOO_MANY_FOLDERS = (
    f"a change that lowers the settings of more than {FOLDERS_WEIGHED} folders at once is not weighed against your "
    "company's policy: change fewer folders at a time"
)


def fresh_reasons(floor: Floor | None, before: Config, after: Config) -> list[str]:
    """Why the change from ``before`` to ``after`` takes them below the floor: the reasons ``after`` is below it whose
    setting the change itself lowers (``protection.loosened``) and that ``before`` was not below it for in those very
    words. Settings below it since before it came (or a config.yaml edited by hand) do not stop a change to something
    else, nor one that comes closer to it or protects more (a reason's count or its "from ... to" moves with any
    change, its setting does not), nor one that lowers an item of your own where the floor's reason stays as it was
    (your own term taken out while the company's are not among yours).

    Weighed scope by scope, never by reading folders' names back out of the words: the settings themselves (with no
    folder), then each folder whose own entry the change touches, alone (its reasons then all of that one folder). A
    change that lowers more than ``FOLDERS_WEIGHED`` folders is refused (``TOO_MANY_FOLDERS``), none of them weighed."""
    if floor is None:
        return []
    from latet.projects import norm_path

    was = {norm_path(e.path): e for e in before.projects}
    now = {norm_path(e.path): e for e in after.projects}
    scopes: list[tuple[Config, Config, str]] = [
        (before.model_copy(update={"projects": []}), after.model_copy(update={"projects": []}), "")
    ]
    for path in sorted({*was, *now}):
        if was.get(path) == now.get(path):
            continue
        name = Path(path.rstrip("/\\")).name or path  # as ``protection`` names a folder
        old, new = was.get(path), now.get(path)
        alone_before = before.model_copy(update={"projects": [old] if old is not None else []})
        alone_after = after.model_copy(update={"projects": [new] if new is not None else []})
        scopes.append((alone_before, alone_after, f"project {name}: "))
    lowering = [(scope, lowered) for scope in scopes if (lowered := _lowered_in(*scope))]
    if sum(1 for (_, _, scope), _ in lowering if scope) > FOLDERS_WEIGHED:
        return [TOO_MANY_FOLDERS]
    return [reason for (old, new, scope), lowered in lowering for reason in _fresh_in(floor, old, new, scope, lowered)]


def _lowered_in(before: Config, after: Config, scope: str) -> set[str]:
    """The settings the change from ``before`` to ``after`` lowers in one scope (``protection.loosened``'s reasons that
    start with ``scope``, by their setting): no raise is needed where it lowers none."""
    from latet.protection import loosened

    return {_name(reason, scope) for reason in loosened(before, after) if reason.startswith(scope)}


def _fresh_in(floor: Floor, before: Config, after: Config, scope: str, lowered: set[str]) -> list[str]:
    """``fresh_reasons`` in one scope: the reasons that start with ``scope`` (``""``: those of no folder, as ``before``
    and ``after`` then hold none), of the settings the change lowers there (``lowered``)."""
    already = set(floor.below(before))
    return [
        reason
        for reason in floor.below(after)
        if reason.startswith(scope) and reason not in already and _name(reason, scope) in lowered
    ]


def _name(reason: str, scope: str) -> str:
    """A reason's setting, the words before ": " once its scope is taken off (``profile``, ``values never to mask``,
    ``policy for EMAIL``, ``Latet turned off``)."""
    return reason.removeprefix(scope).partition(": ")[0]


def raised_to(floor: Floor | None, config: Config) -> Config:
    """``config`` raised to ``floor`` (``config`` itself without one), as an engine is built from it: the company's
    terms carried to the build and every span of yours left in clear cut where it says so (``carves_terms``), or
    MaskingError (nothing is built without them: a copy dumped and read again on the way drops them,
    ``Config.locked_terms``, ``Config.carves_terms``)."""
    if floor is None:
        return config
    effective = floor.raise_to(config)
    if floor.locks_terms and not effective.locked_terms:
        from latet.engine.vault import MaskingError

        raise MaskingError("the terms your company locks are not in the settings in use: nothing is built without them")
    if floor.carves_terms and not effective.carves_terms:
        from latet.engine.vault import MaskingError

        raise MaskingError("your terms are not cut around what your company masks: nothing is built without it")
    return effective
