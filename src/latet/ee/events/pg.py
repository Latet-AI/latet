"""The event log and the SIEM queue in the server's PostgreSQL, which every instance writes and reads: the tables of
E4's migrations (`latet/ee/migrations/`), in the server's schema."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, LiteralString

from psycopg import sql
from psycopg.errors import DataError

from latet.ee.events.schema import checked
from latet.ee.events.store import (
    ADMIN_DROPPED,
    BATCH_DAYS,
    DAY,
    DIMENSIONS,
    GAP,
    NEVER_DROPPED,
    SUMS,
    Holder,
    Noting,
    QueueState,
    Skipped,
    UnreadableQueue,
    _marker,
    audit_rows,
    bounded,
    counted,
    dimensions,
    holder,
    holders,
    hours,
    is_gap,
    queued,
    ranked,
    retention,
    rows_of,
    skipped_head,
    span_of,
    to_collapse,
)

if TYPE_CHECKING:
    import psycopg

    from latet.server.db import Database

# Each dimension's column ("group" is a word of SQL's).
_COLUMNS = {
    "hour": "hour", "type": "type", "identity": "identity_id", "kind": "kind", "group": "group_id", "agent": "agent",
    "project": "project", "code": "code", "decider": "decider",
}  # fmt: skip
_COUNTED_COLUMNS = _COLUMNS | {"category": "category"}
# ``sums``' arithmetic (``MemoryEvents.sums``'): the events are their own rows (category ''), the values
# the kinds' rows; with a kind of value chosen (``category = %s`` among the conditions), its rows alone.
_EVERY_KIND = sql.SQL(
    "COALESCE(SUM(events) FILTER (WHERE category = ''), 0) AS n_exchanges,"
    " COALESCE(SUM(events) FILTER (WHERE category = '' AND type = 'stopped'), 0) AS n_stopped,"
    " COALESCE(SUM(masked) FILTER (WHERE category <> ''), 0) AS n_masked"
)
_ONE_KIND = sql.SQL(
    "COALESCE(SUM(events), 0) AS n_exchanges,"
    " COALESCE(SUM(events) FILTER (WHERE type = 'stopped'), 0) AS n_stopped,"
    " COALESCE(SUM(masked), 0) AS n_masked"
)
_KEYS = sql.SQL("COUNT(*) OVER () AS n_keys")  # the keys there were, before LIMIT
# ... but the empty key (every dimension '': no group, no project, no one), which names nothing (``store.ranked``).
_NAMED_KEYS = sql.SQL("COUNT(*) FILTER (WHERE NOT ({empty})) OVER () AS n_keys")
_ADD = (
    "INSERT INTO ee_events (hour, type, identity_id, kind, group_id, agent, project, code, decider, category, events,"
    " masked) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (hour, type, identity_id, kind, group_id, agent, project, code, decider, category)"
    " DO UPDATE SET events = ee_events.events + excluded.events, masked = ee_events.masked + excluded.masked"
)
# A write asks for it first, as the administration log's does: it reads the projects kept once their locks are held,
# and a stricter transaction would read them as they were before it waited (db.py's connections are READ COMMITTED
# already; this does not rest on it).
_READ_COMMITTED = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
# The locks of the stripes a write's holders are in, in the order given (an array's, which unnest keeps).
_LOCK = "SELECT pg_advisory_xact_lock(k) FROM unnest(%s::bigint[]) AS k"
# The projects kept of each holder in its hour (ee_event_projects): by its whole key, never the hour's counts.
_KNOWN = (
    "SELECT p.hour, p.identity_id, p.group_id, p.project FROM ee_event_projects p"
    " JOIN unnest(%s::bigint[], %s::text[], %s::text[]) AS h (hour, identity_id, group_id)"
    " ON p.hour = h.hour AND p.identity_id = h.identity_id AND p.group_id = h.group_id"
)
_KEEP = (
    "INSERT INTO ee_event_projects (hour, identity_id, group_id, project) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT DO NOTHING"
)
# The first hour from an hour on that either table holds: prune goes from it, a day of hours a transaction.
_NEXT = (
    "SELECT LEAST((SELECT min(hour) FROM ee_events WHERE hour >= %s),"
    " (SELECT min(hour) FROM ee_event_projects WHERE hour >= %s))"
)
_BEFORE_ALL = -(2**63)  # an hour, or a place in a queue, before any: BIGINT's least
STRIPES = 64  # the locks of a schema's holders: a write takes as many at most, whatever its batch names
PRUNE_HOURS = 24  # hours a transaction of prune forgets: each short, however much the log holds


EACH = 10_000  # rows a fetch of ``each``'s cursor
_PROJECT = DIMENSIONS.index("project")

_PUT = "INSERT INTO ee_siem_queue (output, at, type, event, ref) VALUES (%s, %s, %s, %s, %s)"
_TAKE = "DELETE FROM ee_siem_queue WHERE output = %s AND seq = ANY(%s)"
# Rows delivered, each only while it holds the text it held when it was read (a collapse or a skip may have made a gap
# marker of it meanwhile, in its place): by its primary key, a few hundred rows at most, the text compared as text, so
# that a row whatever its JSON holds (an id that is no text, a number jsonb refuses) is known (Task 7's fix round 2).
_TAKE_SENT = (
    "DELETE FROM ee_siem_queue q USING unnest(%s::bigint[], %s::text[]) AS d (seq, event)"
    " WHERE q.output = %s AND q.seq = d.seq AND q.event = d.event"
)
# A batch refused for good: never an event of the administration log in it (I5: one refused holds the queue).
_DROP = "DELETE FROM ee_siem_queue WHERE output = %s AND seq = ANY(%s) AND type <> 'admin'"
# The lock of one output's queue (``_queue_lock``), the first statement of a collapse and of a skip: two instances'
# never collapse one queue past its bound, nor skip its head while it collapses.
_LOCK_OUTPUT = "SELECT pg_advisory_xact_lock(%s)"
_OK = (
    "INSERT INTO ee_siem_state (output, last_ok) VALUES (%s, %s) ON CONFLICT (output) DO UPDATE SET"
    " last_ok = excluded.last_ok, last_error = NULL, failing_since = NULL, retry_at = NULL, tries = 0,"
    " blocked = false, collapsed = 0, held_to = NULL"
)
_COLLAPSED = (  # the bound's collapse (round 3): FAIL in the status until the output delivers again
    "INSERT INTO ee_siem_state (output, collapsed) VALUES (%s, %s) ON CONFLICT (output) DO UPDATE SET"
    " collapsed = excluded.collapsed"
)
# A queue kept short (I5, I13): the oldest `apps` events first, then the oldest of the others but `admin`.
_TRIM_APPS = (
    "DELETE FROM ee_siem_queue WHERE seq IN"
    " (SELECT seq FROM ee_siem_queue WHERE output = %s AND type = 'apps' ORDER BY seq LIMIT %s)"
)
_TRIM_OTHERS = (
    "DELETE FROM ee_siem_queue WHERE seq IN"
    " (SELECT seq FROM ee_siem_queue WHERE output = %s AND type NOT IN ('apps', 'admin') ORDER BY seq LIMIT %s)"
)
# What outputs removed took of the log, added up in a job's note: a note that is no count (written by hand) counts as
# 0, so that an output is always forgotten.
_ADMIN_DROPPED_ADD = (
    "INSERT INTO ee_jobs (name, at, note) VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET"
    " at = excluded.at, note = ((CASE WHEN ee_jobs.note ~ '^[0-9]{1,18}$' THEN ee_jobs.note::bigint ELSE 0 END)"
    " + excluded.note::bigint)::text"
)
_DROPPED = (
    "INSERT INTO ee_siem_state (output, dropped, last_error) VALUES (%s, %s, %s) ON CONFLICT (output) DO UPDATE SET"
    " dropped = ee_siem_state.dropped + excluded.dropped,"
    " last_error = COALESCE(excluded.last_error, ee_siem_state.last_error)"
)
_FAILED = (
    "INSERT INTO ee_siem_state (output, last_error, failing_since, retry_at, tries, blocked, held_to)"
    " VALUES (%s, %s, %s, %s, 1, %s, %s)"
    " ON CONFLICT (output) DO UPDATE SET last_error = excluded.last_error,"
    " failing_since = COALESCE(ee_siem_state.failing_since, excluded.failing_since),"
    " retry_at = excluded.retry_at, tries = ee_siem_state.tries + 1, blocked = excluded.blocked,"
    " held_to = excluded.held_to"
)
# The gap markers waiting (R4-1): read on the parsed JSON, every output, no `type` filter, so that no way of writing a
# row escapes; a row that is not JSON fails the cast, which `markers()` raises as UnreadableQueue. A queue holds
# millions of rows when receivers are gone (about 1.5 s of casts a million): it is read a page of the primary key at a
# time, MARKERS_PAGE rows each, every row of each page cast, the pages one snapshot.
MARKERS_PAGE = 500_000
_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
_PAGE_END = "SELECT max(seq) FROM (SELECT seq FROM ee_siem_queue WHERE seq > %s ORDER BY seq LIMIT %s) AS page"
_MARKERS = (
    "SELECT output, event FROM ee_siem_queue WHERE seq > %s AND seq <= %s AND (event::jsonb) ->> 'outcome' = %s"
    " ORDER BY seq"
)


def _keys(names: Sequence[str]) -> sql.Composable:
    """How ``sums`` counts the keys there were: every one but the empty key (each of its text dimensions ''). Totals
    are one key, and a key with its hour is never empty."""
    if not names or "hour" in names:
        return _KEYS
    empty = sql.SQL(" AND ").join(sql.SQL("{} = ''").format(sql.Identifier(_COUNTED_COLUMNS[name])) for name in names)
    return _NAMED_KEYS.format(empty=empty)


def _row(keys: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(keys, row[:-2], strict=True)) | {"events": int(row[-2]), "masked": int(row[-1])}


def _stripe(who: Holder) -> int:
    """The stripe of a holder's lock: the same on every instance (SHA-256, never Python's hash, which each process
    salts)."""
    hour, identity, group = who
    return int.from_bytes(hashlib.sha256(f"{hour}:{identity}:{group}".encode()).digest()[:8], "big") % STRIPES


def _lock_of(schema: str, stripe: int) -> int:
    """The advisory lock of one stripe of the holders' projects in one schema (an advisory lock is the whole
    database's, and each server, each test, has a schema of its own)."""
    text = f"latet-events-projects:{schema}:{stripe}"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big", signed=True)


def _queue_lock(schema: str, output: str) -> int:
    """The advisory lock of one output's SIEM queue in one schema: the same on every instance (SHA-256)."""
    text = f"latet-siem-queue:{schema}:{output}"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big", signed=True)


class PgEvents:
    def __init__(self, db: Database) -> None:
        self.db = db

    def write(self, batch: str, events: Sequence[Mapping[str, Any]], outputs: Sequence[str] = (), *, at: float) -> bool:
        """Count ``events`` and queue them for each of ``outputs`` in one transaction, once a batch: False, and nothing
        written, for a batch written already. Each event is checked once, before anything is written (NotAnEvent
        refuses the batch whole), and the outputs get the events as checked, never as given. The queue's rows go in
        right after the batch's id, before ``_add`` takes the stripes' locks, which are so held the shortest time."""
        events = [checked(e) for e in events]
        rows = rows_of(events)
        with self.db.transaction() as conn:
            conn.execute(_READ_COMMITTED)  # first: see _READ_COMMITTED
            fresh = conn.execute(
                "INSERT INTO ee_event_batches (id, at) VALUES (%s, %s) ON CONFLICT DO NOTHING", (batch, at)
            )
            if fresh.rowcount == 0:
                return False
            if outputs:  # with the counts, in their transaction: the outputs get what is counted, once
                with conn.cursor() as cur:
                    cur.executemany(_PUT, queued(outputs, events, at))
            self._add(conn, rows)
        return True

    def _add(self, conn: psycopg.Connection[Any], rows: Mapping[tuple[Any, ...], Sequence[int]]) -> None:
        """Add ``rows`` to the counts in ``conn``'s transaction, each project past its holder's bound as ``OTHER``,
        and keep the names newly counted in ``ee_event_projects``. The projects kept of each holder are read, by their
        key, once the locks of the holders' stripes are taken (``pg_advisory_xact_lock``, held to the commit), so that
        instances writing one identity's new projects at once keep the bound exactly. A write takes 64 locks at most,
        in the order of their keys, before it adds to any row, and adds to its rows in the order of theirs: two writes
        never wait on each other in a circle (two that add to a holder's projects hold its stripe's lock, one after
        the other)."""
        wanted = sorted(holders(rows))
        known: dict[Holder, set[str]] = {}
        if wanted:
            conn.execute(_LOCK, (sorted({_lock_of(self.db.schema, _stripe(who)) for who in wanted}),))
            found = conn.execute(_KNOWN, ([w[0] for w in wanted], [w[1] for w in wanted], [w[2] for w in wanted]))
            for hour, identity, group, project in found:
                known.setdefault((hour, identity, group), set()).add(project)
        counted = bounded(rows, known)
        named = sorted(
            {(*holder(key), key[_PROJECT]) for key in counted if key[_PROJECT]}
            - {(*who, project) for who, projects in known.items() for project in projects}
        )
        # In the order of their keys: two instances adding to the same rows at once never wait on each other in a
        # circle.
        ordered = [(*key, count, values) for key, (count, values) in sorted(counted.items())]
        with conn.cursor() as cur:
            if named:
                cur.executemany(_KEEP, named)
            cur.executemany(_ADD, ordered)

    @staticmethod
    def _conditions(
        since: float, until: float, where: Mapping[str, Collection[str]] | None
    ) -> tuple[list[sql.Composable], list[Any]] | None:
        """The rows of ``[since, until)`` (by the hour) that ``where`` keeps, as clauses and their arguments; None
        when a filter keeps nothing."""
        clauses: list[sql.Composable] = [sql.SQL("hour >= %s"), sql.SQL("hour < %s")]
        args: list[Any] = list(hours(since, until))
        for name, values in (where or {}).items():
            chosen = list(values)
            if not chosen:
                return None
            clauses.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(_COLUMNS[name])))
            args.append(chosen)
        return clauses, args

    @classmethod
    def _statement(
        cls, since: float, until: float, by: Sequence[str], where: Mapping[str, Collection[str]] | None
    ) -> tuple[sql.Composed, list[Any], list[str]] | None:
        """The query of ``query`` and ``each``, its arguments and its rows' keys; None when a filter keeps nothing."""
        names = dimensions(by, where)
        columns = [sql.Identifier(_COLUMNS[name]) for name in names] + [sql.Identifier("category")]
        found = cls._conditions(since, until, where)
        if found is None:
            return None
        clauses, args = found
        query = sql.SQL("SELECT {cols}, SUM(events), SUM(masked) FROM ee_events WHERE {where} GROUP BY {cols}").format(
            cols=sql.SQL(", ").join(columns), where=sql.SQL(" AND ").join(clauses)
        )
        return query, args, [*names, "category"]

    def sums(
        self,
        since: float,
        until: float,
        by: Sequence[str] = (),
        where: Mapping[str, Collection[str]] | None = None,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """``MemoryEvents.sums`` in one statement: the sums with ``FILTER``, ranked with ``ORDER BY`` (the keys in
        the ``C`` collation, as every instance and Python order them) and cut with ``LIMIT``, how many keys there were
        from ``COUNT(*) OVER ()``: a report reads what it shows, never every row of its period."""
        names = counted(by, where, limit)
        found = self._conditions(since, until, where)
        if found is None:
            return ranked({}, names, limit)
        clauses, args = found
        if category is not None:
            clauses.append(sql.SQL("category = %s"))
            args.append(category)
        columns = [sql.Identifier(_COUNTED_COLUMNS[name]) for name in names]
        keys = [column if name == "hour" else sql.SQL('{} COLLATE "C"').format(column) for name, column in zip(
            names, columns, strict=True)]  # fmt: skip
        query = sql.SQL("SELECT {select} FROM ee_events WHERE {where}{group} ORDER BY {order}{limit}").format(
            select=sql.SQL(", ").join([*columns, _ONE_KIND if category is not None else _EVERY_KIND, _keys(names)]),
            where=sql.SQL(" AND ").join(clauses),
            group=sql.SQL(" GROUP BY {}").format(sql.SQL(", ").join(columns)) if names else sql.SQL(""),
            order=sql.SQL(", ").join([sql.SQL("n_exchanges DESC"), sql.SQL("n_masked DESC"), *keys]),
            limit=sql.SQL(" LIMIT %s") if limit is not None else sql.SQL(""),
        )
        if limit is not None:
            args.append(limit)
        with self.db.connection() as conn:
            rows = conn.execute(query, args).fetchall()
        out = [
            dict(zip(names, row[: len(names)], strict=True))
            | dict(zip(SUMS, map(int, row[len(names) : -1]), strict=True))
            for row in rows
        ]
        return out, int(rows[0][-1]) if rows else 0

    def query(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> list[dict[str, Any]]:
        found = self._statement(since, until, by, where)
        if found is None:
            return []
        query, args, keys = found
        with self.db.connection() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row(keys, row) for row in rows]

    def each(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> Iterator[dict[str, Any]]:
        """``query``'s rows one at a time, from a cursor on the server, ``EACH`` rows a fetch (R2-8): a caller that
        stops early (an export past its most) never holds them all."""
        found = self._statement(since, until, by, where)
        if found is None:
            return
        query, args, keys = found
        with self.db.transaction() as conn, conn.cursor(name=f"ee_each_{uuid.uuid4().hex}") as cur:
            cur.itersize = EACH
            cur.execute(query, args)
            for row in cur:
                yield _row(keys, row)

    def prune(self, days: int, *, now: float) -> int:
        """Forget the counts older than ``days`` days and the batches older than a day: the rows of counts removed.
        ValueError, and nothing forgotten, for ``days`` that are not a retention (``retention``).

        A day of hours a transaction (``PRUNE_HOURS``), from the first hour held, the hours that hold nothing skipped:
        each transaction is short however much the log holds, never near the statements' bound, and a prune stopped
        half way forgets the rest the next time. Each range starts where the last ended, so prune ends whatever a
        range leaves (a late batch of an hour it passed waits for the next prune)."""
        oldest = int((now - retention(days) * DAY) // 3600)
        removed, after = 0, _BEFORE_ALL
        while True:
            with self.db.transaction() as conn:
                found = conn.execute(_NEXT, (after, after)).fetchone()
                first = None if found is None else found[0]
                if first is None or first >= oldest:
                    break
                after = min(first + PRUNE_HOURS, oldest)
                deleted = conn.execute("DELETE FROM ee_events WHERE hour >= %s AND hour < %s", (first, after))
                removed += deleted.rowcount
                conn.execute("DELETE FROM ee_event_projects WHERE hour >= %s AND hour < %s", (first, after))
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM ee_event_batches WHERE at < %s", (now - BATCH_DAYS * DAY,))
        return removed

    def kept(self, name: str, value: bytes) -> bytes:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ee_event_keys (name, value) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (name, value)
            )
            row = conn.execute("SELECT value FROM ee_event_keys WHERE name = %s", (name,)).fetchone()
        assert row is not None
        return bytes(row[0])

    def job(self, name: str) -> tuple[float | None, str | None]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT at, note FROM ee_jobs WHERE name = %s", (name,)).fetchone()
        return (row[0], row[1]) if row is not None else (None, None)

    def set_job(self, name: str, at: float, note: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ee_jobs (name, at, note) VALUES (%s, %s, %s)"
                " ON CONFLICT (name) DO UPDATE SET at = excluded.at, note = excluded.note",
                (name, at, note),
            )

    @staticmethod
    def _still(conn: psycopg.Connection[Any], name: str, expected: str | None) -> bool:
        """In ``conn``'s transaction, its first statements: whether the job's note is still ``expected``, its row made
        first when there is none and then read as this transaction's own (``FOR UPDATE``), so that two instances that
        both read no note never both write over it: the second waits for the first's commit and finds its note."""
        conn.execute(_READ_COMMITTED)  # first: the note is read after the wait for another's commit
        conn.execute("INSERT INTO ee_jobs (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        row = conn.execute("SELECT note FROM ee_jobs WHERE name = %s FOR UPDATE", (name,)).fetchone()
        return (row[0] if row is not None else None) == expected

    def swap_job(self, name: str, at: float, note: str, *, expected: str | None) -> bool:
        """As in memory: the note written only while it is still ``expected``, in one transaction (``_still``)."""
        with self.db.transaction() as conn:
            if not self._still(conn, name, expected):
                return False
            conn.execute("UPDATE ee_jobs SET at = %s, note = %s WHERE name = %s", (at, note, name))
        return True

    # --- the SIEM queue -----------------------------------------------------------------------------------------------

    def put(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        at: float,
        refs: Sequence[int | None] | None = None,
    ) -> None:
        if not outputs or not events:
            return
        rows = queued(outputs, events, at, refs)
        with self.db.transaction() as conn, conn.cursor() as cur:
            cur.executemany(_PUT, rows)

    def put_audit(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        refs: Sequence[int | None],
        job: str,
        note: str,
        expected: str | None,
        at: float,
        noted: Callable[[Any], None] | None = None,
    ) -> bool:
        """As in memory, in one transaction: the job's row is made first when there is none, then read as this
        transaction's own (``FOR UPDATE``), so that two instances that both read no note (a page that outlived its
        lease) never both queue it: the second waits for the first's commit and finds its note. ``noted`` writes the
        gap's entry of the log last, in the same transaction (R3-1), after this transaction's own locks, as a
        collapse does."""
        with self.db.transaction() as conn:
            if not self._still(conn, job, expected):
                return False  # another instance moved the note since it was read (R2-10)
            rows = audit_rows(outputs, events, refs, at)
            with conn.cursor() as cur:
                if rows:
                    cur.executemany(_PUT, rows)
                cur.execute("UPDATE ee_jobs SET at = %s, note = %s WHERE name = %s", (at, note, job))
            if noted is not None:  # R3-1: the gap's entry of the log, in the transaction that queues its marker
                noted(conn)
        return True

    def collapse(
        self,
        output: str,
        most: int,
        *,
        origin: str,
        at: float,
        keep: int | None = None,
        noted: Noting | None = None,
    ) -> tuple[int, int, int] | None:
        """As in memory: past ``most`` (one at least), the oldest `admin` rows become one marker in the place of the
        oldest until ``keep`` wait, and ``noted`` writes the log's entry naming its range in the same transaction
        (R3-1). Under the output's lock, taken first: two instances' collapses never count the same rows."""
        with self.db.transaction() as conn:
            conn.execute(_LOCK_OUTPUT, (_queue_lock(self.db.schema, output),))
            counted = conn.execute(
                "SELECT count(*) FROM ee_siem_queue WHERE output = %s AND type = 'admin'", (output,)
            ).fetchone()
            many = to_collapse(int(counted[0]) if counted is not None else 0, most, keep)
            if not many:
                return None
            rows = conn.execute(
                "SELECT seq, ref, event FROM ee_siem_queue WHERE output = %s AND type = 'admin'"
                " ORDER BY seq LIMIT %s FOR UPDATE",
                (output, many),
            ).fetchall()
            if len(rows) < 2:  # taken off meanwhile: nothing left to collapse
                return None
            spans = [span_of(json.loads(event), ref) for _, ref, event in rows]
            marker = _marker(spans, origin=origin, at=at)
            conn.execute(
                "UPDATE ee_siem_queue SET event = %s, ref = %s WHERE seq = %s", (marker, spans[0][0], rows[0][0])
            )
            conn.execute("DELETE FROM ee_siem_queue WHERE seq = ANY(%s)", ([seq for seq, _, _ in rows[1:]],))
            total = sum(span[2] for span in spans)
            conn.execute(_COLLAPSED, (output, total))
            if noted is not None:  # R3-1: the marker and its entry of the log, or neither
                noted(conn, GAP, {"output": output, "from": spans[0][0], "to": spans[-1][1], "count": total})
        return spans[0][0], spans[-1][1], total

    def skip(self, output: str, *, origin: str, at: float, conn: Any = None, held_only: bool = False) -> Skipped | None:
        """As in memory: the refused run at the head dropped and counted, and the entry the queue waits at behind a
        marker of its own (R2-12; Task 7's fix round 2), the queue no longer held; in ``conn``'s transaction when
        given, where the caller writes `siem.skipped` (R3-1), else in one of its own. Under the output's lock, taken
        first, as a collapse is; the output's state read there, its row held to the end (``held_only``: None unless
        ``blocked`` or ``held_to`` is set, Task 10's route)."""
        if conn is None:
            with self.db.transaction() as own:
                return self.skip(output, origin=origin, at=at, conn=own, held_only=held_only)
        conn.execute(_LOCK_OUTPUT, (_queue_lock(self.db.schema, output),))
        found = conn.execute(
            "SELECT held_to, blocked FROM ee_siem_state WHERE output = %s FOR UPDATE", (output,)
        ).fetchone()
        held_to = None if found is None or found[0] is None else int(found[0])
        if held_only and held_to is None and not (found is not None and found[1]):
            return None
        if held_to is None:  # nothing held but by an entry: the head alone
            rows = conn.execute(
                "SELECT seq, type, ref, event FROM ee_siem_queue WHERE output = %s ORDER BY seq LIMIT 1 FOR UPDATE",
                (output,),
            ).fetchall()
        else:  # the run the receiver refused: within the batch it was sent, a few hundred rows at most
            rows = conn.execute(
                "SELECT seq, type, ref, event FROM ee_siem_queue WHERE output = %s AND seq <= %s ORDER BY seq"
                " FOR UPDATE",
                (output, held_to),
            ).fetchall()
        run, entry = skipped_head(((int(row[0]), str(row[1])) for row in rows), held_to)
        if not run and entry is None:
            return None
        dropped = conn.execute(_DROP, (output, run)).rowcount if run else 0
        first: int | None = None
        last: int | None = None
        count = 0
        if entry is not None:
            _, _, ref, text = next(row for row in rows if int(row[0]) == entry)
            event = json.loads(text)
            span = span_of(event, ref)
            first, last, count = span[0], span[1], span[2]
            if is_gap(event):
                conn.execute(_TAKE, (output, [entry]))
            else:
                marker = _marker([span], origin=origin, at=at)
                conn.execute("UPDATE ee_siem_queue SET event = %s WHERE seq = %s", (marker, entry))
        conn.execute(
            "UPDATE ee_siem_state SET retry_at = NULL, tries = 0, blocked = false, held_to = NULL,"
            " dropped = dropped + %s WHERE output = %s",
            (dropped, output),
        )
        return Skipped(first, last, count, dropped)

    def markers(self) -> list[tuple[str, dict[str, Any]]]:
        """As in memory (R4-1): selected on the parsed JSON, across every output and with no `type` filter, so that no
        way of writing a row escapes; a row that is not JSON makes PostgreSQL's cast fail, raised as
        ``UnreadableQueue``, as is one Python cannot read. Read rarely (`latet server log verify`): a page of the
        primary key at a time (``MARKERS_PAGE`` rows, each a statement well within the statements' bound, every row of
        it cast), all of them in one snapshot (REPEATABLE READ), so that a row moved meanwhile is neither missed nor
        read twice."""
        found: list[tuple[str, dict[str, Any]]] = []
        try:
            with self.db.transaction() as conn:
                conn.execute(_SNAPSHOT)  # first: the snapshot is taken by the first page
                after = _BEFORE_ALL
                while True:
                    end = conn.execute(_PAGE_END, (after, MARKERS_PAGE)).fetchone()
                    if end is None or end[0] is None:
                        break
                    rows = conn.execute(_MARKERS, (after, end[0], GAP)).fetchall()
                    found.extend((str(output), json.loads(event)) for output, event in rows)
                    after = end[0]
        except (DataError, ValueError) as exc:
            raise UnreadableQueue("a row of a SIEM queue is not JSON") from exc
        return found

    def peek(self, output: str, n: int) -> list[tuple[int, dict[str, Any]]]:
        return [(seq, event) for seq, event, _ in self.peek_rows(output, n)]

    def peek_rows(self, output: str, n: int) -> list[tuple[int, dict[str, Any], str]]:
        """``peek``'s rows, each with its text as the queue keeps it: what ``done`` takes back to know them."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT seq, event FROM ee_siem_queue WHERE output = %s ORDER BY seq LIMIT %s", (output, n)
            ).fetchall()
        return [(int(seq), json.loads(event), str(event)) for seq, event in rows]

    def done(self, output: str, seqs: Sequence[int], *, at: float, texts: Sequence[str] | None = None) -> None:
        """Delivered: these rows leave the queue (by their places: one made visible meanwhile with a lower place
        stays), and the failure, if any, is over. With ``texts``, as in memory: a row leaves only while it holds the
        text it held when it was read."""
        with self.db.transaction() as conn:
            if texts is None:
                conn.execute(_TAKE, (output, list(seqs)))
            else:
                if len(texts) != len(seqs):
                    raise ValueError("done takes one text for each row")
                conn.execute(_TAKE_SENT, (list(seqs), list(texts), output))
            conn.execute(_OK, (output, at))

    def drop(self, output: str, seqs: Sequence[int], why: str) -> int:
        """As in memory: never an event of the administration log (I5)."""
        with self.db.transaction() as conn:
            taken = conn.execute(_DROP, (output, list(seqs))).rowcount
            conn.execute(_DROPPED, (output, taken, why))
        return taken

    def failed(
        self, output: str, why: str, *, at: float, retry_at: float, blocked: bool = False, held_to: int | None = None
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(_FAILED, (output, why, at, retry_at, blocked, held_to))

    def schedule(self, output: str) -> tuple[float | None, int]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT retry_at, tries FROM ee_siem_state WHERE output = %s", (output,)).fetchone()
        return (row[0], int(row[1])) if row is not None else (None, 0)

    def trim(self, output: str, limit: int) -> int:
        """As in memory: ``apps`` first, ``admin`` never (I5, I13)."""
        with self.db.transaction() as conn:
            counted = conn.execute("SELECT count(*) FROM ee_siem_queue WHERE output = %s", (output,)).fetchone()
            excess = (int(counted[0]) if counted is not None else 0) - limit
            taken = 0
            for trimmed in (_TRIM_APPS, _TRIM_OTHERS):
                if excess - taken <= 0:
                    break
                taken += conn.execute(trimmed, (output, excess - taken)).rowcount
            if taken:
                conn.execute(_DROPPED, (output, taken, None))
        return taken

    def state(self, output: str) -> QueueState:
        with self.db.connection() as conn:
            counted = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE type = 'admin') FROM ee_siem_queue WHERE output = %s",
                (output,),
            ).fetchone()
            row = conn.execute(
                "SELECT dropped, last_ok, last_error, failing_since, retry_at, tries, blocked, collapsed, held_to"
                " FROM ee_siem_state WHERE output = %s",
                (output,),
            ).fetchone()
        count, admin = (int(counted[0]), int(counted[1])) if counted is not None else (0, 0)
        if row is None:
            return QueueState(count, admin=admin)
        dropped, last_ok, last_error, failing_since, retry_at, tries, blocked, collapsed, held_to = row
        return QueueState(
            count, int(dropped), last_ok, last_error, failing_since, retry_at, int(tries), bool(blocked), admin,
            int(collapsed), None if held_to is None else int(held_to),
        )  # fmt: skip

    @staticmethod
    def _gone(conn: Any, where: LiteralString, args: Sequence[Any]) -> int:
        """The rows ``where`` names taken off, the administration log's among them counted apart (I5)."""
        rows = conn.execute(f"DELETE FROM ee_siem_queue WHERE {where} RETURNING type", args).fetchall()  # noqa: S608
        admin = sum(1 for (kind,) in rows if kind == NEVER_DROPPED)
        if admin:
            conn.execute(_ADMIN_DROPPED_ADD, (ADMIN_DROPPED, time.time(), str(admin)))
        return len(rows)

    def _lock_outputs(self, conn: Any, outputs: Collection[str]) -> list[str]:
        """Each output's lock, taken first in the transaction, in the order of their names: a removal takes them as a
        skip and a collapse take theirs, before any row, so that it never waits for a row while holding one they wait
        for (Task 10's review). The names, in that order."""
        names = sorted(set(outputs))
        for name in names:
            conn.execute(_LOCK_OUTPUT, (_queue_lock(self.db.schema, name),))
        return names

    def forget(self, outputs: Collection[str]) -> int:
        if not outputs:
            return 0
        with self.db.transaction() as conn:
            names = self._lock_outputs(conn, outputs)
            taken = self._gone(conn, "output = ANY(%s)", (names,))
            conn.execute("DELETE FROM ee_siem_state WHERE output = ANY(%s)", (names,))
        return taken

    def forget_unknown(self, keep: Collection[str], *, before: float) -> int:
        """What waits for outputs not in ``keep``, queued before ``before``, taken off, and their state once nothing
        waits for them: under each such output's lock, taken first (``_lock_outputs``); an output first seen after the
        locks are taken is left to the next time."""
        kept = list(keep)
        with self.db.transaction() as conn:
            found = conn.execute(
                "SELECT output FROM ee_siem_queue WHERE output <> ALL(%s) AND at < %s"
                " UNION SELECT output FROM ee_siem_state WHERE output <> ALL(%s)",
                (kept, before, kept),
            ).fetchall()
            names = self._lock_outputs(conn, [str(row[0]) for row in found])
            if not names:
                return 0
            taken = self._gone(conn, "output = ANY(%s) AND at < %s", (names, before))
            conn.execute(
                "DELETE FROM ee_siem_state s WHERE s.output = ANY(%s)"
                " AND NOT EXISTS (SELECT 1 FROM ee_siem_queue q WHERE q.output = s.output)",
                (names,),
            )
        return taken

    def admin_waiting(self, output: str) -> int:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM ee_siem_queue WHERE output = %s AND type = 'admin'", (output,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def admin_dropped(self) -> int:
        _, note = self.job(ADMIN_DROPPED)
        return int(note) if note and note.isascii() and note.isdigit() else 0

    def counts(self) -> dict[str, int]:
        """What waits for each output, in one read (the metrics' gauge reads it every 30 seconds: M12)."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT output, count(*) FROM ee_siem_queue GROUP BY output").fetchall()
        return {str(output): int(n) for output, n in rows}
