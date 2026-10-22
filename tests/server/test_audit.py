"""The administration log: who did what, to what, when and from where, never a value; read back by time, action and
actor; pruned by age. It is a chain under a key of the server's, so that someone who writes the table without the key
cannot change an entry, add one, take one out, or make a prune of his own, and leave the chain as it was."""

import dataclasses
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from latet.server import audit as audit_module
from latet.server.audit import AuditLog, ChainCheck, anchor_tag, entry_hash
from latet.server.db import Database, migrate

from .conftest import AUDIT_KEY, DATABASE, dump, lose_pins

pytestmark = pytest.mark.server

OTHER_KEY = b"\x0b" * 32  # the audit key of another server
DAY = 86400
NUL, LONE, SUBSTITUTE = chr(0), chr(0xD83D), chr(0xFFFD)  # a zero byte, a lone surrogate, and what each is kept as
BACKSLASH = chr(0x5C)


@pytest.fixture
def log(db, clock):
    return AuditLog(db, key=AUDIT_KEY, clock=clock)


def _run(db, query, *args):
    """A statement of the writer's own: how many rows it changed."""
    with db.connection() as conn:
        return conn.execute(query, args).rowcount


def _read(db, query, *args):
    with db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _chain(log, clock, count=4):
    """``count`` entries a day apart, oldest first; the clock is a day after the last."""
    for n in range(count):
        log.record("admin:a_first00000", "person.added", f"u_person{n:05d}", {"n": n}, "10.0.0.5")
        clock.now += DAY
    return log.query()[::-1]


def _head(entry):
    return (entry.id, entry.hash)


def _until(check, seconds=20.0):
    """Whether ``check()`` came true within ``seconds``: asked again until it does (an effect on another connection)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def _waiting(db, lock):
    """How many connections of the database wait for the advisory lock ``lock`` (the bigint key, which PostgreSQL shows
    as two halves)."""
    found = _read(
        db,
        "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid::bigint = %s"
        " AND objid::bigint = %s AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
        (lock >> 32) & 0xFFFFFFFF,
        lock & 0xFFFFFFFF,
    )
    return found[0][0]


# --- what is kept, read back, pruned ------------------------------------------------------------------------------


def test_each_action_is_kept_with_who_what_when_and_where(log, clock):
    log.record("admin:a_first00000", "person.added", "u_emilycarte", {"kind": "person"}, "10.0.0.5")
    clock.now += 60
    log.record("u_emilycarte", "token.added", "u_emilycarte", {"label": "laptop"})
    clock.now += 60
    log.record("server", "server.started", None, {"instance": "a1b2c3d4e5f6"})
    started, token, added = log.query()
    assert (added.actor, added.action, added.target, added.details, added.address) == (
        "admin:a_first00000", "person.added", "u_emilycarte", {"kind": "person"}, "10.0.0.5",
    )  # fmt: skip
    assert (token.ts, started.target, started.details) == (clock.now - 60, None, {"instance": "a1b2c3d4e5f6"})
    assert [e.action for e in log.query(action="person.*")] == ["person.added"]
    assert [e.action for e in log.query(action="token.added")] == ["token.added"]
    assert [e.action for e in log.query(actor="u_emilycarte")] == ["token.added"]
    assert [e.action for e in log.query(since=clock.now - 90)] == ["server.started", "token.added"]
    assert [e.action for e in log.query(until=clock.now - 90)] == ["person.added"]
    assert len(log.query(limit=1)) == 1


def test_a_time_with_microseconds_and_text_of_every_script_come_back_as_they_were_hashed(db, clock):
    clock.now = 1_760_431_234.123456
    log = AuditLog(db, key=AUDIT_KEY, clock=clock)
    name = "Zo" + chr(0xEB) + " " + chr(0x5C71) + chr(0x7530) + " " + chr(0x1F469) + chr(0x200D) + chr(0x1F4BB)
    log.record(f"u_{name}", "token.added", name, {name: [name, 10**20, -3, True, None]}, "2001:db8::1")
    log.record("server", "server.started")
    entries = log.query()[::-1]
    assert entries[0].ts == 1_760_431_234.123456 and entries[0].target == name
    assert entries[0].details == {name: [name, 10**20, -3, True, None]}
    assert log.verify() == ChainCheck(True, 2, None, _head(entries[1]))


def test_a_clock_that_counts_whole_seconds_leaves_a_chain_that_holds(db):
    log = AuditLog(db, key=AUDIT_KEY, clock=lambda: 1_800_000_000)  # an int: the time is kept, and hashed, as a float
    log.record("server", "server.started")
    log.record("server", "server.started")
    assert log.verify().ok and [e.ts for e in log.query()] == [1_800_000_000.0] * 2


def test_the_log_is_pruned_by_age(log, clock):
    log.record("server", "server.started")
    clock.now += 366 * DAY
    log.record("server", "server.started")
    assert log.prune(365) == 1 and len(log.query()) == 1
    assert log.verify().ok


def test_a_prefix_search_takes_the_characters_of_a_pattern_literally(log):
    actions = ["key_a.added", "keyXa.added", "key%a.added", f"key{BACKSLASH}a.added", "key.added"]
    for action in actions:
        log.record("server", action)

    def found(action):
        return sorted(e.action for e in log.query(action=action))

    assert found("key_a.*") == ["key_a.added"]  # an underscore is not "any character"
    assert found("key%a.*") == ["key%a.added"]  # nor a percent sign "any text"
    assert found(f"key{BACKSLASH}a.*") == [f"key{BACKSLASH}a.added"]  # nor a backslash an escape
    assert found("key*") == sorted(actions)
    assert found("key.added") == ["key.added"]  # without the star, the action itself


def test_the_log_is_a_chain_that_tampering_breaks_and_pruning_keeps(db, log, clock):
    for n in range(4):
        log.record("admin:a_first00000", "person.added", f"u_person{n:05d}")
        clock.now += DAY
    second, first = log.query()[-2:]  # the oldest two (newest first)
    newest = log.query(limit=1)[0]
    assert first.prev == "" and second.prev == first.hash
    assert log.verify() == ChainCheck(True, 4, None, _head(newest))
    with db.connection() as conn:
        conn.execute("UPDATE audit SET target = 'u_someoneelse' WHERE id = %s", (second.id,))
    assert log.verify() == ChainCheck(False, 1, second.id, _head(newest))  # changed after the fact: no longer follows
    with db.connection() as conn:
        conn.execute("UPDATE audit SET target = %s WHERE id = %s", (second.target, second.id))
    clock.now += 363 * DAY
    assert log.prune(365) == 2 and log.verify() == ChainCheck(True, 2, None, _head(newest))  # the anchor carries it
    with db.connection() as conn:
        conn.execute("DELETE FROM audit_anchor")  # the anchor dropped: the oldest entry kept follows nothing
    assert not log.verify().ok


# --- the chain is keyed ------------------------------------------------------------------------------------------


def _unkeyed(prev, entry):
    """An entry's hash as a plain SHA-256 would make it: what someone who writes the table can compute."""
    fields = [entry.ts, entry.actor, entry.action, entry.target, entry.details, entry.address]
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{prev}\n{text}".encode()).hexdigest()


def _keyed(key):
    return lambda prev, e: entry_hash(key, prev, e.ts, e.actor, e.action, e.target, e.details, e.address)


def _rewritten(db, entries, make):
    """The chain written again from ``entries[0]`` on, each hash made by ``make(prev, entry)``."""
    prev = entries[0].prev
    for entry in entries:
        digest = make(prev, entry)
        _run(db, "UPDATE audit SET prev = %s, hash = %s WHERE id = %s", prev, digest, entry.id)
        prev = digest


@pytest.mark.parametrize(
    ("make", "holds"),
    [(_unkeyed, False), (_keyed(OTHER_KEY), False), (_keyed(AUDIT_KEY), True)],
    ids=["a plain SHA-256", "another server's key", "the key (what the writer lacks)"],
)
def test_a_writer_without_the_key_cannot_make_the_chain_again(db, log, clock, make, holds):
    entries = _chain(log, clock)
    _run(db, "UPDATE audit SET target = 'u_someoneelse' WHERE id = %s", entries[1].id)
    changed = [dataclasses.replace(entries[1], target="u_someoneelse"), *entries[2:]]
    _rewritten(db, changed, make)  # the changed entry and every one after it, hashed again
    check = log.verify()
    if holds:
        assert (check.ok, check.checked) == (True, 4)  # with the key a rewrite is as good as the log's own
    else:
        assert (check.ok, check.checked, check.broken_at) == (False, 1, entries[1].id)
    assert AUDIT_KEY not in dump(db) and AUDIT_KEY.hex().encode() not in dump(db)  # the key is nowhere in the database


def test_entries_made_under_another_key_do_not_hold(db, log, clock):
    elsewhere = AuditLog(db, key=OTHER_KEY, clock=clock)
    elsewhere.record("server", "server.started")
    newest = elsewhere.query()[0]
    assert elsewhere.verify() == ChainCheck(True, 1, None, _head(newest))
    assert log.verify() == ChainCheck(False, 0, newest.id, _head(newest))


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit SET ts = ts + 1 WHERE id = %s",
        "UPDATE audit SET actor = 'admin:a_second0000' WHERE id = %s",
        "UPDATE audit SET action = 'person.removed' WHERE id = %s",
        "UPDATE audit SET target = 'u_someoneelse' WHERE id = %s",
        "UPDATE audit SET target = NULL WHERE id = %s",
        "UPDATE audit SET details = jsonb_build_object('n', 7) WHERE id = %s",
        "UPDATE audit SET details = '{}' WHERE id = %s",
        "UPDATE audit SET address = '10.9.9.9' WHERE id = %s",
        "UPDATE audit SET address = NULL WHERE id = %s",
        "UPDATE audit SET prev = 'x' WHERE id = %s",
        "UPDATE audit SET hash = 'x' WHERE id = %s",
    ],
)
def test_an_entry_changed_in_any_field_is_found(db, log, clock, change):
    entries = _chain(log, clock)
    assert _run(db, change, entries[2].id) == 1
    assert log.verify() == ChainCheck(False, 2, entries[2].id, _head(entries[3]))


def _delete_the_middle(db, entries):
    _run(db, "DELETE FROM audit WHERE id = %s", entries[2].id)
    return entries[3].id, 2


def _copied_to_the_end(db, entry):
    """A row copied as it is under the next id (the hash covers no id, so the copy holds): the new id."""
    copy = (
        "INSERT INTO audit (ts, actor, action, target, details, address, prev, hash)"
        " SELECT ts, actor, action, target, details, address, prev, hash FROM audit WHERE id = %s RETURNING id"
    )
    return _read(db, copy, entry.id)[0][0]


def _copy_an_entry_to_the_end(db, entries):
    return _copied_to_the_end(db, entries[1]), 4


def _swap_two_entries(db, entries):
    _run(db, "UPDATE audit SET id = -1 WHERE id = %s", entries[1].id)
    _run(db, "UPDATE audit SET id = %s WHERE id = %s", entries[1].id, entries[2].id)
    _run(db, "UPDATE audit SET id = %s WHERE id = -1", entries[2].id)
    return entries[1].id, 1


def _add_one_of_his_own(db, entries):
    added = (
        "INSERT INTO audit (ts, actor, action, details, prev, hash) VALUES (0, 'x', 'x.y', '{}', %s, 'x') RETURNING id"
    )
    return _read(db, added, entries[-1].hash)[0][0], 4


@pytest.mark.parametrize(
    "tamper", [_delete_the_middle, _copy_an_entry_to_the_end, _swap_two_entries, _add_one_of_his_own]
)
def test_an_entry_taken_out_of_the_middle_copied_moved_or_added_breaks_the_chain(db, log, clock, tamper):
    entries = _chain(log, clock)
    broken_at, checked = tamper(db, entries)
    check = log.verify()
    assert (check.ok, check.checked, check.broken_at) == (False, checked, broken_at)


# --- pruning, and the anchor it leaves ----------------------------------------------------------------------------

FORGED = {
    "any text": lambda e: "x",
    "the plain hash of what it says": lambda e: hashlib.sha256(f"{e.id}\n{e.hash}".encode()).hexdigest(),
    "the hash of the entry it stands for": lambda e: e.hash,
    "the tag of another server": lambda e: anchor_tag(OTHER_KEY, e.id, e.hash),
}


def _prune_by_hand(db, entries, tag):
    """The two oldest entries dropped and an anchor written from the second's hash, as someone who writes the table
    would do it: the hash is in the table, the tag is what he cannot make."""
    second = entries[1]
    _run(db, "DELETE FROM audit WHERE id <= %s", second.id)
    _run(
        db,
        "INSERT INTO audit_anchor (id, through, hash, tag) VALUES (1, %s, %s, %s)",
        second.id,
        second.hash,
        tag(second),
    )
    return second


@pytest.mark.parametrize("forged", FORGED.values(), ids=list(FORGED))
def test_a_prune_made_by_hand_breaks_the_chain(db, log, clock, forged):
    entries = _chain(log, clock)
    second = _prune_by_hand(db, entries, forged)
    assert log.verify() == ChainCheck(False, 0, second.id, _head(entries[-1]))


def test_a_prune_made_with_the_key_is_believed(db, log, clock):
    """The control of the test above: with the key the same hand-made prune holds, so the key is what it lacked."""
    entries = _chain(log, clock)
    _prune_by_hand(db, entries, lambda e: anchor_tag(AUDIT_KEY, e.id, e.hash))
    assert log.verify() == ChainCheck(True, 2, None, _head(entries[-1]))


# What each change to a real anchor leaves ``verify`` saying broke: the id the anchor says, or, when there is no anchor,
# the oldest entry kept (it follows nothing).
CHANGED_ANCHORS = {
    "its id moved": ("UPDATE audit_anchor SET through = through + 1", lambda through, oldest: through + 1),
    "its hash another entry's": (
        "UPDATE audit_anchor SET hash = (SELECT hash FROM audit ORDER BY id DESC LIMIT 1)",
        lambda through, oldest: through,
    ),
    "its hash any text": ("UPDATE audit_anchor SET hash = 'x'", lambda through, oldest: through),
    "its tag any text": ("UPDATE audit_anchor SET tag = 'x'", lambda through, oldest: through),
    "dropped": ("DELETE FROM audit_anchor", lambda through, oldest: oldest),
}


@pytest.mark.parametrize(("change", "broken_at"), CHANGED_ANCHORS.values(), ids=list(CHANGED_ANCHORS))
def test_an_anchor_changed_or_dropped_breaks_the_chain(db, log, clock, change, broken_at):
    entries = _chain(log, clock)
    clock.now += 363 * DAY
    assert log.prune(365) == 2 and log.verify().ok
    [(through,)] = _read(db, "SELECT through FROM audit_anchor")
    _run(db, change)
    assert log.verify() == ChainCheck(False, 0, broken_at(through, entries[2].id), _head(entries[-1]))


def test_a_prune_does_not_cut_at_an_entry_that_was_changed(db, log, clock, caplog):
    """A time changed by hand would make the prune cut at an entry of its writer's choosing, drop what is before it
    and write an anchor with the key's own tag: the evidence of the change would go with the entries."""
    entries = _chain(log, clock)
    clock.now += 363 * DAY  # the two oldest are over a year old
    _run(db, "UPDATE audit SET ts = 0 WHERE id = %s", entries[2].id)  # the third looks old: a cut here takes three
    with caplog.at_level(logging.WARNING, logger=audit_module.__name__):
        assert log.prune(365) == 0
    assert len(log.query()) == 4 and _read(db, "SELECT COUNT(*) FROM audit_anchor") == [(0,)]
    assert log.verify() == ChainCheck(False, 2, entries[2].id, _head(entries[3]))
    said = [r.getMessage() for r in caplog.records if r.name == audit_module.__name__]
    assert len(said) == 1 and "does not hold" in said[0]
    for kept in (entries[2].hash, entries[2].target, entries[2].actor, entries[2].address):
        assert kept not in caplog.text  # nothing of the row, which a writer could have filled with anything


def test_a_prune_that_finds_nothing_old_keeps_everything_and_writes_no_anchor(db, log, clock):
    assert log.prune(365) == 0  # no entry at all
    _chain(log, clock)
    assert log.prune(365) == 0
    assert _read(db, "SELECT COUNT(*) FROM audit_anchor") == [(0,)] and len(log.query()) == 4


def test_a_prune_does_not_take_what_an_old_entry_copied_above_the_newest_id_would(db, log, clock):
    """The hash covers no id, so a copy of an old entry under an id above the newest holds, and it is the newest entry
    that is old. A prune that cut there would drop every entry before it, the recent ones too, and write an anchor of
    its own over the copy. The run it drops is the one it has checked: the old entries from the first, up to the first
    that is not old."""
    entries = _chain(log, clock)
    clock.now += 363 * DAY  # the two oldest are over a year old
    copied = _copied_to_the_end(db, entries[0])
    assert log.prune(365) == 2
    assert [e.id for e in log.query()] == [copied, entries[3].id, entries[2].id]  # the recent ones, and the copy, stay
    assert log.verify() == ChainCheck(False, 2, copied, (copied, entries[0].hash))  # and the copy still shows


def test_a_prune_that_meets_an_old_entry_that_follows_nothing_drops_nothing(db, log, clock, caplog):
    """Every entry is old, and the last one is a copy: the run reaches it, it does not follow the entry before, and
    nothing is dropped. The log says so, once, without a word of any row."""
    entries = _chain(log, clock)
    clock.now += 400 * DAY
    copied = _copied_to_the_end(db, entries[0])
    with caplog.at_level(logging.WARNING, logger=audit_module.__name__):
        assert log.prune(365) == 0
    assert len(log.query()) == 5 and _read(db, "SELECT COUNT(*) FROM audit_anchor") == [(0,)]
    assert log.verify().broken_at == copied
    said = [r.getMessage() for r in caplog.records if r.name == audit_module.__name__]
    assert len(said) == 1 and "does not hold" in said[0]
    for kept in (entries[0].hash, entries[0].target, entries[0].actor, entries[0].address):
        assert kept not in caplog.text


RUNS_THAT_DO_NOT_HOLD = {
    "an entry taken out": lambda db, entries: _run(db, "DELETE FROM audit WHERE id = %s", entries[1].id),
    "an entry changed": lambda db, entries: _run(db, "UPDATE audit SET actor = 'x' WHERE id = %s", entries[0].id),
    "an entry that follows another": lambda db, entries: _run(
        db, "UPDATE audit SET prev = 'x' WHERE id = %s", entries[1].id
    ),
    "an entry's time made older": lambda db, entries: _run(db, "UPDATE audit SET ts = 0 WHERE id = %s", entries[4].id),
}


@pytest.mark.parametrize("tamper", RUNS_THAT_DO_NOT_HOLD.values(), ids=list(RUNS_THAT_DO_NOT_HOLD))
def test_a_run_of_old_entries_with_a_link_that_does_not_hold_is_not_dropped(db, log, clock, tamper):
    entries = _chain(log, clock, 5)
    clock.now += 400 * DAY  # all five are old
    tamper(db, entries)
    before = len(log.query())
    assert log.prune(365) == 0
    assert len(log.query()) == before and _read(db, "SELECT COUNT(*) FROM audit_anchor") == [(0,)]


def test_a_prune_stops_at_the_first_entry_that_is_not_old_whatever_comes_after_it(db, log, clock):
    """Instances whose clocks differ write times out of order, so an entry can be old and come after one that is not:
    it is kept until the one before it is old too, and no entry younger than the retention is dropped."""
    start = clock.now
    for days, action in ((0, "a.one"), (10, "a.two"), (1, "a.three"), (12, "a.four")):
        clock.now = start + days * DAY
        log.record("server", action)
    clock.now = start + 370 * DAY  # the retention ends at day 5: a.one and a.three are old, a.two is not
    assert log.prune(365) == 1
    assert [e.action for e in log.query()] == ["a.four", "a.three", "a.two"] and log.verify().ok
    clock.now = start + 380 * DAY  # day 15: all are old now
    assert log.prune(365) == 3 and log.query() == [] and log.verify().ok


@pytest.mark.parametrize(("total", "old"), [(10, 8), (6, 6), (3, 3), (7, 0)])
def test_a_prune_goes_on_in_runs_of_a_page_each_from_the_anchor_of_the_one_before(
    db, log, clock, monkeypatch, total, old
):
    monkeypatch.setattr(audit_module, "PAGE", 3)
    entries = _chain(log, clock, total)
    clock.now = entries[0].ts + (365 + old - 0.5) * DAY  # the retention ends half a day before entry ``old`` + 1
    assert log.prune(365) == old
    left = log.query()
    assert len(left) == total - old and log.verify().ok
    if old:
        assert _read(db, "SELECT through FROM audit_anchor") == [(entries[old - 1].id,)]


def test_a_second_prune_goes_on_from_the_anchor_of_the_first(db, log, clock):
    entries = _chain(log, clock, 6)
    clock.now = entries[0].ts + (365 + 1.5) * DAY  # two entries are old
    assert log.prune(365) == 2
    clock.now = entries[0].ts + (365 + 3.5) * DAY  # and two more
    assert log.prune(365) == 2
    assert len(log.query()) == 2 and log.verify() == ChainCheck(True, 2, None, _head(entries[-1]))


def test_a_prune_does_not_write_an_anchor_over_one_that_does_not_hold(db, log, clock, caplog):
    """The anchor is where the run it drops begins: one a writer made by hand is no start, and a prune that wrote its own
    in its place would take the evidence with it."""
    entries = _chain(log, clock, 6)
    clock.now = entries[0].ts + (365 + 1.5) * DAY
    assert log.prune(365) == 2
    _run(db, "UPDATE audit_anchor SET tag = 'x'")
    clock.now = entries[0].ts + (365 + 3.5) * DAY  # two more are old
    with caplog.at_level(logging.WARNING, logger=audit_module.__name__):
        assert log.prune(365) == 0
    assert _read(db, "SELECT tag FROM audit_anchor") == [("x",)] and len(log.query()) == 4
    assert log.verify() == ChainCheck(False, 0, entries[1].id, _head(entries[-1]))
    assert len([r for r in caplog.records if r.name == audit_module.__name__]) == 1


def _while_the_run_is_checked(monkeypatch, act):
    """``act()`` once, while a prune checks the run it would drop (after it read the entries and the anchor, before it
    drops anything): what a writer of the table, who takes no lock of the chain's, can do at that moment. The list
    holds what ``act`` answered."""
    holds, done = AuditLog._holds, []

    def checking(self, row):
        found = holds(self, row)
        if not done:
            done.append(act())
        return found

    monkeypatch.setattr(AuditLog, "_holds", checking)
    return done


def _waits(db, statement, *args):
    """Whether ``statement``, made on a connection of its own, waits for a row another transaction holds (given 200
    ms); when it does not wait, it is made."""
    try:
        with db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout = '200ms'")
            conn.execute(statement, args)
    except psycopg.errors.LockNotAvailable:
        return True
    return False


def test_a_row_written_by_hand_while_a_prune_checks_its_run_is_not_dropped_with_it(db, log, clock, monkeypatch):
    """A prune drops the entries it checked, by their ids: a row that a writer of the table puts below the run's end
    meanwhile (under an id no entry has) is no entry it checked, and stays to show in the chain."""
    entries = _chain(log, clock)
    clock.now += 363 * DAY  # the two oldest are over a year old
    added = "INSERT INTO audit (id, ts, actor, action, details, prev, hash) VALUES (0, 0, 'x', 'x.y', '{}', 'x', 'x')"
    done = _while_the_run_is_checked(monkeypatch, lambda: _run(db, added))
    assert log.prune(365) == 2 and done == [1]
    monkeypatch.undo()
    assert _read(db, "SELECT id FROM audit ORDER BY id") == [(0,), (entries[2].id,), (entries[3].id,)]
    assert log.verify().broken_at == 0  # the row it did not check is there to be found


def test_the_run_a_prune_checks_cannot_be_changed_before_it_is_dropped(db, log, clock, monkeypatch):
    """The entries a prune checks are read as its own (FOR UPDATE): a change a writer makes to one of them meanwhile
    waits until they are gone, so that what is dropped is what was checked."""
    entries = _chain(log, clock)
    clock.now += 363 * DAY
    change = "UPDATE audit SET actor = 'x' WHERE id = %s"
    done = _while_the_run_is_checked(monkeypatch, lambda: _waits(db, change, entries[1].id))
    assert log.prune(365) == 2 and done == [True]
    monkeypatch.undo()
    assert log.verify() == ChainCheck(True, 2, None, _head(entries[-1]))


def test_the_anchor_a_prune_goes_on_from_cannot_be_changed_before_it_writes_the_next(db, log, clock, monkeypatch):
    entries = _chain(log, clock, 6)
    clock.now = entries[0].ts + (365 + 1.5) * DAY
    assert log.prune(365) == 2  # the anchor the next prune goes on from
    clock.now = entries[0].ts + (365 + 3.5) * DAY  # two more are old
    done = _while_the_run_is_checked(monkeypatch, lambda: _waits(db, "UPDATE audit_anchor SET tag = 'x'"))
    assert log.prune(365) == 2 and done == [True]
    monkeypatch.undo()
    assert log.verify() == ChainCheck(True, 2, None, _head(entries[-1]))


def test_an_anchor_written_by_hand_while_the_first_prune_checks_its_run_is_not_written_over(
    db, log, clock, monkeypatch, caplog
):
    """There was no anchor when the prune began: one that a writer puts there meanwhile is no anchor it checked, and
    writing its own over it would take the evidence with it. Nothing is dropped, and the log says so."""
    entries = _chain(log, clock)
    clock.now += 363 * DAY
    forged = "INSERT INTO audit_anchor (id, through, hash, tag) VALUES (1, %s, %s, 'x')"
    _while_the_run_is_checked(monkeypatch, lambda: _run(db, forged, entries[1].id, entries[1].hash))
    with caplog.at_level(logging.WARNING, logger=audit_module.__name__):
        assert log.prune(365) == 0
    monkeypatch.undo()
    assert _read(db, "SELECT tag FROM audit_anchor") == [("x",)] and len(log.query()) == 4
    assert log.verify() == ChainCheck(False, 0, entries[1].id, _head(entries[-1]))
    said = [r.getMessage() for r in caplog.records if r.name == audit_module.__name__]
    assert len(said) == 1 and "does not hold" in said[0]
    for kept in (entries[1].hash, entries[1].target, entries[1].actor, entries[1].address):
        assert kept not in caplog.text


@pytest.mark.parametrize(("ahead", "said"), [(10 * 365 * DAY, 1), (60, 0)], ids=["ten years", "a minute"])
def test_a_prune_held_back_by_an_entry_dated_ahead_of_its_clock_says_so(db, log, clock, caplog, ahead, said):
    """An entry written by an instance whose clock ran far ahead is not old until that time: the prune stops at it, and
    keeps every entry after it, old or not. The log says so in words that hold nothing of the entry; an instance whose
    clock is a little ahead of another's (the bus lets them differ by a minute) is no news."""
    start = clock.now
    log.record("server", "a.one")
    clock.now = start + ahead
    log.record("server", "a.two")
    clock.now = start + DAY
    log.record("server", "a.three")
    clock.now = start + 400 * DAY  # a.one and a.three are old, a.two holds a.three back (when it is far ahead)
    with caplog.at_level(logging.WARNING, logger=audit_module.__name__):
        dropped = log.prune(365)
    assert dropped == (1 if said else 3) and log.verify().ok
    lines = [r.getMessage() for r in caplog.records if r.name == audit_module.__name__]
    assert len(lines) == said and all("ahead of this instance's clock" in line for line in lines)
    assert "a.two" not in caplog.text


def test_a_query_answers_one_entry_at_least_and_max_query_at_most(log, clock, monkeypatch):
    _chain(log, clock)
    monkeypatch.setattr(audit_module, "MAX_QUERY", 2)
    assert [len(log.query(limit=asked)) for asked in (0, -5, 1, 2, 10)] == [1, 1, 1, 2, 2]


# --- the walk -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 3, 4, 6, 7])
def test_the_chain_is_walked_in_pages(log, clock, monkeypatch, count):
    monkeypatch.setattr(audit_module, "PAGE", 3)
    read = []
    page = AuditLog._page

    def counting(self, conn, after):
        rows = page(self, conn, after)
        read.append(len(rows))
        return rows

    monkeypatch.setattr(AuditLog, "_page", counting)
    entries = _chain(log, clock, count)
    expected = _head(entries[-1]) if entries else None
    assert log.verify() == ChainCheck(True, count, None, expected)
    assert max(read, default=0) <= 3 and sum(read) == count  # never more than a page at a time, each entry once
    assert len(read) == count // 3 + 1  # a short page, an empty one at last, ends it


def test_a_walk_sees_the_log_as_it_was_when_it_began(log, clock, monkeypatch):
    """A prune that commits between two pages takes the entries the next page would begin at: read as the log is at
    that moment, the walk would find the chain broken where it is not."""
    monkeypatch.setattr(audit_module, "PAGE", 2)
    entries = _chain(log, clock, 6)
    clock.now += 363 * DAY  # the first four are over a year old
    page = AuditLog._page
    changed = []

    def interleaved(self, conn, after):
        rows = page(self, conn, after)
        if after is None:  # the first page is read: the log changes on other connections
            changed.append((log.prune(365), log.record("server", "server.started")))
        return rows

    monkeypatch.setattr(AuditLog, "_page", interleaved)
    assert log.verify() == ChainCheck(True, 6, None, _head(entries[-1]))
    assert changed == [(4, None)]
    monkeypatch.undo()
    after = log.query()
    assert len(after) == 3 and log.verify() == ChainCheck(True, 3, None, _head(after[0]))  # both changes were made


def _written_at_once(log, monkeypatch):
    """Six threads write four entries each, with a pause between reading the last hash and writing the entry, which is
    the room a fork needs: the entries of the log, newest first."""
    last = AuditLog._last

    def slowly(conn):
        found = last(conn)
        time.sleep(0.02)
        return found

    monkeypatch.setattr(AuditLog, "_last", staticmethod(slowly))

    def work(worker):
        for n in range(4):
            log.record(f"admin:a_worker{worker:04d}", "person.added", f"u_person{n:05d}")

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(work, range(6)))
    return log.query()


def test_entries_written_at_once_still_follow_each_other(log, monkeypatch):
    entries = _written_at_once(log, monkeypatch)
    assert len(entries) == 24 and len({e.prev for e in entries}) == 24  # no two entries follow the same one
    assert log.verify() == ChainCheck(True, 24, None, _head(entries[0]))


PIN_OR_NOT = pytest.mark.parametrize("pin", [True, False], ids=["the pin holds", "the pin is gone"])
STRICTER = pytest.mark.parametrize("isolation", ["repeatable read", "serializable"])


@PIN_OR_NOT
@STRICTER
def test_entries_written_at_once_follow_each_other_in_sessions_that_default_to_a_stricter_isolation(
    role_url, schema, clock, monkeypatch, pin, isolation
):
    """In REPEATABLE READ the snapshot is taken by the lock's own statement, before it waits: the entry that waited reads
    the hash that was the last then, and two entries follow one; in SERIALIZABLE one of two is refused and its action goes
    unrecorded. The server's connections are READ COMMITTED whatever the role defaults to (the pin), and a record says so
    itself when they are not."""
    url = role_url(default_transaction_isolation=isolation)
    with closing(Database(url, schema=schema, max_size=4)) as home:
        migrate(home)
        if not pin:
            lose_pins(monkeypatch, schema, "default_transaction_isolation")  # connections made from here on
        with closing(Database(url, schema=schema, max_size=4)) as db:
            with db.connection() as conn:  # the control: what this test's connections default to
                found = conn.execute("SHOW default_transaction_isolation").fetchone()
            assert found == (("read committed",) if pin else (isolation,))
            log = AuditLog(db, key=AUDIT_KEY, clock=clock)
            entries = _written_at_once(log, monkeypatch)
            assert len(entries) == 24 and len({e.prev for e in entries}) == 24
            assert log.verify() == ChainCheck(True, 24, None, _head(entries[0]))


@PIN_OR_NOT
@STRICTER
def test_two_prunes_at_once_both_succeed_in_sessions_that_default_to_a_stricter_isolation(
    role_url, schema, clock, monkeypatch, pin, isolation
):
    """Both wait for the chain's lock with the snapshot they took before it: the one that goes second would delete rows
    that the first deleted after that snapshot, and PostgreSQL would refuse it in REPEATABLE READ and SERIALIZABLE."""
    url = role_url(default_transaction_isolation=isolation)
    with closing(Database(url, schema=schema, max_size=4)) as home:
        migrate(home)
        if not pin:
            lose_pins(monkeypatch, schema, "default_transaction_isolation")
        with closing(Database(url, schema=schema, max_size=4)) as db:
            log = AuditLog(db, key=AUDIT_KEY, clock=clock)
            _chain(log, clock)
            clock.now += 363 * DAY  # the two oldest are over a year old
            holder, pool = psycopg.connect(url), ThreadPoolExecutor(2)
            try:
                # The chain is busy: both prunes arrive, and wait.
                holder.execute("SELECT pg_advisory_xact_lock(%s)", (log._lock,))
                futures = [pool.submit(log.prune, 365) for _ in range(2)]
                assert _until(lambda: _waiting(db, log._lock) == 2)
            finally:
                holder.close()  # closing rolls back: the lock is let go and both go on
            try:
                dropped = sorted(future.result(timeout=60) for future in futures)
            finally:
                pool.shutdown()
            assert dropped == [0, 2] and log.verify().ok


@PIN_OR_NOT
def test_entries_hold_in_sessions_that_default_to_a_short_float(role_url, schema, clock, monkeypatch, pin):
    """With ``extra_float_digits`` at 0 PostgreSQL gives a time back with 15 digits: every entry would read as changed,
    and the prune would refuse for good. The server's connections are told 3 whatever the role defaults to."""
    clock.now = 1_760_431_234.123456
    url = role_url(extra_float_digits="0")
    with closing(Database(url, schema=schema)) as home:
        migrate(home)
        if not pin:
            lose_pins(monkeypatch, schema, "extra_float_digits")
        with closing(Database(url, schema=schema)) as db:
            log = AuditLog(db, key=AUDIT_KEY, clock=clock)
            log.record("server", "server.started")
            clock.now += 1.000001
            log.record("server", "server.started")
            clock.now += 400 * DAY
            assert log.verify().ok is pin
            assert log.prune(365) == (2 if pin else 0)


def test_the_chain_lock_is_the_schemas_own(db, settings, clock, monkeypatch):
    """Two servers in two schemas of one database do not wait for each other's entries."""
    name = db.schema + "_b"
    other = Database(settings.database_url, schema=name, max_size=2)
    try:
        migrate(other)
        here, there = AuditLog(db, key=AUDIT_KEY, clock=clock), AuditLog(other, key=AUDIT_KEY, clock=clock)
        inside, release, calls = threading.Event(), threading.Event(), []
        last = AuditLog._last

        def holding(conn):  # the first entry stops with its lock taken
            found = last(conn)
            calls.append(1)
            if len(calls) == 1:
                inside.set()
                assert release.wait(30)
            return found

        monkeypatch.setattr(AuditLog, "_last", staticmethod(holding))
        first = threading.Thread(target=here.record, args=("server", "server.started"))
        second = threading.Thread(target=there.record, args=("server", "server.started"))
        first.start()
        try:
            assert inside.wait(30)
            second.start()
            second.join(10)
            assert not second.is_alive()  # the other schema's entry was written while this one's lock was held
        finally:
            release.set()
            first.join(30)
            second.join(30)
        assert here.verify().ok and there.verify().ok
        assert len(here.query()) == len(there.query()) == 1
    finally:
        other.close()
        with psycopg.connect(DATABASE, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


# --- what the chain cannot see, and the head that shows it --------------------------------------------------------


def test_what_the_chain_cannot_see_is_said_in_the_module_and_is_so(db, log, clock):
    said = audit_module.__doc__
    assert said and "cannot see" in said and "head" in said and "copy kept elsewhere" in said
    entries = _chain(log, clock)
    copy = log.verify().head  # what a copy kept elsewhere holds
    assert copy == _head(entries[-1])
    _run(db, "DELETE FROM audit WHERE id > %s", entries[1].id)  # the two newest taken off the end
    after = log.verify()
    assert (after.ok, after.checked) == (True, 2)  # the chain that is left holds...
    assert after.head == _head(entries[1]) != copy  # ...and ends before the copy's head: only the copy shows it
    _run(db, "DELETE FROM audit")  # and the whole table, with no anchor to find
    assert log.verify() == ChainCheck(True, 0, None, None)


def test_the_head_is_the_newest_entry_and_once_all_are_pruned_the_anchor(log, clock):
    assert log.verify() == ChainCheck(True, 0, None, None)  # a log that never held an entry
    entries = _chain(log, clock, 2)
    assert log.verify().head == _head(entries[1])
    clock.now += 400 * DAY
    assert log.prune(365) == 2 and log.query() == []
    assert log.verify() == ChainCheck(True, 0, None, _head(entries[1]))  # the last entry the chain held
    log.record("server", "server.started")
    new = log.query()[0]
    assert new.prev == entries[1].hash  # the chain goes on from the anchor
    assert log.verify() == ChainCheck(True, 1, None, _head(new))


@pytest.mark.parametrize(
    "details",
    ["[1, 2]", "5", '"text"', "null", '{"n": ' + "1" * 5000 + "}", '{"a": ' + "[" * 5000 + "]" * 5000 + "}"],
    ids=["a list", "a number", "a text", "null", "a number of 5,000 digits", "a nesting 5,000 deep"],
)
def test_a_row_the_walk_cannot_read_is_a_break_and_not_an_error(db, log, clock, details):
    """Whoever writes the table can put into ``details`` what Python's JSON reader refuses or what is no object: that
    makes a broken entry, and ends neither the walk nor a read of the log with an error."""
    entries = _chain(log, clock, 3)
    _run(
        db,
        "INSERT INTO audit (ts, actor, action, details, prev, hash) VALUES (0, 'x', 'x.y', %s::jsonb, %s, 'x')",
        details,
        entries[-1].hash,
    )
    check = log.verify()
    assert (check.ok, check.checked) == (False, 3) and check.broken_at == log.query(limit=1)[0].id
    assert log.query(limit=1)[0].details == {}


# --- what a record takes in ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"seconds": 1.5},
        {"seconds": 2.0},
        {"n": 1e20},
        {"a": {"b": [1, {"ratio": 0.5}]}},
        {"n": float("nan")},
        {"n": float("inf")},
    ],
    ids=["a fraction", "a whole float", "1e20", "nested", "NaN", "infinity"],
)
def test_a_float_in_the_details_is_refused_and_nothing_is_recorded(log, details):
    """PostgreSQL's JSONB writes some floats back as other numbers (1e20 as a whole number): a float in the details
    would read as a change. The details are names, ids, counts and flags."""
    with pytest.raises(TypeError, match="float"):
        log.record("server", "server.started", None, details)
    assert log.query() == []


def test_why_a_float_is_refused_jsonb_gives_some_back_as_other_numbers(db, log):
    """The entry a float would make if it were let in: its hash is made of the float, and the table gives back the
    whole number PostgreSQL's JSONB wrote it as (1e+20 and 100000000000000000000), so the chain would read as broken."""
    details = {"n": 1e20}
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO audit (ts, actor, action, details, prev, hash) VALUES (1, 'server', 'x.y', %s, '', %s)",
            (Jsonb(details), entry_hash(AUDIT_KEY, "", 1.0, "server", "x.y", None, details, None)),
        )
    assert log.query()[0].details == {"n": 10**20} and not log.verify().ok


def test_counts_flags_names_and_nesting_are_kept_as_given_and_hold(log):
    details = {"count": 3, "big": 10**30, "negative": -7, "flag": True, "off": False, "none": None, "name": "laptop"}
    details |= {"list": [1, "a", None, True, {"z": 1, "a": [2]}], "nested": {"b": 1, "a": {"c": [3]}}}
    log.record("server", "server.started", None, details)
    [entry] = log.query()
    assert entry.details == details and entry.details["flag"] is True and entry.details["off"] is False
    assert log.verify().ok


def test_no_text_a_caller_gives_makes_an_action_go_unrecorded(log):
    """PostgreSQL's TEXT and JSONB refuse a zero byte and the hash cannot encode a lone surrogate: each is kept as
    U+FFFD, in the actor, the action, the target, the address and every string of the details, keys included."""
    for bad in (NUL, LONE):
        details = {f"k{bad}": f"v{bad}", "list": [f"a{bad}", {f"deep{bad}": bad}]}
        log.record(f"admin:a_{bad}", f"person.{bad}added", f"u_{bad}x", details, f"10.0.{bad}.5")
    entries = log.query()
    assert len(entries) == 2
    for entry in entries:
        assert entry.actor == f"admin:a_{SUBSTITUTE}" and entry.action == f"person.{SUBSTITUTE}added"
        assert entry.target == f"u_{SUBSTITUTE}x" and entry.address == f"10.0.{SUBSTITUTE}.5"
        assert entry.details == {
            f"k{SUBSTITUTE}": f"v{SUBSTITUTE}", "list": [f"a{SUBSTITUTE}", {f"deep{SUBSTITUTE}": SUBSTITUTE}]
        }  # fmt: skip
    assert log.verify().ok
    # The same text in a question finds them, and is no error either.
    assert len(log.query(actor=f"admin:a_{NUL}")) == len(log.query(action=f"person.{LONE}*")) == 2


def test_details_that_are_no_mapping_of_texts_to_json_are_refused(log):
    for details in ([1, 2], {1: "a"}, {"a": {1, 2}}, {"a": b"bytes"}, {"a": object()}):
        with pytest.raises(TypeError):
            log.record("server", "server.started", None, details)
    assert log.query() == []


@pytest.mark.parametrize(
    "key",
    [b"", b"k" * 31, b"k" * 33, "k" * 32, None],
    ids=["empty", "31 bytes", "33 bytes", "32 characters of text", "none"],
)
def test_a_key_that_is_not_32_bytes_is_refused_without_naming_it(db, clock, key):
    with pytest.raises(ValueError, match="32 bytes") as refused:
        AuditLog(db, key=key, clock=clock)
    assert "kkkk" not in str(refused.value) and "kkkk" not in repr(refused.value)


# --- an entry written in the transaction of the change it records --------------------------------------------------


def _added(conn, identity_id):
    """A change: a person added."""
    conn.execute(
        "INSERT INTO identities (id, kind, name, created) VALUES (%s, 'person', 'Emily Carter', 0)", (identity_id,)
    )


def _added_recorded_and_stopped(db, log, identity_id):
    """A change and its entry, and then the transaction fails."""
    with db.transaction() as conn:
        _added(conn, identity_id)
        log.record("admin:a_first00000", "person.added", identity_id, conn=conn)
        raise RuntimeError("stopped after the entry was written")


def test_an_entry_written_in_the_callers_transaction_commits_and_rolls_back_with_it(db, log):
    with db.transaction() as conn:
        _added(conn, "u_emilycarte")
        log.record("admin:a_first00000", "person.added", "u_emilycarte", conn=conn)
        assert log.query() == []  # not yet: it is in the transaction that has not ended
    assert _read(db, "SELECT id FROM identities") == [("u_emilycarte",)]
    assert [e.target for e in log.query()] == ["u_emilycarte"]
    with pytest.raises(RuntimeError, match="stopped"):
        _added_recorded_and_stopped(db, log, "u_danielokaf")
    assert _read(db, "SELECT id FROM identities") == [("u_emilycarte",)]  # neither the change...
    assert [e.target for e in log.query()] == ["u_emilycarte"]  # ...nor its entry
    log.record("server", "server.started")
    assert log.verify().ok and len(log.query()) == 2  # and the chain goes on from the one that was kept


def test_the_chains_lock_is_held_by_an_entry_in_the_callers_transaction_until_it_ends(db, log):
    pool = ThreadPoolExecutor(1)
    try:
        with db.transaction() as conn:
            log.record("admin:a_first00000", "person.added", "u_emilycarte", conn=conn)
            second = pool.submit(log.record, "server", "server.started")
            assert _until(lambda: _waiting(db, log._lock) == 1)  # it waits for the lock this transaction took
            assert not second.done()
        second.result(timeout=30)
    finally:
        pool.shutdown()
    first, last = log.query()[::-1]
    assert last.prev == first.hash and log.verify() == ChainCheck(True, 2, None, _head(last))


def _recorded_in_autocommit(log):
    with psycopg.connect(DATABASE, autocommit=True, options=f"-c search_path={log.db.schema}") as conn:
        log.record("server", "server.started", conn=conn)


def test_an_entry_is_not_written_in_a_connection_in_autocommit_mode(log):
    """There the lock would end with its statement and two entries could follow one."""
    with pytest.raises(ValueError, match="autocommit"):
        _recorded_in_autocommit(log)
    assert log.query() == []


def _recorded_after_a_read_in_repeatable_read(db, log):
    with db.transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SELECT 1")
        log.record("server", "server.started", conn=conn)


def test_an_entry_does_not_join_a_repeatable_read_transaction_that_has_read(db, log):
    """The chain's last hash is read after the lock is waited for: only READ COMMITTED sees what the entry before wrote.
    A transaction that cannot be made so is refused, never joined to fork the chain."""
    with pytest.raises(psycopg.errors.ActiveSqlTransaction):
        _recorded_after_a_read_in_repeatable_read(db, log)
    assert log.query() == []
