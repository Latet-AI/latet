"""The SIEM queue: each output's events in order, taken off only once delivered, a failure remembered with when to try
again, the newest kept when a receiver is gone for long, and an output removed taking its queue along; the same in
memory and in PostgreSQL, where an event committed late is not lost."""

import contextlib
import json
import sys
import threading
import time

import pytest

from latet.ee.events.schema import NotAnEvent, checked
from latet.ee.events.store import (
    ADMIN_DROPPED,
    ADMIN_KEPT,
    GAP,
    OTHER,
    SLACK,
    MemoryEvents,
    QueueState,
    UnreadableQueue,
    gap_event,
    is_gap,
    span_of,
)
from latet.events import make_event

T0 = 1_790_000_000.0  # 2026-09-21T14:13:20Z
HOUR = 3600.0


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.server)])
def events(request):
    if request.param == "memory":
        return MemoryEvents()
    from latet.ee.events.pg import PgEvents

    return PgEvents(request.getfixturevalue("database"))


def _events(n, start=0):
    return [{"id": f"{i:032x}", "n": i} for i in range(start, start + n)]


def _ns(batch):
    return [event["n"] for _, event in batch]


def _seqs(batch):
    return [seq for seq, _ in batch]


ORIGIN = "https://latet.acme.lan"


def _admin(i):
    """The `admin` event of the log's entry ``i``, with its place in the chain as the log gives it."""
    return {"id": f"{i:032x}", "type": "admin", "outcome": "person.added", "n": i,
            "chain": {"prev": "" if i == 1 else f"{i - 1:064x}", "hash": f"{i:064x}"}}  # fmt: skip


def test_past_the_bound_the_oldest_entries_collapse_into_one_gap_marker_extended_never_stacked(events):
    events.put(["hook"], [_admin(i) for i in range(1, 6)], at=T0, refs=list(range(1, 6)))
    events.put(["hook"], _events(1, start=90), at=T0)  # another type: never collapsed
    assert events.collapse("hook", 3, origin=ORIGIN, at=T0) == (1, 3, 3)
    head, *rest = [event for _, event in events.peek("hook", 10)]
    assert (head["outcome"], head["reason"], head["stopped"]) == ("siem.gap", "audit:1-3", 3)
    assert head["chain"] == {"prev": "", "hash": f"{3:064x}"} and [e["n"] for e in rest] == [4, 5, 90]
    events.put(["hook"], [_admin(6), _admin(7)], at=T0, refs=[6, 7])
    assert events.collapse("hook", 3, origin=ORIGIN, at=T0) == (1, 5, 5)  # the marker extended, never a second
    head, *rest = [event for _, event in events.peek("hook", 10)]
    assert (head["reason"], head["stopped"], head["chain"]["hash"]) == ("audit:1-5", 5, f"{5:064x}")
    assert [e["n"] for e in rest] == [90, 6, 7]
    assert (events.state("hook").admin, events.state("hook").collapsed) == (3, 5)  # `latet server status`: FAIL
    noted: list[tuple[str, dict]] = []
    events.put(["hook"], [_admin(8)], at=T0, refs=[8])
    assert events.collapse("hook", 3, origin=ORIGIN, at=T0, noted=lambda conn, action, details: noted.append(
        (action, dict(details)))) == (1, 6, 6)  # fmt: skip
    assert noted == [("siem.gap", {"output": "hook", "from": 1, "to": 6, "count": 6})]  # its entry, with it (R3-1)
    assert events.collapse("hook", 3, origin=ORIGIN, at=T0) is None  # within the bound
    events.done("hook", _seqs(events.peek("hook", 1)), at=T0)  # the output delivers again: the marker went out
    assert events.state("hook").collapsed == 0


def test_a_collapse_whose_entry_cannot_be_written_leaves_no_marker(events):
    events.put(["hook"], [_admin(i) for i in range(1, 4)], at=T0, refs=[1, 2, 3])

    def failing(conn, action, details):
        raise OSError("the database restarts")  # R3-1: the marker and its entry of the log, or neither

    with pytest.raises(OSError, match="the database restarts"):
        events.collapse("hook", 1, origin=ORIGIN, at=T0, noted=failing)
    assert _ns(events.peek("hook", 10)) == [1, 2, 3] and events.state("hook").collapsed == 0
    assert events.markers() == []


def test_a_collapse_writes_its_entry_of_the_log_in_its_own_transaction(events):
    """On a server, ``noted`` is given the collapse's own connection, where the marker is already written: the entry
    of the administration log commits with the marker or not at all (R3-1). In memory there is none."""
    events.put(["hook"], [_admin(i) for i in range(1, 4)], at=T0, refs=[1, 2, 3])
    seen = []

    def noting(conn, action, details):
        if conn is None:
            seen.append(None)
            return
        found = conn.execute("SELECT event FROM ee_siem_queue WHERE output = 'hook' ORDER BY seq").fetchall()
        seen.append([json.loads(text)["reason"] for (text,) in found if json.loads(text)["outcome"] == GAP])

    events.collapse("hook", 1, origin=ORIGIN, at=T0, noted=noting)
    assert seen == ([None] if isinstance(events, MemoryEvents) else [["audit:1-3"]])


def test_a_bound_below_one_keeps_the_marker_alone_and_records_it_once(events):
    """A marker is itself one of the log's rows: a bound below one keeps it alone, never collapses it into itself again
    with an entry of the log at each round."""
    events.put(["hook"], [_admin(1), _admin(2)], at=T0, refs=[1, 2])
    noted = []
    assert events.collapse("hook", 0, origin=ORIGIN, at=T0, noted=lambda c, a, d: noted.append(dict(d))) == (1, 2, 2)
    assert events.collapse("hook", 0, origin=ORIGIN, at=T0, noted=lambda c, a, d: noted.append(dict(d))) is None
    assert len(noted) == 1 and [e["reason"] for _, e in events.peek("hook", 10)] == ["audit:1-2"]


def test_the_gap_markers_waiting_are_listed_by_output(events):
    events.put(["hook"], [_admin(1), _admin(2)], at=T0, refs=[1, 2])
    events.put(["siem"], [_admin(3), *_events(1, start=90)], at=T0, refs=[3, None])
    events.collapse("hook", 1, origin=ORIGIN, at=T0)
    events.skip("siem", origin=ORIGIN, at=T0)
    found = sorted((output, event["reason"], event["stopped"]) for output, event in events.markers())
    assert found == [("hook", "audit:1-2", 2), ("siem", "audit:3-3", 1)]  # what `latet server log verify` checks


def _forge(events, output, text, kind=""):
    """A row written in the database by hand, in a form Latet never writes: its `type` column empty (R4-1)."""
    if isinstance(events, MemoryEvents):
        with events._lock:
            events._seq += 1
            events._queue.append((events._seq, output, T0, text, kind, None))
        return
    with events.db.transaction() as conn:
        conn.execute(
            "INSERT INTO ee_siem_queue (output, at, type, event) VALUES (%s, %s, %s, %s)", (output, T0, kind, text)
        )


def test_every_event_said_to_be_a_gap_is_listed_whatever_its_form(events):
    events.put(["hook"], [_admin(1), _admin(2)], at=T0, refs=[1, 2])
    events.collapse("hook", 1, origin=ORIGIN, at=T0)
    _forge(events, "hook", '{"type": "admin", "outcome": "siem.gap", "reason": "audit:100-100", "stopped": "1"}')
    found = [(output, event["reason"], event["stopped"]) for output, event in events.markers()]
    assert found == [("hook", "audit:1-2", 2), ("hook", "audit:100-100", "1")]  # typed '', spaced, its count text
    _forge(events, "siem", "not JSON")
    with pytest.raises(UnreadableQueue):  # R4-1: the check fails closed, never passes a row it cannot read
        events.markers()


def test_a_gap_written_with_an_escape_or_typed_otherwise_is_listed_too(events):
    """The outcome is read as JSON reads it, never as text: one spelled with an escape, in a row typed `exchange`, is a
    gap like any other (R4-1)."""
    escaped = '{"outcome":"' + chr(0x5C) + 'u0073iem.gap","reason":"audit:5-5","stopped":1}'
    _forge(events, "hook", escaped, kind="exchange")
    _forge(events, "hook", '{"outcome": "person.added"}', kind="admin")
    _forge(events, "hook", "[1, 2]", kind="admin")  # JSON, though no event: no outcome to read
    assert [(output, event["reason"]) for output, event in events.markers()] == [("hook", "audit:5-5")]


def test_a_held_entry_of_the_log_is_skipped_behind_a_marker_of_its_own(events):
    events.put(["hook"], [_admin(7), *_events(1, start=90)], at=T0, refs=[7, None])
    events.failed("hook", "it answered 400: an entry of the administration log", at=T0, retry_at=T0 + 5, blocked=True)
    assert events.skip("hook", origin=ORIGIN, at=T0) == (7, 7, 1, 0)  # R2-12: `latet server siem skip hook`
    head, after = [event for _, event in events.peek("hook", 10)]
    assert (head["outcome"], head["reason"], head["stopped"], after["n"]) == ("siem.gap", "audit:7-7", 1, 90)
    assert not events.state("hook").blocked and events.schedule("hook") == (None, 0)
    assert events.state("hook").collapsed == 0  # a skip is the administrator's, never the bound's collapse
    assert events.skip("hook", origin=ORIGIN, at=T0) == (7, 7, 1, 0)  # the marker refused in its turn: taken off, its
    # whole range said, which `siem.skipped` records (the coordinator's ruling on concern 2)
    assert _ns(events.peek("hook", 10)) == [90] and events.skip("hook", origin=ORIGIN, at=T0) is None


@pytest.mark.server
def test_a_skip_runs_in_the_callers_transaction_when_given_one(database):
    """``skip(…, conn=)`` writes in the caller's transaction, where the route records `siem.skipped`: one rolled back
    leaves the entry at the head, the queue held (R3-1)."""
    from latet.ee.events.pg import PgEvents

    queue = PgEvents(database)
    queue.put(["hook"], [_admin(7)], at=T0, refs=[7])
    queue.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, blocked=True)
    skipped = []

    def skip_then_fail():
        with database.transaction() as conn:
            skipped.append(queue.skip("hook", origin=ORIGIN, at=T0, conn=conn))
            [(text,)] = conn.execute("SELECT event FROM ee_siem_queue WHERE output = 'hook'").fetchall()
            skipped.append(json.loads(text)["reason"])  # the marker, seen in the caller's transaction
            raise OSError("the entry could not be written")

    with pytest.raises(OSError, match="the entry could not be written"):
        skip_then_fail()
    assert skipped == [(7, 7, 1, 0), "audit:7-7"]
    [(_, head)] = queue.peek("hook", 10)
    assert head["outcome"] == "person.added" and queue.state("hook").blocked is True


@pytest.mark.server
@pytest.mark.parametrize("removal", ["forget", "forget_unknown"])
def test_a_skip_racing_an_outputs_removal_never_deadlocks(database, removal):
    """Task 10's review: a removal takes the output's lock before any row, as a skip does, so that it never holds a
    queue's row while it waits for the state's row a skip holds (and the skip waits for that queue's row)."""
    from latet.ee.events.pg import _LOCK_OUTPUT, PgEvents, _queue_lock

    queue = PgEvents(database)
    queue.put(["hook"], [_admin(7)], at=T0, refs=[7])
    queue.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, blocked=True)
    done, failed = threading.Event(), []

    def remove():
        try:
            if removal == "forget":
                queue.forget(["hook"])
            else:
                queue.forget_unknown([], before=T0 + 1)
        except Exception as exc:
            failed.append(type(exc).__name__)
        finally:
            done.set()

    with database.transaction() as conn:
        conn.execute(_LOCK_OUTPUT, (_queue_lock(database.schema, "hook"),))  # the skip's first steps: the output's
        conn.execute("SELECT blocked FROM ee_siem_state WHERE output = 'hook' FOR UPDATE")  # lock, then its state row
        worker = threading.Thread(target=remove)
        worker.start()
        assert not done.wait(1.5)  # the removal waits for the output's lock, holding nothing the skip needs
        skipped = queue.skip("hook", origin=ORIGIN, at=T0, conn=conn, held_only=True)
    worker.join(10)
    assert (failed, skipped, done.is_set()) == ([], (7, 7, 1, 0), True)
    assert queue.state("hook").queued == 0 and queue.peek("hook", 10) == []  # removed after the skip committed


def test_the_logs_own_siem_gap_entry_is_an_entry_never_a_marker(events):
    recorded = {
        "id": "9" * 32,
        "type": "admin",
        "outcome": "siem.gap",
        "reason": "server",
        "rule": "hook",
        "chain": {"prev": f"{8:064x}", "hash": f"{9:064x}"},
    }  # a collapse the server recorded in the log
    events.put(["hook"], [recorded], at=T0, refs=[9])
    assert events.skip("hook", origin=ORIGIN, at=T0) == (9, 9, 1, 0)  # behind a marker of its own, never taken off
    [(_, head)] = events.peek("hook", 10)
    assert (head["reason"], head["stopped"], head["chain"]) == ("audit:9-9", 1, recorded["chain"])


def test_a_gap_marker_is_latets_form_alone_in_ascii_digits_with_a_whole_count():
    """What reads a marker's range reads the form Latet writes and nothing else (R4-1): ASCII digits, the whole reason,
    a count that is an int. Python's ``int`` reads other scripts' digits, so ``[0-9]``, never ``\\d``."""
    one = "\N{ARABIC-INDIC DIGIT ONE}"
    marker = {"type": "admin", "outcome": "siem.gap", "reason": "audit:1-2", "stopped": 2}
    assert is_gap(marker) and span_of(marker, None) == (1, 2, 2, None, None)
    for reason in (f"audit:{one}-{one}", f"audit:1-{one}", "audit:1-2 ", "audit:1-", "audit:-2", "server", None):
        assert not is_gap(marker | {"reason": reason})
        assert span_of(marker | {"reason": reason}, 7) == (7, 7, 1, None, None)  # an entry, itself, no chain
    for stopped in ("2", True, 2.0, None):
        assert not is_gap(marker | {"stopped": stopped})
    assert not is_gap(marker | {"outcome": "person.added"})


def test_a_gap_marker_is_an_admin_event_of_the_schema_the_same_on_every_instance():
    marker = gap_event(4, 9, 6, f"{3:064x}", f"{9:064x}", origin=ORIGIN, at=T0)
    assert (ADMIN_KEPT, GAP) == (1_000_000, "siem.gap") and marker == checked(marker)
    assert (marker["type"], marker["outcome"], marker["reason"], marker["stopped"]) == ("admin", GAP, "audit:4-9", 6)
    assert marker["chain"] == {"prev": f"{3:064x}", "hash": f"{9:064x}"} and marker["ts"] == "2026-09-21T14:13:20Z"
    assert marker["id"] == gap_event(4, 9, 6, f"{3:064x}", f"{9:064x}", origin=ORIGIN, at=T0 + 60)["id"]
    assert marker["id"] != gap_event(4, 9, 6, f"{3:064x}", f"{9:064x}", origin="https://other.acme.lan", at=T0)["id"]
    assert marker["id"] != gap_event(4, 8, 5, f"{3:064x}", f"{8:064x}", origin=ORIGIN, at=T0)["id"]
    assert all(marker[name] is None for name in ("identity", "kind", "group", "agent", "project", "by", "rule"))
    assert gap_event(1, 1, 1, "", "not a hash", origin=ORIGIN, at=T0)["chain"] is None  # never a chain half read


def test_events_leave_in_order_and_only_once_delivered(events):
    events.put(["splunk"], _events(5), at=T0)
    batch = events.peek("splunk", 3)
    assert _ns(batch) == [0, 1, 2]
    events.failed("splunk", "it could not be reached", at=T0, retry_at=T0 + 5)
    assert _ns(events.peek("splunk", 3)) == [0, 1, 2] and events.schedule("splunk") == (T0 + 5, 1)
    events.done("splunk", _seqs(batch), at=T0 + 60)
    assert _ns(events.peek("splunk", 10)) == [3, 4] and events.schedule("splunk") == (None, 0)
    assert events.state("splunk") == QueueState(2, last_ok=T0 + 60)


def test_done_takes_off_only_the_rows_that_still_hold_the_events_sent(events):
    events.put(["hook"], [_admin(i) for i in range(1, 7)], at=T0, refs=list(range(1, 7)))
    batch = events.peek_rows("hook", 2)  # an instance sends the entries 1 and 2...
    assert events.collapse("hook", 3, origin=ORIGIN, at=T0) == (1, 4, 4)  # ...and another collapses them meanwhile
    events.done("hook", [seq for seq, _, _ in batch], at=T0, texts=[text for _, _, text in batch])
    head = events.peek("hook", 3)
    assert is_gap(head[0][1]) and head[0][1]["reason"] == "audit:1-4" and _ns(head[1:]) == [5, 6]  # the marker stays
    rows = events.peek_rows("hook", 3)
    assert [(seq, event) for seq, event, _ in rows] == head  # peek's rows, each with its text as kept
    events.done("hook", [seq for seq, _, _ in rows], at=T0, texts=[text for _, _, text in rows])  # the same: gone
    assert events.state("hook").queued == 0


def test_a_row_is_known_by_its_text_whatever_its_id_or_its_numbers(events):
    """A row written by hand, with no id of the schema's or a number PostgreSQL's JSON cannot hold, is taken off once
    delivered as any other: never sent again for ever, nor failing each try (Task 7's fix round 2)."""
    _forge(events, "hook", '{"id": 5, "n": 1}', kind="exchange")
    _forge(events, "hook", '{"id": "' + "a" * 32 + '", "n": NaN}', kind="exchange")
    rows = events.peek_rows("hook", 10)
    assert [event["id"] for _, event, _ in rows] == [5, "a" * 32]
    events.done("hook", [seq for seq, _, _ in rows], at=T0, texts=[text for _, _, text in rows])
    assert events.state("hook").queued == 0


def _apps(i):
    return {"id": f"{i:032x}", "type": "apps", "n": i}


def test_a_skip_drops_the_run_the_receiver_refused_and_never_an_entry_of_the_log(events):
    """A head held by events the receiver refused, each alone, taking none (``held_to``: the last of them): a skip
    drops and counts those but the log's entries, up to the first entry of the log, which goes behind a gap marker of
    its own when the receiver refused it too (R2-12, Task 7's fix round 2)."""
    events.put(["hook"], [_apps(1), _apps(2)], at=T0)
    events.put(["hook"], [_admin(7)], at=T0, refs=[7])
    events.put(["hook"], [{"id": "9" * 32, "type": "exchange", "n": 90}], at=T0)
    rows = events.peek("hook", 10)
    events.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, held_to=rows[2][0])  # the two, then the entry
    state = events.state("hook")
    assert state.held_to == rows[2][0] and not state.blocked
    assert events.skip("hook", origin=ORIGIN, at=T0) == (7, 7, 1, 2)  # the entry behind its marker, two dropped
    head, after = [event for _, event in events.peek("hook", 10)]
    assert (head["reason"], head["stopped"], after["n"]) == ("audit:7-7", 1, 90)
    state = events.state("hook")
    assert state.dropped == 2 and state.held_to is None and events.schedule("hook") == (None, 0)
    assert events.skip("hook", origin=ORIGIN, at=T0) == (7, 7, 1, 0)  # the marker, as before: taken off
    assert events.skip("hook", origin=ORIGIN, at=T0) is None  # nothing held: an event at the head stays


def test_a_skip_asked_for_a_held_queue_alone_passes_nothing_that_merely_waits(events):
    """Task 10's route: a skip passes only what the receiver refused (``blocked``, or a run refused: ``held_to``), read
    under the queue's lock; an entry of the log merely waiting its turn is never put behind a marker."""
    events.put(["hook"], [_admin(7), *_events(1, start=90)], at=T0, refs=[7, None])
    events.failed("hook", "it could not be reached", at=T0, retry_at=T0 + 5)  # a failure, nothing refused
    assert events.skip("hook", origin=ORIGIN, at=T0, held_only=True) is None
    head, _ = [event for _, event in events.peek("hook", 10)]
    assert head["outcome"] == "person.added" and events.schedule("hook") == (T0 + 5, 1)  # untouched
    events.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, blocked=True)
    assert events.skip("hook", origin=ORIGIN, at=T0, held_only=True) == (7, 7, 1, 0)
    assert events.skip("hook", origin=ORIGIN, at=T0, held_only=True) is None  # the marker waits its turn
    events.put(["held"], [_apps(1), _apps(2)], at=T0)
    rows = events.peek("held", 10)
    events.failed("held", "it answered 400", at=T0, retry_at=T0 + 5, held_to=rows[1][0])  # a run refused, each alone
    assert events.skip("held", origin=ORIGIN, at=T0, held_only=True) == (None, None, 0, 2)
    assert events.skip("nothing", origin=ORIGIN, at=T0, held_only=True) is None  # an output with no state at all


def test_a_skip_stops_where_the_refused_run_ends(events):
    events.put(["hook"], [_apps(1), _apps(2), _apps(3)], at=T0)
    events.put(["hook"], [_admin(8)], at=T0, refs=[8])
    rows = events.peek("hook", 10)
    events.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, held_to=rows[1][0])  # it refused two, alone
    assert events.skip("hook", origin=ORIGIN, at=T0) == (None, None, 0, 2)
    assert [event["n"] for _, event in events.peek("hook", 10)] == [3, 8]  # never tried: neither dropped nor marked
    assert events.state("hook").dropped == 2 and events.admin_waiting("hook") == 1


def test_a_failure_holds_its_refused_run_until_a_delivery_or_another_failure(events):
    events.put(["hook"], _events(2), at=T0)
    seq = events.peek("hook", 1)[0][0]
    events.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, held_to=seq)
    assert events.state("hook").held_to == seq
    events.failed("hook", "it could not be reached", at=T0 + 5, retry_at=T0 + 15)  # the last try says what holds
    assert events.state("hook").held_to is None
    events.failed("hook", "it answered 400", at=T0 + 15, retry_at=T0 + 45, held_to=seq)
    events.done("hook", [seq], at=T0 + 45)
    assert events.state("hook").held_to is None


def test_failures_in_a_row_are_counted_and_the_first_one_dated(events):
    events.put(["hook"], _events(1), at=T0)
    events.failed("hook", "it answered 503", at=T0, retry_at=T0 + 5)
    events.failed("hook", "it did not answer in time", at=T0 + 5, retry_at=T0 + 15)
    state = events.state("hook")
    assert (state.failing_since, state.last_error, state.retry_at, state.tries) == (
        T0,
        "it did not answer in time",
        T0 + 15,
        2,
    )


def test_each_output_gets_what_was_written_with_the_counts(events):
    written = [checked(make_event("exchange", clock=lambda: T0, masked={"EMAIL": 1})) for _ in range(2)]
    assert events.write("a" * 32, written, ["splunk", "syslog"], at=T0)
    assert [e for _, e in events.peek("splunk", 10)] == written == [e for _, e in events.peek("syslog", 10)]


def test_each_output_gets_the_events_as_the_log_checked_them_never_as_given(events):
    """A write checks each event once and queues what it checked (its time in UTC, its zero counts left out), never
    what it was given; a batch written again is queued once, and one the schema refuses queues nothing."""
    given = make_event("exchange", clock=lambda: T0, masked={"EMAIL": 1, "IBAN": 0}) | {
        "ts": "2026-09-21T16:13:20+02:00"
    }
    assert events.write("b" * 32, [given], ["hook"], at=T0)
    [(_, sent)] = events.peek("hook", 10)
    assert sent == checked(given) and (sent["ts"], sent["masked"]) == ("2026-09-21T14:13:20Z", {"EMAIL": 1})
    assert events.write("b" * 32, [given], ["hook"], at=T0) is False and events.counts() == {"hook": 1}
    with pytest.raises(NotAnEvent):
        events.write("c" * 32, [given, make_event("exchange", clock=lambda: T0) | {"value": "x"}], ["hook"], at=T0)
    assert events.counts() == {"hook": 1}


def test_a_write_whose_queuing_fails_counts_nothing_and_keeps_no_batch(events, monkeypatch):
    """The counts, the batch's id and the queue's rows are one transaction: a queue that cannot be written leaves the
    batch to be written again whole."""
    written = [checked(make_event("exchange", clock=lambda: T0, masked={"EMAIL": 1}))]

    def failing(*args, **kwargs):
        raise OSError("the database restarts")

    with monkeypatch.context() as patched:
        patched.setattr(sys.modules[type(events).__module__], "queued", failing)
        with pytest.raises(OSError, match="the database restarts"):
            events.write("e" * 32, written, ["hook"], at=T0)
    assert events.query(T0 - HOUR, T0 + HOUR) == [] and events.counts() == {}
    assert events.write("e" * 32, written, ["hook"], at=T0) is True  # its id was not kept either
    assert events.counts() == {"hook": 1}


def test_the_projects_bound_holds_through_a_write_that_queues_for_the_outputs(events):
    """Queuing for the outputs changes nothing of the counts: an identity's 50 projects an hour by name, the others as
    `other`; each output gets each event as it was written."""
    named = [
        checked(make_event("exchange", clock=lambda: T0, identity="u_emilycarte", kind="person", project=f"p{n:02d}"))
        for n in range(60)
    ]
    assert events.write("d" * 32, named[:30], ["hook"], at=T0)
    assert events.write("f" * 32, named[30:], ["hook"], at=T0)  # another batch, as another instance writes one
    rows = events.query(T0 - HOUR, T0 + HOUR, by=("project",))
    assert {r["project"] for r in rows} == {f"p{n:02d}" for n in range(50)} | {OTHER}
    assert [r["events"] for r in rows if r["project"] == OTHER] == [10]
    assert [e for _, e in events.peek("hook", 100)] == named


def test_a_batch_refused_for_good_is_dropped_and_counted(events):
    events.put(["hook"], _events(4), at=T0)
    assert events.drop("hook", _seqs(events.peek("hook", 2)), "it answered 400") == 2
    assert events.state("hook") == QueueState(2, dropped=2, last_error="it answered 400")


def test_a_receiver_gone_for_long_keeps_the_newest_and_counts_the_rest(events):
    events.put(["elastic"], _events(6), at=T0)
    assert events.trim("elastic", 4) == 2 and events.trim("elastic", 4) == 0
    assert _ns(events.peek("elastic", 10)) == [2, 3, 4, 5] and events.state("elastic").dropped == 2
    assert events.counts() == {"elastic": 4}


def test_a_queue_kept_short_drops_the_applications_api_first_and_the_administration_log_never(events):
    typed = [{"id": f"{i:032x}", "n": i, "type": kind}
             for i, kind in enumerate(["admin", "exchange", "apps", "admin", "apps", "stopped", "apps", "admin"])]  # fmt: skip
    events.put(["elastic"], typed, at=T0)
    assert events.trim("elastic", 5) == 3  # the three apps events, oldest first (I13)
    assert _ns(events.peek("elastic", 10)) == [0, 1, 3, 5, 7]
    assert events.trim("elastic", 2) == 2  # then the others: the exchange and the stopped one
    assert _ns(events.peek("elastic", 10)) == [0, 3, 7] and events.trim("elastic", 0) == 0  # admin never (I5)
    assert events.state("elastic").dropped == 5 and events.admin_waiting("elastic") == 3


def test_a_trim_takes_the_oldest_apps_events_first(events):
    typed = [{"id": f"{i:032x}", "n": i, "type": "apps"} for i in range(4)]
    events.put(["elastic"], [*typed, {"id": "f" * 32, "n": 9, "type": "exchange"}], at=T0)
    assert events.trim("elastic", 3) == 2 and _ns(events.peek("elastic", 10)) == [2, 3, 9]


def test_an_output_removed_counts_the_administration_logs_events_it_took_along(events):
    events.put(["old"], [{"id": "a" * 32, "type": "admin"}, {"id": "b" * 32, "type": "exchange"}], at=T0)
    assert events.forget(["old"]) == 2 and events.admin_dropped() == 1
    events.put(["gone"], [{"id": "c" * 32, "type": "admin"}], at=T0 - 7200)
    assert events.forget_unknown(set(), before=T0 - 3600) == 1 and events.admin_dropped() == 2


def test_a_refused_entry_of_the_administration_log_holds_the_queue_until_it_is_taken(events):
    events.put(["hook"], [{"id": "a" * 32, "type": "admin"}], at=T0)
    events.failed("hook", "it answered 400", at=T0, retry_at=T0 + 5, blocked=True)
    assert events.state("hook").blocked is True
    events.done("hook", _seqs(events.peek("hook", 1)), at=T0 + 10)
    assert events.state("hook").blocked is False


def test_an_output_removed_takes_its_queue_along_and_one_long_gone_is_cleared(events):
    events.put(["old", "kept"], _events(2), at=T0)
    assert events.forget(["old"]) == 2 and events.state("old") == QueueState(0)
    events.put(["gone"], _events(1), at=T0 - 7200)
    events.put(["new"], _events(1), at=T0)
    assert events.forget_unknown({"kept"}, before=T0 - 3600) == 1  # gone's; new's is too recent to judge
    assert [events.state(name).queued for name in ("gone", "new", "kept")] == [0, 1, 2]


@pytest.mark.server
def test_an_event_committed_late_is_not_lost_when_a_later_one_was_delivered_first(database):
    from latet.ee.events.pg import PgEvents

    queue = PgEvents(database)
    with database.transaction() as slow:  # an instance whose transaction is still open
        slow.execute(
            "INSERT INTO ee_siem_queue (output, at, event) VALUES (%s, %s, %s)",
            ("hook", T0, json.dumps({"id": "a" * 32, "n": 0})),
        )
        queue.put(["hook"], [{"id": "b" * 32, "n": 1}], at=T0)  # another instance's, visible at once
        batch = queue.peek("hook", 10)
        assert _ns(batch) == [1]
        queue.done("hook", _seqs(batch), at=T0)
    assert _ns(queue.peek("hook", 10)) == [0]  # visible now: still there, sent next


# --- fix round 1 ------------------------------------------------------------------------------------------------------


def _blocks_another(conn, seconds=10.0):
    """Whether another connection comes to wait on ``conn``'s locks within ``seconds`` (PostgreSQL's
    ``pg_blocking_pids``): a transaction held open until another instance waits on it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        # A waiter may have connected after this transaction first read the activity view.
        conn.execute("SELECT pg_stat_clear_snapshot()")
        found = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid))"
        ).fetchone()
        if found[0]:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.server
def test_markers_are_read_a_page_at_a_time_and_each_page_casts_every_row(database, monkeypatch):
    """A queue holds millions of rows when receivers are gone: `markers()` reads it by pages of the primary key, each
    a statement well within the statements' bound, every row of each page read as JSON (R4-1)."""
    from latet.ee.events import pg

    from .test_store import _noting

    queue = pg.PgEvents(database)
    monkeypatch.setattr(pg, "MARKERS_PAGE", 2)
    queue.put(["hook"], _events(3), at=T0)  # page 1: 0 and 1; page 2: 2 and the marker
    _forge(queue, "siem", '{"outcome": "siem.gap", "reason": "audit:4-4", "stopped": 1}', kind="admin")
    queue.put(["hook"], _events(1, start=5), at=T0)  # page 3
    transactions = _noting(database, monkeypatch)
    assert [(output, event["reason"]) for output, event in queue.markers()] == [("siem", "audit:4-4")]
    [statements] = transactions
    assert statements[0][0] == pg._SNAPSHOT  # every page of one snapshot
    assert len([query for query, _ in statements if query == pg._MARKERS]) == 3
    _forge(queue, "siem", "not JSON")  # in page 3, past the marker's page
    with pytest.raises(UnreadableQueue):
        queue.markers()


@pytest.mark.server
def test_the_pages_of_markers_are_one_snapshot(database, monkeypatch):
    """A marker another instance commits while the pages are read is not half seen: the pages are one snapshot,
    REPEATABLE READ, and the next check sees it."""
    from latet.ee.events import pg

    queue = pg.PgEvents(database)
    monkeypatch.setattr(pg, "MARKERS_PAGE", 1)
    queue.put(["hook"], _events(2), at=T0)
    marker = json.dumps(gap_event(9, 9, 1, "", f"{9:064x}", origin=ORIGIN, at=T0))
    real, added = database.transaction, []

    class Late:
        """A connection of `markers()` that lets another instance commit a marker once its first page is read."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=None):
            result = self._conn.execute(query, params)
            if query == pg._MARKERS and not added:
                with database.connection() as other:
                    other.execute(
                        "INSERT INTO ee_siem_queue (output, at, type, event) VALUES ('hook', %s, 'admin', %s)",
                        (T0, marker),
                    )
                added.append(True)
            return result

    @contextlib.contextmanager
    def transaction():
        with real() as conn:
            yield Late(conn)

    with monkeypatch.context() as patched:
        patched.setattr(database, "transaction", transaction)
        assert queue.markers() == [] and added
    assert [event["reason"] for _, event in queue.markers()] == ["audit:9-9"]


def test_a_collapse_leaves_slack_below_the_bound_so_that_its_own_entry_starts_no_other(events):
    """Each collapse's `siem.gap` entry is queued in its turn: a collapse that left the queue at its bound would be
    past it again at the next round. Past ``most``, the oldest collapse down to ``keep``; a second round collapses
    nothing until the queue is past ``most`` again."""
    events.put(["hook"], [_admin(i) for i in range(1, 11)], at=T0, refs=list(range(1, 11)))
    assert events.collapse("hook", 6, keep=4, origin=ORIGIN, at=T0) == (1, 7, 7)
    assert events.admin_waiting("hook") == 4
    assert events.collapse("hook", 6, keep=4, origin=ORIGIN, at=T0) is None  # a second round, nothing new
    events.put(["hook"], [_admin(11)], at=T0, refs=[11])  # the collapse's own siem.gap entry, queued in its turn
    assert events.collapse("hook", 6, keep=4, origin=ORIGIN, at=T0) is None
    events.put(["hook"], [_admin(12), _admin(13)], at=T0, refs=[12, 13])
    assert events.collapse("hook", 6, keep=4, origin=ORIGIN, at=T0) == (1, 10, 10)  # past the bound: down to keep
    assert events.admin_waiting("hook") == 4
    assert (ADMIN_KEPT, SLACK) == (1_000_000, 10_000)  # Task 7: collapse(…, ADMIN_KEPT, keep=ADMIN_KEPT - SLACK)


@pytest.mark.server
def test_two_collapses_at_once_never_collapse_past_the_bound(database):
    """Two instances' collapses of one output at once: the second waits for the first's commit under the output's
    lock, then finds the queue within the bound; one marker, one entry of the log."""
    from latet.ee.events.pg import PgEvents

    queue = PgEvents(database)
    queue.put(["hook"], [_admin(i) for i in range(1, 11)], at=T0, refs=list(range(1, 11)))
    inside, noted, answers, waited = threading.Event(), [], [], []

    def holding(conn, action, details):
        noted.append(dict(details))
        if len(noted) == 1:  # the first holds its transaction open until the second waits on it
            inside.set()
            waited.append(_blocks_another(conn))

    def collapse():
        answers.append(queue.collapse("hook", 3, origin=ORIGIN, at=T0, noted=holding))

    first = threading.Thread(target=collapse)
    first.start()
    assert inside.wait(10)
    second = threading.Thread(target=collapse)
    second.start()
    first.join(30)
    second.join(30)
    assert waited == [True] and len(answers) == 2
    assert answers.count(None) == 1 and (1, 8, 8) in answers
    assert len(noted) == 1 and queue.admin_waiting("hook") == 3


@pytest.mark.server
def test_a_skip_waits_for_its_outputs_lock_and_never_for_anothers(database):
    from latet.ee.events.pg import PgEvents, _queue_lock

    queue = PgEvents(database)
    queue.put(["hook"], [_admin(7)], at=T0, refs=[7])
    queue.put(["siem"], [_admin(8)], at=T0, refs=[8])
    answers = []
    waiting = threading.Thread(target=lambda: answers.append(queue.skip("hook", origin=ORIGIN, at=T0)))
    with database.transaction() as holder:  # a collapse of hook's, as another instance holds it
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (_queue_lock(database.schema, "hook"),))
        waiting.start()
        assert _blocks_another(holder)
        assert queue.skip("siem", origin=ORIGIN, at=T0) == (8, 8, 1, 0)  # another output's lock is another
        assert answers == []
    waiting.join(30)
    assert answers == [(7, 7, 1, 0)]


def test_an_outputs_lock_is_the_same_in_every_process():
    """The lock of an output's queue is a hash of its schema and its name that every instance computes the same
    (SHA-256, never Python's salted hash)."""
    from latet.ee.events.pg import _queue_lock

    assert [_queue_lock("latet_a", "hook"), _queue_lock("latet_a", "siem"), _queue_lock("latet_b", "hook")] == [
        2661447022209743790,
        7783844832492472607,
        4753040994864755125,
    ]


def test_a_marker_carries_a_chain_only_across_entries_that_link(events):
    """A marker's chain says the entries it stands for link from its prev to its hash: across a break (1, then 5 and
    6: 1's hash is not 5's prev) or past an entry without a chain, it has none, never a prev read as the first's."""
    events.put(["hook"], [_admin(1), _admin(5), _admin(6)], at=T0, refs=[1, 5, 6])
    assert events.collapse("hook", 1, origin=ORIGIN, at=T0) == (1, 6, 3)
    [(_, marker)] = events.peek("hook", 10)
    assert (marker["reason"], marker["stopped"], marker["chain"]) == ("audit:1-6", 3, None)
    unchained = {"id": "2" * 32, "type": "admin", "outcome": "person.added"}
    events.put(["siem"], [unchained, _admin(3)], at=T0, refs=[2, 3])
    assert events.collapse("siem", 1, origin=ORIGIN, at=T0) == (2, 3, 2)
    assert [event["chain"] for _, event in events.peek("siem", 10)] == [None]
    events.put(["kept"], [_admin(4), _admin(5), _admin(6)], at=T0, refs=[4, 5, 6])
    assert events.collapse("kept", 1, origin=ORIGIN, at=T0) == (4, 6, 3)
    assert [event["chain"] for _, event in events.peek("kept", 10)] == [{"prev": f"{3:064x}", "hash": f"{6:064x}"}]
    events.put(["held"], [unchained], at=T0, refs=[2])
    assert events.skip("held", origin=ORIGIN, at=T0) == (2, 2, 1, 0)
    assert [event["chain"] for _, event in events.peek("held", 10)] == [None]
    half = {"id": "3" * 32, "type": "admin", "outcome": "person.added", "chain": {"hash": f"{3:064x}"}}
    events.put(["half"], [half], at=T0, refs=[3])  # a hash and no prev: never read as the log's first entry's ""
    assert events.skip("half", origin=ORIGIN, at=T0) == (3, 3, 1, 0)
    assert [event["chain"] for _, event in events.peek("half", 10)] == [None]


def test_a_marker_that_cannot_be_made_writes_no_entry_of_the_log(events):
    """The marker is made before its entry of the log is written: one that cannot be made (more entries than an event
    counts) leaves the queue as it was and the log without an entry (R3-1)."""
    full = {"id": "f" * 32, "type": "admin", "outcome": "siem.gap", "reason": "audit:1-5", "stopped": 1_000_000_000}
    events.put(["hook"], [full, _admin(6)], at=T0, refs=[1, 6])
    noted = []
    with pytest.raises(NotAnEvent, match="stopped is a count"):
        events.collapse("hook", 1, origin=ORIGIN, at=T0, noted=lambda c, a, d: noted.append(dict(d)))
    assert noted == [] and [event["outcome"] for _, event in events.peek("hook", 10)] == ["siem.gap", "person.added"]


@pytest.mark.server
def test_a_write_queues_its_events_before_it_takes_the_stripes_locks(database, monkeypatch):
    """The queue's rows go in right after the batch's id, before the stripes' locks are taken: those locks are held
    the shortest time, and everything is still one transaction."""
    from latet.ee.events import pg

    from .test_store import _noting

    queue = pg.PgEvents(database)
    transactions = _noting(database, monkeypatch)
    named = make_event("exchange", clock=lambda: T0, identity="u_emilycarte", kind="person", project="p01")
    assert queue.write("a" * 32, [named], ["hook"], at=T0)
    [statements] = transactions
    sent = [query for query, _ in statements]
    assert sent[0] == pg._READ_COMMITTED and sent.index(pg._PUT) < sent.index(pg._LOCK)


def test_a_job_note_that_is_no_count_never_stops_an_output_being_forgotten(events):
    events.set_job(ADMIN_DROPPED, T0, "not a count")
    events.put(["old"], [{"id": "a" * 32, "type": "admin"}], at=T0)
    assert events.forget(["old"]) == 1 and events.admin_dropped() == 1


def test_a_drop_never_takes_the_administration_logs_events(events):
    """A batch refused for good is dropped, but never an event of the administration log in it (I5: one refused holds
    the queue, and the administrator skips it)."""
    kinds = ["admin", "exchange", "apps"]
    events.put(["hook"], [{"id": f"{i:032x}", "n": i, "type": kind} for i, kind in enumerate(kinds)], at=T0)
    assert events.drop("hook", _seqs(events.peek("hook", 3)), "it answered 400") == 2
    assert _ns(events.peek("hook", 10)) == [0] and events.state("hook").dropped == 2
    assert events.admin_waiting("hook") == 1


@pytest.mark.server
def test_the_lock_probe_sees_a_waiter_connected_after_its_first_activity_read(database):
    from latet.ee.events.pg import PgEvents, _queue_lock

    queue = PgEvents(database)
    queue.put(["hook"], [_admin(7)], at=T0, refs=[7])
    answers, failed = [], []
    waiting = waiter = None
    try:
        with database.transaction() as holder:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (_queue_lock(database.schema, "hook"),))
            holder_pid = holder.info.backend_pid
            # Read the whole activity view before this test creates its waiter.
            # The assertion below distinguishes a new connection from a reused
            # pool connection already present in this first activity read.
            before = {pid for (pid,) in holder.execute("SELECT pid FROM pg_stat_activity").fetchall()}
            waiter = type(holder).connect(**database._parameters)
            waiter_pid = waiter.info.backend_pid
            assert waiter_pid not in before

            def skip():
                try:
                    answers.append(queue.skip("hook", origin=ORIGIN, at=T0, conn=waiter))
                except BaseException as exc:
                    failed.append(type(exc).__name__)

            waiting = threading.Thread(target=skip)
            waiting.start()
            # Do not enumerate the possibly cached activity view to establish
            # the ground truth. Query the new backend's exact PID instead.
            # Retain the helper's existing ten-second/20ms polling bounds.
            deadline = time.monotonic() + 10.0
            blocked = False
            while time.monotonic() < deadline:
                (blocked,) = holder.execute(
                    "SELECT %s = ANY(pg_blocking_pids(%s))", (holder_pid, waiter_pid)
                ).fetchone()
                if blocked:
                    break
                time.sleep(0.02)
            assert blocked, "the exact new backend never waited for the holder"
            assert failed == [] and answers == [] and waiting.is_alive()
            # This is the expected causal RED if the helper's enumeration is
            # stale: the exact backend is blocked, yet the unchanged helper
            # cannot see it. No production queue assertion is weakened.
            assert _blocks_another(holder)
    finally:
        # The holder transaction has exited before joining, even when an
        # assertion failed. Its lock is released, so the actual skip can finish.
        try:
            if waiting is not None:
                waiting.join(30)
                assert not waiting.is_alive(), "the waiter did not leave after the holder released its lock"
        finally:
            if waiter is not None:
                # The explicit transaction was diagnostic and is always rolled back;
                # the fixture still owns and drops the schema after this test.
                waiter.close()
    assert failed == [] and answers == [(7, 7, 1, 0)]
