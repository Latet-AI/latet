"""SCIM 2.0 for a Latet server (RFC 7643, RFC 7644): the subset Microsoft Entra ID and Okta use to create, change and
disable a company's people, and to push its groups, which are Latet's groups.

The provider authenticates with a bearer token made on the Sign-in page or with `latet server sign-in scim-token`; the
server keeps its keyed hash only (scim_tokens), with a keyed tag over its row, and a token ends as an administration
token does. Who may get an account at a first sign-in is the sign-in settings' ``accounts``, never whether a token
exists (M2).

SCIM sees and changes only the people it manages (``Account.scim_managed``: those it provisioned, and those an
administrator handed to it with `latet server people link … --scim`): a person added by hand is unknown to it (404),
and their e-mail or user name never hands them over (M3). It never renames the user name of someone who never signed
in when they were handed to it or their account holds anything (a role, an API key's control, a computer, a code to
connect one: the final security review's M1 and L2), which would point their first sign-in at someone else; a rename
it makes of anyone else who never signed in leaves their account to no first sign-in, by either user name, until an
administrator links it again (``People._claimable``: the review's wider option). The code every person is made with is
deleted when SCIM makes them, since SCIM shows it to nobody. It never takes back a deleted person's account that was
used (M2), takes a userName as it is sent or refuses it (L1), never lifts an administrator's disable (L7), and never
disables the last administrator. Once a trial ended it still disables and removes people, and does nothing else (the
user's decision O4): taking access away never manages.

SCIM's groups are Latet's groups whose source is SCIM: it sees and changes no other, their members are only people it
manages (a role given to such a group is given to whoever holds a SCIM token, through the people it provisions), a
group change never leaves the server without an administrator, and once a trial ended no group changes at all (the
coordinator's ruling on O4, which names people only). No SCIM group takes a name the sign-in settings' mapping names
(409, as a name taken): sign-ins would join it, and no sign-in joins a group SCIM pushed (``People._group_named``).
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from latet.ee.api import json_body, read_body
from latet.ee.license import NotLicensed, State, TrialEnded, require_serving
from latet.ee.people import (
    GROUP_ID,
    USER_NAME_LENGTH,
    Group,
    LastAdmin,
    People,
    Taken,
    disable_person,
    enable_person,
    group_name,
    user_name_key,
)
from latet.ee.server import settings_in
from latet.ee.work import working
from latet.server.identities import EMAIL_LENGTH, NAME_LENGTH, EmailTaken, clean
from latet.server.machines import (
    LOOKUP_LIMIT,
    LOOKUP_WINDOW,
    RETRY_AFTER,
    Known,
    StoresUnavailable,
    TooManyMisses,
    address,
    admitted,
    checked,
    client_of,
    from_a_page,
    miss_key,
    pre_auth,
)
from latet.server.tokens import keyed_hash, new_id, new_secret

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.ee.license import Licensing
    from latet.ee.people import Account, Changing
    from latet.server.app import Server
    from latet.server.db import Database
    from latet.server.identities import Identity

TOKEN_PREFIX = "latet_scim_"  # noqa: S105 - a prefix, by which secret scanners know Latet's tokens
DAY = 86400.0
MOST_DAYS = 3650  # a token's life, when one is said: ten years at most
USED_EVERY = 60.0  # a token's use is written at most once a minute


@dataclass(frozen=True)
class ScimToken:
    id: str
    created: float
    expires: float | None  # None: never
    last_used: float | None
    made_by: str = field(default="", repr=False)  # the actor who made it (an identity's id, or "admin:<token id>")


class ScimTokens:
    """The bearer tokens of SCIM: several at a time, so that a new one is given to the provider before the old one is
    removed. Each is shown once; its keyed hash is kept (``key``: the server's credentials' key,
    ``MasterKey.derive(b"credentials")``, as E0b keeps every token), with a keyed tag over its id, hash, end and maker,
    so that a row this server's key did not make, or whose end was moved, finds nothing. ``days``: a token's life
    unless one is said (the server's ``admin_token_days``)."""

    def __init__(self, db: Database, *, key: bytes, days: int = 90, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._key = key
        self.days = days
        self.clock = clock

    def _hash(self, token: str) -> str:
        return keyed_hash(self._key, "scim-token", token)

    def _tag(self, token_id: str, hashed: str, expires: float | None, made_by: str) -> str:
        return keyed_hash(self._key, "scim-token-row", token_id, hashed, repr(expires), made_by)

    @contextlib.contextmanager
    def _within(self, conn: Connection[Any] | None) -> Iterator[Connection[Any]]:
        if conn is not None:
            yield conn
            return
        with self.db.transaction() as own:
            yield own

    def make(self, *, made_by: str, days: int | None = 0, conn: Connection[Any] | None = None) -> tuple[ScimToken, str]:
        """A new token, shown once, ending in ``days`` (0: ``self.days``; None: never, which only the administration
        gives), made by ``made_by``. Given ``conn``, it is written in the caller's transaction, beside the entry that
        records it."""
        if not made_by:
            raise ValueError("a SCIM token names who made it")
        if days is not None and not 0 <= days <= MOST_DAYS:
            raise ValueError(f"a SCIM token lasts 1 to {MOST_DAYS} days, or never")
        token, now = new_secret(TOKEN_PREFIX), self.clock()
        expires = None if days is None else now + (days or self.days) * DAY
        made, hashed = ScimToken(new_id("s_"), now, expires, None, made_by), self._hash(token)
        with self._within(conn) as tx:
            tx.execute(
                "INSERT INTO scim_tokens (id, hash, tag, created, expires, made_by) VALUES (%s, %s, %s, %s, %s, %s)",
                (made.id, hashed, self._tag(made.id, hashed, expires, made_by), now, expires, made_by),
            )
        return made, token

    def all(self, *, conn: Connection[Any] | None = None) -> list[ScimToken]:
        """Every token's row; given ``conn``, as its transaction finds them."""
        sql = "SELECT id, created, expires, last_used, made_by FROM scim_tokens ORDER BY created, id"
        if conn is not None:
            rows = conn.execute(sql).fetchall()
        else:
            with self.db.connection() as own:
                rows = own.execute(sql).fetchall()
        return [ScimToken(*row) for row in rows]

    def remove(self, token_id: str, *, conn: Connection[Any] | None = None) -> bool:
        with self._within(conn) as tx:
            return tx.execute("DELETE FROM scim_tokens WHERE id = %s", (token_id,)).rowcount > 0

    def any(self) -> bool:
        """A token that has not ended exists (what the Sign-in line of `latet server status` says; never who gets an
        account)."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM scim_tokens WHERE expires IS NULL OR expires > %s LIMIT 1", (self.clock(),)
            ).fetchone()
        return row is not None

    def check(self, token: str) -> str | None:
        """The id of the token ``token`` is, when its row holds and it has not ended, its use noted at most once a
        minute; None for any other. Scim asks it through the shared limiter, in the credentials' threads."""
        if not token.startswith(TOKEN_PREFIX) or not token.isascii():
            return None
        hashed, now = self._hash(token), self.clock()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, tag, expires, made_by FROM scim_tokens WHERE hash = %s", (hashed,)
            ).fetchone()
            if row is None:
                return None
            token_id, tag, expires, made_by = row
            if not hmac.compare_digest(tag, self._tag(token_id, hashed, expires, made_by)):
                return None  # a row this server's key did not make, or whose end or maker was moved
            if expires is not None and expires <= now:
                return None
            conn.execute(
                "UPDATE scim_tokens SET last_used = %s WHERE id = %s AND (last_used IS NULL OR last_used < %s)",
                (now, token_id, now - USED_EVERY),
            )
        return token_id


# --- the routes -----------------------------------------------------------------------------------------------------

log = logging.getLogger(__name__)

BASE = "/scim/v2"
MEDIA = "application/scim+json"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
MAX_RESULTS = 200
FILTER_LENGTH = 1024  # characters of a filter read at most: a provider's are a few dozen
_MAX_BODY = 1_000_000
_FILTER = re.compile(r'^\s*(\w+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
_EMAILS = re.compile(r"^\s*emails\s*(\[[^\]]*\])?\s*(\.\s*value)?\s*$", re.IGNORECASE)
_MEMBER = re.compile(r'^\s*members\s*\[\s*value\s+eq\s+"((?:[^"\\]|\\.)*)"\s*\]\s*$', re.IGNORECASE)
_NAME_PARTS = {"givenname": "givenName", "familyname": "familyName", "formatted": "formatted"}
_CHANGES = frozenset({"POST", "PUT", "PATCH", "DELETE"})  # what a request does that is not a read
_READS = ("GET", "HEAD")  # Starlette answers HEAD on a route that has GET
NOT_A_MEMBER = "a group's members are people SCIM manages: one named is not"
MEMBER_VALUE = 'a members value is a list of {"value": "<a person\'s id>"}, each naming a person'
GROUP_NAME_TAKEN = "a group has this displayName or externalId in Latet already: rename or remove it there first"
GROUP_NAME_RULE = (
    "a group's displayName is one line of 1 to 80 characters, with no control or bidirectional character, and not "
    "shaped like a group's id"
)
EXTERNAL_ID_RULE = "an externalId is one line with no control or bidirectional character, and not too long"
LAST_ADMIN_LEFT = "this would leave the server without an administrator: make someone else administrator first"
NOT_LICENSED = "provisioning needs Latet Enterprise with single sign-on (sso)"
UNAVAILABLE = "the server cannot answer now: try again"
NOT_SCIMS = (
    "a person SCIM does not manage has this userName: an administrator hands them to SCIM "
    "(`latet server people link … --scim`)"
)
EMAIL_TAKEN = "another enabled person has this e-mail"
USER_NAME_RULE = (
    f"a userName is one line of at most {USER_NAME_LENGTH} characters, as Latet keeps it: no white space around it, "
    "and no control or bidirectional character"
)
RENAME_REFUSED = (
    "the userName of a person who never signed in is not SCIM's to change while an administrator handed them to SCIM, "
    "or their account holds a role, an API key's control, a computer or a code to connect one: an administrator links "
    "them by hand (`latet server people link`)"
)
DELETED_IN_USE = (
    "a person deleted at the provider has this userName, and their account was used or holds a role, an API key's "
    "control or a code to connect a computer: an administrator removes them first (`latet server people remove`), "
    "or, for the same person back, hands them back to SCIM (`latet server people link … --scim`)"
)


class ScimError(Exception):
    """What a SCIM request is answered when it cannot be done (RFC 7644, 3.12); ``more``: what E1's refusals say
    beside it (``code: "trial-ended"``). Its words are fixed: never what the provider sent."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None, **more: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.more = more


async def _in_work[T](work: Callable[..., T], *args: Any) -> T:
    """``work(*args)`` in E0b's work threads, a ScimError it raises answered as SCIM's (ee/work.py: R8)."""
    return await working(work, *args, refusals=(ScimError,))


@dataclass(frozen=True)
class Caller:
    """Who makes a SCIM request, for the administration log: its token (``scim:<id>``), and from where."""

    actor: str
    address: str | None


@dataclass(frozen=True)
class _Held:
    """A person SCIM manages, as the change's transaction finds them, their rows held until it ends."""

    id: str
    source: str
    email: str | None = field(repr=False)
    disabled: bool
    user_name: str | None = field(repr=False)
    subject: str | None = field(repr=False)
    external_id: str | None = field(repr=False)
    scim: dict[str, Any] | None = field(repr=False)
    deleted: bool


def _error(status: int, detail: str, scim_type: str | None = None, **more: str) -> JSONResponse:
    """SCIM's error (RFC 7644, 3.12); ``more``: what E1's refusals say beside it (``code: "trial-ended"``)."""
    body: dict[str, Any] = {"schemas": [ERROR], "status": str(status), "detail": detail, **more}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def _read(method: str, answer: Response) -> Response:
    """A read's answer: the GET's, and for HEAD its status and headers without its body (RFC 9110, 9.3.2)."""
    if method == "GET":
        return answer
    return Response(status_code=answer.status_code, headers=dict(answer.headers))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def flag(value: Any) -> bool | None:
    """A SCIM boolean as providers send it: true or false, or Entra ID's strings "True" and "False"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _number(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _filter(text: str, attributes: tuple[str, ...], names: str) -> tuple[str, str]:
    """``<attribute> eq "<value>"``, the filter providers send to find a person or a group; no other. The refusal
    names what Latet takes (``names``), never what was sent."""
    found = _FILTER.match(text) if len(text) <= FILTER_LENGTH else None
    if found is None or found.group(1).lower() not in attributes:
        raise ScimError(400, f"the filters Latet takes: {names} eq a quoted value", "invalidFilter")
    return found.group(1).lower(), _unescape(found.group(2))


def _email(value: Any) -> str | None:
    """The e-mail of an ``emails`` value: a string, an object with ``value``, or a list (its primary one, else its work
    one, else its first)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        return _email(value.get("value"))
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, Mapping)]
        chosen = (
            next((item for item in items if flag(item.get("primary"))), None)
            or next((item for item in items if item.get("type") == "work"), None)
            or (items[0] if items else None)
        )
        return _email(chosen) if chosen is not None else None
    return None


def _attribute(profile: dict[str, Any], path: str, value: Any, op: str) -> None:
    """One attribute into ``profile``, what Latet keeps of a user: userName, externalId, displayName, name and its
    parts, the e-mail, active. Any other is accepted and not kept."""
    key = path.strip()
    if key.lower().startswith(USER.lower() + ":"):  # the core schema written out
        key = key[len(USER) + 1 :]
    lower = key.lower()
    removing = op == "remove"
    text = value.strip() if isinstance(value, str) and value.strip() and not removing else None
    if lower == "username":
        if isinstance(value, str) and value and not removing:  # as sent, never stripped (``_checked``: L1)
            profile["user_name"] = value
    elif lower == "externalid":
        profile["external_id"] = text
    elif lower == "displayname":
        profile["display_name"] = text
    elif lower == "active":
        if not removing and (active := flag(value)) is not None:
            profile["active"] = active
    elif lower == "name":
        if removing:
            profile["name"] = {}
        elif isinstance(value, Mapping):
            for part, item in value.items():
                _attribute(profile, f"name.{part}", item, op)
    elif lower.startswith("name.") and (part := _NAME_PARTS.get(lower[5:])) is not None:
        profile.setdefault("name", {})[part] = text
    elif _EMAILS.match(key):
        profile["email"] = None if removing else _email(value)


def _operate(apply: Callable[[str, Any, str], None], operation: Any) -> None:
    """One operation of a PATCH (RFC 7644, 3.5.2), its op in any case (Entra ID writes "Replace"); with no path, its
    value is an object of attributes (as Okta sends them)."""
    if not isinstance(operation, Mapping):
        raise ScimError(400, "an operation is an object", "invalidSyntax")
    op = str(operation.get("op", "")).lower()
    if op not in ("add", "replace", "remove"):
        raise ScimError(400, "an operation's op is add, replace or remove", "invalidSyntax")
    path, value = operation.get("path"), operation.get("value")
    if path is None:
        if not isinstance(value, Mapping):
            raise ScimError(400, "an operation without a path carries an object", "invalidSyntax")
        for key, item in value.items():
            apply(str(key), item, op)
    elif isinstance(path, str):
        apply(path, value, op)
    else:
        raise ScimError(400, "a path is a string", "invalidPath")


def _operations(body: Mapping[str, Any]) -> list[Any]:
    operations = body.get("Operations", body.get("operations"))
    if not isinstance(operations, list):
        raise ScimError(400, "a PATCH carries its Operations", "invalidSyntax")
    return operations


def _display(profile: Mapping[str, Any]) -> str | None:
    """The name the profile gives the person: its displayName, else its formatted name, else its parts joined; None
    when it says none (the person's name is then left as it is: F12)."""
    name = profile.get("name") or {}
    joined = " ".join(part for part in (name.get("givenName"), name.get("familyName")) if part)
    return profile.get("display_name") or name.get("formatted") or joined or None


def _text(value: str | None, limit: int, what: str) -> None:
    """A name or an e-mail SCIM writes, refused (400, naming nothing of it) past ``limit`` characters or with a control
    or bidirectional character, which E0b's ``clean`` would take out unsaid (L4: People' rule)."""
    if value is not None and (len(value) > limit or clean(value, limit) != value):
        raise ScimError(
            400, f"{what} is one line of at most {limit} characters, with no control character", "invalidValue"
        )


def _checked(profile: Mapping[str, Any]) -> None:
    """What of ``profile`` E0b's identity keeps, checked before anything is written (the rest is People's to check),
    and its userName as the provider sent it: one with white space around it, or a character Latet never keeps, is
    refused (400), never stripped, since stripped it could be another person's (the final security review's L1)."""
    _text(_display(profile), NAME_LENGTH, "a name")
    _text(profile.get("email"), EMAIL_LENGTH, "an e-mail")
    user_name = profile.get("user_name")
    if isinstance(user_name, str) and clean(user_name, USER_NAME_LENGTH) != user_name:
        raise ScimError(400, USER_NAME_RULE, "invalidValue")


async def _body(request: Request) -> dict[str, Any]:
    """The request's body as a JSON object, read as E1 reads one (``read_body``: 413 past the limit; ``json_body``:
    NaN and the infinities refused), parsed in a work thread."""
    raw = await read_body(request, _MAX_BODY)
    if isinstance(raw, Response):
        raise ScimError(413, "the request is too large")
    try:
        found = await working(json_body, raw or b"{}", refusals=(ValueError, RecursionError))
    except (ValueError, RecursionError):
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from None
    if not isinstance(found, dict):
        raise ScimError(400, "the body is a JSON object", "invalidSyntax")
    return found


def _range(query: Mapping[str, str]) -> tuple[int, int]:
    """The page a provider asks for: ``startIndex`` at least 1, ``count`` from 0 to MAX_RESULTS, anything that is not
    a whole number their default."""
    start = max(1, _number(query.get("startIndex"), 1))
    count = max(0, min(MAX_RESULTS, _number(query.get("count"), MAX_RESULTS)))
    return start, count


def _page(items: list[Any], query: Mapping[str, str]) -> tuple[int, list[Any]]:
    start, count = _range(query)
    return start, items[start - 1 : start - 1 + count]


def _listed(resources: list[dict[str, Any]], total: int, start: int) -> JSONResponse:
    body = {
        "schemas": [LIST], "totalResults": total, "startIndex": start, "itemsPerPage": len(resources),
        "Resources": resources,
    }  # fmt: skip
    return JSONResponse(body, media_type=MEDIA)


def _members_named(value: Any) -> set[str] | None:
    """The people's ids a ``members`` value names: a list of ``{"value": id}`` (or one of them alone); None when no
    value is given. A value given is refused (400, quoting nothing of it) unless each of its items names an id: one
    that names none is never read as no value, which a removal takes for every member (RFC 7644, 3.5.2.2)."""
    if value is None:
        return None
    items = [value] if isinstance(value, Mapping) else value if isinstance(value, list) else None
    if items is None or not all(
        isinstance(item, Mapping) and isinstance(item.get("value"), str) and item["value"] for item in items
    ):
        raise ScimError(400, MEMBER_VALUE, "invalidValue")
    return {item["value"] for item in items}


@dataclass
class _Group:
    """A SCIM group as a request leaves it, before it is kept: its name, externalId and the ids of its members from
    SCIM (the people SCIM manages; which they are is judged where it is written). Printed, it names nothing."""

    name: str = field(repr=False)
    external_id: str | None = field(repr=False)
    members: set[str] = field(default_factory=set, repr=False)

    def apply(self, path: str, value: Any, op: str) -> None:
        """One attribute of a POST or a PUT (``op`` "replace"), or one operation of a PATCH; any attribute Latet does
        not keep is accepted and not kept (``id``, ``schemas``, ``meta``)."""
        key = path.strip()
        if key.lower().startswith(GROUP.lower() + ":"):  # the core schema written out
            key = key[len(GROUP) + 1 :]
        lower = key.lower()
        if (member := _MEMBER.match(key)) is not None:
            if op == "remove":  # Entra ID: members[value eq "<id>"]
                self.members.discard(_unescape(member.group(1)))
        elif lower == "displayname":
            if op == "remove" or not isinstance(value, str):
                raise ScimError(400, "a group's displayName is required, and is text", "invalidValue")
            self.name = value
        elif lower == "externalid":
            self.external_id = value.strip() if op != "remove" and isinstance(value, str) and value.strip() else None
        elif lower == "members":
            ids = _members_named(value)
            if op == "add":
                self.members |= ids or set()
            elif op == "replace":
                self.members = ids or set()
            elif ids is None:  # remove with no value: every member (RFC 7644, 3.5.2.2)
                self.members = set()
            elif ids:  # remove: those named
                self.members -= ids
            else:  # a value given that names no one: never every member
                raise ScimError(400, MEMBER_VALUE, "invalidValue")


def _excluded(query: Mapping[str, str]) -> bool:
    """Whether a request asks for groups without their members (``excludedAttributes=members``, as Entra ID does)."""
    asked = query.get("excludedAttributes") or ""
    return "members" in {part.strip().lower() for part in asked.split(",")}


def _described(name: str, kind: str = "string", **more: Any) -> dict[str, Any]:
    """An attribute of a schema, as RFC 7643's section 7 describes one."""
    return {
        "name": name, "type": kind, "multiValued": False, "required": False, "caseExact": False,
        "mutability": "readWrite", "returned": "default", "uniqueness": "none", **more,
    }  # fmt: skip


_USER_ATTRIBUTES = [
    _described("userName", required=True, uniqueness="server"),
    _described("externalId", caseExact=True),
    _described("displayName"),
    _described(
        "name", "complex", subAttributes=[_described(part) for part in ("formatted", "familyName", "givenName")]
    ),
    _described(
        "emails",
        "complex",
        multiValued=True,
        subAttributes=[_described("value"), _described("type"), _described("primary", "boolean")],
    ),
    _described("active", "boolean"),
]
_GROUP_ATTRIBUTES = [
    _described("displayName", required=True, uniqueness="server"),
    _described("externalId", caseExact=True),
    _described(
        "members",
        "complex",
        multiValued=True,
        subAttributes=[_described("value", caseExact=True), _described("display", mutability="readOnly")],
    ),
]


Handler = Callable[[Request, Caller], Awaitable[Response]]


class Scim:
    """The SCIM routes of a server, ``/scim/v2/…``. A request from a web page, or in clear to an https server, is
    refused; its bearer token is looked up through the shared limiter, as a computer's token is (R10); it is answered
    only while the license serves sso (a paid license paused still serves: R2 to R4), and once a trial ended only to
    read, disable and remove people (O4: 403 ``code: "trial-ended"`` to anything else, every change of a group
    included). Every change is one transaction with its entries in the administration log, by the token that made it
    (``scim:<token id>``), the license weighed again in it (F18)."""

    def __init__(
        self,
        server: Server,
        people: People,
        tokens: ScimTokens,
        licensing: Licensing,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.server = server
        self.people = people
        self.tokens = tokens
        self.licensing = licensing
        self.clock = clock
        self.base = server.settings.url.rstrip("/") + BASE
        self._https = server.settings.url.startswith("https://")
        # The SCIM tokens found valid in the last hour, on any instance (E0b's Known): one skips its address's limit.
        self.known = Known(server.bus, server.master.derive(b"known"), kind="scim")

    def routes(self) -> list[BaseRoute]:
        guarded = self._guarded
        return [
            Route(f"{BASE}/Users", guarded(self.list_users), methods=["GET"]),
            Route(f"{BASE}/Users", guarded(self.create_user), methods=["POST"]),
            Route(f"{BASE}/Users/{{id}}", guarded(self.user), methods=["GET", "PUT", "PATCH", "DELETE"]),
            Route(f"{BASE}/Groups", guarded(self.list_groups), methods=["GET"]),
            Route(f"{BASE}/Groups", guarded(self.create_group, _CHANGES), methods=["POST"]),
            Route(f"{BASE}/Groups/{{id}}", guarded(self.group, _CHANGES), methods=["GET", "PUT", "PATCH", "DELETE"]),
            Route(f"{BASE}/ServiceProviderConfig", guarded(self.service_provider_config), methods=["GET"]),
            Route(f"{BASE}/ResourceTypes", guarded(self.resource_types), methods=["GET"]),
            Route(f"{BASE}/Schemas", guarded(self.schemas), methods=["GET"]),
        ]

    async def _token_of(self, token: str, bucket: str) -> str | None:
        """The id of the SCIM token ``token`` is, checked as E0b checks a computer's token (``secret_identity``): by the
        limiter of the client's bucket, one found valid in the last hour skipping its slot and looked up all the same.
        None for a text that is no SCIM token, at no cost. Raises TooManyMisses, StoresUnavailable."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        key = miss_key(bucket)
        if (elsewhere := await admitted(self.known, self.server.limits, token, key)) is None:
            raise TooManyMisses
        found, limited = await pre_auth(
            checked, self.server.bus, self.known, self.server.limits, token, key, LOOKUP_LIMIT, LOOKUP_WINDOW,
            self.tokens.check, elsewhere,
        )  # fmt: skip
        if limited:
            raise TooManyMisses
        return found

    def _serves(self) -> bool:
        """Whether the license serves sso as this instance sees it (NotLicensed when it does not list it, or cannot be
        used); False once a trial ended, when SCIM only reads, disables and removes (O4)."""
        try:
            require_serving(self.licensing, "sso")
        except TrialEnded:
            return False
        return True

    def _guarded(
        self, handler: Handler, ended: frozenset[str] = frozenset({"POST"})
    ) -> Callable[[Request], Awaitable[Response]]:
        """``handler`` behind SCIM's checks; ``ended``: the methods refused once a trial ended, before the request's
        body is read (a user's POST: O4; every change of a group: the coordinator's ruling)."""

        async def endpoint(request: Request) -> Response:
            if from_a_page(request.scope):
                return _error(403, "SCIM requests do not come from web pages")
            if self._https and request.url.scheme != "https":  # as E0b's administration (L11): the token never in clear
                return _error(403, "SCIM answers over HTTPS only")
            scheme, _, given = request.headers.get("authorization", "").partition(" ")
            try:
                token_id = await self._token_of(given.strip() if scheme.lower() == "bearer" else "", address(request))
                serves = token_id is None or await working(self._serves, refusals=(NotLicensed,))
            except TooManyMisses:
                refused = _error(429, "too many refused tokens from this address: wait a minute")
                refused.headers["retry-after"] = RETRY_AFTER
                return refused
            except NotLicensed:
                return _error(403, NOT_LICENSED)
            except StoresUnavailable:
                return _error(503, UNAVAILABLE)
            if token_id is None:
                return _error(401, "a valid bearer token is needed")
            if not serves and request.method in ended:  # nothing made, no group changed, once a trial ended (O4)
                return await self._ended()
            caller = Caller(f"scim:{token_id}", client_of(request.scope))
            try:
                return await handler(request, caller)
            except ScimError as exc:
                return _error(exc.status, exc.detail, exc.scim_type, **exc.more)
            except StoresUnavailable:
                return _error(503, UNAVAILABLE)
            except Exception as exc:  # by its type: its words can quote what the provider sent
                log.error("SCIM error on %s (%s)", request.method, type(exc).__name__)
                return _error(500, "internal error")

        return endpoint

    async def _ended(self) -> Response:
        """403 ``trial-ended``, in E1's words (the trial's last day); what SCIM made stays (R3)."""
        try:
            await working(require_serving, self.licensing, "sso", refusals=(NotLicensed, TrialEnded))
        except TrialEnded as exc:
            return _error(403, str(exc), code="trial-ended")
        except (NotLicensed, StoresUnavailable):
            pass
        return _error(403, NOT_LICENSED)

    def _license(self, within: Changing) -> TrialEnded | None:
        """The license as the change's transaction finds it (F18), its row held there until the change commits: a
        ScimError 403 unless it serves sso; the trial that ended, when one did (the caller then only disables and
        removes: O4); None while it serves."""
        standing = self.licensing.standing_in(within.conn)
        lic = standing.license
        if standing.state is State.INVALID or lic is None or "sso" not in lic.features:
            raise ScimError(403, NOT_LICENSED)
        return TrialEnded(lic.expires) if standing.state is State.TRIAL_ENDED else None

    def _keeping(self, within: Changing) -> TrialEnded | None:
        """How each of SCIM's changes begins: the license as its transaction finds it (``_license``, F18), then the lock
        that every change keeping the last administrator takes (``People.hold_admins``), before any row of a person, an
        account or a group is held. A change of SCIM's may disable a person or take a member out, each keeping the last
        administrator: one that held those rows and then waited for the lock would wait for a disable from the page,
        which holds the lock and waits for the person's rows (a deadlock, which PostgreSQL ends with a 503)."""
        ended = self._license(within)
        self.people.hold_admins(within.conn)
        return ended

    # --- users ------------------------------------------------------------------------------------------------

    def _pair(self, identity_id: str | None) -> tuple[Identity, Account] | None:
        """The person and their account, when SCIM manages them (M3); None when there is no such person, SCIM does
        not manage them, or SCIM deleted them."""
        identity = self.server.identities.current(identity_id) if identity_id else None
        if identity is None or identity.kind != "person":
            return None
        account = self.people.account(identity.id)
        if account is None or not account.scim_managed or account.deleted:
            return None
        return identity, account

    def _resource(self, identity: Identity, account: Account) -> dict[str, Any]:
        kept = account.scim or {}
        resource: dict[str, Any] = {
            "schemas": [USER],
            "id": identity.id,
            "userName": account.user_name or identity.email or identity.id,
            "displayName": identity.name,
            "active": not identity.disabled,
            "meta": {
                "resourceType": "User",
                "created": _iso(identity.created),
                "lastModified": _iso(account.modified),
                "location": f"{self.base}/Users/{identity.id}",
            },
        }
        if account.external_id:
            resource["externalId"] = account.external_id
        if kept.get("name"):
            resource["name"] = kept["name"]
        if identity.email:
            resource["emails"] = [{"value": identity.email, "type": "work", "primary": True}]
        return resource

    def _answer(self, identity_id: str) -> dict[str, Any]:
        pair = self._pair(identity_id)
        if pair is None:
            raise ScimError(404, "no such user")
        return self._resource(*pair)

    def _matching(self, text: str) -> list[tuple[Identity, Account]]:
        """The person SCIM manages that ``text`` (a filter) finds: one at most."""
        attribute, value = _filter(text, ("username", "externalid"), "userName or externalId")
        if attribute == "username":
            identity_id = self.people.find(user_name=value, deleted=False)
        else:
            identity_id = self.people.find(external_id=value, deleted=False)
        pair = self._pair(identity_id)
        return [pair] if pair is not None else []

    async def list_users(self, request: Request, caller: Caller) -> Response:
        """``GET /Users``: with a filter, the one person it finds; without one, a page of everyone SCIM manages, paged
        in PostgreSQL (``People.scim_page``, E3's release gate 3), never every account read for each page."""
        query = request.query_params
        if text := query.get("filter"):
            found = await _in_work(self._matching, text)
            start, page = _page(found, query)
            return _listed([self._resource(*pair) for pair in page], len(found), start)
        start, count = _range(query)
        total, page = await _in_work(self.people.scim_page, start, count)
        return _listed([self._resource(*pair) for pair in page], total, start)

    async def create_user(self, request: Request, caller: Caller) -> Response:
        profile: dict[str, Any] = {"name": {}}
        for key, value in (await _body(request)).items():
            _attribute(profile, str(key), value, "replace")
        if not profile.get("user_name"):
            raise ScimError(400, "userName is required", "invalidValue")
        identity_id = await _in_work(self._create, profile, caller)
        resource = await _in_work(self._answer, identity_id)
        location = {"location": resource["meta"]["location"]}  # RFC 7644, 3.3
        return JSONResponse(resource, status_code=201, media_type=MEDIA, headers=location)

    async def user(self, request: Request, caller: Caller) -> Response:
        """GET (and HEAD, which Starlette adds to a route that has GET: the GET's answer without its body), PUT, PATCH
        and DELETE of one person; a read never reaches a change's path."""
        identity_id, method = request.path_params["id"], request.method
        if method in _READS:
            return _read(method, JSONResponse(await _in_work(self._answer, identity_id), media_type=MEDIA))
        if method == "DELETE":
            await _in_work(self._delete, identity_id, caller)
            return Response(status_code=204)
        if method not in ("PUT", "PATCH"):  # the route's methods are these alone
            return _error(405, "not a method of SCIM's users")
        body = await _body(request)
        await _in_work(self._change, identity_id, body, method == "PUT", caller)
        return JSONResponse(await _in_work(self._answer, identity_id), media_type=MEDIA)

    def _held(self, conn: Connection[Any], identity_id: str) -> _Held | None:
        """The person ``identity_id`` as the transaction of ``conn`` finds them, their identity's row then their
        account's held until it ends (the order every change of People takes them in), when SCIM manages them (M3);
        None otherwise."""
        row = conn.execute(
            "SELECT source, email, disabled FROM identities WHERE id = %s AND kind = 'person' FOR UPDATE",
            (identity_id,),
        ).fetchone()
        if row is None:
            return None
        account = conn.execute(
            "SELECT user_name, subject, external_id, scim, deleted FROM accounts "
            "WHERE identity_id = %s AND scim_managed FOR UPDATE",
            (identity_id,),
        ).fetchone()
        if account is None:
            return None
        source, email, disabled = row
        user_name, subject, external_id, scim, deleted = account
        return _Held(identity_id, source, email, bool(disabled), user_name, subject, external_id, scim, bool(deleted))

    def _create(self, profile: dict[str, Any], caller: Caller) -> str:
        """A person made, or one SCIM manages taken back (deleted at the provider before, or handed to SCIM by an
        administrator and not provisioned yet), with their account and its entries in one transaction (R6): a person
        made twice at once leaves one, the other's transaction rolled back by the userName's uniqueness. An account of
        that userName SCIM does not manage is never taken (M3): 409. Nor is a deleted person's account that someone
        signed in to, or that holds a computer, a role or an API key's control (``_holds_something``): the same userName
        can be another person's now (the final security review's M2), and an administrator removes the old person or
        hands them back to SCIM (`latet server people link … --scim`)."""
        _checked(profile)
        user_name = profile["user_name"]
        with self.people.changing(caller.actor, caller.address) as within:
            if (ended := self._keeping(within)) is not None:
                raise ScimError(403, str(ended), code="trial-ended")
            conn = within.conn
            row = conn.execute(  # A to Z's case aside, nothing else folded, as the unique index (``user_name_key``)
                'SELECT identity_id FROM accounts WHERE lower(user_name COLLATE "C") = lower(%s::text COLLATE "C")',
                (user_name,),
            ).fetchone()
            if row is not None:
                held = self._held(conn, row[0])
                if held is None or held.user_name is None or user_name_key(held.user_name) != user_name_key(user_name):
                    raise ScimError(409, NOT_SCIMS, "uniqueness")  # not SCIM's, or its user name changed meanwhile
                if held.scim is not None and not held.deleted:
                    raise ScimError(409, "a user with this userName exists", "uniqueness")
                if held.deleted and (held.subject is not None or self._holds_something(within, held.id)):
                    # A user name is given again at a provider (a namesake, a rehire with a new account): what the
                    # deleted person left in their account goes back to no one by it (the final security review's M2).
                    raise ScimError(409, DELETED_IN_USE, "uniqueness")
                self._write(held, profile, within, takeover=True)
                return held.id
            try:
                identity, _code = self.server.identities.add_person(
                    _display(profile) or user_name, profile.get("email"), source="scim", within=within.changes
                )
            except EmailTaken:
                raise ScimError(409, EMAIL_TAKEN, "uniqueness") from None
            # The one-time code every new person is made with: SCIM shows it to nobody, and a code that waits for a
            # computer is something the account holds (``_holds_something``: the final security review's L2).
            conn.execute("DELETE FROM codes WHERE identity_id = %s", (identity.id,))
            within.note("person.added", identity.id, {"source": "scim"})
            held = self._held(conn, identity.id)
            if held is None:  # its account is made by the first link: one with nothing in it yet
                held = _Held(identity.id, "scim", identity.email, False, None, None, None, None, False)
            self._write(held, profile, within, takeover=True)
        return identity.id

    def _change(self, identity_id: str, body: Mapping[str, Any], whole: bool, caller: Caller) -> None:
        """A PUT (``whole``) or a PATCH of a person SCIM manages, in one transaction with its entries. Once a trial
        ended, only a disable is made of it (O4), anything else refused: 403 ``trial-ended``."""
        with self.people.changing(caller.actor, caller.address) as within:
            ended = self._keeping(within)
            held = self._held(within.conn, identity_id)
            if held is None or held.deleted:
                raise ScimError(404, "no such user")
            if whole:
                profile: dict[str, Any] = {"name": {}}
                for key, value in body.items():
                    _attribute(profile, str(key), value, "replace")
            else:
                kept = held.scim or {}
                profile = {
                    "user_name": held.user_name or held.email or held.id,
                    "external_id": held.external_id,
                    "display_name": kept.get("displayName"),
                    "name": dict(kept.get("name") or {}),
                    "email": held.email,
                }
                for operation in _operations(body):
                    _operate(lambda path, value, op: _attribute(profile, path, value, op), operation)
            if ended is not None:  # before anything else of it is judged: a disable needs no userName
                if profile.get("active") is not False:
                    raise ScimError(403, str(ended), code="trial-ended")
                if not held.disabled:  # the disable alone: nothing else of it is written
                    self._disable(identity_id, within)
                return
            if not profile.get("user_name"):
                raise ScimError(400, "userName is required", "invalidValue")
            _checked(profile)
            self._write(held, profile, within)

    def _holds_something(self, within: Changing, identity_id: str) -> bool:
        """Whether the person's account holds anything that a first sign-in claiming it would take, as the
        transaction finds it (the final security review's M1 and M2): a role, or an API key's control, given to them or
        to a local group of theirs (made by hand); a computer connected now, or one that was used (``last_seen``:
        their space, history and vault, stays when it is disconnected); or a code to connect one that has not ended,
        which an administrator made (L2: the computer it connects would go to the account's new owner). The code every
        new person is made with (``Identities.add_person``) is deleted when SCIM makes them (``_create``), since SCIM
        shows it to nobody; a code that ended connects nothing."""
        row = within.conn.execute(
            "WITH local AS (SELECT m.group_id FROM members m JOIN groups g ON g.id = m.group_id "
            "WHERE m.identity_id = %(id)s AND g.source = 'local') "
            "SELECT 1 FROM identities i WHERE i.id = %(id)s AND (i.last_seen IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM tokens t WHERE t.identity_id = i.id) "
            "OR EXISTS (SELECT 1 FROM codes c WHERE c.identity_id = i.id AND c.expires > %(now)s) "
            "OR EXISTS (SELECT 1 FROM roles r WHERE r.subject = i.id OR r.subject IN (SELECT group_id FROM local)) "
            "OR EXISTS (SELECT 1 FROM key_controllers k WHERE k.subject = i.id "
            "OR k.subject IN (SELECT group_id FROM local)))",
            {"id": identity_id, "now": self.clock()},
        ).fetchone()
        return row is not None

    def _disable(self, identity_id: str, within: Changing) -> None:
        """SCIM's disable (``disabled_by = 'scim'``: L7), never of the last administrator (409)."""
        try:
            disable_person(self.people, identity_id, by="scim", within=within, keep_admin=True)
        except LastAdmin:
            raise ScimError(
                409, "this is the server's last administrator: make someone else administrator first"
            ) from None

    def _write(self, held: _Held, profile: Mapping[str, Any], within: Changing, *, takeover: bool = False) -> None:
        """``profile`` kept for the person, in the transaction of ``within``: their name, e-mail and state as E0b's
        identity, the rest in their account. A name the profile does not give, and an e-mail or an externalId it does
        not name, are left as they are (F12)."""
        identity_id, user_name = held.id, profile["user_name"]
        if (
            held.user_name is not None
            and user_name_key(held.user_name) != user_name_key(user_name)
            and held.subject is None
            and (held.source != "scim" or self._holds_something(within, identity_id))
        ):  # their first sign-in finds them by it: pointed elsewhere, it would hand them, and what their account holds,
            # to whoever has that name at the provider (M3: a leaked token must not rename an administrator who never
            # signed in; the final security review's M1: nor someone whose computer or key control the account holds)
            raise ScimError(400, RENAME_REFUSED, "mutability")
        # A rename made here (A to Z's case aside) of someone who never signed in drops their account's claim
        # (``People._claimable``): a leaked token renaming them to another provider account's user name, then back,
        # leaves that account's first sign-in nothing to take; an administrator links them again by hand.
        name = {part: text for part, text in (profile.get("name") or {}).items() if text}
        try:
            self.people.link(
                identity_id,
                source="scim",
                within=within,
                user_name=user_name,
                external_id=profile.get("external_id", held.external_id),  # left out: kept
                scim={"name": name, "displayName": profile.get("display_name")},
                deleted=False,
                scim_managed=True,
            )
        except Taken:
            raise ScimError(409, "another user has this userName or externalId", "uniqueness") from None
        except ValueError:  # People's checks (L4): a control or bidirectional character, or past its length
            raise ScimError(
                400, "a userName, externalId or name holds a control character or is too long", "invalidValue"
            ) from None
        email: str | None = None  # not named: left as it is
        if "email" in profile:
            email = profile["email"] or ""  # named empty, or removed: taken away
        try:
            current, fields = self.server.identities.update(
                identity_id, name=_display(profile), email=email, within=within.changes
            )
        except EmailTaken:
            raise ScimError(409, EMAIL_TAKEN, "uniqueness") from None
        if fields:
            within.note("person.changed", identity_id, {"fields": sorted(fields)})
        active = profile.get("active")
        if current is not None and active is False and not current.disabled:
            self._disable(identity_id, within)
        elif current is not None and current.disabled and (active is True or (takeover and active is None)):
            try:  # SCIM lifts only a disable of its own (L7)
                enable_person(self.people, identity_id, by="scim", within=within)
            except EmailTaken:
                raise ScimError(409, EMAIL_TAKEN, "uniqueness") from None

    def _delete(self, identity_id: str, caller: Caller) -> None:
        """DELETE: disabled at once, and unknown to SCIM from now on (RFC 7644, 3.6); their space stays. Once a trial
        ended too (O4)."""
        with self.people.changing(caller.actor, caller.address) as within:
            self._keeping(within)  # a trial that ended still removes
            held = self._held(within.conn, identity_id)
            if held is None or held.deleted:
                raise ScimError(404, "no such user")
            if not held.disabled:
                self._disable(identity_id, within)
            self.people.link(identity_id, source="scim", within=within, deleted=True)
            self.people.leave_all(identity_id, source="scim", within=within)

    # --- groups: Latet's own, those whose source is SCIM ------------------------------------------------------------

    def _scim_group(self, group_id: str) -> Group:
        """The group ``group_id`` for a read, when it is SCIM's (by its id alone, never its name): 404 otherwise."""
        group = self.people.group(group_id) if GROUP_ID.fullmatch(group_id) else None
        if group is None or group.id != group_id or group.source != "scim":
            raise ScimError(404, "no such group")
        return group

    def _group_held(self, conn: Connection[Any], group_id: str) -> Group:
        """The group ``group_id`` as the change's transaction finds it, its row held until it ends, when it is SCIM's:
        404 otherwise (a group made by hand or for sign-ins is unknown to SCIM)."""
        row = None
        if GROUP_ID.fullmatch(group_id):
            row = conn.execute(
                "SELECT id, name, external_id, source FROM groups WHERE id = %s AND source = 'scim' FOR UPDATE",
                (group_id,),
            ).fetchone()
        if row is None:
            raise ScimError(404, "no such group")
        return Group(*row)

    def _group_resources(self, groups: list[Group], members: bool = True) -> list[dict[str, Any]]:
        """``groups`` as SCIM answers them, each with its members from SCIM that SCIM manages (M3: anyone else holding
        a membership of SCIM's is never shown to it) unless ``members`` is False."""
        shown: dict[str, list[tuple[str, str]]] = {group.id: [] for group in groups}
        if members and groups:
            with self.people.db.connection() as conn:
                rows = conn.execute(
                    "SELECT m.group_id, m.identity_id FROM members m JOIN accounts a ON a.identity_id = m.identity_id "
                    "WHERE m.group_id = ANY(%s) AND m.source = 'scim' AND a.scim_managed AND NOT a.deleted",
                    (sorted(shown),),
                ).fetchall()
            for group_id, identity_id in rows:
                if (identity := self.server.identities.get(identity_id)) is not None and identity.kind == "person":
                    shown[group_id].append((identity.name, identity.id))
        resources = []
        for group in groups:
            resource: dict[str, Any] = {
                "schemas": [GROUP],
                "id": group.id,
                "displayName": group.name,
                "meta": {"resourceType": "Group", "location": f"{self.base}/Groups/{group.id}"},
            }
            if group.external_id:
                resource["externalId"] = group.external_id
            if members:
                resource["members"] = [
                    {"value": identity_id, "display": name, "$ref": f"{self.base}/Users/{identity_id}"}
                    for name, identity_id in sorted(shown[group.id], key=lambda pair: (pair[0].casefold(), pair[1]))
                ]
            resources.append(resource)
        return resources

    def _group_answer(self, group_id: str, members: bool = True) -> dict[str, Any]:
        return self._group_resources([self._scim_group(group_id)], members)[0]

    def _groups_matching(self, text: str | None) -> list[Group]:
        """SCIM's groups that ``text`` (a filter) finds, or all of them, by name. A displayName is compared case aside
        (RFC 7643, 4.2) as the names' unique index compares them (PostgreSQL's ``lower``), never Python's ``casefold``,
        which folds more (a sharp s and "SS" are one to it, two to the index): a provider that finds no group by a name
        pushes one, which the index then takes."""
        if not text:
            return [group for group in self.people.groups() if group.source == "scim"]
        attribute, value = _filter(text, ("displayname", "externalid"), "displayName or externalId")
        with self.people.db.connection() as conn:
            if attribute == "displayname":
                rows = conn.execute(
                    "SELECT id, name, external_id, source FROM groups "
                    "WHERE source = 'scim' AND lower(name) = lower(%s)",
                    (value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, external_id, source FROM groups WHERE source = 'scim' AND external_id = %s",
                    (value,),
                ).fetchall()
        return sorted((Group(*row) for row in rows), key=lambda group: group.name.casefold())

    async def list_groups(self, request: Request, caller: Caller) -> Response:
        query = request.query_params
        found = await _in_work(self._groups_matching, query.get("filter"))
        start, page = _page(found, query)
        return _listed(await _in_work(self._group_resources, page, not _excluded(query)), len(found), start)

    async def create_group(self, request: Request, caller: Caller) -> Response:
        group_id = await _in_work(self._create_group, await _body(request), caller)
        resource = await _in_work(self._group_answer, group_id)
        location = {"location": resource["meta"]["location"]}  # RFC 7644, 3.3
        return JSONResponse(resource, status_code=201, media_type=MEDIA, headers=location)

    async def group(self, request: Request, caller: Caller) -> Response:
        """GET (and HEAD: the GET's answer without its body), PUT, PATCH and DELETE of one group of SCIM's; a read never
        reaches a change's path."""
        group_id, method = request.path_params["id"], request.method
        if method in _READS:
            found = await _in_work(self._group_answer, group_id, not _excluded(request.query_params))
            return _read(method, JSONResponse(found, media_type=MEDIA))
        if method == "DELETE":
            await _in_work(self._delete_group, group_id, caller)
            return Response(status_code=204)
        if method not in ("PUT", "PATCH"):  # the route's methods are these alone
            return _error(405, "not a method of SCIM's groups")
        body = await _body(request)
        await _in_work(self._change_group, group_id, body, method == "PUT", caller)
        if method == "PATCH":
            return Response(status_code=204)
        return JSONResponse(await _in_work(self._group_answer, group_id), media_type=MEDIA)

    def _changes_groups(self, within: Changing) -> None:
        """The license as the change's transaction finds it (F18): a ScimError 403 unless it serves sso, and 403
        ``trial-ended`` once a trial ended, when no group changes, a member taken out included (the coordinator's ruling
        on O4, which names people only); then the lock of the last administrator, before any row (``_keeping``)."""
        if (ended := self._keeping(within)) is not None:
            raise ScimError(403, str(ended), code="trial-ended")

    @staticmethod
    def _group_name(name: str) -> str:
        """People's ``group_name`` (L4): one line of 1 to 80 characters, with no control or bidirectional character, not
        shaped like a group's id; refused 400, quoting nothing of it."""
        try:
            return group_name(name)
        except ValueError:
            raise ScimError(400, GROUP_NAME_RULE, "invalidValue") from None

    @staticmethod
    def _not_mapped(conn: Connection[Any], name: str) -> None:
        """409, as a name taken (quoting nothing), when ``name`` is one the sign-in settings' mapping names, as the
        change's transaction reads them, compared as the names' unique index compares them (``lower``): sign-ins would
        join a SCIM group of that name, its roles then everyone's whom the ID token puts in the mapped group."""
        sso = settings_in(conn).sso
        targets = sorted(set(sso.groups.values())) if sso is not None else []
        if not targets:
            return
        query = "SELECT 1 FROM unnest(%s::text[]) AS t(name) WHERE lower(t.name) = lower(%s) LIMIT 1"
        if conn.execute(query, (targets, name)).fetchone() is not None:
            raise ScimError(409, GROUP_NAME_TAKEN, "uniqueness")

    @staticmethod
    def _current(conn: Connection[Any], group_id: str) -> tuple[set[str], set[str]]:
        """The group's members from SCIM as the transaction finds them (its row held): those SCIM manages, and the
        others (a person handed back, say), whom SCIM neither sees nor takes out (M3)."""
        rows = conn.execute(
            "SELECT m.identity_id, coalesce(a.scim_managed AND NOT a.deleted, false) FROM members m "
            "LEFT JOIN accounts a ON a.identity_id = m.identity_id WHERE m.group_id = %s AND m.source = 'scim'",
            (group_id,),
        ).fetchall()
        return {identity_id for identity_id, ok in rows if ok}, {identity_id for identity_id, ok in rows if not ok}

    @staticmethod
    def _managed(conn: Connection[Any], identity_ids: set[str]) -> set[str]:
        """Those of ``identity_ids`` who are people SCIM manages and has not removed, as the transaction finds them,
        their identity's row then their account's held until it ends (the order People takes them in), so that neither
        a removal nor a hand-back slips in before the membership is written."""
        if not identity_ids:
            return set()
        ordered = sorted(identity_ids)
        conn.execute(
            "SELECT id FROM identities WHERE id = ANY(%s) AND kind = 'person' ORDER BY id FOR KEY SHARE", (ordered,)
        )
        rows = conn.execute(
            "SELECT a.identity_id FROM accounts a JOIN identities i ON i.id = a.identity_id WHERE a.identity_id = "
            "ANY(%s) AND i.kind = 'person' AND a.scim_managed AND NOT a.deleted ORDER BY a.identity_id FOR SHARE OF a",
            (ordered,),
        ).fetchall()
        return {identity_id for (identity_id,) in rows}

    def _keep_members(
        self, group_id: str, wanted: _Group, within: Changing, current: tuple[set[str], set[str]]
    ) -> None:
        """The group's members from SCIM become ``wanted``'s, in the transaction of ``within``: each one added a person
        SCIM manages (M3: anyone else 400, naming no one, nothing changed), those SCIM does not manage left as they are,
        and never the server's last administrator taken out (409)."""
        managed, others = current
        added = wanted.members - managed
        if added - self._managed(within.conn, added):
            raise ScimError(400, NOT_A_MEMBER, "invalidValue")
        try:
            self.people.set_members(group_id, wanted.members | others, source="scim", within=within, keep_admin=True)
        except LastAdmin:
            raise ScimError(409, LAST_ADMIN_LEFT) from None

    def _create_group(self, body: Mapping[str, Any], caller: Caller) -> str:
        """A group made with its members and their entries, in one transaction (R6)."""
        wanted = _Group("", None)
        for key, value in body.items():
            wanted.apply(str(key), value, "replace")
        name = self._group_name(wanted.name)
        with self.people.changing(caller.actor, caller.address) as within:
            self._changes_groups(within)
            self._not_mapped(within.conn, name)
            try:
                group = self.people.add_group(name, source="scim", external_id=wanted.external_id, within=within)
            except Taken:
                raise ScimError(409, GROUP_NAME_TAKEN, "uniqueness") from None
            except ValueError:  # People's check of the externalId (L4)
                raise ScimError(400, EXTERNAL_ID_RULE, "invalidValue") from None
            self._keep_members(group.id, wanted, within, (set(), set()))
        return group.id

    def _change_group(self, group_id: str, body: Mapping[str, Any], whole: bool, caller: Caller) -> None:
        """A PUT (``whole``) or a PATCH of a group of SCIM's, with its members and their entries, in one transaction. A
        PUT that leaves out the externalId or the members keeps them, as SCIM's users keep what a PUT does not name."""
        with self.people.changing(caller.actor, caller.address) as within:
            self._changes_groups(within)
            group = self._group_held(within.conn, group_id)
            current = self._current(within.conn, group.id)
            if whole:
                wanted = _Group("", group.external_id, set(current[0]))
                for key, value in body.items():
                    wanted.apply(str(key), value, "replace")
            else:
                wanted = _Group(group.name, group.external_id, set(current[0]))
                for operation in _operations(body):
                    _operate(wanted.apply, operation)
            name = self._group_name(wanted.name)
            if name != group.name:  # a rename: never to a name the sign-in mapping names
                self._not_mapped(within.conn, name)
            if (name, wanted.external_id) != (group.name, group.external_id):
                try:
                    self.people.update_group(group.id, name, wanted.external_id, within=within)
                except Taken:
                    raise ScimError(409, GROUP_NAME_TAKEN, "uniqueness") from None
                except ValueError:  # People's check of the externalId (L4)
                    raise ScimError(400, EXTERNAL_ID_RULE, "invalidValue") from None
            self._keep_members(group.id, wanted, within, current)

    def _delete_group(self, group_id: str, caller: Caller) -> None:
        """DELETE: the group goes, with the roles given to it and its control of keys; its members stay people of the
        server. Never the server's last administrator's way to that role (409)."""
        with self.people.changing(caller.actor, caller.address) as within:
            self._changes_groups(within)
            group = self._group_held(within.conn, group_id)
            try:
                self.people.remove_group(group.id, within=within, keep_admin=True)
            except LastAdmin:
                raise ScimError(409, LAST_ADMIN_LEFT) from None

    # --- what the service provider supports (RFC 7644, 4) -----------------------------------------------------------

    async def service_provider_config(self, request: Request, caller: Caller) -> Response:
        return JSONResponse(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                "documentationUri": "https://docs.latet.ai/enterprise/sign-in/",
                "patch": {"supported": True},
                "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": MAX_RESULTS},
                "changePassword": {"supported": False},
                "sort": {"supported": False},
                "etag": {"supported": False},
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer token",
                        "primary": True,
                        "description": "A token made on Latet's Sign-in page, or with latet server sign-in scim-token",
                    }
                ],
                "meta": {"resourceType": "ServiceProviderConfig", "location": f"{self.base}/ServiceProviderConfig"},
            },
            media_type=MEDIA,
        )

    async def resource_types(self, request: Request, caller: Caller) -> Response:
        kinds = [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": name,
                "name": name,
                "endpoint": f"/{name}s",
                "schema": schema,
                "meta": {"resourceType": "ResourceType", "location": f"{self.base}/ResourceTypes/{name}"},
            }
            for name, schema in (("User", USER), ("Group", GROUP))
        ]
        return _listed(kinds, len(kinds), 1)

    async def schemas(self, request: Request, caller: Caller) -> Response:
        found = [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
                "id": schema,
                "name": name,
                "attributes": attributes,
                "meta": {"resourceType": "Schema", "location": f"{self.base}/Schemas/{schema}"},
            }
            for schema, name, attributes in ((USER, "User", _USER_ATTRIBUTES), (GROUP, "Group", _GROUP_ATTRIBUTES))
        ]
        return _listed(found, len(found), 1)
