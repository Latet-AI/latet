"""Term sync's tables on PostgreSQL: no term held in clear, a value bound to its row, and terms another master key
sealed skipped, counted, and never matched against."""

import base64

import pytest

from latet.ee.events.locks import MemoryLocks
from latet.ee.sync.pgstore import SEALER_PURPOSE, PgTermStore
from latet.ee.sync.store import NewerKey, Run, SealedElsewhere
from latet.server.master import MasterKey
from latet.server.sealing import version_of

from ..servers import MASTER_KEY, services
from .fakes import Clock, id_of, make_sync

pytestmark = [pytest.mark.server, services]
TABLES = ("ee_terms", "ee_term_runs", "ee_terms_state")


def _rows(conn) -> str:
    """Every row of term sync's tables as ``conn`` sees them, each table in the order of its first column."""
    return "\n".join(
        repr(row) for table in TABLES for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    )


def held(db) -> str:
    """Every row of term sync's tables, as the database keeps it."""
    with db.connection() as conn:
        return _rows(conn)


def test_no_term_is_held_in_clear(server):
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    store.record_run(Run("customers", "file", 1.0, 2.0, "ok", found=1, added=1))
    store.ask(["customers"], "u_sec0000001", now=3.0)
    assert "Halden" not in held(server.db) and "halden" not in held(server.db)


def test_a_value_moved_to_another_row_does_not_open(server):
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")], now=1.0)
    with server.db.transaction() as conn:
        first, second = conn.execute("SELECT id, value FROM ee_terms ORDER BY id").fetchall()
        conn.execute("UPDATE ee_terms SET value = %s WHERE id = %s", (second[1], first[0]))
    assert len(store.terms()) == 1 and store.unreadable == 1


def test_terms_another_master_key_sealed_are_skipped_counted_and_never_matched_against(server):
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    other = PgTermStore(server.db, MasterKey([b"\x08" * 32]).sealer(SEALER_PURPOSE))  # a restore with another key
    assert other.terms() == [] and other.unreadable == 1
    with pytest.raises(SealedElsewhere):
        other.apply("customers", [("Halden Insurance", "CLIENT")], now=2.0)
    assert [t.value for t in store.terms()] == ["Halden Insurance"]  # nothing changed


# Each change a route writes in its own transaction, with its entry in the administration log; each changes something
# in the state `test_a_change_written_in_the_callers_transaction_is_undone_with_it` sets up.
CHANGES = {
    "apply": lambda store, conn: store.apply("projects", [("Project Heron", "PROJECT")], 3.0, conn=conn),
    "retire": lambda store, conn: store.retire([], 3.0, conn=conn),
    "remove": lambda store, conn: store.remove(conn=conn),
    "remove_unreadable": lambda store, conn: store.remove_unreadable(conn=conn),
    "ask": lambda store, conn: store.ask(["customers"], "u_sec0000001", 3.0, conn=conn),
    "forget_runs": lambda store, conn: store.forget_runs(["projects"], conn=conn),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_change_written_in_the_callers_transaction_is_undone_with_it(server, change):
    """Given ``conn``, a change is written in the caller's transaction and undone with it: the terms, the runs and the
    asks, and the version, are as they were."""
    store = _one_unopenable(server)
    store.apply("customers", [], now=2.0)  # both gone, one of them no key opens
    store.apply("projects", [("Project Falcon", "PROJECT")], now=2.0)
    store.record_run(Run("projects", "url", 2.0, 2.5, "ok", found=1, added=1))
    before, version = held(server.db), store.version()

    def route() -> None:
        with server.db.transaction() as conn:
            CHANGES[change](store, conn)
            assert _rows(conn) != before  # written, in the caller's transaction
            raise RuntimeError("the entry could not be written")

    with pytest.raises(RuntimeError, match="the entry could not be written"):
        route()
    assert held(server.db) == before and store.version() == version


def _one_unopenable(server):
    """Two terms, one of whose values was moved to the other's row: that row no key opens (its additional data differs)."""
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")], now=1.0)
    with server.db.transaction() as conn:
        first, second = conn.execute("SELECT id, value FROM ee_terms ORDER BY id").fetchall()
        conn.execute("UPDATE ee_terms SET value = %s WHERE id = %s", (second[1], first[0]))
    assert len(store.terms()) == 1 and store.unreadable == 1
    return store


def test_a_term_no_key_opens_that_its_source_still_gives_is_never_removed_and_heals_at_the_next_run(server):
    """B-R5, as root ruled it: the administration's removal never takes a row a source still gives; the next run seals
    it again (the term's id, keyed over its value, proves the value given now)."""
    store = _one_unopenable(server)
    assert store.remove_unreadable() == {}  # its source still gives it (not gone): never removed by hand
    assert store.kept() == 2 and len(store.terms()) == 1 and store.unreadable == 1
    store.apply("customers", [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")], now=2.0)
    assert sorted(t.value for t in store.terms()) == ["Halden Insurance", "Kestrel Analytics"] and store.unreadable == 0


def test_a_term_no_key_opens_and_no_source_gives_is_removed_by_the_administration(server):
    """B-R5: the rows removed are those no key of this server opens AND that are gone (the last successful run of their
    source did not give them again, or the source was taken out), counted by type."""
    store = _one_unopenable(server)
    store.retire([], 2.0)  # the source taken out: both terms gone, still masked
    assert store.remove_unreadable() == {"CLIENT": 1}  # the one no key opens alone
    assert store.kept() == 1 and len(store.terms()) == 1 and store.unreadable == 0


def test_the_administrations_removal_stops_at_a_term_of_a_newer_key_version_with_nothing_removed(server):
    """B-R5: never while a row it would remove names a newer key version than this server holds (another instance may
    open it): nothing is removed."""
    store = _one_unopenable(server)
    newer = PgTermStore(server.db, MasterKey([base64.b64decode(MASTER_KEY), b"\x0a" * 32]).sealer(SEALER_PURPOSE))
    newer.apply("projects", [("Project Falcon", "PROJECT")], now=2.0)  # sealed with version 2, which this server lacks
    store.retire([], 3.0)  # every source taken out: every term gone, the one no key opens and the newer one among them
    with pytest.raises(NewerKey):
        store.remove_unreadable()
    assert store.kept() == 3  # nothing removed


def test_the_gone_list_holds_its_limit_of_readable_terms_when_rows_no_key_opens_come_first(server):
    """E6's deferred minor: the rows no key opens are left out before the limit applies, the next ones read in their
    place (a page further when needed), so the list holds ``limit`` readable terms whenever that many are gone; its
    total counts every gone row, those no key opens included."""
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [(f"Client {n:02}", "CLIENT") for n in range(6)], now=1.0)
    store.apply("customers", [], now=2.0)  # all six gone at 2.0
    store.apply("projects", [("Project Falcon", "PROJECT"), ("Project Heron", "PROJECT")], now=1.0)
    store.apply("projects", [], now=3.0)  # gone later: listed first
    with server.db.transaction() as conn:
        rows = conn.execute("SELECT id, value FROM ee_terms ORDER BY gone DESC, id").fetchall()
        for term, _ in rows[:3]:  # the first three listed (both projects, then a customer): no key opens them
            conn.execute("UPDATE ee_terms SET value = %s WHERE id = %s", (rows[-1][1], term))
    listed = [str(term) for term, _ in rows]
    readable = listed[3:]
    assert ([t.id for t in store.gone(3)[0]], store.gone(3)[1]) == (readable[:3], 8)  # every gone row counted
    assert [t.id for t in store.gone(2)[0]] == readable[:2]  # two pages read past the three no key opens
    assert ([t.id for t in store.gone(100)[0]], store.gone(100)[1]) == (readable, 8)
    assert ([t.id for t in store.gone(2, "customers")[0]], store.gone(2, "customers")[1]) == (readable[:2], 6)
    assert store.gone(2, "projects") == ([], 2)  # none readable: an empty list, its total still said


def test_the_key_of_the_ids_is_made_by_a_run_alone(server):
    """Making the ids' key is `apply`'s alone, on the row it holds locked: no other change makes it (a retire, a
    removal, a sync asked for), and the ids a run gives are keyed with it, a change of case the same id."""
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    with server.db.transaction() as conn:
        store.retire([], 1.0, conn=conn)
        store.remove(conn=conn)
        store.ask(["customers"], None, 1.0, conn=conn)
    with server.db.connection() as conn:
        assert conn.execute("SELECT ids_key FROM ee_terms_state").fetchall() == [(None,)]
    with pytest.raises(LookupError, match="no synced term has been kept yet"):
        id_of(store, "customers", "CLIENT", "Halden Insurance")
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=2.0)
    assert [t.id for t in store.terms()] == [id_of(store, "customers", "CLIENT", "HALDEN INSURANCE")]


def test_a_term_sealed_again_moves_the_version_once(server):
    """A row sealed again changes what an instance reads (a term it could not open, it now can): the version moves, so
    that every instance reads the terms again; once the row opens, the next run seals nothing again."""
    store = _one_unopenable(server)
    version = store.version()
    both = [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")]
    assert store.apply("customers", both, now=2.0) == (0, 0) and store.version() == version + 1
    assert store.apply("customers", both, now=3.0) == (0, 0) and store.version() == version + 1


def test_a_term_sealed_again_leaves_the_tag_of_the_terms_masked_as_it_was(server):
    """The final review's I1: a run that seals again a row no key opened (B-R5) moves the store's version, and the
    terms masked are the same: their tag stays, so that nothing taken under them is masked again or refused."""
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    both = [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")]
    store.apply("customers", both, now=1.0)
    clock = Clock()
    sync = make_sync(store, locks=MemoryLocks(clock), clock=clock)
    sync.reload()
    tag = sync.live.tag
    with server.db.transaction() as conn:  # one value moved to the other row: that row no key opens
        first, second = conn.execute("SELECT id, value FROM ee_terms ORDER BY id").fetchall()
        conn.execute("UPDATE ee_terms SET value = %s WHERE id = %s", (second[1], first[0]))
    assert len(store.terms()) == 1 and store.unreadable == 1
    version = store.version()
    store.apply("customers", both, now=2.0)  # its source gives it again: sealed again
    assert store.version() == version + 1 and store.unreadable == 1
    assert sync.reload() is False and (sync.live.tag, sync.live.count) == (tag, 2) and store.unreadable == 0
    sync.close()


def test_an_instance_short_of_a_newer_key_version_never_seals_its_terms_again(server):
    """A term sealed with a version this instance does not hold opens on the instances that have it: this one's run
    leaves it as it is, rather than seal it with an older version."""
    store = PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
    store.apply("customers", [], now=0.5)  # the ids' key, sealed with this instance's version
    newer = PgTermStore(server.db, MasterKey([base64.b64decode(MASTER_KEY), b"\x0a" * 32]).sealer(SEALER_PURPOSE))
    newer.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)  # sealed with version 2
    assert store.terms() == [] and store.unreadable == 1
    version = store.version()
    assert store.apply("customers", [("Halden Insurance", "CLIENT")], now=2.0) == (0, 0)
    assert store.version() == version and [t.value for t in newer.terms()] == ["Halden Insurance"]
    with server.db.connection() as conn:
        (sealed,) = conn.execute("SELECT value FROM ee_terms").fetchall()[0]
    assert version_of(bytes(sealed)) == 2
