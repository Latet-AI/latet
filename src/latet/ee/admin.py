"""The Enterprise Edition's part of a server's administration API, under /_latet/server/v1/admin/ee/: the license
(shown, installed, removed) and the Enterprise settings (a section set, or put back to its defaults; the sign-in's
through its own routes, ee/sign_in_api.py).

Each route is served behind the administration's own gate (``latet.server.admin.AdminGate``, E0b's): an
administration token (x-latet-admin), over HTTPS when the server's address is https and never from a web page, with
the same limiter of lookups, ceiling of refused entries, threads and log as the server's other administration routes,
and no second copy of any of it. Every call is in the administration log under its route's name, reads and refusals
too. A change is written in one transaction with its entry (``license.installed``, ``license.removed``,
``<section>.changed``, by ``admin:<token id>``): the call's token's row held first, the entry last, so that both are
kept or neither, and a change whose entry cannot be written is not made and not answered as done. Every instance is
told once it commits.

The license answers in every state, a trial that ended included: there a license is installed, or the trial's file
removed. The Enterprise settings are read in every state, and changed only while the license can manage them (403,
``code: "trial-ended"`` once a trial ended, ``"not-manageable"`` otherwise), nothing changed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from latet.ee.api import MAX_BODY, change_refused, error, json_body, license_of, read_body, refusal
from latet.ee.edition import license_details
from latet.ee.license import LicenseError, details, not_a_license
from latet.ee.server import DatabaseSettings
from latet.ee.settings import EnterpriseSettingsError, UnknownSection, section_value
from latet.server.identities import Changes

if TYPE_CHECKING:
    from starlette.requests import Request

    from latet.ee.edition import EnterpriseEdition
    from latet.ee.license import License
    from latet.ee.settings import EnterpriseSettings
    from latet.server.admin import AdminGate, Endpoint, Handler, _Call
    from latet.server.app import Server

ADMIN_EE: Final = "/_latet/server/v1/admin/ee"
MAX_SECTION: Final = 64 * 1024  # a section's settings, as a JSON object
NO_SECTION: Final = "there is no Enterprise setting of that name"  # the name a path gives is never said back
NOT_A_SECTION: Final = "the body is the section's settings, as a JSON object"
# E3: the sign-in's section is set through its own routes (ee/sign_in_api.py), which check the issuer, keep what only
# `latet server` sets, apply the groups' mapping and record it, all in one transaction.
BY_SIGN_IN: Final = "the sign-in's settings are set with `latet server sign-in set` (and turned off with `off`)"
# E4: the SIEM outputs and the reports' settings are set through their own routes (ee/events/api.py), which check what
# an output may hold and where it may connect, keep its secret apart, and record the change (R11).
BY_SIEM: Final = (
    "the SIEM outputs are set with `latet server siem set` (and their secrets with `latet server siem secret`)"
)
BY_REPORTS: Final = "the reports' settings are set with `latet server reports settings`"
# E5: who decides on approvals is set through its own route (ee/approvals_api.py), which checks the license's
# `approvals` and records which fields changed (R9).
BY_APPROVALS: Final = "who decides on approvals is set with `latet server approvals set`"
# E6: term sync's sources are set through their own routes, which check where each source may connect, keep its secret
# apart, bound to where it goes and what it reads, and record the change: never through the generic section's route.
BY_TERM_SYNC: Final = "term sync's sources are set with `latet server terms sources set`"
BY_OWN_ROUTES: Final = {
    "sso": BY_SIGN_IN,
    "siem": BY_SIEM,
    "reports": BY_REPORTS,
    "approvals": BY_APPROVALS,
    "term_sync": BY_TERM_SYNC,
}


def admin_endpoint(gate: AdminGate, name: str, handler: Handler) -> Endpoint:
    """``handler`` behind the administration's own gate, every call recorded under ``name``: what every Enterprise
    administration route uses (E2's too). The handler is given the request and its call, whose ``actor`` is
    ``admin:<token id>``; a change it makes goes through ``gate.holding``, ``gate.entry`` and ``gate.recorded``."""
    return gate.admin(name, handler)


class _Refused(Exception):
    """A change the license refuses, as the change's own transaction finds it: ``response`` says why."""

    def __init__(self, response: Response) -> None:
        super().__init__("refused by the license")
        self.response = response


def _ends_the_policy(
    edition: EnterpriseEdition, conn: Any, before: frozenset[str], after: License | None
) -> dict[str, Any]:
    """In the change's transaction, what the change stops of what protects (its callers' 409 and entries carry it, and
    the confirmation that lets the change through covers each). ``before``: the features what protects kept before the
    change, read in that transaction before it was written (``EnterpriseEdition.held_features``: a file this instance
    cannot verify keeps its installer's, the final security review's L2); ``after``: the license the change leaves,
    None once removed. ``{"policy": "stops"}`` when a policy is stored, ``before`` lists locked rules and ``after`` does
    not, so that the server's policy stops being in force (L4); ``{"terms": "stop"}`` when synced terms are kept (a row
    of ee_terms) and ``before`` lists term sync while ``after`` does not, so that every synced term stops being masked
    (R29); ``{"approvals": "stop"}`` when ``before`` lists approvals, ``after`` does not, and someone other than
    whoever asks decides now (``_decided_by_others``: the security team, or an API key's other controllers while
    ``after`` keeps the key's space open), so that whoever asks decides again (the final security review's L1, L1').
    Nothing of what was not in force before."""
    from latet.ee.approvals_api import FEATURE as APPROVALS
    from latet.ee.policy import FEATURE
    from latet.ee.policy_server import ServerPolicy
    from latet.ee.sync.service import FEATURE as TERM_SYNC

    def ends(feature: str) -> bool:
        return feature in before and (after is None or feature not in after.features)

    stops: dict[str, Any] = {}
    policy = edition.policy
    if ends(FEATURE) and isinstance(policy, ServerPolicy) and policy.stored_in(conn):
        stops["policy"] = "stops"
    if ends(TERM_SYNC) and conn.execute("SELECT 1 FROM ee_terms LIMIT 1").fetchone() is not None:
        stops["terms"] = "stop"
    if ends(APPROVALS) and _decided_by_others(conn, after):
        stops["approvals"] = "stop"
    return stops


def _to_the_security_team(conn: Any) -> bool:
    """Whether who decides, as the change's transaction finds it (ee_settings' approvals row), sends changes or the
    Live Reviewer's texts to the security team. A row this release cannot read is taken as sending them: the change
    is asked about rather than made unseen."""
    from pydantic import ValidationError

    from latet.ee.settings import ApprovalsSettings

    row = conn.execute("SELECT value FROM ee_settings WHERE section = 'approvals'").fetchone()
    try:
        approvals = ApprovalsSettings() if row is None else ApprovalsSettings.model_validate(row[0])
    except ValidationError:
        return True
    return "security" in (approvals.changes, approvals.live_review)


def _decided_by_others(conn: Any, after: License | None) -> bool:
    """Whether, with central approvals, someone other than whoever asks decides, as the change's transaction finds
    it: the security team (``_to_the_security_team``), or, the final security review's L1', an API key's other
    controllers, while the license left lists `roles` (a key's space stays open, where whoever works approves their
    own change once approvals go) and the server has an API key. A license left without `roles` closes every key's
    space: nothing of a key stops there."""
    from latet.ee.approvals_api import has_api_keys

    if _to_the_security_team(conn):
        return True
    return after is not None and "roles" in after.features and has_api_keys(conn)


def _section_of(body: bytes) -> dict[str, Any] | None:
    """In a worker thread: ``body`` as a JSON object; None for anything else (not UTF-8, not JSON, NaN or an infinity
    anywhere in it, nested deeper than Python reads, a list, a text, a number, ``null``), which is refused, never read
    as the section's defaults."""
    try:
        found = json_body(body)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def admin_routes(edition: EnterpriseEdition, server: Server, gate: AdminGate) -> list[BaseRoute]:
    """The license and the Enterprise settings, for the server's administrators."""
    store = edition.settings_store
    if not isinstance(store, DatabaseSettings):  # a server's edition keeps them in PostgreSQL
        raise TypeError("the Enterprise administration is a server's: its settings are kept in PostgreSQL")

    # --- the license --------------------------------------------------------------------------------------------------

    async def show_license(request: Request, call: _Call) -> Response:
        standing = await gate.run(edition.standing)  # a database that does not answer is said, never raised
        return JSONResponse(details(standing))

    def installed(call: _Call, data: bytes) -> License:
        """In a worker thread: ``data`` checked and kept as the server's license, with its entry, in one transaction;
        every instance told once it commits. LicenseError, nothing changed."""
        with server.db.transaction() as conn:
            gate.holding(call, conn)  # first: a token gone or past its day changes nothing
            before = edition.held_features(conn)  # what protects keeps of the one this replaces, its row held
            lic = edition.install_license(data, conn=conn)
            details = license_details(lic) | _ends_the_policy(edition, conn, before, lic)
            gate.entry(call, Changes(conn), "license.installed", details, target=lic.id)  # last
        gate.recorded(call, "license.installed", details, lic.id)
        edition.committed("license.installed")
        return lic

    def removed(call: _Call) -> bool:
        """In a worker thread: the server's license removed, with its entry naming the one removed, in one transaction;
        every instance told once it commits. Whether there was one."""
        with server.db.transaction() as conn:
            gate.holding(call, conn)  # first: a token gone or past its day changes nothing
            lic = edition.held_license(conn)  # the one this transaction removes, its row held
            before = edition.held_features(conn)  # what protects keeps of it, a file that does not verify's too
            if not edition.remove_license(conn=conn):
                return False
            details = license_details(lic) | _ends_the_policy(edition, conn, before, None)
            target = lic.id if lic is not None else None
            gate.entry(call, Changes(conn), "license.removed", details, target=target)  # last
        gate.recorded(call, "license.removed", details, target)
        edition.committed("license.removed")
        return True

    async def change_license(request: Request, call: _Call) -> Response:
        if request.method == "DELETE":
            if not (done := await gate.run(removed, call)):
                call.details = {"removed": False}
            standing = await gate.run(edition.standing)
            return JSONResponse({**details(standing), "removed": done})
        body = await read_body(request, MAX_BODY)
        if isinstance(body, Response):
            return error("too large for a license file", 413)
        if isinstance(text := await gate.run(license_of, body), Response):
            return text
        try:
            try:
                data = text.encode("utf-8")
            except UnicodeEncodeError:  # a lone surrogate, sent as JSON's escape of one: no license file holds it
                raise not_a_license("it holds text that is not Unicode") from None
            await gate.run(installed, call, data)
        except LicenseError as exc:  # its words never quote the file
            call.details = {"code": exc.code}
            return error(str(exc), 400, code=exc.code)
        return await show_license(request, call)

    # --- the Enterprise settings --------------------------------------------------------------------------------------

    async def show_settings(request: Request, call: _Call) -> Response:
        # In every state: once a trial ended, as they stood at its end.
        settings: EnterpriseSettings = await gate.run(edition.settings)
        return JSONResponse(settings.model_dump(mode="json"))

    def changed(call: _Call, name: str, value: dict[str, Any] | None) -> EnterpriseSettings:
        """In a worker thread: the section ``name`` set to ``value`` (None: its defaults), with its entry, in one
        transaction; every instance told once it commits. UnknownSection or EnterpriseSettingsError, nothing changed;
        ``_Refused`` when the license, as that transaction finds it (its row held there until it ends), cannot manage
        them: a license removed, or a trial that ended, meanwhile on any instance."""
        with server.db.transaction() as conn:
            gate.holding(call, conn)  # first: a token gone or past its day changes nothing
            if (refused := refusal(edition.licensing.standing_in(conn))) is not None:
                raise _Refused(refused)
            settings = store.set_section(name, value, actor=call.actor, address=call.address, conn=conn)
            done, details = f"{name.replace('_', '-')}.changed", {"section": name}
            gate.entry(call, Changes(conn), done, details, target=name)  # last
        gate.recorded(call, done, details, name)
        store.changed(name)
        return settings

    async def change_section(request: Request, call: _Call) -> Response:
        if (by := BY_OWN_ROUTES.get(request.path_params["section"])) is not None:  # F9, R11: their own routes' alone
            return error(by, 409)
        # Said before the body is read, as this instance sees the license; checked again in the change's transaction.
        if (refused := await gate.run(change_refused, edition.licensing)) is not None:
            return refused
        value: dict[str, Any] | None = None
        if request.method == "PUT":
            body = await read_body(request, MAX_SECTION)
            if isinstance(body, Response):
                return body
            if (value := await gate.run(_section_of, body)) is None:
                return error(NOT_A_SECTION, 400)
        name = request.path_params["section"]
        try:
            settings = await gate.run(changed, call, name, value)
        except _Refused as refused_there:
            return refused_there.response
        except UnknownSection:
            return error(NO_SECTION, 404)
        except EnterpriseSettingsError as exc:  # by field and kind of error, never what was given
            return error(f"not changed: {exc}", 400)
        return JSONResponse({name: section_value(settings, name)})

    return [
        Route(f"{ADMIN_EE}/license", admin_endpoint(gate, "ee.license.read", show_license), methods=["GET"]),
        Route(
            f"{ADMIN_EE}/license", admin_endpoint(gate, "ee.license.change", change_license), methods=["PUT", "DELETE"]
        ),
        Route(f"{ADMIN_EE}/settings", admin_endpoint(gate, "ee.settings.read", show_settings), methods=["GET"]),
        Route(
            f"{ADMIN_EE}/settings/{{section}}",
            admin_endpoint(gate, "ee.settings.change", change_section),
            methods=["PUT", "DELETE"],
        ),
    ]
