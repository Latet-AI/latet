"""The company's record of central approvals, in memory and in PostgreSQL (the second needs the server's services:
marker ``server``): the same answers from both, each person shown only what they may see, and never a text, a value or
a folder's name in the database unsealed."""

import contextlib
import time

import pytest

from latet.ee.approvals import MemoryRecords, PostgresRecords, Row, View
from latet.ee.edition import APPROVALS_RETENTION_LOCK, APPROVALS_RETENTION_SECONDS
from latet.server import db as db_module
from latet.server.db import Database, SchemaMismatch, check_schema, migrate
from latet.state import IdentityRef

from ..servers import DATABASE, MASTER_KEY, Instances, master, services
from ..test_upgrade_010 import _catalog

TABLE = "latet.ee.migrations:500_approvals"  # the record's table: E5's hundred, after E4's 401_siem
ENTERPRISE = "latet.ee.migrations:"
AFTER_E5 = (f"{ENTERPRISE}600_terms",)  # each later part's, after E5's table, in order (E6's synced terms)
MARK = IdentityRef("u_markellisa", "Mark Ellis", "person")
EMILY = ("u_emilycarte", "Emily Carter", "person")
KEY = ("u_billingcia", "Billing CI", "key")
SECURITY = View("u_markellisa", security=True, keys=frozenset(), admin=False)
DAY = 86400.0


def _change(item_id: str, created: float, route: str = "security", asker: tuple[str, str, str] = EMILY, **kw) -> Row:
    reasons = {"source": "dashboard", "agent": None, "loosened": ["project Orion: values never to mask: 1 added"]}
    return Row(item_id, *asker, "change", route, reasons | {"keys": ["allow_values"]}, created, **kw)


def _batch(item_id: str, created: float) -> Row:
    summary = {"agent": "claude-code", "kinds": ["user_text"], "items": 1}
    return Row(item_id, *EMILY, "batch", "security", summary, created)


@pytest.fixture
def unpruned(server):
    """The server's own prune of the record held off while the test runs, as another instance holding its lock would
    hold it off (R73: the edition's jobs' thread prunes at its start): these tests write rows dated near 1970, long
    past the retention as the server's clock (2027) sees it. Taken once a prune under way ended."""
    with contextlib.ExitStack() as held:
        deadline = time.monotonic() + 30.0
        while not held.enter_context(server.bus.lock(APPROVALS_RETENTION_LOCK, APPROVALS_RETENTION_SECONDS)):
            held.close()
            assert time.monotonic() < deadline, "the server's prune of the record never ended"
            time.sleep(0.02)
        yield server


@pytest.fixture(params=["memory", pytest.param("postgres", marks=[pytest.mark.server, services])])
def records(request):
    if request.param == "memory":
        return MemoryRecords()
    server = request.getfixturevalue("unpruned")  # E1's instance in a schema of its own, its prune held off
    return PostgresRecords(server.db, server.master.sealer(b"approvals"))


def test_what_waits_is_listed_oldest_first_and_ends_once(records):
    records.add(_change("c2", 20.0))
    records.add(_batch("b1", 10.0))
    assert [row.id for row in records.waiting(SECURITY, 10)] == ["b1", "c2"]
    assert [row.id for row in records.waiting(SECURITY, 1)] == ["b1"]
    assert not records.end("u_someoneelse", "c2", "approved", 30.0, MARK)  # another identity's id ends nothing
    assert records.end(EMILY[0], "c2", "approved", 30.0, MARK)
    assert not records.end(EMILY[0], "c2", "rejected", 31.0, None)  # it ended already: the first end stays
    ended = records.get("c2")
    assert ended is not None and (ended.state, ended.decided) == ("approved", 30.0)
    assert (ended.by_id, ended.by_name) == (MARK.id, "Mark Ellis")
    assert ended.summary["loosened"] == ["project Orion: values never to mask: 1 added"]
    assert [row.id for row in records.waiting(SECURITY, 10)] == ["b1"] and records.get("nothing") is None


def test_each_person_sees_their_own_their_route_s_and_their_keys(records):
    records.add(_change("c1", 1.0))
    records.add(_change("k1", 2.0, route="controllers", asker=KEY, asked_by="u_emilycarte"))
    records.add(_change("k2", 3.0, route="controllers", asker=("u_otherkeyaa", "Other CI", "key")))
    emily = View("u_emilycarte", security=False, keys=frozenset(), admin=False)
    controller = View("u_danielokaf", security=False, keys=frozenset({KEY[0]}), admin=False)
    admin = View("u_paulferris", security=False, keys=frozenset(), admin=True)
    assert [row.id for row in records.waiting(emily, 10)] == ["c1", "k1"]  # hers, and the one she made in a key's space
    assert [row.id for row in records.waiting(controller, 10)] == ["k1"]
    assert [row.id for row in records.waiting(admin, 10)] == ["k1", "k2"]  # every key's: the edition keeps those
    assert [row.id for row in records.waiting(SECURITY, 10)] == ["c1"]  # the security team's alone
    assert records.count(SECURITY) == 1 and records.count(emily) == 0  # her own wait for someone else


def test_the_same_item_added_twice_is_kept_once_and_one_that_never_waited_is_forgotten(records):
    records.add(_change("c1", 10.0))
    records.add(_change("c1", 99.0, route="controllers"))
    assert [(row.id, row.created, row.route) for row in records.waiting(SECURITY, 10)] == [("c1", 10.0, "security")]
    assert not records.forget("u_someoneelse", "c1")
    assert records.forget(EMILY[0], "c1") and records.get("c1") is None
    records.add(_change("c3", 11.0))
    records.end(EMILY[0], "c3", "approved", 12.0, MARK)
    assert not records.forget(EMILY[0], "c3") and records.get("c3") is not None  # what ended is history


def test_the_history_is_newest_first_and_what_waits_is_counted(records):
    for n in range(3):
        records.add(_change(f"c{n}", float(n)))
    records.add(_batch("b1", 5.0))
    records.add(_change("k1", 6.0, route="controllers", asker=KEY))
    records.end(EMILY[0], "c0", "approved", 10.0, MARK)
    records.end(EMILY[0], "c1", "withdrawn", 12.0, None)
    assert [row.id for row in records.history(SECURITY, 10)] == ["c1", "c0"]
    assert [row.id for row in records.history(SECURITY, 1)] == ["c1"]
    assert records.counts() == {("change", "security"): 1, ("batch", "security"): 1, ("change", "controllers"): 1}


def test_rows_of_the_same_time_are_listed_by_their_ids_in_both_stores(records):
    """Task 5's review, M3: what waits, and what ended, at the same time comes in the order of its ids, the same from
    memory and from PostgreSQL, so that a decider's list never swaps two rows between two reads."""
    for item_id in ("c3", "c1", "c2"):
        records.add(_change(item_id, 5.0))
    assert [row.id for row in records.waiting(SECURITY, 10)] == ["c1", "c2", "c3"]
    for item_id in ("c3", "c1", "c2"):
        records.end(EMILY[0], item_id, "approved", 9.0, MARK)
    assert [row.id for row in records.history(SECURITY, 10)] == ["c1", "c2", "c3"]


def test_decisions_older_than_the_retention_go_and_what_waits_stays(records):
    records.add(_change("old", 1.0))
    records.add(_change("recent", 2.0))
    records.add(_change("waiting", 3.0))
    records.end(EMILY[0], "old", "approved", 10.0, MARK)
    records.end(EMILY[0], "recent", "rejected", 10.0 + 365 * DAY, MARK)
    assert records.prune(20.0) == 1
    assert records.get("old") is None and records.get("recent") is not None and records.get("waiting") is not None


def test_each_identity_keeps_its_latest_ended_items_and_what_waits_and_the_others_stay(records, monkeypatch):
    """The whole-branch security review's L3: past ENDED_KEPT ended items of one identity, its oldest go at the next
    prune, whatever their date, in runs of PRUNE_RUN (a person, or a program, who opens and ends items in a loop never
    fills the record for a year); what waits, what is counted as waiting, and another identity's items stay."""
    from latet.ee import approvals

    monkeypatch.setattr(approvals, "ENDED_KEPT", 3)
    monkeypatch.setattr(approvals, "PRUNE_RUN", 2)
    for n in range(6):
        records.add(_change(f"e{n}", 10 * DAY + n))
        records.end(EMILY[0], f"e{n}", "superseded", 10 * DAY + 10 + n, None)
    records.add(_change("k0", 10 * DAY, "controllers", KEY))
    records.end(KEY[0], "k0", "approved", 10 * DAY + 5, MARK)
    records.add(_change("waiting", 10 * DAY + 20))
    waiting = records.counts()
    assert records.prune(0.0) == 3  # nothing past the retention: three past the bound
    assert [records.get(f"e{n}") is None for n in range(6)] == [True] * 3 + [False] * 3
    assert records.get("k0") is not None and records.get("waiting") is not None and records.counts() == waiting
    assert records.prune(0.0) == 0


def test_an_approval_not_applied_is_said_as_what_it_is_once_and_nothing_else_is(records):
    """R60: an item that ended approved, whose save was then refused, is kept as ``approved-unapplied``, who decided
    kept; one still waiting, one that ended otherwise, another identity's id and an id not there are left as they are."""
    records.add(_change("c1", 1.0))
    records.add(_change("c2", 2.0))
    assert not records.unapplied(EMILY[0], "c1")  # it waits still
    records.end(EMILY[0], "c1", "approved", 3.0, MARK)
    records.end(EMILY[0], "c2", "rejected", 3.0, MARK)
    assert not records.unapplied("u_someoneelse", "c1") and not records.unapplied(EMILY[0], "c2")
    assert not records.unapplied(EMILY[0], "nothing")
    assert records.unapplied(EMILY[0], "c1") and not records.unapplied(EMILY[0], "c1")  # once
    ended, other = records.get("c1"), records.get("c2")
    assert ended is not None and ended.state == "approved-unapplied"
    assert (ended.decided, ended.by_name) == (3.0, "Mark Ellis")  # when, and who decided, kept
    assert other is not None and other.state == "rejected"


@pytest.mark.server
@services
def test_a_text_withheld_where_it_was_sent_is_said_so_and_names_who_approved_whichever_end_comes_first(records):
    """The review of Task 10 (fix rounds 2 and 3): ``approved-changed`` over an approval, or over a row not ended yet
    (the sending instance heard first), whose decider's end then names them once (m2); never over another end."""
    for item_id in ("b_first", "b_after", "b_rejected"):
        records.add(_batch(item_id, 10.0))
    assert records.end(EMILY[0], "b_first", "approved", 20.0, MARK)
    assert records.withheld(EMILY[0], "b_first", 21.0)
    assert records.withheld(EMILY[0], "b_after", 21.0)
    assert records.end(EMILY[0], "b_after", "approved", 22.0, MARK)  # named, and counted where it is told
    assert not records.end(EMILY[0], "b_after", "approved", 23.0, MARK)  # once
    assert records.end(EMILY[0], "b_rejected", "rejected", 20.0, MARK)
    assert not records.withheld(EMILY[0], "b_rejected", 21.0)
    for item_id, decided in (("b_first", 20.0), ("b_after", 21.0)):
        row = records.get(item_id)
        assert row is not None and (row.state, row.decided, row.by_id, row.by_name) == (
            "approved-changed", decided, MARK.id, "Mark Ellis"
        ), item_id  # fmt: skip
    assert records.get("b_rejected").state == "rejected"


def test_the_database_holds_no_reason_unsealed(server):
    """R40 (the plan review's M1): a change's reasons name folders and destinations the person chose: sealed."""
    records = PostgresRecords(server.db, server.master.sealer(b"approvals"))
    records.add(_change("c1", 1.0))
    with server.db.connection() as conn:
        [raw] = conn.execute("SELECT summary FROM ee_approvals WHERE id = 'c1'").fetchone()
    assert b"Orion" not in bytes(raw) and b"allow_values" not in bytes(raw)
    assert records.get("c1").summary["loosened"] == ["project Orion: values never to mask: 1 added"]


@pytest.mark.server
@services
def test_every_arm_of_what_one_person_sees_of_the_history_has_an_index(server):
    """Task 5's review, M1: PostgreSQL serves an OR of conditions from several indexes only when each has one; who made
    a change in an API key's space (``asked_by``) has its own, so that a controller's or an admin's history, sparse
    among the security team's rows, is never read back to the retention's end."""
    with server.db.connection() as conn:
        found = dict(
            conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = 'ee_approvals'",
                (server.db.schema,),
            ).fetchall()
        )
    assert "(asked_by, decided)" in found["ee_approvals_asked_decided"]
    assert "(state IS NOT NULL) AND (asked_by IS NOT NULL)" in found["ee_approvals_asked_decided"]
    assert "(identity_id, decided)" in found["ee_approvals_identity_decided"]
    assert "(route, decided)" in found["ee_approvals_route_decided"]


def _scanned(plan: dict) -> int:
    """The rows a plan's scans of ee_approvals read: those they gave and those their filter removed."""
    found = 0
    if plan.get("Relation Name") == "ee_approvals":
        found += int(plan.get("Actual Rows", 0)) + int(plan.get("Rows Removed by Filter", 0))
    return found + sum(_scanned(child) for child in plan.get("Plans", []))


@pytest.mark.server
@services
def test_no_one_s_history_is_read_back_to_the_retention_s_end(server, unpruned):
    """Task 5's review, M1, and Task 8's check of the plans: among many ended rows, mostly the security team's texts
    (``live_review: security``), the history of a key's controller, of a person, of an admin and of the security team
    each reads a small part of the table, never all of it (each arm of what one sees has an index, or the newest rows
    come first)."""
    from latet.ee import approvals

    with server.db.transaction() as conn:
        conn.execute(
            "INSERT INTO ee_approvals (id, identity_id, identity_name, identity_kind, kind, route, asked_by, summary, "
            "created, state, decided, by_id, by_name) SELECT 's' || g, 'u_p' || (g % 500), 'P', 'person', 'batch', "
            "'security', NULL, '\\x00'::bytea, g, 'approved', g + 0.5, 'u_markellisa', 'Mark Ellis' "
            "FROM generate_series(1, 100000) g"
        )
        conn.execute(
            "INSERT INTO ee_approvals (id, identity_id, identity_name, identity_kind, kind, route, asked_by, summary, "
            "created, state, decided, by_id, by_name) SELECT 'k' || g, 'u_key' || (g % 20), 'K', 'key', 'change', "
            "'controllers', CASE WHEN g % 5 = 0 THEN 'u_p' || (g % 500) END, '\\x00'::bytea, g * 100.0, "
            "'approved', g * 100.0 + 0.5, 'u_danielokaf', 'Daniel Okafor' FROM generate_series(1, 1000) g"
        )
    with server.db.connection() as conn:
        conn.execute("ANALYZE ee_approvals")
    query = (
        f"EXPLAIN (ANALYZE, FORMAT JSON) SELECT {approvals._COLUMNS} FROM ee_approvals "
        f"WHERE state IS NOT NULL AND {approvals._SEES} ORDER BY decided DESC, id LIMIT %s"
    )
    views = {
        "a key's controller": (View("u_danielokaf", False, frozenset({"u_key7"}), False), 50),
        "a person": (View("u_p5", False, frozenset(), False), 50),
        "an admin": (View("u_paulferris", False, frozenset(), True), approvals.WAITING_READ),
        "the security team": (View("u_markellisa", True, frozenset(), False), 50),
    }
    with server.db.connection() as conn:
        for who, (view, limit) in views.items():
            [(found,)] = conn.execute(query, (*PostgresRecords._sees(view), limit)).fetchall()
            assert _scanned(found[0]["Plan"]) < 10_000, who  # a tenth of the table at most


@pytest.mark.server
@services
def test_the_history_is_planned_for_whoever_reads_it_never_once_for_everyone(server):
    """Task 8's check of the plans: psycopg prepares a statement run five times on one connection, and PostgreSQL then
    plans it once for every reader (a generic plan); after an admin's reads, a controller's history was read back to
    the retention's end. The history is never prepared: each read is planned for whoever reads it."""
    db = Database(DATABASE, schema=server.db.schema, max_size=1)  # one connection: every read on it
    try:
        records = PostgresRecords(db, server.master.sealer(b"approvals"))
        admin = View("u_paulferris", security=False, keys=frozenset(), admin=True)
        for _ in range(8):
            records.history(admin, 10)
        with db.connection() as conn:
            prepared = [row[0] for row in conn.execute("SELECT statement FROM pg_prepared_statements").fetchall()]
        assert not [statement for statement in prepared if "ee_approvals" in statement]
    finally:
        db.close()


@pytest.mark.server
@services
def test_a_prune_goes_in_bounded_runs_each_in_a_transaction_of_its_own(server, unpruned, monkeypatch):
    """Task 5's review, M2: what ended past the retention goes PRUNE_RUN rows a transaction, oldest first, until a run
    takes fewer (a retention lowered never makes one statement past the database's bound); what ended since, and what
    waits, stay."""
    from latet.ee import approvals

    records = PostgresRecords(server.db, server.master.sealer(b"approvals"))
    for n in range(5):
        records.add(_change(f"old{n}", float(n)))
        records.end(EMILY[0], f"old{n}", "approved", 10.0 + n, MARK)
    records.add(_change("recent", 6.0))
    records.end(EMILY[0], "recent", "rejected", 100.0, MARK)
    records.add(_change("waiting", 7.0))
    monkeypatch.setattr(approvals, "PRUNE_RUN", 2)
    statements: list[str] = []
    transaction = server.db.transaction

    def watched():
        statements.append("transaction")
        return transaction()

    monkeypatch.setattr(server.db, "transaction", watched)
    assert records.prune(50.0) == 5
    assert len(statements) == 3  # two, two, then one: fewer than a run, the last
    assert [records.get(f"old{n}") for n in range(5)] == [None] * 5
    assert records.get("recent") is not None and records.get("waiting") is not None
    assert records.prune(50.0) == 0 and len(statements) == 4


@pytest.mark.server
@services
def test_a_damaged_row_is_left_out_and_breaks_no_read(server, caplog):
    """R66, R72 (the re-review's RR3-m2): a row whose summary has one byte changed fails its seal's check (InvalidTag):
    left out of the lists and gone to a decision, said by its type, and the other rows still read."""
    records = PostgresRecords(server.db, server.master.sealer(b"approvals"))
    records.add(_change("c1", 1.0))
    records.add(_change("c2", 2.0))
    with server.db.transaction() as conn:
        [raw] = conn.execute("SELECT summary FROM ee_approvals WHERE id = 'c1'").fetchone()
        damaged = bytearray(raw)
        damaged[-1] ^= 1
        conn.execute("UPDATE ee_approvals SET summary = %s WHERE id = 'c1'", (bytes(damaged),))
    everyone = View(person="u_markellisa", security=True, keys=frozenset(), admin=True)
    assert [row.id for row in records.waiting(everyone, 10)] == ["c2"] and records.get("c1") is None
    assert "InvalidTag" in caplog.text and "Orion" not in caplog.text


def _before(name: str) -> bool:
    """A migration of the head before E5: every Community one, and an Enterprise one numbered below E5's hundred (by
    number, as ``test_upgrade_010._before_e4``: a later part's migration is no more the head before E5 than E5's is)."""
    return not name.startswith(ENTERPRISE) or int(name.removeprefix(ENTERPRISE)[:3]) < 500


@pytest.fixture
def before_e5(clock, monkeypatch):
    """A server's schema with every migration this branch had before E5's (its head before the record), no further."""
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    every = db_module._migrations
    with monkeypatch.context() as before:
        before.setattr(db_module, "_migrations", lambda packages: [(n, t) for n, t in every(packages) if _before(n)])
        opened = Instances(clock)
    yield opened
    opened.close()


@pytest.mark.server
@services
def test_a_server_migrated_before_e5_takes_the_record_s_table_alone_and_keeps_what_it_holds(before_e5, instances):
    """The table comes after every Community and Enterprise migration before it, and before each later part's alone
    (AFTER_E5), on a server of the head before E5 as on a new one (E1's ``instances``: every migration), which end with
    the same schema; the Community Edition's migrations have no such table."""
    db = Database(DATABASE, schema=before_e5.schema, max_size=2)
    fresh = Database(DATABASE, schema=instances.schema, max_size=2)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO identities (id, kind, name, created) VALUES (%s, 'person', %s, 0)", (EMILY[0], EMILY[1])
            )
        with pytest.raises(SchemaMismatch, match="older than this Latet"):
            check_schema(db)
        assert migrate(db, master=master()) == [TABLE, *AFTER_E5]
        check_schema(db)
        with db.connection() as conn:
            assert conn.execute("SELECT id, name FROM identities").fetchall() == [EMILY[:2]]
            assert conn.execute("SELECT count(*) FROM ee_approvals").fetchone() == (0,)
        assert _catalog(db) == _catalog(fresh)
        with fresh.connection() as conn:
            applied = [row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY applied, name")]
        assert applied[-1 - len(AFTER_E5) :] == [TABLE, *AFTER_E5]
    finally:
        db.close()
        fresh.close()
    community = db_module._migrations(["latet.server.migrations"])
    assert not any("ee_approvals" in text for _name, text in community)
