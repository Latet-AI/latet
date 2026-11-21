"""The policy's routes (Latet Enterprise, locked rules): a server's administration API (`latet server policy`), and the
dashboard's view of the floor, which names what is locked and never quotes a value. The administration routes are
served behind E0b's one gate, as E1's license and settings are: an administration token, over HTTPS when the
server's address is https and never from a web page, every call in the administration log under its route's name.
A change is written in one transaction with its entry (``policy.changed``, by ``admin:<token id>``, its keys, whose
exceptions count and its etag, never a value): the call's token's row held first, the license checked as that
transaction finds it, the entry last. The policy is read in every state, a license or none: it says whether it is in
force; it is changed only while the license lists locked rules and can manage them (E1's answers: 403 with
``feature``, ``trial-ended``, ``not-manageable``), the same answer before the body is read, as this instance sees the
license, and in the change's transaction.

A policy's text is read before that transaction, so that the rows it holds are held for the writes alone. Once a change
commits, this instance reads it again and every other is told (``ServerPolicy.committed``): each space, on each
instance, is raised to the new floor in its own worker (``Spaces.floor_changed``), never in this route's thread. An
instance of a release before locked rules, or one without the Enterprise Edition, applies no policy: while one says it
is alive, a policy set answers a problem saying so."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from latet.ee.admin import ADMIN_EE, _Refused, admin_endpoint
from latet.ee.api import EE_API, error, json_body, read_body, refusal
from latet.ee.license import NotLicensed
from latet.ee.policy import FEATURE, PolicyError, floor_view, unread_words
from latet.ee.policy_server import PolicyUnavailable
from latet.server.identities import Changes
from latet.server.machines import QUEUED_RETRY_AFTER, CredentialsBusy
from latet.transfer import KEYS, MAX_BYTES

if TYPE_CHECKING:
    from starlette.requests import Request

    from latet.api.rest import Dashboard
    from latet.config import Config
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.license import Standing
    from latet.ee.policy import Floor
    from latet.ee.policy_server import ServerPolicy
    from latet.ee.policy_sources import PolicySource
    from latet.server.admin import AdminGate, _Call
    from latet.server.app import Server

log = logging.getLogger(__name__)

ADMIN_POLICY: Final = f"{ADMIN_EE}/policy"
NOT_A_POLICY: Final = 'the body is {"policy": "<the policy file\'s text>"}'
ACCEPT: Final = "accept_notices"  # the body's word that keys left out whole were seen, and the policy set without them
NOT_UNICODE: Final = "not changed: the policy holds half of a character (a lone surrogate)"
UNREAD: Final = "the server's policy cannot be read now: try again"  # fixed words: a driver's can name the database
APPLIES: Final = "enterprise"  # the edition whose instances apply the policy, as each says it is alive
BELOW_KEPT: Final = 256  # the person's view: what of a person's settings is below a policy, kept for so many
# A change that names the policy it replaces, refused (409 ``policy-changed``) when that is no longer the one stored:
# the Policy page's save and removal (E3), and `latet server policy set` and `remove` (final review L3). Fixed words.
CHANGED: Final = "not changed: the policy was changed since its text was read here: read it again, then change it"
BAD_ETAG: Final = (
    "the body's \"etag\" is the etag of the policy it replaces, as the policy's GET gave it, null when none is stored"
)
NOT_A_REMOVAL: Final = (
    'the body is empty, or {"etag": "<the etag of the policy it removes>"}, as the policy\'s GET gave it, null when '
    "none is stored"
)
MOST_ETAG: Final = 128  # characters: an etag is a SHA-256 in hex (64)
MOST_REMOVAL: Final = 1024  # bytes of a removal's body: an etag in JSON


def _etag(etag: Any) -> bool:
    """An etag a body may name: text of at most MOST_ETAG characters, or null (none stored)."""
    return etag is None or (isinstance(etag, str) and 0 < len(etag) <= MOST_ETAG)


def viewed(policy: ServerPolicy) -> dict[str, Any]:
    """In a worker thread: ``admin_view()``, and ``etag``, what a change sends back to name the policy it replaces: the
    stored policy's as its text was read, or, when its text cannot be opened or read, the stored one's as read before it
    (a policy stored in between makes the change answer 409, never lets it overwrite unseen); null when none is stored.
    The Policy page's GET /ee/policy/text and the administration's GET answer it."""
    before = policy.stored_etag()
    view = policy.admin_view()
    shown = view["policy"]
    return {**view, "etag": shown["etag"] if shown is not None else before}


def _text_of(body: bytes) -> tuple[str, bool] | Response:
    """In a worker thread: the policy file's text in ``body``, {"policy": "...", "accept_notices": true}, and whether
    the caller accepts that keys of it left out whole are not set (``accept_notices`` true, and nothing else); else the
    answer 400 that refuses it (not UTF-8, not JSON, NaN or an infinity in it, nested deeper than Python reads, not an
    object, no text under ``policy``, or a text holding half of a character, which JSON's escape of a lone surrogate
    gives)."""
    try:
        found = json_body(body)
    except (ValueError, RecursionError):
        found = None
    text = found.get("policy") if isinstance(found, dict) else None
    if not isinstance(text, str) or not isinstance(found, dict):
        return error(NOT_A_POLICY, 400)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return error(NOT_UNICODE, 400)
    return text, found.get(ACCEPT) is True


def _set_of(body: bytes) -> tuple[str, bool, bool, str | None] | Response:
    """In a worker thread: the administration's PUT body, ``_text_of``'s reading of it, and whether it names the etag
    of the policy its text replaces, and that etag (a string, or null when none is stored: `latet server policy set`
    sends the one its GET answered); else the 400 that refuses it. Optional, so that a caller that names none sets the
    policy as before, checking nothing."""
    given = _text_of(body)
    if isinstance(given, Response):
        return given
    found = json_body(body)  # an object holding a text: _text_of read it so
    if "etag" not in found:
        return given[0], given[1], False, None
    if not _etag(etag := found["etag"]):
        return error(BAD_ETAG, 400)
    return given[0], given[1], True, etag


def _deletion_of(body: bytes) -> tuple[bool, str | None] | Response:
    """In a worker thread: whether the administration's DELETE body names the etag of the policy it removes, and that
    etag (a string, or null when none is stored: `latet server policy remove` sends the one its GET answered); else the
    400 that refuses it, quoting nothing. Optional, as the PUT's: an empty body, or one with no etag, removes the
    policy as before, checking nothing."""
    if not body.strip():
        return False, None
    try:
        found = json_body(body)
    except (ValueError, RecursionError):
        found = None
    if not isinstance(found, dict) or ("etag" in found and not _etag(found["etag"])):
        return error(NOT_A_REMOVAL, 400)
    return ("etag" in found), found.get("etag")


def _unread(floor: Floor) -> Response:
    """The answer 400 to a policy whose file has keys left out whole, sent without ``accept_notices``: what it would
    not lock, by the keys' names (one Latet does not know counted, never quoted)."""
    return error(
        f"not changed: the policy leaves out what it could not read in {unread_words(floor.unread)}, so it would not "
        f'lock it: correct the file, or send it again with "{ACCEPT}": true (`latet server policy set` asks first)',
        400,
    )


def _refused_by(standing: Standing) -> Response | None:
    """Why the license, as ``standing`` says it, does not let the policy change (R1), the same answer at both checks:
    E1's ``trial-ended`` or ``not-manageable`` (no license at all among them), else 403 ``feature`` for a license that
    does not list locked rules; None when it lets it."""
    if (refused := refusal(standing, FEATURE)) is not None:
        return refused
    if standing.license is None or FEATURE not in standing.license.features:
        return error(str(NotLicensed(FEATURE)), 403, feature=FEATURE)
    return None


def _not_applying(server: Server) -> list[str]:
    """In a worker thread, once a policy is set: the problem said while instances that do not apply it say they are
    alive: of a release before locked rules (a rolling upgrade under way) or without the Enterprise Edition. Each reads
    the policy as it starts, so it applies it once it runs this release with the Enterprise Edition. A listing cut
    at its bound says "at least", or, with none listed that does not, that one it does not show may not
    (``some_instances``). Valkey not answering says nothing (logged by the error's type): the policy is set all the
    same."""
    from latet.server.app import instances_said, some_instances

    try:
        listed, whole = instances_said(server)
    except Exception as exc:  # its words can name Valkey's address
        log.warning("whether every instance applies the policy could not be read (%s)", type(exc).__name__)
        return []
    count = sum(1 for said in listed if said.rpartition(" ")[2] != APPLIES)
    if not count and whole:
        return []
    some = some_instances(count, whole)
    return [
        f"{some} not apply the policy (a release of Latet before locked rules, or one without the Enterprise "
        "Edition): each applies it as it starts with this release and the Enterprise Edition"
    ]


def policy_admin_routes(
    edition: EnterpriseEdition, server: Server, gate: AdminGate, policy: ServerPolicy
) -> list[BaseRoute]:
    """``GET`` the policy as an administrator reads it (``ee.policy.read``, in every state), with the etag a change
    replacing it names (``viewed``); ``PUT {"policy": <the policy file's text>}`` sets it and ``DELETE`` removes it
    (``ee.policy.change``). A policy whose file has keys left out whole (one malformed term leaves out every term) is
    refused unless the body also says ``"accept_notices": true``: what it would not lock is never set unseen. A PUT
    whose body also names ``"etag"``, the policy it replaces (null: none), or a DELETE whose body ``{"etag": ...}``
    names the one it removes, is refused when that is no longer the one stored (409 ``policy-changed``, checked in the
    change's transaction, every other change held off there until it commits), so that `latet server policy set` and
    `latet server policy remove` never replace or remove unseen a policy set after they read the server's."""

    async def show(request: Request, call: _Call) -> Response:
        try:
            return JSONResponse(await gate.run(viewed, policy))
        except CredentialsBusy:  # the administration's threads too busy: its gate says it
            raise
        except Exception as exc:  # the database not answering: by its type, its words can name it
            log.warning("the server's policy could not be read for its administration (%s)", type(exc).__name__)
            unread = error(UNREAD, 503)
            unread.headers["retry-after"] = QUEUED_RETRY_AFTER
            return unread

    def changed(
        call: _Call, text: str | None, floor: Floor | None, checks: bool = False, replaces: str | None = None
    ) -> dict[str, Any]:
        """In a worker thread: ``text``, read as ``floor``, set as the server's policy (None: the policy removed), with
        its entry, in one transaction; this instance reads it again and every other is told once it commits.
        ``_Refused`` when the license, as that transaction finds it (its row held there until it ends), does not let
        it change: a license replaced or removed, or a trial that ended, meanwhile on any instance; and, when the body
        named the policy it replaces (``checks``), when ``replaces`` is no longer the stored policy's etag."""
        with server.db.transaction() as conn:
            gate.holding(call, conn)  # first: a token gone or past its day changes nothing
            if (refused := _refused_by(edition.licensing.standing_in(conn))) is not None:
                raise _Refused(refused)
            if checks and policy.held_etag(conn) != replaces:  # changed meanwhile: held off from here until the commit
                raise _Refused(error(CHANGED, 409, code="policy-changed"))
            details: dict[str, Any]
            if text is None or floor is None:
                if not policy.remove(conn=conn):
                    return {"removed": False}  # nothing stored: nothing changed, no entry says it was
                details = {"removed": True}
            else:
                policy.save(text, floor, actor=call.actor, conn=conn)
                keys = [key for key in KEYS if key in floor.keys]
                details = {"etag": floor.etag, "keys": keys, "exceptions": floor.exceptions}
            gate.entry(call, Changes(conn), "policy.changed", details, target=None)  # last
        gate.recorded(call, "policy.changed", details, None)
        told = policy.committed()  # this instance reads it again, its spaces raised in their own workers; then the rest
        if floor is None:
            return details
        late = f"the other instances were not told now: they take it within {policy.every:g} seconds"
        problems = [] if told else [late]
        return {**details, "notices": list(floor.notices), "problems": [*problems, *_not_applying(server)]}

    async def change(request: Request, call: _Call) -> Response:
        # Said before the body is read, as this instance sees the license; checked again in the change's transaction.
        if (refused := _refused_by(await gate.run(edition.licensing.standing))) is not None:
            return refused
        text: str | None = None
        floor: Floor | None = None
        checks, replaces = False, None
        if request.method == "PUT":
            body = await read_body(request, 2 * MAX_BYTES)  # a rules file's size, quoted in JSON
            if isinstance(body, Response):
                return error("larger than a rules file may be", 413)
            if isinstance(given := await gate.run(_set_of, body), Response):
                return given
            text, accepted, checks, replaces = given
            try:
                read: Floor = await gate.run(policy.read, text)  # before the transaction: its rows for the writes alone
            except PolicyError as exc:  # its words never quote the policy
                return error(f"not changed: {exc}", 400)
            if read.unread and not accepted:  # a key with one mistake in it is left out whole: never set unseen
                return _unread(read)
            floor = read
        else:  # DELETE: a body, when there is one, names the policy it removes
            removal = await read_body(request, MOST_REMOVAL)
            if isinstance(removal, Response):
                return error("larger than a removal's body may be", 413)
            if isinstance(named := await gate.run(_deletion_of, removal), Response):
                return named
            checks, replaces = named
        try:
            answer = await gate.run(changed, call, text, floor, checks, replaces)
        except _Refused as refused_there:
            return refused_there.response
        if answer == {"removed": False}:
            call.details = {"removed": False}
        return JSONResponse(answer)

    return [
        Route(ADMIN_POLICY, admin_endpoint(gate, "ee.policy.read", show), methods=["GET"]),
        Route(ADMIN_POLICY, admin_endpoint(gate, "ee.policy.change", change), methods=["PUT", "DELETE"]),
    ]


def policy_routes(dashboard: Dashboard, policy: PolicySource) -> list[BaseRoute]:
    """`GET /_latet/api/v1/ee/policy`: the floor in force, where it comes from, and which of this person's settings are
    below it (raised while in use). Every person reads it: their Rules page shows its locks. It is a person's view
    (``floor_view`` without values): what is locked is named, and no term, value, pattern or guidance of the company's
    is in it, nor the words of a notice; the administrators' view is theirs alone, through the administration API. It
    answers in every state, a license or none (no license: no floor, the problems say why), and reads nothing per
    request but the floor and the summary as the last look left them."""

    kept: OrderedDict[tuple[str, str, str], list[str]] = OrderedDict()
    lock = threading.Lock()

    def below(floor: Floor, config: Config) -> list[str]:
        """What of ``config`` is below ``floor``, weighed once for a policy and a person's settings: a page that asks
        again (each person's Rules page, at each visit) raises nothing again, the last ``BELOW_KEPT`` kept."""
        from latet.runtime import digest

        if not floor.etag:  # a floor of no text: weighed each time
            return floor.below(config)
        key = (floor.etag, floor.exceptions, digest(config))
        with lock:
            if (said := kept.get(key)) is not None:
                kept.move_to_end(key)
                return list(said)
        said = floor.below(config)
        with lock:
            kept[key] = said
            while len(kept) > BELOW_KEPT:
                kept.popitem(last=False)
        return list(said)

    def view() -> dict[str, Any]:
        summary = policy.summary() or {"problems": []}
        try:
            floor = policy.floor()
        except PolicyUnavailable:  # said among the summary's problems
            floor = None
        return {
            "floors": [floor_view(floor)] if floor is not None else [],
            "below": below(floor, dashboard.rt.config) if floor is not None else [],
            "problems": [problem["text"] for problem in summary["problems"]],
        }

    async def handler(request: Request) -> Response:
        # In the dashboard's threads (one job per identity on a server), never the default executor.
        return JSONResponse(await dashboard._in_pool(view))

    return [Route(f"{EE_API}/policy", dashboard.guarded(handler), methods=["GET"])]
