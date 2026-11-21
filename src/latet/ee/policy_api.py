"""The policy set and removed from the Policy page by the security role (E3): E2's ServerPolicy behind the gate of
ee/access.py (policy.edit), the person signed in as the actor, as E2's administration route sets it
(latet.ee.policy_routes): the body read as E2 reads it, the text read before the change's transaction (its rows held
for the writes alone), a file with keys left out whole set only when the body says they were seen, then one
transaction with the policy.changed entry, the license (locked-rules, and that it can manage) checked there (E2's R1,
R5; ee/routes.py's ``held``), and every instance told once it commits (E2's ``committed``). E2's GET /ee/policy stays
the floor every person reads (counts and names), and its administration routes stay `latet server policy`'s.

A save names the etag of the policy its text replaces, and a removal the etag of the one it removes (the one GET
/ee/policy/text answered, or none): checked in the change's transaction, every other change of the policy held off there
until it commits, so that nobody's change made meanwhile, from the page or from `latet server policy set`, is
overwritten or removed unseen (409, nothing changed).

The policy applies to everyone, and its text holds the company's terms, values and patterns in clear: while the
sign-in settings say ``company_changes: sso``, its change and the read of its text take a session opened with the
company's sign-in (the user's decision (c): a one-time code of `latet dashboard`, which an agent at the person's
computer can ask for, opens neither), the change through ee/routes.py's ``dashboard_route``, the read here, under the
sign-in settings as PostgreSQL has them (never this instance's copy, which may be seconds behind). E2's view
of the floor, which holds no value, is read from any session. An API key's space reaches none of these routes
(ee/access.py)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.ee.api import error, json_body, read_body
from latet.ee.people import Changing
from latet.ee.policy import FEATURE, PolicyError
from latet.ee.policy_routes import CHANGED, MOST_REMOVAL, UNREAD, _etag, _not_applying, _text_of, _unread, viewed
from latet.ee.policy_server import ServerPolicy
from latet.ee.routes import VIA, EeRoute, Refused, Who, company_sign_in
from latet.ee.server import settings_in
from latet.server.machines import QUEUED_RETRY_AFTER
from latet.transfer import KEYS, MAX_BYTES

if TYPE_CHECKING:
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.policy import Floor
    from latet.server.app import Server

log = logging.getLogger(__name__)

# Fixed words: a refusal never quotes what the request sent.
READ_BY_COMPANY_SIGN_IN: Final = "sign in with your company account to read the policy's text"
NO_ETAG: Final = (
    'the body is {"policy": "<the policy file\'s text>", "etag": "<the etag of the policy it replaces>"}, the etag '
    "null when no policy is stored"
)
NO_REMOVAL_ETAG: Final = (
    'the body is {"etag": "<the etag of the policy it removes>"}, as the policy\'s text gave it, null when none is '
    "stored"
)


def _change_of(body: bytes) -> tuple[str, bool, str | None] | Response:
    """In a worker thread: E2's reading of the body (``_text_of``: the text and ``accept_notices``), and the etag of
    the policy the text replaces, which the body must name (a string, or null when none is stored); else the 400
    that refuses it."""
    given = _text_of(body)
    if isinstance(given, Response):
        return given
    found = json_body(body)  # an object holding a text: _text_of read it so
    etag = found.get("etag")
    if "etag" not in found or not _etag(etag):
        return error(NO_ETAG, 400)
    return given[0], given[1], etag


def _removal_of(body: bytes) -> str | Response | None:
    """In a worker thread: the etag of the policy a removal takes away, which the body must name as the save names the
    one it replaces (a string, or null when none is stored); else the 400 that refuses it, quoting nothing."""
    try:
        found = json_body(body or b"{}")
    except (ValueError, RecursionError):
        found = None
    if not isinstance(found, dict) or "etag" not in found or not _etag(found["etag"]):
        return error(NO_REMOVAL_ETAG, 400)
    return found["etag"]


class PolicyApi:
    """The routes of the Policy page's editor, served to the dashboard only (ee/routes.py). E2's checks apply as they
    do to `latet server policy set`: a text the server cannot read changes nothing, keys left out whole are never set
    unseen, and the license must allow the change; each change is E2's policy.changed in the administration log, by
    the person, in the change's transaction."""

    def __init__(self, edition: EnterpriseEdition, server: Server) -> None:
        self.edition = edition  # its policy (E2's, made after E3's objects) is read at each request
        self.server = server

    def routes(self) -> list[EeRoute]:
        return [
            EeRoute("GET", "/policy/text", self.text, feature=FEATURE, admin=False),
            EeRoute("PUT", "/policy", self.save, feature=FEATURE, admin=False),
            EeRoute("DELETE", "/policy", self.remove, feature=FEATURE, admin=False),
        ]

    def _policy(self) -> ServerPolicy:
        policy = self.edition.policy
        if not isinstance(policy, ServerPolicy):  # a server's edition always makes one; the tests' in-memory one not
            raise Refused(404, "not found")
        return policy

    def _shown(self, policy: ServerPolicy) -> dict[str, Any]:
        """In a worker thread: E2's ``admin_view()``, and ``etag``, what a save sends back (``viewed``, as the
        administration's GET answers it for `latet server policy set`)."""
        return viewed(policy)

    def _company_read(self, via: object) -> Refused | None:
        """In a worker thread: whether a session opened as ``via`` may read the policy's text (``company_sign_in``),
        under the sign-in settings as PostgreSQL has them now, never this instance's copy, which may be seconds behind
        a change of ``company_changes`` made on another instance."""
        with self.server.db.connection() as conn:
            return company_sign_in(lambda: settings_in(conn), via)

    async def text(self, request: Request, who: Who) -> Response:
        """The stored policy, its text included, as `latet server policy show` sees it: in every standing (a trial
        that ended leaves it frozen, and read), from a session the company's sign-in opened while the sign-in settings
        ask for one. A database that does not answer is E2's 503, in E2's fixed words; sign-in settings that cannot be
        read refuse it so too."""
        policy = self._policy()
        try:
            if await who.run(self._company_read, request.scope.get(VIA)) is not None:
                raise Refused(403, READ_BY_COMPANY_SIGN_IN, code="company-sign-in")
            return JSONResponse(await who.run(self._shown, policy))
        except Refused:
            raise
        except Exception as exc:  # by its type: its words can name the database
            log.warning("the server's policy could not be read for its editor (%s)", type(exc).__name__)
            unread = error(UNREAD, 503)
            unread.headers["retry-after"] = QUEUED_RETRY_AFTER
            return unread

    async def save(self, request: Request, who: Who) -> Response:
        """{"policy": <the policy file's text>, "etag": <the etag of the policy it replaces, or null>,
        "accept_notices": true?}: E2's administration route's body, and the etag."""
        body = await read_body(request, 2 * MAX_BYTES)  # a rules file's size, quoted in JSON
        if isinstance(body, Response):
            raise Refused(413, "larger than a rules file may be")
        given = await who.run(_change_of, body)
        if isinstance(given, Response):  # E2's 400: not JSON, no text, half of a character; or no etag
            raise Refused(given.status_code, "not a policy", response=given)
        text, accepted, replaces = given
        policy = self._policy()
        try:
            floor: Floor = await who.run(policy.read, text)  # before the transaction (E2's): nothing written
        except PolicyError as exc:  # its words never quote the policy
            raise Refused(400, f"not changed: {exc}") from None
        if floor.unread and not accepted:  # a key with one mistake in it is left out whole: never set unseen
            refused = _unread(floor)
            raise Refused(refused.status_code, "keys left out whole", response=refused)
        keys = [key for key in KEYS if key in floor.keys]
        told: list[bool] = []

        def save(within: Changing) -> None:
            if policy.held_etag(within.conn) != replaces:  # changed meanwhile: held off from here until the commit
                raise Refused(409, CHANGED, code="policy-changed")
            policy.save(text, floor, actor=within.actor, conn=within.conn)
            within.note("policy.changed", None, {"etag": floor.etag, "keys": keys, "exceptions": floor.exceptions})
            within.after.append(lambda: told.append(policy.committed()))  # after the commit: refresh, then publish

        await who.change(save)
        late = f"the other instances were not told now: they take it within {policy.every:g} seconds"
        problems = ([] if all(told) else [late]) + await who.run(_not_applying, self.server)
        answer: dict[str, Any] = {
            "etag": floor.etag,
            "keys": keys,
            "exceptions": floor.exceptions,
            "notices": list(floor.notices),
            "problems": problems,
        }
        return JSONResponse(answer)

    async def remove(self, request: Request, who: Who) -> Response:
        """{"etag": <the etag of the policy it removes, or null>}: as a save names the one it replaces, checked in the
        change's transaction with every other change of the policy held off there (409 when it changed meanwhile)."""
        body = await read_body(request, MOST_REMOVAL)
        if isinstance(body, Response):
            raise Refused(413, "larger than a removal's body may be")
        removes = await who.run(_removal_of, body)
        if isinstance(removes, Response):
            raise Refused(removes.status_code, "no etag", response=removes)
        policy = self._policy()

        def remove(within: Changing) -> bool:
            if policy.held_etag(within.conn) != removes:  # changed meanwhile: held off from here until the commit
                raise Refused(409, CHANGED, code="policy-changed")
            removed = policy.remove(conn=within.conn)
            if removed:  # a removal of nothing records nothing (E2's)
                within.note("policy.changed", None, {"removed": True})
                within.after.append(lambda: policy.committed())  # after the commit: refresh, then publish
            return removed

        return JSONResponse({"removed": await who.change(remove)})
