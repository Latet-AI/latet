"""The Sign-in page's routes and `latet server sign-in`: single sign-on's settings (the sso section of E1's Enterprise
settings), its client secret (sealed with the master key for the issuer it was saved for: E1's DatabaseSecrets), the
test of the provider, SCIM's tokens; the code a person signed in makes to connect a computer; and what a person's
dashboard tells them of the computers connected as them and the codes made for them until they say they saw it (O6).
Each change and its entries in one transaction (ee/routes.py: ``who.change``); a section changed tells every instance
once it commits. A change of the settings may name the etag of those it was made from (``sso_etag``, which the view
gives), as `latet server sign-in set` with options alone does: a change made meanwhile is then never overwritten unseen
(409, nothing changed).

Whether the dashboard's changes that apply to everyone take a session opened with the company's sign-in
(``company_changes``, the user's decision (c)) is set with `latet server` alone: the page keeps the stored setting,
refuses a body that names it, and while it asks for the company's sign-in leaves turning single sign-on off to `latet
server sign-in off`. A new issuer leaves the saved client secret unusable until it is saved again (M4), and once someone
signed in it is `latet server sign-in set`'s alone; so is a new user-name claim once someone signed in or an account
waits for its first sign-in by its user name (the final review's F5), and then turning single sign-on off too, whatever
``company_changes`` says (off and on again would set either unweighed). ``sign-in.changed`` names the fields a change
set, never their values. No answer holds a secret, a token after the one that makes it, or a code after the one that
makes it; no refusal quotes what the request sent."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.ee.oidc import _LOOPBACK_ISSUERS, _loopback, secret_bound
from latet.ee.people import Changing, LastAdmin
from latet.ee.people_api import connect_command
from latet.ee.routes import VIA, EeRoute, Refused, Who, json_body
from latet.ee.scim import BASE, DAY, MOST_DAYS
from latet.ee.server import DatabaseSecrets, DatabaseSettings, settings_in
from latet.ee.settings import DEFAULT_SECRET, SSO_SECRETS, SsoSettings, _field_names
from latet.logline import Paced
from latet.server import egress

if TYPE_CHECKING:
    from latet.doctor.checks import Check
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.oidc import Oidc
    from latet.ee.people import People
    from latet.ee.scim import ScimToken, ScimTokens
    from latet.server.app import Server
    from latet.server.identities import Identity

log = logging.getLogger(__name__)

MAX_SECRET: Final = 4096  # characters of a client secret: a provider's are a few dozen
WARN_DAYS: Final = 14  # with accounts: scim, the Sign-in line warns this many days before the last SCIM token ends
TOLD: Final = 40  # codes the person made themselves a visit is told of at most: the newest (the rest are counted)
NOTICE_DAYS: Final = 14  # an entry is told this long after it happened, unless the person says it seen sooner
SCIM_TOKEN_ID = re.compile(r"s_[a-z2-7]{10}")  # as ScimTokens.make names one: never anything a path may hold else
# E3's release gates 1 and 2 (the owner's numbers, D1 and D3, each one constant): a person's own code (``connect_code``)
# is refused while COMPUTERS_MOST computers are connected to their account, whoever made the codes they were connected
# with (409; an administrator's codes are never refused, and their computers count), and past CONNECT_CODES codes of
# their own in any CONNECT_CODE_WINDOW (429). Both counted in the code's own transaction, the person's row held: exact
# across instances and workers, with Valkey or without it; a refusal writes nothing, so it costs no code.
COMPUTERS_MOST: Final = 20
CONNECT_CODES: Final = 10
CONNECT_CODE_WINDOW: Final = 3600.0  # seconds: any hour, sliding
# The person's own codes, newest first, as the administration log committed them (its ids follow the commits, and the
# person's row serializes theirs), CONNECT_CODES at most: a walk of the notices' index (301_notices.sql: ``target, id``,
# the action a literal it lists) that stops at the limit, whatever the person's history (N2).
CODES_MADE: Final = (
    "SELECT ts FROM audit WHERE target = %s AND actor = %s AND action = 'person.code.made' ORDER BY id DESC LIMIT %s"
)


def codes_made_by(person: str) -> tuple[str, str, int]:
    """What ``CODES_MADE`` is run with for ``person``'s own codes: the target, the actor, the limit."""
    return person, person, CONNECT_CODES


_AT_MOST = Paced()  # codes refused at COMPUTERS_MOST: said once a minute
_TOO_MANY = Paced()  # codes refused past CONNECT_CODES: said once a minute
# Fixed words: a refusal never quotes what the request said (a path, a name, a body's text).
_SSO_BODY = "the body is {sso}: the sign-in settings, or null to turn single sign-on off"
_AN_ETAG = "etag is the etag of the sign-in settings the change was made from, as they were shown, or null for none"
_CHANGED_SINCE = (
    "not changed: the sign-in settings were changed since they were read here: read them again, then change them"
)
MOST_ETAG: Final = 128  # characters: an etag is a SHA-256 in hex (64)
_COMPANY_CHANGES = (
    "whether changes that apply to everyone take the company sign-in is set with "
    "`latet server sign-in set --company-changes`"
)
_OFF_BY_COMMAND = (
    "while changes that apply to everyone take the company sign-in, single sign-on is turned off with "
    "`latet server sign-in off`"
)
_OFF_WHILE_LINKED = (
    "people have signed in, or an account waits for its first sign-in by its user name: single sign-on is turned off "
    "with `latet server sign-in off`"
)
_ISSUER_BY_COMMAND = (
    "people have signed in with this provider: a new issuer is set with `latet server sign-in set`, and their accounts "
    "are linked again with `latet server people unlink` and `link`"
)
_CLAIM_BY_COMMAND = (
    "people have signed in, or an account waits for its first sign-in by its user name: a new user name claim is set "
    "with `latet server sign-in set`"
)
_UNREACHED = (
    "the issuer is at an address the server never reaches: link-local, unspecified, reserved or multicast, an IPv6 "
    "form that carries an IPv4 address, or this computer under another name than localhost, 127.0.0.1 or [::1]"
)
_NOT_SET_UP = "single sign-on is not set up: its settings first, since the client secret is kept for their issuer"
_NO_SECRET_NAMED = "the sign-in settings name no client secret of the sign-in's"  # noqa: S105 - words
_A_SECRET = f"the client secret is one line of printable characters, {MAX_SECRET} at most"
_NEVER_BY_COMMAND = (
    "a SCIM token made from the dashboard ends: `latet server sign-in scim-token --never` makes one that never does"
)
_NO_TOKEN = "there is no such SCIM token"  # noqa: S105 - words, not a token
_A_PERSONS = "a code to connect a computer is made for a person"
_NOT_FOUND = "not found"
_AN_UNTIL = "until is the until of what /me told you, as it gave it"
_TOLD_BY_SSO = (
    "what your dashboard tells you is said seen from a session opened with the company sign-in; without one, it goes "
    f"by itself {NOTICE_DAYS} days after it happened"
)


def _computers_most() -> str:
    return (
        f"you have {COMPUTERS_MOST} computers or more connected to your account, the most one person may connect with "
        "their own codes: disconnect one you no longer use (`latet disconnect` on it, or an administrator with `latet "
        "server people tokens`), then make a code"
    )


def _codes_too_many() -> str:
    return f"you made {CONNECT_CODES} codes in the last hour, the most one person may: try again later"


def _said(paced: Paced, line: str) -> None:
    if count := paced.due():
        log.warning("%s%s", line, f" ({count} times since the line before)" if count > 1 else "")


def issuer_problem(issuer: str) -> str | None:
    """F15: why the server never reaches ``issuer``, judged by its host as written: a literal address that egress
    refuses for an administrator's own service (``inside``: the company's network allowed), or this computer under
    another name than the issuer names it by (``PinnedTransport`` refuses both when it connects); None otherwise. A name
    is judged when the server connects."""
    host = urlsplit(issuer).hostname or ""
    if host in _LOOPBACK_ISSUERS:  # Keycloak by hand on the server's own machine
        return None
    if _loopback(host):
        return _UNREACHED
    found = egress.literal(host)
    return _UNREACHED if found is not None and egress.forbidden(found, inside=True) else None


def _invalid(exc: ValidationError) -> JSONResponse:
    """400, each problem by its place and pydantic's words, which quote nothing given; a place below a field that names
    none of the settings' fields (a group of the ID token, a key the body made up) is shown as ``*``."""
    names = _field_names()
    details = [
        {
            "loc": [part if isinstance(part, int) or part in names else "*" for part in error["loc"]],
            "msg": error["msg"].removeprefix("Value error, "),
            "type": error["type"],
        }
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    return JSONResponse({"error": "invalid settings", "details": details}, status_code=400)


def _sso_of(raw: Any) -> SsoSettings | JSONResponse:
    try:
        return SsoSettings.model_validate(raw)
    except ValidationError as exc:
        return _invalid(exc)


def _same_issuer(one: str, other: str) -> bool:
    """Whether two issuers are one, as the client secret is bound to them (``secret_bound``)."""
    return secret_bound(one) == secret_bound(other)


def _changed_fields(before: SsoSettings | None, after: SsoSettings | None) -> list[str]:
    """The names of the sign-in settings' fields a change sets otherwise than they were (single sign-on turned on or
    off: those either side sets), for ``sign-in.changed`` to say: never their values."""
    old = before.model_dump(mode="json") if before is not None else {}
    new = after.model_dump(mode="json") if after is not None else {}
    return sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))


def _mapping(sso: SsoSettings | None) -> dict[str, str] | None:
    """The groups mapping of the sign-in settings ``sso``; None while single sign-on is off: what a save regroups
    everyone for when it changes."""
    return dict(sso.groups) if sso is not None else None


def sso_etag(sso: SsoSettings | None) -> str | None:
    """The etag of the sign-in settings ``sso`` (None: single sign-on off), which a change made from them names: a
    change made meanwhile, on any instance, is then never overwritten unseen. A digest of what the page shows of them,
    never of a secret (the settings name the client secret, never hold it)."""
    if sso is None:
        return None
    shown = json.dumps(sso.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(shown.encode("utf-8")).hexdigest()


class SignInApi:
    def __init__(
        self, edition: EnterpriseEdition, server: Server, people: People, oidc: Oidc, tokens: ScimTokens
    ) -> None:
        store, secrets = edition.settings_store, edition.secrets
        if not isinstance(store, DatabaseSettings) or not isinstance(secrets, DatabaseSecrets):
            raise TypeError("the sign-in's settings are a server's: kept in PostgreSQL")
        self.edition = edition
        self.server = server
        self.people = people
        self.oidc = oidc
        self.tokens = tokens
        self.store = store
        self.secrets = secrets

    def routes(self) -> list[EeRoute]:
        return [
            EeRoute("GET", "/sign-in", self.show, feature="sso"),
            EeRoute("PUT", "/sign-in", self.save, feature="sso"),
            EeRoute("PUT", "/sign-in/secret", self.save_secret, feature="sso"),
            EeRoute("DELETE", "/sign-in/secret", self.remove_secret, feature="sso"),
            EeRoute("POST", "/sign-in/test", self.test, feature="sso"),
            EeRoute("POST", "/sign-in/scim-tokens", self.make_token, feature="sso", answers=201),
            EeRoute("DELETE", "/sign-in/scim-tokens/{id}", self.remove_token, feature="sso"),
            EeRoute("POST", "/sign-in/code", self.connect_code, kind="serve", feature="sso", admin=False),
            # O6: a person says they saw what their dashboard told them, from a session their company sign-in opened:
            # served while sso serves, which alone opens one (the notice is listed while roles or sso serve).
            EeRoute("POST", "/notices/told", self.told, kind="serve", feature="sso", admin=False),
        ]

    def _scim_url(self) -> str:
        return self.server.settings.url.rstrip("/") + BASE

    # --- what the page shows --------------------------------------------------------------------------------------

    def _secret(self, sso: SsoSettings | None) -> tuple[bool, bool]:
        """Whether the client secret is saved for the issuer ``sso`` names, and whether one is saved for another (a
        new issuer: it is saved again before anyone signs in). Never the secret."""
        if sso is None or sso.client_secret is None:
            return False, False
        try:
            return self.secrets.load(sso.client_secret, bound=secret_bound(sso.issuer)) is not None, False
        except InvalidTag:
            return False, True

    def _token_views(self, makers_told: bool) -> list[dict[str, Any]]:
        """SCIM's tokens, never the token; to whoever's roles allow sign-in.edit (``makers_told``) each with its maker
        and whether they still administer (O3); to anyone else (everyone reads the page while the license has no
        roles) each by its id and its dates alone."""
        tokens = self.tokens.all()
        if not makers_told:
            return [{"id": t.id, "created": t.created, "expires": t.expires, "last_used": t.last_used} for t in tokens]
        admins = self.people.admins()
        makers = {token.made_by for token in tokens if not token.made_by.startswith("admin:")}
        found = {made_by: self.server.identities.current(made_by) for made_by in makers}
        return [self._token_view(token, admins, found) for token in tokens]

    @staticmethod
    def _token_view(token: ScimToken, admins: set[str], found: dict[str, Identity | None]) -> dict[str, Any]:
        if token.made_by.startswith("admin:"):  # `latet server`'s administration token
            maker: dict[str, Any] = {"kind": "administration", "id": token.made_by.removeprefix("admin:")}
        else:
            person = found.get(token.made_by)
            maker = {
                "kind": "person",
                "id": token.made_by,
                "name": person.name if person is not None else None,  # None: removed since
                "admin": token.made_by in admins,  # an enabled administrator still
                "disabled": person is not None and person.disabled,
            }
        return {
            "id": token.id,
            "created": token.created,
            "expires": token.expires,
            "last_used": token.last_used,
            "made_by": token.made_by,
            "maker": maker,
        }

    def _view(self, sso: SsoSettings | None, who: Who) -> dict[str, Any]:
        """The page's view of ``sso`` (the settings in use, or those a change just wrote), as ``who`` reads it:
        ``company_changes`` beside the section, which the page sends back unchanged and never names."""
        saved, elsewhere = self._secret(sso)
        makers_told = self.edition.access is not None and self.edition.access.allows(who.identity_id, "sign-in.edit")
        return {
            "sso": sso.model_dump(mode="json", exclude={"company_changes"}) if sso is not None else None,
            "company_changes": sso.company_changes if sso is not None else None,  # None: any administrator's session
            "etag": sso_etag(sso),  # what a change made from these names (``save``)
            "redirect_uri": self.oidc.redirect_uri(),
            "secret": saved,
            "secret_issuer_changed": elsewhere,
            "scim": {"url": self._scim_url(), "tokens": self._token_views(makers_told)},
        }

    def _shown(self, who: Who) -> dict[str, Any]:
        """The page's view of the settings as PostgreSQL has them now, never this instance's copy (seconds behind a
        change made on another instance): the etag it gives names the settings a change made from them replaces."""
        with self.server.db.connection() as conn:
            sso = settings_in(conn).sso
        return self._view(sso, who)

    async def show(self, request: Request, who: Who) -> Response:
        return JSONResponse(await who.run(self._shown, who))

    # --- the settings -----------------------------------------------------------------------------------------

    async def save(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        raw = data.get("sso")
        if "sso" not in data or (raw is not None and not isinstance(raw, dict)):
            raise Refused(400, _SSO_BODY)
        # The etag of the settings the change was made from (``sso_etag``), when the body names one: checked with every
        # other change of the settings held off (409 when they changed meanwhile). None: none were set up.
        named_etag, etag = "etag" in data, data.get("etag")
        if named_etag and etag is not None and not (isinstance(etag, str) and 0 < len(etag) <= MOST_ETAG):
            raise Refused(400, _AN_ETAG)
        page = who.person  # the dashboard's: company_changes is `latet server`'s alone (decision (c))
        named = raw is not None and "company_changes" in raw
        if page and named:
            raise Refused(400, _COMPANY_CHANGES)
        sso: SsoSettings | None = None
        if raw is not None:
            if isinstance(found := _sso_of(raw), JSONResponse):
                return found
            if (problem := issuer_problem(found.issuer)) is not None:
                raise Refused(400, problem)
            sso = found

        def save(within: Changing) -> SsoSettings | None:
            conn = within.conn
            if page:  # the last administrator kept (regroup, L5): their lock first, as every keeping change takes it
                self.people.hold_admins(conn)
            # Every other change of the Enterprise settings, on any instance, waits for this one from here (a section
            # or none: an insert waits too); reads never wait. The settings as this transaction finds them, past every
            # copy, are those the change replaces.
            conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            before = settings_in(conn).sso
            if named_etag and sso_etag(before) != etag:
                raise Refused(409, _CHANGED_SINCE, code="sign-in-changed")
            after = sso
            if after is not None and not named:  # the stored setting kept, or the safer default
                kept = before.company_changes if before is not None else "sso"
                after = after.model_copy(update={"company_changes": kept})
            moved = before is not None and after is not None and not _same_issuer(before.issuer, after.issuer)
            # The claim a first sign-in is found by (H1, M4): pointed at one people may set themselves, it would hand an
            # account waiting for its first sign-in to whoever sets it to that account's user name.
            renamed = before is not None and after is not None and before.user_name_claim != after.user_name_claim
            if page:
                if after is None and before is not None and before.company_changes == "sso":
                    raise Refused(409, _OFF_BY_COMMAND)
                # Off, then on again, would weigh a new issuer or claim against nothing: `latet server`'s, once anyone
                # signed in or an account waits, whatever company_changes says (the final fix's re-review).
                turned_off = after is None and before is not None
                if turned_off and (self.people.signed_in(conn=conn) or self.people.claimable(conn=conn)):
                    raise Refused(409, _OFF_WHILE_LINKED)
                if moved and self.people.signed_in(conn=within.conn):
                    raise Refused(409, _ISSUER_BY_COMMAND)
                if renamed and (self.people.signed_in(conn=conn) or self.people.claimable(conn=conn)):
                    raise Refused(409, _CLAIM_BY_COMMAND)
            # The mapping applied to everyone at once, the last administrator kept from the page (L5): only when it
            # changed, or single sign-on was turned on or off; any other change of the settings regroups no one.
            if _mapping(before) != _mapping(after):
                try:
                    self.people.regroup(after.groups if after is not None else {}, within=within, keep_admin=page)
                except LastAdmin as exc:
                    raise Refused(409, str(exc)) from None
            value = after.model_dump(mode="json") if after is not None else None
            # E1's store: in this transaction, the row alone; the entry is this change's (last: the call's own on the
            # administration API), and every instance hears the section changed once it commits.
            self.store.set_section("sso", value, actor=within.actor, address=within.address, conn=within.conn)
            details: dict[str, Any] = {"section": "sso", "company_changes": after.company_changes if after else None}
            details["fields"] = _changed_fields(before, after)  # their names, never their values
            if moved:
                details["issuer_changed"] = True  # never the issuer: the saved secret no longer opens for it
            within.note("sign-in.changed", "sso", details)
            within.after.append(lambda: self.store.changed("sso"))
            return after

        written = await who.change(save)
        return JSONResponse(await who.run(self._view, written, who))

    async def save_secret(self, request: Request, who: Who) -> Response:
        value = (await json_body(request, who)).get("secret")
        secret = value.strip() if isinstance(value, str) and len(value) <= MAX_SECRET else ""
        if not secret or not secret.isprintable():
            raise Refused(400, _A_SECRET)

        def save(within: Changing) -> None:
            sso = settings_in(within.conn).sso
            if sso is None:
                raise Refused(409, _NOT_SET_UP)
            if sso.client_secret is None or not sso.client_secret.startswith(SSO_SECRETS):
                raise Refused(409, _NO_SECRET_NAMED)
            # Sealed for the issuer of the settings this transaction finds (M4): another never opens it.
            self.secrets.save(sso.client_secret, secret, bound=secret_bound(sso.issuer), conn=within.conn)
            within.note("sign-in.secret.changed", None, {"saved": True})

        await who.change(save)
        return JSONResponse({"ok": True})

    async def remove_secret(self, request: Request, who: Who) -> Response:
        def remove(within: Changing) -> None:
            sso = settings_in(within.conn).sso
            name = sso.client_secret if sso is not None and sso.client_secret else DEFAULT_SECRET
            if not name.startswith(SSO_SECRETS) or not self.secrets.remove(name, conn=within.conn):
                raise Refused(404, "no client secret is saved")
            within.note("sign-in.secret.changed", None, {"saved": False})

        await who.change(remove)
        return JSONResponse({"ok": True})

    async def test(self, request: Request, who: Who) -> Response:
        raw = (await json_body(request, who)).get("sso")
        if raw is None:
            sso = await who.run(self.oidc.settings)
            if sso is None:
                raise Refused(409, "single sign-on is not set up: there is nothing to test")
        elif not isinstance(raw, dict):
            raise Refused(400, _SSO_BODY)
        elif isinstance(found := _sso_of(raw), JSONResponse):
            return found
        else:
            sso = found
        if (problem := issuer_problem(sso.issuer)) is not None:
            raise Refused(400, problem)
        answer, outcome = await self.oidc.test(sso)  # the server reaches an address the body may have named

        def tested(within: Changing) -> None:
            # Who had the server reach a provider, from where, and how it went; never the issuer, an address or the
            # provider's words. The answer is given once the entry is written: a test the log does not take is not told.
            within.note("sign-in.tested", None, {"outcome": outcome})

        await who.change(tested)
        return JSONResponse(answer)  # the provider's own words, to an administrator alone

    # --- SCIM's tokens ----------------------------------------------------------------------------------------

    async def make_token(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        days: int | None = 0  # absent: the server's default (an administration token's)
        if "days" in data:
            days = data["days"]
            if days is None:
                if who.person:  # M3: from the dashboard a token ends
                    raise Refused(400, _NEVER_BY_COMMAND)
            elif isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MOST_DAYS:
                never = "" if who.person else ", or null for a token that never ends"
                raise Refused(400, f"days is a whole number from 1 to {MOST_DAYS}{never}")

        def make(within: Changing) -> dict[str, Any]:
            made, token = self.tokens.make(made_by=within.actor, days=days, conn=within.conn)
            within.note("scim.token.made", made.id, {"expires": made.expires is not None})
            return {"id": made.id, "token": token, "url": self._scim_url(), "expires": made.expires}

        return JSONResponse(await who.change(make), status_code=201)  # the token shown this once

    async def remove_token(self, request: Request, who: Who) -> Response:
        token_id = request.path_params["id"]
        if not SCIM_TOKEN_ID.fullmatch(token_id):
            raise Refused(404, _NO_TOKEN)

        def remove(within: Changing) -> None:
            if not self.tokens.remove(token_id, conn=within.conn):
                raise Refused(404, _NO_TOKEN)
            within.note("scim.token.removed", token_id)

        await who.change(remove)
        return JSONResponse({"ok": True})

    # --- a code to connect a computer -------------------------------------------------------------------------

    async def connect_code(self, request: Request, who: Who) -> Response:
        """A one-time code for the person signed in to connect a computer (E0c's `latet connect`), while single sign-on
        is set up and serves. A change that applies to everyone (O1): it mints a lasting computer token, so a session a
        one-time code opened makes none while the server asks for the company's sign-in (L10). Recorded
        ``person.code.made``, for the person by themselves; their next visit is told of it (O6). Refused past the
        computers one person connects and the codes they make in an hour (``_within_limits``, E3's gates 1 and 2)."""
        person = who.identity_id
        if not isinstance(person, str) or not person:  # the dashboard always names someone; `latet server` never comes
            raise Refused(403, _A_PERSONS)
        found = await who.run(self.server.identities.current, person)
        if found is None or found.kind != "person":  # Task 9's review: never an API key's
            raise Refused(403, _A_PERSONS)
        if not await who.run(self.oidc.ready):
            raise Refused(404, _NOT_FOUND)

        def make(within: Changing) -> str:
            if settings_in(within.conn).sso is None:  # turned off meanwhile, on any instance
                raise Refused(404, _NOT_FOUND)
            row = within.conn.execute(
                "SELECT disabled FROM identities WHERE id = %s AND kind = 'person' FOR UPDATE", (person,)
            ).fetchone()  # their row held: a disable meanwhile waits for the code, and one before is seen
            if row is None:
                raise Refused(403, _A_PERSONS)
            if row[0]:
                raise Refused(409, "your account is disabled")
            # Gates 1 and 2, their row held: an enrolment committing meanwhile (FOR SHARE) and another code of theirs,
            # on any instance, wait or are waited for, so each count sees what committed before it.
            self._within_limits(within, person)
            code = self.server.identities.code(person, within=within.changes)
            if code is None:
                raise Refused(403, _A_PERSONS)
            within.note("person.code.made", person)  # for them, by them: never the code
            return code

        code = await who.change(make)
        command = await who.run(connect_command, self.server.settings)  # E0c's shape (F14): the code apart
        return JSONResponse({"code": code, "command": command})

    def _within_limits(self, within: Changing, person: str) -> None:
        """In the code's transaction, the person's row held: refused (nothing written, so a refusal costs no code)
        while ``COMPUTERS_MOST`` computers are connected to their account, every token of theirs a computer's, whoever
        made the code it was connected with (409, gate 1); then past ``CONNECT_CODES`` codes of their own whose entries
        fall within the last ``CONNECT_CODE_WINDOW`` (429, gate 2, with retry-after: until the oldest of them leaves
        it). The code not yet used is not counted: a person holds one at a time, and a new one ends it. Fixed words and
        the server's numbers; the server's log says it once a minute, naming no one."""
        conn = within.conn
        computers = conn.execute("SELECT count(*) FROM tokens WHERE identity_id = %s", (person,)).fetchone()
        if computers is not None and computers[0] >= COMPUTERS_MOST:
            _said(_AT_MOST, "a code to connect a computer was refused: its person has the most computers connected")
            raise Refused(409, _computers_most(), code="computers-most", most=COMPUTERS_MOST)
        # The clock `connected` compares the log's times with: each entry carries its instance's (E0b's NTP bound).
        now = self.edition.context.clock()
        made = conn.execute(CODES_MADE, codes_made_by(person)).fetchall()
        if len(made) >= CONNECT_CODES and float(made[-1][0]) > now - CONNECT_CODE_WINDOW:
            _said(_TOO_MANY, "a code to connect a computer was refused: its person made the most codes of an hour")
            seconds = float(made[-1][0]) + CONNECT_CODE_WINDOW - now
            body = {"error": _codes_too_many(), "code": "codes-too-many", "most": CONNECT_CODES}
            answer = JSONResponse(body, status_code=429, headers={"retry-after": str(max(1, math.ceil(seconds)))})
            raise Refused(429, body["error"], response=answer)

    # --- what a person's next visit is told (O6) --------------------------------------------------------------

    def connected(self, identity_id: str) -> dict[str, Any] | None:
        """What the person's dashboard tells them (O6, for M4 and L10): the computers connected as them (E0c's
        ``token.added``) and the one-time codes made for them, by an administrator or by themselves (E3's
        ``person.code.made``, E0c's ``code.issued``), each for ``NOTICE_DAYS`` after it happened or until they said
        they saw it (``told``), whichever is sooner; and ``until``, which the dashboard sends back once they are on
        screen. Every computer and every code someone else made, however many: whoever adds entries (a session at
        their computer making codes, an administrator making many) pushes none of them out of view. Only the codes
        they made themselves are the newest ``TOLD``, and ``more`` says how many of theirs are left out. None when
        nothing is to tell. Read alone, never written: a /me nobody looks at (a probe, a HEAD, a session someone else
        opened) tells nobody anything away, and without single sign-on nobody can hide it sooner. The entries in the
        order the administration log committed them (its ids); the days counted by this instance's clock, as the
        entries' times were written; the labels and the times alone, never a token, a code or who the administrator
        was."""
        recent = self.edition.context.clock() - NOTICE_DAYS * DAY
        with self.server.db.connection() as conn:
            row = conn.execute("SELECT until FROM notified WHERE identity_id = %s", (identity_id,)).fetchone()
            since = int(row[0]) if row is not None else 0
            # The actions written out, as the index on them names them (301_notices.sql). Every computer and every code
            # someone else made; of the person's own codes the newest TOLD, and how many there are (counted before the
            # cut). One statement, so one snapshot: every entry below the newest it lists was committed before it (the
            # log's ids follow the commits) and is listed, so that saying ``until`` seen says seen nothing unlisted.
            rows = conn.execute(
                "WITH found AS (SELECT a.id, a.ts, a.actor, a.action, t.label, "
                "a.action <> 'token.added' AND a.actor = %s AS mine FROM audit a LEFT JOIN tokens t "
                "ON a.action = 'token.added' AND t.id = a.details->>'token' AND t.identity_id = a.target "
                "WHERE a.action IN ('person.code.made', 'code.issued', 'token.added') AND a.target = %s AND a.id > %s "
                "AND a.ts > %s), ranked AS (SELECT *, row_number() OVER (PARTITION BY mine ORDER BY id DESC) AS nth, "
                "count(*) OVER (PARTITION BY mine) AS mine_total FROM found) "
                "SELECT id, ts, actor, action, label, mine, mine_total FROM ranked WHERE NOT mine OR nth <= %s "
                "ORDER BY id",
                (identity_id, identity_id, since, recent, TOLD),
            ).fetchall()
        if not rows:
            return None
        mine = [row for row in rows if row[5]]
        more = int(mine[0][6]) - len(mine) if mine else 0
        computers: list[dict[str, Any]] = []
        codes: list[dict[str, Any]] = []
        for _id, ts, actor, action, label, _mine, _total in rows:
            if action == "token.added":  # a label of None: disconnected since
                computers.append({"label": label, "connected": float(ts)})
            else:
                codes.append({"made": float(ts), "by_administrator": actor != identity_id})
        return {"until": int(rows[-1][0]), "computers": computers, "codes": codes, "more": more}

    def computers(self, identity_id: str) -> list[dict[str, Any]]:
        """The computers connected to the person's account now (E0b's tokens of theirs), newest first, always: each
        one's label, when it was connected, when it was last used, and whether the one-time code it was connected with
        was made by someone else than them (an administrator: ``by_administrator``; None when the log does not say).
        What the dashboard lists for good, beside the notice of ``connected``, which goes after ``NOTICE_DAYS`` (the
        final review's F4). Read alone; the person's own tokens and nobody else's, never a token or its hash. The code a
        computer was connected with is the last one made for them before it (a new code ends the one before, E0b):
        ``person.added`` (their first, made with them), E0c's ``code.issued`` or E3's ``person.code.made``, written in
        the administration log before the ``token.added`` that names the token, by whom it names."""
        with self.server.db.connection() as conn:
            rows = conn.execute(
                "SELECT t.label, t.created, t.last_used, (SELECT c.actor <> t.identity_id FROM audit c "
                "WHERE c.target = t.identity_id AND c.action IN ('person.added', 'code.issued', 'person.code.made') "
                "AND c.id < a.id ORDER BY c.id DESC LIMIT 1) FROM tokens t LEFT JOIN audit a "
                "ON a.action = 'token.added' AND a.target = t.identity_id AND a.details->>'token' = t.id "
                "WHERE t.identity_id = %s ORDER BY t.created DESC, t.id",
                (identity_id,),
            ).fetchall()
        return [
            {
                "label": label,
                "connected": float(created),
                "last_used": float(last_used) if last_used is not None else None,
                "by_administrator": by_administrator,
            }
            for label, created, last_used, by_administrator in rows
        ]

    async def told(self, request: Request, who: Who) -> Response:
        """``POST /ee/notices/told {until}``: the person saw what their dashboard told them, up to ``until`` (an entry
        of theirs that ``connected`` gave), and it is told no more; whether this call moved it (two at once: one).
        A change of the person's own, weighed in its transaction as the dashboard's changes are, and only from a
        session their company sign-in opened: any other may be whoever holds a computer of theirs (`latet dashboard`'s
        one-time code: an administrator who made a code and connected a computer as them, an agent at their computer),
        whom the server cannot tell from them. Without single sign-on the notice goes by itself after
        ``NOTICE_DAYS``."""
        person = who.identity_id
        if not isinstance(person, str) or not person or request.scope.get(VIA) != "sso":
            raise Refused(403, _TOLD_BY_SSO)
        until = (await json_body(request, who)).get("until")
        if isinstance(until, bool) or not isinstance(until, int) or not 0 < until < 2**63:
            raise Refused(400, _AN_UNTIL)

        def told(within: Changing) -> bool:
            conn = within.conn
            row = conn.execute(
                "SELECT until FROM notified WHERE identity_id = %s FOR UPDATE", (person,)
            ).fetchone()  # held: a second acknowledgement waits for this one, and finds it made
            since = int(row[0]) if row is not None else 0
            named = conn.execute(
                "SELECT 1 FROM audit WHERE id = %s AND target = %s "
                "AND action IN ('person.code.made', 'code.issued', 'token.added')",
                (until, person),
            ).fetchone()
            if named is None:  # never past what was told: an entry still to come stays to tell
                raise Refused(400, _AN_UNTIL)
            if until <= since:
                return False
            moved = conn.execute(
                "INSERT INTO notified (identity_id, until) VALUES (%s, %s) ON CONFLICT (identity_id) DO UPDATE "
                "SET until = EXCLUDED.until WHERE notified.until < EXCLUDED.until RETURNING identity_id",
                (person, until),
            ).fetchone()
            return moved is not None

        return JSONResponse({"ok": True, "told": await who.change(told)})

    # --- `latet server status` ---------------------------------------------------------------------------------

    def check(self) -> Check | None:
        """The Sign-in line of the edition's checks: what is set up, worked out here; None while nothing is (no line,
        as the edition shows nothing of what is not in use). The identity provider is not asked (`latet server
        sign-in test` asks it). A warning for what lets in more people than meant, or soon nobody new (M2), a secret
        not saved for the issuer, a mapping SCIM's groups hold (Task 7: the mapping's own names, never the
        provider's). It never raises: a store that fails is said by its type (R17)."""
        from latet.doctor.checks import OK, WARN, Check

        try:
            sso = self.oidc.settings()
            now = self.tokens.clock()
            live = [token for token in self.tokens.all() if token.expires is None or token.expires > now]
            saved, elsewhere = self._secret(sso)
            taken = self.people.mapping_taken(sso.groups.values()) if sso is not None else []
        except Exception as exc:  # the database not answering: said, never raised. Its words can name its address
            return Check(WARN, "Sign-in", f"its settings could not be read ({type(exc).__name__})")
        if sso is None and not live:
            return None
        count = len(live)
        scim = f"SCIM provisioning on ({count} token{'' if count == 1 else 's'})" if count else "SCIM provisioning off"
        if sso is None:
            return Check(
                OK,
                "Sign-in",
                f"single sign-on is off: any administrator's dashboard session makes the changes that apply to "
                f"everyone; {scim}",
            )
        problems: list[str] = []
        if sso.client_secret is not None and not saved:
            problems.append(
                "the client secret was saved for another issuer: save it again with `latet server sign-in secret`"
                if elsewhere
                else "the client secret is not saved: run `latet server sign-in secret`"
            )
        if sso.accounts == "open":
            problems.append("accounts: open gives an account to anyone the provider signs in")
        elif sso.accounts == "scim":
            ends = [token.expires for token in live if token.expires is not None]
            if not live:
                problems.append("accounts: scim, and no SCIM token serves: nobody new gets an account")
            elif len(ends) == len(live) and max(ends) - now <= WARN_DAYS * DAY:  # none that never ends
                day = datetime.fromtimestamp(max(ends), UTC).date().isoformat()
                problems.append(
                    f"accounts: scim, and the last SCIM token ends on {day}: make the next one with "
                    "`latet server sign-in scim-token`"
                )
        if taken:
            problems.append(f"the mapping names groups SCIM pushed, which no sign-in joins: {', '.join(taken)}")
        company = (
            "changes that apply to everyone take the company sign-in"
            if sso.company_changes == "sso"
            else "changes that apply to everyone take any administrator's session (company_changes: any-session)"
        )
        detail = f"single sign-on with {sso.issuer}; {scim}; {company}"
        if problems:
            return Check(WARN, "Sign-in", f"{detail}; {'; '.join(problems)}")
        return Check(OK, "Sign-in", detail)
