"""The prints of a space's vault held in a process (the server performance plan's S2): every print of the word rows of
an identity's vault (``latet.server.prints``: a cheap keyed fingerprint of each value's and fake's word and edges, in
the slot of its row's role) in a sorted numpy array of distinct signed 64-bit numbers, eight bytes a print (about 16 MB
for a million values), beside a side set of the newest, merged into it past ``INDEX_SIDE_MOST``. A text's words' prints
asked of it (``PrintIndex.present``) say which words may be one of the vault's: a word whose prints are absent is no
value's or fake's word of the vault, and nothing is asked of PostgreSQL for it; one whose print is present has its
entries fetched by print, through a cache (``PrintCache``), and each of them is checked by its decrypted string before
it masks or restores anything (two words may share a print of sixty-four bits, once in trillions). The index holds
prints alone: no word, no value, no entry's id.

Built from PostgreSQL in one snapshot (``PrintIndex.build``): the identity's change counter, then whether every word row
has its print (``PgVaultStore.prints_complete``: a vault kept by 0.10.0 gets them in the background, ``fill_prints``,
and has no index meanwhile), then every print; its watermark is the counter it read. Kept current by the change counts
(``PrintIndex.catch_up``): the statement that moves an identity's count writes it into the word rows it counts
(``counted``) and holds the counter's row until it commits, so that the counts commit in the order they are given. In a
snapshot where the counter reads N, every change counted N or less is committed and none counted past N is, and each
row carries the count of the last change that wrote it: the rows stamped past the watermark and up to N, read in that
same snapshot, are all that the index lacks, whether or not this process heard of their changes. Read apart from the
counter, they would not be: a change that stamps an entry's rows again (a rule, a fake given anew) could carry them past
the counter read. A count a message tells only says there is something to read. A value forgotten leaves its prints in
the index until it is built again; they find nothing.

The cache is told what each catch-up names (``PrintCache.drop``), by the catch-up itself when it is given the cache
(``catch_up(cache=...)``): inside its lock, before it adds the new prints and moves its watermark, so that no reader
sees the new watermark or the new prints and then a print of the cache the new rows changed; and inside the store's
telling of what changed (``named``), so that no vault reads that telling before the cache is told. Rows deleted name
nothing: a forget moves a count of its own in its transaction (``gone_owner``), read in the catch-up's snapshot, and a
catch-up that finds it moved empties the whole cache instead (``PrintCache.clear``). And the cache refuses a
fetch older than a drop: whoever fetches reads ``PrintCache.epoch`` before asking PostgreSQL, and gives it back to
``PrintCache.put``, which keeps nothing of a fetch begun before a drop (it may lack an entry the catch-up named, or hold
one as it was before it changed)."""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

import numpy as np
from psycopg.adapt import Buffer, Loader
from psycopg.pq import Format

from latet.server.prints import prints

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.engine.vault import Entity
    from latet.server.db import Database

# The newest prints held beside the sorted array at most (``PrintIndex.add``): one more merges the two, a copy of the
# array (a few milliseconds a million values), so that a value kept costs a copy of the side alone, half a megabyte at
# most, and the side's 65,536 a lookup that a binary search answers as fast as the array's.
INDEX_SIDE_MOST = 65_536
# The indexes a process builds at once, each in a thread of its own: each reads its vault's prints in one statement
# and sorts them, about a second and some tens of megabytes a million values while it lasts.
INDEX_BUILDS_AT_ONCE = 2
BUILD_TURNS = threading.BoundedSemaphore(INDEX_BUILDS_AT_ONCE)
# Bound even the temporary aggregate; an identity past this size keeps using the guarded SQL lookup (``IndexTooLarge``).
INDEX_BUILD_BYTES = 128 << 20


class IndexTooLarge(MemoryError):
    """A build refused before its aggregate is made: the vault has more print rows than ``INDEX_BUILD_BYTES`` allows. A
    vault's size only grows, so its space builds no index again while it lasts (``spaces.Spaces._index_judged``)."""


# What a build holds at its highest, in eight-byte buffers of its print rows (``build_bytes``), measured as the native
# private bytes of a process (the lifetime peak of its private commit after a build, less its private bytes before):
# three at once while the aggregate is read (libpq's input buffer and its result's value, each the whole aggregate, and
# the native array read from it), two and a little once its connection is closed (that array, the distinct prints and
# the mask that picks them). On the bench's million values (2,000,000 rows, 16 MB a buffer) 49.0 to 52.5 MB, 3.1 to 3.3
# buffers, in nine builds; four, so that two builds at once (``INDEX_BUILDS_AT_ONCE``) are weighed for what they take.
BUILD_BUFFERS = 4
# What a catch-up tells what changed by (``PgVaultStore._telling``): given the entries its rows name (None: not known by
# name), the cache is told inside what it gives, which no reader of the tellings is let into before it ends.
Telling = Callable[[set[str] | None], contextlib.AbstractContextManager[None]]


def build_bytes(rows: int) -> int:
    """What a build of ``rows`` print rows is reserved, before its aggregate is made or sent: ``BUILD_BUFFERS`` arrays
    of eight bytes a row."""
    return BUILD_BUFFERS * 8 * rows


def gone_owner(identity_id: str) -> str:
    """Whose count of ``change_counters`` counts the forgets that deleted an entry of the identity's vault (each one, in
    its own transaction, ``PgVaultStore.forget_entity`` and ``forget_values``): rows deleted cannot name themselves to a
    catch-up, and this count says one went (``PrintIndex.catch_up``)."""
    return f"{identity_id}:gone"


_COUNTER: Final = "SELECT counter FROM change_counters WHERE owner = %s"
# The change counter and the count of forgets (``gone_owner``), in the build's snapshot.
_COUNTERS: Final = (
    "SELECT (SELECT counter FROM change_counters WHERE owner = %s),"
    " (SELECT counter FROM change_counters WHERE owner = %s)"
)
# How many print rows the build's snapshot holds, a LIMIT past the allowance at most: read in the same REPEATABLE READ
# snapshot as the aggregate (``_PRINTS``), it is the number of rows the aggregate reads, so that a count within the
# allowance bounds the aggregate before PostgreSQL makes it, and the build's memory is reserved for it before it is sent
# (``build_bytes``). 0.2 s of the bench's million values (two million rows).
_BUILD_ROWS: Final = (
    "SELECT count(*) FROM (SELECT 1 FROM vault_words WHERE identity_id = %s AND print IS NOT NULL LIMIT %s) AS bounded"
)
# Every print of the identity's word rows, in one value: each the eight bytes of a binary int8 (big-endian), one after
# another, in no order, NULL when there is none. A row a print costs more to send and to read than the print itself: on
# the bench's million values (two million rows), 1.2 s this way, 1.8 s as rows of a binary cursor, 3.5 s by a binary
# COPY read a row at a time; PostgreSQL's own part is 0.2 s of it, a scan in parallel. Unbounded in SQL: its rows are
# those ``_BUILD_ROWS`` counted, in the same snapshot (a LIMIT, or a CTE materialized to bound it, takes the scan out
# of parallel: 0.67 s, against 0.22 s this way).
_PRINTS: Final = (
    "SELECT string_agg(int8send(print), ''::bytea) FROM vault_words WHERE identity_id = %s AND print IS NOT NULL"
)
# Whether a fake of the vault has a reading of its own (``vault.fake_reading``: a fake written in escapes, kept with
# how it reads, a second string of the fake's role of its entry): those readings are not among the words' prints, so
# that an index of a vault that has one does not tell a string that is no fake (``PgVaultStore.may_be_fake``). Every
# fake's string is looked at: 3.7 s a million values through the strings' index of entries (a heap fetch a row), 0.9 s
# read in the order of the table's pages (``_READINGS_PLAN``), which no other identity's rows slow either. Read apart
# from the build (``PrintIndex.check_readings``), after the index is ready.
_READINGS: Final = (
    "SELECT EXISTS (SELECT 1 FROM vault_strings WHERE identity_id = %s AND role = 'surrogate'"
    " AND entity_id IS NOT NULL GROUP BY entity_id HAVING count(*) > 1)"
)
_READINGS_PLAN: Final = (
    "SET LOCAL enable_indexscan = off",
    "SET LOCAL enable_indexonlyscan = off",
    "SET LOCAL enable_seqscan = off",
)
# The counter, the count of forgets, and the rows stamped past the watermark and up to it (``vault_words_counted``),
# their entries and prints (NULL for a row without one: its entry is named all the same), in one statement, so in one
# snapshot: a change that stamps an entry's rows again (a rule, a fake given anew, a part of the backfill of the words
# or of the prints) and commits after a counter read apart would carry them past that counter, and a watermark moved to
# it would pass rows never read. One row of the counters and no entry when no row was stamped since; none when the
# identity has no counter yet.
_SINCE: Final = (
    "WITH c AS (SELECT counter FROM change_counters WHERE owner = %s)"
    " SELECT c.counter, (SELECT counter FROM change_counters WHERE owner = %s), w.entity_id, w.print FROM c"
    " LEFT JOIN vault_words w ON w.identity_id = %s AND w.counted > %s AND w.counted <= c.counter"
)
_NONE: Final = np.empty(0, dtype=np.int64)
_NONE.flags.writeable = False


def _among(held: np.ndarray, asked: np.ndarray) -> np.ndarray:
    """Which of ``asked`` are in ``held`` (sorted, distinct): a binary search each."""
    if not held.size:
        return np.zeros(asked.shape, dtype=bool)
    at = np.searchsorted(held, asked)
    np.minimum(at, held.size - 1, out=at)
    return held[at] == asked


def _count(row: Any) -> int:
    return int(row[0]) if row is not None and row[0] is not None else 0


class _Prints(Loader):
    """The aggregate's bytes (``_PRINTS``: big-endian int8s) read from libpq's result into a native array of signed
    64-bit numbers, once: no Python bytes of it in between, which would be one more copy of the aggregate."""

    format = Format.BINARY

    def load(self, data: Buffer) -> np.ndarray:
        return np.frombuffer(data, dtype=">i8").astype(np.int64)


def _distinct(found: np.ndarray) -> np.ndarray:
    """The distinct numbers of ``found``, sorted: ``found`` sorted in place, then those unlike the one before them
    taken, by a mask of a byte a number (``np.unique`` copies its input first)."""
    found.sort()
    if found.size < 2:
        return found
    keep = np.empty(found.size, dtype=bool)
    keep[0] = True
    np.not_equal(found[1:], found[:-1], out=keep[1:])
    return found[keep]


@contextlib.contextmanager
def _reading(db: Database) -> Iterator[Connection[Any]]:
    """A connection of the pool whose statements are each a transaction of their own (autocommit), for a read of one
    statement: the COMMIT a pool's connection sends at the end costs as much as the read (a counter read in 0.6 ms
    rather than 1.6 on the bench's machine), and a call pays it each time it checks the index is current. The
    connection goes back to the pool as it came."""
    with db.connection() as conn:
        conn.autocommit = True
        try:
            yield conn
        finally:
            if not conn.closed:  # a connection that broke: the pool makes another
                conn.autocommit = False


class PrintIndex:
    """The prints of one identity's vault in this process (the module's docstring). ``present`` reads it from any
    thread without a lock: the arrays are never changed in place, each change makes new ones and puts them in place at
    once. ``add``, ``catch_up`` and the end of ``build`` change it one at a time."""

    def __init__(self, db: Database, identity_id: str, key: bytes) -> None:
        """``key``: the key of the vault's prints (``prints.print_key`` of the identity's root key), by which a text's
        words' prints are made (``prints_of``)."""
        self.db, self.identity, self.key = db, identity_id, key
        self.ready = False  # built from a vault whose every word has its print; until then, its words are asked of SQL
        self.watermark = 0  # the change count it holds every row up to
        self._held: tuple[np.ndarray, np.ndarray] = (_NONE, _NONE)  # the sorted array, and the side set, sorted too
        self._changing = threading.Lock()
        self.building_bytes = 0  # reserved while a build lasts (``build_bytes``), besides its published arrays
        self.may_have_readings = True  # decoded legacy fakes need an exact lookup when their anchor differs
        self.gone = 0  # the count of forgets (``gone_owner``) in the snapshot of its watermark
        # The watermark up to which a catch-up told a cache what its rows named (``catch_up(cache=...)``): moved before
        # the watermark, so that a store that reads it behind the watermark knows its cache was not told.
        self.told = 0

    def __len__(self) -> int:
        """How many distinct prints it holds."""
        main, side = self._held
        return int(main.size + side.size)

    @property
    def side(self) -> int:
        """How many of them are in the side set."""
        return int(self._held[1].size)

    @property
    def nbytes(self) -> int:
        """What its prints take in memory: eight bytes each."""
        main, side = self._held
        return int(main.nbytes + side.nbytes)

    def prints_of(self, words: Sequence[str]) -> np.ndarray:
        """The four prints of each of ``words`` (``prints.prints``: as a value's word, a fake's, a value's edge and a
        fake's), a row of four each."""
        return np.array([prints(self.key, word) for word in words], dtype=np.int64).reshape(-1, 4)

    def build(self) -> bool:
        """Read every print of the vault from PostgreSQL, in one snapshot (REPEATABLE READ): the change counter and the
        count of forgets, then whether every word row has its print, then how many print rows there are (past
        ``INDEX_BUILD_BYTES`` of them: IndexTooLarge, and the SQL lookup stays), the build's memory reserved for them
        (``building_bytes``), then the prints, read straight into a native array and made distinct in place. True, and
        ready, its watermark the counter read, when every row has one; False, and not ready, when a print is missing (a
        vault kept by 0.10.0 whose prints are not all made yet): its words are asked of PostgreSQL meanwhile. Whether a
        fake has a reading is not read here: the exact lookup of fakes stays on until ``check_readings`` says."""
        # The store's own question (``PgVaultStore.prints_complete``), imported when a build asks it rather than with
        # this module, which the store's may import.
        from latet.server.vault import _PRINTLESS

        found = held = None
        try:
            with self.db.apart() as conn, conn.transaction():  # libpq keeps its buffer as large as the aggregate
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                counts = conn.execute(_COUNTERS, (self.identity, gone_owner(self.identity))).fetchone()
                counter, gone = (_count(counts), _count(counts[1:])) if counts is not None else (0, 0)
                printless = conn.execute(_PRINTLESS, (self.identity,)).fetchone()
                incomplete = printless is None or bool(printless[0])
                if not incomplete:
                    room = INDEX_BUILD_BYTES // 8
                    rows = _count(conn.execute(_BUILD_ROWS, (self.identity, room + 1)).fetchone())
                    if rows > room:
                        raise IndexTooLarge("the index aggregate exceeds its build allowance")
                    # The same snapshot as the aggregate: its rows are those counted. Reserved before it is made.
                    self.building_bytes = build_bytes(rows)
                    with conn.cursor(binary=True) as cursor:
                        cursor.adapters.register_loader("bytea", _Prints)
                        row = cursor.execute(_PRINTS, (self.identity,)).fetchone()
                    found = row[0] if row is not None else None  # its libpq result let go with the cursor
                    if found is not None and found.size != rows:
                        raise RuntimeError("the build's snapshot read other rows than it counted")
            # Never acquire the changing lock while still holding a connection. catch_up takes them in the
            # opposite order; a one-connection pool must work too.
            if incomplete:
                with self._changing:
                    self.ready, self.watermark, self._held = False, 0, (_NONE, _NONE)
                return False
            held = _distinct(found) if found is not None and found.size else _NONE
            found = None
            with self._changing:
                self._held = (held, _NONE)
                self.watermark = counter
                self.gone = gone
                self.may_have_readings = True
                self.ready = True
            return True
        finally:
            found = held = None  # release the build's arrays before removing their space reservation
            self.building_bytes = 0

    def check_readings(self) -> None:
        """Whether a fake of the vault has a reading (``_READINGS``), read after the index is ready, apart from its
        build, in a snapshot taken after its watermark was read: none there means none in a row up to that watermark.
        Told so (``may_have_readings`` False) only while no catch-up moved the watermark meanwhile, since a catch-up
        may take in a reading (and turns the exact lookup on again whenever it takes in rows). Raises what PostgreSQL
        raised: the exact lookup stays on."""
        watermark = self.watermark
        if not self.ready:
            return
        with self.db.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            for setting in _READINGS_PLAN:
                conn.execute(setting)
            row = conn.execute(_READINGS, (self.identity,)).fetchone()
        if row is None or row[0]:
            return
        with self._changing:
            if self.ready and self.watermark == watermark:
                self.may_have_readings = False

    def tell(self, cache: PrintCache, named: Telling | None = None) -> None:
        """``cache`` told of every row up to the watermark: emptied when the watermark moved past what it was told (a
        build, or a catch-up not given it), one catch-up at a time. ``named`` (``catch_up``'s) is told that what
        changed is not known (None), the cache emptied inside it."""
        with self._changing:
            if self.told < self.watermark:
                with named(None) if named is not None else contextlib.nullcontext():
                    cache.clear()
                    self.told = self.watermark

    def present(self, asked: np.ndarray | Sequence[int]) -> np.ndarray:
        """Which of the prints ``asked`` it holds, a boolean each: in the sorted array or in the side set, by a binary
        search in each. Integer sequences use the same signed int64 conversion as array inputs."""
        asked = np.asarray(asked, dtype=np.int64)
        main, side = self._held
        found = _among(main, asked)
        if side.size:
            found |= _among(side, asked)
        return found

    def add(self, found: Iterable[int]) -> None:
        """Hold ``found`` too: those it does not hold yet go to the side set, merged into the sorted array once it holds
        more than ``INDEX_SIDE_MOST``."""
        with self._changing:
            self._add(found)

    def _add(self, found: Iterable[int]) -> None:
        new = np.unique(np.fromiter(found, dtype=np.int64))
        main, side = self._held
        new = new[~_among(main, new)]
        new = new[~_among(side, new)]
        if not new.size:
            return
        side = np.insert(side, np.searchsorted(side, new), new)
        if side.size > INDEX_SIDE_MOST:
            main, side = np.insert(main, np.searchsorted(main, side), side), _NONE
        self._held = (main, side)

    def catch_up(
        self, counter: int | None = None, cache: PrintCache | None = None, named: Telling | None = None
    ) -> tuple[set[str], set[int]] | None:
        """Take in the rows written since its watermark. ``counter``: a count a change's message told (it says there
        is something to read, never how far); when not given, the identity's change counter is read alone, the check
        each call pays. When it is past the watermark, the counter, the count of forgets and the rows stamped past the
        watermark and up to it are read in one snapshot (``_SINCE``); then, in this order and before another catch-up
        can begin: ``cache`` is told (its epoch moves, so that a fetch under way is not put), their prints are added,
        and the watermark moves to that snapshot's counter (at least any count told, since a change is told once
        committed). Told what the rows name (``PrintCache.drop``: each entry the rows name, with every print it was
        known by, and each print of the rows, with every entry of it): every change that writes an entry stamps all of
        its rows, a row without a print too, so that an entry changed or new, and a print with a new entry, are named.
        But rows deleted name nothing: when the count of forgets moved, every answer of the cache is dropped
        (``PrintCache.clear``). A reader that sees the new watermark, or the new prints present, finds no answer in the
        cache that the rows changed; a caller that gives no cache leaves ``told`` behind, and drops what it holds
        itself. ``named`` is told the entries the rows name, or None when the count of forgets moved (what changed is
        not known by name), the cache told inside it, before anything else moves: no reader of what it was told finds it
        before the cache is (``PgVaultStore._telling``), and the store's resync of a vault's memory reads those alone
        (``PgVaultStore.named_after``). Returns the entries and the prints those rows name, both empty when the counter
        moved and no row was stamped (a value forgotten: its rows went with its entry); None when nothing moved, or
        while not ready (a build takes in everything up to its own counter)."""
        if not self.ready:
            return None
        if counter is None:
            with _reading(self.db) as conn:
                counter = _count(conn.execute(_COUNTER, (self.identity,)).fetchone())
        if counter <= self.watermark:
            return None
        with self._changing:  # one catch-up at a time: the second finds the watermark the first moved
            since = self.watermark
            if not self.ready or counter <= since:
                return None
            with _reading(self.db) as conn:
                args = (self.identity, gone_owner(self.identity), self.identity, since)
                rows = conn.execute(_SINCE, args).fetchall()
            now = _count(rows[0]) if rows else 0
            if now <= since:  # a count told that the counter has not reached: nothing to read
                return None
            gone = _count(rows[0][1:])
            entities = {str(entity_id) for _, _, entity_id, _ in rows if entity_id is not None}
            found = {int(printed) for _, _, _, printed in rows if printed is not None}
            # First of all, before the watermark moves: what changed told, and the cache told inside the telling.
            telling = named(entities if gone == self.gone else None) if named is not None else contextlib.nullcontext()
            with telling:
                if cache is not None:  # nothing a reader can see moved yet
                    if gone != self.gone:  # a forget: its deleted rows name nothing
                        cache.clear()
                    else:
                        cache.drop(entities, found)
                    self.told = now
            self.gone = gone
            # New rows may include an escaped fake whose decoded exact string has a different anchor.
            # Conservatively keep the exact lookup until the next complete build checks the string rows again.
            self.may_have_readings = True
            self._add(found)
            self.watermark = now
        return entities, found


class PrintCache:
    """The entries fetched from PostgreSQL by print (a fetch by print gives every entry that has it), as many as
    ``capacity``, the least recently used let go first. A print is known only with every entry that has it, so that
    what it gives for a print is that print's whole answer, or nothing: an entry let go takes every print it was known
    by with it, and an entry no print names any longer is let go. A print fetched with no entry (its value forgotten
    since the index was built) is known as such, as many of them as ``capacity`` too. A fetch is put only when no drop
    came since it began (``epoch``). Thread-safe.

    Whoever fetches: ``since = cache.epoch``, then ``cache.get(wanted)``, then the prints it did not know fetched from
    PostgreSQL, then ``cache.put(fetched, since)``."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[str, Entity] = OrderedDict()  # by id, the least recently used first
        self._prints: dict[int, tuple[str, ...]] = {}  # every entry of each print it knows, by id
        self._known_by: dict[str, set[int]] = {}  # the prints each entry is known by
        self._none: OrderedDict[int, None] = OrderedDict()  # the prints known to have no entry, the oldest first
        self._epoch = 0  # the drops that named something so far
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """How many entries it holds."""
        return len(self._entries)

    @property
    def nbytes(self) -> int:
        """Conservative resident weight, including complete-print and negative-answer metadata."""
        with self._lock:
            return sum(
                1536 + 4 * sum(len(s) for s in (e.id, e.etype, e.original, e.surrogate, e.rule or ""))
                for e in self._entries.values()
            ) + 128 * len(self._prints)

    def clear(self) -> None:
        """A barrier even for an empty cache: an older in-flight fetch cannot fill it afterward."""
        with self._lock:
            self._epoch += 1
            self._entries.clear()
            self._prints.clear()
            self._known_by.clear()
            self._none.clear()

    @property
    def epoch(self) -> int:
        """How many drops named something so far: read before a fetch begins, and given to ``put`` with what it
        fetched."""
        return self._epoch

    def get(self, wanted: set[int]) -> tuple[list[Entity], set[int]]:
        """The entries of the prints of ``wanted`` it knows (each once), and the prints it does not know."""
        found: dict[str, Entity] = {}
        unknown: set[int] = set()
        with self._lock:
            for printed in wanted:
                ids = self._prints.get(printed)
                if ids is None:
                    unknown.add(printed)
                    continue
                if not ids:
                    self._none.move_to_end(printed)
                for entity_id in ids:
                    if entity_id not in found:
                        found[entity_id] = self._entries[entity_id]
                        self._entries.move_to_end(entity_id)
        return list(found.values()), unknown

    def put(self, fetched: Mapping[int, list[Entity]], since: int) -> None:
        """Hold each print of ``fetched`` with its entries, every entry that has it (as a fetch by print answers it;
        none: a print known to have no entry), in place of what it held of it; then let go of the least recently used
        entries past ``capacity``, each with its prints. ``since``: the ``epoch`` read before the fetch began. When a
        drop came since, nothing of the fetch is kept: it may be older than the rows the drop was for (a print with an
        entry more, an entry changed), and is fetched again when asked."""
        with self._lock:
            if self._epoch != since:
                return
            for printed, entities in fetched.items():
                self._forget_print(printed)
                for e in entities:
                    self._entries[e.id] = e
                    self._entries.move_to_end(e.id)
                    self._known_by.setdefault(e.id, set()).add(printed)
                ids = tuple(dict.fromkeys(e.id for e in entities))
                self._prints[printed] = ids
                if not ids:
                    self._none[printed] = None
            while len(self._entries) > self.capacity:
                self._let_go(next(iter(self._entries)))
            while len(self._none) > self.capacity:
                printed, _ = self._none.popitem(last=False)
                del self._prints[printed]

    def drop(self, entities: Iterable[str], found: Iterable[int]) -> None:
        """Let go of the entries of ``entities`` (by id), each with every print it was known by, and of the prints of
        ``found``: what a catch-up of the index names (``PrintIndex.catch_up``), and a value forgotten. A drop that
        names anything, held or not (a fetch of it may be under way), moves the ``epoch``."""
        entities, found = list(entities), list(found)
        with self._lock:
            if entities or found:
                self._epoch += 1
            for entity_id in entities:
                self._let_go(entity_id)
            for printed in found:
                self._forget_print(printed)

    def _let_go(self, entity_id: str) -> None:
        self._entries.pop(entity_id, None)
        for printed in self._known_by.pop(entity_id, set()):
            self._forget_print(printed)

    def _forget_print(self, printed: int) -> None:
        """``printed`` no longer known; an entry it alone named is let go."""
        ids = self._prints.pop(printed, None)
        if ids is None:
            return
        if not ids:
            del self._none[printed]
        for entity_id in ids:
            known_by = self._known_by.get(entity_id)
            if known_by is None:
                continue
            known_by.discard(printed)
            if not known_by:
                del self._known_by[entity_id]
                self._entries.pop(entity_id, None)
