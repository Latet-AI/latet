"""A Latet server's administration log, in PostgreSQL: when, who (an identity, ``admin:<token id>``, or ``server``),
what (``<area>.<verb>``: person.added, key.rotated, token.revoked, shared.changed, server.started...), on what, from
where, and the action's details, which never hold a value (a setting's name, never what it masks): names, ids, counts
and flags, and never a float (PostgreSQL's JSONB writes some floats back as other numbers, which would read as a
change). A zero byte or a lone surrogate in a text, which PostgreSQL refuses or the hash cannot encode, is kept as
U+FFFD, so that no text a caller gives can make an action go unrecorded.

It is a hash chain, and the hash is keyed. Each entry carries the hash of the one before it and its own, an
HMAC-SHA256 under the log's key (on a server ``MasterKey.derive(b"audit")``) of the one before and of the entry's
fields; entries are written one at a time. Pruning keeps the hash of the last entry it drops as the anchor the rest
verifies from, with a tag over the anchor's id and hash made under the same key. ``verify`` walks the chain from its
anchor. Someone who writes the table without the key can make neither the hash of an entry nor the tag of an anchor,
so he cannot change an entry, add one, take one out of the middle or the start, or make a prune of his own, and
leave the chain as it was: ``verify`` finds the first entry that does not follow. A prune drops only a run it has
checked: from the anchor (or the first entry), in id order, each entry old, following the one before and holding, up to
the first that is not old. It reads the anchor and the entries as its own (``FOR UPDATE``), so that a writer of the
table, who takes no lock of the chain's, changes neither until the prune is done, and it drops the run by its entries'
ids, so that a row written meanwhile under an id below the run's end stays to be found; an anchor written meanwhile
where there was none is not written over. A time or an id changed by hand cannot steer it into dropping what it did
not check; when the run does not hold it drops nothing, and says so. An entry dated far ahead of the instance's clock
(``AHEAD``: an instance whose clock ran ahead wrote it) is not old until that time, and holds back every entry after
it: the prune says so, and waits for it.

The chain rests on two things of the session, which the database layer sets for every connection and checks at start:
READ COMMITTED (the last hash is read after the lock is waited for, and a stricter transaction reads from before the
wait: two entries would follow one) and all the digits of a time (what is read back must be what was hashed).
``record`` and ``prune`` also say READ COMMITTED themselves. An entry can be written in the transaction of the change it
records (``record(conn=...)``), and then both are kept, or neither is.

What the chain cannot see. Entries taken off its end, the table emptied together with its anchor, and the table put
back as it was some time ago all leave a chain that holds: only a copy kept elsewhere shows them, and
``ChainCheck.head``, the id and hash of the newest entry, is what such a copy (E4's export) is compared with. Whoever
has the key, an instance or the master key it derives from, can write the whole log again. A prune keeps a hash and
not the entries: a change made to an entry before it was pruned is no longer there to be found. The key derives from
the current version of the master key: a rotation of the master key (not in these plans) must write the chain again
under the new key, or every entry reads as changed."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, LiteralString

from psycopg import Connection
from psycopg.types.json import Jsonb

from latet.log_paging import ID_MOST, LOG_MOST, LOG_PAGE_BYTES, LogCursor, LogFilters
from latet.server.db import Database
from latet.server.tokens import check_key, keyed_hash

log = logging.getLogger(__name__)

PAGE = 10_000  # entries ``verify`` reads, and ``prune`` drops, at a time
MAX_QUERY = 100_000  # entries ``query`` answers at most (E4 reads windows of up to 64,000)
# Seconds an entry may be dated ahead of a pruning instance's clock before the prune says that it is held back by it:
# far more than instances' clocks may differ (the bus drops the messages of those a minute apart).
AHEAD = 86_400.0
_ENTRY, _ANCHOR = "audit-entry", "audit-anchor"  # what a keyed hash is of (the label of ``keyed_hash``)
_ENTRIES: Final = "SELECT id, ts, actor, action, target, details::text, address, prev, hash FROM audit"
# The anchor a prune writes: over the one it read (which it holds), or, when it read none, only where none is still.
_NEXT_ANCHOR: Final = "UPDATE audit_anchor SET through = %s, hash = %s, tag = %s WHERE id = 1"
_FIRST_ANCHOR: Final = "INSERT INTO audit_anchor (id, through, hash, tag) VALUES (1, %s, %s, %s) ON CONFLICT DO NOTHING"
# What PostgreSQL's TEXT and JSONB refuse (a zero byte) and what a hash cannot encode (a lone surrogate).
_UNSTORABLE = re.compile("[" + re.escape(chr(0)) + re.escape(chr(0xD800)) + "-" + re.escape(chr(0xDFFF)) + "]")
_BACKSLASH = chr(0x5C)
_LIKE = str.maketrans({_BACKSLASH: _BACKSLASH * 2, "%": _BACKSLASH + "%", "_": _BACKSLASH + "_"})
# HTTP pages use their own reader; E4's query and all chain operations keep their existing id ordering.
_page_clock = time.monotonic
_PAGE_SECONDS = 60.0
_HTTP_ENTRIES: Final = (
    "SELECT id, ts, fits, CASE WHEN fits THEN actor END, CASE WHEN fits THEN action END,"
    " CASE WHEN fits THEN target END, CASE WHEN fits THEN details::text END,"
    " CASE WHEN fits THEN address END, CASE WHEN fits THEN prev END, CASE WHEN fits THEN hash END"
    " FROM (SELECT *, (octet_length(actor)::bigint + octet_length(action) + coalesce(octet_length(target), 0)"
    " + octet_length(details::text) + coalesce(octet_length(address), 0) + octet_length(prev) + octet_length(hash))"
    " <= %s AS fits FROM audit"
)


class LogReadRefused(Exception):
    """Fixed, content-free HTTP log-read refusal; never changes the row or advances across it."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason, self.message = reason, message
        super().__init__(message)


def _too_large() -> LogReadRefused:
    return LogReadRefused("log_entry_too_large", "an entry is past the log-read bound; retrieval is incomplete")


def _invalid_entry() -> LogReadRefused:
    return LogReadRefused("log_entry_invalid", "an entry cannot be read safely; retrieval is incomplete")


def _json_parts(value: Any, depth: int = 0) -> Iterator[bytes]:
    """The JSONResponse encoding policy, in bounded string fragments, with a fixed nesting bound."""
    if depth > 64:
        raise _invalid_entry()
    if isinstance(value, str):
        yield b'"'
        for start in range(0, len(value), 4096):
            yield json.dumps(value[start : start + 4096], ensure_ascii=False)[1:-1].encode("utf-8")
        yield b'"'
    elif value is None or isinstance(value, bool | int | float):
        yield json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise _invalid_entry()
            if index:
                yield b","
            yield from _json_parts(key, depth + 1)
            yield b":"
            yield from _json_parts(item, depth + 1)
        yield b"}"
    elif isinstance(value, list):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_parts(item, depth + 1)
        yield b"]"
    else:
        raise _invalid_entry()


def _encoded_entry(row: tuple[Any, ...], check: Callable[[], float]) -> bytes:
    if not row[2]:
        raise _too_large()
    if type(row[0]) is not int or not 1 <= row[0] <= ID_MOST or not math.isfinite(row[1]):
        raise _invalid_entry()
    details = _object(row[6])
    if details is None:
        raise _invalid_entry()
    value = dict(
        zip(
            ("id", "ts", "actor", "action", "target", "details", "address", "prev", "hash"),
            (row[0], row[1], row[3], row[4], row[5], details, row[7], row[8], row[9]),
            strict=True,
        )
    )
    encoded = bytearray()
    try:
        for part in _json_parts(value):
            check()
            if len(encoded) + len(part) > LOG_PAGE_BYTES:
                raise _too_large()
            encoded.extend(part)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise _invalid_entry() from None
    return bytes(encoded)


def _page_tail(cursor: str | None) -> bytes:
    return b'],"next_cursor":' + json.dumps(cursor, separators=(",", ":")).encode("ascii") + b"}"


def _selection(filters: LogFilters, ceiling: int, after: LogCursor | None) -> tuple[LiteralString, list[Any]]:
    where: list[LiteralString] = ["id <= %s"]
    args: list[Any] = [ceiling]
    if filters.since is not None:
        where.append("ts >= %s")
        args.append(filters.since)
    if filters.until is not None:
        where.append("ts < %s")
        args.append(filters.until)
    if filters.action:
        where.append("action LIKE %s" if filters.action.endswith("*") else "action = %s")
        args.append(
            _text(filters.action[:-1]).translate(_LIKE) + "%" if filters.action.endswith("*") else _text(filters.action)
        )
    if filters.actor:
        where.append("actor = %s")
        args.append(_text(filters.actor))
    if after is not None:
        where.append("(ts, id) < (%s, %s)")
        args.extend((after.ts, after.id))
    return " WHERE " + " AND ".join(where), args


class _AnchorWritten(Exception):
    """A prune met an anchor that was not there when it read the chain: written by hand meanwhile."""


def _chain_lock(schema: str) -> int:
    """The advisory lock of a schema's chain: one entry at a time, as the migrations take their own lock."""
    return int.from_bytes(hashlib.sha256(f"latet-audit-chain:{schema}".encode()).digest()[:8], "big", signed=True)


def _text(text: str) -> str:
    """``text`` as the log keeps it: a zero byte and a lone surrogate are U+FFFD, and nothing else changes (the log says
    what it was told)."""
    return _UNSTORABLE.sub(chr(0xFFFD), text)


def _plain(value: Any) -> Any:
    """``value`` of the details as the log keeps it: texts cleaned (a mapping's names too), a tuple as a list, counts
    and flags as they are. Raises TypeError, in words that quote nothing, for a float (see the module) and for what
    JSON has no name for."""
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        raise TypeError(
            "a float in the details: they are names, ids, counts and flags (JSONB may write a float back as another "
            "number)"
        )
    if isinstance(value, Mapping):
        kept: dict[str, Any] = {}
        for name, item in value.items():
            if not isinstance(name, str):
                raise TypeError("a name in the details is a text")
            kept[_text(name)] = _plain(item)
        return kept
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    raise TypeError(f"a {type(value).__name__} in the details: they are names, ids, counts and flags")


def _details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    kept = _plain({} if details is None else details)
    if not isinstance(kept, dict):
        raise TypeError("the details are a mapping of names to values")
    return kept


def _object(text: str) -> dict[str, Any] | None:
    """The details a row says, read from their JSON text; None when they are no object, or when Python cannot read
    them (a writer of the table can put there a number of thousands of digits, or a nesting deeper than its
    recursion): that is a row that does not hold, and not an error that ends a walk or a read of the log."""
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _same(found: str, expected: str) -> bool:
    """Whether a hash or tag read from the database is the one expected, in constant time (bytes, so that a text with
    any character in it, which a writer of the table may have put there, is a mismatch and not an error)."""
    return hmac.compare_digest(found.encode("utf-8", "surrogatepass"), expected.encode())


def entry_hash(
    key: bytes,
    prev: str,
    ts: float,
    actor: str,
    action: str,
    target: str | None,
    details: Mapping[str, Any],
    address: str | None,
) -> str:
    """The hash of an entry: an HMAC under ``key`` of the hash of the one before it and of its fields, canonical (the
    time as a float, the details with their names sorted, no space, so that what PostgreSQL gives back is hashed as
    what was written)."""
    fields = json.dumps(
        [float(ts), actor, action, target, dict(details), address],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return keyed_hash(key, _ENTRY, prev, fields)


def anchor_tag(key: bytes, through: int, digest: str) -> str:
    """The tag of an anchor: an HMAC under ``key``, with a label of its own, of the id and hash of the last entry
    pruned. It is never an entry's hash, and a writer of the table who copies a hash into an anchor cannot make its
    tag."""
    return keyed_hash(key, _ANCHOR, str(through), digest)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: float
    actor: str
    action: str
    target: str | None
    details: dict[str, Any]
    address: str | None
    prev: str = ""  # the hash of the entry before (the anchor's for the oldest kept; "" for the very first)
    hash: str = ""


@dataclass(frozen=True)
class ChainCheck:
    """What ``AuditLog.verify`` found: whether every entry follows the one before, how many it checked, the id of the
    first that does not (when the anchor is what does not hold, the id it stands for), and ``head``: the id and hash of
    the newest entry (the anchor's once every entry was pruned, None for a log that never held one), which a copy kept
    elsewhere is compared with, since nothing in the table shows entries taken off its end."""

    ok: bool
    checked: int
    broken_at: int | None = None
    head: tuple[int, str] | None = None


class AuditLog:
    """``key``: the log's key, 32 bytes (on a server ``MasterKey.derive(b"audit")``), under which every entry's hash and
    every anchor's tag are made and checked."""

    def __init__(self, db: Database, *, key: bytes, clock: Callable[[], float] = time.time) -> None:
        self.db, self._clock = db, clock
        self._key = check_key(key)
        self._lock = _chain_lock(db.schema)

    @staticmethod
    def _last(conn: Any) -> str:
        """The hash the next entry follows: the newest entry's, else the anchor's, else nothing. What is there is
        believed: the log goes on, and ``verify`` is what finds a row that does not hold."""
        row = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            return str(row[0])
        anchor = conn.execute("SELECT hash FROM audit_anchor WHERE id = 1").fetchone()
        return str(anchor[0]) if anchor is not None else ""

    def _holds(self, row: tuple[Any, ...]) -> bool:
        """Whether a row of ``_ENTRIES`` is the entry its hash says: the hash is the one the key makes of the row. A row
        whose details cannot be read is not (see ``_object``)."""
        details = _object(row[5])
        return details is not None and _same(
            row[8], entry_hash(self._key, row[7], row[1], row[2], row[3], row[4], details, row[6])
        )

    def _write(
        self,
        conn: Connection[Any],
        ts: float,
        actor: str,
        action: str,
        target: str | None,
        details: dict[str, Any],
        address: str | None,
    ) -> None:
        """The entry, in the transaction of ``conn``. READ COMMITTED is asked for first: the last hash is read after the
        lock is waited for, and a stricter transaction reads from the snapshot it took before the wait, so that two
        entries would follow one. The connections of the server are READ COMMITTED already (db.py), and this does not
        rest on it. A transaction that is stricter and has read is not joined: PostgreSQL refuses to change it."""
        conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock,))  # one entry at a time: the chain
        prev = self._last(conn)
        conn.execute(
            "INSERT INTO audit (ts, actor, action, target, details, address, prev, hash)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ts,
                actor,
                action,
                target,
                Jsonb(details),
                address,
                prev,
                entry_hash(self._key, prev, ts, actor, action, target, details, address),
            ),
        )

    def record(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        details: Mapping[str, Any] | None = None,
        address: str | None = None,
        *,
        conn: Connection[Any] | None = None,
    ) -> None:
        """Keep an action, chained to the one before. Raises TypeError for a float or what JSON has no name for in
        ``details``; no text makes it raise.

        Given ``conn``, a connection in the middle of a transaction (not in autocommit mode: the lock of the chain would
        end with its statement), the entry is written there and kept or lost with that transaction, so that a change and
        the entry that records it are never apart. The lock of the chain is taken there and held until the transaction
        ends: write the entry last, after the change's own locks, so that nothing is waited for while it is held. The
        transaction is made READ COMMITTED if it has not read yet; one that is stricter and has read is refused, with
        the database's own error, and not joined."""
        ts = float(self._clock())
        actor, action = _text(actor), _text(action)
        target = None if target is None else _text(target)
        address = None if address is None else _text(address)
        kept = _details(details)
        if conn is None:
            with self.db.transaction() as own:
                self._write(own, ts, actor, action, target, kept, address)
        elif conn.autocommit:
            raise ValueError("an entry is written in a transaction: this connection is in autocommit mode")
        else:
            self._write(conn, ts, actor, action, target, kept, address)

    def query(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 500,
    ) -> list[AuditEntry]:
        """The entries of ``[since, until)``, newest first, at most ``limit``. ``action`` ending in `*` matches what
        starts with the rest (`person.*`), its characters taken literally."""
        where: list[LiteralString] = []
        args: list[Any] = []
        if since is not None:
            where.append("ts >= %s")
            args.append(since)
        if until is not None:
            where.append("ts < %s")  # a window's end is the next one's start: an entry is in one of them
            args.append(until)
        if action:
            if action.endswith("*"):
                where.append("action LIKE %s")
                args.append(_text(action[:-1]).translate(_LIKE) + "%")
            else:
                where.append("action = %s")
                args.append(_text(action))
        if actor:
            where.append("actor = %s")
            args.append(_text(actor))
        query: LiteralString = _ENTRIES
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id DESC LIMIT %s"
        args.append(max(1, min(limit, MAX_QUERY)))  # E4 asks again with a larger limit when it gets as many as it asked
        with self.db.connection() as conn:
            rows = conn.execute(query, args).fetchall()
        return [AuditEntry(r[0], r[1], r[2], r[3], r[4], _object(r[5]) or {}, r[6], r[7], r[8]) for r in rows]

    def read_page(self, filters: LogFilters, cursor: LogCursor | None, limit: int) -> tuple[bytes, int]:
        """One bounded HTTP page in time/id order, with no database state retained between requests.

        The committed insertion ceiling excludes later writes, including this read's audit entry. Retention and
        privileged edits can still change rows between requests. This does not verify or change the hash chain.
        """
        if not 1 <= limit <= LOG_MOST:
            raise ValueError("invalid log page limit")
        end = _page_clock() + _PAGE_SECONDS

        def left() -> float:
            remaining = end - _page_clock()
            if remaining <= 0:
                raise TimeoutError("the log page exceeded its work budget")
            return remaining

        chunks: list[bytes] = []
        size, count = len(b'{"entries":['), 0
        last: LogCursor | None = None
        more = False
        with self.db.connection(deadline=end, clock=_page_clock) as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

            def budget() -> None:
                conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(left() * 1000))),))

            budget()
            if cursor is not None:
                ceiling = cursor.ceiling_id
            else:
                maximum = conn.execute("SELECT coalesce(max(id), 0) FROM audit").fetchone()
                assert maximum is not None
                ceiling = maximum[0]
            where, args = _selection(filters, ceiling, cursor)

            def exists(after: LogCursor) -> bool:
                tail_where, tail_args = _selection(filters, ceiling, after)
                budget()
                query: LiteralString = "SELECT 1 FROM audit"
                query += tail_where + " LIMIT 1"
                return conn.execute(query, tail_args).fetchone() is not None

            budget()
            query = _HTTP_ENTRIES + where + " ORDER BY ts DESC, id DESC LIMIT %s) AS bounded ORDER BY ts DESC, id DESC"
            with conn.cursor(name="latet_log_page", scrollable=False, withhold=False) as rows:
                rows.execute(query, [LOG_PAGE_BYTES, *args, limit])
                while count < limit:
                    budget()
                    row = rows.fetchone()
                    left()
                    if row is None:
                        break
                    try:
                        encoded = _encoded_entry(row, left)
                    except LogReadRefused:
                        if not count:
                            raise
                        more = True
                        break
                    boundary = LogCursor(row[1], row[0], ceiling, filters.digest())
                    del row
                    tail = _page_tail(boundary.encode())
                    cost = len(encoded) + bool(count)
                    if size + cost + len(tail) > LOG_PAGE_BYTES:
                        # Near the exact boundary, reserve null only when exhaustion has actually been established.
                        if size + cost + len(_page_tail(None)) <= LOG_PAGE_BYTES and not exists(boundary):
                            tail = _page_tail(None)
                        else:
                            if not count:
                                raise _too_large()
                            more = True
                            del encoded
                            break
                    if count:
                        chunks.append(b",")
                    chunks.append(encoded)
                    size += cost
                    count += 1
                    last = boundary
                else:
                    assert last is not None
                    more = exists(last)
            left()
            continuation = last.encode() if more and last is not None else None
            body = b"".join([b'{"entries":[', *chunks, _page_tail(continuation)])
            left()
            if len(body) > LOG_PAGE_BYTES:
                raise _too_large()
            return body, count

    def _page(self, conn: Any, after: int | None) -> list[tuple[Any, ...]]:
        """The next ``PAGE`` entries by id: from the first, or those after the id ``after``."""
        if after is None:
            return conn.execute(f"{_ENTRIES} ORDER BY id LIMIT %s", (PAGE,)).fetchall()
        return conn.execute(f"{_ENTRIES} WHERE id > %s ORDER BY id LIMIT %s", (after, PAGE)).fetchall()

    @staticmethod
    def _head(conn: Any, anchor: tuple[Any, ...] | None) -> tuple[int, str] | None:
        """The id and hash of the newest entry; of the anchor when no entry is left; None when there is neither."""
        newest = conn.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        if newest is not None:
            return (newest[0], newest[1])
        return (anchor[0], anchor[1]) if anchor is not None else None

    def verify(self) -> ChainCheck:
        """Walk the chain from its anchor, a page at a time: the anchor's tag holds, and every entry follows the one
        before and is the entry its hash says. The log is read as it is when the walk begins (a prune or an entry that
        commits meanwhile is not seen), so that a walk never finds a chain broken where it is whole."""
        with self.db.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            anchor = conn.execute("SELECT through, hash, tag FROM audit_anchor WHERE id = 1").fetchone()
            if anchor is not None and not _same(anchor[2], anchor_tag(self._key, anchor[0], anchor[1])):
                # An anchor that does not hold says nothing, not even where the log ends.
                return ChainCheck(False, 0, anchor[0], self._head(conn, None))
            head = self._head(conn, anchor)
            prev, checked, after = (anchor[1] if anchor is not None else ""), 0, None
            while True:
                rows = self._page(conn, after)
                for row in rows:
                    if row[7] != prev or not self._holds(row):
                        return ChainCheck(False, checked, row[0], head)
                    prev, checked = row[8], checked + 1
                if len(rows) < PAGE:
                    return ChainCheck(True, checked, None, head)
                after = rows[-1][0]

    def prune(self, days: int) -> int:
        """Drop the entries older than ``days``, keeping the hash of the last one dropped as the chain's anchor, with
        its tag: how many. Only a run it has checked is dropped, ``PAGE`` entries at most in a transaction (the lock of
        the chain is not held for long): from the anchor, or the first entry, in id order, each entry older than
        ``days``, following the one before and holding, up to the first that is not old. An entry younger than that is
        never dropped, and one that is old and comes after it waits for it (instances whose clocks differ write times
        out of order); one dated more than ``AHEAD`` ahead of this instance's clock is said in the log, since it holds
        the rest back until that time. When the anchor or an entry of the run does not hold, nothing more is dropped
        and the log says so: a time or an id changed by hand cannot steer the prune into dropping what it did not
        check, nor into writing an anchor of its own over what was changed."""
        now = self._clock()
        cutoff = now - days * 86400
        total = 0
        while True:
            dropped, more = self._prune_run(cutoff, now)
            total += dropped
            if not more:
                return total

    def _prune_run(self, cutoff: float, now: float) -> tuple[int, bool]:
        """One transaction of ``prune``: how many entries it dropped, and whether a run as long as a page was dropped
        (another may follow). The anchor and the page of entries are read as this transaction's own (``FOR UPDATE``):
        a writer of the table, who takes no lock of the chain's, changes neither before the run is dropped. The run
        goes by its entries' ids, never by a range a row written meanwhile could fall in; the anchor is written where
        the one read was, and when none was read one written meanwhile is not written over: nothing is dropped."""
        try:
            with self.db.transaction() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")  # see ``_write``
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock,))
                anchor = conn.execute("SELECT through, hash, tag FROM audit_anchor WHERE id = 1 FOR UPDATE").fetchone()
                if anchor is not None and not _same(anchor[2], anchor_tag(self._key, anchor[0], anchor[1])):
                    return self._refused()
                prev = anchor[1] if anchor is not None else ""
                run: list[tuple[Any, ...]] = []
                for row in conn.execute(f"{_ENTRIES} ORDER BY id LIMIT %s FOR UPDATE", (PAGE,)).fetchall():
                    if row[1] >= cutoff:  # the first that is not old ends the run
                        if row[1] > now + AHEAD:
                            self._held_back()
                        break
                    if row[7] != prev or not self._holds(row):
                        return self._refused()
                    prev = row[8]
                    run.append(row)
                if not run:
                    return 0, False
                last = run[-1]
                removed = conn.execute("DELETE FROM audit WHERE id = ANY(%s)", ([row[0] for row in run],)).rowcount
                written = conn.execute(
                    _NEXT_ANCHOR if anchor is not None else _FIRST_ANCHOR,
                    (last[0], last[8], anchor_tag(self._key, last[0], last[8])),
                ).rowcount
                if not written:  # an anchor where none was read: written by hand meanwhile, and no start of this run's
                    raise _AnchorWritten
                return removed, len(run) == PAGE
        except _AnchorWritten:  # raised inside the transaction, so that nothing of the run is dropped
            return self._refused()

    @staticmethod
    def _held_back() -> None:
        """An entry dated ahead of this instance's clock ends the run: said in words that hold nothing of it."""
        log.warning(
            "the administration log keeps entries past its retention: one is dated more than a day ahead of this "
            "instance's clock (an instance whose clock ran ahead wrote it), and the prune waits for it, and for every "
            "entry after it, until that time"
        )

    @staticmethod
    def _refused() -> tuple[int, bool]:
        """Nothing is dropped, and the log says so in words that hold nothing of any row (a writer of the table could
        have filled a row with anything)."""
        log.warning(
            "the administration log was not pruned: the run of entries it would drop does not hold from its anchor (an "
            "entry or the anchor was changed, or written without this server's key)"
        )
        return 0, False
