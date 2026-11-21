"""The Company approvals page's API (Latet Enterprise, E5) and, from Task 8, who decides for `latet server approvals`:
E3's EeRoutes, served by ``dashboard_route`` behind E3's gate (one RULES row: the routes check who decides
themselves) and E1's gates by kind (the lists ``manage``: in every standing of a license that lists approvals, a
trial's end included; decisions, and the texts and images read to take them, ``serve``: refused once a trial ended, a
pause deciding on). A decision is weighed and recorded in
E3's change transaction (the license as it finds it, the company sign-in of decision (c), who may decide past every
copy, the entry in the administration log), then carried out in the asking identity's space on this instance: an entry
that cannot be written decides nothing."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Final, TypeVar

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.api.rest import image_response
from latet.ee.approvals import APPROVED_UNAPPLIED, own
from latet.ee.routes import VIA, EeRoute, Refused, json_body
from latet.ee.server import DatabaseSettings, settings_in
from latet.ee.settings import EnterpriseSettingsError
from latet.events import code_of
from latet.floor import AccountUnavailable, BelowFloor, FloorUnavailable, unavailable
from latet.gateway.approvals import Changed, MaskError, batch_version
from latet.logline import Paced
from latet.server.spaces import SpaceUnavailable

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.ee.approvals import CentralApprovals, Row
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.people import Changing
    from latet.ee.routes import Who
    from latet.ee.settings import EnterpriseSettings
    from latet.server.app import Server
    from latet.state import IdentityRef

T = TypeVar("T")
FEATURE: Final = "approvals"
NOT_YOURS_TO_SEE: Final = "only the people who decide on it see it and decide on it"
YOURS: Final = "you asked for it: someone else decides on it"
STALE: Final = "their settings changed since: it was dropped, and they must ask for it again"
GONE: Final = "it no longer waits: someone else decided, or it was withdrawn"
UNREACHED: Final = "the space it waits in cannot be reached on this instance now: try again"
UNKEPT: Final = "their settings could not be kept now: nothing was decided; try again"
UNRECORDED: Final = "the decision could not be kept in the administration log: nothing was decided; try again"
UNLOGGED: Final = "opening it could not be kept in the administration log: nothing of it was shown; try again"
# A decider's first opening of another person's text, its words or an image of it (the final security review's N2, the
# owner's decision): the security role's act, named as its decisions are (``events.admin.PERMISSIONS``).
OPENED: Final = "approval.opened"
_SKEW: Final = 3600.0  # seconds the instances' clocks may be apart, at most, when an opening is looked for
NOT_OUR_PAGE: Final = "a text opens only from this server's own pages"
UNAPPLIED: Final = (
    "approved, but their settings changed meanwhile on another instance and did not take it: they ask again"
)
UNEXPECTED: Final = "the decision ended in a way this server does not expect: look at what was decided lately"
# The review of Task 10, I1: a decider approves a text only as they read it (its `version`).
CHANGED: Final = "it was masked again since you read it: nothing was decided; read it again, then decide"
UNVERSIONED: Final = "an approval of a text names the version of it you read (`version`, as reading it gives it)"
TEXT_SIGN_IN: Final = "sign in with your company account to open or decide on what someone else's agent sends"
UNOPENED: Final = (
    "texts go to the security team only where its people open them with the company's sign-in: set it up first, with "
    "company changes asked from it (latet server sign-in set --company-changes sso)"
)
WHO_DECIDES: Final = ("changes", "live_review")  # what `latet server approvals set` changes: the section's two keys
# Each refusal and each decision not taken carries a code beside its English words (the review of Task 7, m5): the
# dashboard says it by its code, in each of its languages.
_REFUSALS: Final = {
    "yours": (403, YOURS), "not-yours": (403, NOT_YOURS_TO_SEE), "gone": (409, GONE), "changed": (409, CHANGED),
}  # fmt: skip
_NOT_TAKEN: Final = {"stale": STALE, "gone": GONE, APPROVED_UNAPPLIED: UNAPPLIED}
log = logging.getLogger(__name__)
_UNRECORDED = Paced()


def _refused(why: str) -> Refused:
    status, message = _REFUSALS[why]
    return Refused(status, message, code=why)


def _decided(state: str) -> Response:
    """A decision's answer: how it ended (``approved``, ``rejected``), or why it was not taken (409, its code the
    state). An end no answer expects is never said as done (the review of Task 7, m6): 500 in fixed words."""
    if state in ("approved", "rejected"):
        return JSONResponse({"ok": True, "state": state})
    words = _NOT_TAKEN.get(state)
    if words is not None:
        return JSONResponse({"error": words, "code": state}, status_code=409)
    log.warning("a decision on approvals ended as %r, which no answer expects", code_of(state))
    return JSONResponse({"error": UNEXPECTED, "code": "unexpected"}, status_code=500)


def action_of(row: Row, capacity: str) -> str:
    """The administration log's action of a decision on ``row`` taken as ``capacity`` (R15, R47): the security
    team's (and a withdrawal of one); an API key's controllers' (and a withdrawal of one); an admin's on a key nobody
    else controls, an administrator's power that E4 names in the SIEM (``keys.manage``)."""
    if capacity == "admin":
        return "key.approval.admin-decided"
    return "approval.decided" if row.route == "security" else "key.approval.decided"


def texts_open(settings: EnterpriseSettings) -> bool:
    """[D6]: whether the security team can open a text on this server: single sign-on set up, and its settings asking
    it for what applies to everyone (``company_changes: sso``), so that no session `latet dashboard`'s code opens is
    the only way in."""
    return settings.sso is not None and settings.sso.company_changes == "sso"


def not_routing(server: Server) -> list[str]:
    """In a worker thread, once who decides is set: the problem said while instances alive say another release or
    edition than this one's (``instances_said``, as `latet server policy set` says which do not apply the policy, the
    whole-branch security review's L5). One of a release before central approvals, left running across the migration,
    routes nothing to whoever decides and lets a person approve their own change there; one without the Enterprise
    Edition routes nothing. A listing cut at its bound says "at least", or that one it does not show may not
    (``some_instances``). Valkey not answering says nothing (logged by the error's type): who decides is set all the
    same."""
    from latet import __version__
    from latet.server.app import instances_said, some_instances

    this = f"{__version__} {server.edition.name}"
    try:
        listed, whole = instances_said(server)
    except Exception as exc:  # its words can name Valkey's address
        log.warning("whether every instance routes approvals could not be read (%s)", type(exc).__name__)
        return []
    count = sum(1 for said in listed if said != this)
    if not count and whole:
        return []
    some = some_instances(count, whole)
    return [
        f"{some} not run this release of Latet with the Enterprise Edition: one of a release before central approvals "
        "routes nothing to whoever decides, and lets a person approve their own change there; stop it, or replace it "
        "with this release"
    ]


def has_api_keys(conn: Any) -> bool:
    """Whether the server has an API key, enabled or not, as ``conn`` finds it: with central approvals and ``changes:
    self``, a key's change that lowers protection goes to another person who controls it (D2), so a license change
    that keeps `roles` and drops approvals ends that (the final security review's L1': ``latet.ee.admin``'s
    ``_decided_by_others``, and `latet server license install`, which reads it here as ``api_keys``)."""
    return conn.execute("SELECT 1 FROM identities WHERE kind = 'key' LIMIT 1").fetchone() is not None


def opened_before(conn: Connection[Any], actor: str, row: Row) -> bool:
    """In an opening's transaction: whether ``actor`` opened ``row``'s text before, as the administration log has it
    (since the text began to wait, give or take ``_SKEW``). The openings of one decider and one text are taken one at a
    time (a lock of the transaction's, by both), so that the words and the images the page reads together find each
    other's entry and write one."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"latet:{OPENED}:{row.id}:{actor}",))
    found = conn.execute(
        "SELECT 1 FROM audit WHERE action = %s AND ts >= %s AND actor = %s AND target = %s AND details->>'item' = %s "
        "LIMIT 1",
        (OPENED, row.created - _SKEW, actor, row.identity_id, row.id),
    ).fetchone()
    return found is not None


def _from_our_pages(request: Request) -> None:
    """The re-review of the fix rounds, R2: a text or its image opens, and its opening is written, only for a request of
    the server's own pages (``Sec-Fetch-Site: same-origin``, the dashboard's fetch and its ``<img>`` alike), or one that
    says nothing of where it comes from (a client with no fetch metadata). Another site's page (the server's door
    refuses those already: ``Server._from_elsewhere``) and a link opened from outside the page (``none``) open
    nothing: an opening is written from a GET that no header of the dashboard's guards."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.lower() != "same-origin":
        raise Refused(403, NOT_OUR_PAGE, code="not-our-page")


def _company_sign_in(request: Request) -> None:
    """[D6] (the plan review's H1): another person's text, values included, is opened from a dashboard session the
    company's sign-in opened, never one `latet dashboard`'s one-time code opened (an agent at the computer can ask for
    one), whatever the sign-in settings say."""
    if request.scope.get(VIA) != "sso":
        raise Refused(403, TEXT_SIGN_IN, code="company-sign-in")  # E3's code: the page offers the company's sign-in


class ApprovalsApi:
    """The deciders' routes (``/_latet/api/v1/ee/approvals…``), for the people who decide something: the security team,
    a key's controllers, the admins; 403 to anyone else."""

    def __init__(self, edition: EnterpriseEdition, server: Server, central: CentralApprovals) -> None:
        self.edition = edition
        self.server = server
        self.central = central

    def routes(self) -> list[EeRoute]:
        manage: dict[str, Any] = {"kind": "manage", "feature": FEATURE, "admin": False}
        serve: dict[str, Any] = {"kind": "serve", "feature": FEATURE, "admin": False}
        return [
            EeRoute("GET", "/approvals", self.queue, **manage),
            EeRoute("GET", "/approvals/count", self.count, **manage),
            EeRoute("GET", "/approvals/history", self.history, **manage),
            # A text is read to decide on it: refused once a trial ended, as deciding is (R61).
            EeRoute("GET", "/approvals/batches/{id}", self.batch, **serve),
            EeRoute("GET", "/approvals/batches/{id}/images/{item}", self.image, **serve),
            EeRoute("POST", "/approvals/changes/{id}", self.decide_change, **serve),
            EeRoute("POST", "/approvals/batches/{id}", self.decide_batch, **serve),
            # Who decides, set by the administration alone [D3] (`latet server approvals`): E5's own route, as E4's SIEM
            # and reports have theirs (the generic settings route refuses the section: BY_APPROVALS).
            EeRoute("GET", "/approvals/settings", self.show_settings, kind="manage", feature=FEATURE, dashboard=False),
            EeRoute("PUT", "/approvals/settings", self.set_settings, kind="manage", feature=FEATURE, dashboard=False),
        ]

    async def show_settings(self, request: Request, who: Who) -> Response:
        """Who decides, and whether the server has an API key (``api_keys``: its changes go to its other controllers
        while ``changes`` is ``self``), which `latet server license install` weighs (L1')."""
        settings = await who.run(self.edition.settings)  # in every standing: once a trial ended, as it stood
        keys = await who.run(self._has_api_keys)
        return JSONResponse({"approvals": settings.approvals.model_dump(mode="json"), "api_keys": keys})

    def _has_api_keys(self) -> bool:
        with self.server.db.connection() as conn:
            return has_api_keys(conn)

    async def set_settings(self, request: Request, who: Who) -> Response:
        """Who decides, changed with its entry (``approvals.changed``: the fields changed and the section as it is
        now) in one transaction, under the settings' lock, every instance told once it commits. Texts go to the
        security team only where the company's sign-in opens them [D6]: refused (409) otherwise, as the sign-in's
        settings stand in that transaction. The answer's ``problems`` say the instances alive that run another release
        or edition (``not_routing``)."""
        body = await json_body(request, who)
        if not body or set(body) - set(WHO_DECIDES):
            raise Refused(400, "give changes, live_review or both, each self or security")
        store = self.edition.settings_store
        if not isinstance(store, DatabaseSettings):  # a server's edition keeps them in PostgreSQL
            raise Refused(503, "the Enterprise settings are a server's")

        def changed(within: Changing) -> dict[str, Any]:
            # Every other change of the Enterprise settings, on any instance, waits for this one from here, and this one
            # for any under way (the sign-in's, another administrator's), as each takes it (the review of Task 8, I1):
            # what it reads next is what it changes. Reads never wait.
            within.conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            held = settings_in(within.conn)  # in the change's transaction: the sign-in's settings as they stand
            if body.get("live_review") == "security" and not texts_open(held):
                raise Refused(409, UNOPENED)  # [D6]: nobody could open a text, so none would ever be decided
            before = held.approvals
            after = store.set_section(
                "approvals", {**before.model_dump(mode="json"), **body}, actor=within.actor, address=within.address,
                conn=within.conn,
            ).approvals  # fmt: skip
            fields = sorted(key for key in WHO_DECIDES if getattr(before, key) != getattr(after, key))
            within.note("approvals.changed", "approvals", {"fields": fields, **after.model_dump(mode="json")})
            within.after.append(lambda: store.changed("approvals"))  # every instance told once it commits
            return after.model_dump(mode="json")

        try:
            approvals = await who.change(changed)
        except EnterpriseSettingsError as exc:  # by field and kind of error, never what was given
            raise Refused(400, f"not changed: {exc}") from None
        return JSONResponse({"approvals": approvals, "problems": await who.run(not_routing, self.server)})

    async def _decider(self, who: Who) -> IdentityRef:
        identity_id = who.identity_id
        found = await who.run(self.central.decider, identity_id) if isinstance(identity_id, str) else None
        if found is None:
            raise _refused("not-yours")
        return found

    async def _row(self, who: Who, me: IdentityRef, item_id: str, kind: str, approve: bool | None) -> Row:
        row, why = await who.run(self.central.admitted, None, me.id, item_id, kind, approve)
        if row is None:
            raise _refused(why)
        return row

    async def _reached(self, work: Awaitable[T]) -> T:
        """``work``, which reaches the asking identity's space on this instance: what stops it is answered."""
        try:
            return await work
        except SpaceUnavailable:  # NotListening too: no space is built or handed out on this instance now
            raise Refused(503, UNREACHED, code="unreached") from None
        except FloorUnavailable as exc:  # the company's policy cannot be read here: nothing is decided below it
            code = "account-unavailable" if isinstance(exc, AccountUnavailable) else "floor-unavailable"
            raise Refused(503, unavailable(exc), code=code) from None
        except OSError:  # their settings' store did not keep them: none of it runs
            raise Refused(503, UNKEPT, code="unkept") from None

    def _weighed(self, within: Changing, me: IdentityRef, item_id: str, kind: str, approve: bool) -> tuple[Row, str]:
        """In the decision's transaction (after the license and the session, ``dashboard_route``): who may decide, past
        every copy, and the entry, written last in it. The entry names the asking identity and changes no person: it
        leaves People's copies on every instance alone (R38)."""
        row, capacity = self.central.admitted(within.conn, me.id, item_id, kind, approve)
        if row is None:
            raise _refused(capacity)
        details: dict[str, Any] = {"kind": kind, "decision": "approve" if approve else "reject", "route": row.route}
        if capacity == "own":
            details["withdrawn"] = True  # whoever asked stops it: the entry is the decision taken, the record its end
        elif capacity == "admin":
            details["as"] = "admin"
        within.note(action_of(row, capacity), row.identity_id, details)
        within.people = False
        return row, capacity

    async def _recorded(self, who: Who, me: IdentityRef, item_id: str, kind: str, approve: bool) -> tuple[Row, str]:
        """The decision weighed and its entry written (``Who.change``), or why not: a refusal as it is, anything else
        (the entry not written) as 503 in fixed words, said by its type (R50)."""
        try:
            return await who.change(lambda within: self._weighed(within, me, item_id, kind, approve))
        except Refused:
            raise
        except Exception as exc:  # its words can name the database's address
            if count := _UNRECORDED.due():
                log.warning(
                    "a decision on approvals was not recorded (%s): nothing was decided%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            raise Refused(503, UNRECORDED, code="unrecorded") from None

    async def queue(self, request: Request, who: Who) -> Response:
        me = await self._decider(who)
        return JSONResponse(await who.run(self.central.queue, me))

    async def count(self, request: Request, who: Who) -> Response:
        me = await self._decider(who)
        return JSONResponse({"waiting": await who.run(self.central.count, me)})

    async def history(self, request: Request, who: Who) -> Response:
        me = await self._decider(who)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return JSONResponse({"items": await who.run(self.central.history, me, limit)})

    async def _opened(self, who: Who, me: IdentityRef, row: Row) -> None:
        """N2 (the final security review, the owner's decision): a decider's opening of another person's text, its
        words or an image of it, kept in the administration log before anything of it is shown, in a transaction of
        its own (the license weighed there, as for a decision): one entry (``OPENED``) for each decider and each text,
        naming them and whose text it is, with no text and no image in it (``opened_before``); none for whoever asked.
        An opening that cannot be written shows nothing (503), as a decision whose entry cannot be written is not
        carried out (R50)."""
        if own(row, me.id):
            return

        def opening(within: Changing) -> None:
            within.people = False  # it names a person and changes nothing of them (R38)
            if not opened_before(within.conn, within.actor, row):
                # OPENED as a literal, so what the change touches can be read from it (tests/ee/people/test_people.py)
                within.note("approval.opened", row.identity_id, {"kind": row.kind, "item": row.id, "route": row.route})

        try:
            await who.change(opening)
        except Refused:
            raise
        except Exception as exc:  # its words can name the database's address
            if count := _UNRECORDED.due():
                log.warning(
                    "opening a text on Company approvals was not recorded (%s): nothing of it was shown%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            raise Refused(503, UNLOGGED, code="opening-unrecorded") from None

    async def batch(self, request: Request, who: Who) -> Response:
        _from_our_pages(request)  # R2, before anything is read
        _company_sign_in(request)  # [D6], before anything is read
        me = await self._decider(who)
        row = await self._row(who, me, request.path_params["id"], "batch", None)
        found = await self._reached(self.central.batch(row, who.run))
        if found is None:
            raise _refused("gone")
        await self._opened(who, me, row)  # N2: kept before it is shown
        asked = await who.run(self.central.asked, row, own(row, me.id))
        # Its version: what an approval of it names, refused once it was masked again since (I1). Worked out off the
        # event loop: a large text's digest costs milliseconds (the re-review of fix round 1, n2).
        version = await who.run(batch_version, found.items)
        return JSONResponse({**found.summary(), **asked, "version": version})

    async def image(self, request: Request, who: Who) -> Response:
        _from_our_pages(request)  # R2
        _company_sign_in(request)  # [D6]
        me = await self._decider(who)
        try:
            item = int(request.path_params["item"])
        except ValueError:
            raise Refused(404, "not found") from None
        row = await self._row(who, me, request.path_params["id"], "batch", None)
        sent = request.query_params.get("view") == "sent"
        found = await self._reached(self.central.image(row, item, sent, who.run))
        if found is not None:
            await self._opened(who, me, row)  # N2: an image opens the text too, kept once with its words
        return await image_response(found, who.run)

    async def decide_change(self, request: Request, who: Who) -> Response:
        action = (await json_body(request, who)).get("action")
        if action not in ("approve", "reject"):
            raise Refused(400, "action is approve or reject")
        approve, item_id = action == "approve", request.path_params["id"]
        me = await self._decider(who)
        row, capacity = await self._recorded(who, me, item_id, "change", approve)
        by = None if capacity == "own" else me  # whoever asked, undoing it, withdraws it
        try:
            state = await self._reached(self.central.carry_change(row, approve, by, run=who.run))
        except BelowFloor as refused:  # approved below the floor of now: rejected, and the floor says why
            body = {"error": str(refused), "floor": refused.reasons, "code": "below-floor"}
            return JSONResponse(body, status_code=409)
        return _decided(state)

    async def decide_batch(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        action, masks, images = data.get("action"), data.get("masks") or [], data.get("images") or []
        if action not in ("approve", "reject") or not isinstance(masks, list) or not isinstance(images, list):
            raise Refused(400, "action is approve or reject; masks and images lists")
        approve, item_id = action == "approve", request.path_params["id"]
        _company_sign_in(request)  # [D6]: deciding a text is reading it
        me = await self._decider(who)
        version = data.get("version")
        if approve:  # the text as it was read, and its masks, checked before anything is recorded
            if not isinstance(version, str) or not version:
                raise Refused(400, UNVERSIONED)
            checked = await self._row(who, me, item_id, "batch", True)
            try:
                await self._reached(self.central.check(checked, masks, images, who.run, version=version))
            except Changed:
                raise _refused("changed") from None
            except MaskError as exc:
                raise Refused(422, str(exc)) from None
        row, capacity = await self._recorded(who, me, item_id, "batch", approve)
        by = None if capacity == "own" else me
        carried = self.central.carry_batch(
            row, approve, masks, images, by, run=who.run, version=version if approve else None
        )
        try:
            state = await self._reached(carried)
        except Changed:
            # Masked again between the check and the decision (another instance's space made its previews anew): it
            # still waits, nothing decided. The entry written stands as the decision taken (R43), as below.
            raise _refused("changed") from None
        except MaskError as exc:
            # The text was shown anew between the check and the decision: its mask no longer fits, and the text still
            # waits. The entry written stands as the decision taken (R43), as an approval its space did not take does.
            raise Refused(422, str(exc)) from None
        return _decided(state)
