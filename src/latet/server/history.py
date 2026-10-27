"""An identity's history in PostgreSQL (``storage.exchanges.ExchangeStore`` on a computer, its public methods the
same): exchanges, the blocks they carried, the images as sent, the local reviewer's records.

What can hold a value or a text is compressed, then sealed with the identity's root key (``Sealer``, which frames it
with the key's version), the summaries too (a project's folder name is in them), each for its own row and part: an
exchange's parts for that exchange, each as itself, a block as a block, an image as an image, a review's record as
one. Beside them, in clear, only what the history's queries need: times, agents, flags, routes and counts, and the
identity's keyed hashes of the conversations' keys and of the projects' ids (a project's id is an unkeyed hash of its
folder, which a list of usual folders would give back); a filter looks for the keyed hash of what it is given. And the
keyed hashes of the vault's entries each row names (``entity_keys``: a block's replacements, an exchange's answer and
sources, a reviewer's record's findings), so that the values of an API key forgotten by themselves take the rows that
name them with them, found through an index (``forget_entities``: Task 11b, Ruling 11b-R1). A person's history, whose
values are never forgotten so, keeps none (an empty list): nothing that tells a reader of the database which of a
person's rows name the same entry (the final review's M6, Ruling F1).

A history has a limit in bytes (``ServerSettings.history_limit_mb``, a gibibyte unless set), counted from what it keeps
(``history_bytes``: each row's sealed values and 256 bytes for the rest of it, added and taken off in the transaction
that keeps or drops the row). An exchange recorded past the limit makes room: the oldest of what is kept goes, by when
it was kept (an exchange with the blocks and images it brought, a block or an image that a newer exchange carried again
staying with that one, a block or an image keeping the latest time any instance carried it at, whatever its clock),
never the exchange just recorded nor anything as new, down to nine tenths of the limit, so that room is not made again
at every exchange. A count past the limit with nothing to cut has drifted from what is kept (rows taken out some other
way than through the store): it is counted again, under its own row lock. The log says each of these, and a failure to
make room, at most once a minute for each identity, naming it alone.

Who calls: never the event loop (``latet.state.HistoryStore``). The recorder's thread and the dashboard's workers use
one store together, and zstandard's compressors are not for two threads at once: each seal and each opening makes its
own, which costs microseconds."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, Final, LiteralString

import zstandard
from cryptography.exceptions import InvalidTag

from latet.engine.modules import module_of
from latet.logline import Paced
from latet.server.sealing import DERIVE, Sealer, derived
from latet.storage.exchanges import ExchangeRecord, block_ref, new_exchange_id, ref_parts

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.db import Database

log = logging.getLogger(__name__)
HISTORY_LIMIT = 1 << 30  # bytes one identity's history may keep (ServerSettings.history_limit_mb: a gibibyte)
SAY_EVERY = 60.0  # seconds between two lines of the log about a history that made room
_ROOM = 0.9  # what a history that makes room keeps at most, of its limit: room is not made again at every exchange
_LEVEL = 6  # zstandard's level, as on a computer
SUMMARIES_AT_ONCE = 1_000  # summaries read and opened at a time by ``summaries_parts``: what one part holds
ROWS_AT_ONCE = 500  # rows kept before their entries were given theirs at a time (``index_entities``): blocks are large
# What a row keeps, in bytes, as the limit counts it: its sealed values, and 256 bytes for its ids, hashes, times and
# index entries. The same expression when a row is kept and when it goes, so that the count follows the rows.
_EXCHANGE_BYTES: Final = (
    "octet_length(summary) + COALESCE(octet_length(refs), 0) + COALESCE(octet_length(request_headers), 0)"
    " + COALESCE(octet_length(response), 0) + COALESCE(octet_length(leaks), 0) + COALESCE(octet_length(images), 0)"
    " + 256"
)
_DATA_BYTES: Final = "octet_length(data) + 256"  # a block's, an image's
_REVIEW_BYTES: Final = "octet_length(summary) + 256"
# The statements that keep a row, and answer its bytes (none when it was there already). Each, as `_CUT` below, is
# built from this module's constants alone.
_BLOCK: Final = (
    "INSERT INTO blocks (identity_id, id, kind, data, last_seen, entity_keys)"  # noqa: S608
    " VALUES (%s, %s, %s, %s, %s, %s::bytea[]) ON CONFLICT DO NOTHING RETURNING " + _DATA_BYTES
)
_IMAGE: Final = (
    "INSERT INTO images (identity_id, id, data, last_seen) VALUES (%s, %s, %s, %s)"  # noqa: S608
    " ON CONFLICT DO NOTHING RETURNING " + _DATA_BYTES
)
_EXCHANGE: Final = (
    "INSERT INTO exchanges (identity_id, id, ts, recorded, session, agent, flags, upstream, new_masked,"  # noqa: S608
    " project, summary, refs, request_headers, response, leaks, body_bytes, images, entity_keys)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::bytea[]) ON CONFLICT DO NOTHING"
    " RETURNING " + _EXCHANGE_BYTES
)
_REVIEW: Final = (
    "INSERT INTO review_records (identity_id, id, ts, session, project, block, kind, inline, summary,"  # noqa: S608
    " entity_keys) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::bytea[]) RETURNING " + _REVIEW_BYTES
)
# The bytes the history keeps, changed last in a transaction that keeps or drops rows: its row stays locked until that
# transaction ends, and the transaction waits for nothing after it.
_COUNT: Final = (
    "INSERT INTO history_bytes (identity_id, bytes) VALUES (%s, %s)"
    " ON CONFLICT (identity_id) DO UPDATE SET bytes = history_bytes.bytes + EXCLUDED.bytes RETURNING bytes"
)
# The history's tables, each with what a row of it keeps in bytes; a prune drops from each by a condition of its own,
# given in this order: what is past the retention (an exchange by when its request began, a block or an image by when
# an exchange last carried it, a review by its time), what is older than a cut made to make room (by when it was kept,
# never as new as the exchange just recorded), or everything.
_TABLES: Final[tuple[tuple[LiteralString, LiteralString], ...]] = (
    ("exchanges", _EXCHANGE_BYTES),
    ("blocks", _DATA_BYTES),
    ("images", _DATA_BYTES),
    ("review_records", _REVIEW_BYTES),
)
_RETAINED: Final[tuple[LiteralString, ...]] = (
    "ts < %(cut)s",
    "last_seen < %(cut)s",
    "last_seen < %(cut)s",
    "ts < %(cut)s",
)
_OLDEST: Final[tuple[LiteralString, ...]] = (
    "recorded <= %(cut)s AND recorded < %(newest)s",
    "last_seen <= %(cut)s AND last_seen < %(newest)s",
    "last_seen <= %(cut)s AND last_seen < %(newest)s",
    "ts <= %(cut)s AND ts < %(newest)s",
)
_EVERYTHING: Final[tuple[LiteralString, ...]] = ("TRUE", "TRUE", "TRUE", "TRUE")
# What names an entry forgotten (``forget_entities``): an image names none (its text is a block of its own).
_NAMING: Final[tuple[LiteralString, ...]] = (
    "entity_keys && %(keys)s::bytea[]",
    "entity_keys && %(keys)s::bytea[]",
    "FALSE",
    "entity_keys && %(keys)s::bytea[]",
)
# The rows kept before their entries were kept beside them (migration 010: NULL), each table's sealed parts that name
# entries with each row's id, read in the order of their ids from where the part before ended (``index_entities``); and
# a part's rows given theirs in one statement: each row's keyed hashes joined in one string (32 bytes each, a keyed
# hash's length), cut back into an array there. The condition keeps a row given its own meanwhile as it is.
_UNNAMED: Final[tuple[tuple[LiteralString, LiteralString, tuple[str, ...]], ...]] = (
    ("blocks", "data", ("block:{id}",)),
    ("exchanges", "refs, response", ("{id}:refs", "{id}:response")),
    ("review_records", "summary", ("{id}:review",)),
)
_UNNAMED_ROWS: Final = (
    "SELECT id, {columns} FROM {table} WHERE identity_id = %s AND entity_keys IS NULL AND id > %s ORDER BY id LIMIT %s"
)
_NAMED: Final = (
    "UPDATE {table} AS r SET entity_keys = ARRAY("
    "SELECT substring(given.joined FROM 1 + 32 * n FOR 32) FROM generate_series(0, length(given.joined) / 32 - 1) AS n"
    " ORDER BY n) FROM unnest(%s::text[], %s::bytea[]) AS given (id, joined)"
    " WHERE r.identity_id = %s AND r.id = given.id AND r.entity_keys IS NULL"
)
_DROP: Final = (
    "WITH gone AS (DELETE FROM {table} WHERE identity_id = %(identity)s AND {when} RETURNING {size} AS size)"
    " SELECT COUNT(*), COALESCE(SUM(size), 0) FROM gone"
)
# Where room is made: the newest time at which what the history keeps, counted from the newest, passes ``room`` bytes.
# What was kept at that time or before goes.
_CUT: Final = (
    "SELECT at FROM (SELECT at, SUM(size) OVER (ORDER BY at DESC ROWS UNBOUNDED PRECEDING) AS kept FROM ("  # noqa: S608
    "SELECT recorded AS at, " + _EXCHANGE_BYTES + " AS size FROM exchanges WHERE identity_id = %(identity)s"
    " UNION ALL SELECT last_seen, " + _DATA_BYTES + " FROM blocks WHERE identity_id = %(identity)s"
    " UNION ALL SELECT last_seen, " + _DATA_BYTES + " FROM images WHERE identity_id = %(identity)s"
    " UNION ALL SELECT ts, " + _REVIEW_BYTES + " FROM review_records WHERE identity_id = %(identity)s"
    ") AS stored) AS newest_first WHERE kept > %(room)s ORDER BY at DESC LIMIT 1"
)
# What the history keeps, in bytes, counted from its rows: what its count is made again of when it drifted.
_KEPT: Final = (
    "SELECT (SELECT COALESCE(SUM(" + _EXCHANGE_BYTES + "), 0) FROM exchanges WHERE identity_id = %(identity)s)"  # noqa: S608
    " + (SELECT COALESCE(SUM(" + _DATA_BYTES + "), 0) FROM blocks WHERE identity_id = %(identity)s)"
    " + (SELECT COALESCE(SUM(" + _DATA_BYTES + "), 0) FROM images WHERE identity_id = %(identity)s)"
    " + (SELECT COALESCE(SUM(" + _REVIEW_BYTES + "), 0) FROM review_records WHERE identity_id = %(identity)s)"
)
_SUMMARIES: Final = "SELECT id, summary FROM exchanges WHERE "  # a page of them: ``list`` adds its conditions
# Each conversation with its counts, the latest first, and the newest of its summaries: the table keeps its key's keyed
# hash alone, and the key is read in a summary.
_SESSIONS: Final = (
    "SELECT g.agent, g.first_ts, g.last_ts, g.exchanges, g.masked, g.blocked, e.id, e.summary FROM ("
    "SELECT session, MAX(agent) AS agent, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS exchanges,"
    " COALESCE(SUM(new_masked), 0) AS masked, SUM(CASE WHEN flags LIKE %(blocked)s THEN 1 ELSE 0 END) AS blocked"
    " FROM exchanges WHERE identity_id = %(identity)s GROUP BY session ORDER BY MAX(ts) DESC LIMIT %(limit)s) AS g"
    " CROSS JOIN LATERAL (SELECT id, summary FROM exchanges WHERE identity_id = %(identity)s AND session = g.session"
    " ORDER BY seq DESC LIMIT 1) AS e ORDER BY g.last_ts DESC"
)


def _size(row: Any) -> int:
    """The bytes of a row an insert kept, by its RETURNING; none when it kept nothing (the row was there already)."""
    return 0 if row is None else int(row[0])


def _named(value: Any, out: set[str] | None = None) -> set[str]:
    """The vault's entries a part of the history names: each ``entity`` of its spans, however deep (a block's
    replacements, an exchange's references and answer, a reviewer's record's findings)."""
    out = set() if out is None else out
    if isinstance(value, dict):
        entity = value.get("entity")
        if isinstance(entity, str) and entity:
            out.add(entity)
        for item in value.values():
            _named(item, out)
    elif isinstance(value, list):
        for item in value:
            _named(item, out)
    return out


class PgHistoryStore:
    def __init__(
        self,
        db: Database,
        identity_id: str,
        root_key: bytes,
        retention_days: int = 14,
        *,
        limit: int = HISTORY_LIMIT,
        clock: Callable[[], float] = time.time,
        forgets_values: bool = True,
    ) -> None:
        """``forgets_values``: whether its identity's values are forgotten by themselves (an API key's), so that each
        row is kept with the keyed hashes of the entries it names; a person's are kept with none."""
        self.db, self.identity = db, identity_id
        self.forgets_values = forgets_values
        self._sealer = Sealer(root_key, b"exchanges")
        self._ids = derived(root_key, DERIVE, b"exchange-ids")  # what names blocks and images
        self.retention_days = retention_days
        self.limit = limit  # the bytes it may keep
        self._clock = clock  # what paces the log's lines about making room
        self._lock = threading.Lock()
        self._said: float | None = None  # when the last of those lines was written
        self._unsaid = 0  # the times room was made since
        self._failed, self._drifted = Paced(SAY_EVERY, clock=clock), Paced(SAY_EVERY, clock=clock)

    # --- sealing ----------------------------------------------------------------------------------

    def _seal(self, value: Any, aad: str) -> bytes:
        data = zstandard.ZstdCompressor(level=_LEVEL).compress(json.dumps(value, ensure_ascii=False).encode())
        return self._sealer.seal(data, f"{self.identity}:{aad}")

    def _open(self, sealed: bytes | None, aad: str) -> Any:
        if sealed is None:
            return None
        return json.loads(zstandard.ZstdDecompressor().decompress(self._sealer.open(sealed, f"{self.identity}:{aad}")))

    def _keyed(self, kind: str, text: str) -> str:
        """The identity's keyed hash of a conversation's key (``kind`` "session") or of a project's id ("project"):
        what the history keeps of them, and what a filter looks for."""
        return self._sealer.keyed(f"{kind}\x00{text}").hex()

    def block_id(self, masked: str) -> str:
        return hmac.new(self._ids, masked.encode(), hashlib.sha256).hexdigest()[:32]

    def _entity_keys(self, entity_ids: Iterable[str]) -> list[bytes]:
        """The keyed hashes of the vault's entries a row names, as ``entity_keys`` keeps them (Ruling 11b-R1): an
        entry's id is a keyed hash of its value already, kept here under a key of the history's own."""
        return sorted(self._sealer.keyed(f"entity\x00{entity_id}") for entity_id in set(entity_ids))

    def _kept_keys(self, entity_ids: Iterable[str]) -> list[bytes]:
        """What a new row is kept with of the entries it names (``_entity_keys``): none in a person's history."""
        return self._entity_keys(entity_ids) if self.forgets_values else []

    def image_id(self, data: str) -> str:
        return "im_" + hmac.new(self._ids, b"image\x00" + data.encode(), hashlib.sha256).hexdigest()[:32]

    # --- writes -----------------------------------------------------------------------------------

    def known_blocks(self, ids: Iterable[str]) -> set[str]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return set()
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM blocks WHERE identity_id = %s AND id = ANY(%s)", (self.identity, wanted)
            ).fetchall()
        return {row[0] for row in rows}

    def last_used(self) -> dict[str, float]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT upstream, MAX(ts) FROM exchanges WHERE identity_id = %s AND upstream IS NOT NULL"
                " AND upstream <> '' GROUP BY upstream",
                (self.identity,),
            ).fetchall()
        return {str(name): float(ts) for name, ts in rows}

    def block_types(self, ids: Iterable[str]) -> dict[str, list[tuple[str, str, str, int]]]:
        wanted = list(dict.fromkeys(ids))
        out: dict[str, list[tuple[str, str, str, int]]] = {}
        if not wanted:
            return out
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, data FROM blocks WHERE identity_id = %s AND id = ANY(%s)", (self.identity, wanted)
            ).fetchall()
        for block_id, data in rows:
            replacements = self._open(data, f"block:{block_id}")[1]
            counts = Counter(
                (r["type"], r.get("class", "?"), r.get("module") or module_of(r.get("rule"), r["type"]))
                for r in replacements
            )
            out[block_id] = sorted((t, c, m, n) for (t, c, m), n in counts.items())
        return out

    def record(self, rec: ExchangeRecord) -> str:
        summary = rec.summary
        exchange_id = summary.setdefault("id", new_exchange_id())
        now = time.time()
        refs = [block_ref(b) for b in rec.blocks]
        images = [{"id": i.id, "path": i.path, "state": i.state, "block": i.block} for i in rec.images]
        flags = " " + " ".join(summary.get("flags", [])) + " "
        project = (summary.get("project") or {}).get("id")
        added = 0  # the bytes of the rows this exchange adds: a block or an image kept already adds none
        with self.db.transaction() as conn:
            for b in rec.blocks:
                if b.masked is not None:
                    sealed = self._seal([b.masked, b.replacements], f"block:{b.id}")
                    named = self._kept_keys(_named(b.replacements))
                    added += _size(conn.execute(_BLOCK, (self.identity, b.id, b.kind, sealed, now, named)).fetchone())
            for image in rec.images:
                if image.id is None:
                    continue
                if image.data is not None:
                    sealed = self._seal({"media_type": image.media_type, "data": image.data}, f"image:{image.id}")
                    added += _size(conn.execute(_IMAGE, (self.identity, image.id, sealed, now)).fetchone())
                conn.execute(  # never back: an instance whose clock is behind carries it too
                    "UPDATE images SET last_seen = GREATEST(last_seen, %s) WHERE identity_id = %s AND id = %s",
                    (now, self.identity, image.id),
                )
            ids = list(dict.fromkeys(b.id for b in rec.blocks))
            if ids:
                conn.execute(
                    "UPDATE blocks SET last_seen = GREATEST(last_seen, %s) WHERE identity_id = %s AND id = ANY(%s)",
                    (now, self.identity, ids),
                )
            row = conn.execute(
                _EXCHANGE,
                (
                    self.identity, exchange_id, summary["ts"], now, self._keyed("session", summary["session"]),
                    summary.get("agent", "other"), flags, summary.get("upstream"),
                    int(summary.get("new_masked") or 0),
                    self._keyed("project", project) if project is not None else None,
                    self._seal(summary, f"{exchange_id}:summary"),
                    self._seal(refs, f"{exchange_id}:refs") if refs else None,
                    self._seal(rec.request_headers, f"{exchange_id}:headers") if rec.request_headers else None,
                    self._seal(rec.response, f"{exchange_id}:response") if rec.response is not None else None,
                    self._seal(rec.leaks, f"{exchange_id}:leaks"), rec.body_bytes,
                    self._seal(images, f"{exchange_id}:images") if images else None,
                    self._kept_keys(_named(refs) | _named(rec.response)),
                ),
            ).fetchone()  # fmt: skip
            kept = self._counted(conn, added + _size(row))
        if kept > self.limit:
            self._make_room(now)
        return exchange_id

    def record_review(self, summary: dict[str, Any]) -> str:
        review_id = f"rv_{int(time.time() * 1000):x}{os.urandom(3).hex()}"
        project = summary.get("project")
        with self.db.transaction() as conn:
            row = conn.execute(
                _REVIEW,
                (
                    self.identity, review_id, summary["ts"], self._keyed("session", summary["session"]),
                    self._keyed("project", project) if project is not None else None, summary["block"],
                    summary["kind"], bool(summary.get("inline")), self._seal(summary, f"{review_id}:review"),
                    self._kept_keys(_named(summary.get("findings"))),
                ),
            ).fetchone()  # fmt: skip
            self._counted(conn, _size(row))
        return review_id

    # --- what the history keeps, in bytes -----------------------------------------------------------

    def _counted(self, conn: Connection[Any], change: int) -> int:
        """Add ``change`` to the bytes the history keeps, last in ``conn``'s transaction: what it keeps now."""
        row = conn.execute(_COUNT, (self.identity, change)).fetchone()
        assert row is not None  # RETURNING gives the row it counted
        return int(row[0])

    def _drop(
        self, conn: Connection[Any], conditions: tuple[LiteralString, ...], args: dict[str, Any]
    ) -> tuple[int, int]:
        """Drop from each table of the history the rows that meet its condition (in ``_TABLES``' order), in ``conn``'s
        transaction: how many exchanges went, and how many bytes in all."""
        went = freed = 0
        for (table, size), when in zip(_TABLES, conditions, strict=True):
            row = conn.execute(
                _DROP.format(table=table, when=when, size=size), {"identity": self.identity, **args}
            ).fetchone()
            assert row is not None  # an aggregate gives one row
            went = int(row[0]) if table == "exchanges" else went
            freed += int(row[1])
        return went, freed

    def _make_room(self, newest: float) -> None:
        """The history keeps more than its limit: drop the oldest of it, by when it was kept, until it keeps nine
        tenths of its limit or less, never what is as new as ``newest`` (the time the exchange just recorded was kept
        at). A failure is said by its type and tried again at the next exchange: the exchange is kept all the same."""
        freed, recounted = 0, False
        try:
            with self.db.transaction() as conn:
                row = conn.execute(_CUT, {"identity": self.identity, "room": int(self.limit * _ROOM)}).fetchone()
                if row is not None:
                    _, freed = self._drop(conn, _OLDEST, {"cut": row[0], "newest": newest})
                    self._counted(conn, -freed)
                else:  # it fits: another instance made room meanwhile, or the count drifted from what is kept
                    recounted = self._recounted(conn)
        except Exception as exc:  # by its type: a driver's words can quote what it was given
            if count := self._failed.due():
                log.warning(
                    "the history of %s could not make room (%s): it is tried again at its next exchange%s",
                    self.identity,
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return
        if freed:
            self._made_room()
        if recounted and (count := self._drifted.due()):
            log.warning(
                "the history of %s was counted again from what it keeps: its count said more, rows having gone some "
                "other way than through Latet%s",
                self.identity,
                f" ({count} times since the line before)" if count > 1 else "",
            )

    def _recounted(self, conn: Connection[Any]) -> bool:
        """The count made again from the rows, under the count's own row lock, when it still says the history is past
        its limit though there is nothing to cut: whether it was. Every change of the rows that goes through this
        store ends with the count, in its transaction, so that one under way waits for this lock, and its change is
        added to what is counted here; one committed before is in what is read."""
        row = conn.execute("SELECT bytes FROM history_bytes WHERE identity_id = %s FOR UPDATE", (self.identity,))
        counted = row.fetchone()
        if counted is None or int(counted[0]) <= self.limit:  # another instance made room meanwhile
            return False
        kept = conn.execute(_KEPT, {"identity": self.identity}).fetchone()
        assert kept is not None  # an aggregate gives one row
        conn.execute("UPDATE history_bytes SET bytes = %s WHERE identity_id = %s", (int(kept[0]), self.identity))
        return True

    def _made_room(self) -> None:
        """Say that the history made room: at once, then one line a minute at most, with how many times since the
        line before. It names the identity and the limit, never a value."""
        now = self._clock()
        with self._lock:
            self._unsaid += 1
            if self._said is not None and abs(now - self._said) < SAY_EVERY:
                return
            self._said, said, self._unsaid = now, self._unsaid, 0
        log.warning(
            "the history of %s kept more than its limit (history_limit_mb, %d bytes): its oldest exchanges went to "
            "make room%s",
            self.identity,
            self.limit,
            f" ({said} times since the line before)" if said > 1 else "",
        )

    def reviews(self, limit: int = 100, session: str | None = None) -> list[dict[str, Any]]:
        query: LiteralString = "SELECT id, summary FROM review_records WHERE identity_id = %s"
        args: list[Any] = [self.identity]
        if session:
            query += " AND session = %s"
            args.append(self._keyed("session", session))
        query += " ORDER BY ts DESC LIMIT %s"
        args.append(max(1, min(limit, 1000)))
        with self.db.connection() as conn:
            rows = conn.execute(query, args).fetchall()
        return [{"id": rid, **self._open(data, f"{rid}:review")} for rid, data in rows]

    def prune(self, now: float | None = None) -> int:
        """Drop exchanges older than the retention period, and blocks no newer exchange carried."""
        cut = (now or time.time()) - self.retention_days * 86400
        with self.db.transaction() as conn:
            removed, freed = self._drop(conn, _RETAINED, {"cut": cut})
            if freed:
                self._counted(conn, -freed)
        return removed

    def clear(self) -> None:
        with self.db.transaction() as conn:
            _, freed = self._drop(conn, _EVERYTHING, {})
            if freed:
                self._counted(conn, -freed)

    # --- the entries of values forgotten (Task 11b) ---------------------------------------------------

    def forget_entities(self, conn: Connection[Any], entity_ids: Iterable[str]) -> int:
        """The rows that name one of the vault's entries ``entity_ids`` dropped, in ``conn``'s transaction: those of
        an API key's values forgotten by themselves (``PgVaultStore.forget_values``' ``gone``, Ruling 11b-R1), which
        keep their old fakes with their ids. Each found by the keyed hashes of the entries it names, through their
        index: a block whose replacements name one, an exchange whose answer or sources do (its blocks naming none
        stay), a reviewer's record whose findings do; an image names none. The count of bytes moved by what went, last.
        How many exchanges went. The rows kept before they were kept with their entries are given theirs first
        (``index_entities``, outside the transaction)."""
        keys = self._entity_keys(entity_ids)
        if not keys:
            return 0
        went, freed = self._drop(conn, _NAMING, {"keys": keys})
        if freed:
            self._counted(conn, -freed)
        return went

    def index_entities(self) -> int:
        """The rows kept before they were kept with the keyed hashes of the entries they name (migration 010, or an
        older instance's meanwhile) given theirs, each opened once, ``ROWS_AT_ONCE`` at a time, each part in a
        transaction of its own: what a forget runs before its own transaction, so that it finds every row that names a
        value forgotten (Ruling 11b-R1). One that does not open (another root key, written by hand) names none, and is
        never opened again. How many rows were given theirs; none, in a statement a table, once every row has its own.
        Blocks are the costly part: each is opened whole, its masked text with it."""
        done = 0
        for table, columns, parts in _UNNAMED:
            query = _UNNAMED_ROWS.format(table=table, columns=columns)
            after = ""
            while True:
                with self.db.transaction() as conn:
                    rows = conn.execute(query, (self.identity, after, ROWS_AT_ONCE)).fetchall()
                    ids, joined = [], []
                    for row in rows:
                        named: set[str] = set()
                        for sealed, aad in zip(row[1:], parts, strict=True):
                            opened = None if sealed is None else bytes(sealed)
                            try:
                                _named(self._open(opened, aad.format(id=row[0])), named)
                            except (InvalidTag, ValueError, zstandard.ZstdError):
                                named = set()  # does not open: names none
                                break
                        ids.append(str(row[0]))
                        joined.append(b"".join(self._entity_keys(named)))
                    if ids:
                        conn.execute(_NAMED.format(table=table), (ids, joined, self.identity))
                done += len(rows)
                if len(rows) < ROWS_AT_ONCE:
                    break
                after = str(rows[-1][0])
        return done

    # --- reads ------------------------------------------------------------------------------------

    def in_order(self, session: str) -> list[str]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM exchanges WHERE identity_id = %s AND session = %s ORDER BY seq",
                (self.identity, self._keyed("session", session)),
            ).fetchall()
        return [row[0] for row in rows]

    def detail(self, exchange_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT summary, refs, request_headers, response, leaks, body_bytes, images FROM exchanges"
                " WHERE identity_id = %s AND id = %s",
                (self.identity, exchange_id),
            ).fetchone()
            if row is None:
                return None
            summary, refs, headers, response, leaks, body_bytes, images = row
            images = self._open(images, f"{exchange_id}:images") or []
            wanted = [i["id"] for i in images if i.get("id")]
            kept = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM images WHERE identity_id = %s AND id = ANY(%s)", (self.identity, wanted)
                )
            } if wanted else set()  # fmt: skip
            refs = self._open(refs, f"{exchange_id}:refs") or []
            ids = list(dict.fromkeys(r[0] for r in refs))
            stored: dict[str, tuple[str, list[dict[str, Any]], str]] = {}
            if ids:
                for block_id, kind, data in conn.execute(
                    "SELECT id, kind, data FROM blocks WHERE identity_id = %s AND id = ANY(%s)", (self.identity, ids)
                ):
                    masked, replacements = self._open(data, f"block:{block_id}")
                    stored[block_id] = (masked, replacements, kind)
        for image in images:
            image["kept"] = image.get("id") in kept
        slots = []
        for ref in refs:
            block_id, path, from_model, new, tool, source = ref_parts(ref)
            found = stored.get(block_id)
            if found is None:
                continue  # pruned
            masked, replacements, kind = found
            slots.append(
                {"block": block_id, "path": path, "kind": kind, "from_model": from_model, "new": new,
                 "masked": masked, "replacements": replacements, "tool": tool, "source": source}
            )  # fmt: skip
        return {
            "summary": self._open(summary, f"{exchange_id}:summary"),
            "request": {
                "headers": self._open(headers, f"{exchange_id}:headers") or [],
                "slots": slots,
                "images": images,
                "body_bytes": body_bytes,
            },
            "response": self._open(response, f"{exchange_id}:response"),
            "leaks": self._open(leaks, f"{exchange_id}:leaks") or [],
        }

    def image(self, image_id: str) -> tuple[str, bytes] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT data FROM images WHERE identity_id = %s AND id = %s", (self.identity, image_id)
            ).fetchone()
        if row is None:
            return None
        found = self._open(row[0], f"image:{image_id}")
        return str(found["media_type"]), base64.b64decode(found["data"])

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                _SESSIONS, {"blocked": "% blocked %", "identity": self.identity, "limit": limit}
            ).fetchall()
        return [
            {"key": self._open(summary, f"{eid}:summary")["session"], "agent": a, "first_ts": f, "last_ts": la,
             "exchanges": n, "masked": int(m), "blocked": int(b)}
            for a, f, la, n, m, b, eid, summary in rows
        ]  # fmt: skip

    def summaries_since(self, ts: float) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, summary FROM exchanges WHERE identity_id = %s AND ts >= %s ORDER BY ts", (self.identity, ts)
            ).fetchall()
        return [self._open(data, f"{eid}:summary") for eid, data in rows]

    def summaries_parts(self, ts: float, most: int) -> Iterator[list[dict[str, Any]]]:
        """The summaries of the exchanges since ``ts``, the latest ``most`` at most, ``SUMMARIES_AT_ONCE`` at a time,
        each part read and opened before the next is read (the dashboard's stats, in a worker thread): a history of
        millions of exchanges is never held whole in the memory every identity shares, as ``summaries_since`` holds it.
        Each part is a page of its own, read after the last one's time and sequence (keyset), its connection given
        back to the pool before the part is opened and aggregated: no call holds one of the pool's connections, which
        the lookups, the vault's saves and readiness share, for its whole run."""
        after: tuple[float, int] | None = None
        left = max(0, most)
        while left:
            size = min(SUMMARIES_AT_ONCE, left)
            with self.db.connection() as conn:
                if after is None:
                    rows = conn.execute(
                        "SELECT id, ts, seq, summary FROM exchanges WHERE identity_id = %s AND ts >= %s"
                        " ORDER BY ts DESC, seq DESC LIMIT %s",
                        (self.identity, ts, size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, ts, seq, summary FROM exchanges WHERE identity_id = %s AND ts >= %s"
                        " AND (ts, seq) < (%s, %s) ORDER BY ts DESC, seq DESC LIMIT %s",
                        (self.identity, ts, *after, size),
                    ).fetchall()
            if not rows:
                return
            left -= len(rows)
            after = (float(rows[-1][1]), int(rows[-1][2]))
            yield [self._open(data, f"{eid}:summary") for eid, _, _, data in rows]
            if len(rows) < size:
                return

    def close(self) -> None:
        """Nothing: the pool is the server's."""

    # Last: a method named `list` hides the builtin in the annotations written after it.
    def list(
        self,
        limit: int = 100,
        before: str | None = None,
        session: str | None = None,
        agent: str | None = None,
        flag: str | None = None,
        project: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        where: list[LiteralString] = ["identity_id = %s"]
        args: list[Any] = [self.identity]
        with self.db.connection() as conn:
            if before:
                row = conn.execute(
                    "SELECT ts FROM exchanges WHERE identity_id = %s AND id = %s", (self.identity, before)
                ).fetchone()
                if row is not None:
                    where.append("(ts < %s OR (ts = %s AND id < %s))")
                    args += [row[0], row[0], before]
            if session:
                where.append("session = %s")
                args.append(self._keyed("session", session))
            if agent:
                where.append("agent = %s")
                args.append(agent)
            if project:
                where.append("project = %s")
                args.append(self._keyed("project", project))
            if flag:
                where.append("(flags LIKE %s OR flags LIKE %s)")
                args += [f"% {flag} %", f"% {flag}:%"]
            query: LiteralString = _SUMMARIES + " AND ".join(where) + " ORDER BY ts DESC, id DESC LIMIT %s"
            rows = conn.execute(query, [*args, limit + 1]).fetchall()
        items = [self._open(data, f"{eid}:summary") for eid, data in rows]
        next_id = items[limit - 1]["id"] if len(items) > limit else None
        return items[:limit], next_id
