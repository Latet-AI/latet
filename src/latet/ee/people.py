"""The people of a Latet server as Latet Enterprise sees them: their groups, their roles, the API keys they control, and
their accounts at the company's identity provider. In PostgreSQL (E3's tables), next to E0b's identities.

- A group is a set of people: made by hand ("local"), pushed by SCIM ("scim"), or named by the sign-in settings for a
  group of the ID token ("oidc"). A person may be in a group through several sources; each source changes only its own
  memberships. No sign-in joins a group SCIM pushed: whoever holds a SCIM token fills it, and a mapping onto it would
  hand its roles to everyone the ID token puts in the mapped group (``mapping_taken`` says which lines are skipped).
- A role is given to a person or to a group, by hand. A person has the user role, the roles given to them and those
  given to their groups; an API key has none.
- The control of an API key is given to people or groups.
- An account names a person at the provider: the ID token's subject, the user name, SCIM's externalId, what SCIM last
  wrote of them, and the groups of their last sign-in; whether SCIM manages them, and who disabled them last. An API
  key has none. A person removed takes along their memberships, their account, the roles given to them and their
  control of API keys (in the removal's transaction: 300_people.sql).

Names and account fields come from people, from SCIM and from ID tokens, and an administrator reads them in a terminal
and a page: they are refused with a control or bidirectional character (E0b's ``clean``: an escape sequence would act
in the terminal, an override make one name read as another), or past a length.

A first sign-in is linked only to an account that SCIM made or an administrator linked (``unclaimed``), never to a
person added by hand found by their e-mail: an attribute the provider lets its users change would hand that person, an
administrator perhaps, to whoever sets it (H1). A person added by hand is linked from their own session, to a company
account that names them as their account does (``link_own``: its user name, or the e-mail given them by hand or by
SCIM, verified), or by an administrator (``link_person``); every link is in the administration log by the fields it
set, never their values.

Every change is one transaction with its entries in the administration log (Changing): written in the caller's when
one is given (``within``), else in one of its own, its entries last in it, so that a change and what records it are
kept together or neither. What a request reads is kept in memory, each person's part apart, under the two generations
it was read at (E3's release gate 4): the server's (cache:people), for a change that may touch anyone, and the
person's own, named in Valkey by a keyed hash, for a change that touches them alone (``_touched``: their own roles,
memberships and controls). Each change, on any instance, writes a new value of what it touched once its transaction
commits, and each read gets both first, in one step that gives an absent one a fresh value: a change applies at the
next request on every instance, nobody else's copy goes, and a read begun before a change keeps nothing once the change
is known. It is announced on ee:people too, naming the people it touched or everyone, for whoever else keeps a copy and
for the streams open in API keys' spaces. A copy is trusted 30 seconds at most, should Valkey miss a change; while
Valkey does not answer nothing is kept.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal, TypeGuard

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from latet.ee.roles import GRANTED, Role
from latet.ee.server import publish
from latet.logline import Paced
from latet.server.identities import _COLUMNS as IDENTITY_COLUMNS
from latet.server.identities import EMAIL_LENGTH, EXTERNAL_ID_LENGTH, IDENTITY_ID, NAME_LENGTH, clean
from latet.server.identities import _identity as identity_of

if TYPE_CHECKING:
    from latet.server.audit import AuditLog
    from latet.server.bus import Bus
    from latet.server.db import Database
    from latet.server.identities import Changes, Identities, Identity, IdentityRef

log = logging.getLogger(__name__)

Source = Literal["local", "scim", "oidc"]
By = Literal["local", "scim"]  # who disables a person: an administrator, or SCIM
Linked = Literal["scim", "admin"]  # who named an account's user name for a first sign-in to find
GENERATION = "cache:people"
CHANNEL = "ee:people"
KEEP = 30.0
# E3's release gate 4: each person's own generation, beside the server's (GENERATION), named by a keyed hash
# (``People.generation_name``: no id in Valkey); it ends by itself a day after it was last set, by a read or a change.
PERSON_GENERATION: Final = "people-generation"
PERSON_LIFE: Final = 86_400_000  # milliseconds
TOUCHED_MOST: Final = 1_000  # people a change names one by one at most: past them it touches everyone
# The actions whose change touches the one person it names, and where it names them (M2): the entry's target, or its
# details' ``identity`` or ``subject``. Any other action, or one of these whose person is no well-formed identity id
# (a group, nothing, another shape), touches everyone. Each touches only what that person's own copy holds: their
# roles, their memberships and the keys they control. A cascade goes by a change that touches everyone (a group
# removed) or by the identities message (a person or a key removed).
NARROWED: Final[Mapping[str, str]] = {
    "person.added": "target",
    "person.linked": "target",
    "person.disabled": "target",
    "person.enabled": "target",
    "person.changed": "target",
    "person.code.made": "target",
    "role.granted": "target",
    "role.revoked": "target",
    "member.added": "identity",
    "member.removed": "identity",
    "key.controller.added": "subject",
    "key.controller.removed": "subject",
}
EVERYONE: Final = "everyone"
# Both generations in one step (L1): each that is absent (never set, ended, or evicted) is first set to a fresh random
# value, so that no copy is ever kept under "absent" and an evicted one brings no old copy back. KEYS: the server's, the
# person's; ARGV: a fresh value for each, and the person's life.
_GENERATIONS: Final = """
redis.call('set', KEYS[1], ARGV[1], 'NX')
redis.call('set', KEYS[2], ARGV[2], 'NX', 'PX', ARGV[3])
return {redis.call('get', KEYS[1]), redis.call('get', KEYS[2])}
"""
LAST_ADMIN = (
    "this is the last administrator of this server: make someone else administrator first "
    "(`latet server roles grant` always can, while the license can manage)"
)
GROUP_NAME_LENGTH = 80
USER_NAME_LENGTH = EMAIL_LENGTH  # a user name is often an e-mail address: as long as E0b keeps one
SUBJECT_LENGTH = 2048  # "<issuer> <sub>": an issuer's address and the provider's id of the person
CLAIMED_MOST, CLAIM_LENGTH = 500, 256  # the groups of a sign-in kept: how many, and how long each
SCIM_ENTRIES, SCIM_KEY_LENGTH = 20, 64  # what SCIM wrote of a person: its attributes, and their names' length
_GROUP: Final = "id, name, external_id, source"
_ACCOUNT: Final = (
    "identity_id, user_name, subject, external_id, scim, deleted, claimed, modified, scim_managed, linked_by, "
    "disabled_by, renamed"
)
# E3's release gate 3: SCIM's GET /Users paged in PostgreSQL (``People.scim_page``). The people SCIM manages, as one
# person's GET finds them (``Scim._pair``): SCIM-managed, not deleted at the provider, a person (disabled ones too).
_SCIM_MANAGED: Final = (
    "FROM accounts a JOIN identities i ON i.id = a.identity_id WHERE a.scim_managed AND NOT a.deleted "
    "AND i.kind = 'person'"
)
_SCIM_COUNT: Final = f"SELECT count(*) {_SCIM_MANAGED}"
# The page's ids, by when each person was made, ties by id: an order that is total, the same on every page and instance.
_SCIM_IDS: Final = f"SELECT i.id {_SCIM_MANAGED} ORDER BY i.created, i.id LIMIT %s OFFSET %s"
# Then the page's rows by id, the identity's columns as Community reads them (``_COLUMNS``, built by its ``_identity``)
# and the four fields of the account a listed resource shows: never ``claimed`` (500 groups) nor the rest of ``scim``.
_SCIM_ROWS: Final = (
    f"SELECT {IDENTITY_COLUMNS}, a.user_name, a.external_id, a.scim -> 'name', a.modified FROM identities i "  # noqa: S608
    "JOIN accounts a ON a.identity_id = i.id WHERE i.id = ANY(%s)"
)
_IDENTITY_WIDTH: Final = IDENTITY_COLUMNS.count(",") + 1
_SNAPSHOT: Final = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"  # as ee/events/pg.py reads one
TAKEN_NAME = "there is a group with this name or externalId already"  # quoting nothing of what was given
LINKED_ALREADY = "this person's account is linked to the provider already"
NOT_YOURS = "this company account is not yours on this server; ask an administrator to link it"
GROUP_ID = re.compile(r"g_[a-z2-7]{10}")  # a group's id, as new_group_id makes it: never a group's name
# The targets of a change that every instance hears on ee:people besides a person and a group (M4): the sign-in's
# settings (``company_changes`` decides whose session opens an API key's space).
TOLD = frozenset({"sso"})
_FIELDS = frozenset({"user_name", "subject", "external_id", "scim", "deleted", "claimed", "scim_managed"})
_LINKED: Final = ("subject", "user_name", "external_id", "scim_managed")  # what names a person at the provider
_ROLES = {role.value: role for role in Role}
Note = tuple[str, str | None, dict[str, Any] | None]  # an entry of the administration log: action, target, details


@dataclass(frozen=True)
class Group:
    id: str  # "g_" + 10 lowercase base32 characters
    name: str
    external_id: str | None  # SCIM's externalId
    source: Source


@dataclass(frozen=True)
class Account:
    """A person's account at the provider. Printed, it names them by their id alone."""

    identity_id: str
    user_name: str | None = field(repr=False)
    subject: str | None = field(repr=False)  # "<issuer> <sub>" of their ID tokens
    external_id: str | None = field(repr=False)  # SCIM's externalId
    # What SCIM last wrote of them (name parts, displayName); None: never provisioned.
    scim: dict[str, Any] | None = field(repr=False)
    deleted: bool  # deleted at the provider: disabled here, and unknown to SCIM
    claimed: tuple[str, ...] = field(repr=False)  # the groups of their last sign-in
    modified: float
    scim_managed: bool = False  # SCIM made the account or was handed the person: SCIM reaches no one else (M3)
    linked_by: Linked | None = None  # who named its user name for a first sign-in: only then is it claimable (H1)
    disabled_by: By | None = None  # who disabled them last: SCIM lifts only a disable of its own (L7)
    # Its user name was changed other than by an administrator's link (SCIM's rename), and no link or sign-in confirmed
    # it since: no first sign-in claims it and `link --all` links it by no user name (``_claimable``, ``_renamed``).
    renamed: bool = False


@dataclass(frozen=True)
class Directory:
    """Everything E3 keeps, read at once: the People and groups page."""

    groups: list[Group]
    members: dict[str, list[tuple[str, Source]]]  # group id → (identity id, source)
    roles: dict[str, frozenset[Role]]  # a person's or a group's id → the roles given to it
    controllers: dict[str, list[str]]  # an API key's id → the people and groups that control it
    accounts: dict[str, Account]


@dataclass(eq=False)
class Changing:
    """One transaction of changes to E3's tables and to E0b's identities: ``changes`` (E0b's
    ``Identities.changing()``: its connection, and what it tells the instances once it commits), who makes them
    (``actor``, ``address``: what the administration log names), the entries they leave (``notes``), which whoever
    opened the transaction writes last in it, and what is done once it commits (``after``: the settings' message,
    the policy's). Printed, it names nothing."""

    changes: Changes = field(repr=False)
    actor: str = "server"
    address: str | None = field(default=None, repr=False)
    notes: list[Note] = field(default_factory=list, repr=False)
    after: list[Callable[[], object]] = field(default_factory=list, repr=False)
    # Whether the change touches people, groups or the sign-in (False: E5's decisions, which name a person they do not
    # change: R38).
    people: bool = True

    @property
    def conn(self) -> psycopg.Connection[Any]:
        return self.changes.conn

    def note(self, action: str, target: str | None, details: dict[str, Any] | None = None) -> None:
        self.notes.append((action, target, details))

    def write(self, audit: AuditLog) -> None:
        """Every entry, in the transaction: last in it, so that the changes and their entries are kept together or
        neither (an entry that cannot be written raises, and nothing is kept)."""
        for action, target, details in self.notes:
            audit.record(self.actor, action, target, details, self.address, conn=self.conn)


@dataclass(frozen=True)
class _Seen:
    """What a person's requests read, kept until the next change: ``generation``, the two it was read under (the
    server's and the person's own)."""

    roles: frozenset[Role]
    groups: tuple[Group, ...]
    keys: tuple[IdentityRef, ...]
    read: float
    generation: tuple[bytes, bytes] | None = None


class Taken(ValueError):
    """Another group has this name or externalId, or another account this user name, subject or externalId; or the
    person's account is linked to the provider already."""


class LastAdmin(ValueError):
    """The change would leave the server with no enabled administrator."""


class NotYours(Exception):
    """A person's own link came back with a company account that does not name them as their account here does
    (``People.link_own``): nothing was linked. Its words name nothing of either."""

    def __init__(self) -> None:
        super().__init__(NOT_YOURS)


def new_group_id() -> str:
    return "g_" + base64.b32encode(secrets.token_bytes(10)).decode().lower()[:10]


def group_key(name: str) -> str:
    """A group as Latet compares it: case aside, without the leading ``/`` of Keycloak's full paths."""
    return name.strip().lstrip("/").casefold()


def group_name(name: str) -> str:
    """A group's name as it is kept: one line of 1 to 80 characters, with no control or bidirectional character (E0b's
    ``clean`` would take one out: refused). Its twin in the sign-in settings' mapping is ``SsoSettings._groups``."""
    name = name.strip()
    if not 0 < len(name) <= GROUP_NAME_LENGTH or clean(name, GROUP_NAME_LENGTH) != name:
        raise ValueError(
            f"a group's name is one line of 1 to {GROUP_NAME_LENGTH} characters, with no control or bidirectional "
            "character"
        )
    if GROUP_ID.fullmatch(name.lower()):  # `group(key)` finds a group by id or name: one never stands for another
        raise ValueError("a group's name is not shaped like a group's id")
    return name


def _kept(value: Any, limit: int, what: str, *, empty: bool = False) -> str:
    """``value`` as it is kept, refused (ValueError, naming nothing of it) unless it is text of at most ``limit``
    characters (and at least one, unless ``empty``) that E0b's ``clean`` leaves as it is: no control or bidirectional
    character, no space around it."""
    if not isinstance(value, str) or len(value) > limit or not (empty or value) or clean(value, limit) != value:
        raise ValueError(
            f"{what} is one line of at most {limit} characters, with no control or bidirectional character"
        )
    return value


def _profile(value: Any, *, nested: bool = False) -> dict[str, Any]:
    """What SCIM wrote of a person (name parts, displayName), checked as a name is: at most ``SCIM_ENTRIES`` attributes,
    each text, nothing, or (once) attributes of its own."""
    if not isinstance(value, Mapping) or len(value) > SCIM_ENTRIES:
        raise ValueError(f"what SCIM writes of a person is {SCIM_ENTRIES} attributes at most")
    kept: dict[str, Any] = {}
    for key, item in value.items():
        name = _kept(key, SCIM_KEY_LENGTH, "a SCIM attribute's name")
        if item is None or isinstance(item, str):
            kept[name] = item if item is None else _kept(item, NAME_LENGTH, "a SCIM attribute", empty=True)
        elif isinstance(item, Mapping) and not nested:
            kept[name] = _profile(item, nested=True)
        else:
            raise ValueError("a SCIM attribute of a person is text, or the parts of their name")
    return kept


def _claimed(value: Any) -> list[str]:
    if isinstance(value, str | bytes) or not isinstance(value, Collection) or len(value) > CLAIMED_MOST:
        raise ValueError(f"the groups of a sign-in are a list of {CLAIMED_MOST} at most")
    return [_kept(item, CLAIM_LENGTH, "a group of a sign-in") for item in value]


def _account_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """``fields`` of an account checked, as they are written (None unsets one)."""
    if unknown := set(fields) - _FIELDS:
        raise TypeError(f"not a field of an account: {', '.join(sorted(unknown))}")
    limits = {"user_name": (USER_NAME_LENGTH, "a user name"), "subject": (SUBJECT_LENGTH, "a subject")}
    limits["external_id"] = (EXTERNAL_ID_LENGTH, "an external id")
    values: dict[str, Any] = {}
    for key, value in fields.items():
        if key in limits:
            values[key] = None if value is None else _kept(value, *limits[key])
        elif key == "scim":
            values[key] = None if value is None else Jsonb(_profile(value))
        elif key == "claimed":
            values[key] = Jsonb(_claimed(value))
        elif not isinstance(value, bool):  # deleted, scim_managed
            raise ValueError(f"an account's {key} is true or false")
        else:
            values[key] = value
    return values


def _new_name(before: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """Whether ``values`` change the user name ``before`` has into another one, A to Z's case aside as a first sign-in
    compares them (``user_name_key``): a change of capitals alone keeps the same user name, and a first one given to an
    account that had none changes none."""
    if "user_name" not in values or before["user_name"] is None:
        return False
    named = values["user_name"]
    return named is None or user_name_key(named) != user_name_key(before["user_name"])


def _claimable(before: Mapping[str, Any], values: Mapping[str, Any], linked: Linked | None) -> Linked | None:
    """Who an account is claimable through once ``values`` are written over ``before`` (H1, M3): nobody once it names
    a subject; ``linked`` when ``link_person`` links them (an administrator's link, made on purpose); SCIM when it
    provisions them (``scim_managed`` set on an account it did not manage); otherwise what it was, except that SCIM's
    claim goes with SCIM's management, and either claim with a user name changed outside ``link_person``
    (``_new_name``). SCIM's later writes keep it as it is: an account someone signed in to and an administrator
    unlinked (``unlink``) is claimed by no first sign-in until it is linked again on purpose, whatever SCIM writes of it
    since; and so is one SCIM renamed before anyone signed in to it, the rename back included (the final security
    review's wider option: a leaked SCIM token would otherwise point the first sign-in of another provider account at
    it, rename it back, and leave the company to fill an account that someone else holds)."""
    if values.get("subject", before["subject"]) is not None:
        return None
    if linked is not None:
        return linked
    if values.get("scim_managed") is True and not before["scim_managed"]:
        return "scim"
    kept: Linked | None = before["linked_by"]
    if kept == "scim" and not values.get("scim_managed", before["scim_managed"]):
        return None
    if kept is not None and _new_name(before, values):
        return None
    return kept


def _renamed(before: Mapping[str, Any], values: Mapping[str, Any], linked: Linked | None) -> bool:
    """Whether an account is ``renamed`` once ``values`` are written over ``before``: its user name changed
    (``_new_name``) other than by ``link_person`` (SCIM's rename), and no link or sign-in confirmed it since. Whether or
    not the account names a subject (the claim fix's review, L3): someone who signed in goes on signing in by it, and
    the user name SCIM wrote stays on their account, which `latet server people link --all` would link again after an
    issuer change. It lists such an account apart instead. An administrator's link or a subject written ends it, and
    so does a sign-in that brings that user name (``People.confirmed``). A claim drops only while the account names no
    subject (``_claimable``)."""
    if linked is not None or values.get("subject") is not None:
        return False
    return _new_name(before, values) or bool(before["renamed"])


_ASCII_LOWER: Final = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}


def user_name_key(user_name: str) -> str:
    """A user name as Latet compares it with another: A to Z folded to a to z, every other character as it is, as
    PostgreSQL's ``lower(user_name COLLATE "C")`` folds it (the unique index ``accounts_user_name``, and every query
    that finds an account by its user name). Never Unicode's case folding (``casefold``, ``lower`` of a non-ASCII text,
    or ``lower`` under the database's locale), which folds look-alikes onto ASCII letters (the Kelvin sign onto ``k``, a
    capital I with a dot onto ``i``): a provider's user named with one would claim, or be found as, another person's
    account (the final review's F3), as ``_same_ascii`` keeps an e-mail from doing."""
    return user_name.translate(_ASCII_LOWER)


def _claims(before: Mapping[str, Any], user_name: str) -> bool:
    """Whether a first sign-in as ``user_name`` may claim the account ``before`` (held FOR UPDATE): no subject yet, not
    deleted at the provider, claimable (``linked_by``), and that user name, A to Z's case aside (``unclaimed``'s
    terms)."""
    named = before["user_name"]
    return (
        before["subject"] is None
        and not before["deleted"]
        and before["linked_by"] is not None
        and isinstance(named, str)
        and user_name_key(named) == user_name_key(user_name)
    )


def _told(target: object) -> TypeGuard[str]:
    """Whether a change's entry names what every instance hears of (M4): a person or a key, a group, ``TOLD``."""
    return isinstance(target, str) and (target.startswith("u_") or bool(GROUP_ID.fullmatch(target)) or target in TOLD)


def _named(action: object, target: str, details: object) -> str | None:
    """The one person an entry's change touches (``NARROWED``), when it names them as a well-formed identity id;
    None: everyone."""
    place = NARROWED.get(action) if isinstance(action, str) else None
    if place is None:
        return None
    if place == "target":
        person: object = target
    elif isinstance(details, Mapping):
        person = details.get(place)
    else:
        return None
    return person if isinstance(person, str) and IDENTITY_ID.fullmatch(person) else None


def _touched(changing: Changing) -> frozenset[str] | str | None:
    """Whom a committed change touches (gate 4, M2), in three steps; never raises:
    1. nobody (None) when it says so (``Changing.people`` False, R38), or when no entry names a person or a key, a
       group or ``TOLD`` (M4);
    2. the people the listed actions name (``NARROWED``), each a well-formed identity id;
    3. everyone (``EVERYONE``) for any other entry kept: a group, a key's own change, the sign-in's settings, an action
       not listed, now or later; and past ``TOUCHED_MOST`` people."""
    try:
        if not changing.people:
            return None
        kept = [(action, target, details) for action, target, details in changing.notes if _told(target)]
        if not kept:
            return None
        people: set[str] = set()
        for action, target, details in kept:
            person = _named(action, target, details)
            if person is None:
                return EVERYONE
            people.add(person)
        return EVERYONE if len(people) > TOUCHED_MOST else frozenset(people)
    except Exception:  # whatever an entry holds: everyone, never an error after the commit
        return EVERYONE


def _account(row: tuple[Any, ...]) -> Account:
    identity_id, user_name, subject, external_id, scim, deleted, claimed, modified, managed, linked, by, renamed = row
    return Account(
        identity_id,
        user_name,
        subject,
        external_id,
        scim,
        bool(deleted),
        tuple(claimed or ()),
        modified,
        bool(managed),
        linked,
        by,
        bool(renamed),
    )


def _same_ascii(given: str | None, other: str | None) -> bool:
    """Whether two e-mails are one, for a person's own link (``People.link_own``): both ASCII, and equal with ASCII's
    case aside. Never PostgreSQL's ``lower``, which follows the database's locale and folds look-alikes onto ASCII
    letters (the Kelvin sign onto ``k``, a capital I with a dot onto ``i``): a provider would verify such an address
    for someone else. A non-ASCII e-mail on either side matches nothing: an administrator links that person."""
    if given is None or other is None or not given.isascii() or not other.isascii():
        return False
    return given.lower() == other.lower()  # on ASCII alone, lower folds A-Z and nothing else


def _mapped(claimed: Iterable[str], mapping: Mapping[str, str]) -> list[str]:
    """The Latet groups ``mapping`` (a group of the ID token → a Latet group) names for these groups of a sign-in."""
    by_key = {group_key(value): group for value, group in mapping.items()}
    return sorted({by_key[group_key(value)] for value in claimed if group_key(value) in by_key})


class People:
    def __init__(
        self,
        identities: Identities,
        db: Database,
        bus: Bus,
        audit: AuditLog,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.identities = identities
        self.db = db
        self.bus = bus
        self.audit = audit
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, _Seen] = {}
        self._server: bytes | None = None  # the server's generation last read: when it moves, every copy goes
        self._moved = 0  # moved by every drop, forget and change of this instance's: a read begun before keeps nothing
        self._step: Any = None  # _GENERATIONS, registered with Valkey's client at the first read
        self._closing = False  # the edition closes: reads go to PostgreSQL alone (``close``)
        self._unheard = Paced()  # Valkey not answering: said once a minute, not at each request
        self._scims = Paced()  # sign-ins whose mapping names a group SCIM pushed: said once a minute

    # --- one transaction of changes ---------------------------------------------------------------------------

    @contextlib.contextmanager
    def changing(self, actor: str = "server", address: str | None = None) -> Iterator[Changing]:
        """A transaction for several changes (a SCIM request, a sign-in): each given it (``within``) writes there; its
        entries are written last in it, and once it commits every instance is told (``committed``). Nothing is kept,
        nor told, when the block raises."""
        with self.identities.changing() as changes:
            changing = Changing(changes, actor, address)
            yield changing
            changing.write(self.audit)
        self.committed(changing)

    @contextlib.contextmanager
    def _within(self, within: Changing | None, actor: str, address: str | None) -> Iterator[Changing]:
        """``within``, the caller's transaction (its entries are the caller's to write); else one of the change's
        own."""
        if within is not None:
            yield within
            return
        with self.changing(actor, address) as own:
            yield own

    def committed(self, changing: Changing) -> None:
        """Once the transaction of ``changing`` has committed: the copies of whom it touched go, on this instance at
        once and on every other at its next read, the change is told on ee:people, and what was to follow it is done.
        Only a change that names a person, a group or the sign-in's settings (``TOLD``: whose sessions may open an API
        key's space, which every stream open there asks again at once) does the first two (M4): a change of a later
        part's settings, an export or a test names none of them, and every instance would read everyone's roles again
        for nothing. Nor does one that says it touches nobody (``Changing.people`` False: E5's decisions, whose entries
        name the asking person, R38). Whom it touched: ``_touched`` (gate 4)."""
        subjects = [target for _action, target, _details in changing.notes if _told(target)]
        touched = _touched(changing)
        if touched is not None:
            self._changed(subjects, touched)
        for then in changing.after:
            then()

    def generation_name(self, identity_id: str) -> str:
        """The name in Valkey of the person's own generation (gate 4): a keyed hash of their id, as the limits' names
        are (``Bus.rate_key``), so that whoever reads Valkey learns from it no one whose roles changed."""
        return self.bus.rate_key(f"{PERSON_GENERATION}:{identity_id}")

    def close(self) -> None:
        """This instance stops (its edition closes, first): from now on a read of someone's roles, groups and keys goes
        to PostgreSQL alone, is not kept, and reads or writes nothing in Valkey, so that what still reads them while the
        instance stops (E4's events written as it closes, a request under way) gives no person a generation (fix round
        2: a closing instance creates no key). A change still writes its generations and tells the other instances:
        they must hear it. Closing again does nothing more."""
        with self._lock:
            self._closing = True
            self._seen.clear()
            self._moved += 1

    def drop(self) -> None:
        """Forget this instance's copy, all of it (a message on ee:people that says everyone, or an older instance's,
        or one malformed; an identity's message that names none); a read begun before keeps nothing of what it read."""
        with self._lock:
            self._seen.clear()
            self._moved += 1

    def forget(self, identity_id: str) -> None:
        """Forget this instance's copy of ``identity_id``, and every copy that holds it among its keys (a key renamed,
        disabled or removed is in its controllers' copies): a person another instance changed (ee:people), an identity
        changed (the identities message). A read begun before keeps nothing of what it read."""
        with self._lock:
            self._moved += 1
            self._forget(identity_id)

    def _forget(self, identity_id: str) -> None:
        """Under the lock."""
        self._seen.pop(identity_id, None)
        holding = [other for other, seen in self._seen.items() if any(ref.id == identity_id for ref in seen.keys)]
        for other in holding:
            del self._seen[other]

    def heard(self, message: object) -> None:
        """A message on ee:people (a change on any instance, this one's too): the people it names are forgotten, when
        it names them one by one, each a well-formed identity id; anything else drops every copy (M1): a message that
        says everyone, an older instance's (``changed`` alone), one malformed."""
        people = message.get("people") if isinstance(message, Mapping) else None
        named = (
            isinstance(message, Mapping)
            and message.get("everyone") is not True
            and isinstance(people, list)
            and len(people) <= TOUCHED_MOST
            and all(isinstance(person, str) and IDENTITY_ID.fullmatch(person) for person in people)
        )
        if not named:
            self.drop()
            return
        with self._lock:
            self._moved += 1
            for person in people or ():
                self._forget(person)

    # --- what a request reads, kept until a change on any instance ---------------------------------------------

    def _said(self, line: str, exc: BaseException) -> None:
        if count := self._unheard.due():  # by its type: its words can name Valkey's address
            log.warning(
                "%s (%s)%s", line, type(exc).__name__, f" ({count} times since the line before)" if count > 1 else ""
            )

    def _generations(self, identity_id: str) -> tuple[bytes, bytes] | None:
        """The server's generation and the person's own as Valkey has them, the ones a read now is made under: one
        step that first gives an absent one a fresh value (``_GENERATIONS``, L1); this instance's copies go when the
        server's moved. None when they cannot be trusted, and nothing is kept: Valkey not answering (every copy kept
        before goes too), or refusing the step (full under ``noeviction``, which refuses any script that may write)
        while a plain read of both misses one (N3). Said once a minute, by the error's type."""
        bus = self.bus
        names = [bus.key(GENERATION), bus.key(self.generation_name(identity_id))]
        try:
            if self._step is None:
                self._step = bus.client.register_script(_GENERATIONS)
            found = self._step(keys=names, args=[secrets.token_bytes(8), secrets.token_bytes(8), PERSON_LIFE])
        except Exception as refused:
            try:
                found = bus.client.mget(names)
            except Exception as exc:
                self._said("Valkey did not answer: the people's roles are read from the database at each request", exc)
                # Every copy goes, and a read in flight keeps nothing (fix round 1, L1): a change made meanwhile may
                # have reached Valkey neither as its generation nor as its message. The first read Valkey answers again
                # finds no server's generation known, and keeps afresh.
                with self._lock:
                    self._seen.clear()
                    self._server = None
                    self._moved += 1
                return None
            self._said(
                "Valkey refused the step that reads the people's generations: they are read without it, and a person "
                "whose generation is missing is read from the database at each request",
                refused,
            )
        try:
            server, own = found
        except (TypeError, ValueError):  # not the two it answers: nothing trusted
            return None
        if not isinstance(server, bytes) or not isinstance(own, bytes):
            return None
        with self._lock:
            if server != self._server:
                self._seen.clear()
                self._server = server
        return server, own

    def _of(self, identity_id: str) -> _Seen:
        with self._lock:
            moved, closing = self._moved, self._closing
        if closing:  # the instance stops: PostgreSQL alone, nothing kept, nothing in Valkey (``close``)
            return self._read(identity_id, self.clock())
        generations = self._generations(identity_id)  # before anything is read: what is read is kept under them alone
        now = self.clock()
        with self._lock:
            seen = self._seen.get(identity_id)
        fresh = seen is not None and generations is not None and seen.generation == generations
        if fresh and seen is not None and 0 <= now - seen.read < KEEP:
            return seen
        seen = replace(self._read(identity_id, now), generation=generations)
        with self._lock:
            # A drop, a forget or a change of this instance's meanwhile: what was read answers, and is not kept.
            if generations is not None and self._moved == moved:
                self._seen[identity_id] = seen
        return seen

    def _read(self, identity_id: str, now: float) -> _Seen:
        identity = self.identities.get(identity_id)
        if identity is None or identity.kind != "person":  # an API key, or nobody: no role, group or key
            return _Seen(frozenset(), (), (), now)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT g.id, g.name, g.external_id, g.source FROM members m JOIN groups g "
                "ON g.id = m.group_id WHERE m.identity_id = %s",
                (identity_id,),
            ).fetchall()
            groups = sorted((Group(*row) for row in rows), key=lambda group: group.name.casefold())
            subjects = [identity_id, *(group.id for group in groups)]
            given = conn.execute("SELECT DISTINCT role FROM roles WHERE subject = ANY(%s)", (subjects,)).fetchall()
            key_ids = conn.execute(
                "SELECT DISTINCT key_id FROM key_controllers WHERE subject = ANY(%s)", (subjects,)
            ).fetchall()
        roles = {_ROLES[role] for (role,) in given if role in _ROLES}
        keys = [key.ref() for (key_id,) in key_ids if (key := self.identities.get(key_id)) and key.kind == "key"]
        keys.sort(key=lambda ref: ref.name.casefold())
        return _Seen(frozenset({Role.USER, *roles}), tuple(groups), tuple(keys), now)

    def _changed(self, subjects: list[str], touched: frozenset[str] | str) -> None:
        """After a change is committed (gate 4): a new value of each generation it moved in Valkey (the server's for
        ``EVERYONE``, else each person's own), then this instance's copies of them go, then it is told on ee:people,
        always: by the people it touched or everyone, beside the subjects of its entries (``changed``, as before), which
        older instances drop everything at, and which wakes the streams open in API keys' spaces. A write Valkey does
        not take: this instance trusts nothing it kept, and the message says everyone. Never raises: the change is kept
        already."""
        everyone = touched == EVERYONE or isinstance(touched, str)
        people = sorted(touched) if not everyone else []
        try:
            if everyone:
                self.bus.set(GENERATION, secrets.token_bytes(8))
            else:
                pipe = self.bus.client.pipeline()
                for person in people:
                    pipe.set(self.bus.key(self.generation_name(person)), secrets.token_bytes(8), px=PERSON_LIFE)
                pipe.execute()
        except Exception as exc:  # by its type: its words can name Valkey's address
            log.warning(
                "Valkey did not take a change of people (%s): the other instances read it within %.0f s",
                type(exc).__name__,
                KEEP,
            )
            everyone = True
        with self._lock:
            self._moved += 1
            if everyone:
                self._seen.clear()
            else:
                for person in people:
                    self._forget(person)
        changed = sorted(set(subjects))
        if everyone:  # E1's publish: a failure is logged by its type
            publish(self.bus, CHANNEL, {"changed": changed, "everyone": True})
        else:
            publish(self.bus, CHANNEL, {"changed": changed, "people": people})

    def roles_of(self, identity_id: str) -> frozenset[Role]:
        """The user role and every role given to the person or to one of their groups; none for an API key."""
        return self._of(identity_id).roles

    def groups_of(self, identity_id: str) -> list[Group]:
        """Their groups by name, each once, whatever the source (E4's events carry the first)."""
        return list(self._of(identity_id).groups)

    def controlled_by(self, identity_id: str) -> list[IdentityRef]:
        """The API keys they control, themselves or through one of their groups."""
        return list(self._of(identity_id).keys)

    def roles_in(self, conn: psycopg.Connection[Any], identity_id: str) -> frozenset[Role] | None:
        """The roles of the person ``identity_id`` as ``conn``'s transaction finds them, past every copy: what a change
        made in their name weighs there (ee/routes.py), so that a role taken away, or the person disabled, on any
        instance before that transaction read them is seen. None for no one, an API key or a person disabled."""
        row = conn.execute("SELECT kind, disabled FROM identities WHERE id = %s", (identity_id,)).fetchone()
        if row is None or row[0] != "person" or row[1]:
            return None
        rows = conn.execute(
            "SELECT role FROM roles WHERE subject = %s UNION "
            "SELECT r.role FROM members m JOIN roles r ON r.subject = m.group_id WHERE m.identity_id = %s",
            (identity_id, identity_id),
        ).fetchall()
        return frozenset({Role.USER, *(_ROLES[role] for (role,) in rows if role in _ROLES)})

    # --- groups and their members -----------------------------------------------------------------------------

    def groups(self) -> list[Group]:
        with self.db.connection() as conn:
            rows = conn.execute(f"SELECT {_GROUP} FROM groups").fetchall()  # noqa: S608
        return sorted((Group(*row) for row in rows), key=lambda group: group.name.casefold())

    def group(self, key: str) -> Group | None:
        """A group by its id, or by its name (case aside): a key shaped like an id is an id alone (no name has that
        shape: ``group_name``)."""
        with self.db.connection() as conn:
            if GROUP_ID.fullmatch(key):
                row = conn.execute(f"SELECT {_GROUP} FROM groups WHERE id = %s", (key,)).fetchone()  # noqa: S608
            else:
                row = conn.execute(
                    f"SELECT {_GROUP} FROM groups WHERE lower(name) = lower(%s)",  # noqa: S608
                    (key.strip(),),
                ).fetchone()
        return Group(*row) if row is not None else None

    def members(self, group_id: str, source: Source | None = None) -> list[IdentityRef]:
        """The people in the group, through any source or through ``source``, by name."""
        with self.db.connection() as conn:
            if source is None:
                rows = conn.execute("SELECT DISTINCT identity_id FROM members WHERE group_id = %s", (group_id,))
            else:
                rows = conn.execute(
                    "SELECT identity_id FROM members WHERE group_id = %s AND source = %s", (group_id, source)
                )
            ids = [identity_id for (identity_id,) in rows]
        refs = [identity.ref() for identity_id in ids if (identity := self.identities.get(identity_id)) is not None]
        return sorted(refs, key=lambda ref: ref.name.casefold())

    def held_members(self, conn: psycopg.Connection[Any], group_id: str) -> list[str]:
        """The ids of the people in the group, through any source, as ``conn``'s transaction finds them, the group's
        row held until it ends (someone joining meanwhile waits for it, and one who joined before is read): those a
        removal of the group, or of a role given to it, takes it from. Empty for no such group. A change that keeps the
        last administrator takes their lock before it (``hold_admins``)."""
        if conn.execute("SELECT 1 FROM groups WHERE id = %s FOR UPDATE", (group_id,)).fetchone() is None:
            return []
        rows = conn.execute(
            "SELECT DISTINCT identity_id FROM members WHERE group_id = %s ORDER BY identity_id", (group_id,)
        ).fetchall()
        return [identity_id for (identity_id,) in rows]

    def add_group(
        self,
        name: str,
        *,
        source: Source = "local",
        external_id: str | None = None,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> Group:
        if external_id is not None:
            _kept(external_id, EXTERNAL_ID_LENGTH, "an external id")
        group = Group(new_group_id(), group_name(name), external_id, source)
        now = self.clock()
        with self._within(within, actor, address) as changing:
            try:
                with changing.conn.transaction():  # a savepoint: a name taken leaves the caller's transaction usable
                    changing.conn.execute(
                        "INSERT INTO groups (id, name, external_id, source, created, modified) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (group.id, group.name, group.external_id, group.source, now, now),
                    )
            except psycopg.errors.UniqueViolation:
                raise Taken(TAKEN_NAME) from None
            changing.note("group.added", group.id, {"source": source})
        return group

    def update_group(
        self,
        group_id: str,
        name: str,
        external_id: str | None,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> Group:
        """The group's name and SCIM's externalId, as given."""
        name = group_name(name)
        if external_id is not None:
            _kept(external_id, EXTERNAL_ID_LENGTH, "an external id")
        query: Final = f"UPDATE groups SET name = %s, external_id = %s, modified = %s WHERE id = %s RETURNING {_GROUP}"  # noqa: S608
        with self._within(within, actor, address) as changing:
            try:
                with changing.conn.transaction():
                    row = changing.conn.execute(query, (name, external_id, self.clock(), group_id)).fetchone()
            except psycopg.errors.UniqueViolation:
                raise Taken(TAKEN_NAME) from None
            if row is None:
                raise KeyError(group_id)
            changing.note("group.changed", group_id)
        return Group(*row)

    def remove_group(
        self,
        group_id: str,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        keep_admin: bool = False,
    ) -> bool:
        """The group goes, with its memberships, the roles given to it and its control of API keys; its members stay
        people of the server."""
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            before = self._admins(conn, hold=True) if keep_admin else set()
            conn.execute("DELETE FROM roles WHERE subject = %s", (group_id,))
            conn.execute("DELETE FROM key_controllers WHERE subject = %s", (group_id,))
            gone = conn.execute("DELETE FROM groups WHERE id = %s", (group_id,)).rowcount  # its members go with it
            if keep_admin and before and not self._admins(conn):
                raise LastAdmin(LAST_ADMIN)
            if gone:
                changing.note("group.removed", group_id)
        return gone > 0

    def add_member(
        self,
        group_id: str,
        identity_id: str,
        *,
        source: Source = "local",
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> bool:
        with self._within(within, actor, address) as changing:
            self._members(changing.conn, group_id, [identity_id])
            added = changing.conn.execute(
                "INSERT INTO members (group_id, identity_id, source) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (group_id, identity_id, source),
            ).rowcount
            if added:
                changing.note("member.added", group_id, {"identity": identity_id, "source": source})
        return added > 0

    def remove_member(
        self,
        group_id: str,
        identity_id: str,
        *,
        source: Source = "local",
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        keep_admin: bool = False,
    ) -> bool:
        """Takes away the membership of ``source`` only: one from the provider stays until it changes there."""
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            before = self._admins(conn, hold=True) if keep_admin else set()
            gone = conn.execute(
                "DELETE FROM members WHERE group_id = %s AND identity_id = %s AND source = %s",
                (group_id, identity_id, source),
            ).rowcount
            if keep_admin and before and not self._admins(conn):
                raise LastAdmin(LAST_ADMIN)
            if gone:
                changing.note("member.removed", group_id, {"identity": identity_id, "source": source})
        return gone > 0

    def set_members(
        self,
        group_id: str,
        identity_ids: Collection[str],
        *,
        source: Source,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        keep_admin: bool = False,
    ) -> None:
        """The group's members from ``source`` become ``identity_ids`` (SCIM's PUT and PATCH): only what changes is
        written and recorded. ``keep_admin``: refused (LastAdmin) when it would leave no enabled administrator, under
        the lock the other keeping changes take."""
        wanted = set(identity_ids)
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            admins = self._admins(conn, hold=True) if keep_admin else set()
            rows = conn.execute(
                "SELECT identity_id FROM members WHERE group_id = %s AND source = %s", (group_id, source)
            )
            before = {identity_id for (identity_id,) in rows}
            self._members(conn, group_id, sorted(wanted - before))  # before anything is written
            for identity_id in sorted(before - wanted):
                conn.execute(
                    "DELETE FROM members WHERE group_id = %s AND identity_id = %s AND source = %s",
                    (group_id, identity_id, source),
                )
                changing.note("member.removed", group_id, {"identity": identity_id, "source": source})
            for identity_id in sorted(wanted - before):
                if conn.execute(
                    "INSERT INTO members (group_id, identity_id, source) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (group_id, identity_id, source),
                ).rowcount:  # one added meanwhile by another transaction is not added, nor recorded, twice
                    changing.note("member.added", group_id, {"identity": identity_id, "source": source})
            if keep_admin and admins and not self._admins(conn):
                raise LastAdmin(LAST_ADMIN)

    def _members(self, conn: psycopg.Connection[Any], group_id: str, identity_ids: Iterable[str]) -> None:
        """A membership is a person's, in a group of this server, as ``conn``'s transaction finds them (their rows held
        until it ends, so that neither is removed meanwhile): KeyError for no such group or identity, ValueError for an
        API key."""
        found = conn.execute("SELECT 1 FROM groups WHERE id = %s FOR KEY SHARE", (group_id,)).fetchone()
        if found is None:
            raise KeyError(group_id)
        for identity_id in identity_ids:
            self._person(conn, identity_id, "an API key is in no group: name a person")

    def leave_all(
        self,
        identity_id: str,
        *,
        source: Source,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> None:
        """Every membership of the person from ``source`` goes (SCIM deleted them)."""
        with self._within(within, actor, address) as changing:
            rows = changing.conn.execute(
                "DELETE FROM members WHERE identity_id = %s AND source = %s RETURNING group_id", (identity_id, source)
            ).fetchall()
            for (group_id,) in rows:
                changing.note("member.removed", group_id, {"identity": identity_id, "source": source})

    # --- roles ------------------------------------------------------------------------------------------------

    def _subject(self, conn: psycopg.Connection[Any], subject: str) -> None:
        """A subject is a group of this server, or a person, as ``conn``'s transaction finds them, its row held until
        that transaction ends (a removal meanwhile waits, and leaves no row naming it): KeyError for neither,
        ValueError for an API key."""
        if subject.startswith("g_"):
            if conn.execute("SELECT 1 FROM groups WHERE id = %s FOR KEY SHARE", (subject,)).fetchone() is None:
                raise KeyError(subject)
            return
        self._person(conn, subject, "an API key takes no role and controls no key: name a person or a group")

    def given(self, subject: str, *, conn: psycopg.Connection[Any] | None = None) -> frozenset[Role]:
        """The roles given to this person or group itself (not through groups); given ``conn``, as its transaction
        finds them."""
        query: Final = "SELECT role FROM roles WHERE subject = %s"
        if conn is not None:
            rows = conn.execute(query, (subject,)).fetchall()
        else:
            with self.db.connection() as own:
                rows = own.execute(query, (subject,)).fetchall()
        return frozenset(_ROLES[role] for (role,) in rows if role in _ROLES)

    def grant(
        self,
        subject: str,
        role: Role,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> bool:
        if role not in GRANTED:
            raise ValueError("everyone has the user role: give group-lead, security, auditor or admin")
        with self._within(within, actor, address) as changing:
            self._subject(changing.conn, subject)
            added = changing.conn.execute(
                "INSERT INTO roles (subject, role) VALUES (%s, %s) ON CONFLICT DO NOTHING", (subject, role.value)
            ).rowcount
            if added:
                changing.note("role.granted", subject, {"role": role.value})
        return added > 0

    def revoke(
        self,
        subject: str,
        role: Role,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        keep_admin: bool = False,
    ) -> bool:
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            before = self._admins(conn, hold=True) if keep_admin else set()
            gone = conn.execute("DELETE FROM roles WHERE subject = %s AND role = %s", (subject, role.value)).rowcount
            if keep_admin and before and not self._admins(conn):
                raise LastAdmin(LAST_ADMIN)
            if gone:
                changing.note("role.revoked", subject, {"role": role.value})
        return gone > 0

    def admins(self, *, conn: psycopg.Connection[Any] | None = None) -> set[str]:
        """The enabled people with the admin role, given to them or to one of their groups; given ``conn``, as its
        transaction finds them."""
        if conn is not None:
            return self._admins(conn)
        with self.db.connection() as own:
            return self._admins(own)

    def holding(self, role: Role, *, conn: psycopg.Connection[Any] | None = None) -> set[str]:
        """The enabled people given ``role``, themselves or through one of their groups (E5: who decides for the
        security team; ``_admins`` reads the admins the same way, under the last administrator's lock); given ``conn``,
        as its transaction finds them."""
        query: Final = (
            "SELECT i.id FROM identities i WHERE i.kind = 'person' AND NOT i.disabled AND ("
            " EXISTS (SELECT 1 FROM roles r WHERE r.subject = i.id AND r.role = %s)"
            " OR EXISTS (SELECT 1 FROM members m JOIN roles r ON r.subject = m.group_id"
            " WHERE m.identity_id = i.id AND r.role = %s))"
        )
        if conn is not None:
            return {identity_id for (identity_id,) in conn.execute(query, (role.value, role.value)).fetchall()}
        with self.db.connection() as own:
            return {identity_id for (identity_id,) in own.execute(query, (role.value, role.value)).fetchall()}

    def hold_admins(self, conn: psycopg.Connection[Any]) -> None:
        """The transaction's lock that every change keeping the last administrator takes (R18), held until ``conn``'s
        transaction ends. Taken before any row of people, an account or a group is held in that transaction (after the
        license's row alone, which every change reads shared first): a change that held a person's row and then waited
        for it would wait for one that holds it and waits for that row (a disable from the page and a SCIM change of the
        same person at once), which PostgreSQL would end as a deadlock."""
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('latet-last-admin:' || %s, 0))", (self.db.schema,))

    def _admins(self, conn: psycopg.Connection[Any], *, hold: bool = False) -> set[str]:
        """The enabled administrators as ``conn``'s transaction finds them. ``hold``: first the transaction's lock that
        every change keeping the last administrator takes (``hold_admins``), so that two such changes at once, on any
        instances, never both pass on what the other is taking away (the second counts what the first left: R18)."""
        if hold:
            self.hold_admins(conn)
        rows = conn.execute(
            "SELECT i.id FROM identities i WHERE i.kind = 'person' AND NOT i.disabled AND ("
            " EXISTS (SELECT 1 FROM roles r WHERE r.subject = i.id AND r.role = 'admin')"
            " OR EXISTS (SELECT 1 FROM members m JOIN roles r ON r.subject = m.group_id"
            " WHERE m.identity_id = i.id AND r.role = 'admin'))"
        ).fetchall()
        return {identity_id for (identity_id,) in rows}

    # --- the control of API keys ------------------------------------------------------------------------------

    def controllers(self, key_id: str, *, conn: psycopg.Connection[Any] | None = None) -> list[str]:
        """The people and groups that control the key; given ``conn``, as its transaction finds them."""
        query: Final = "SELECT subject FROM key_controllers WHERE key_id = %s ORDER BY subject"
        if conn is not None:
            return [subject for (subject,) in conn.execute(query, (key_id,)).fetchall()]
        with self.db.connection() as own:
            return [subject for (subject,) in own.execute(query, (key_id,)).fetchall()]

    def controlling(self, key_id: str, *, conn: psycopg.Connection[Any] | None = None) -> set[str]:
        """The enabled people who control the key, themselves or through one of their groups, in one read with no row
        held (E5: who decides on a key's change); given ``conn``, as its transaction finds them."""
        query: Final = (
            "SELECT i.id FROM identities i WHERE i.kind = 'person' AND NOT i.disabled AND ("
            " EXISTS (SELECT 1 FROM key_controllers c WHERE c.key_id = %s AND c.subject = i.id)"
            " OR EXISTS (SELECT 1 FROM members m JOIN key_controllers c ON c.subject = m.group_id"
            " WHERE c.key_id = %s AND m.identity_id = i.id))"
        )
        if conn is not None:
            return {identity_id for (identity_id,) in conn.execute(query, (key_id, key_id)).fetchall()}
        with self.db.connection() as own:
            return {identity_id for (identity_id,) in own.execute(query, (key_id, key_id)).fetchall()}

    def add_controller(
        self,
        key_id: str,
        subject: str,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> bool:
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            row = conn.execute("SELECT kind FROM identities WHERE id = %s FOR KEY SHARE", (key_id,)).fetchone()
            if row is None or row[0] != "key":
                raise KeyError(key_id)
            self._subject(conn, subject)
            added = conn.execute(
                "INSERT INTO key_controllers (key_id, subject) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (key_id, subject),
            ).rowcount
            if added:
                changing.note("key.controller.added", key_id, {"subject": subject})
        return added > 0

    def remove_controller(
        self,
        key_id: str,
        subject: str,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> bool:
        with self._within(within, actor, address) as changing:
            gone = changing.conn.execute(
                "DELETE FROM key_controllers WHERE key_id = %s AND subject = %s", (key_id, subject)
            ).rowcount
            if gone:
                changing.note("key.controller.removed", key_id, {"subject": subject})
        return gone > 0

    # --- accounts at the provider, and the groups of a sign-in ----------------------------------------------------

    def account(self, identity_id: str) -> Account | None:
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT {_ACCOUNT} FROM accounts WHERE identity_id = %s",  # noqa: S608
                (identity_id,),
            ).fetchone()
        return _account(row) if row is not None else None

    def accounts(self) -> dict[str, Account]:
        with self.db.connection() as conn:
            return self._accounts(conn)

    def scim_page(self, start: int, count: int) -> tuple[int, list[tuple[Identity, Account]]]:
        """E3's release gate 3: how many people SCIM manages (as one person's GET finds them: disabled ones too, not
        those deleted at the provider), and the page of ``count`` of them from the ``start``-th (1: the first), by when
        each was made, ties by id. One snapshot (REPEATABLE READ, read only): a person made meanwhile is in both, or in
        neither. Their ids are paged first, then the page's rows read by id, so a page reads narrow rows of everyone and
        the needed columns of its own people alone, never every account. Each identity is built as Community builds
        it; each account carries the four fields a listed resource shows (user name, externalId, SCIM's name, when it
        changed), and leaves ``subject`` and ``claimed`` empty, which a page never reads. A ``start`` past the people,
        or a ``count`` of 0, costs the count alone."""
        with self.db.transaction() as conn:
            conn.execute(_SNAPSHOT)  # first: the snapshot is taken by the count
            found = conn.execute(_SCIM_COUNT).fetchone()
            total = int(found[0]) if found is not None else 0
            if count <= 0 or start > total:
                return total, []
            ids = [row[0] for row in conn.execute(_SCIM_IDS, (count, start - 1)).fetchall()]
            rows = conn.execute(_SCIM_ROWS, (ids,)).fetchall() if ids else []
        by_id = {row[0]: row for row in rows}  # ANY() keeps no order: the page's is the ids' (N1)
        page: list[tuple[Identity, Account]] = []
        for identity_id in ids:
            row = by_id.get(identity_id)
            if row is None:  # one snapshot: never, but a page never fails for it
                continue
            identity = identity_of(row[:_IDENTITY_WIDTH])
            user_name, external_id, name, modified = row[_IDENTITY_WIDTH:]
            scim = {"name": name} if name is not None else None
            page.append((identity, Account(identity.id, user_name, None, external_id, scim, False, (), modified, True)))
        return total, page

    def _accounts(self, conn: psycopg.Connection[Any]) -> dict[str, Account]:
        rows = conn.execute(f"SELECT {_ACCOUNT} FROM accounts").fetchall()  # noqa: S608
        return {row[0]: _account(row) for row in rows}

    def signed_in(self, *, conn: psycopg.Connection[Any]) -> bool:
        """Whether an account holds the subject of someone's sign-in, as ``conn``'s transaction finds them: a new issuer
        then leaves them to no sign-in until they are linked again (M4: `latet server`'s change alone)."""
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM accounts WHERE subject IS NOT NULL)").fetchone()
        return bool(row is not None and row[0])

    def claimable(self, *, conn: psycopg.Connection[Any]) -> bool:
        """Whether an account waits for its first sign-in by its user name (``unclaimed``'s terms: SCIM or an
        administrator named it, no subject yet, not deleted at the provider), as ``conn``'s transaction finds them: a
        new user-name claim would then decide whose company account claims it (`latet server`'s change alone)."""
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM accounts WHERE user_name IS NOT NULL AND subject IS NULL AND NOT deleted "
            "AND linked_by IS NOT NULL)"
        ).fetchone()
        return bool(row is not None and row[0])

    def find(
        self,
        *,
        subject: str | None = None,
        user_name: str | None = None,
        external_id: str | None = None,
        deleted: bool = True,
    ) -> str | None:
        """The person whose account has this subject, user name (A to Z's case aside: ``user_name_key``) or SCIM
        externalId; one deleted at the provider too unless ``deleted`` is False."""
        with self.db.connection() as conn:
            rows: list[tuple[Any, ...] | None] = []
            if subject is not None:
                rows.append(
                    conn.execute("SELECT identity_id, deleted FROM accounts WHERE subject = %s", (subject,)).fetchone()
                )
            if user_name is not None:
                rows.append(
                    conn.execute(
                        'SELECT identity_id, deleted FROM accounts WHERE lower(user_name COLLATE "C") = '
                        'lower(%s::text COLLATE "C")',
                        (user_name,),
                    ).fetchone()
                )
            if external_id is not None:
                rows.append(
                    conn.execute(
                        "SELECT identity_id, deleted FROM accounts WHERE external_id = %s", (external_id,)
                    ).fetchone()
                )
        for row in rows:
            if row is not None and (deleted or not row[1]):
                return row[0]
        return None

    def unclaimed(self, user_name: str, *, scim_only: bool) -> str | None:
        """The person a first sign-in as ``user_name`` is, or a SCIM POST of that userName (``scim_only``): an account
        that names that user name (A to Z's case aside, nothing else folded: ``user_name_key``), no subject yet, not
        deleted at the provider, and claimable (``linked_by``): SCIM provisioned it or was handed the person (``scim``),
        or an administrator linked them (``admin``: a sign-in's alone, never SCIM's: M3). Never a person added by hand
        found by their e-mail, whom an attribute the provider lets its users change would hand to whoever sets it (H1),
        nor an account someone signed in to once."""
        with self.db.connection() as conn:
            if scim_only:
                row = conn.execute(
                    'SELECT identity_id FROM accounts WHERE lower(user_name COLLATE "C") = lower(%s::text COLLATE "C")'
                    " AND subject IS NULL AND NOT deleted AND scim_managed AND linked_by = 'scim'",
                    (user_name,),
                ).fetchone()
            else:
                row = conn.execute(
                    'SELECT identity_id FROM accounts WHERE lower(user_name COLLATE "C") = lower(%s::text COLLATE "C")'
                    " AND subject IS NULL AND NOT deleted AND linked_by IS NOT NULL",
                    (user_name,),
                ).fetchone()
        return row[0] if row is not None else None

    def _person(
        self,
        conn: psycopg.Connection[Any],
        identity_id: str,
        refusal: str = "an API key has no account at the provider: name a person",
    ) -> None:
        """A person, as ``conn``'s transaction finds them, their row held until it ends (a removal meanwhile waits, and
        leaves no row naming them): KeyError for no one, ValueError (``refusal``) for an API key (an account linked to
        one would sign someone in as the key)."""
        row = conn.execute("SELECT kind FROM identities WHERE id = %s FOR KEY SHARE", (identity_id,)).fetchone()
        if row is None:
            raise KeyError(identity_id)
        if row[0] != "person":
            raise ValueError(refusal)

    def link(
        self,
        identity_id: str,
        *,
        source: Literal["oidc", "scim"] = "oidc",
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        claim: str | None = None,
        **fields: Any,
    ) -> None:
        """Sets what names the person ``identity_id`` at the provider: user_name, subject, external_id, scim (a dict or
        None), deleted, claimed (a list), scim_managed (True: SCIM provisions them, and their account is SCIM's to
        claim); each checked first (ValueError: a control or bidirectional character, or past its length). Raises Taken
        when another account has that user name, subject or externalId, or when the account names another subject
        already (a subject stays: two first sign-ins at once never both link, and an account someone signed in to is
        never claimable again); its writes are a savepoint of the transaction, so that a caller may go on. A user name
        changed here (A to Z's case aside) leaves the account claimable by nobody, and ``renamed``, until an
        administrator links it or a sign-in confirms it (``_claimable``, ``_renamed``, ``confirmed``). A change of what
        names the person (``_LINKED``) is noted ``person.linked``, by the fields it set and where they come from
        (``source``), never their values (L6), and ``linked_by`` among them when ``link_person`` gives the claim.
        ``claim``: a first sign-in as that user name claims the account (``unclaimed`` found it), which is checked again
        with the account's row held (no subject yet, not deleted at the provider, claimable, that user name): Taken,
        nothing changed, when it no longer is."""
        self._link(identity_id, source=source, actor=actor, address=address, within=within, claim=claim, **fields)

    def _link(
        self,
        identity_id: str,
        *,
        source: Literal["oidc", "scim"] = "oidc",
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        linked: Linked | None = None,
        claim: str | None = None,
        noted: tuple[str, ...] = (),
        **fields: Any,
    ) -> None:
        """``link``, and ``linked``: who makes the account claimable (``link_person`` alone gives it); ``noted``: the
        fields beside ``_LINKED`` whose change ``person.linked`` names (``link_person``'s ``deleted``)."""
        values = _account_fields(fields)
        now = self.clock()
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            try:
                with conn.transaction():
                    self._person(conn, identity_id)
                    conn.execute(
                        "INSERT INTO accounts (identity_id, modified) VALUES (%s, %s) "
                        "ON CONFLICT (identity_id) DO NOTHING",
                        (identity_id, now),
                    )
                    row = conn.execute(
                        "SELECT subject, user_name, external_id, scim_managed, linked_by, deleted, renamed "
                        "FROM accounts WHERE identity_id = %s FOR UPDATE",
                        (identity_id,),
                    ).fetchone()
                    columns: tuple[str, ...] = (*_LINKED, "linked_by", "deleted", "renamed")
                    found = row or (None, None, None, False, None, False, False)
                    before: dict[str, Any] = dict(zip(columns, found, strict=True))
                    if claim is not None and not _claims(before, claim):
                        raise Taken("this account is no longer one a first sign-in may claim")
                    if "subject" in values and before["subject"] is not None and values["subject"] != before["subject"]:
                        raise Taken(LINKED_ALREADY)
                    writes = dict(values)
                    if (claimable := _claimable(before, values, linked)) != before["linked_by"]:
                        writes["linked_by"] = claimable
                    if (renamed := _renamed(before, values, linked)) != before["renamed"]:
                        writes["renamed"] = renamed
                    if writes:
                        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(key)) for key in writes)
                        query = sql.SQL("UPDATE accounts SET {sets}, modified = %s WHERE identity_id = %s")
                        conn.execute(query.format(sets=sets), (*writes.values(), now, identity_id))
            except psycopg.errors.UniqueViolation:
                raise Taken("another account has this user name, subject or externalId") from None
            changed = sorted(key for key in (*_LINKED, *noted) if key in values and values[key] != before[key])
            if linked is not None and writes.get("linked_by") is not None:
                # ``link_person`` gave the account its claim, or gave it back (after ``unlink``, or a rename): the
                # change that opens it to a first sign-in is named, even with the user name it had (the review's L2).
                changed = sorted({*changed, "linked_by"})
            if changed:
                changing.note("person.linked", identity_id, {"source": source, "fields": changed})
            if {"subject", "scim_managed"} & set(changed):
                # Whether the person is masked under the name the provider gives follows these (E3's step 6b,
                # ``EnterpriseEdition.provider_name``): once this commits, every instance's space of theirs looks again
                # (E0b's ``updated``, ``Spaces.renamed``). Only the id travels.
                changing.changes.told.append((identity_id, "updated"))

    def link_person(
        self,
        identity_id: str,
        *,
        subject: str | None = None,
        user_name: str | None = None,
        scim: bool = False,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> None:
        """A person added by hand linked to the company's identity provider (H1): by ``subject``, the one a sign-in
        begun from their own dashboard session came back with (**Link my company account**), or by ``user_name``, which
        an administrator gives (their first sign-in as it then finds them: ``unclaimed``), ``scim`` handing them to SCIM
        too: one SCIM deleted is SCIM's again, handed back on purpose, since a new provisioning of their user name takes
        back no account that was used (the final security review's M2). Raises Taken, nothing changed, when another
        account has that subject or user name, or the person's account names a subject already (a person is linked
        once). Recorded ``person.linked``."""
        if subject is None and user_name is None:
            raise ValueError("a person is linked by the subject of their own sign-in, or by their user name")
        if scim and user_name is None:
            raise ValueError("a person handed to SCIM is named by their user name at the provider")
        fields: dict[str, Any] = {"subject": subject, "user_name": user_name}
        fields = {key: value for key, value in fields.items() if value is not None}
        if scim:
            fields["scim_managed"] = True
            fields["deleted"] = False
        _account_fields(fields)  # checked before anything is written
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            with conn.transaction():  # a savepoint: a refusal leaves nothing of it, and the caller's transaction usable
                self._person(conn, identity_id)
                conn.execute(
                    "INSERT INTO accounts (identity_id, modified) VALUES (%s, %s) ON CONFLICT (identity_id) DO NOTHING",
                    (identity_id, self.clock()),
                )
                row = conn.execute(
                    "SELECT subject FROM accounts WHERE identity_id = %s FOR UPDATE", (identity_id,)
                ).fetchone()
                if row is not None and row[0] is not None and row[0] != subject:
                    raise Taken(LINKED_ALREADY)
                linked: Linked = "scim" if scim else "admin"
                source: Literal["oidc", "scim"] = "scim" if scim else "oidc"
                self._link(identity_id, within=changing, source=source, linked=linked, noted=("deleted",), **fields)

    def link_own(
        self, identity_id: str, *, subject: str, user_name: str | None, email: str | None, within: Changing
    ) -> None:
        """**Link my company account** (H1 (i)): the subject a sign-in begun from the person's own dashboard session
        came back with, linked to them only when the ID token names them as their account here does. Whoever holds that
        session may not be them (an administrator who connected a computer as them, an agent at their computer), and a
        company account of their own linked to the person would sign them in as the person. An account that has a user
        name takes the ID token whose user-name claim (``user_name``: the one the sign-in settings name) is that user
        name, A to Z's case aside, as a first sign-in's claim compares (``unclaimed``, ``user_name_key``), and nothing
        else; one that has none, the
        ID token whose e-mail (``email``: given only when the provider verified it, within the allowed domains) is the
        person's, both ASCII and compared ASCII's case aside (``_same_ascii``), when that e-mail was given by hand or by
        SCIM (the identity's ``source``, as a body naming a person reads it), never one an ID token brought. A value
        holding a character Latet never keeps is nobody's. NotYours, nothing changed, otherwise: someone with neither
        is linked by an administrator (``link_person``). So is an account whose user name was changed since a link or a
        sign-in confirmed it (``renamed``: a SCIM rename, which a leaked SCIM token can make): that user name names
        nobody's company account until an administrator links it (the claim fix's review, L1). Weighed with the
        account's row held, in the caller's transaction, then linked as ``link_person`` links (Taken when the account
        names a subject already)."""
        user_name = user_name if user_name is not None and clean(user_name, USER_NAME_LENGTH) == user_name else None
        email = email if email is not None and clean(email, EMAIL_LENGTH) == email else None
        conn = within.conn
        with conn.transaction():  # a savepoint: a refusal leaves nothing of it
            self._person(conn, identity_id)
            conn.execute(
                "INSERT INTO accounts (identity_id, modified) VALUES (%s, %s) ON CONFLICT (identity_id) DO NOTHING",
                (identity_id, self.clock()),
            )
            row = conn.execute(
                'SELECT a.user_name IS NOT NULL, lower(a.user_name COLLATE "C") = lower(%s::text COLLATE "C"), '
                "i.source, i.email, a.renamed FROM accounts a JOIN identities i ON i.id = a.identity_id "
                "WHERE a.identity_id = %s FOR UPDATE OF a",
                (user_name, identity_id),
            ).fetchone()
            named, by_user_name, source, given, renamed = row if row is not None else (False, None, None, None, False)
            by_email = source in ("local", "scim") and _same_ascii(given, email)
            if renamed or not (by_user_name if named else by_email):
                raise NotYours()
            self.link_person(identity_id, subject=subject, within=within)

    def confirmed(self, identity_id: str, user_name: str, *, within: Changing) -> None:
        """A sign-in by the subject the account names, whose user-name claim is the account's user name (A to Z's case
        aside, as ``unclaimed`` compares): it confirms the user name SCIM wrote, and ``renamed`` ends. A sign-in that
        brings another user name leaves the mark, so that `latet server people link --all` never links the account
        again by a user name only SCIM gave it (the claim fix's review, L3). In the sign-in's transaction, the person's
        row held first, then their account's, the order SCIM (``Scim._held``) and every change of People take them in:
        the other way round, a SCIM write of the same person at that moment was a deadlock (the re-review's R1).
        Nothing is recorded, since nothing that names the person changes."""
        self._person(within.conn, identity_id)
        within.conn.execute(
            "UPDATE accounts SET renamed = false WHERE identity_id = %s AND renamed AND subject IS NOT NULL "
            'AND lower(user_name COLLATE "C") = lower(%s::text COLLATE "C")',
            (identity_id, user_name),
        )

    def unlink(
        self,
        identity_id: str,
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
    ) -> bool:
        """The subject of the person's sign-ins taken from their account, by an administrator (`latet server`): the
        provider's issuer changed, and their next sign-in brings another subject, which a person is linked to once.
        Until they are linked again nobody's first sign-in claims the account, whoever manages it (``linked_by``
        unset): an account someone signed in to is never claimable again by itself, a SCIM user name included, since a
        claim takes the whole account (roles, history, values). It is claimable again only by a link made on purpose:
        an administrator's (``link_person``, ``--scim`` for one SCIM manages), or the person's own from their session.
        Recorded ``person.linked`` by the field it unset, never its value; False, nothing changed, when their account
        names no subject."""
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            row = conn.execute(
                "SELECT subject FROM accounts WHERE identity_id = %s FOR UPDATE", (identity_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return False
            conn.execute(
                "UPDATE accounts SET subject = NULL, linked_by = NULL, modified = %s WHERE identity_id = %s",
                (self.clock(), identity_id),
            )
            changing.note("person.linked", identity_id, {"source": "oidc", "fields": ["subject"], "unlinked": True})
            # Whether the provider names the person follows the subject (E3's step 6b): as ``_link`` tells it.
            changing.changes.told.append((identity_id, "updated"))
        return True

    def apply_claims(
        self,
        identity_id: str,
        claimed: Collection[str],
        mapping: Mapping[str, str],
        *,
        address: str | None = None,
        within: Changing | None = None,
    ) -> None:
        """A sign-in's groups (the ID token's, by name or ID at the provider): kept, and the person's memberships from
        sign-ins become the Latet groups ``mapping`` (the sign-in settings' groups) names for them, each made when
        missing. The administration log names the person signing in (``within``'s actor, when given)."""
        with self._within(within, identity_id, address) as changing:
            self.link(identity_id, within=changing, claimed=sorted(set(claimed)))
            self._set_oidc(changing, identity_id, _mapped(claimed, mapping))

    def regroup(
        self,
        mapping: Mapping[str, str],
        *,
        actor: str = "server",
        address: str | None = None,
        within: Changing | None = None,
        keep_admin: bool = False,
    ) -> None:
        """A new mapping, applied at once to everyone from the groups of their last sign-in: each Latet group it names
        for anyone's groups found (or made for sign-ins) once, then everyone's memberships from sign-ins made what it
        gives them, by one statement that takes out and one that puts in what changes (the locks a save holds, the last
        administrator's and the settings', are held for a few statements, however many people the server has).
        ``keep_admin``: refused (LastAdmin) when it would leave no enabled administrator, under the lock the other
        keeping changes take (L5)."""
        with self._within(within, actor, address) as changing:
            conn = changing.conn
            before = self._admins(conn, hold=True) if keep_admin else set()
            rows = conn.execute("SELECT identity_id, claimed FROM accounts").fetchall()
            mapped = {identity_id: _mapped(claimed or (), mapping) for identity_id, claimed in rows}
            groups = self._groups_named(changing, sorted({name for names in mapped.values() for name in names}))
            wanted = {
                (group_id, identity_id)
                for identity_id, names in mapped.items()
                for name in names
                if (group_id := groups[name]) is not None
            }
            held = conn.execute(
                "SELECT m.group_id, m.identity_id FROM members m JOIN accounts a ON a.identity_id = m.identity_id "
                "WHERE m.source = 'oidc'"
            ).fetchall()
            self._oidc_members(changing, {(group_id, identity_id) for group_id, identity_id in held}, wanted)
            if keep_admin and before and not self._admins(conn):
                raise LastAdmin(LAST_ADMIN)

    def _set_oidc(self, changing: Changing, identity_id: str, names: list[str]) -> None:
        """The person's memberships from sign-ins become the groups ``names``, a group SCIM pushed skipped (and said),
        a membership a sign-in made there before taken out."""
        groups = self._groups_named(changing, names)
        wanted = {(group_id, identity_id) for group_id in groups.values() if group_id is not None}
        rows = changing.conn.execute(
            "SELECT group_id FROM members WHERE identity_id = %s AND source = 'oidc'", (identity_id,)
        ).fetchall()
        self._oidc_members(changing, {(group_id, identity_id) for (group_id,) in rows}, wanted)

    def _groups_named(self, changing: Changing, names: Iterable[str]) -> dict[str, str | None]:
        """The id of the group each of ``names`` names (``_group_named``: made for sign-ins when there is none), each
        found once; None for a group SCIM pushed, which no sign-in joins (said once, in fixed words)."""
        found = {name: self._group_named(changing, name) for name in names}
        if None in found.values() and (count := self._scims.due()):  # fixed words: no group's name, no person
            log.warning(
                "the sign-in settings map a group of the ID token to a group SCIM pushed: no sign-in joins it, the "
                "mapping's other groups apply (rename the group at the provider, or map another)%s",
                f" ({count} times since the line before)" if count > 1 else "",
            )
        return found

    def _oidc_members(self, changing: Changing, before: set[tuple[str, str]], wanted: set[tuple[str, str]]) -> None:
        """The memberships from sign-ins ``before`` (group, person) made ``wanted``: those to take out by one statement,
        those to put in by another, each noted only when its row changed (two sign-ins of one person at once, or a
        sign-in during a new mapping: a membership once, and noted once)."""
        conn = changing.conn
        gone = sorted(before - wanted, key=lambda pair: (pair[1], pair[0]))
        if gone:
            pairs = ([group_id for group_id, _ in gone], [identity_id for _, identity_id in gone])
            # Their rows held first in one order, by person then group, as a sign-in's own change holds them, whatever
            # order the plan of the removal below would take them in: two such changes at once never wait on each other.
            conn.execute(
                "SELECT 1 FROM members m JOIN unnest(%s::text[], %s::text[]) AS gone(group_id, identity_id) "
                "ON m.group_id = gone.group_id AND m.identity_id = gone.identity_id WHERE m.source = 'oidc' "
                "ORDER BY m.identity_id, m.group_id FOR UPDATE OF m",
                pairs,
            )
            rows = conn.execute(
                "DELETE FROM members m USING unnest(%s::text[], %s::text[]) AS gone(group_id, identity_id) "
                "WHERE m.group_id = gone.group_id AND m.identity_id = gone.identity_id AND m.source = 'oidc' "
                "RETURNING m.group_id, m.identity_id",
                pairs,
            ).fetchall()
            for group_id, identity_id in sorted(rows, key=lambda pair: (pair[1], pair[0])):
                changing.note("member.removed", group_id, {"identity": identity_id, "source": "oidc"})
        made = sorted(wanted - before, key=lambda pair: (pair[1], pair[0]))
        if made:
            rows = conn.execute(
                "INSERT INTO members (group_id, identity_id, source) SELECT group_id, identity_id, 'oidc' "
                "FROM unnest(%s::text[], %s::text[]) AS made(group_id, identity_id) ON CONFLICT DO NOTHING "
                "RETURNING group_id, identity_id",
                ([group_id for group_id, _ in made], [identity_id for _, identity_id in made]),
            ).fetchall()
            for group_id, identity_id in sorted(rows, key=lambda pair: (pair[1], pair[0])):
                changing.note("member.added", group_id, {"identity": identity_id, "source": "oidc"})

    def _group_named(self, changing: Changing, name: str) -> str | None:
        """The group of this name (case aside), made for sign-ins when there is none; None when it is a group SCIM
        pushed, which no sign-in joins (a group made by hand is the administrator's choice, and is joined)."""
        name = group_name(name)
        group_id, now = new_group_id(), self.clock()
        made = changing.conn.execute(
            "INSERT INTO groups (id, name, external_id, source, created, modified) "
            "VALUES (%s, %s, NULL, 'oidc', %s, %s) ON CONFLICT DO NOTHING",
            (group_id, name, now, now),
        ).rowcount
        if made:
            changing.note("group.added", group_id, {"source": "oidc"})
            return group_id
        row = changing.conn.execute("SELECT id, source FROM groups WHERE lower(name) = lower(%s)", (name,)).fetchone()
        if row is None:  # the id drawn was taken: never in practice
            raise Taken("a group for this sign-in's mapping cannot be made")
        return None if row[1] == "scim" else row[0]

    def mapping_taken(self, names: Iterable[str]) -> list[str]:
        """Those of ``names`` (the Latet groups the sign-in settings' mapping names) that a group SCIM pushed holds,
        compared as the names' unique index compares them (``lower``): lines of the mapping no sign-in applies, which
        the Sign-in page's check says."""
        wanted = sorted({name for name in names if name})
        if not wanted:
            return []
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT t.name FROM unnest(%s::text[]) AS t(name) WHERE EXISTS "
                "(SELECT 1 FROM groups g WHERE lower(g.name) = lower(t.name) AND g.source = 'scim')",
                (wanted,),
            ).fetchall()
        return sorted(name for (name,) in rows)

    def directory(self) -> Directory:
        """Everything E3 keeps, read at once."""
        members: dict[str, list[tuple[str, Source]]] = {}
        roles: dict[str, set[Role]] = {}
        controllers: dict[str, list[str]] = {}
        with self.db.connection() as conn:
            groups = [Group(*row) for row in conn.execute(f"SELECT {_GROUP} FROM groups")]  # noqa: S608
            for group_id, identity_id, source in conn.execute(
                "SELECT group_id, identity_id, source FROM members ORDER BY identity_id, source"
            ):
                members.setdefault(group_id, []).append((identity_id, source))
            for subject, role in conn.execute("SELECT subject, role FROM roles"):
                if role in _ROLES:
                    roles.setdefault(subject, set()).add(_ROLES[role])
            for key_id, subject in conn.execute("SELECT key_id, subject FROM key_controllers ORDER BY subject"):
                controllers.setdefault(key_id, []).append(subject)
            accounts = self._accounts(conn)
        groups.sort(key=lambda group: group.name.casefold())
        frozen = {subject: frozenset(given) for subject, given in roles.items()}
        return Directory(groups, members, frozen, controllers, accounts)

    # --- who disabled a person ----------------------------------------------------------------------------------

    def _disabled_by(self, conn: psycopg.Connection[Any], identity_id: str, by: By) -> None:
        """Who disabled the person, in their account (one made when they have none; an API key has none)."""
        conn.execute(
            "INSERT INTO accounts (identity_id, disabled_by, modified) "
            "SELECT id, %s, %s FROM identities WHERE id = %s AND kind = 'person' "
            "ON CONFLICT (identity_id) DO UPDATE SET disabled_by = EXCLUDED.disabled_by, modified = EXCLUDED.modified",
            (by, self.clock(), identity_id),
        )


def disable_person(
    people: People,
    identity_id: str,
    *,
    by: By = "local",
    actor: str = "server",
    address: str | None = None,
    within: Changing | None = None,
    keep_admin: bool = False,
) -> bool:
    """A person disabled at once: their computers' tokens stop (E0b's Identities.disable, in the transaction) and, once
    it commits, every instance hears it on `identities` and ends their dashboard sessions (E0b's Server._ended: no part
    ends them itself); their place in the count of users is free; their space's data stays. ``by``: who disables them,
    an administrator ("local") or SCIM, kept in their account (L7): an administrator's disable of someone SCIM disabled
    becomes theirs, which SCIM does not lift. With ``keep_admin``, refused (LastAdmin) for the last enabled
    administrator. True when it disabled them, or made SCIM's disable an administrator's."""
    with people._within(within, actor, address) as changing:
        conn = changing.conn
        if keep_admin and people._admins(conn, hold=True) == {identity_id}:
            raise LastAdmin(LAST_ADMIN)
        done = people.identities.disable(identity_id, within=changing.changes)
        if done:
            people._disabled_by(conn, identity_id, by)
        held = (
            not done
            and by == "local"
            and conn.execute(
                "UPDATE accounts a SET disabled_by = 'local', modified = %s FROM identities i "
                "WHERE a.identity_id = %s AND i.id = a.identity_id AND i.disabled AND a.disabled_by = 'scim'",
                (people.clock(), identity_id),
            ).rowcount
            > 0
        )
        if done or held:
            changing.note("person.disabled", identity_id, {"by": by})
    return done or held


def enable_person(
    people: People,
    identity_id: str,
    *,
    by: By = "local",
    actor: str = "server",
    address: str | None = None,
    within: Changing | None = None,
) -> bool:
    """A person enabled again (EmailTaken, nothing changed, when another enabled person has their e-mail now). ``by``:
    who enables them; SCIM lifts only a disable of its own, never an administrator's nor one no one recorded (L7):
    False then, nothing changed."""
    with people._within(within, actor, address) as changing:
        conn = changing.conn
        if by == "scim":
            row = conn.execute(
                "SELECT disabled_by FROM accounts WHERE identity_id = %s FOR UPDATE", (identity_id,)
            ).fetchone()
            if row is None or row[0] != "scim":
                return False
        done = people.identities.enable(identity_id, within=changing.changes)
        if done:
            conn.execute(
                "UPDATE accounts SET disabled_by = NULL, modified = %s WHERE identity_id = %s",
                (people.clock(), identity_id),
            )
            changing.note("person.enabled", identity_id, {"by": by})
    return done
