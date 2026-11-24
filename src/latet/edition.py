"""The edition of Latet that runs: the Community Edition, or the Enterprise Edition when its code is there.

The Community code reaches the Enterprise code at a few hooks only, the methods of ``Edition``: the floor of the
settings and a watch on it (E2), the name a person linked to the company's identity provider is masked under (E3),
where a change that lowers protection and a Live Reviewer batch go, and the company's record of them (E5), the events
of what happens (E4), the terms synced from the company's systems (E6), the rules a server's administrators set for
everyone (``shared_rules``, which ``server.shared.in_force`` lays over the shared settings, once validated), the
Enterprise routes of the dashboard and of the server, what /me says of the edition, and the lines of `latet server
status`. The Community Edition's hooks do nothing, so Latet behaves as without them (people share rules by
hand there: a rules file, a project's latet.yaml); ``latet.ee`` gives the Enterprise Edition's, which act only as far
as its license goes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from latet.events import HIDDEN_PROJECT

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from latet.api.rest import Dashboard
    from latet.config import Config
    from latet.doctor.checks import Check
    from latet.engine.detectors.base import Detector
    from latet.floor import Floor
    from latet.gateway.approvals import Central
    from latet.server.admin import AdminGate
    from latet.server.app import Server
    from latet.state import IdentityRef


@dataclass(frozen=True)
class EditionContext:
    """Where an edition keeps what it keeps, and its clock: a computer's folders, or the server it runs on (``server``:
    then everything is in the server's PostgreSQL, and the folders are not read). ``one_shot``: the server of a command
    that opens it, does one thing and closes it (`latet server init`), which serves nothing: its edition starts no work
    that would outlive it (Latet Enterprise's term sync and its event jobs)."""

    data: Path
    config_dir: Path
    server: Server | None
    clock: Callable[[], float]
    one_shot: bool = False


class Edition(Protocol):
    name: Literal["community", "enterprise"]

    def floor(self, identity: IdentityRef | None) -> Floor | None: ...  # E2: the settings nobody goes below
    def watch(self, changed: Callable[[], None]) -> Callable[[], None]: ...  # E2: a new floor applies at once
    def provider_name(self, identity: IdentityRef | None) -> str | None:
        """E3: the name ``identity`` is masked under in place of their own (``IdentityConfig.full_name``) wherever the
        settings in use leave names in clear (``runtime.named_in``), which they cannot change: the one the company's
        identity provider gives, where their account is linked to it and the company's floor lets names through as
        only its own exceptions (`exceptions: company`). None: their own. Raises ``FloorUnavailable`` when it cannot be
        said (nothing is then built or changed)."""
        ...

    def changes_go_to(self, identity: IdentityRef | None) -> Literal["self", "security", "controllers"]: ...  # E5
    def live_review_goes_to(self, identity: IdentityRef | None) -> Literal["self", "security"]: ...  # E5
    def central(self) -> Central | None: ...  # E5: the company's record of approvals, on a server
    def keeps_events(self) -> bool: ...  # E4
    def record(self, event: Mapping[str, Any]) -> None: ...  # E4 (latet.events.make_event)
    def hidden_project(self, name: str) -> str:
        """E4: what an event names a project whose ``name`` holds a term its space masks: the same for every person
        and folder of that name, never the name nor its folder's path (``latet.events.HIDDEN_SHAPE``). It may read the
        events' key, and raise: the runtime then sends ``HIDDEN_PROJECT``."""
        ...

    def gaps(self) -> dict[str, Any] | None: ...  # E4: the SIEM's gap markers against the log's entries (log verify)
    def synced_terms(self, identity: IdentityRef | None) -> Detector | None:
        """E6: one detector for every space of an instance, which changes its terms in place (``LiveTerms``); None:
        none (the Community Edition). Raises ``FloorUnavailable`` when they cannot be said (nothing is then built)."""
        ...

    def shared_rules(self, stored: Config) -> Mapping[str, Any]:
        """The rules for everyone in force (2026-10-20), read from the shared settings as ``stored``: rule keys alone
        (``server.shared.RULE_KEYS``), which ``server.shared.in_force`` validates before it lays them over the shared
        settings. Raises when it cannot say its rules: never ``{}`` for "unknown", which would put none in force."""
        ...

    def checks(self) -> list[Check]: ...  # the lines of `latet server status`
    def dashboard_routes(self, dashboard: Dashboard) -> list[BaseRoute]: ...  # /_latet/api/v1/ee/...
    def server_routes(self, server: Server) -> list[BaseRoute]: ...  # a server's routes outside its administration
    def admin_routes(self, server: Server, gate: AdminGate) -> list[BaseRoute]:
        """The edition's routes of the server's administration (/_latet/server/v1/admin/ee/...), each behind ``gate``:
        the administration's own (``latet.server.admin.AdminGate``), never a gate of their own."""
        ...

    def me(self, identity: IdentityRef | None, acting: str | None = None) -> dict[str, Any]:
        """/me's "edition". ``acting``: in an API key's space, the person working there (``scope["latet.acting"]``),
        whom E5 never names as deciding on what they make there."""
        ...

    def permanent_exceptions(self) -> bool: ...

    def named_by_id(self) -> frozenset[str]:
        """The people whose name and e-mail someone other than an administrator sets (Enterprise: SCIM, R2), whom
        `latet server` names by their id alone: a leaked SCIM token could move those values to an impostor."""
        ...

    def close(self) -> None: ...


def _stop() -> None:
    return None


class Community:
    """The Community Edition: every hook does nothing."""

    name: Literal["community", "enterprise"] = "community"

    def floor(self, identity: IdentityRef | None) -> Floor | None:
        return None

    def watch(self, changed: Callable[[], None]) -> Callable[[], None]:
        return _stop  # it has no floor: nothing to watch

    def provider_name(self, identity: IdentityRef | None) -> str | None:
        return None  # nobody is named by a provider: a person's own name, as they set it

    def changes_go_to(self, identity: IdentityRef | None) -> Literal["self", "security", "controllers"]:
        return "self"

    def live_review_goes_to(self, identity: IdentityRef | None) -> Literal["self", "security"]:
        return "self"

    def central(self) -> Central | None:
        return None  # nobody else decides: everything waits for whoever's settings they are

    def keeps_events(self) -> bool:
        return False

    def record(self, event: Mapping[str, Any]) -> None:
        return None

    def hidden_project(self, name: str) -> str:
        return HIDDEN_PROJECT  # it keeps no events, and holds no key to make a name of its own

    def gaps(self) -> dict[str, Any] | None:
        return None  # no SIEM: `latet server log verify` answers `gaps: null`

    def synced_terms(self, identity: IdentityRef | None) -> Detector | None:
        return None

    def shared_rules(self, stored: Config) -> Mapping[str, Any]:
        """The rules for everyone in force, read from the shared settings as stored: none."""
        return {}

    def checks(self) -> list[Check]:
        return []

    def dashboard_routes(self, dashboard: Dashboard) -> list[BaseRoute]:
        return []

    def server_routes(self, server: Server) -> list[BaseRoute]:
        return []

    def admin_routes(self, server: Server, gate: AdminGate) -> list[BaseRoute]:
        return []

    def me(self, identity: IdentityRef | None, acting: str | None = None) -> dict[str, Any]:
        return {"name": "community", "license": None, "features": [], "pages": [], "roles": [], "actions": []}

    def permanent_exceptions(self) -> bool:
        """Whether a request exception may be made permanent on purpose (D3): the Community Edition allows it."""
        return True

    def named_by_id(self) -> frozenset[str]:
        return frozenset()  # an administrator alone sets a person's name and e-mail

    def close(self) -> None:
        return None


def load_edition(context: EditionContext) -> Edition:
    """A computer without a server (``context.server`` None) is the Community Edition whatever its folders hold, a
    license, a policy or Enterprise settings: the Enterprise Edition is a Latet server's, and latet.ee is not even
    looked for, so no Enterprise code runs for a computer's edition. On a server, the Enterprise Edition when latet.ee
    is there, else the Community Edition. Only latet.ee missing means the Community Edition: any other import error in
    it is raised, so a broken install never quietly runs without what its license switches on."""
    if context.server is None:
        return Community()
    try:
        from latet.ee import make_edition
    except ModuleNotFoundError as exc:
        if exc.name != "latet.ee":
            raise
        return Community()
    return make_edition(context)


def server_context(server: Server, *, one_shot: bool = False) -> EditionContext:
    """A server instance's context: its clock and the server, and whether it is a one-shot command's (``one_shot``).
    The folders (the instance's data folder, E0b's ``Server.data``, and this process's settings folder) are not read:
    on a server the edition keeps everything in PostgreSQL."""
    from latet.config import config_path

    return EditionContext(
        data=server.data, config_dir=config_path().parent, server=server, clock=server.clock, one_shot=one_shot
    )


def server_edition(server: Server, *, one_shot: bool = False) -> Edition:
    """The edition of a server's instance: one for every space on it (``one_shot``: a one-shot command's server)."""
    return load_edition(server_context(server, one_shot=one_shot))
