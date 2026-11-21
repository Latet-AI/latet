"""What E3's routes share: who asks, the refusals, and one handler served twice: to the dashboard of a server
(/_latet/api/v1/ee/…, the person signed in, behind the gate of ee/access.py and the dashboard's own guard) and to its
administration API (/_latet/server/v1/admin/ee/…, behind E0b's one administration gate: an administration token, never
from a web page, over HTTPS when the server's address is https, every call recorded under the route's name). A
server's module: psycopg comes with it.

The license is weighed as E1's gates weigh it, by the route's kind: "manage" (reading or changing Enterprise settings)
answers a read in every standing of a license that lists its feature, and a change only while the license can manage;
"serve" (letting people in) answers while the license lists its feature and no trial has ended. A change is one
transaction (``Who.change``): on the administration API the call's token's row first, then the license as that
transaction finds it (``held``), then the work, its entries in the administration log last; once it commits every
instance is told (``People.committed``). The work runs in the dashboard's threads (``Dashboard._in_pool``) or the
administration's (``gate.run``), never the default executor.

From the dashboard, the change's transaction also weighs who makes it, past every copy (``in_their_name``): the
person's roles as it finds them (a role taken away on any instance before it is seen there, not only by the gate's
copy), and the session they make it from: while the sign-in settings say ``company_changes: sso``, a change that
applies to everyone takes a session opened with the company's sign-in (the user's decision (c), H2), which a one-time
code of `latet dashboard` does not open (an agent at the person's computer can ask for one). Reads are never refused
by it, nor is the administration (its token)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from latet.ee.access import ADMINISTRATION, EE, IDENTITY, Subject, rule_for
from latet.ee.admin import ADMIN_EE, admin_endpoint  # E1's: /_latet/server/v1/admin/ee, behind E0b's gate
from latet.ee.api import Runner, change_refused, error, managing_route, read_body, refusal, serving_route
from latet.ee.api import json_body as json_of
from latet.ee.license import NotLicensed, State, TrialEnded, require, require_serving
from latet.ee.people import Changing
from latet.ee.roles import can
from latet.ee.server import settings_in
from latet.server.machines import client_of

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.api.rest import Dashboard
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.license import Licensing, Standing
    from latet.ee.people import People
    from latet.ee.settings import EnterpriseSettings
    from latet.server.admin import AdminGate, _Call

Kind = Literal["manage", "serve"]
MAX_JSON = 1_000_000
VIA: Final = "latet.via"  # E0b's: how the dashboard's session was opened, "sso" or "code" (none: "code")
COMPANY_SIGN_IN: Final = "sign in with your company account to change what applies to everyone"
BY_COMMANDS: Final = "on this server, the Enterprise settings are changed with `latet server`"  # Access's words
_READS = ("GET", "HEAD")


class Refused(Exception):
    """What a request is answered when it cannot be done: its status, why, and anything else the answer carries; or
    ``response``, an answer E1's refusals made already."""

    def __init__(self, status: int, message: str, *, response: Response | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.response = response
        self.extra = extra

    def answer(self) -> Response:
        if self.response is not None:
            return self.response
        return JSONResponse({"error": self.message, **self.extra}, status_code=self.status)


@dataclass(frozen=True)
class Who:
    """Who asks, and how their request's work is done: ``run(work, *args)`` in the request's threads, and
    ``change(work)``: ``work(within)`` in one transaction with its entries, the license weighed there."""

    # The person signed in to the dashboard; ``ADMINISTRATION`` for an administration token (`latet server`), never
    # None: a dashboard request that names no one is refused before a Who is made (L2).
    identity_id: Subject
    actor: str  # whom the administration log names: the person's id, or "admin:<token id>"
    address: str | None
    run: Runner
    change: Callable[[Callable[[Changing], Any]], Awaitable[Any]]

    @property
    def person(self) -> bool:
        """Asked from the dashboard: the last administrator is then kept."""
        return isinstance(self.identity_id, str) and bool(self.identity_id)


Handler = Callable[[Request, Who], Awaitable[Response]]


@dataclass(frozen=True)
class EeRoute:
    method: str
    path: str  # under /ee: "/groups/{id}/members"
    handler: Handler
    kind: Kind = "manage"  # manage: Enterprise settings, read in every standing; serve: lets people in (R2)
    feature: str = "roles"  # what the license must list: roles, sso, or locked-rules (the policy's editor)
    admin: bool = True  # served to the administration API too
    answers: int = 200  # the status a change answers, which its administration-log entry says (201: made)
    dashboard: bool = True  # served to the dashboard; False: `latet server`'s alone
    # A read that changes things (an export: its entry in the log and to the SIEM, the instance's one export): from the
    # dashboard it needs the `X-Latet-UI` header as a change does, so that no page of another site makes it through the
    # session's cookie (L1 of E4's whole-branch review). `latet server`'s calls carry a token, never a cookie.
    marked: bool = False

    @property
    def name(self) -> str:
        """What the administration log calls a call of it: ee.<its first segment>.read, or .change."""
        area = self.path.strip("/").split("/")[0]
        return f"ee.{area}.{'read' if self.method in _READS else 'change'}"


def held(standing: Standing, kind: Kind, feature: str) -> Refused | None:
    """The license as a change's own transaction finds it (its row held there until it ends: E1's ``standing_in``):
    None while it allows a change of ``kind``; else the refusal E1's gates answer, in their words and codes."""
    lic = standing.license
    if kind == "serve":
        if lic is None or feature not in lic.features:
            return Refused(403, str(NotLicensed(feature)), feature=feature)
        if standing.state is State.TRIAL_ENDED:
            return Refused(403, str(TrialEnded(lic.expires)), code="trial-ended", feature=feature)
        return None
    if lic is not None and feature not in lic.features:
        return Refused(403, str(NotLicensed(feature)), feature=feature)
    answer = refusal(standing, feature)  # trial-ended, not-manageable: E1's own answer
    return None if answer is None else Refused(answer.status_code, "refused by the license", response=answer)


def company_sign_in(settings: Callable[[], EnterpriseSettings], via: object) -> Refused | None:
    """H2 (the user's decision (c)): None when a session opened as ``via`` may make a change that applies to everyone,
    under the sign-in settings ``settings()`` gives (read only for a session that is not the company sign-in's); else
    the refusal. A session the company's sign-in opened always may; another (`latet dashboard`'s one-time code, or one
    opened before sessions said how) only where no single sign-on is set up, or its settings say ``any-session``.
    Settings that cannot be read raise: nothing is changed then."""
    if via == "sso":
        return None
    sso = settings().sso
    if sso is None or sso.company_changes == "any-session":
        return None
    return Refused(403, COMPANY_SIGN_IN, code="company-sign-in")


def in_their_name(
    people: People, conn: Connection[Any], standing: Standing, identity_id: str, action: str | None, via: object
) -> Refused | None:
    """In a change's transaction from the dashboard, after the license (``held``), who makes it as that transaction
    finds them: roles decide (the license it finds lists them; else the page changes nothing, `latet server` does), the
    person is enabled and their roles allow ``action`` (None: the route checks for itself), read there past every copy;
    and their session may make a change that applies to everyone (``company_sign_in``, the settings read there)."""
    if action is not None:
        lic = standing.license
        if lic is None or "roles" not in lic.features:
            return Refused(403, BY_COMMANDS)
        roles = people.roles_in(conn, identity_id)
        if roles is None or not can(roles, action):
            return Refused(403, f"your roles on this server do not allow {action}")
    return company_sign_in(lambda: settings_in(conn), via)


def refused_now(licensing: Licensing, kind: Kind, feature: str, method: str) -> Response | None:
    """In a worker thread, before a body is read: the refusal E1's gates answer for this route as this instance sees
    the license (a change is weighed again in its own transaction: ``held``); None when it may go on."""
    try:
        if kind == "serve":
            require_serving(licensing, feature)
            return None
        require(licensing, feature)
    except NotLicensed as exc:
        return error(str(exc), 403, feature=exc.feature)
    except TrialEnded as exc:
        return error(str(exc), 403, code="trial-ended", feature=feature)
    return change_refused(licensing, feature) if method not in _READS else None


def _object_of(body: bytes) -> dict[str, Any] | None:
    try:
        found = json_of(body or b"{}")
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


async def json_body(request: Request, who: Who) -> dict[str, Any]:
    """The request's body as a JSON object, read as E1 reads one (NaN and the infinities refused), parsed in the
    request's threads: 413 past MAX_JSON bytes, 400 for anything else."""
    body = await read_body(request, MAX_JSON)
    if isinstance(body, Response):
        raise Refused(413, "the request is too large")
    found = await who.run(_object_of, body)
    if found is None:
        raise Refused(400, "a JSON object is expected")
    return found


def dashboard_route(route: EeRoute, dashboard: Dashboard, edition: EnterpriseEdition) -> Route:
    """The route in a server's dashboard: behind the dashboard's guard (its session, its origin, its header) and E1's
    gate of its kind (``managing_route`` or ``serving_route``, in the dashboard's threads); the role's gate
    (ee/access.py) is put in front of it with the edition's other routes. A change is refused, before its body is read,
    to a session that may not make it as this instance has the sign-in settings, and weighed again in its transaction
    with who makes it (``in_their_name``)."""
    server, people, licensing, run = edition.server, edition.people, edition.licensing, dashboard._in_pool
    assert people is not None  # E3's routes are a server's (R0): every Enterprise Edition is
    rule = rule_for(EE + route.path)
    action = None if rule is None else rule.read if route.method in _READS else rule.change

    async def endpoint(request: Request) -> Response:
        identity_id = request.scope.get(IDENTITY)
        if not isinstance(identity_id, str) or not identity_id:  # never a request by no one (L2)
            return error("unauthorized", 401)
        if route.marked and request.headers.get("x-latet-ui") != "1":  # before anything is taken or written
            return error("missing X-Latet-UI header", 403)
        actor, address, via = identity_id, client_of(request.scope), request.scope.get(VIA)
        if request.method not in _READS:  # said before the body is read; asked again in the change's transaction
            early: Refused | None = await run(company_sign_in, edition.settings, via)
            if early is not None:
                return early.answer()

        async def change(work: Callable[[Changing], Any]) -> Any:
            def transaction() -> Any:
                with server.identities.changing() as changes:
                    standing = licensing.standing_in(changes.conn)
                    if (refused := held(standing, route.kind, route.feature)) is not None:
                        raise refused
                    if (refused := in_their_name(people, changes.conn, standing, actor, action, via)) is not None:
                        raise refused
                    within = Changing(changes, actor, address)
                    result = work(within)
                    within.write(server.audit)  # last in the transaction
                people.committed(within)
                return result

            return await run(transaction)

        try:
            return await route.handler(request, Who(identity_id, actor, address, run, change))
        except Refused as exc:
            return exc.answer()

    gate = managing_route if route.kind == "manage" else serving_route
    guarded = dashboard.guarded(gate(licensing, route.feature, endpoint, run=run))
    return Route(EE + route.path, guarded, methods=[route.method])


def admin_route(route: EeRoute, edition: EnterpriseEdition, gate: AdminGate) -> Route:
    """The route in the administration API, behind E0b's gate (``admin_endpoint``), everything allowed as for `latet
    server` (``ADMINISTRATION``), the license weighed as on the dashboard: before the body is read, as this instance
    sees it, then in the change's own transaction, after the call's token's row (R7). The change's last entry is the
    call's own (``gate.entry``, its token checked again there); the others are written before it, in the same
    transaction."""
    server, people, licensing = edition.server, edition.people, edition.licensing
    assert people is not None

    async def endpoint(request: Request, call: _Call) -> Response:
        if (refused := await gate.run(refused_now, licensing, route.kind, route.feature, request.method)) is not None:
            return refused

        async def change(work: Callable[[Changing], Any]) -> Any:
            def transaction() -> Any:
                with server.identities.changing() as changes:
                    gate.holding(call, changes.conn)  # first: a token gone or past its day changes nothing
                    if (refused := held(licensing.standing_in(changes.conn), route.kind, route.feature)) is not None:
                        raise refused
                    within = Changing(changes, call.actor, call.address)
                    result = work(within)
                    notes = within.notes
                    before, last = notes[:-1], (notes[-1] if notes else None)
                    for action, target, details in before:
                        server.audit.record(call.actor, action, target, details, call.address, conn=changes.conn)
                    if last is not None:
                        gate.entry(call, changes, last[0], last[2] or {}, target=last[1], status=route.answers)
                if last is not None:
                    gate.recorded(call, last[0], last[2] or {}, last[1])
                people.committed(within)
                return result

            return await gate.run(transaction)

        try:
            return await route.handler(request, Who(ADMINISTRATION, call.actor, call.address, gate.run, change))
        except Refused as exc:
            return exc.answer()

    return Route(ADMIN_EE + route.path, admin_endpoint(gate, route.name, endpoint), methods=[route.method])
