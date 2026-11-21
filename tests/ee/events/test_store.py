"""The event log: counts by the hour and by kind of value, a filter, each batch counted once, the schema enforced, what
is older than the retention forgotten, a key kept once, the jobs' times; the same in memory and in PostgreSQL, where
instances writing at once add up; and a project, the one name a client gives, kept by name 50 times an identity an
hour (a thousand a group's, for the people a log keeps by group alone), the others counted as `other`. In PostgreSQL a
write reads the projects kept by their key, never the hour's counts, under 64 locks at most, READ COMMITTED; prune
forgets a day of hours a transaction."""

import contextlib
import threading
import uuid

import pytest
from pydantic import ValidationError

from latet.ee.events.schema import NotAnEvent, checked
from latet.ee.events.store import MemoryEvents
from latet.ee.settings import EnterpriseSettings
from latet.events import FIELDS, apps_event, exchange_event, make_event

T0 = 1_790_000_000.0  # 2026-09-21T14:13:20Z, hour 497222
HOUR = 3600.0
EMILY, JAMES = "u_emilycarte", "u_jameswilso"
BACKSLASH = chr(0x5C)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.server)])
def store(request):
    if request.param == "memory":
        return MemoryEvents()
    from latet.ee.events.pg import PgEvents

    return PgEvents(request.getfixturevalue("database"))


def _event(event_type="exchange", /, *, at=T0, **fields):
    """A checked event of ``event_type`` (positional: ``kind`` is a field of the event, person or key)."""
    return checked(make_event(event_type, clock=lambda: at, **fields))


def _write(store, *events, batch=None):
    return store.write(batch or uuid.uuid4().hex, list(events), at=T0)


def _together(*work):
    """Each of ``work`` on a thread of its own, at once, as instances write: back once all are done, raising what one
    of them raised."""
    failed: list[BaseException] = []

    def run(one):
        try:
            one()
        except BaseException as exc:
            failed.append(exc)

    threads = [threading.Thread(target=run, args=(one,)) for one in work]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failed:
        raise failed[0]


class _Noted:
    """A connection to PostgreSQL whose statements are noted, with their arguments, as they are sent."""

    def __init__(self, conn, noted):
        self._conn, self._noted = conn, noted

    def execute(self, query, params=None, **options):
        self._noted.append((query, params))
        return self._conn.execute(query, params, **options)

    def cursor(self, *args, **options):
        return _NotedCursor(self._conn.cursor(*args, **options), self._noted)


class _NotedCursor:
    def __init__(self, cur, noted):
        self._cur, self._noted = cur, noted

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *failure):
        return self._cur.__exit__(*failure)

    def executemany(self, query, rows, **options):
        rows = list(rows)
        self._noted.append((query, rows))
        return self._cur.executemany(query, rows, **options)


def _noting(database, monkeypatch, first=None):
    """The statements each transaction of ``database`` sends from now on, a list a transaction (``first``, a statement,
    sent at the start of each before the store's own)."""
    transactions: list[list[tuple[object, object]]] = []
    real = database.transaction

    @contextlib.contextmanager
    def transaction():
        with real() as conn:
            if first is not None:
                conn.execute(first)
            mine: list[tuple[object, object]] = []
            transactions.append(mine)
            yield _Noted(conn, mine)

    monkeypatch.setattr(database, "transaction", transaction)
    return transactions


def _relations(plan):
    """The tables a plan of PostgreSQL (EXPLAIN's JSON) reads."""
    if isinstance(plan, list):
        return set().union(*map(_relations, plan)) if plan else set()
    if not isinstance(plan, dict):
        return set()
    found = {plan["Relation Name"]} if "Relation Name" in plan else set()
    return found.union(*map(_relations, plan.values()))


def test_events_add_up_by_the_hour_and_by_kind_of_value(store):
    _write(store, _event(group="g_payments", agent="codex", masked={"EMAIL": 2, "IBAN": 1}),
           _event(group="g_payments", agent="codex", masked={"EMAIL": 1}, at=T0 + 60))  # fmt: skip
    _write(store, _event("stopped", group="g_platform", agent="claude-code", stopped=1, reason="leak", at=T0 + HOUR))
    rows = store.query(T0 - HOUR, T0 + 2 * HOUR, by=("type",))
    assert {(r["type"], r["category"], r["events"], r["masked"]) for r in rows} == {
        ("exchange", "", 2, 0), ("exchange", "EMAIL", 2, 3), ("exchange", "IBAN", 1, 1), ("stopped", "", 1, 0),
    }  # fmt: skip
    first = store.query(T0 - 800, T0 - 800 + HOUR, by=("hour", "code"))
    assert {(r["hour"], r["code"], r["category"]) for r in first} == {
        (497222, "", ""),
        (497222, "", "EMAIL"),
        (497222, "", "IBAN"),
    }


def test_a_filter_keeps_the_rows_of_one_group(store):
    _write(store, _event(group="g_payments", masked={"EMAIL": 1}), _event(group="g_platform", masked={"EMAIL": 5}))
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("group",), where={"group": {"g_payments"}})
    assert {(r["group"], r["category"], r["masked"]) for r in rows} == {
        ("g_payments", "", 0),
        ("g_payments", "EMAIL", 1),
    }
    assert store.query(T0 - HOUR, T0 + HOUR, where={"group": set()}) == []


def test_identities_are_counted_with_their_kind_and_decisions_by_who_decided(store):
    _write(store, _event(identity=EMILY, kind="person"), _event(identity="u_billingci2", kind="key"),
           _event("approval", outcome="approved", identity=EMILY, kind="person", by=JAMES))  # fmt: skip
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("type", "identity", "kind", "decider", "code"))
    assert {(r["type"], r["identity"], r["kind"], r["decider"], r["code"]) for r in rows} == {
        ("exchange", EMILY, "person", "", ""), ("exchange", "u_billingci2", "key", "", ""),
        ("approval", EMILY, "person", JAMES, "approved"),
    }  # fmt: skip


def test_a_batch_written_again_counts_once(store):
    event = _event(masked={"EMAIL": 1})
    assert _write(store, event, batch="b" * 32) is True
    assert _write(store, event, batch="b" * 32) is False  # the same batch, after an error that hid its commit
    [row] = [r for r in store.query(T0 - HOUR, T0 + HOUR) if r["category"] == "EMAIL"]
    assert (row["events"], row["masked"]) == (1, 1)


def test_instances_writing_at_once_add_up(store):
    def write_many():
        for _ in range(25):
            _write(store, _event(group="g_payments", masked={"EMAIL": 1}), _event(group="g_platform"))

    _together(*[write_many] * 4)
    [row] = [r for r in store.query(T0 - HOUR, T0 + HOUR) if r["category"] == "EMAIL"]
    assert (row["events"], row["masked"]) == (100, 100)


def test_what_is_older_than_the_retention_is_forgotten(store):
    _write(store, _event(at=T0 - 40 * 86400, masked={"EMAIL": 1}), _event(at=T0 - 86400, masked={"EMAIL": 1}))
    assert store.prune(30, now=T0) == 2  # the old event's two rows: itself, and its kind of value
    assert sum(r["masked"] for r in store.query(0, T0)) == 1


def test_a_retention_that_is_not_a_whole_number_of_days_is_refused_and_forgets_nothing(store):
    """What prune forgets is gone for good: a retention read wrong (none, a part of a day) forgets nothing."""
    _write(store, _event(at=T0 - 40 * 86400), _event(at=T0 - 2 * HOUR))
    for days in (0, -1, 1.5, True, None):
        with pytest.raises(ValueError, match="whole number of days, 1 at least"):
            store.prune(days, now=T0)
    assert sum(r["events"] for r in store.query(0, T0)) == 2


def test_a_key_is_kept_once_whoever_offers_it_first(store):
    assert store.kept("reports-pseudonyms", b"first") == b"first"
    assert store.kept("reports-pseudonyms", b"second") == b"first"


def test_each_job_remembers_when_it_ran_and_what_it_noted(store):
    assert store.job("retention") == (None, None)
    store.set_job("license", T0, '{"state": "valid"}')
    assert store.job("license") == (T0, '{"state": "valid"}')


def test_each_gives_the_rows_of_query_one_at_a_time(store):
    _write(store, _event(group="g_payments", masked={"EMAIL": 1}), _event(group="g_platform", masked={"EMAIL": 5}))
    asked = (T0 - HOUR, T0 + HOUR)
    rows = store.each(*asked, by=("group",))
    assert next(rows) in store.query(*asked, by=("group",))  # one at a time (R2-8): an export may stop early
    assert sorted(map(repr, store.each(*asked, by=("group",)))) == sorted(map(repr, store.query(*asked, by=("group",))))


def test_sums_add_up_requests_stops_and_values_in_the_store_and_rank_them(store):
    """``sums`` does a report's arithmetic where the rows are: requests (an event's own row), stopped ones and values
    masked (the kinds' rows) by what is asked, ranked by requests then values then key, the first ``limit`` given and
    how many keys there were."""
    _write(store, _event(project="web-shop", masked={"EMAIL": 2, "IBAN": 1}), _event(project="web-shop"),
           _event("stopped", project="billing", stopped=1, reason="leak", masked={"EMAIL": 1}),
           _event(project="billing"), _event(project="alpha", masked={"IBAN": 4}), _event(project="zeta"))  # fmt: skip
    since, until = T0 - HOUR, T0 + HOUR
    rows, keys = store.sums(since, until, ("project",))
    assert keys == 4 and [(r["project"], r["exchanges"], r["stopped"], r["masked"]) for r in rows] == [
        ("web-shop", 2, 0, 3), ("billing", 2, 1, 1), ("alpha", 1, 0, 4), ("zeta", 1, 0, 0),
    ]  # fmt: skip
    top, keys = store.sums(since, until, ("project",), limit=2)
    assert keys == 4 and [r["project"] for r in top] == ["web-shop", "billing"]
    iban, keys = store.sums(since, until, ("project",), category="IBAN")  # the requests that masked one, its values
    assert keys == 2 and [(r["project"], r["exchanges"], r["masked"]) for r in iban] == [
        ("alpha", 1, 4),
        ("web-shop", 1, 1),
    ]
    kinds, _ = store.sums(since, until, ("category",))
    assert [(r["category"], r["exchanges"], r["masked"]) for r in kinds] == [
        ("", 6, 0),
        ("IBAN", 0, 5),
        ("EMAIL", 0, 3),
    ]
    assert store.sums(since, until) == ([{"exchanges": 6, "stopped": 1, "masked": 8}], 1)


def test_sums_of_nothing_to_count(store):
    """Totals are one row even of nothing; a list of nothing is empty; a filter that keeps nothing reads nothing."""
    zero = {"exchanges": 0, "stopped": 0, "masked": 0}
    assert store.sums(T0, T0 + HOUR) == ([zero], 1)
    assert store.sums(T0, T0 + HOUR, ("group",)) == ([], 0)
    _write(store, _event(group="g_payments"))
    assert store.sums(T0, T0 + HOUR, ("group",), {"group": set()}) == ([], 0)
    assert store.sums(T0, T0 + HOUR, (), {"group": set()}) == ([zero], 1)
    with pytest.raises(ValueError, match="no such dimension"):
        store.sums(T0, T0 + HOUR, ("user",))
    with pytest.raises(ValueError, match="no such dimension"):
        store.sums(T0, T0 + HOUR, ("group",), {"category": {"EMAIL"}})
    for limit in (0, -1, 1.5, True):  # a limit of no row would give a different count of keys in each store
        with pytest.raises(ValueError, match="a limit is a number of rows, one at least"):
            store.sums(T0, T0 + HOUR, ("group",), limit=limit)


def test_sums_count_no_empty_key_among_the_keys(store):
    """The keys there were are those that name something: the requests of no group, no project or no one are a row
    (a list may show it) but no key, so that a list's ``others`` counts only what it could name."""
    _write(store, _event(group="g_payments"), _event(group="g_platform"), _event(), _event(), _event(at=T0 + 60))
    rows, keys = store.sums(T0 - HOUR, T0 + HOUR, ("group",))
    assert keys == 2 and [(r["group"], r["exchanges"]) for r in rows] == [("", 3), ("g_payments", 1), ("g_platform", 1)]
    assert store.sums(T0 - HOUR, T0 + HOUR, ("group",), limit=1) == ([{"group": "", **_sums(3)}], 2)
    assert store.sums(T0 - HOUR, T0 + HOUR, ("hour", "group"))[1] == 3  # an hour is never empty
    assert store.sums(T0 - HOUR, T0 + HOUR, ("group",), {"group": {""}}) == ([{"group": "", **_sums(3)}], 0)


def _sums(exchanges, stopped=0, masked=0):
    return {"exchanges": exchanges, "stopped": stopped, "masked": masked}


def test_sums_rank_keys_by_their_characters_whatever_the_databases_collation(store):
    """Ties are broken by the key's characters (PostgreSQL's ``C`` collation, Python's order): every instance and both
    stores show the same first rows."""
    names = ["b-shop", "B-shop", "a.shop", "a_shop", "\N{LATIN SMALL LETTER E WITH ACUTE}cole", "e-cole", "Zeta"]
    _write(store, *(_event(project=name) for name in names))
    rows, _ = store.sums(T0 - HOUR, T0 + HOUR, ("project",))
    assert [r["project"] for r in rows] == sorted(names)


@pytest.mark.server
def test_sums_rank_keys_in_the_c_collation_where_the_database_sorts_as_a_language_does(database):
    """A server's database may sort text as a language does (glibc's en_US; ICU's root collation here: this test's
    PostgreSQL, on musl, compares its libc locales as C): ``sums`` still ranks the keys by their characters, as Python
    and every instance do, so that each shows the same first rows."""
    from latet.ee.events.pg import PgEvents

    with database.connection() as conn:
        if not conn.execute("SELECT 1 FROM pg_collation WHERE collname = 'und-x-icu'").fetchone():
            pytest.skip("this PostgreSQL has no ICU collation")
    with database.transaction() as conn:
        conn.execute('ALTER TABLE ee_events ALTER COLUMN project TYPE text COLLATE "und-x-icu"')
    store = PgEvents(database)
    names = ["b-shop", "B-shop", "a.shop", "a_shop", "\N{LATIN SMALL LETTER E WITH ACUTE}cole", "e-cole", "Zeta"]
    _write(store, *(_event(project=name) for name in names))
    with database.connection() as conn:
        as_a_language = [row[0] for row in conn.execute("SELECT project FROM ee_events ORDER BY project").fetchall()]
    assert as_a_language != sorted(names)  # the column sorts otherwise
    rows, _ = store.sums(T0 - HOUR, T0 + HOUR, ("project",))
    assert [r["project"] for r in rows] == sorted(names)


def test_a_dimension_the_log_does_not_have_is_refused(store):
    with pytest.raises(ValueError, match="no such dimension: user"):
        store.query(0, T0, by=("user",))


def test_the_log_counts_nothing_of_a_batch_that_holds_what_is_not_an_event_of_the_schema(store):
    """The log checks every event it is given, whoever gives it: a batch that holds one the schema refuses is refused
    whole, and nothing of it is written, not even its id."""
    good = _event(masked={"EMAIL": 1})
    path = make_event("exchange", clock=lambda: T0) | {"project": f"C:{BACKSLASH}Users{BACKSLASH}emily.carter"}
    with pytest.raises(NotAnEvent, match="project is a name"):
        _write(store, good, path, batch="c" * 32)
    assert store.query(T0 - HOUR, T0 + HOUR) == []
    assert _write(store, good, batch="c" * 32) is True  # its id was not kept either


def test_an_identity_keeps_fifty_projects_an_hour_by_name_and_counts_the_others_as_other(store):
    """A project is the name of the folder a client says it works in: a client naming a new one at each request would
    add a row an hour for each, kept for the retention. The log keeps 50 of an identity's in an hour by name, across
    batches (an instance's each), and counts the others under `other`, their values with them."""

    def named(n, who=EMILY, at=T0):
        return _event(identity=who, kind="person", project=f"p{n:02d}", masked={"EMAIL": 1}, at=at)

    _write(store, *(named(n) for n in range(30)))
    _write(store, *(named(n) for n in range(30, 60)), named(0))  # another batch, as another instance writes one
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("project",))
    assert {r["project"] for r in rows} == {f"p{n:02d}" for n in range(50)} | {"other"}
    counted = {(r["project"], r["category"]): (r["events"], r["masked"]) for r in rows}
    assert counted["other", ""] == (10, 0) and counted["other", "EMAIL"] == (10, 10)
    assert counted["p00", ""] == (2, 0)  # a project kept already counts as itself
    _write(
        store, named(59, at=T0 + HOUR), named(59, who=JAMES)
    )  # the next hour, and another identity: bounds of their own
    assert {r["project"] for r in store.query(T0 + HOUR, T0 + 2 * HOUR, by=("project",))} == {"p59"}
    assert {r["project"] for r in store.query(T0 - HOUR, T0 + HOUR, by=("project",), where={"identity": {JAMES}})} == {
        "p59"
    }


def test_instances_naming_projects_at_once_keep_the_bound(store):
    """Eight instances writing one identity's new projects at once, ten places left below its bound: 50 by name and
    `other`, whatever their order."""
    _write(store, *(_event(identity=EMILY, kind="person", project=f"p{n:02d}") for n in range(40)))
    start = threading.Barrier(8, timeout=30)

    def name_from(first):
        def write():
            start.wait()  # all at once, at the edge of the bound
            for n in range(first, first + 5):
                _write(store, _event(identity=EMILY, kind="person", project=f"p{n:02d}"))

        return write

    _together(*(name_from(40 + 5 * i) for i in range(8)))
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("project",))
    assert len({r["project"] for r in rows}) == 51 and "other" in {r["project"] for r in rows}
    assert sum(r["events"] for r in rows) == 80 and next(r["events"] for r in rows if r["project"] == "other") == 30


@pytest.mark.server
@pytest.mark.parametrize("begun", [None, "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"])
def test_two_instances_naming_an_identitys_last_project_at_once_keep_the_bound(database, monkeypatch, begun):
    """PostgreSQL: a write reads the projects the log holds of an identity under that identity's lock, held to its
    commit. Each write here waits, once it has read them, for the other to read them too (a second at most): two
    writes that read them at once would each find 49 and each add a name; under the lock the second reads 50. A write
    asks for READ COMMITTED first, whatever its transaction was begun as: a stricter one would read from before the
    lock's wait, and find 49 too."""
    from latet.ee.events import pg

    store = pg.PgEvents(database)
    _write(store, *(_event(identity=EMILY, kind="person", project=f"p{n:02d}") for n in range(49)))
    _noting(database, monkeypatch, first=begun)
    both, bounded = threading.Barrier(2), pg.bounded

    def after_both_read(rows, known):
        with contextlib.suppress(threading.BrokenBarrierError):
            both.wait(timeout=1)  # the other write's read, when it can come before this write commits
        return bounded(rows, known)

    monkeypatch.setattr(pg, "bounded", after_both_read)
    _together(
        *(
            lambda name=name: _write(store, _event(identity=EMILY, kind="person", project=name))
            for name in ("p49", "p50")
        )
    )
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("project",))
    assert len(rows) == 51 and next(r["events"] for r in rows if r["project"] == "other") == 1


@pytest.mark.server
def test_a_write_reads_the_projects_kept_by_their_holder_never_the_hours_counts(database, monkeypatch):
    """The bound's read is an exact key's of the projects kept (`ee_event_projects`, an identity's with no group, a
    group's with no identity): never the hour's counts, which a server of many people holds by the million, and which a
    group's holder could only find by reading the whole hour."""
    from latet.ee.events.pg import PgEvents

    store = PgEvents(database)
    _write(store, _event(identity=EMILY, kind="person", group="g_payments", project="billing-api"),
           _event(group="g_payments", kind="person", project="web-shop"))  # fmt: skip
    transactions = _noting(database, monkeypatch)
    _write(store, _event(identity=EMILY, kind="person", project="data-pipeline"),
           _event(group="g_payments", kind="person", project="web-shop"))  # fmt: skip
    asked = [(query, params) for sent in transactions for query, params in sent if str(query).startswith("SELECT")]
    read: set[str] = set()
    with database.connection() as conn:
        for query, params in asked:
            read |= _relations(conn.execute(f"EXPLAIN (FORMAT JSON) {query}", params).fetchone()[0])
    assert read == {"ee_event_projects"}
    with database.connection() as conn:
        kept = set(conn.execute("SELECT hour, identity_id, group_id, project FROM ee_event_projects").fetchall())
    assert kept == {(497222, EMILY, "", "billing-api"), (497222, EMILY, "", "data-pipeline"),
                    (497222, "", "g_payments", "web-shop")}  # fmt: skip


@pytest.mark.server
def test_a_write_takes_the_locks_of_its_holders_stripes_at_most_sixty_four_in_order(database, monkeypatch):
    """A holder's lock is one of 64 stripes of the schema's: a batch of 200 identities naming a project takes 64 locks at
    most, each once, in the order of their keys (two writes never wait on each other in a circle), and still keeps the
    bound exactly (the test of two instances naming the last project)."""
    from latet.ee.events import pg

    store = pg.PgEvents(database)
    transactions = _noting(database, monkeypatch)
    _write(store, *(_event(identity=f"u_person{n:03d}", kind="person", project="billing-api") for n in range(200)))
    [locks] = [params[0] for sent in transactions for query, params in sent if query == pg._LOCK]
    assert pg.STRIPES == 64 and 32 < len(locks) <= 64 and locks == sorted(set(locks))
    assert sum(r["events"] for r in store.query(T0 - HOUR, T0 + HOUR, by=("project",))) == 200


def test_a_holders_stripe_is_the_same_in_every_process():
    """A stripe is a hash of the holder that every instance computes the same (SHA-256, not Python's salted hash)."""
    from latet.ee.events.pg import _stripe

    assert [_stripe((497222, EMILY, "")), _stripe((497222, "", "g_payments")), _stripe((497223, EMILY, ""))] == [
        59,
        18,
        37,
    ]


@pytest.mark.server
def test_prune_forgets_a_day_of_hours_a_transaction_and_the_projects_kept_with_them(database, monkeypatch):
    """What prune forgets goes a day of hours at a time, each day in a transaction of its own (none runs into the
    statements' 60 seconds, however much the log holds; a prune stopped half way forgets the rest the next time), the
    days that hold nothing skipped; the projects kept of those hours with them."""
    from latet.ee.events import pg

    store = pg.PgEvents(database)
    ages = (40, 39.6, 35, 1)  # days: the first two in one day of hours, the last kept
    _write(store, *(_event(at=T0 - age * 86400, identity=EMILY, kind="person", project="billing-api",
                           masked={"EMAIL": 1}) for age in ages))  # fmt: skip
    transactions = _noting(database, monkeypatch)
    assert store.prune(30, now=T0) == 6  # three events, two rows each
    forgotten = [
        [params for query, params in sent if str(query).startswith("DELETE FROM ee_events ")] for sent in transactions
    ]
    ranges = [found for found in forgotten if found]
    assert len(ranges) == 2 and all(len(found) == 1 and found[0][1] - found[0][0] <= pg.PRUNE_HOURS for found in ranges)
    with database.connection() as conn:
        kept = conn.execute("SELECT hour FROM ee_event_projects").fetchall()
    assert kept == [(int((T0 - 86400) // 3600),)]
    assert sum(r["masked"] for r in store.query(0, T0)) == 1


def _project_hours(store):
    """The hours whose projects the log keeps for the bound: memory's index, or PostgreSQL's ee_event_projects."""
    if isinstance(store, MemoryEvents):
        return {who[0] for who in store._projects}
    with store.db.connection() as conn:
        return {row[0] for row in conn.execute("SELECT hour FROM ee_event_projects")}


def test_prune_keeps_the_first_hour_kept_forgets_the_one_before_and_each_range_starts_where_the_last_ended(store):
    """The edges of what prune forgets, in the counts and in the projects kept: the first hour kept (now less the
    retention) stays, the hour before it goes; and in PostgreSQL, where each range runs from its first hour held to
    PRUNE_HOURS after it, an hour exactly PRUNE_HOURS after a range's first goes too, with the next range: one that
    holds a project (both tables), and one that holds none (the counts alone, which the next range is found by too)."""
    from latet.ee.events.pg import PRUNE_HOURS

    oldest = int((T0 - 30 * 86400) // 3600)
    first = oldest - 100  # the first hour held: the first range's start
    named = (first, first + PRUNE_HOURS, oldest - 1, oldest)
    _write(store, *(_event(at=h * 3600 + 60, identity=EMILY, kind="person", project="billing-api") for h in named),
           _event(at=(first + 2 * PRUNE_HOURS) * 3600 + 60, identity=EMILY, kind="person"))  # fmt: skip
    assert store.prune(30, now=T0) == 4
    assert {r["hour"] for r in store.query(0, T0, by=("hour",))} == {oldest}
    assert _project_hours(store) == {oldest}


def test_what_the_schema_refuses_is_a_value_error_of_its_own():
    """A caller tells a batch that will never be kept (drop it) from one PostgreSQL did not take (try it again)."""
    assert issubclass(NotAnEvent, ValueError)
    with pytest.raises(NotAnEvent):
        MemoryEvents().write("d" * 32, [make_event("exchange", clock=lambda: T0) | {"value": "x"}], at=T0)
    for other in (lambda: MemoryEvents().prune(0, now=T0), lambda: MemoryEvents().query(0, T0, by=("user",))):
        with pytest.raises(ValueError, match=r"retention|dimension") as refused:
            other()
        assert not isinstance(refused.value, NotAnEvent)  # what is not about an event is not one


def test_the_people_a_log_keeps_by_group_alone_share_a_thousand_projects_an_hour_in_their_group(store):
    """With `people: group-only` a person's events keep no identity: their projects are bounded by their group's, a
    thousand an hour, so that each person's projects stay apart in a report by group while one client naming a folder
    at each request still adds a bounded number."""
    _write(store, *(_event(group="g_payments", kind="person", project=f"p{n:04d}") for n in range(1005)),
           _event(group="g_platform", kind="person", project="p1004"))  # fmt: skip
    rows = store.query(T0 - HOUR, T0 + HOUR, by=("group", "project"))
    payments = {r["project"]: r["events"] for r in rows if r["group"] == "g_payments"}
    assert len(payments) == 1001 and payments["other"] == 5 and "p0999" in payments and "p1000" not in payments
    assert {r["project"] for r in rows if r["group"] == "g_platform"} == {"p1004"}  # another group's own


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"value": "emily.carter@acme.com"}, "fields the schema does not have"),
        ({"reason": "sensitive values would leave in clear: EMAIL[21]"}, "reason is not a code"),
        ({"identity": "Emily Carter"}, "identity is not a code"),
        ({"identity": "p_short"}, "identity is not a code"),
        ({"by": "James Wilson"}, "by is not a code"),
        ({"rule": "emily.carter@acme.com"}, "rule is not a code"),  # E1's pattern of a code, not a looser one (M10)
        ({"outcome": "Set/Removed"}, "outcome is not a code"),
        ({"group": "Payments"}, "group is not a code"),
        ({"group": "g_pay-ments"}, "group is not a code"),  # make_event's id of a group, nothing looser (M10)
        ({"agent": "Codex CLI"}, "agent is not a code"),
        ({"agent": ".codex"}, "agent is not a code"),  # a code begins with a letter or a digit, as make_event's
        ({"kind": "robot"}, "kind is person or key"),
        ({"masked": {"EMAIL": "two"}}, "masked counts"),
        ({"masked": {"EMAIL": True}}, "masked counts"),
        ({"masked": {"client name": 1}}, "masked counts"),  # a type is E1's code of one (type_code), never a label
        ({"masked": {"email": 1}}, "masked counts"),
        ({"masked": [("EMAIL", 1)]}, "masked counts"),
        ({"stopped": -1}, "stopped is a count"),
        ({"project": "C:/Users/emily.carter/billing"}, "project is a name"),
        ({"project": f"C:{BACKSLASH}billing"}, "project is a name"),
        ({"project": "billing" + chr(10) + "api"}, "project is a name"),
        ({"project": ""}, "project is a name"),
        ({"project": "p" * 201}, "project is a name"),
        ({"ts": "yesterday"}, "ts is a time"),
        ({"ts": "2026-09-21T14:13:20"}, "ts is a time"),  # a time without its zone
        ({"type": "lunch"}, "type is one of"),
        ({"v": 2}, "v is 1"),
        ({"id": "emily.carter@acme.com"}, "id is 32 hexadecimal digits"),
    ],
)
def test_what_is_not_an_event_of_the_schema_is_refused(change, why):
    with pytest.raises(NotAnEvent, match=why):
        checked(make_event("exchange", clock=lambda: T0) | change)


def test_what_is_not_a_mapping_is_no_event():
    with pytest.raises(NotAnEvent, match="an event is a mapping"):
        checked(list(make_event("exchange", clock=lambda: T0)))  # type: ignore[arg-type]


def test_a_checked_event_has_every_field_in_order_and_its_time_in_utc():
    event = checked(
        make_event("exchange", clock=lambda: T0, masked={"EMAIL": 0, "IBAN": 2}) | {"ts": "2026-09-21T16:13:20+02:00"}
    )
    assert list(event) == list(FIELDS) and event["ts"] == "2026-09-21T14:13:20Z" and event["masked"] == {"IBAN": 2}
    assert event["chain"] is None


def test_every_event_community_code_builds_is_an_event_of_the_schema_and_a_pseudonym_too():
    """What E1's builders make (an exchange, a stopped request, an applications call, an entry of the administration
    log) passes the check as it is; a person kept by a pseudonym (`pseudonymous`) too."""
    summary = {"ts": T0, "agent": "Claude Code", "project": {"name": "billing-api"}, "flags": [],
               "new_by_type": {"EMAIL": 2, "client name": 1}}  # fmt: skip
    built = [
        exchange_event(summary, EMILY, "person"),
        exchange_event(summary | {"flags": ["blocked", "leak"], "block_reason": "EMAIL[21]"}, EMILY, "person"),
        apps_event("mask", identity="u_billingci2", token="t_abcdefghij", masked={"Client-Name": 1}, clock=lambda: T0),
        make_event(
            "admin",
            clock=lambda: T0,
            outcome="role.granted",
            rule=JAMES,
            reason="admin:t_abcdefghij",
            chain={"prev": "", "hash": "b" * 64},
        ),
    ]
    for event in built:
        assert checked(event) == event
    pseudonymous = make_event("approval", clock=lambda: T0) | {"identity": "p_abcdefghij", "by": "p_bcdefghijk"}
    assert checked(pseudonymous) == pseudonymous


def test_an_admin_events_chain_is_kept_and_nothing_else_in_its_place():
    chain = {"prev": "a" * 64, "hash": "b" * 64}
    assert checked(make_event("admin", clock=lambda: T0, chain=chain))["chain"] == chain
    with pytest.raises(NotAnEvent, match="chain is"):
        checked(make_event("admin", clock=lambda: T0) | {"chain": {"prev": "", "hash": "emily.carter@acme.com"}})


def test_reports_are_by_group_only_and_kept_a_year_unless_the_company_says_otherwise():
    settings = EnterpriseSettings()
    assert (settings.reports.people, settings.reports.retention_days) == ("group-only", 365)
    with pytest.raises(ValidationError):
        EnterpriseSettings.model_validate({"reports": {"people": "everyone"}})
    with pytest.raises(ValidationError):
        EnterpriseSettings.model_validate({"reports": {"retention_days": 0}})
    assert settings.model_dump(mode="json", exclude_defaults=True) == {}  # a section at its defaults is not written
