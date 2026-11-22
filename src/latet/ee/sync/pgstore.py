"""Term sync's store on a Latet server: PostgreSQL through E0b's Database, with the meaning of store.py's `TermStore`.
Every term's value is sealed with the server's master key, through its sealer of the synced terms
(``server.master.sealer(SEALER_PURPOSE)``), the additional data naming the row: a value moved to another row does not
open. A value no key of this server opens (another master key sealed it, a version this server does not hold, a value
moved) is skipped and counted, never matched against. The tables are src/latet/ee/migrations/600_terms.sql's.

A change given ``conn`` runs on the caller's connection, in its transaction (a route writes it with its entry in the
administration log, and both are undone together); else in a transaction of its own. The changes of the terms take
turns on ``_ONE_WRITER``, the row of ee_terms_state, taken after the settings' lock where a route holds one (the lock
order: license, last administrator, people, settings, ee_terms_state, audit)."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Final

from cryptography.exceptions import InvalidTag
from psycopg.types.json import Jsonb

from latet.ee.sync.store import Asked, NewerKey, Run, SealedElsewhere, StoredTerm, TooMany, tally, term_id
from latet.server.sealing import version_of

if TYPE_CHECKING:
    from latet.server.db import Database
    from latet.server.sealing import Sealer

SEALER_PURPOSE: Final = b"terms"  # the master key's sealer of the synced terms: keys of their own
_TERMS: Final = "SELECT id, source, type, value, first_seen, gone FROM ee_terms"
# The gone terms (``source``'s, or every one's when it is None), a page of them in the list's order (the latest gone
# first, then by id), the first or the one past the last row read (``gone`` and ``id``); and how many there are.
_GONE: Final = _TERMS + " WHERE gone IS NOT NULL AND (%(source)s::text IS NULL OR source = %(source)s)"
_GONE_FIRST: Final = _GONE + " ORDER BY gone DESC, id LIMIT %(limit)s"
_GONE_AFTER: Final = (
    _GONE + " AND (gone < %(gone)s OR (gone = %(gone)s AND id > %(id)s)) ORDER BY gone DESC, id LIMIT %(limit)s"
)
_GONE_COUNT: Final = (
    "SELECT COUNT(*) FROM ee_terms WHERE gone IS NOT NULL AND (%(source)s::text IS NULL OR source = %(source)s)"
)
_ONE_WRITER: Final = "SELECT version FROM ee_terms_state WHERE id = 1 FOR UPDATE"  # changes of the terms in turn
_MOVED: Final = "UPDATE ee_terms_state SET version = version + 1 WHERE id = 1"
_COPY_IN: Final = "COPY ee_terms (id, source, type, value, first_seen) FROM STDIN"
_IDS_KEY: Final = "ee_terms_state:ids_key"
_ELSEWHERE: Final = "the synced terms were sealed with another master key"


class PgTermStore:
    """What term sync keeps, in the server's PostgreSQL: `TermStore`'s meaning, every value sealed with ``sealer`` for
    its row. `unreadable` counts the values the last `terms()` could not open."""

    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer
        self._unopened: frozenset[str] = frozenset()  # the rows the last `terms()` could not open (`apply` heals them)
        self.unreadable = 0

    def version(self) -> int:
        with self._db.connection() as conn:
            row = conn.execute("SELECT version FROM ee_terms_state WHERE id = 1").fetchone()
        return int(row[0]) if row is not None else 0

    def apply(
        self, source: str, terms: Sequence[tuple[str, str]], now: float, *, most: int | None = None, conn: Any = None
    ) -> tuple[int, int]:
        """As `MemoryTermStore.apply`, under ``_ONE_WRITER``: new terms added (the first spelling the run gives), those
        back in the source no longer gone, those it no longer gives marked gone; a term already kept keeps its row (its
        value and when it was first seen) whatever its spelling now. With ``most``, `TooMany` before anything is written
        when the rows kept and the new ones would pass it. A row the last `terms()` could not open that the source gives
        again is sealed again with the value given now, which its id proves (the id is keyed over the source, the type
        and the value: B-R5); one that names a newer key version than this server holds is left to the instances that
        open it. `SealedElsewhere`, nothing written, when the ids' key is another master key's."""
        if conn is None:
            with self._db.transaction() as own:
                return self.apply(source, terms, now, most=most, conn=own)
        conn.execute(_ONE_WRITER)
        key = self._ids_key(conn)
        given: dict[str, tuple[str, str]] = {}
        for value, etype in terms:
            given.setdefault(term_id(key, source, etype, value), (value, etype))  # the first spelling a run gives
        rows = conn.execute("SELECT id, gone FROM ee_terms WHERE source = %s", (source,)).fetchall()
        known: dict[str, float | None] = {str(i): gone for i, gone in rows}
        new = [i for i in given if i not in known]
        if most is not None:
            counted = conn.execute("SELECT COUNT(*) FROM ee_terms").fetchone()
            kept = (int(counted[0]) if counted is not None else 0) + len(new)
            if kept > most:
                raise TooMany(kept)
        back = [i for i, gone in known.items() if i in given and gone is not None]
        left = [i for i, gone in known.items() if i not in given and gone is None]
        healed = self._still_unopened(conn, [i for i in self._unopened if i in given and i in known])
        if new:
            with conn.cursor() as cursor, cursor.copy(_COPY_IN) as copy:
                for i in new:
                    value, etype = given[i]
                    copy.write_row((i, source, etype, self._seal(value, i), now))
        if healed:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "UPDATE ee_terms SET value = %s WHERE id = %s", [(self._seal(given[i][0], i), i) for i in healed]
                )
        if back:
            conn.execute("UPDATE ee_terms SET gone = NULL WHERE id = ANY(%s)", (back,))
        if left:
            conn.execute("UPDATE ee_terms SET gone = %s WHERE id = ANY(%s)", (now, left))
        if new or back or left or healed:
            conn.execute(_MOVED)
        return len(new), len(left)

    def retire(self, configured: Iterable[str], now: float, *, conn: Any = None) -> int:
        """The terms of the sources no longer set up become gone. How many did."""
        if conn is None:
            with self._db.transaction() as own:
                return self.retire(configured, now, conn=own)
        conn.execute(_ONE_WRITER)
        count = conn.execute(
            "UPDATE ee_terms SET gone = %s WHERE gone IS NULL AND NOT (source = ANY(%s))", (now, list(configured))
        ).rowcount
        if count > 0:
            conn.execute(_MOVED)
        return max(count, 0)

    def terms(self) -> list[StoredTerm]:
        with self._db.connection() as conn:
            rows = conn.execute(_TERMS).fetchall()
        found, unopened = self._opened(rows)
        self._unopened, self.unreadable = frozenset(unopened), len(unopened)
        return found

    def kept(self) -> int:
        """Every row kept, gone ones and those no key opens included: what `most` counts."""
        with self._db.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM ee_terms").fetchone()
        return int(row[0]) if row is not None else 0

    def gone(self, limit: int = 1000, source: str | None = None) -> tuple[list[StoredTerm], int]:
        """The terms no longer in their source (``source``'s, or every one's), the latest gone first: at most
        ``limit`` of them, and how many there are (those no key opens counted). The rows no key opens are left out
        before the limit applies: the next ones are read in their place, a page of ``limit`` rows at a time after the
        last row read (the latest gone first, then by id), until ``limit`` are opened or no row is left."""
        opened: list[StoredTerm] = []
        with self._db.connection() as conn:
            total = conn.execute(_GONE_COUNT, {"source": source}).fetchone()
            asked: dict[str, Any] = {"source": source, "limit": limit}
            while limit > 0 and len(opened) < limit:
                found = conn.execute(_GONE_AFTER if "id" in asked else _GONE_FIRST, asked).fetchall()
                page, _unopened = self._opened(found)
                opened += page[: limit - len(opened)]
                if len(found) < limit:
                    break
                asked |= {"gone": found[-1][5], "id": found[-1][0]}  # the next page starts past the last row read
        return opened, int(total[0]) if total is not None else 0

    def remove(
        self, ids: Iterable[str] | None = None, *, source: str | None = None, conn: Any = None
    ) -> dict[str, int]:
        """Forget terms no longer in their source: these, or every one of ``source``, or every one. A term still in
        its source is left alone. How many of each type went."""
        if conn is None:
            with self._db.transaction() as own:
                return self.remove(ids, source=source, conn=own)
        conn.execute(_ONE_WRITER)
        if ids is not None:
            cursor = conn.execute(
                "DELETE FROM ee_terms WHERE gone IS NOT NULL AND id = ANY(%s) RETURNING type", (list(ids),)
            )
        elif source is not None:
            cursor = conn.execute(
                "DELETE FROM ee_terms WHERE gone IS NOT NULL AND source = %s RETURNING type", (source,)
            )
        else:
            cursor = conn.execute("DELETE FROM ee_terms WHERE gone IS NOT NULL RETURNING type")
        removed = Counter(str(etype) for (etype,) in cursor.fetchall())
        if removed:
            conn.execute(_MOVED)
        return dict(removed)

    def remove_unreadable(self, *, conn: Any = None) -> dict[str, int]:
        """Forget the rows no key of this server opens among the gone ones (B-R5, as root ruled it): never one a source
        still gives, which that source's next run seals again (`apply`). `NewerKey`, nothing removed, when one of them
        names a newer master key version than this server holds: an instance that has it may open it. How many of each
        type went."""
        if conn is None:
            with self._db.transaction() as own:
                return self.remove_unreadable(conn=own)
        conn.execute(_ONE_WRITER)
        rows = conn.execute("SELECT id, value FROM ee_terms WHERE gone IS NOT NULL").fetchall()
        lost = [(str(i), bytes(sealed)) for i, sealed in rows if self._open(str(i), bytes(sealed)) is None]
        newest = max((version_of(sealed) or 0 for _, sealed in lost), default=0)
        if newest > self._sealer.version:
            raise NewerKey(
                f"a synced term is sealed with master key version {newest}, newer than this server's "
                f"{self._sealer.version}: nothing is removed"
            )
        if not lost:
            return {}
        cursor = conn.execute(
            "DELETE FROM ee_terms WHERE gone IS NOT NULL AND id = ANY(%s) RETURNING type", ([i for i, _ in lost],)
        )
        removed = Counter(str(etype) for (etype,) in cursor.fetchall())
        if removed:
            conn.execute(_MOVED)
        return dict(removed)

    def record_run(self, run: Run) -> None:
        """``run`` is its source's last; a sync asked for before it began is done (one asked while it ran stays for
        the next run)."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO ee_term_runs (source, run) VALUES (%s, %s) ON CONFLICT (source) DO UPDATE SET run ="
                " EXCLUDED.run, asked = CASE WHEN ee_term_runs.asked <= %s THEN NULL ELSE ee_term_runs.asked END,"
                " asked_by = CASE WHEN ee_term_runs.asked <= %s THEN NULL ELSE ee_term_runs.asked_by END",
                (run.source, Jsonb(asdict(run)), run.started, run.started),
            )

    def forget_runs(self, names: Iterable[str], *, conn: Any = None) -> None:
        """As `MemoryTermStore.forget_runs`: the rows of ``names`` in ee_term_runs, their last runs and the syncs asked
        for of them, deleted."""
        if conn is None:
            with self._db.transaction() as own:
                self.forget_runs(names, conn=own)
            return
        conn.execute("DELETE FROM ee_term_runs WHERE source = ANY(%s)", (list(names),))

    def runs(self) -> dict[str, Run]:
        with self._db.connection() as conn:
            rows = conn.execute("SELECT source, run FROM ee_term_runs WHERE run IS NOT NULL").fetchall()
        return {str(source): Run(**run) for source, run in rows}

    def ask(self, names: Iterable[str], by: str | None, now: float, *, conn: Any = None) -> None:
        """As `MemoryTermStore.ask`: a source whose last run began at ``now`` or after has it already. The row a run
        is keeping is locked by the upsert, which reads it as that run committed it."""
        if conn is None:
            with self._db.transaction() as own:
                self.ask(names, by, now, conn=own)
            return
        for name in names:
            conn.execute(
                "INSERT INTO ee_term_runs (source, asked, asked_by) VALUES (%s, %s, %s)"
                " ON CONFLICT (source) DO UPDATE SET asked = EXCLUDED.asked, asked_by = EXCLUDED.asked_by"
                " WHERE ee_term_runs.run IS NULL OR (ee_term_runs.run->>'started')::double precision < EXCLUDED.asked",
                (name, now, by),
            )

    def asks(self) -> dict[str, Asked]:
        with self._db.connection() as conn:
            rows = conn.execute("SELECT source, asked, asked_by FROM ee_term_runs WHERE asked IS NOT NULL").fetchall()
        return {str(source): Asked(str(source), float(at), by) for source, at, by in rows}

    def counts(self) -> dict[str, dict[str, Any]]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT source, type, gone IS NOT NULL, COUNT(*) FROM ee_terms GROUP BY source, type, gone IS NOT NULL"
            ).fetchall()
        return tally((str(source), str(etype), bool(gone), int(n)) for source, etype, gone, n in rows)

    def close(self) -> None:
        return None  # the Database is the server's

    def _ids_key(self, conn: Any) -> bytes:
        """The key of the terms' ids, on ``conn``, for `apply` alone (it holds ``_ONE_WRITER``): 32 random bytes made by
        the first run, and kept sealed (two making it at once keep the first: the second's update waits for the first's
        and finds the key set). `SealedElsewhere` when this server's master key does not open it."""
        sealed = _sealed_ids_key(conn)
        if sealed is None:
            made = self._sealer.seal(os.urandom(32), _IDS_KEY)
            conn.execute("UPDATE ee_terms_state SET ids_key = %s WHERE id = 1 AND ids_key IS NULL", (made,))
            sealed = _sealed_ids_key(conn)
        if sealed is None:  # no row: 600_terms.sql makes it
            raise RuntimeError("the synced terms' state is missing: the server's schema is not migrated")
        return self._opened_ids_key(sealed)

    def _opened_ids_key(self, sealed: bytes) -> bytes:
        try:
            return self._sealer.open(sealed, _IDS_KEY)
        except (InvalidTag, ValueError):  # another master key, or a version of it this server does not hold
            raise SealedElsewhere(_ELSEWHERE) from None

    def _seal(self, value: str, term: str) -> bytes:
        return self._sealer.seal(value.encode(), f"ee_terms:{term}")

    def _open(self, term: str, sealed: bytes) -> str | None:
        """The value sealed for row ``term``; None when this store's sealer does not open it."""
        try:
            return self._sealer.open(sealed, f"ee_terms:{term}").decode()
        except (InvalidTag, ValueError):  # another master key's, a version not held, or a value moved from another row
            return None

    def _opened(self, rows: Iterable[tuple[Any, ...]]) -> tuple[list[StoredTerm], list[str]]:
        """The rows' terms, their values opened; the ids of those this store's sealer cannot open, skipped."""
        out: list[StoredTerm] = []
        unopened: list[str] = []
        for term, source, etype, sealed, first_seen, gone in rows:
            value = self._open(str(term), bytes(sealed))
            if value is None:
                unopened.append(str(term))
                continue
            out.append(
                StoredTerm(
                    str(term), str(source), str(etype), value, float(first_seen), None if gone is None else float(gone)
                )
            )
        return out, unopened

    def _still_unopened(self, conn: Any, ids: list[str]) -> list[str]:
        """Of ``ids``, the rows this store's sealer cannot open now (another instance may have sealed one again since),
        leaving out those that name a newer key version than this server holds (the instances that have it open them,
        and a seal of this server's would take that version away)."""
        if not ids:
            return []
        rows = conn.execute("SELECT id, value FROM ee_terms WHERE id = ANY(%s)", (ids,)).fetchall()
        return [
            str(i)
            for i, sealed in rows
            if self._open(str(i), bytes(sealed)) is None and (version_of(bytes(sealed)) or 0) <= self._sealer.version
        ]


def _sealed_ids_key(conn: Any) -> bytes | None:
    row = conn.execute("SELECT ids_key FROM ee_terms_state WHERE id = 1").fetchone()
    return bytes(row[0]) if row is not None and row[0] is not None else None
