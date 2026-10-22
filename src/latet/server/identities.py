"""The identities of a Latet server, people and API keys, with their secrets; and the administration's tokens.

A person gets a one-time code (24 hours) and swaps it on each computer for a personal token; an API key is an identity
of its own, made by an administrator, its secret shown once. Every lookup asks whether the identity is enabled; an
instance keeps what it looked up for a few seconds, and forgets it at once when the `identities` channel says the
identity changed (each change is published there). A lookup that began before a change and ends after it keeps nothing
of what it read, so that an identity disabled here stops here at once, as it does on the other instances.

The server never keeps a secret, a one-time code or a route key. It keeps a keyed hash of each (`tokens.keyed_hash`: an
HMAC-SHA256 under the credentials' key, which a server derives from the master key), and with every token, one-time code
and administration token a tag, keyed the same way, over what its row says: a token's tag covers its id, its identity,
its hash and its route; a code's its hash, its identity and when it ends; an administration token's its id, its hash and
when it ends. A person's token also keeps its computer's key (R16) by a keyed hash over the token's id and the key,
which binds the hash to its own row: the computer's CLI alone has the key, and the server asks for it on a sign-in link
to the dashboard and on a change of the person's identity (``computer_key``). Someone who writes the database without
the key can make neither the hash of a secret of his own choosing nor a row that a lookup believes, and a row that was
made or changed without the key finds no one: its tag is checked before anything is kept in memory, and a row that
fails is said in the log, once a minute at most, without a word of it.

What a tag cannot stop: a row that the key made and that was copied before its deletion (a token revoked or rotated, a
one-time code used, an administration token revoked) holds again when it is written back as it was; `disabled` is not
tagged, and set back by hand it enables the identity; and a database restored from an older copy brings all of it back.
Only state kept outside the database stops a rollback (as the administration log's chain says of itself). The
credentials' key derives from the current version of the master key: a rotation of the master key (not in these plans)
must make the hashes and the tags again, or every computer enrols again.

A name, an email and a label come from a person, from a computer, and later from another system (SCIM, OIDC); an
administrator reads them in a terminal and in a page. They are kept without control characters (an escape sequence
would act in the terminal, a bidirectional override would make one name read as another) and no longer than a limit.

A change can be made in a transaction its caller holds (``changing``), with the entry of the administration log that
records it written last there: both are kept, or neither. What a change tells the instances is told once that
transaction has committed, never before (an instance that heard of a change rolled back would forget nothing wrong,
but one told before the commit could read the row as it was)."""

from __future__ import annotations

import base64
import contextlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, Literal, LiteralString

from latet.logline import Paced, described
from latet.server.bus import Bus
from latet.server.db import Database
from latet.server.tokens import (
    ADMIN_PREFIX,
    COMPUTER_PREFIX,
    KEY_PREFIX,
    OUTLIVES_ISSUER,
    TOKEN_PREFIX,
    check_key,
    is_route_key,
    keyed_hash,
    new_id,
    new_secret,
    route_key,
)
from latet.state import IdentityRef

log = logging.getLogger(__name__)

__all__ = [
    "ADMIN_TOKEN_ID",
    "CACHE_SECONDS",
    "CACHE_SIZE",
    "CODE_TTL",
    "EMAIL_LENGTH",
    "EXTERNAL_ID_LENGTH",
    "IDENTITY_ID",
    "LABEL_LENGTH",
    "NAME_LENGTH",
    "TOKEN_ID",
    "AdminTokenEnded",
    "AdminTokenInfo",
    "AdminTokens",
    "Changes",
    "Identities",
    "Identity",
    "IdentityRef",
    "NoSuchIdentity",
    "OutlivesIssuer",
    "TokenInfo",
    "clean",
]

CODE_TTL = 24 * 3600.0
CACHE_SECONDS = 5.0  # what an instance keeps of a lookup, unless a message drops it first
CACHE_SIZE = 10_000  # identities an instance keeps at most: those it found, never a lookup that found nothing
SEEN_EVERY = 60.0  # last_seen and last_used are written at most this often by an instance
# The uses waiting to be written at most (``Notes``): past it one is dropped, and written at a later use.
NOTES_WAITING = 1_000
# The thread that writes when a token, an API key, an administration token or an identity was last used: never the
# lookups' own, which a row held by a long transaction (a removal's cascade over a large space) would hold with it.
_NOTING = ThreadPoolExecutor(1, thread_name_prefix="latet-noted")
NAME_LENGTH, EMAIL_LENGTH, LABEL_LENGTH = 200, 320, 100  # characters kept of a name, an email, a label
EXTERNAL_ID_LENGTH = 255  # characters of the id another system gave an identity, at most (an OpenID `sub` is the same)
SAY_EVERY = 60.0  # seconds between two lines of the log about rows that do not hold
_COLUMNS: Final = "i.id, i.name, i.email, i.kind, i.disabled, i.created, i.last_seen, i.source, i.external_id"
_SELECT: Final = f"SELECT {_COLUMNS} FROM identities i"  # noqa: S608 - built from this module's constants alone
# An identity made, with the columns of ``_COLUMNS`` as its row has them: none made when the id is taken.
_INSERTED: Final = (
    "INSERT INTO identities (id, kind, name, email, created, source, external_id)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING"
    " RETURNING id, name, email, kind, disabled, created, last_seen, source, external_id"
)
# An identity with the columns of the token it is found by, for the tag to be checked on.
_OF_TOKEN: Final = (
    f"SELECT {_COLUMNS}, t.id, t.hash, t.route, t.tag FROM identities i JOIN tokens t ON t.identity_id = i.id"  # noqa: S608
)
# What a keyed hash is of (the label of ``keyed_hash``): the hash of a thing is never the hash or the tag of another.
_SECRET, _ROUTE, _CODE, _ADMIN = "secret", "route", "code", "admin"
_TOKEN_TAG, _CODE_TAG, _ADMIN_TAG = "token-tag", "code-tag", "admin-tag"
_COMPUTER = "computer-key"  # a computer's key, over its token's id and the key: bound to its own row
_ADMIN_LISTED = "admin-listed"  # the id a row written by hand with an id of another shape is listed and revoked by
SOURCES = ("local", "scim", "oidc")  # where an identity comes from: by hand, a SCIM provider, an OpenID provider
# What is taken out of a name, an email or a label (and refused in an external id): the C0 and C1 controls (an escape,
# a bell, a zero byte, the 8-bit introducer of a sequence), the surrogates (a lone one is no text, and PostgreSQL's
# driver cannot write it), the bidirectional controls of Unicode (its Bidi_Control property: the Arabic letter mark,
# the left-to-right and right-to-left marks, the embeddings and overrides, the isolates), which change the order a
# text is shown in, and what shows as nothing or splits a line: the zero-width space, the line and paragraph
# separators (which Python's splitlines splits on), the word joiner and the byte order mark. The zero-width
# non-joiner and joiner stay: scripts and emoji need them.
_UNWANTED_RANGES = (
    (0x0000, 0x001F),
    (0x007F, 0x009F),
    (0x061C, 0x061C),
    (0x200B, 0x200B),
    (0x200E, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x2060),
    (0x2066, 0x2069),
    (0xD800, 0xDFFF),
    (0xFEFF, 0xFEFF),
)
_UNWANTED = re.compile("[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _UNWANTED_RANGES) + "]")
# An identity's id, a token's and an administration token's, as the server makes them (``tokens.new_id``): never a
# pattern given.
IDENTITY_ID = re.compile(r"u_[a-z2-7]{10}")
TOKEN_ID = re.compile(r"t_[a-z2-7]{10}")
ADMIN_TOKEN_ID = re.compile(r"a_[a-z2-7]{10}")
# The administration token's columns, in the order ``AdminTokens._info`` reads them.
_ADMIN_COLUMNS: Final = "id, label, created, last_used, expires, hash, tag, issued_by"
_ADMIN_ISSUED: Final = (
    "INSERT INTO admin_tokens (id, hash, label, created, expires, tag, issued_by) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
# A token's row as the transaction that makes a change finds it, locked until that transaction ends: its revocation
# waits for it (``FOR KEY SHARE`` takes nothing from a note of its last use); a revocation's own rows, ``FOR UPDATE``.
_ADMIN_HELD: Final = f"SELECT {_ADMIN_COLUMNS} FROM admin_tokens WHERE id = %s FOR KEY SHARE"  # noqa: S608
_ADMIN_HELD_FOR_UPDATE: Final = f"SELECT {_ADMIN_COLUMNS} FROM admin_tokens WHERE id = %s FOR UPDATE"  # noqa: S608
_ADMIN_EVERY: Final = f"SELECT {_ADMIN_COLUMNS} FROM admin_tokens ORDER BY created, id"  # noqa: S608
_ADMIN_BY_HASH: Final = f"SELECT {_ADMIN_COLUMNS} FROM admin_tokens WHERE hash = %s"  # noqa: S608


class NoSuchIdentity(LookupError):
    """The identity a change names is not there (removed, on this instance or another): nothing was done. Its words
    quote nothing of what the caller gave."""


class AdminTokenEnded(Exception):
    """The administration token a change is made with is no longer there (revoked, on any instance, or its row changed
    by hand) or is past its day, as the change's own transaction finds it: the change is not made, its transaction
    rolled back. ``reason``: ``revoked`` or ``expired``, what the administration log records of the call. Its words
    quote nothing."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the administration token this change was made with is {reason}")
        self.reason = reason


class EmailTaken(ValueError):
    """A person asked for with an e-mail address an enabled person has already (whatever its case): none is made. It
    carries that person's id, which its words name, never the address."""

    def __init__(self, identity_id: str) -> None:
        super().__init__(
            f"an enabled person has this e-mail address already: {identity_id} (`latet server people list`)"
        )
        self.identity_id = identity_id


class OutlivesIssuer(ValueError):
    """An administration token asked for would end after the token that issues it (``AdminTokens.issued``'s
    ``not_after``, which it carries): none is made."""

    def __init__(self, not_after: float) -> None:
        super().__init__(OUTLIVES_ISSUER)
        self.not_after = not_after


def clean(text: str, limit: int) -> str:
    """``text`` as it is kept: without the characters of ``_UNWANTED``, stripped, and cut at ``limit`` characters."""
    return _UNWANTED.sub("", text).strip()[:limit].rstrip()


# What an instance that is not told of a change keeps doing, by the change: said with it in the log.
_UNTOLD = {
    "added": "they find it at their first lookup",
    "enabled": f"what they keep of it lasts {CACHE_SECONDS:g} seconds at most",
    "updated": (
        f"what they keep of it lasts {CACHE_SECONDS:g} seconds at most, and a space of it there keeps its name until "
        "it is made again"
    ),
    "closing": (
        f"what they keep of it lasts {CACHE_SECONDS:g} seconds at most, and a space of it there is closed at their "
        "next check of their spaces"
    ),
}


def _same(found: str, expected: str) -> bool:
    """Whether a tag read from the database is the one expected, in constant time (bytes, so that a tag with any
    character in it, which a writer of the database may have put there, is a mismatch and not an error)."""
    return hmac.compare_digest(found.encode("utf-8", "surrogatepass"), expected.encode())


def _when(moment: float | None) -> str:
    """A time as a tag takes it: exactly (a float's hex form), whatever it was written as; ``never`` for none."""
    return "never" if moment is None else float(moment).hex()


class _Tampering:
    """The rows that were refused because their tag does not hold: said in the log, for each kind of row the first at
    once and then one line a minute at most with how many of that kind since its line before. A line names the kind of
    row and nothing of it, since a writer of the database can fill a row with anything."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._said: dict[str, float] = {}  # by kind of row: when its line was said last
        self._unsaid: dict[str, int] = {}  # by kind of row: how many met since

    def met(self, what: str) -> None:
        now = self._clock()
        with self._lock:
            self._unsaid[what] = self._unsaid.get(what, 0) + 1
            said = self._said.get(what)
            if said is not None and abs(now - said) < SAY_EVERY:
                return
            self._said[what], count, self._unsaid[what] = now, self._unsaid[what], 0
        log.warning(
            "%s whose tag does not hold was refused: its row was made or changed without this server's key%s",
            what.capitalize(),
            f" ({count} since the line before)" if count > 1 else "",
        )


class Notes:
    """When a token, an API key, an administration token or an identity was last used, written in a thread of its own
    (``_NOTING``), at most once a minute for each by an instance (``due``): never in the thread that looks them up,
    which a row held by a long transaction would hold with it (a removal's cascade over a large space,
    ``STATEMENT_SECONDS``). ``NOTES_WAITING`` wait at most: past it a use is dropped, and noted at a later one. A write
    that fails is said in the log by its error's type, once a minute. Safe across threads."""

    def __init__(self, db: Database, clock: Callable[[], float]) -> None:
        self.db, self._clock = db, clock
        self._lock = threading.Lock()
        self._waiting = 0
        # When each use was noted last, by what it is of; the one noted longest ago is dropped past CACHE_SIZE.
        self._written: OrderedDict[str, float] = OrderedDict()
        self._failed = Paced()

    def due(self, key: str) -> bool:
        """Whether the use of ``key`` is to be noted now: at most once in ``SEEN_EVERY`` seconds by this instance."""
        now = self._clock()
        with self._lock:
            last = self._written.get(key)
            if last is not None and now - last < SEEN_EVERY:
                return False
            self._written[key] = now
            self._written.move_to_end(key)
            while len(self._written) > CACHE_SIZE:  # the one dropped is noted again at its next use: early, no more
                self._written.popitem(last=False)
        return True

    def note(self, query: LiteralString, *args: Any) -> Future[None] | None:
        """``query`` run with ``args`` in the notes' thread; its future, or None when ``NOTES_WAITING`` wait already
        (or the process's threads are let go)."""
        with self._lock:
            if self._waiting >= NOTES_WAITING:
                return None
            self._waiting += 1
        try:
            return _NOTING.submit(self._write, query, args)
        except RuntimeError:  # the threads of the process are let go: it stops
            self._left()
            return None

    def _left(self) -> None:
        with self._lock:
            self._waiting -= 1

    def _write(self, query: LiteralString, args: tuple[Any, ...]) -> None:
        try:
            with self.db.connection() as conn:
                conn.execute(query, args)
        except Exception as exc:  # its words can quote what the database was given: its type alone
            if count := self._failed.due():
                log.warning(
                    "a use could not be noted (%s): it is noted at a later one%s",
                    described(exc),
                    f" ({count} times since the line before)" if count > 1 else "",
                )
        finally:
            self._left()


@dataclass(frozen=True)
class Identity:
    """An identity. Its name, its email and its external id are out of its ``repr``: an identity that is printed, in a
    log or an error, names itself by its id."""

    id: str
    name: str = field(repr=False)
    email: str | None = field(repr=False)
    kind: Literal["person", "key"]
    disabled: bool
    created: float
    last_seen: float | None
    source: Literal["local", "scim", "oidc"]
    external_id: str | None = field(repr=False)

    def ref(self) -> IdentityRef:
        return IdentityRef(self.id, self.name, self.kind)


@dataclass(frozen=True)
class TokenInfo:
    id: str
    label: str
    created: float
    last_used: float | None
    # Its tag does not hold: its row was made or changed without this server's key, and finds no one. Listed and
    # flagged so, never hidden: an administrator sees what the database holds.
    tampered: bool = False


@dataclass(eq=False)
class Changes:
    """A transaction for changes to identities (``Identities.changing``): its connection, on which the caller writes
    what records the changes too, and what they tell the instances once it commits. Printed, it names nothing."""

    conn: Any = field(repr=False)
    told: list[tuple[str, str]] = field(default_factory=list, repr=False)  # (identity's id, event), in order


def _identity(row: Any) -> Identity:
    return Identity(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6], row[7], row[8])


def _fresh(entry: tuple[Identity, float] | None, now: float) -> bool:
    """Whether an entry of the memory is good at ``now``: its end is ahead, and no further ahead than a lookup is kept
    (a wall clock that stepped back would otherwise make every entry last the step longer)."""
    return entry is not None and 0 < entry[1] - now <= CACHE_SECONDS


class Identities:
    """``key``: the credentials' key, 32 bytes (on a server ``MasterKey.derive(b"credentials")``), under which every
    hash and tag of a token, an API key, a one-time code and a route key is made and checked."""

    def __init__(self, db: Database, bus: Bus, *, key: bytes, clock: Callable[[], float] = time.time) -> None:
        self.db, self.bus, self._clock = db, bus, clock
        self._key = check_key(key)
        self._tampering = _Tampering(clock)
        self._lock = threading.Lock()
        # "secret:<hash>", "route:<hash>", "id:<id>" -> the identity found, and until when: a bounded memory, the least
        # recently used dropped first; a lookup that found nothing is never kept (a stranger's guesses must not fill
        # it).
        self._cache: OrderedDict[str, tuple[Identity, float]] = OrderedDict()
        # Counted by every ``forget``. A lookup notes it before it reads and keeps what it found only if it has not
        # moved: what it read may be what the change that was told meanwhile has already made old.
        self._generation = 0
        self.notes = Notes(db, clock)  # last_seen and last_used, written in a thread of their own

    # --- what an instance keeps -------------------------------------------------------------------

    def _cached(self, key: str, load: Callable[[], Identity | None]) -> Identity | None:
        now = self._clock()
        with self._lock:
            found = self._cache.get(key)
            if found is not None and _fresh(found, now):
                self._cache.move_to_end(key)
                return found[0]
            generation = self._generation  # before ``load`` reads anything
        identity = load()
        if identity is None:
            return None
        with self._lock:
            if self._generation != generation:  # something was forgotten while it read: it answers, and keeps nothing
                return identity
            if len(self._cache) >= CACHE_SIZE:  # what expired goes first, then the least recently used
                for stale in [k for k, v in self._cache.items() if not _fresh(v, now)]:
                    del self._cache[stale]
                while len(self._cache) >= CACHE_SIZE:
                    self._cache.popitem(last=False)
            self._cache[key] = (identity, now + CACHE_SECONDS)
            self._cache.move_to_end(key)
        return identity

    def holds(self, *, secret: str | None = None, route: str | None = None) -> bool:
        """Whether this instance's memory holds, still good, what a lookup of ``secret`` and a lookup of ``route`` (each
        one that is given) would find: such a lookup reads no database. False when neither is given, and for a text no
        lookup would find (it is no token, no address): it is a miss like any other. It changes nothing of the memory,
        its order included. For the gate: a request that the memory serves needs no look at the limits that guard the
        database; what it answers may be out of date by the time the lookup is made, which then reads the database."""
        keys = []
        if secret is not None:
            if not secret.startswith((TOKEN_PREFIX, KEY_PREFIX)):
                return False
            keys.append(f"secret:{self._hash(_SECRET, secret)}")
        if route is not None:
            if not is_route_key(route):
                return False
            keys.append(f"route:{self._hash(_ROUTE, route)}")
        if not keys:
            return False
        now = self._clock()
        with self._lock:
            return all(_fresh(self._cache.get(key), now) for key in keys)

    def remembered(self, *, secret: str | None = None, route: str | None = None) -> Identity | None:
        """The enabled identity that this instance's memory holds, still good, for ``secret`` (or, when it is not
        given, ``route``): what ``by_secret`` or ``by_route`` would answer from it. No store is asked, ever, and nothing
        is written (a token's use and an identity's visit are noted by the next lookup that reads): the gate serves a
        request it holds on the event loop with this, and looks up the others in its threads. None when the memory does
        not hold it, and for a text no lookup would find."""
        if secret is not None:
            if not secret.startswith((TOKEN_PREFIX, KEY_PREFIX)):
                return None
            key = f"secret:{self._hash(_SECRET, secret)}"
        elif route is not None:
            if not is_route_key(route):
                return None
            key = f"route:{self._hash(_ROUTE, route)}"
        else:
            return None
        now = self._clock()
        with self._lock:
            found = self._cache.get(key)
            if found is None or not _fresh(found, now):
                return None
            self._cache.move_to_end(key)  # a use, as a lookup's
            return found[0]

    @property
    def generation(self) -> int:
        """Counted by every ``forget``: a lookup begun before it may answer what the change made old
        (``machines.OneAtATime``)."""
        return self._generation

    def held(self, identity_id: str) -> Identity | None:
        """The identity of ``identity_id`` that this instance's memory holds, still good (what ``get`` would answer from
        it, disabled or not): no store is asked, so the gate asks it on the event loop. None when it does not hold
        it."""
        key, now = f"id:{identity_id}", self._clock()
        with self._lock:
            found = self._cache.get(key)
            if found is None or not _fresh(found, now):
                return None
            self._cache.move_to_end(key)
            return found[0]

    def forget(self, identity_id: str | None = None) -> None:
        """Forget what this instance looked up of ``identity_id`` (None: of every identity): it changed. A lookup that
        is reading meanwhile does not keep what it reads."""
        with self._lock:
            self._generation += 1
            if identity_id is None:
                self._cache.clear()
            else:
                for key in [k for k, v in self._cache.items() if v[0].id == identity_id]:
                    del self._cache[key]

    def _changed(self, identity_id: str, event: str) -> None:
        """Every instance, this one too, forgets what it kept of the identity: told once its change has committed. When
        it cannot be told (Valkey does not answer), the error is said in the log by its type, whatever the bus raised,
        with what was not done and the identity's id, and not raised: an operation that took effect is answered as one
        (a retried one would make a second identity, or lose a key's new secret)."""
        self.forget(identity_id)
        try:
            self.bus.publish("identities", {"id": identity_id, "event": event})
        except Exception as exc:
            log.warning(
                "a change of %s (%s) could not be told to the other instances (%s): %s",
                identity_id,
                event,
                type(exc).__name__,
                _UNTOLD.get(event, _UNTOLD["closing"]),
            )

    @contextlib.contextmanager
    def changing(self) -> Iterator[Changes]:
        """A transaction for changes to identities: each method given it (``within``) writes there, and the caller can
        write there too what records the changes (``AuditLog.record(conn=...)``, last). What the changes tell the
        instances is told once the transaction has committed, and nothing when it does not."""
        with self.db.transaction() as conn:
            changes = Changes(conn)
            yield changes
        for identity_id, event in changes.told:
            self._changed(identity_id, event)

    @contextlib.contextmanager
    def _within(self, within: Changes | None) -> Iterator[Changes]:
        """``within``, the caller's transaction; or one of the method's own, committed and told when it ends."""
        if within is not None:
            yield within
            return
        with self.changing() as changes:
            yield changes

    def _one(self, query: LiteralString, *args: Any) -> Identity | None:
        with self.db.connection() as conn:
            row = conn.execute(query, args).fetchone()
        return _identity(row) if row is not None else None

    def _hash(self, label: str, *parts: str) -> str:
        return keyed_hash(self._key, label, *parts)

    def _owner(self, query: LiteralString, digest: str) -> Identity | None:
        """The enabled identity that the token found by ``digest`` (the hash of its secret or of its route key) gives,
        when the tag of its row holds: what the row says is checked before the row is believed, and a row that fails
        finds no one (and so is never kept in memory)."""
        with self.db.connection() as conn:
            row = conn.execute(query, (digest,)).fetchone()
        if row is None:
            return None
        identity, (token_id, hashed, route, tag) = _identity(row), row[9:13]
        if not _same(tag, self._hash(_TOKEN_TAG, token_id, identity.id, hashed, route)):
            self._tampering.met("a token")
            return None
        return identity

    # --- making them -----------------------------------------------------------------------------

    def _insert(
        self, changes: Changes, kind: str, name: str, email: str | None, source: str, external_id: str | None
    ) -> Identity:
        name = clean(name, NAME_LENGTH)
        if not name:
            raise ValueError("a name is required")
        if source not in SOURCES:
            raise ValueError("a source is local, scim or oidc")
        # The id another system gave is not cleaned: it must match the provider's byte for byte, so it is refused.
        if external_id is not None and (len(external_id) > EXTERNAL_ID_LENGTH or _UNWANTED.search(external_id)):
            raise ValueError(
                f"an external id holds no control character and is {EXTERNAL_ID_LENGTH} characters at most"
            )
        address = (clean(email, EMAIL_LENGTH) if email else "") or None
        for _ in range(10):
            identity_id = new_id("u_")
            row = changes.conn.execute(
                _INSERTED, (identity_id, kind, name, address, self._clock(), source, external_id)
            ).fetchone()
            if row is not None:
                changes.told.append((identity_id, "added"))
                return _identity(row)
        raise RuntimeError("no free identity id after ten tries")

    def _new_secret(
        self, conn: Any, identity_id: str, prefix: str, label: str, *, computer: bool = False
    ) -> tuple[str, str | None]:
        """A new secret's row: the secret, and, for a ``computer``'s token, its computer's key (kept by its keyed hash
        over the token's id: ``_COMPUTER``); None for an API key, which has no computer."""
        secret = new_secret(prefix)
        token_id, hashed, route = new_id("t_"), self._hash(_SECRET, secret), self._hash(_ROUTE, route_key(secret))
        key = new_secret(COMPUTER_PREFIX) if computer else None
        conn.execute(
            "INSERT INTO tokens (id, identity_id, hash, route, label, created, tag, computer_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                token_id,
                identity_id,
                hashed,
                route,
                clean(label, LABEL_LENGTH) or "a computer",
                self._clock(),
                self._hash(_TOKEN_TAG, token_id, identity_id, hashed, route),
                self._hash(_COMPUTER, token_id, key) if key is not None else None,
            ),
        )
        return secret, key

    def add_person(
        self,
        name: str,
        email: str | None = None,
        *,
        source: str = "local",
        external_id: str | None = None,
        within: Changes | None = None,
    ) -> tuple[Identity, str]:
        """A person, and their first one-time code, in one transaction (``within``: the caller's). Raises EmailTaken
        when an enabled person has ``email`` already, whatever its case (``_held_email``)."""
        with self._within(within) as changes:
            address = (clean(email, EMAIL_LENGTH) if email else "") or None
            if address is not None:
                self._held_email(changes.conn, address)
            identity = self._insert(changes, "person", name, email, source, external_id)
            code = self.code(identity.id, within=changes)
        assert code is not None
        return identity, code

    def _held_email(self, conn: Any, address: str, other_than: str | None = None) -> None:
        """``address`` held for the rest of the transaction, then checked: raises EmailTaken when an enabled person
        other than ``other_than`` has it, whatever its case. The lock is keyed on the address as PostgreSQL's
        ``lower()`` folds it, the folding the check makes, so that an add, an address changed and a person enabled with
        it at once on any instances leave one enabled person with it. A caller takes it after the identity's row it
        changes, and waits for no other row while it holds it: no two of them wait on each other."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('latet-person-email:' || %s || ':' || lower(%s), 0))",
            (self.db.schema, address),
        )
        taken = conn.execute(
            "SELECT id FROM identities WHERE kind = 'person' AND NOT disabled AND lower(email) = lower(%s)"
            " AND id IS DISTINCT FROM %s ORDER BY created, id LIMIT 1",
            (address, other_than),
        ).fetchone()
        if taken is not None:
            raise EmailTaken(taken[0])

    def add_key(self, name: str, *, within: Changes | None = None) -> tuple[Identity, str]:
        """An API key: an identity of its own, and its secret, shown once; in one transaction."""
        with self._within(within) as changes:
            identity = self._insert(changes, "key", name, None, "local", None)
            return identity, self._new_secret(changes.conn, identity.id, KEY_PREFIX, "key")[0]

    def _locked(self, conn: Any, identity_id: str) -> Identity | None:
        """The identity as the database has it, its row locked until the transaction ends: a removal or a change made
        meanwhile waits for it, and one made before is seen (this instance's memory could be a few seconds late)."""
        row = conn.execute(f"{_SELECT} WHERE i.id = %s FOR UPDATE OF i", (identity_id,)).fetchone()
        return _identity(row) if row is not None else None

    def rotate_key(self, identity_id: str, *, within: Changes | None = None) -> str:
        """A new secret for an API key (and so a new address): the one before stops at once. Raises NoSuchIdentity, in
        words that quote nothing of what the caller gave, when there is no such API key, as the database has it now."""
        with self._within(within) as changes:
            identity = self._locked(changes.conn, identity_id)
            if identity is None or identity.kind != "key":
                raise NoSuchIdentity("there is no such API key")
            changes.conn.execute("DELETE FROM tokens WHERE identity_id = %s", (identity_id,))
            secret = self._new_secret(changes.conn, identity_id, KEY_PREFIX, "key")[0]
            changes.told.append((identity_id, "rotated"))
        return secret

    def code(self, identity_id: str, *, within: Changes | None = None) -> str | None:
        """A new one-time code for 24 hours, for a person (None for an API key, or no one, as the database has it now):
        the one before stops."""
        with self._within(within) as changes:
            identity = self._locked(changes.conn, identity_id)
            if identity is None or identity.kind != "person":
                return None
            code = secrets.token_urlsafe(24)
            hashed, expires = self._hash(_CODE, code), self._clock() + CODE_TTL
            changes.conn.execute("DELETE FROM codes WHERE identity_id = %s", (identity_id,))
            changes.conn.execute(
                "INSERT INTO codes (hash, identity_id, expires, tag) VALUES (%s, %s, %s, %s)",
                (hashed, identity_id, expires, self._hash(_CODE_TAG, hashed, identity_id, _when(expires))),
            )
        return code

    def enrol(self, code: str, label: str, *, within: Changes | None = None) -> tuple[Identity, str, str] | None:
        """The person, a new personal token for a computer and that computer's key (each shown this once: the server
        keeps their keyed hashes); None when the code is unknown, used, late, written by hand (its tag does not hold),
        or its person disabled. The code is spent in the transaction that makes the token
        (``within``: the caller's, which records the token there too): whatever the answer once it commits (a second
        try of the code waits for it, and finds it spent), and not at all when it fails, so that the computer can try
        the code again, no token and no entry made."""
        hashed = self._hash(_CODE, code)
        with self._within(within) as changes:
            conn = changes.conn
            row = conn.execute(
                "DELETE FROM codes WHERE hash = %s RETURNING identity_id, expires, tag", (hashed,)
            ).fetchone()
            if row is None:
                return None
            identity_id, expires, tag = row
            if not _same(tag, self._hash(_CODE_TAG, hashed, identity_id, _when(expires))):
                self._tampering.met("a one-time code")
                return None
            if expires < self._clock():
                return None
            # The person's row is read from the database, and locked until the token is made: a disabling that comes
            # meanwhile waits for it, and one that came before is seen. (The memory could be five seconds late.)
            found = conn.execute(f"{_SELECT} WHERE i.id = %s FOR SHARE OF i", (identity_id,)).fetchone()
            identity = _identity(found) if found is not None else None
            if identity is None or identity.disabled or identity.kind != "person":
                return None
            token, key = self._new_secret(conn, identity.id, TOKEN_PREFIX, label, computer=True)
        assert key is not None
        return identity, token, key

    # --- finding them ----------------------------------------------------------------------------

    def by_secret(self, secret: str) -> Identity | None:
        """An enabled identity's token or API key: its identity, its use noted (at most once a minute, in the notes'
        thread: ``Notes``)."""
        if not secret.startswith((TOKEN_PREFIX, KEY_PREFIX)):
            return None
        digest = self._hash(_SECRET, secret)
        query = f"{_OF_TOKEN} WHERE t.hash = %s AND NOT i.disabled"
        identity = self._cached(f"secret:{digest}", lambda: self._owner(query, digest))
        if identity is not None and self.notes.due(f"token:{digest}"):
            self.notes.note("UPDATE tokens SET last_used = %s WHERE hash = %s", self._clock(), digest)
        return identity

    def by_route(self, route: str) -> Identity | None:
        """An enabled identity's address."""
        if not is_route_key(route):
            return None
        digest = self._hash(_ROUTE, route)
        query = f"{_OF_TOKEN} WHERE t.route = %s AND NOT i.disabled"
        return self._cached(f"route:{digest}", lambda: self._owner(query, digest))

    def get(self, identity_id: str) -> Identity | None:
        return self._cached(f"id:{identity_id}", lambda: self._one(f"{_SELECT} WHERE i.id = %s", identity_id))

    def current(self, identity_id: str) -> Identity | None:
        """The identity as PostgreSQL has it now, read past this instance's memory (which may be a few seconds late, and
        keeps nothing of this read): what a space is built for. None when there is no such identity."""
        return self._one(f"{_SELECT} WHERE i.id = %s", identity_id)

    def by_external(self, external_id: str) -> Identity | None:
        return self._one(f"{_SELECT} WHERE i.external_id = %s", external_id)

    def all(self, kind: str | None = None) -> list[Identity]:
        where: LiteralString = "WHERE i.kind = %s" if kind else ""
        with self.db.connection() as conn:
            rows = conn.execute(f"{_SELECT} {where} ORDER BY lower(i.name), i.id", (kind,) if kind else ()).fetchall()
        return [_identity(row) for row in rows]

    def _holds(self, token_id: str, identity_id: str, hashed: str, route: str, tag: str) -> bool:
        """Whether a token's row is one this server's key made: its tag holds."""
        return _same(tag, self._hash(_TOKEN_TAG, token_id, identity_id, hashed, route))

    def secret_digest(self, secret: str) -> str:
        """The keyed hash a token or an API key is kept by (its row's ``hash``): what a dashboard's sign-in code is
        bound to (``holds_token``), and nothing anyone without the credentials' key can make."""
        return self._hash(_SECRET, secret)

    def holds_token(self, identity_id: str, digest: str) -> bool:
        """Whether the token kept by ``digest`` (``secret_digest``) is ``identity_id``'s still, as PostgreSQL has it now
        (read past this instance's memory) and its tag holds: revoked meanwhile, on any instance, it is not."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id, identity_id, hash, route, tag FROM tokens WHERE hash = %s", (digest,)
            ).fetchone()
        if row is None or row[1] != identity_id:
            return False
        if not self._holds(*row):
            self._tampering.met("a token")
            return False
        return True

    def computer_key(self, identity_id: str, secret: str, key: str) -> Literal["held", "wrong", "keyless", "gone"]:
        """Whether ``key`` is the computer's key of ``identity_id``'s token ``secret``, as PostgreSQL has its row now
        (read past this instance's memory, which may hold a token a few seconds past its revocation): ``held`` when it
        is, compared by its keyed hash in constant time; ``wrong`` when it is not, or is not given (``""``); ``keyless``
        for a token made before computer keys, whose computer connects again to get one; ``gone`` when the token is not
        there, is another identity's, or its row's tag does not hold."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id, identity_id, hash, route, tag, computer_key FROM tokens WHERE hash = %s",
                (self._hash(_SECRET, secret),),
            ).fetchone()
        if row is None or row[1] != identity_id:
            return "gone"
        if not self._holds(*row[:5]):
            self._tampering.met("a token")
            return "gone"
        if row[5] is None:
            return "keyless"
        return "held" if _same(row[5], self._hash(_COMPUTER, row[0], key)) else "wrong"

    def tokens(self, identity_id: str) -> list[TokenInfo]:
        """The identity's tokens, each flagged when its tag does not hold (listed, never hidden: it finds no one)."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, label, created, last_used, hash, route, tag FROM tokens WHERE identity_id = %s"
                " ORDER BY created, id",
                (identity_id,),
            ).fetchall()
        return [
            TokenInfo(id_, label, created, used, tampered=not self._holds(id_, identity_id, hashed, route, tag))
            for id_, label, created, used, hashed, route, tag in rows
        ]

    def token_counts(self) -> dict[str, tuple[int, int]]:
        """How many tokens each identity has (a person's computers, an API key's one key), and how many of them are
        rows whose tag does not hold (counted and flagged, never hidden)."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT identity_id, id, hash, route, tag FROM tokens").fetchall()
        counts: dict[str, tuple[int, int]] = {}
        for identity_id, token_id, hashed, route, tag in rows:
            every, tampered = counts.get(identity_id, (0, 0))
            counts[identity_id] = (every + 1, tampered + (not self._holds(token_id, identity_id, hashed, route, tag)))
        return counts

    def enabled_count(self) -> int:
        """People and API keys enabled: the license's users (E1)."""
        with self.db.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM identities WHERE NOT disabled").fetchone()
        return int(row[0]) if row is not None else 0

    # --- changing them ---------------------------------------------------------------------------

    def update(
        self, identity_id: str, *, name: str | None = None, email: str | None = None, within: Changes | None = None
    ) -> tuple[Identity | None, list[str]]:
        """Change an identity's name, its email, or both (None leaves one as it is; an empty email takes it away; a
        name that is blank once cleaned is left as it was). The identity as it is now (None when there is no such
        identity), and the fields this change changed (``email``, ``name``): never one another change made at the same
        moment. The instances are told only when a row changed. Raises EmailTaken, nothing changed, when the identity is
        an enabled person and another enabled person has the new email (``_held_email``)."""
        new_name = clean(name, NAME_LENGTH) if name is not None else ""
        fields: list[str] = []
        with self._within(within) as changes:
            conn = changes.conn
            if email is not None:
                new_email = clean(email, EMAIL_LENGTH) or None
                current = self._locked(conn, identity_id) if new_email is not None else None
                if new_email is not None and current is not None and current.kind == "person" and not current.disabled:
                    self._held_email(conn, new_email, other_than=identity_id)
                if conn.execute(
                    "UPDATE identities SET email = %s WHERE id = %s AND email IS DISTINCT FROM %s",
                    (new_email, identity_id, new_email),
                ).rowcount:
                    fields.append("email")
            if (
                new_name
                and conn.execute(
                    "UPDATE identities SET name = %s WHERE id = %s AND name <> %s", (new_name, identity_id, new_name)
                ).rowcount
            ):
                fields.append("name")
            if fields:
                changes.told.append((identity_id, "updated"))
            row = conn.execute(f"{_SELECT} WHERE i.id = %s", (identity_id,)).fetchone()
        return (_identity(row) if row is not None else None), fields

    def _disabled(self, identity_id: str, disabled: bool, within: Changes | None) -> bool:
        with self._within(within) as changes:
            if not disabled:  # a person enabled again: their address may have been given to someone since
                current = self._locked(changes.conn, identity_id)
                if current is not None and current.kind == "person" and current.disabled and current.email:
                    self._held_email(changes.conn, current.email, other_than=identity_id)
            done = changes.conn.execute(
                "UPDATE identities SET disabled = %s WHERE id = %s AND disabled <> %s",
                (disabled, identity_id, disabled),
            ).rowcount
            if done:
                changes.told.append((identity_id, "disabled" if disabled else "enabled"))
        return bool(done)

    def disable(self, identity_id: str, *, within: Changes | None = None) -> bool:
        """True when it disabled the identity; False when there is no such identity or it was disabled already (nothing
        changed, and nothing is told to the other instances)."""
        return self._disabled(identity_id, True, within)

    def enable(self, identity_id: str, *, within: Changes | None = None) -> bool:
        """True when it enabled the identity; False when there is no such identity or it was enabled already (nothing
        changed, and nothing is told to the other instances). Raises EmailTaken, nothing changed, when another enabled
        person has the person's email now (``_held_email``)."""
        return self._disabled(identity_id, False, within)

    def remove(self, identity_id: str, *, within: Changes | None = None) -> bool:
        """The identity and its space: its secrets, settings, vault, history and changes (the database cascades)."""
        with self._within(within) as changes:
            done = changes.conn.execute("DELETE FROM identities WHERE id = %s", (identity_id,)).rowcount
            if done:
                changes.told.append((identity_id, "removed"))
        return bool(done)

    def revoke(self, identity_id: str, token_id: str | None = None, *, within: Changes | None = None) -> int:
        with self._within(within) as changes:
            if token_id is None:
                count = changes.conn.execute("DELETE FROM tokens WHERE identity_id = %s", (identity_id,)).rowcount
            else:
                count = changes.conn.execute(
                    "DELETE FROM tokens WHERE identity_id = %s AND id = %s", (identity_id, token_id)
                ).rowcount
            if count:
                changes.told.append((identity_id, "revoked"))
        return count

    def revoke_secret(self, secret: str, *, within: Changes | None = None) -> str | None:
        """A computer forgets its own token (`latet disconnect`): the id of the identity it was of, None when it was
        none."""
        found = self.revoked_secret(secret, within=within)
        return found[0] if found is not None else None

    def revoked_secret(self, secret: str, *, within: Changes | None = None) -> tuple[str, str] | None:
        """``revoke_secret``, answering the id of the identity the token was of and the token's own id (what the
        administration log records of it); None when it was none."""
        with self._within(within) as changes:
            row = changes.conn.execute(
                "DELETE FROM tokens WHERE hash = %s RETURNING identity_id, id", (self._hash(_SECRET, secret),)
            ).fetchone()
            if row is not None:
                changes.told.append((row[0], "revoked"))
        return (str(row[0]), str(row[1])) if row is not None else None

    def token_id(self, secret: str, *, within: Changes) -> str | None:
        """The id of the token ``secret`` is, as the transaction of ``within`` has it (one just made there: what the
        administration log records of it); None for no token."""
        row = within.conn.execute("SELECT id FROM tokens WHERE hash = %s", (self._hash(_SECRET, secret),)).fetchone()
        return str(row[0]) if row is not None else None

    def seen(self, identity_id: str) -> Future[None] | None:
        """The identity was here: written at most once a minute by an instance, in the notes' thread (``Notes``), never
        the caller's. The write's future; None when none was due, or the notes waiting are at their bound."""
        if not self.notes.due(f"seen:{identity_id}"):
            return None
        return self.notes.note("UPDATE identities SET last_seen = %s WHERE id = %s", self._clock(), identity_id)


@dataclass(frozen=True)
class AdminTokenInfo:
    id: str
    label: str
    created: float
    last_used: float | None
    expires: float | None  # None: never
    # Its tag does not hold: its row was made or changed without this server's key, and opens nothing. Listed and
    # flagged so (``AdminTokens.listed``), never hidden, and revoked by its id as any other.
    tampered: bool = False
    # What issued it: the id of the token that did, ``server`` for `latet server init`; None for one issued before this
    # was kept. Not in its tag (the module's words).
    issued_by: str | None = None

    def expired(self, now: float) -> bool:
        return self.expires is not None and self.expires <= now


class AdminTokens:
    """The server's administration tokens (`latet server init` makes the first): kept by a keyed hash, each with a
    label, when it expires (``default_days`` after it was issued, unless issued for good), what issued it, and a tag
    over its id, its hash and when it expires, so that a row written or changed by hand finds no one. ``key``: the
    credentials' key, 32 bytes (on a server ``MasterKey.derive(b"credentials")``), the one the identities have.

    A change the administration makes is made with a token, and commits only while that token is there and within its
    day: its transaction reads the token's row first (``held``), which stays locked until it ends, so that a revocation,
    on any instance, waits for the change or finds it made, never the other way round. A token issues tokens that end
    no later than itself (``issued``'s ``not_after``). What issued a token is kept beside it (``issued_by``) and not in
    its tag, like its label: whoever writes the database can change both, and the administration log's entry of the
    issue is what holds, while the log keeps it.

    A row is listed and revoked by an id of the server's shape (``listed_id``): its own, or, for a row written by hand
    with an id of another shape (any length, any character), one made from it under the credentials' key, so that the
    administration answers and records none of its writer's text, and still removes the row (``row_named``)."""

    def __init__(
        self, db: Database, *, key: bytes, clock: Callable[[], float] = time.time, default_days: int = 90
    ) -> None:
        self.db, self._clock, self.default_days = db, clock, default_days
        self._key = check_key(key)
        self._tampering = _Tampering(clock)
        self.notes = Notes(db, clock)  # last_used, written in a thread of its own

    def _hash(self, label: str, *parts: str) -> str:
        return keyed_hash(self._key, label, *parts)

    def _info(self, row: Any, *, flagged: bool = False) -> AdminTokenInfo | None:
        """The token a row (``_ADMIN_COLUMNS``) says, when its tag holds; a row whose tag does not hold is refused, said
        in the log, and is None, or is the token flagged (``tampered``) when ``flagged``, as a list shows it. Its id is
        the one it is listed by (``listed_id``); its label and what issued it, which its tag does not cover, are read as
        the store keeps a label, whoever wrote them."""
        token_id, label, created, last_used, expires, hashed, tag, issued_by = row
        tampered = not _same(tag, self._hash(_ADMIN_TAG, token_id, hashed, _when(expires)))
        if tampered and not flagged:
            self._tampering.met("an administration token")
            return None
        issuer = clean(issued_by, LABEL_LENGTH) if issued_by is not None else None
        listed = self.listed_id(token_id)
        return AdminTokenInfo(listed, clean(label, LABEL_LENGTH), created, last_used, expires, tampered, issuer)

    def listed_id(self, token_id: str) -> str:
        """The id a row with the id ``token_id`` is listed and revoked by: its own, when it has the server's shape
        (every row the server makes); else one of that shape made from it under the credentials' key (a row written by
        hand), the same at every reading, which its writer cannot choose to be another row's."""
        if ADMIN_TOKEN_ID.fullmatch(token_id):
            return token_id
        digest = bytes.fromhex(self._hash(_ADMIN_LISTED, token_id))
        return "a_" + base64.b32encode(digest[:7]).decode("ascii").lower()[:10]

    def row_named(self, token_id: str, *, conn: Any) -> str | None:
        """The id of the row the administration names by ``token_id``, an id of the server's shape as ``listed_id``
        gives it, in the transaction of ``conn``: the row of that id, else the row written by hand whose listed id it
        is; None for none. Nothing is locked here (``held`` locks it)."""
        if conn.execute("SELECT 1 FROM admin_tokens WHERE id = %s", (token_id,)).fetchone() is not None:
            return token_id
        for (other,) in conn.execute("SELECT id FROM admin_tokens").fetchall():
            if not ADMIN_TOKEN_ID.fullmatch(other) and self.listed_id(other) == token_id:
                return other
        return None

    def issued(
        self,
        label: str,
        *,
        days: int = 0,
        never: bool = False,
        by: str | None = None,
        not_after: float | None = None,
        conn: Any = None,
    ) -> tuple[str, str, float | None]:
        """A token, its secret shown once: its id, its secret and when it ends (None: never). It lasts ``days`` (0:
        the server's default) or, with ``never``, for good, a flag of its own, never a ``days`` of None. ``by``: what
        issues it (an administration token's id, ``server`` for `latet server init`), kept beside it. ``not_after``:
        when the token that issues it ends, past which none is made (``OutlivesIssuer``, raised for ``days`` that end
        later and for ``never``), but the server's default, which is cut there. Written in the transaction of ``conn``
        when given (whose caller writes there what records it, last), else in one of its own. Raises ValueError for
        ``days`` and ``never`` both."""
        if never and days:
            raise ValueError("a token lasts some days or for good, not both")
        secret, now = new_secret(ADMIN_PREFIX), self._clock()
        expires = None if never else now + (days or self.default_days) * 86400
        if not_after is not None:
            if expires is None or (days and expires > not_after):
                raise OutlivesIssuer(not_after)
            expires = min(expires, not_after)
        token_id, hashed = new_id("a_"), self._hash(_ADMIN, secret)
        row = (
            token_id, hashed, clean(label, LABEL_LENGTH) or "an administrator", now, expires,
            self._hash(_ADMIN_TAG, token_id, hashed, _when(expires)), by,
        )  # fmt: skip
        if conn is not None:
            conn.execute(_ADMIN_ISSUED, row)
        else:
            with self.db.connection() as own:
                own.execute(_ADMIN_ISSUED, row)
        return token_id, secret, expires

    def issue(self, label: str, *, days: int | None = 0) -> str:
        """A token, shown once: it lasts ``days`` (0: the server's default; None: for good)."""
        return self.issued(label, days=days or 0, never=days is None)[1]

    def held(self, token_id: str, *, conn: Any, update: bool = False) -> AdminTokenInfo | None:
        """The token ``token_id`` as the transaction of ``conn`` finds it, its row locked until that transaction ends:
        ``FOR KEY SHARE``, so that its revocation, on any instance, waits for the transaction (a note of its last use
        does not); ``FOR UPDATE`` with ``update``, a revocation's own rows, which two revocations take in the order of
        their ids. None when it is not there; a row whose tag does not hold is answered flagged (``tampered``): a
        revocation finds it to remove, and ``ended`` refuses it as the token of a call."""
        row = conn.execute(_ADMIN_HELD_FOR_UPDATE if update else _ADMIN_HELD, (token_id,)).fetchone()
        return self._info(row, flagged=True) if row is not None else None

    def ended(self, info: AdminTokenInfo | None) -> str | None:
        """Why ``info``, the token a call was made with as its change's transaction found it (``held``), makes no
        change now: ``revoked`` when it is not there or its tag does not hold (said in the log, as any row refused so),
        ``expired`` when its day is past; None when it serves."""
        if info is None:
            return "revoked"
        if info.tampered:
            self._tampering.met("an administration token")
            return "revoked"
        if info.expired(self._clock()):
            return "expired"
        return None

    def lookup(self, secret: str) -> AdminTokenInfo | None:
        """The token a secret is, expired or not; None for a secret that is no token, and for a row written or changed
        by hand (its tag does not hold)."""
        if not secret.startswith(ADMIN_PREFIX):
            return None
        with self.db.connection() as conn:
            row = conn.execute(_ADMIN_BY_HASH, (self._hash(_ADMIN, secret),)).fetchone()
        return self._info(row) if row is not None else None

    def check(self, secret: str) -> str | None:
        """The token's id, when it is one and has not expired; None otherwise (its use noted, at most once a minute)."""
        return self.checked(secret)[0]

    def checked(self, secret: str) -> tuple[str | None, bool]:
        """``check``'s answer, and whether ``secret`` is a token past its day, from the one lookup: what a refusal says
        (the administration's ``reason: expired``) with no second read of the database. Its use is noted at most once a
        minute, in the notes' thread (``Notes``)."""
        found, now = self.lookup(secret), self._clock()
        if found is None:
            return None, False
        if found.expired(now):
            return None, True
        if (found.last_used is None or now - found.last_used >= SEEN_EVERY) and self.notes.due(found.id):
            self.notes.note("UPDATE admin_tokens SET last_used = %s WHERE id = %s", now, found.id)
        return found.id, False

    def revoke(self, token_id: str, *, conn: Any = None) -> bool:
        """Whether the token ``token_id`` was there and is gone now, a row whose tag does not hold too: in the
        transaction of ``conn`` when given (whose caller writes there what records it, last), else in one of its
        own."""
        if conn is not None:
            return conn.execute("DELETE FROM admin_tokens WHERE id = %s", (token_id,)).rowcount > 0
        with self.db.connection() as own:
            return own.execute("DELETE FROM admin_tokens WHERE id = %s", (token_id,)).rowcount > 0

    def listed(self) -> list[AdminTokenInfo]:
        """Every row, oldest first, as the administration lists them: one whose tag does not hold flagged
        (``tampered``), never hidden, so that an administrator sees what the database holds and can revoke it."""
        with self.db.connection() as conn:
            rows = conn.execute(_ADMIN_EVERY).fetchall()
        return [info for row in rows if (info := self._info(row, flagged=True)) is not None]

    # Last: a method named `list` hides the builtin in the annotations written after it.
    def list(self, *, conn: Any = None) -> list[AdminTokenInfo]:
        """Every token, oldest first: id, label, when made, last used, when it expires (never a secret). A row written
        or changed by hand (its tag does not hold) opens nothing and is not listed: it says what its writer chose, and
        no such row passes for an issued token (`latet server init`'s first one, T12.4). ``listed`` shows them.
        ``conn``: read in the caller's transaction."""
        if conn is not None:
            rows = conn.execute(_ADMIN_EVERY).fetchall()
        else:
            with self.db.connection() as own:
                rows = own.execute(_ADMIN_EVERY).fetchall()
        return [info for info in map(self._info, rows) if info is not None]
