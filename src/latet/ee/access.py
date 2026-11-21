"""Who may do what with the Enterprise pages of a server's dashboard, and the gate every Enterprise route passes.

A person's roles decide (ee/roles.py, through People), asked of `licensed("roles")`, which never depends on dates or
counts: a trial's end leaves roles deciding who reads, so nothing it held becomes readable by more people, or by fewer;
what may be changed then is the license's, weighed by each route (ee/routes.py). Without roles in the license everyone
reads the Enterprise pages and nobody changes them from the dashboard: `latet server` does. The Enterprise Edition is a
server's alone: a computer runs the Community Edition, which has none of this. Each route under /_latet/api/v1/ee/ is
put behind the first rule of RULES its path falls under: the feature the license must list, and the action a read
(GET) and a change need. A route under no rule is refused. Each later part adds the rows of its routes. The rule is
weighed in the dashboard's own threads (``run``: its ``Dashboard._in_pool``), never the default executor, and a rule
that cannot be weighed (the people's roles not read) refuses.

No identity is never the server's administration: ``actions(None)`` is empty, and `latet server`'s calls are named by
``ADMINISTRATION`` alone (L2), so that a request that lost its identity on the way is refused, never let through. A
request in an API key's space (ee/keys.py: the key its identity, the person working there ``ACTING``) reaches no
Enterprise route: they are people's and their roles', never a key's.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from latet.api.rest import ACTING  # the person working in an API key's space: Community's constant, not repeated
from latet.ee.roles import ACTIONS, PAGES, Role, can
from latet.logline import Paced

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from latet.ee.api import Runner
    from latet.ee.people import People
    from latet.state import IdentityRef

log = logging.getLogger(__name__)

API: Final = "/_latet/api/v1"
EE: Final = API + "/ee"
IDENTITY: Final = "latet.identity"  # E0b: the identity whose app a request was handed to
UNREAD: Final = "who may do what on this server cannot be read now: try again"  # fixed words: a driver's name its host
IN_A_KEYS_SPACE: Final = "an API key's space reaches none of the Enterprise pages"


class _Administration(enum.Enum):
    CALLS = "the server's administration"


# `latet server`'s calls (the administration token, ``Who(ADMINISTRATION, "admin:<token id>", …)``): every action.
# An enum's member, so that no identity's id, no text and no None is ever taken for it.
ADMINISTRATION: Final = _Administration.CALLS
Subject = str | Literal[_Administration.CALLS] | None


class LicenseView(Protocol):
    def licensed(self, feature: str) -> bool: ...


@dataclass(frozen=True)
class Rule:
    path: str  # under /_latet/api/v1; a "*" segment stands for any one
    feature: str | None  # what the license must list; None: the route decides (E1's and E2's)
    read: str | None  # the action GET needs; None: anyone signed in
    change: str | None  # the action any other method needs; None: anyone signed in (the route checks for itself)
    # Whether the paths below ``path`` fall under it too. A rule that names both actions covers its subtree; one that
    # lets anyone read or change covers its own path alone unless it says so (the key's space: KeySpace checks the
    # control of the key for all of it), so that no route of a later part is anyone's by a prefix.
    subtree: bool = False

    def covers_below(self) -> bool:
        return self.subtree or (self.read is not None and self.change is not None)


KEY_SPACE: Final = "/ee/keys/*/space"  # the rule of an API key's space, which the gate marks what it closes (``_Gate``)

# Reports, the administration log, the policy's text and the synced terms (E6) are read through roles only: without
# roles in the license, nobody in the dashboard (the administration uses `latet server`; E4; the text: E2 gives it to
# the administration token, E3 to those who set it). ``Seen.reads`` answers everyone for an action outside it while
# roles do not decide, and the pages /me lists weigh the same ``reads`` (fix round 1's C1 of E6's plan).
ROLES_ONLY: Final = frozenset({"reports.group", "reports.all", "audit.read", "policy.edit", "terms.manage"})

RULES: Final[tuple[Rule, ...]] = (
    Rule("/ee/license", None, "license.manage", "license.manage"),  # E1; its install and remove (decision (a))
    Rule("/ee/policy/text", None, "policy.edit", "policy.edit"),  # E3: the policy's text, for those who set it
    Rule("/ee/policy", None, None, "policy.edit"),  # E2: everyone reads the floor, for the locks of their pages
    Rule("/ee/people", "roles", "people.manage", "people.manage"),
    Rule("/ee/groups", "roles", "people.manage", "people.manage"),
    Rule("/ee/roles", "roles", "people.manage", "people.manage"),
    Rule(KEY_SPACE, "roles", None, None, subtree=True),  # its controllers and administrators: KeySpace checks
    Rule("/ee/keys/*", "roles", "keys.manage", "keys.manage"),  # a key rotated, disabled, enabled; who controls it
    Rule("/ee/keys", "roles", None, "keys.manage"),  # GET: the keys the person may open
    Rule("/ee/sign-in/code", "sso", None, None),  # anyone, to connect a computer
    Rule("/ee/sign-in", "sso", "sign-in.edit", "sign-in.edit"),
    Rule("/ee/notices/told", "sso", None, None),  # anyone, of what their own dashboard told them (O6)
    # E4: the reports read by group leads (their groups), the security role and auditors; the administration log by
    # auditors, the security role and administrators; the SIEM outputs read by the security role and auditors and
    # changed by the security role. No change of the reports is the dashboard's (`latet server reports settings`):
    # a later route under /ee/reports would need reports.all.
    Rule("/ee/reports", "reports", "reports.group", "reports.all"),
    Rule("/ee/audit", "reports", "audit.read", "audit.read"),
    Rule("/ee/siem", "siem", "reports.all", "siem.edit"),
    # E5: whoever decides something (the security team, a key's controllers, the admins), and whoever asked for their
    # own requests: the routes check who. Its subtree: the history, a text, its images, the decisions.
    Rule("/ee/approvals", "approvals", None, None, subtree=True),
    # E6: the Term sync page's routes, for whoever terms.manage allows (the security role), the sources and their
    # secrets among them ([D4]); without roles in the license nobody (ROLES_ONLY).
    Rule("/ee/term-sync", "term-sync", "terms.manage", "terms.manage"),
)


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def rule_for(path: str) -> Rule | None:
    """The first rule ``path`` falls under: a route's path under /_latet/api/v1/ee/, its parameters as written (its own
    path, or one below it when the rule covers its subtree, ``Rule.covers_below``)."""
    if not path.startswith(EE + "/"):
        return None
    parts = _parts(path[len(API) :])
    for rule in RULES:
        wanted = _parts(rule.path)
        fits = len(parts) > len(wanted) if rule.covers_below() else False
        if (len(parts) == len(wanted) or fits) and all(w in ("*", p) for w, p in zip(wanted, parts, strict=False)):
            return rule
    return None


@dataclass(frozen=True)
class Seen:
    """What one identity may do, from one read of its roles (``Access.seen``): what /me says, the pages it lists and
    the headcount it tells all weigh the same read."""

    roles: frozenset[Role]
    actions: frozenset[str]
    by_roles: bool  # the license lists roles: they decide
    identified: bool  # an identity (or the administration): no identity reads nothing

    def allows(self, action: str) -> bool:
        return action in self.actions

    def reads(self, action: str) -> bool:
        """Whether it sees what ``action`` covers: everyone while roles do not decide, but the reports, the
        administration log, the policy's text and the synced terms (``ROLES_ONLY``), which nobody reads in the
        dashboard then (E4: administrators use `latet server`). Nobody for no identity."""
        if not self.identified:
            return False
        return self.allows(action) if self.by_roles else action not in ROLES_ONLY


# Roles that could not be read: no role, no action, nothing read (the pages PAGES names are all left out).
UNSEEN: Final = Seen(frozenset(), frozenset(), by_roles=True, identified=True)


class Access:
    def __init__(self, people: People, licensing: LicenseView) -> None:
        self.people = people
        self.licensing = licensing
        self.unruled: set[str] = set()  # Enterprise routes under no rule, which the gate refuses
        self._unread = Paced()  # roles that cannot be read, at each request: said once a minute

    def by_roles(self) -> bool:
        return self.licensing.licensed("roles")

    def seen(self, identity_id: Subject) -> Seen:
        """What ``identity_id`` may do, from one read of its roles (People's database or Valkey: it raises as they
        do): every action for the server's administration token (``ADMINISTRATION``: `latet server`'s calls, never a
        person), none for no identity (L2: never a default that lets through), none while the license has no roles."""
        by_roles = self.by_roles()
        if identity_id is ADMINISTRATION:
            return Seen(frozenset(), frozenset(ACTIONS), by_roles, identified=True)
        if not isinstance(identity_id, str) or not identity_id:
            return Seen(frozenset(), frozenset(), by_roles, identified=False)
        if not by_roles:
            return Seen(frozenset(), frozenset(), by_roles, identified=True)
        roles = self.people.roles_of(identity_id)
        return Seen(roles, frozenset(action for action in ACTIONS if can(roles, action)), by_roles, identified=True)

    def roles(self, identity_id: Subject) -> frozenset[Role]:
        return self.seen(identity_id).roles

    def actions(self, identity_id: Subject) -> frozenset[str]:
        """The actions of ee/roles.py the person may take (``seen``)."""
        return self.seen(identity_id).actions

    def allows(self, identity_id: Subject, action: str) -> bool:
        return self.seen(identity_id).allows(action)

    def reads(self, identity_id: Subject, action: str) -> bool:
        """Whether the person sees what ``action`` covers (``Seen.reads``)."""
        return self.seen(identity_id).reads(action)

    def refusal(
        self, rule: Rule | None, method: str, identity_id: str | None, *, acting: object = None
    ) -> tuple[int, str] | None:
        """None when the person may make this request; else its status and why. ``acting``: the person working in an
        API key's space (``scope[ACTING]``, which only ee/keys.py sets), whose request names the key as its identity:
        every Enterprise route is refused there, those that let anyone signed in through (the keys one may open, the
        floor, connecting a computer: a person's) as those of a role (which a key never holds)."""
        if rule is None:
            return 403, "this Enterprise route has no access rule"
        if not isinstance(identity_id, str) or not identity_id:  # the server always hands a person or a key on
            return 401, "unauthorized"
        if acting is not None:
            return 403, IN_A_KEYS_SPACE
        if rule.feature is not None and not self.licensing.licensed(rule.feature):
            return 403, f"this needs Latet Enterprise with {rule.feature}"
        reading = method in ("GET", "HEAD")
        action = rule.read if reading else rule.change
        if action is None:
            return None
        seen = self.seen(identity_id)
        if seen.reads(action) if reading else seen.allows(action):
            return None
        if not seen.by_roles:
            return 403, "on this server, the Enterprise settings are changed with `latet server`"
        return 403, f"your roles on this server do not {'include' if reading else 'allow'} {action}"

    def pages(
        self, identity: IdentityRef | None, pages: Iterable[str], *, sign_in_ready: bool, seen: Seen | None = None
    ) -> list[str]:
        """The Enterprise pages the person is shown, of those the license gives: each when they read it. People and
        groups and Sign-in are a server's: the first for administrators, the second for them and, once single sign-on
        is set up, for everyone (to connect a computer). An API key's space shows none. A page PAGES does not name is
        left as its part listed it; a page no role decides is added by its part after this filter (E5's
        company-approvals, for an API key's controllers, who hold no role). ``seen``: the person's roles as /me read
        them already (else read here, once)."""
        if identity is not None and identity.kind == "key":
            return []
        if seen is None:
            seen = self.seen(identity.id if identity is not None else None)
        shown = [
            page
            for page in pages
            if page not in ("people", "sign-in") and (page not in PAGES or seen.reads(PAGES[page]))
        ]
        if seen.by_roles and seen.allows("people.manage"):
            shown.append("people")
        if self.licensing.licensed("sso") and seen.identified and (sign_in_ready or seen.reads("sign-in.edit")):
            shown.append("sign-in")
        return shown

    def gated(self, routes: Iterable[BaseRoute], *, run: Runner) -> list[BaseRoute]:
        """``routes`` (every one the edition's) with each behind its rule, weighed through ``run`` (the dashboard's
        threads: ``Dashboard._in_pool``). Deny by default: only a plain route under /_latet/api/v1/ee/ is served, behind
        the first rule it falls under, or refused when it falls under none; anything else (a Mount, a Router, a
        WebSocket route, a route outside /_latet/api/v1/ee/ or at its root) is left out. Both are named in
        ``unruled`` and logged."""
        out: list[BaseRoute] = []
        for route in routes:
            path = getattr(route, "path", "")
            if not isinstance(route, Route) or not route.path.startswith(EE + "/"):
                self.unruled.add(path)
                log.error("the Enterprise route %s is no plain route under %s/: it is left out", path or "-", EE)
                continue
            rule = rule_for(route.path)
            if rule is None:
                self.unruled.add(route.path)
                log.error("the Enterprise route %s is under no access rule: it is refused", route.path)
            methods = sorted(route.methods) if route.methods else None
            out.append(Route(route.path, _Gate(route.app, rule, self, run), methods=methods, name=route.name))
        return out

    def weighed(
        self, rule: Rule | None, method: str, identity_id: str | None, *, acting: object = None
    ) -> tuple[int, str] | None:
        """``refusal``, in a worker thread, that never raises: the people's roles that cannot be read (PostgreSQL,
        Valkey) refuse the request (503, fixed words), the failure said by its type once a minute."""
        try:
            return self.refusal(rule, method, identity_id, acting=acting)
        except Exception as exc:  # its words can name the database's or Valkey's address
            if count := self._unread.due():
                log.warning(
                    "who may do what could not be read for an Enterprise route (%s): it is refused%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return 503, UNREAD


class _Gate:
    """A route's app behind its rule: the request goes on, or is refused with the rule's reason."""

    def __init__(self, app: ASGIApp, rule: Rule | None, access: Access, run: Runner) -> None:
        self.app = app
        self.rule = rule
        self.access = access
        self.run = run

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        identity_id, acting = scope.get(IDENTITY), scope.get(ACTING)
        refusal = await self.run(self.access.weighed, self.rule, scope["method"], identity_id, acting=acting)
        if refusal is None:
            await self.app(scope, receive, send)
            return
        status, message = refusal
        headers = {"cache-control": "no-store"} | ({"retry-after": "1"} if status == 503 else {})
        body = {"error": message}
        # What the gate refuses in front of an API key's space closes it, as KeySpace's own refusals say: the license
        # without roles (403), who may do what unreadable (503). The dashboard goes back to the person's own space on
        # the marker, never on these words. An Enterprise route refused inside the space (``acting``) closes nothing:
        # the key's app refuses a page that is the person's, and the space stays open.
        if self.rule is not None and self.rule.path == KEY_SPACE and acting is None and status in (403, 503):
            body["space"] = "closed"
        await JSONResponse(body, status_code=status, headers=headers)(scope, receive, send)
