"""The prints of an API key's vault held in a process (``latet.server.index.PrintIndex``, the server performance plan's
Task 6), built from PostgreSQL in one snapshot (the change counter, whether every word has its print, then every print)
and caught up by the change counts the rows were stamped with (``counted``, S3): whatever another instance keeps, an
applications call that commits later than one after it, a fake given anew, a part of the backfill of the words."""

import contextlib
from dataclasses import replace

import numpy as np
import psycopg
import pytest

import latet.server.index as index_module
from latet.engine.stored import edges_of, word_of
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server import vault as vault_module
from latet.server.db import Database, DatabaseError
from latet.server.index import PrintCache, PrintIndex
from latet.server.prints import SLOT, print_key, prints
from latet.server.vault import PgVaultStore

from ..conftest import ROOT_KEY
from .support import DATABASE

pytestmark = pytest.mark.server

OTHER_KEY = b"\x08" * 32
KEY = print_key(ROOT_KEY)

LAURA = Entity("e" + "1" * 19, "PERSON", EntityClass.NER, "Laura Bianchi", "Ada Rossi", 0, 1.0, "ner")
MARCO = Entity("e" + "2" * 19, "PERSON", EntityClass.NER, "Marco Bianchi", "Ugo Ferri", 0, 1.0, "ner")  # bianchi again
EMAIL = Entity("e" + "3" * 19, "EMAIL", EntityClass.PII, "Laura.Bianchi@nordest.it", "g.rossi@posta.it", 0, 1.0, None)
SECRET = Entity("e" + "5" * 19, "API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4", "jqb_9jI0bA3tA3", 0, 1.0, None)
IBAN = Entity("e" + "6" * 19, "IBAN", EntityClass.PII, "IT60X0542811101000000123456", "IT84W7093766632581800787511", 0, 1.0, None)  # fmt: skip
HELD = Entity("e" + "7" * 19, "PASSWORD", EntityClass.SECRET, "hunter2", "pw9Lq4z", 0, 1.0, None)  # a `held` row too
WORDLESS = Entity("e" + "8" * 19, "BADGE", EntityClass.CUSTOM, "x9-k2", "q7-z4", 0, 1.0, None)  # no anchor: NULL words
KEPT = (LAURA, MARCO, EMAIL, SECRET, IBAN, HELD, WORDLESS)


def _entity(n: int, value: str, fake: str) -> Entity:
    return Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, value, fake, 0, 1.0, None)


def _vault_prints(store: PgVaultStore) -> list[int]:
    """Every print of the identity's word rows, as many times as rows have it."""
    with store.db.connection() as conn:
        rows = conn.execute(
            "SELECT print FROM vault_words WHERE identity_id = %s AND print IS NOT NULL", (store.identity,)
        ).fetchall()
    return [found for (found,) in rows]


def _counter(store: PgVaultStore) -> int:
    with store.db.connection() as conn:
        found = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (store.identity,)).fetchone()
    return found[0] if found else 0


def _prints_of(e: Entity) -> set[int]:
    """The prints of ``e``'s word rows: its value's and its fake's words, and their edges, each in its role's slot."""
    found = set()
    for role in ("original", "surrogate"):
        word = word_of(e, role)
        if word is not None:
            found.add(prints(KEY, word)[SLOT[role]])
        head, tail = edges_of(e, role)
        for side, edge in (("head", head), ("tail", tail)):
            if edge is not None:
                found.add(prints(KEY, edge)[SLOT[f"{role}-{side}"]])
    return found


def _held(index: PrintIndex, found: set[int] | list[int]) -> list[bool]:
    return index.present(np.array(sorted(found), dtype=np.int64)).tolist()


@pytest.fixture
def key(server):
    """An API key's vault (``ROOT_KEY``), the changes it tells, and a second store of it: another instance's."""
    identity, _ = server.identities.add_key("Billing CI")
    heard: list[tuple[str, str | None, int]] = []
    mine = PgVaultStore(server.db, identity.id, ROOT_KEY, changed=lambda *change: heard.append(change))
    theirs = PgVaultStore(server.db, identity.id, ROOT_KEY)
    yield mine, theirs, heard
    for store in (mine, theirs):
        for together in list(store._together.values()):  # a test that failed halfway: its schema can be dropped
            together.rollback()


def test_the_index_holds_every_print_of_the_vault(server, key):
    """Built, the index holds every print of the vault's word rows once (two values of one surname share one), as
    eight bytes each, and none of another key's vault; it is ready, its watermark the change counter it read."""
    mine, _, _ = key
    other, _ = server.identities.add_key("Support bot")
    theirs = PgVaultStore(server.db, other.id, OTHER_KEY)
    for e in KEPT:
        assert mine.save_entity(e) is None and theirs.save_entity(e) is None
    index = PrintIndex(server.db, mine.identity, KEY)
    assert not index.ready and index.catch_up() is None  # nothing to catch up before it is built
    assert index.build()
    rows = _vault_prints(mine)
    assert index.ready and index.watermark == _counter(mine) == len(KEPT)
    assert len(rows) == sum(len(_prints_of(e)) for e in KEPT) and len(set(rows)) == len(rows) - 1  # bianchi twice
    assert set(rows) == set().union(*(_prints_of(e) for e in KEPT))
    assert all(_held(index, rows)) and len(index) == len(set(rows)) and index.side == 0
    assert index.nbytes == 8 * len(set(rows))
    assert not any(_held(index, _vault_prints(theirs)))
    others = np.random.default_rng(7).integers(-(2**63), 2**63 - 1, 10_000, dtype=np.int64)
    assert not index.present(others).any()
    assert index.catch_up() is None  # nothing kept since


def test_an_index_is_not_ready_while_a_print_is_missing(key):
    """A vault with a word row and no print (kept by 0.10.0) gives no index: its space keeps reading the words through
    PostgreSQL. Once ``fill_prints`` gave them, the index is built, and stands for every word."""
    mine, _, _ = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET print = NULL, counted = NULL WHERE identity_id = %s AND entity_id = %s",
            (mine.identity, EMAIL.id),
        )
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert not index.build() and not index.ready and len(index) == 0
    assert index.catch_up() is None
    assert mine.fill_prints() == 2
    assert index.build() and index.ready and index.watermark == _counter(mine)
    assert all(_held(index, _prints_of(EMAIL)))
    # Built again over an index that was ready: a print missing again makes it not ready.
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET print = NULL WHERE identity_id = %s AND entity_id = %s", (mine.identity, LAURA.id)
        )
    assert not index.build() and not index.ready


def test_a_value_kept_by_another_store_is_caught_up_by_its_count(key):
    """Another instance keeps a value: the index does not hold its prints until it reads the change counter past its
    watermark, then takes in the rows stamped since (``counted``), and names their entry and prints for the cache to
    drop. Read again with nothing kept since, nothing moves; a count told by a message past the watermark has the
    rows read, and one not past it nothing."""
    mine, theirs, heard = key
    for e in (LAURA, EMAIL):
        assert mine.save_entity(e) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.watermark == 2
    assert theirs.save_entity(SECRET) is None
    assert not any(_held(index, _prints_of(SECRET)))
    assert index.catch_up() == ({SECRET.id}, _prints_of(SECRET))
    assert all(_held(index, _prints_of(SECRET))) and index.watermark == 3 and index.side == len(_prints_of(SECRET))
    assert index.catch_up() is None and index.catch_up(2) is None
    # Told by a message: the count it carries is past the watermark, so the rows are read.
    assert mine.save_entity(IBAN) is None
    assert heard[-1] == ("entity", IBAN.id, 4)
    assert index.catch_up(heard[-1][2]) == ({IBAN.id}, _prints_of(IBAN))
    assert index.watermark == 4 and index.catch_up() is None and all(_held(index, _prints_of(IBAN)))
    # A forget moves the counter and stamps nothing (its rows went with its entry): nothing to take in.
    mine.forget_entity(LAURA.id)
    assert index.catch_up() == (set(), set()) and index.watermark == 5
    assert all(_held(index, _prints_of(LAURA)))  # its prints stay until the index is built again, and find nothing
    assert index.build() and not any(_held(index, _prints_of(LAURA) - _prints_of(EMAIL)))


def test_rows_of_a_call_still_open_are_caught_up_once_it_commits(key):
    """An applications call keeps its values together, in a transaction that moves the counter last, as it commits;
    meanwhile another instance keeps a value and commits. The index catches up the second's rows, not the call's,
    which nothing can see yet; then, once the call commits, the call's, by the count it stamped them with."""
    mine, theirs, _ = key
    assert mine.save_entity(LAURA) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.watermark == 1
    a, b = _entity(10, "m.conti@posta.it", "r.galli@posta.it"), _entity(11, "p.neri@posta.it", "u.sala@posta.it")
    together = mine.together()
    assert mine.save_entity(a) is None and mine.save_entity(b) is None  # the call's, not committed
    assert theirs.save_entity(EMAIL) is None  # another instance's, committed: count 2
    assert index.catch_up() == ({EMAIL.id}, _prints_of(EMAIL)) and index.watermark == 2
    assert not any(_held(index, _prints_of(a) | _prints_of(b)))
    together.commit()  # count 3 and 4: the rows stamped 4, the last
    assert _counter(mine) == 4
    assert index.catch_up() == ({a.id, b.id}, _prints_of(a) | _prints_of(b)) and index.watermark == 4
    assert all(_held(index, _prints_of(a) | _prints_of(b)))


def test_a_fake_given_anew_is_caught_up(key):
    """Another instance gives a value a new fake: the index takes in the new fake's word and edges by their count, and
    names the entry, whose fake the cache holds no longer; the old fake's prints stay, and find nothing."""
    mine, theirs, _ = key
    assert mine.save_entity(IBAN) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build()
    drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
    assert theirs.replace_fake(IBAN, drawn) is None
    new = _prints_of(drawn) - _prints_of(IBAN)
    assert new and not any(_held(index, new))
    entities, found = index.catch_up() or (set(), set())
    assert entities == {IBAN.id} and found == _prints_of(drawn) and all(_held(index, _prints_of(drawn)))
    assert all(_held(index, _prints_of(IBAN)))  # the old fake's, until built again


def test_a_value_restamped_before_its_message_is_handled_is_caught_up(key):
    """A value is kept (count 2), then given a new fake (count 3), which stamps its rows again with 3, before the
    message of count 2 is handled: the count a message tells only says there is something to read. The index reads
    the counter and the rows in one snapshot, takes in the value's rows as the later change left them, and its
    watermark is that snapshot's counter, never past a row it has not read."""
    mine, theirs, _ = key
    assert theirs.save_entity(LAURA) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.watermark == 1
    assert theirs.save_entity(IBAN) is None
    told = _counter(mine)
    drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
    assert theirs.replace_fake(IBAN, drawn) is None
    assert (told, _counter(mine)) == (2, 3)
    assert index.catch_up(told) == ({IBAN.id}, _prints_of(drawn)) and index.watermark == 3
    assert all(_held(index, _prints_of(drawn))) and index.catch_up() is None


def test_a_change_between_the_counter_read_and_the_rows_is_caught_up(key, monkeypatch):
    """Another instance gives a value a new fake (its rows stamped again, past the counter just read) between the
    index's read of the counter and its read of the rows: the rows are read with the counter of their own snapshot, so
    the value's rows are taken in, and the watermark is the counter they were read with."""
    mine, theirs, _ = key
    assert theirs.save_entity(LAURA) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build()
    assert theirs.save_entity(IBAN) is None  # count 2: the counter the index reads first
    reading, reads = index_module._reading, []

    @contextlib.contextmanager
    def changed_between(db):
        reads.append(db)
        if len(reads) == 2:  # the rows' read: another instance's change commits just before it (count 3)
            drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
            assert theirs.replace_fake(IBAN, drawn) is None
        with reading(db) as conn:
            yield conn

    monkeypatch.setattr(index_module, "_reading", changed_between)
    entities, found = index.catch_up() or (set(), set())
    drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
    assert len(reads) == 2 and entities == {IBAN.id} and found == _prints_of(drawn)
    assert index.watermark == _counter(mine) == 3 and all(_held(index, _prints_of(drawn)))
    monkeypatch.undo()
    assert index.catch_up() is None


def test_a_catch_up_drops_from_the_cache_before_its_watermark_moves(key):
    """The cache holds Laura's print (`bianchi`, her value's word) with her entry alone; another instance keeps Marco
    Bianchi, whose value's word has the same print. A catch-up given the cache drops what the new rows name from it
    before its watermark moves: from the moment a reader can see the new watermark (and the new prints present), the
    cache no longer answers that print with Laura alone. Without the cache, the caller drops it after."""
    mine, theirs, _ = key
    assert mine.save_entity(LAURA) is None
    bianchi = prints(KEY, "bianchi")[SLOT["original"]]
    assert word_of(LAURA, "original") == word_of(MARCO, "original") == "bianchi"
    cache = PrintCache(10)
    seen: list[tuple[int, list[str], set[int], bool]] = []

    class Watched(PrintIndex):
        """An index that says, each time its watermark moves past the build's, what the cache and the index answer
        for the shared print at that moment."""

        @property
        def watermark(self) -> int:
            return self._watermark

        @watermark.setter
        def watermark(self, value: int) -> None:
            if self.ready and value > getattr(self, "_watermark", 0):
                found, unknown = cache.get({bianchi})
                seen.append((value, [e.id for e in found], unknown, bool(_held(self, _prints_of(MARCO))[0])))
            self._watermark = value

    index = Watched(mine.db, mine.identity, KEY)
    assert index.build() and index.watermark == 1
    cache.put({bianchi: [LAURA]}, cache.epoch)  # fetched by print: every entry that had it then
    assert [e.id for e in cache.get({bianchi})[0]] == [LAURA.id]
    assert theirs.save_entity(MARCO) is None  # count 2: its value's row has the same print
    since = cache.epoch  # a fetch under way meanwhile, put after the catch-up: refused
    assert index.catch_up(cache=cache) == ({MARCO.id}, _prints_of(MARCO))
    assert seen == [(2, [], {bianchi}, True)]  # at the watermark's move: the print unknown, Marco's prints present
    cache.put({bianchi: [LAURA]}, since)
    assert cache.get({bianchi}) == ([], {bianchi}) and len(cache) == 0
    cache.put({bianchi: [LAURA, MARCO]}, cache.epoch)
    assert sorted(e.id for e in cache.get({bianchi})[0]) == [LAURA.id, MARCO.id]
    # Nothing new: nothing dropped, the epoch where it was.
    epoch = cache.epoch
    assert index.catch_up(cache=cache) is None and cache.epoch == epoch and len(cache) == 2


def test_a_backfill_part_is_caught_up(key, monkeypatch):
    """Entries kept before their words were (no word rows): the index built without them; the backfill writes their
    words in parts, each counted once and told as `words` (no entry's), and the index takes them in by that count,
    read or told."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    mine, _, heard = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    old = (LAURA, EMAIL, SECRET)
    with mine.db.connection() as conn:
        conn.execute(
            "DELETE FROM vault_words WHERE identity_id = %s AND entity_id = ANY(%s)",
            (mine.identity, [e.id for e in old]),
        )
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.watermark == len(KEPT)
    assert not any(_held(index, _prints_of(EMAIL) | _prints_of(SECRET)))
    heard.clear()
    assert mine.backfill_words() == 3
    assert [kind for kind, *_ in heard] == ["words", "words"] and heard[-1][2] == len(KEPT) + 2
    # The first part's message, handled once both parts committed: it says there is something to read, and the index
    # reads every row up to the counter of its own snapshot, the second part's too.
    entities, found = index.catch_up(heard[0][2]) or (set(), set())
    assert entities == {e.id for e in old} and found == set().union(*(_prints_of(e) for e in old))
    assert all(_held(index, set().union(*(_prints_of(e) for e in KEPT))))
    assert index.watermark == _counter(mine) == heard[-1][2]
    assert index.catch_up(heard[-1][2]) is None and index.catch_up() is None


def test_a_catch_up_leaves_the_pools_connection_as_it_came(key, schema, monkeypatch):
    """The counter and the rows are read in autocommit (a COMMIT would cost as much as the read): the pool's
    connection goes back as it came, whether the read worked or failed, and the next borrower's statements are a
    transaction again, as every other statement of the server expects."""
    mine, theirs, _ = key
    db = Database(DATABASE, schema=schema, max_size=1)  # one connection: the one the index used
    try:
        assert mine.save_entity(LAURA) is None
        index = PrintIndex(db, mine.identity, KEY)
        assert index.build()
        assert theirs.save_entity(EMAIL) is None
        assert index.catch_up() == ({EMAIL.id}, _prints_of(EMAIL))
        with db.connection() as conn:
            assert not conn.autocommit
        assert theirs.save_entity(SECRET) is None
        broken = "SELECT no_such_column FROM vault_words WHERE %s AND %s AND %s AND %s"  # _SINCE's four arguments
        monkeypatch.setattr(index_module, "_SINCE", broken)
        with pytest.raises(psycopg.errors.UndefinedColumn):
            index.catch_up()
        assert index.watermark == 2  # nothing taken in
        with db.connection() as conn:
            assert not conn.autocommit
            conn.execute("SELECT 1")
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        monkeypatch.undo()
        assert index.catch_up() == ({SECRET.id}, _prints_of(SECRET))
    finally:
        db.close()


# --- Task 8 fix round 1: the build's cost back to Task 6's, every bound kept ------------------------------------------

# The build as Task 8 first shipped it (b1d6d466): a bounded count, a materialized bounded aggregate and the readings
# in the same snapshot. The oracle of what the index must hold.
_TASK8_ROWS = (
    "SELECT count(*) FROM (SELECT 1 FROM vault_words WHERE identity_id = %s AND print IS NOT NULL LIMIT %s) AS bounded"
)
_TASK8_PRINTS = (
    "WITH bounded AS MATERIALIZED (SELECT print FROM vault_words WHERE identity_id = %s"
    " AND print IS NOT NULL LIMIT %s) SELECT count(*),"
    " CASE WHEN count(*) <= %s THEN string_agg(int8send(print), ''::bytea) END FROM bounded"
)
_TASK8_READINGS = (
    "SELECT EXISTS (SELECT 1 FROM vault_strings WHERE identity_id = %s AND role = 'surrogate'"
    " AND entity_id IS NOT NULL GROUP BY entity_id HAVING count(*) > 1)"
)
ESCAPED = replace(LAURA, id="e" + "9" * 19, original="Lia%20Fontana", surrogate="L.%42ianchi", decoded=None)


def _task8_build(store: PgVaultStore) -> tuple[np.ndarray, int, bool]:
    room = index_module.INDEX_BUILD_BYTES // 8
    with store.db.connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        counter = conn.execute(index_module._COUNTER, (store.identity,)).fetchone()
        assert conn.execute(_TASK8_ROWS, (store.identity, room + 1)).fetchone()[0] <= room
        row = conn.execute(_TASK8_PRINTS, (store.identity, room + 1, room), binary=True).fetchone()
        readings = conn.execute(_TASK8_READINGS, (store.identity,)).fetchone()[0]
    raw = row[1]
    held = np.unique(np.frombuffer(raw, dtype=">i8").astype(np.int64)) if raw else np.empty(0, dtype=np.int64)
    return held, counter[0] if counter else 0, bool(readings)


@pytest.mark.parametrize("vault", ["kept", "with-a-reading", "empty"])
def test_the_index_built_holds_exactly_what_task_8s_first_build_held(key, vault):
    """The same vault built both ways: the same distinct prints in the same order, the same watermark, and the same
    answer on escaped fakes' readings once the build has decided it."""
    mine, _, _ = key
    entities = {"kept": KEPT, "with-a-reading": (*KEPT, ESCAPED), "empty": ()}[vault]
    for e in entities:
        assert mine.save_entity(e) is None
    expected, counter, readings = _task8_build(mine)
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.ready
    index.check_readings()
    main, side = index._held
    assert main.dtype == expected.dtype and np.array_equal(main, expected) and side.size == 0
    assert index.watermark == counter and index.may_have_readings is readings is (vault == "with-a-reading")
    assert len(index) == len(set(_vault_prints(mine)))


def test_a_build_reads_its_prints_in_one_unbounded_parallel_aggregate_after_a_bounded_count(key, monkeypatch):
    """No materialized or limited aggregate (no parallel scan) and no readings in the build's snapshot: the bounded
    count in the same REPEATABLE READ snapshot is what bounds the aggregate, before it is made or sent."""
    mine, _, _ = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    sent: list[str] = []
    execute = psycopg.Cursor.execute

    def record(cursor, query, params=None, **kwargs):
        sent.append(str(query))
        return execute(cursor, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", record)
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build()
    aggregate = [query for query in sent if "string_agg" in query]
    assert aggregate == [index_module._PRINTS]
    assert "LIMIT" not in aggregate[0] and "MATERIALIZED" not in aggregate[0]
    assert sent.index(index_module._BUILD_ROWS) < sent.index(index_module._PRINTS)
    assert "LIMIT" in index_module._BUILD_ROWS  # the count's own work is bounded too
    assert not any("vault_strings" in query for query in sent)
    assert index.may_have_readings  # until it is decided, apart


def test_the_aggregate_is_read_on_a_connection_of_its_own_closed_after_the_build(key, monkeypatch):
    """libpq keeps its input buffer as large as the largest answer a connection read, for as long as it lives (16 MB
    a million values, 128 MiB at the allowance): where PostgreSQL has a connection to spare, the aggregate is not read
    on a connection the pool keeps."""
    mine, _, _ = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    read_on = []
    execute = psycopg.Cursor.execute

    def record(cursor, query, params=None, **kwargs):
        if str(query) == index_module._PRINTS:
            read_on.append(cursor.connection)
        return execute(cursor, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", record)
    pooled = []
    connection = mine.db.connection

    @contextlib.contextmanager
    def lent(*args, **kwargs):
        with connection(*args, **kwargs) as conn:
            pooled.append(conn)
            yield conn

    monkeypatch.setattr(mine.db, "connection", lent)
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and len(index) == len(set(_vault_prints(mine)))
    assert len(read_on) == 1 and read_on[0].closed
    assert all(conn is not read_on[0] for conn in pooled)


def test_a_build_with_no_connection_to_spare_reads_on_one_of_the_pools(key, monkeypatch):
    """PostgreSQL makes no connection more (``max_connections`` reached): the build does not fail, it reads on one of
    the pool's, which goes back to the pool open."""
    mine, _, _ = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    expected = sorted(set(_vault_prints(mine)))

    def refused():
        raise DatabaseError("the connection to PostgreSQL failed (OperationalError)")

    monkeypatch.setattr(mine.db, "_own", refused)
    read_on = []
    execute = psycopg.Cursor.execute

    def record(cursor, query, params=None, **kwargs):
        if str(query) == index_module._PRINTS:
            read_on.append(cursor.connection)
        return execute(cursor, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", record)
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.ready and index.watermark == _counter(mine)
    assert index._held[0].tolist() == expected
    assert len(read_on) == 1 and not read_on[0].closed


def test_an_error_on_the_pools_connection_of_a_read_apart_is_not_chained_to_the_refused_connection(key, monkeypatch):
    """The fallback is no handler of the refused connection: what fails on the pool's connection is raised alone, not
    "during handling of" the connection PostgreSQL did not make (the review of the fix rounds, minor 3)."""
    mine, _, _ = key

    def refused():
        raise DatabaseError("the connection to PostgreSQL failed (OperationalError)")

    monkeypatch.setattr(mine.db, "_own", refused)

    def failing_read() -> None:
        with mine.db.apart() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
            raise ValueError("inside the read")

    with pytest.raises(ValueError, match="inside") as raised:
        failing_read()
    assert raised.value.__context__ is None and raised.value.__cause__ is None


def test_readings_decided_apart_are_trusted_only_while_no_catch_up_moved_the_watermark(key, monkeypatch):
    """The readings are read after the watermark, in a later snapshot: none there means none up to the watermark. A
    catch-up meanwhile may have taken in a reading: the answer is not used, and the exact lookup stays on."""
    mine, theirs, _ = key
    for e in KEPT:
        assert mine.save_entity(e) is None
    index = PrintIndex(mine.db, mine.identity, KEY)
    assert index.build() and index.may_have_readings
    execute = psycopg.Cursor.execute

    def caught_up_meanwhile(cursor, query, params=None, **kwargs):
        if str(query) == index_module._READINGS:
            assert theirs.save_entity(ESCAPED) is None
            assert index.catch_up() is not None  # the reading's entry taken in before the answer comes back
        return execute(cursor, query, params, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(psycopg.Cursor, "execute", caught_up_meanwhile)
        index.check_readings()
    assert index.may_have_readings
    assert index.build()
    index.check_readings()
    assert index.may_have_readings  # the reading is there now
    theirs.forget_entity(ESCAPED.id)
    assert index.build()
    index.check_readings()
    assert not index.may_have_readings
    assert index.catch_up() is None and not index.may_have_readings
    assert theirs.save_entity(_entity(40, "nuova.persona@example.test", "altra.persona@example.test")) is None
    assert index.catch_up() is not None and index.may_have_readings  # any row taken in turns it on again


def test_the_build_reservation_is_the_measured_native_peak_a_print_row():
    """The reservation formula: the build's native private bytes a print row, measured on a million values (the lifetime
    peak of private commit after a build in a fresh process, less its private bytes before; ``bench_engine.py ready
    --production-rebuild`` samples the same counter), rounded up to a whole number of eight-byte buffers."""
    assert index_module.BUILD_BUFFERS == 4  # measured 3.1-3.3 (49.0-52.5 MB for 2,000,000 rows), nine builds
    assert index_module.build_bytes(2_000_000) == 4 * 8 * 2_000_000 == 64_000_000
    assert index_module.build_bytes(0) == 0
    assert index_module.build_bytes(index_module.INDEX_BUILD_BYTES // 8) == 4 * index_module.INDEX_BUILD_BYTES
