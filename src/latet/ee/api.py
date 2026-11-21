"""The Enterprise routes of the dashboard's API on a Latet server, under /_latet/api/v1/ee/ (E1: the license), behind
the dashboard's sign-in as its other routes, and each behind its access rule (ee/access.py, E3). The License page is
its administrators' once roles decide (`license.manage`), everyone's while they do not; its administrators install and
remove the license there while the license can manage (E3, the user's decision (a)), and with `latet server license
install | remove` in every state.

And the gates every later part's route goes through: ``licensed_route`` (reading what protects, a feature the
license switches on: reads only),
``managing_route`` (reading or changing Enterprise settings: reads answer whatever the standing, changes only while the
license can manage) and ``serving_route`` (letting people in, deciding for the company: stopped once a trial ended).
What protects is behind none of them."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from latet.ee.license import (
    MANAGEABLE,
    LicenseError,
    Licensing,
    NotLicensed,
    Standing,
    State,
    TrialEnded,
    details,
    not_a_license,
    require,
    require_serving,
)

if TYPE_CHECKING:
    from latet.api.rest import Dashboard
    from latet.ee.edition import EnterpriseEdition

EE_API: Final = "/_latet/api/v1/ee"
MAX_BODY: Final = 256 * 1024  # a license file, quoted in JSON
NOT_A_LICENSE: Final = 'the body is {"license": "<the license file\'s text>"}'
POLICY_STOPS: Final = "policy_stops"  # the body's word that the person saw the server's policy stop, and goes on
NOT_A_REMOVAL: Final = f'the body is nothing, or {{"{POLICY_STOPS}": true}}'
STOPS: Final = (
    "not changed: the server's policy stops being in force with this change (the license left lists no locked rules): "
    f'send it again with "{POLICY_STOPS}": true to make it'
)
# E6 (R29): what the change stops of the synced terms, alone or with the policy; the same confirmation covers each.
TERMS_STOP: Final = (
    "not changed: the synced terms stop being masked with this change (the license left lists no term sync): "
    f'send it again with "{POLICY_STOPS}": true to make it'
)
BOTH_STOP: Final = (
    "not changed: the server's policy stops being in force and the synced terms stop being masked with this change "
    f'(the license left lists neither locked rules nor term sync): send it again with "{POLICY_STOPS}": true to make it'
)
# What each of ``_ends_the_policy``'s answers stops, and what the license left does not list, in the order said:
# the final security review's L1 adds central approvals, whose decisions go back to whoever asks.
_STOPPING: Final = (
    ("policy", "the server's policy stops being in force", "locked rules"),
    ("terms", "the synced terms stop being masked", "term sync"),
    ("approvals", "central approvals stop", "approvals"),
)


def _listed(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def stops_said(stops: dict[str, Any]) -> str:
    """The words of the 409 for what a license change stops (``_ends_the_policy``'s answer: the policy, the synced
    terms, central approvals, or several), each named (``STOPS``, ``TERMS_STOP``, ``BOTH_STOP`` among them)."""
    named = [(words, lacks) for key, words, lacks in _STOPPING if key in stops] or [_STOPPING[0][1:]]
    said = _listed([words for words, _ in named])
    features = [lacks for _, lacks in named]
    if len(features) == 1:
        left = f"no {features[0]}"
    elif len(features) == 2:
        left = f"neither {features[0]} nor {features[1]}"
    else:
        left = f"none of {_listed(features)}"
    again = f'send it again with "{POLICY_STOPS}": true to make it'
    return f"not changed: {said} with this change (the license left lists {left}): {again}"


Handler = Callable[[Request], Awaitable[Response]]
# Where a gate reads the license, off the event loop: ``work(*args)`` in a thread. On a server, a dashboard's route
# passes its bounded threads (``Dashboard._in_pool``), and an administration route the administration's: never the
# default executor, which the spaces' builds hold for seconds. Every gate is given one: none has a default.
Runner = Callable[..., Awaitable[Any]]
_READS: Final = ("GET", "HEAD")


def error(message: str, status: int, **more: Any) -> JSONResponse:
    return JSONResponse({"error": message, **more}, status_code=status)


async def read_body(request: Request, limit: int) -> bytes | Response:
    """The request's body, or the answer 413 once it passes ``limit`` bytes."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return error("too large", 413)
    return bytes(body)


def _no_number(constant: str) -> Any:
    raise ValueError("NaN and the infinities are not JSON")


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):  # 1e999: valid JSON that no float holds, read as an infinity
        raise ValueError("a number too large for a float")
    return number


def json_body(body: bytes) -> Any:
    """``body`` read as JSON, and only JSON: Python's json also reads NaN, Infinity and -Infinity, and a number too
    large for a float (1e999) as an infinity, which a store cannot keep and no client should send, so they are refused
    (ValueError) as anything else that is not JSON."""
    return json.loads(body, parse_constant=_no_number, parse_float=_finite)


def license_of(body: bytes) -> str | Response:
    """The license file's text in ``body``, {"license": "..."}, or the answer 400 that refuses any body that cannot be
    read as one (not UTF-8, not JSON, nested past what Python reads, not an object), never a 500: the administration
    API's install, in a worker thread, where the call reads its stores."""
    try:
        text = json_body(body or b"{}").get("license")
    except (ValueError, AttributeError, RecursionError):
        text = None
    if not isinstance(text, str):
        return error(NOT_A_LICENSE, 400)
    return text


def _identity_of(request: Request) -> str | None:
    """The identity the server handed the request to (E0b's ``scope["latet.identity"]``), None without one."""
    identity = request.scope.get("latet.identity")
    return identity if isinstance(identity, str) and identity else None


def _change_of(body: bytes, *, installing: bool) -> tuple[bytes | None, bool] | Response:
    """In a worker thread: what a change of the License page sends, the license file's text in UTF-8 (installing, as
    ``license_of`` reads it) and whether the person confirmed what stops with it, the server's policy, the synced
    terms or central approvals (``"policy_stops": true``, and nothing else); else the answer 400, never a 500
    (``license_of``'s refusals). A removal's body may be empty."""
    if not installing and not body:
        return None, False
    try:
        found = json_body(body)
    except (ValueError, RecursionError):
        found = None
    if not isinstance(found, dict):
        return error(NOT_A_LICENSE if installing else NOT_A_REMOVAL, 400)
    confirmed = found.get(POLICY_STOPS) is True
    if not installing:
        return None, confirmed
    if not isinstance(text := found.get("license"), str):
        return error(NOT_A_LICENSE, 400)
    try:
        return text.encode("utf-8"), confirmed
    except UnicodeEncodeError:  # a lone surrogate, sent as JSON's escape of one: no license file holds it
        raise not_a_license("it holds text that is not Unicode") from None


def license_routes(edition: EnterpriseEdition, dashboard: Dashboard) -> list[BaseRoute]:
    """GET the server's license as it stands, whatever the standing (a trial that ended included), as its reader is
    told it (``EnterpriseEdition.told``: the headcount to its administrators alone, R19). On a server (E3's ``access``:
    never in the tests' in-memory edition), ``PUT`` installs a license and ``DELETE`` removes it, from the License page,
    for `license.manage` (the gate of ee/access.py, before the route, and the person's roles again as the change's
    transaction finds them), from a session the sign-in settings let change what applies to everyone (403
    ``company-sign-in`` otherwise: H2, ee/routes.py), while the license can manage (403 ``trial-ended`` or
    ``not-manageable`` otherwise), each as this instance sees it before the body is read and as the change's
    transaction finds it: the change and its entry in the administration log (``license.installed``,
    ``license.removed``, by the person) in one transaction, every instance told once it commits. A change that stops
    the server's policy (a removal, or a license without locked rules) answers 409 ``policy: "stops"``, one that
    stops the synced terms while some are kept (a license without term sync: E6, R29) ``terms: "stop"``, and one that
    stops central approvals while the security team decides on changes or texts (a license without approvals: the
    final security review's L1) ``approvals: "stop"`` (each that stops, ``stops_said`` naming each), nothing changed,
    unless the body says ``"policy_stops": true``, which confirms what stops. What stops is weighed from the features
    what protects kept before the change (``EnterpriseEdition.held_features``). Everything is read and written in the
    dashboard's threads (``Dashboard._in_pool``: the server's bounded ones, one job per identity)."""

    async def show(request: Request) -> Response:
        identity_id = _identity_of(request)
        standing = await dashboard._in_pool(lambda: edition.told(edition.standing(), identity_id))
        return JSONResponse(details(standing))

    routes: list[BaseRoute] = [Route(f"{EE_API}/license", dashboard.guarded(show), methods=["GET"])]
    people = edition.people
    if edition.access is None or people is None:  # the tests' in-memory edition (R22): no route of E3's
        return routes

    def changed(identity_id: str, address: str, data: bytes | None, confirmed: bool, via: object) -> bool:
        """In a worker thread: the license installed (``data``) or removed, with its entry, in one transaction; every
        instance told once it commits. Whether there was one to remove. LicenseError, ``_Refused`` (the license as
        that transaction finds it cannot manage, or the policy would stop unconfirmed) or ``Refused`` (the person, as it
        finds them, may no longer manage the license, or their session may not make a change that applies to everyone:
        ee/routes.py), nothing changed."""
        from latet.ee.admin import _ends_the_policy, _Refused
        from latet.ee.edition import license_details
        from latet.ee.routes import in_their_name

        server = edition.server
        action = "license.installed" if data is not None else "license.removed"
        with server.db.transaction() as conn:
            before = edition.held_license(conn)  # the one this transaction replaces or removes, its row held
            protected = edition.held_features(conn)  # what protects keeps of it (L2), read before the change
            standing = edition.licensing.standing_in(conn)
            if (refused := refusal(standing)) is not None:
                raise _Refused(refused)
            if (asked := in_their_name(people, conn, standing, identity_id, "license.manage", via)) is not None:
                raise asked
            if data is None:
                if not edition.remove_license(conn=conn):
                    return False
                lic, after = before, None
            else:
                lic = after = edition.install_license(data, conn=conn)
            stops = _ends_the_policy(edition, conn, protected, after)
            if stops and not confirmed:  # {policy: "stops"}, {terms: "stop"} (R29), {approvals: "stop"} (L1): each said
                raise _Refused(error(stops_said(stops), 409, **stops))
            target = lic.id if lic is not None else None
            said = license_details(lic) | stops
            server.audit.record(identity_id, action, target=target, details=said, address=address, conn=conn)  # last
        edition.committed(action, dashboard.rt.identity)
        return True

    async def change(request: Request) -> Response:
        from latet.ee.admin import _Refused
        from latet.ee.routes import VIA, Refused, company_sign_in
        from latet.server.machines import client_of

        identity_id = _identity_of(request)
        if identity_id is None:  # the gate refused it already: never a change by nobody
            return error("unauthorized", 401)
        # Said before the body is read, as this instance sees the license and the sign-in settings (a license installed
        # or removed applies to everyone: H2); checked again in the change's transaction.
        if (refused := await dashboard._in_pool(change_refused, edition.licensing)) is not None:
            return refused
        via = request.scope.get(VIA)
        if (early := await dashboard._in_pool(company_sign_in, edition.settings, via)) is not None:
            return early.answer()
        body = await read_body(request, MAX_BODY)
        if isinstance(body, Response):
            return error("too large for a license file", 413)
        installing = request.method == "PUT"
        try:
            sent = await dashboard._in_pool(_change_of, body, installing=installing)
            if isinstance(sent, Response):
                return sent
            data, confirmed = sent
            done = await dashboard._in_pool(changed, identity_id, client_of(request.scope), data, confirmed, via)
        except _Refused as refused_there:
            return refused_there.response
        except Refused as asked_there:
            return asked_there.answer()
        except LicenseError as exc:  # its words never quote the file
            return error(str(exc), 400, code=exc.code)
        standing = await dashboard._in_pool(lambda: edition.told(edition.standing(), identity_id))
        return JSONResponse(details(standing) if installing else {**details(standing), "removed": done})

    routes.append(Route(f"{EE_API}/license", dashboard.guarded(change), methods=["PUT", "DELETE"]))
    return routes


def licensed_route(licensing: Licensing, feature: str, handler: Handler, *, run: Runner) -> Handler:
    """``handler``, for a route that reads what protects (a floor, rules for everyone, where changes go), which answers
    whatever the standing while the license in use, or the last one verified, lists ``feature``: 403 with a plain
    message while it does not. It only reads: ``GET`` and ``HEAD`` reach ``handler``, any other method answers 405,
    ``handler`` never called, since ``licensed`` stays on after a trial's end, during a pause and while the file cannot
    be used (M1), when nothing may change. A route that changes goes through ``managing_route``, ``serving_route``
    for what lets people in or decides. The license is read through ``run`` (``Runner``)."""

    async def endpoint(request: Request) -> Response:
        if request.method not in _READS:
            only = "this route only reads: a change goes through the Enterprise administration"
            return JSONResponse({"error": only}, status_code=405, headers={"allow": ", ".join(_READS)})
        try:
            await run(require, licensing, feature)
        except NotLicensed as exc:
            return error(str(exc), 403, feature=exc.feature)
        return await handler(request)

    return endpoint


def managing_route(licensing: Licensing, feature: str, handler: Handler, *, run: Runner) -> Handler:
    """``handler``, for a route that reads or changes Enterprise settings (each later part's administration): 403 while
    the license does not switch ``feature`` on. ``GET`` and ``HEAD`` then answer whatever the standing (what was set can
    be read, as it stood at the end); any other method answers only while the license can manage
    (``Licensing.manageable``), else 403 with ``code: "trial-ended"`` once a trial ended, ``code: "not-manageable"``
    otherwise (paused, a file that cannot be used), ``handler`` never called. What protects is not behind it. The
    license is read through ``run`` (``Runner``)."""

    async def endpoint(request: Request) -> Response:
        try:
            await run(require, licensing, feature)
        except NotLicensed as exc:
            return error(str(exc), 403, feature=exc.feature)
        if request.method not in _READS:
            refused = await run(change_refused, licensing, feature)
            if refused is not None:
                return refused
        return await handler(request)

    return endpoint


def serving_route(licensing: Licensing, feature: str, handler: Handler, *, run: Runner) -> Handler:
    """``handler``, for a route that lets people in or decides for the company (sign-in with SSO, the decisions of
    central approvals, an Enterprise page's actions): 403 while the license does not switch ``feature`` on, and 403
    with ``code: "trial-ended"`` to every method once a trial ended (an SSO callback is a GET), ``handler`` never
    called. What protects is not behind it. The license is read through ``run`` (``Runner``)."""

    async def endpoint(request: Request) -> Response:
        try:
            await run(require_serving, licensing, feature)
        except NotLicensed as exc:
            return error(str(exc), 403, feature=exc.feature)
        except TrialEnded as exc:
            return error(str(exc), 403, code="trial-ended", feature=feature)
        return await handler(request)

    return endpoint


def change_refused(licensing: Licensing, feature: str | None = None) -> Response | None:
    """None while the license can manage the Enterprise settings; else the 403 that says why, and that they can still
    be read: ``code: "trial-ended"`` once a trial ended, ``"not-manageable"`` otherwise (``feature``: the one the route
    is for, said with it). In a worker thread: it reads the license."""
    return None if licensing.manageable() else refusal(licensing.standing(), feature)


def refusal(standing: Standing, feature: str | None = None) -> Response | None:
    """``change_refused`` for the license as ``standing`` says it (a server's administration reads it again in the
    change's own transaction): None while it can manage the Enterprise settings."""
    if standing.state in MANAGEABLE:
        return None
    said = {} if feature is None else {"feature": feature}
    if standing.state is State.TRIAL_ENDED and standing.license is not None:
        return error(str(TrialEnded(standing.license.expires)), 403, code="trial-ended", **said)
    return error(_not_manageable(standing), 403, code="not-manageable", **said)


def _not_manageable(standing: Standing) -> str:
    """Why the license in use cannot manage the Enterprise settings, in words that quote nothing of the file."""
    if standing.state is State.PAUSED:
        why = "the Latet Enterprise license is paused (past the 30 days of grace)"
        until = "until a license that covers them is installed"
    elif standing.state is State.INVALID:
        why = "the license file cannot be used (`latet server license show` says why)"
        until = "until it is replaced"
    elif standing.state is State.NONE:
        why = "there is no Latet Enterprise license"
        until = "until one is installed"
    else:
        why = "the Latet Enterprise license cannot manage them now"
        until = "until it can"
    return f"{why}: the Enterprise settings can be read, not changed, {until}; protection is unchanged"
