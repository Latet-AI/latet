"""The administration log reaches each SIEM output as `admin` events: each entry once and in order, by id, an entry
whose time is out of order too, nothing of what happened while the SIEM was paused or had no output, never an entry's
details or address, read whole however busy the log, and once whatever the number of instances; who acted is named when
they administer, and a person's own acts, and a person acted on, are kept as reports.people keeps a person (Decision
1 (d))."""

import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from latet.ee.events import admin as admin_module
from latet.ee.events.admin import AdminForwarder, admin_event
from latet.ee.events.gaps import accounted, check, recorded
from latet.ee.events.locks import MemoryLocks
from latet.ee.events.schema import iso
from latet.ee.events.store import MemoryEvents, gap_event

from .fakes import HEAD, Clock, Entry, FakeAudit

T0 = 1_790_000_000.0
ORIGIN = "https://latet.acme.lan"
EMILY, PRIYA, CI = "u_emilycartr", "u_priyashahx", "u_billingcix"


def _pseudonym(identity: str) -> str:
    return "p_" + identity[2:12]  # a pseudonym's shape (the pipeline's is an HMAC of the id)


def _kind(identity: str) -> str:
    return "key" if identity == CI else "person"


def _forwarder(audit, queue, clock, *, outputs=("hook",), producing=True, locks=None, people="named",
               administers=lambda identity: True, empowers=lambda group_id: False, noted=None):  # fmt: skip
    return AdminForwarder(audit, queue, queue, (locks or MemoryLocks(clock=clock)).lease, outputs=lambda: list(outputs),
                          producing=lambda: producing, origin=ORIGIN, kind_of=_kind,
                          group_of=lambda identity: "g_security" if identity != CI else None, administers=administers,
                          people=lambda: people, pseudonym=_pseudonym, empowers=empowers, noted=noted,
                          clock=clock)  # fmt: skip


def _made(entry, origin=ORIGIN, people="named"):
    return admin_event(entry, origin=origin, kind_of=_kind, group_of=lambda _: None, administers=lambda _: True,
                       people=people, pseudonym=_pseudonym)  # fmt: skip


def _sent(queue):
    return [event for _, event in queue.peek("hook", 50_000)]


def test_each_entry_of_the_administration_log_reaches_the_siem_once_in_order_with_no_detail():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 60, "admin:3f9a", "person.added", EMILY, {"email": "set"}, "10.0.0.7"),
              Entry(2, T0 - 30, EMILY, "siem.changed", "siem", {"outputs": ["hook"]}, "10.0.0.8"))  # fmt: skip
    forwarder = _forwarder(audit, queue, clock)
    assert forwarder.round() is True and forwarder.round() is False  # nothing new the second time
    sent = _sent(queue)
    assert [(e["type"], e["outcome"], e["rule"], e["identity"], e["reason"]) for e in sent] == [
        ("admin", "person.added", EMILY, None, "admin:3f9a"),
        ("admin", "siem.changed", "siem", EMILY, None),
    ]
    assert (sent[1]["kind"], sent[1]["group"]) == ("person", "g_security")
    text = json.dumps(sent)
    assert "10.0.0.7" not in text and "email" not in text and "outputs" not in text


@pytest.mark.parametrize(
    ("people", "priya"), [("group-only", None), ("pseudonymous", "p_priyashahx"), ("named", PRIYA)]
)
def test_who_administers_is_named_and_a_persons_own_acts_are_kept_as_the_company_chose(people, priya):
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, EMILY, "person.added", PRIYA),  # an administrator adds a person
              Entry(2, T0 - 20, PRIYA, "token.added", PRIYA, {"computer": "laptop"}, "10.0.4.31"),  # she connects one
              Entry(3, T0 - 10, CI, "apps.unmasked", CI, {"token": "t_k3yfortest"}))  # a program unmasks  # fmt: skip
    _forwarder(audit, queue, clock, people=people, administers=lambda identity: identity == EMILY).round()
    added, connected, unmasked = _sent(queue)
    assert (added["identity"], added["rule"]) == (EMILY, priya)  # the administrator named, the person acted on kept
    assert (connected["identity"], connected["rule"], connected["kind"], connected["group"]) == (
        priya, priya, "person", "g_security",
    )  # her own act, as reports.people keeps a person: no timeline of an employee under group-only (I12)  # fmt: skip
    assert (unmasked["identity"], unmasked["rule"], unmasked["kind"]) == (CI, CI, "key")  # a program, by its id
    assert "10.0.4.31" not in json.dumps(_sent(queue))


def _reports_changed(id_, ts, people, fields=("people",)):
    """The entry `PUT /ee/reports/settings` writes (`latet server reports settings`): the fields it changed, and the
    section as it set it."""
    details = {"section": "reports", "fields": list(fields), "people": people, "retention_days": 365}
    return Entry(id_, ts, "admin:3f9a", "reports.changed", "reports", details)


def _connected(id_, ts):
    return Entry(id_, ts, PRIYA, "token.added", PRIYA, {"computer": "laptop"})  # her own act


def _kept(queue):
    """Who each of Priya's acts the SIEM received names: her id, a pseudonym or no one."""
    return [e["identity"] for e in _sent(queue) if e["outcome"] == "token.added"]


def test_a_backlog_goes_to_the_siem_with_people_kept_as_they_were_when_each_entry_was_written():
    """L2 of the final security review: entries the forwarder could not send for a while (a note put right a week
    later) are kept as `reports.people` was when each was written, never as it is when they are read: switching to
    `named` names none of what came before, whether `group-only` was the default or chosen."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(_connected(1, T0 - 50), _reports_changed(2, T0 - 40, "named"), _connected(3, T0 - 30))
    _forwarder(audit, queue, clock, people="named", administers=lambda identity: identity == EMILY).round()
    assert _kept(queue) == [None, PRIYA]

    audit, queue = FakeAudit(), MemoryEvents()
    audit.add(_reports_changed(1, T0 - 60, "pseudonymous"))
    forwarder = _forwarder(audit, queue, clock, people="named", administers=lambda identity: identity == EMILY)
    assert forwarder.round() is True  # the note now stands past the change: the next round reads where it began
    audit.add(_connected(2, T0 - 50), _reports_changed(3, T0 - 40, "named"), _connected(4, T0 - 30))
    assert forwarder.round() is True
    assert _kept(queue) == ["p_priyashahx", PRIYA]


def test_the_mode_in_force_is_carried_from_page_to_page_of_a_round(monkeypatch):
    """L2: a round of several pages keeps the mode its pages crossed, page after page."""
    monkeypatch.setattr(admin_module, "PAGE", 2)
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(_connected(1, T0 - 50), _reports_changed(2, T0 - 45, "pseudonymous"), _connected(3, T0 - 40),
              _connected(4, T0 - 35), _reports_changed(5, T0 - 30, "named"), _connected(6, T0 - 25))  # fmt: skip
    _forwarder(audit, queue, clock, people="named", administers=lambda identity: identity == EMILY).round()
    assert _kept(queue) == [None, "p_priyashahx", "p_priyashahx", PRIYA]


def test_a_backlog_is_kept_as_the_company_chooses_now_when_that_is_more_private():
    """L2: entries written under `named` and sent once the company chose `group-only` name no one: the more private of
    the two modes."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(_reports_changed(1, T0 - 60, "named"), _connected(2, T0 - 50), _reports_changed(3, T0 - 40, "group-only"))
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY).round()
    assert _kept(queue) == [None]


def test_a_change_of_the_retention_alone_says_the_mode_before_it():
    """L2: with no change kept before an entry, the first one after it says the mode it was written under, when that
    change left `people` as it was."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(_connected(1, T0 - 50), _reports_changed(2, T0 - 40, "pseudonymous", fields=("retention_days",)))
    _forwarder(audit, queue, clock, people="pseudonymous", administers=lambda identity: identity == EMILY).round()
    assert _kept(queue) == ["p_priyashahx"]


def test_a_change_of_people_written_by_hand_says_nothing_of_the_mode():
    """L2, R4-4: a `reports.changed` whose keyed hash does not hold (written in the database by hand) is no change of
    the mode: what came after it stays as the mode in force left it."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(_connected(1, T0 - 50), _reports_changed(2, T0 - 45, "named"), _connected(3, T0 - 40),
              _reports_changed(4, T0 - 30, "named"), _connected(5, T0 - 20))  # fmt: skip
    audit.broken.add(2)
    _forwarder(audit, queue, clock, people="named", administers=lambda identity: identity == EMILY).round()
    assert _kept(queue) == [None, None, PRIYA]


def test_every_actor_the_log_records_is_said_and_only_an_identity_is_one():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 50, "scim:s_k3yfortest", "person.disabled", EMILY),  # E3's SCIM
              Entry(2, T0 - 40, "server", "server.migrated"),  # E0c's, on the server's machine
              Entry(3, T0 - 30, "unknown", "calls.refused", None, {"count": 3}),  # E0b's refusals before a token
              Entry(4, T0 - 20, CI, "apps.unmasked", CI, {"token": "t_k3yfortest"}))  # 0.10.0  # fmt: skip
    _forwarder(audit, queue, clock).round()
    assert [(e["identity"], e["kind"], e["reason"], e["outcome"]) for e in _sent(queue)] == [
        (None, None, "scim:s_k3yfortest", "person.disabled"), (None, None, "server", "server.migrated"),
        (None, None, "unknown", "calls.refused"), (CI, "key", None, "apps.unmasked"),
    ]  # fmt: skip
    assert "t_k3yfortest" not in json.dumps(_sent(queue))  # an entry's details never go


def test_an_admin_event_carries_its_entrys_place_in_the_chain_and_a_broken_one_none():
    good = _made(Entry(7, T0, "admin:3f9a", "person.added", prev="a" * 64, hash="b" * 64))
    first = _made(Entry(1, T0, "admin:3f9a", "server.initialized"))
    written = _made(Entry(8, T0, "admin:3f9a", "person.added", prev="a" * 64, hash="not a hash"))
    assert good["chain"] == {"prev": "a" * 64, "hash": "b" * 64} and first["chain"] == {"prev": "", "hash": HEAD}
    assert written["chain"] is None  # a row written by hand: sent, its chain left out (`log verify` says it)


def test_an_entry_whose_time_is_out_of_order_is_sent_by_its_id_and_none_twice():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, "admin:3f9a", "key.added", CI))
    forwarder = _forwarder(audit, queue, clock)
    forwarder.round()
    # E0b gives ids in the order entries commit; their times are taken before the chain's lock, so may run backwards.
    audit.add(Entry(2, T0 - 25, "admin:3f9a", "person.added", EMILY))
    forwarder.round()
    forwarder.round()
    assert [e["outcome"] for e in _sent(queue)] == ["key.added", "person.added"]


def test_what_happens_while_nothing_is_sent_is_marked_by_one_gap_the_chain_linked_through_it():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    a, b, d = "a" * 64, "b" * 64, "d" * 64
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY, prev="", hash=a))
    _forwarder(audit, queue, clock, producing=False).round()  # the license produces nothing
    audit.add(Entry(2, T0 - 20, "admin:3f9a", "siem.changed", "siem", prev=a, hash=b))  # the last output removed
    _forwarder(audit, queue, clock, outputs=()).round()  # no output: queued nowhere
    assert _sent(queue) == []
    audit.add(Entry(3, T0 - 10, "admin:3f9a", "siem.changed", "siem", prev=b, hash=d))  # an output added
    _forwarder(audit, queue, clock, noted=audit.noted).round()
    gap, entry = _sent(queue)
    assert (gap["outcome"], gap["reason"], gap["stopped"]) == ("siem.gap", "audit:1-2", 2)  # R2-2: Latet's own
    assert gap["chain"] == {"prev": "", "hash": b} and entry["chain"] == {"prev": b, "hash": d}  # linked through
    assert gap["identity"] is None and gap["rule"] is None  # the ids and how many: no detail
    [written] = [e for e in audit.entries if e.action == "siem.gap"]  # R3-1: its entry of the log, with it
    assert (written.actor, written.details) == ("server", {"output": None, "from": 1, "to": 2, "count": 2})
    assert recorded(audit) == {(1, 2, 2)}  # what `latet server log verify` matches the marker against


def test_a_gap_whose_entry_cannot_be_written_is_marked_at_a_later_round():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY, prev="", hash="a" * 64))
    _forwarder(audit, queue, clock, outputs=()).round()  # no output: a gap
    audit.add(Entry(2, T0 - 20, "admin:3f9a", "siem.changed", "siem", prev="a" * 64, hash="b" * 64))

    def failing(conn, action, details):
        raise OSError("the database restarts")

    with pytest.raises(OSError, match="the database restarts"):
        _forwarder(audit, queue, clock, noted=failing).round()
    assert _sent(queue) == []  # R3-1: the marker and its entry of the log, or neither
    _forwarder(audit, queue, clock, noted=audit.noted).round()
    gap, entry = _sent(queue)
    assert (gap["reason"], entry["outcome"]) == ("audit:1-1", "siem.changed")


def test_a_gap_marker_is_latets_only_when_an_entry_of_the_log_names_its_range():
    audit = FakeAudit()
    audit.add(Entry(5, T0, "server", "siem.gap", "hook", {"output": "hook", "from": 1, "to": 3, "count": 3}),
              Entry(6, T0, "admin:3f9a", "siem.skipped", "siem", {"output": "hook", "from": 4, "to": 4, "count": 1}),
              Entry(7, T0, "server", "siem.gap", None, {"output": None, "from": 8, "to": 9, "count": 2}))  # fmt: skip
    ranges = recorded(audit)
    assert ranges == {(1, 3, 3), (4, 4, 1), (8, 9, 2)}
    markers = [
        ("hook", gap_event(1, 3, 3, "", "a" * 64, origin=ORIGIN, at=T0)),
        ("hook", gap_event(100, 200, 101, "a" * 64, "b" * 64, origin=ORIGIN, at=T0)),  # written in the database by hand
        (None, gap_event(8, 9, 2, "a" * 64, "b" * 64, origin=ORIGIN, at=T0)),  # from a SIEM's copy
    ]
    assert [(m["output"], m["reason"], m["count"]) for m in accounted(markers, ranges, 1)["unrecorded"]] == [
        ("hook", "audit:100-200", 101)
    ]  # R3-1: not Latet's


@pytest.mark.parametrize(
    "forged",
    [{"stopped": "1"}, {"stopped": 1.0}, {"reason": "audit:100-100x"}],
    ids=["count-text", "count-float", "range"],
)
def test_a_gap_marker_in_a_form_latet_never_writes_is_not_latets(forged):
    audit = FakeAudit()
    audit.add(Entry(5, T0, "server", "siem.gap", "hook", {"output": "hook", "from": 100, "to": 100, "count": 1}))
    marker = gap_event(100, 100, 1, "a" * 64, "b" * 64, origin=ORIGIN, at=T0)  # its range recorded: Latet's
    own = _made(audit.entries[0])  # the log's own entry as it reaches the SIEM: `reason` `server`, never a marker
    judged = accounted([("hook", marker), ("hook", own), ("hook", marker | forged)], recorded(audit), 1)
    assert [(m["readable"], m["count"]) for m in judged["unrecorded"]] == [(False, None)]  # R4-1: the forged form alone


def test_a_marker_older_than_the_log_cannot_be_checked_and_is_no_failure():
    audit = FakeAudit()
    audit.through = 40  # a prune kept the entries from 41 on: the `siem.gap` of entries 1 to 3 went with them
    old = gap_event(1, 3, 3, "", "a" * 64, origin=ORIGIN, at=T0)
    judged = accounted([(None, old)], recorded(audit), audit.first_kept())
    assert judged["unrecorded"] == [] and [m["reason"] for m in judged["unchecked"]] == ["audit:1-3"]  # R4-2


def test_an_entry_whose_hash_does_not_hold_names_no_marker():
    audit = FakeAudit()
    audit.add(Entry(9, T0, "server", "siem.gap", None, {"output": None, "from": 100, "to": 200, "count": 101}))
    audit.broken.add(9)  # a row written in the database by hand, with a forged marker's range (R4-4)
    assert recorded(audit) == set()


def test_a_round_stops_when_its_lease_runs_out_and_the_next_holder_goes_on(monkeypatch):
    monkeypatch.setattr(admin_module, "PAGE", 1)
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(*(Entry(i, T0 - 10, "admin:3f9a", "key.added", CI) for i in (1, 2, 3)))
    put = queue.put_audit

    def slow(*args, **kwargs):  # a page that takes longer than the lease (R2-10)
        written = put(*args, **kwargs)
        clock.now += 61
        return written

    monkeypatch.setattr(queue, "put_audit", slow)
    forwarder = _forwarder(audit, queue, clock)
    forwarder.round()
    assert len(_sent(queue)) == 1  # the next page waits for the lease's next holder
    monkeypatch.setattr(queue, "put_audit", put)
    while forwarder.round():
        pass
    assert [e["outcome"] for e in _sent(queue)] == ["key.added"] * 3  # each once


def test_a_note_another_instance_moved_is_never_written_back():
    queue = MemoryEvents()
    assert queue.put_audit(["hook"], [], refs=[], job="admin-to-siem", note='{"gap": null, "last": 5}', expected=None,
                           at=T0)  # fmt: skip
    stale = queue.put_audit(["hook"], [{"id": "e" * 32, "type": "admin"}], refs=[4], job="admin-to-siem",
                            note='{"gap": null, "last": 4}', expected=None, at=T0)  # read before the other wrote  # fmt: skip
    assert stale is False and queue.job("admin-to-siem")[1] == '{"gap": null, "last": 5}' and _sent(queue) == []


@pytest.mark.parametrize(("role", "named"), [("admin", True), ("security", True), ("auditor", False)])
def test_who_is_given_or_loses_administrative_power_is_named_whatever_reports_people_says(role, named):
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, EMILY, "role.granted", PRIYA, {"role": role}),
              Entry(2, T0 - 20, EMILY, "role.revoked", PRIYA, {"role": role}))  # fmt: skip
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY).round()
    assert [e["rule"] for e in _sent(queue)] == ([PRIYA, PRIYA] if named else [None, None])  # Decision 1 (d)


@pytest.mark.parametrize(("group", "rule"), [("g_admins0000", PRIYA), ("g_platform00", "g_platform00")])
def test_who_gains_or_loses_power_through_a_group_is_named_as_by_a_grant(group, rule):
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(
        Entry(1, T0 - 30, EMILY, "member.added", group, {"identity": PRIYA, "source": "manual"}),
        Entry(2, T0 - 20, "scim:s_k3yfortest", "member.removed", group, {"identity": PRIYA, "source": "scim"}),
    )
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY,
               empowers=lambda group_id: group_id == "g_admins0000").round()  # fmt: skip
    assert [e["rule"] for e in _sent(queue)] == [rule, rule]  # R3-2: the member of a group that holds admin or security


def test_a_busy_log_is_read_whole_once_in_order():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    # 25,000 entries in a minute: a program unmasking 400 times a second (0.10.0's apps.unmasked), I4.
    audit.add(*(Entry(i, T0 - 60 + i * 0.002, CI, "apps.unmasked", CI) for i in range(1, 25_001)))
    forwarder = _forwarder(audit, queue, clock)
    while forwarder.round():
        pass
    assert [e["id"] for e in _sent(queue)] == [_made(Entry(i, 0, CI, "x"))["id"] for i in range(1, 25_001)]


def test_two_instances_forward_each_entry_once():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, "admin:3f9a", "person.added", EMILY),
              Entry(2, T0 - 10, "admin:3f9a", "key.added", CI))  # fmt: skip
    locks = MemoryLocks(clock=clock)
    instances = [_forwarder(audit, queue, clock, locks=locks) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda forwarder: forwarder.round(), instances))
    for forwarder in instances:
        forwarder.round()
    assert [e["outcome"] for e in _sent(queue)] == ["person.added", "key.added"]


def test_an_admin_event_has_an_id_of_its_own_on_each_server():
    entry = Entry(7, T0, "admin:3f9a", "person.added", EMILY)
    assert _made(entry)["id"] == _made(entry)["id"] != _made(entry, "https://latet.other.lan")["id"]


# --- beyond the brief: what the task's rulings and carries ask of the same code ----------------------------------------


def test_the_first_round_notes_where_it_starts_though_it_reads_nothing():
    """The first round starts before the first entry of the last hour, or after the newest entry when none is that
    recent, and notes it even when nothing follows: a later round goes on from that id, never from an hour before its
    own time, which would pass over an entry written since by an instance whose clock is behind (I4: the id decides,
    never the time)."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 7200, "admin:3f9a", "key.added", CI))
    forwarder = _forwarder(audit, queue, clock)
    assert forwarder.round() is False
    note = json.loads(queue.job(admin_module.JOB)[1])
    assert (note["last"], note["gap"]) == (1, None) and audit.tagged(_body(note), note["tag"])
    clock.now += 60
    audit.add(Entry(2, T0 - 7000, "admin:3f9a", "person.added", EMILY))  # its time taken by a clock two hours behind
    forwarder.round()
    assert [e["outcome"] for e in _sent(queue)] == ["person.added"]


def _body(note):
    """What a note's tag is made of: its place and its gap, as the forwarder writes them."""
    return json.dumps({"gap": note.get("gap"), "last": note.get("last")}, sort_keys=True)


def _tagged(audit, note):
    """A note as the forwarder writes one, its tag made with the log's key: only whoever holds it writes one."""
    return json.dumps(note | {"tag": audit.tag(_body(note))}, sort_keys=True)


@pytest.mark.parametrize(
    "chains",
    [
        [("not a hash", "a" * 64), ("a" * 64, "b" * 64)],  # the first entry's chain is no chain: never the log's first
        [("", "a" * 64), ("e" * 64, "b" * 64)],  # the second follows another entry: one between was taken out
        [("", "a" * 64), ("a" * 64, "not a hash")],  # the last entry's chain is no chain
    ],
    ids=["first-unchained", "break", "last-unchained"],
)
def test_a_gap_that_holds_a_break_or_an_entry_without_a_chain_carries_none(chains):
    """A marker carries the chain across its gap only when every entry it stands for has one and each follows the one
    before (Task 4's rule for a collapse's marker): never a link the entries do not make, which would hide an entry
    taken out while nothing was sent, nor a missing chain read as the log's first."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    for i, (prev, found) in enumerate(chains, start=1):
        audit.add(Entry(i, T0 - 30 + i, "admin:3f9a", "person.added", EMILY, prev=prev, hash=found))
        _forwarder(audit, queue, clock, outputs=()).round()  # one round each: the gap widened at the second
    audit.add(Entry(3, T0 - 10, "admin:3f9a", "siem.changed", "siem", prev="b" * 64, hash="d" * 64))
    _forwarder(audit, queue, clock, noted=audit.noted).round()
    gap, _ = _sent(queue)
    assert (gap["reason"], gap["stopped"], gap["chain"]) == ("audit:1-2", 2, None)


def test_a_gap_whose_entries_follow_each_other_across_rounds_carries_the_chain_across_them():
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    hashes = ["", "a" * 64, "b" * 64, "d" * 64]
    for i in (1, 2, 3):
        audit.add(Entry(i, T0 - 30 + i, "admin:3f9a", "person.added", EMILY, prev=hashes[i - 1], hash=hashes[i]))
        _forwarder(audit, queue, clock, producing=False).round()
    audit.add(Entry(4, T0 - 10, "admin:3f9a", "siem.changed", "siem", prev="d" * 64, hash="e" * 64))
    _forwarder(audit, queue, clock, noted=audit.noted).round()
    gap, entry = _sent(queue)
    assert gap["chain"] == {"prev": "", "hash": "d" * 64} and entry["chain"]["prev"] == "d" * 64


@pytest.mark.parametrize(
    "note",
    [
        "not JSON",
        '["last", 3]',
        '{"last": 3, "gap": null}',  # the shape a note has, and no tag
        '{"last": 3, "gap": null, "tag": "' + "0" * 64 + '"}',  # a tag the key never made
        '{"last": 9, "gap": {"first": 1, "last": 9, "count": 9, "prev": "", "hash": "' + "c" * 64 + '"}}',  # probe 3
    ],
    ids=["text", "list", "untagged", "forged-tag", "forged-gap"],
)
def test_a_note_written_without_the_logs_key_stops_the_job_and_vouches_for_nothing(note):
    """The forwarder's note is tagged with the log's key, as the anchor of the chain is: one written by hand (a place
    to skip to, a gap Latet would mark and record as its own) is refused in fixed words that never quote it. The round
    raises, which the jobs' thread says by its type at each turn, and nothing is queued and no `siem.gap` entry is
    written (the review's probe 3: Latet vouched for a gap someone wrote)."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(*(Entry(i, T0 - 60 + i, "admin:3f9a", "role.granted", PRIYA, {"role": "admin"}) for i in range(1, 11)))
    queue.set_job(admin_module.JOB, T0, note)
    with pytest.raises(ValueError, match="the note of the administration log's forwarder") as raised:
        _forwarder(audit, queue, clock, noted=audit.noted).round()
    assert note not in str(raised.value) and _sent(queue) == [] and queue.job(admin_module.JOB)[1] == note
    assert [e for e in audit.entries if e.action == "siem.gap"] == []


@pytest.mark.parametrize(
    "note",
    [
        {"last": "3", "gap": None},
        {"last": True, "gap": None},
        {"last": -1, "gap": None},
        {"gap": None},
        {"last": 3, "gap": {"first": "1", "last": 3, "count": 2, "prev": "", "hash": None}},
        {"last": 3, "gap": {"first": 1, "last": 3, "count": 2, "prev": 7, "hash": None}},
        {"last": 3, "gap": [1, 2]},
        {"last": 3, "gap": {"first": 1, "last": 2, "count": 2, "prev": "", "hash": None}},  # a gap that ends elsewhere
        {"last": 3, "gap": {"first": 0, "last": 3, "count": 2, "prev": "", "hash": None}},  # before the first id
        {"last": 3, "gap": {"first": 4, "last": 3, "count": 1, "prev": "", "hash": None}},  # after its own end
        {"last": 3, "gap": {"first": 2, "last": 3, "count": 3, "prev": "", "hash": None}},  # more than its ids
        {"last": 3, "gap": {"first": 2, "last": 3, "count": 0, "prev": "", "hash": None}},  # none
        {"last": 3, "gap": None, "more": 1},  # a field no round writes
        {"last": 3, "gap": {"first": 2, "last": 3, "count": 1, "prev": ""}},  # a gap without its hash
    ],
    ids=[
        "id-text",
        "id-bool",
        "id-negative",
        "no-id",
        "gap-text",
        "gap-prev-number",
        "gap-list",
        "gap-elsewhere",
        "gap-first-zero",
        "gap-inverted",
        "gap-count-over",
        "gap-count-zero",
        "more",
        "gap-no-hash",
    ],
)
def test_a_note_of_another_shape_stops_the_job_and_writes_nothing(note):
    """Even tagged (whoever holds the key), a note a round of the forwarder never writes says nothing to go on from: an
    id that is no count, a gap that does not end at its place, starts before the first id or after its end, or counts
    more entries than its ids or none. The round raises in fixed words and queues nothing, never reading from an id it
    cannot trust nor sending a marker that claims a range it was never given."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY), Entry(2, T0 - 20, "admin:3f9a", "key.added", CI))
    text = _tagged(audit, note)
    queue.set_job(admin_module.JOB, T0, text)
    with pytest.raises(ValueError, match="the note of the administration log's forwarder cannot be read"):
        _forwarder(audit, queue, clock, noted=audit.noted).round()
    assert _sent(queue) == [] and queue.job(admin_module.JOB)[1] == text


def test_an_entry_of_no_form_latet_writes_never_stops_the_forwarder_nor_names_a_person():
    """A row written by hand may hold anything: a role or a member that is no text, an actor or a target that is an
    identity's id in capitals or too long. It is sent all the same (`latet server log verify` finds it), what it says
    read as a code and a person in it kept as reports.people keeps one; never an event the schema refuses, which would
    hold the log's every later entry for good."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 40, EMILY, "role.granted", PRIYA, {"role": ["admin"]}),
              Entry(2, T0 - 30, EMILY, "member.added", "g_admins0000", {"identity": {"id": PRIYA}}),
              Entry(3, T0 - 20, PRIYA.upper(), "token.added", PRIYA.upper()),
              Entry(4, T0 - 10, "u_" + "x" * 40, "token.added", "u_" + "y" * 40))  # fmt: skip
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY,
               empowers=lambda group_id: True).round()  # fmt: skip
    sent = _sent(queue)
    assert [(e["outcome"], e["identity"], e["reason"], e["rule"]) for e in sent] == [
        ("role.granted", EMILY, None, None), ("member.added", EMILY, None, "g_admins0000"),
        ("token.added", None, None, None), ("token.added", None, None, None),
    ]  # fmt: skip
    assert "priyashahx" not in json.dumps(sent) and "xxxx" not in json.dumps(sent) and "yyyy" not in json.dumps(sent)


def test_a_choice_of_people_it_does_not_know_keeps_no_person():
    """As the pipeline keeps a person (Task 6): a value of reports.people it does not know keeps none."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, PRIYA, "token.added", PRIYA))
    _forwarder(audit, queue, clock, people="everyone", administers=lambda identity: False).round()
    [sent] = _sent(queue)
    assert (sent["identity"], sent["rule"], sent["kind"]) == (None, None, "person")


class _Held:
    """A lease its holder believes its own to the end: the note is then what decides."""

    def __bool__(self):
        return True

    def held(self, margin=0.0):
        return True

    def release(self):
        return None


@contextlib.contextmanager
def _always(name, seconds):
    yield _Held()


def test_a_note_another_instance_moved_while_the_round_read_is_never_written_over():
    """The lease ends while a round reads (a page longer than its minute) and the instance that holds it now queues
    the same entries: the first round's page is written only over the note it read (R2-10), never queued twice."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, "admin:3f9a", "person.added", EMILY))
    first, other = (_forwarder(audit, queue, clock, locks=SimpleNamespace(lease=_always)) for _ in range(2))
    real, ran = audit.after, []

    def reading(*args, **kwargs):
        found = real(*args, **kwargs)
        if not ran:
            ran.append(True)
            assert other.round() is True  # the other instance's whole round, while the first reads
        return found

    audit.after = reading
    assert first.round() is False
    assert [e["outcome"] for e in _sent(queue)] == ["person.added"]


def test_a_round_while_another_instance_holds_the_lease_reads_and_writes_nothing():
    """One instance at a time (R2-10): while another holds `events:admin`, a round reads nothing and notes nothing."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, "admin:3f9a", "person.added", EMILY))
    locks = MemoryLocks(clock=clock)
    read: list[int] = []
    real = audit.after
    audit.after = lambda *args, **kwargs: read.append(1) or real(*args, **kwargs)
    with locks.lease(admin_module.LOCK, 60) as theirs:
        assert theirs and _forwarder(audit, queue, clock, locks=locks).round() is False
    assert read == [] and queue.job(admin_module.JOB) == (None, None) and _sent(queue) == []


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.server)])
def events(request):
    if request.param == "memory":
        return MemoryEvents()
    from latet.ee.events.pg import PgEvents

    return PgEvents(request.getfixturevalue("database"))


@pytest.mark.parametrize(
    ("page", "refs", "refused"),
    [
        ([{"id": "e" * 32, "type": "admin"}], [1], "v is 1"),  # an event the schema refuses (NotAnEvent)
        (None, [None], "names its entry by its id"),  # an entry of the log with no id: it would read as entry 0
        (None, [0], "names its entry by its id"),
        (None, [], "one entry for each event"),  # fewer ids than events
    ],
    ids=["not-an-event", "no-id", "id-zero", "ids-short"],
)
def test_a_page_of_the_log_is_queued_checked_and_each_event_with_its_entry_or_not_at_all(events, page, refs, refused):
    """Task 4's carries: `put` checks nothing and an `admin` row with no id reads as the log's entry 0, so a page goes
    through ``put_audit``, which checks each event and refuses one without its entry's id: nothing written then, the
    note neither."""
    page = page if page is not None else [_made(Entry(1, T0, "admin:3f9a", "person.added", EMILY))]
    with pytest.raises(ValueError, match=refused):
        events.put_audit(["hook"], page, refs=refs, job="admin-to-siem", note='{"gap": null, "last": 1}',
                         expected=None, at=T0)  # fmt: skip
    assert events.peek("hook", 10) == [] and events.job("admin-to-siem")[1] is None


def test_who_each_identity_is_is_asked_once_a_round():
    """R2-10: whether an identity administers, its kind and its group, and whether a group gives power, are asked
    once a round however many of its entries the round reads (on a server each is a round trip)."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(*(Entry(i, T0 - 30, EMILY, "member.added", "g_admins0000", {"identity": PRIYA}) for i in range(1, 51)))
    asked: list[tuple[str, str]] = []

    def counted(name, answer):
        def ask(identity):
            asked.append((name, identity))
            return answer(identity)

        return ask

    forwarder = AdminForwarder(audit, queue, queue, MemoryLocks(clock=clock).lease, outputs=lambda: ["hook"],
                               producing=lambda: True, origin=ORIGIN, kind_of=counted("kind", _kind),
                               group_of=counted("group", lambda _: None),
                               administers=counted("administers", lambda _: True), people=lambda: "named",
                               pseudonym=_pseudonym, empowers=counted("empowers", lambda _: True),
                               clock=clock)  # fmt: skip
    forwarder.round()
    assert len(_sent(queue)) == 50 and len(asked) == len(set(asked))


# --- the check of `latet server log verify` (R3-1, R4-1, R4-2, R4-4) --------------------------------------------------


class _Collapsing:
    """A queue whose collapse commits, marker and its entry of the log together, while the check reads: once its
    markers are read, the entry is in the log."""

    def __init__(self, audit):
        self.audit, self.calls = audit, []

    def markers(self):
        self.calls.append("markers")
        self.audit.add(Entry(9, T0, "server", "siem.gap", "hook", {"output": "hook", "from": 1, "to": 3, "count": 3}))
        return [("hook", gap_event(1, 3, 3, "", "a" * 64, origin=ORIGIN, at=T0))]


def _logged(calls, name, real):
    def call(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    return call


def test_the_check_reads_the_markers_before_the_log_so_a_collapse_meanwhile_is_no_false_failure():
    """A marker and its entry of the log commit together: read the markers first, and every marker read has its entry
    in the log read after; read the other way, a collapse that lands between the two reads shows its marker with no
    entry (Task 4's re-review). The first id the log keeps is read last: a prune meanwhile leaves a marker whose entry
    went with it said as too old to check, never as not Latet's."""
    audit = FakeAudit()
    queue = _Collapsing(audit)
    audit.after = _logged(queue.calls, "after", audit.after)
    audit.first_kept = _logged(queue.calls, "first_kept", audit.first_kept)
    judged = check(audit, queue)
    assert judged == {"recorded": [(1, 3, 3)], "first_kept": 1, "unrecorded": [], "unchecked": []}
    assert queue.calls[0] == "markers" and queue.calls[-1] == "first_kept" and "after" in queue.calls


class _Unreadable:
    def __init__(self, exc):
        self.exc = exc

    def markers(self):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        RecursionError("too deep"),
        OSError("the statement was cancelled"),
        ValueError("a row of a SIEM queue is not JSON"),
    ],
    ids=["recursion", "cancelled", "not-json"],
)
def test_the_check_fails_closed_whatever_the_queue_raises(exc):
    """Whatever the queues' read raises (a row that is not JSON, a statement cancelled past its bound, a recursion too
    deep for a row PostgreSQL's JSON reads), the check raises: a check that could not run is never a pass (Task 4's
    review); the route says it by its type."""
    with pytest.raises(type(exc)):
        check(FakeAudit(), _Unreadable(exc))


def test_a_skip_that_made_no_marker_names_no_range_and_a_count_that_is_no_int_none():
    """A skip that passed refused events alone made no marker (Task 7's carry: `first` None): its `siem.skipped`
    names no range. And only ints name one, as Latet writes them (R4-1)."""
    audit = FakeAudit()
    audit.add(
        Entry(5, T0, "admin:3f9a", "siem.skipped", "siem",
              {"output": "hook", "from": None, "to": None, "count": 0, "dropped": 5}),
        Entry(6, T0, "server", "siem.gap", None, {"output": None, "from": "1", "to": "2", "count": "2"}),
        Entry(7, T0, "server", "siem.gap", None, {"output": None, "from": True, "to": 2, "count": 2}),
        Entry(8, T0, "server", "siem.gap", None, {"output": None, "from": 3, "to": 4, "count": 2}),
    )  # fmt: skip
    assert recorded(audit) == {(3, 4, 2)}


def test_what_a_forged_marker_says_is_said_in_printable_ascii_alone():
    """A row written by hand may put anything in its `reason`, `id` or output's name: the check says them in printable
    ASCII alone, 80 characters at most, never a sequence a terminal acts on."""
    escape = chr(0x1B)
    forged = {"outcome": "siem.gap", "reason": f"audit:1-2{escape}[2J", "stopped": 2, "id": f"{escape}]0;x" + "e" * 99}
    [said] = accounted([(f"hook{escape}[31m", forged)], set(), 1)["unrecorded"]
    assert escape not in json.dumps(said) and said["reason"] == "audit:1-2[2J" and len(said["id"]) == 80
    assert (said["output"], said["readable"]) == ("hook[31m", False)


# --- on a server: PostgreSQL, Valkey and E0b's log ----------------------------------------------------------------------


@pytest.mark.server
def test_a_page_and_its_note_are_written_together_or_not_at_all_in_postgresql(database):
    from latet.ee.events.pg import PgEvents

    queue = PgEvents(database)
    one = _made(Entry(1, T0, "admin:3f9a", "person.added", EMILY))

    def failing(conn):
        raise OSError("the database restarts")

    with pytest.raises(OSError, match="the database restarts"):
        queue.put_audit(["hook"], [one], refs=[1], job="admin-to-siem", note='{"gap": null, "last": 1}', expected=None,
                        at=T0, noted=failing)  # fmt: skip
    assert queue.peek("hook", 10) == [] and queue.job("admin-to-siem") == (None, None)
    assert queue.put_audit(["hook"], [one], refs=[1], job="admin-to-siem", note='{"gap": null, "last": 1}',
                           expected=None, at=T0)  # fmt: skip
    stale = queue.put_audit(["hook"], [one], refs=[1], job="admin-to-siem", note='{"gap": null, "last": 0}',
                            expected=None, at=T0)  # fmt: skip
    assert stale is False and len(queue.peek("hook", 10)) == 1 and queue.job("admin-to-siem")[1].endswith("1}")
    [(_, event)] = queue.peek("hook", 10)
    assert event == one and queue.state("hook").admin == 1


@pytest.mark.server
def test_two_instances_that_found_no_note_queue_a_page_once_in_postgresql(database):
    """The first page: two instances whose leases overlapped (a page longer than its lease) both read no note. The
    note's row is made first, so that the second waits for the first's transaction and then finds its note: never both
    pages queued (R2-10)."""
    from latet.ee.events.pg import PgEvents

    from .test_queue import _blocks_another

    queue = PgEvents(database)
    one = _made(Entry(1, T0, "admin:3f9a", "person.added", EMILY))
    inside, release, answers, waited = threading.Event(), threading.Event(), {}, []

    def holding(conn):  # the first instance's transaction, held open until the second waits on it
        inside.set()
        waited.append(_blocks_another(conn))
        release.wait(10)

    def first():
        answers["first"] = queue.put_audit(["hook"], [one], refs=[1], job="admin-to-siem",
                                           note='{"gap": null, "last": 1}', expected=None, at=T0,
                                           noted=holding)  # fmt: skip

    def second():
        answers["second"] = queue.put_audit(["hook"], [one], refs=[1], job="admin-to-siem",
                                            note='{"gap": null, "last": 1}', expected=None, at=T0)  # fmt: skip

    one_thread = threading.Thread(target=first)
    one_thread.start()
    assert inside.wait(10)
    other = threading.Thread(target=second)
    other.start()
    other.join(15)
    release.set()
    one_thread.join(15)
    other.join(15)
    assert waited == [True] and answers == {"first": True, "second": False} and len(queue.peek("hook", 10)) == 1


@pytest.mark.server
def test_the_log_reaches_the_siem_from_postgresql_its_gap_recorded_and_checked(database, bus):
    """End to end on a server's backends: E0b's log read by id, a gap while there is no output marked before the next
    page with its entry of the log written in the same transaction (R3-1), that entry's keyed hash holding (R4-4), the
    marker accounted for by the check (R4-1), and the gap's entry itself sent at the next round."""
    from latet.ee.events.pg import PgEvents
    from latet.server.audit import AuditLog

    from ...server.conftest import AUDIT_KEY

    clock = Clock(T0)
    audit, queue = AuditLog(database, key=AUDIT_KEY, clock=clock), PgEvents(database)

    def noted(conn, action, details):
        audit.record("server", action, details.get("output"), dict(details), conn=conn)

    def forwarder(outputs):
        return AdminForwarder(audit, queue, queue, bus.lease, outputs=lambda: list(outputs), producing=lambda: True,
                              origin=ORIGIN, kind_of=_kind, group_of=lambda _: None, administers=lambda _: True,
                              people=lambda: "named", pseudonym=_pseudonym, noted=noted, clock=clock)  # fmt: skip

    audit.record("admin:a_first00000", "person.added", EMILY, {"source": "local"}, "10.0.0.5")
    audit.record(EMILY, "siem.changed", "siem", {"outputs": []}, "10.0.0.6")
    assert forwarder(()).round() is False  # no output: a gap, noted, and where the forwarding began recorded
    audit.record(EMILY, "siem.changed", "siem", {"outputs": ["hook"]}, "10.0.0.6")
    assert forwarder(("hook",)).round() is True
    first, second, began, third = audit.after(0)[:4]
    assert (began.actor, began.action, began.details) == ("server", "siem.forwarding", {"from": 0})
    assert audit.holds(began)
    gap, forwarding, entry = (event for _, event in queue.peek("hook", 10))
    assert (gap["reason"], gap["stopped"], gap["chain"]) == (
        f"audit:{first.id}-{second.id}", 2, {"prev": "", "hash": second.hash},
    )  # fmt: skip
    assert (forwarding["outcome"], forwarding["reason"]) == ("siem.forwarding", "server")
    assert entry["chain"] == {"prev": began.hash, "hash": third.hash}
    [written] = audit.after(third.id, action="siem.gap")
    assert written.details == {"output": None, "from": first.id, "to": second.id, "count": 2} and audit.holds(written)
    assert check(audit, queue) == {
        "recorded": [(first.id, second.id, 2)], "first_kept": 1, "unrecorded": [], "unchecked": [],
    }  # fmt: skip
    assert forwarder(("hook",)).round() is True  # the gap's own entry, as any other
    sent = [event for _, event in queue.peek("hook", 10)]
    assert (sent[-1]["outcome"], sent[-1]["reason"], sent[-1]["chain"]["hash"]) == ("siem.gap", "server", written.hash)
    assert "10.0.0" not in json.dumps(sent) and audit.verify().ok


# --- fix round 1 (the review): rows written by hand, power judged by the act, a marker whose entry is kept ------------


@pytest.mark.parametrize(
    "ts",
    [float("nan"), float("inf"), float("-inf"), 1e12, 1e13, -1e13, -1.0],
    ids=["nan", "inf", "-inf", "1e12", "1e13", "-1e13", "before-1970"],
)
def test_an_entry_whose_time_no_clock_gives_is_sent_with_the_rounds_time(ts):
    """A row written by hand may hold a time no clock gives (not a number, an infinity, a year past 9999 or before
    1970): the entry is sent all the same, stamped with the round's time, its chain placing it; never an event the
    schema refuses at every round (the review's probe 1), nor a first round that starts past the entries of the last
    hour because the newest of them is dated long ago (probe 2)."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY),
              Entry(2, ts, "admin:3f9a", "person.added", PRIYA),
              Entry(3, T0 - 10, "admin:3f9a", "role.granted", PRIYA, {"role": "admin"}))  # fmt: skip
    forwarder = _forwarder(audit, queue, clock)
    assert forwarder.round() is True and forwarder.round() is False
    sent = _sent(queue)
    assert [e["outcome"] for e in sent] == ["person.added", "person.added", "role.granted"]
    assert sent[1]["ts"] == iso(T0) and sent[0]["ts"] == iso(T0 - 30)


def test_an_entry_dated_far_ahead_does_not_drag_the_first_round_back_to_the_start_of_the_log():
    """The first round starts before the first entry of the last hour, dated no further ahead than an hour: an older
    entry dated by hand far ahead (a year of 33658) does not make it send the whole log from its start."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, 1e13, "x", "y"), Entry(2, T0 - 7200, "admin:3f9a", "key.added", CI),
              Entry(3, T0 - 30, "admin:3f9a", "person.added", EMILY))  # fmt: skip
    _forwarder(audit, queue, clock).round()
    assert [e["outcome"] for e in _sent(queue)] == ["person.added"]


def test_a_row_written_by_hand_above_the_sequence_never_moves_the_forwarder_past_entries_to_come():
    """A row inserted with an id the log's sequence never gave (2^63 - 1: the review's probe 4) is never read, at the
    first round as later: the place noted never passes the entries the server writes next."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 7200, "admin:3f9a", "person.added", EMILY))
    audit.add(Entry(2**63 - 1, T0 - 7100, "x", "y", hash="not"), by_hand=True)  # newest by id, dated long ago
    forwarder = _forwarder(audit, queue, clock)
    assert forwarder.round() is False and json.loads(queue.job(admin_module.JOB)[1])["last"] == 1
    for i in range(2, 6):  # the server goes on: ids from the sequence
        audit.add(Entry(i, T0 - 10, EMILY, "role.granted", PRIYA, {"role": "admin"}))
    forwarder.round()
    audit.add(Entry(2**63 - 2, T0 - 5, "x", "y", hash="not"), by_hand=True)
    audit.add(Entry(6, T0 - 1, EMILY, "key.added", CI))
    forwarder.round()
    assert [e["outcome"] for e in _sent(queue)] == ["role.granted"] * 4 + ["key.added"]
    assert json.loads(queue.job(admin_module.JOB)[1])["last"] == 6


def test_a_page_that_holds_an_entry_whose_hash_does_not_hold_is_said(caplog):
    """An entry whose keyed hash does not hold (changed, or written, by hand) is sent as any other, and the round says,
    in fixed words naming nothing of it, that the page held one: `latet server log verify` says which."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY),
              Entry(2, T0 - 20, "u_mallory0000", "role.granted", "u_mallory0000", {"role": "admin"}, "10.6.6.6"),
              Entry(3, T0 - 10, "admin:3f9a", "key.added", CI))  # fmt: skip
    audit.broken.add(2)
    _forwarder(audit, queue, clock).round()
    assert len(_sent(queue)) == 3
    said = [r.getMessage() for r in caplog.records if "does not hold" in r.getMessage()]
    assert said == [admin_module.UNHELD] and "mallory" not in said[0] and "10.6.6.6" not in said[0]
    caplog.clear()
    audit.add(Entry(4, T0 - 5, "admin:3f9a", "key.added", CI))
    _forwarder(audit, queue, clock).round()  # a page whose entries all hold says nothing
    assert not [r for r in caplog.records if "does not hold" in r.getMessage()]


@pytest.mark.parametrize(
    "entries",
    [
        [Entry(1, T0 - 30, EMILY, "person.added", PRIYA),
         Entry(2, T0 - 20, EMILY, "role.revoked", EMILY, {"role": "admin"})],  # she gives up the role right after
        [Entry(1, T0 - 30, EMILY, "key.rotated", CI),
         Entry(2, T0 - 20, "admin:3f9a", "role.revoked", EMILY, {"role": "admin"})],  # demoted before the round reads
        [Entry(1, T0 - 30, EMILY, "policy.changed", None, {"etag": "a" * 64}),
         Entry(2, T0 - 20, "admin:3f9a", "role.revoked", EMILY, {"role": "security"})],  # security's act
    ],
    ids=["self-revoke", "demoted", "security"],
)  # fmt: skip
def test_an_administrators_acts_name_them_though_they_no_longer_administer(entries):
    """Who administers is judged by the act as well (the review): an act only the admin or the security role may make
    names who made it, whatever roles they hold when the round reads it; a demotion right after acting hides nothing."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(*entries)
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: False).round()
    assert _sent(queue)[0]["identity"] == EMILY
    assert [e["identity"] for e in _sent(queue)][1] == (EMILY if entries[1].actor == EMILY else None)


def test_who_holds_the_admin_or_security_role_when_the_entry_is_read_is_named_for_any_act():
    """The roles held when the round reads the entry name who acted as well (Decision 1 (d)): an administrator's own
    computer connected is named, though the act is anyone's; a person who holds no such role is kept as
    reports.people says."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 20, EMILY, "token.added", EMILY), Entry(2, T0 - 10, PRIYA, "token.added", PRIYA))
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY).round()
    assert [e["identity"] for e in _sent(queue)] == [EMILY, None]


def test_a_persons_own_acts_through_their_sign_in_stay_kept_as_reports_people_says():
    """What a person's own sign-in or session records (their account made, linked, the groups the identity provider
    names for them, a code for their own computer) are no administrator's acts, though the same actions are when an
    administrator makes them for someone else."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 50, PRIYA, "person.added", PRIYA, {"source": "oidc"}),
              Entry(2, T0 - 40, PRIYA, "person.linked", PRIYA, {"source": "oidc", "fields": ["subject"]}),
              Entry(3, T0 - 35, PRIYA, "group.added", "g_ssogroup01", {"source": "oidc"}),
              Entry(4, T0 - 30, PRIYA, "member.added", "g_ssogroup01", {"identity": PRIYA, "source": "oidc"}),
              Entry(5, T0 - 20, PRIYA, "person.code.made", PRIYA),
              Entry(6, T0 - 10, EMILY, "person.code.made", PRIYA))  # fmt: skip
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: False).round()
    assert [e["identity"] for e in _sent(queue)] == [None] * 5 + [EMILY]


def test_a_persons_withdrawal_of_their_own_request_on_company_approvals_is_kept_as_reports_people_says():
    """The whole-branch security review's L2: whoever asked for a change the security team decides on, and withdraws
    it on Company approvals (a person who only controls an API key there), takes no decision of the security role's:
    their entry (`approval.decided` with `withdrawn`) is kept as reports.people keeps a person, while the security
    team's own decision on it names who took it."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    withdrawn = {"kind": "change", "decision": "reject", "route": "security", "withdrawn": True}
    decided = {"kind": "change", "decision": "approve", "route": "security"}
    audit.add(Entry(1, T0 - 20, PRIYA, "approval.decided", PRIYA, withdrawn),
              Entry(2, T0 - 10, PRIYA, "approval.decided", EMILY, decided))  # fmt: skip
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: False).round()
    assert [e["identity"] for e in _sent(queue)] == [None, PRIYA]


@pytest.mark.parametrize("people", ["group-only", "pseudonymous", "named"])
def test_a_decider_s_opening_of_another_person_s_text_names_the_decider_and_keeps_the_person(people):
    """The final security review's N2 (the owner's decision): a decider of the security team who opens another person's
    text on Company approvals (`approval.opened`) acts as the security role, and is named as its decisions name them;
    the person whose text it is is kept as reports.people keeps a person; the item and its details never leave."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    opened = {"kind": "batch", "item": "Zx81kP-2qLmT", "route": "security"}
    audit.add(Entry(1, T0 - 10, PRIYA, "approval.opened", EMILY, opened))
    _forwarder(audit, queue, clock, people=people, administers=lambda identity: False).round()
    [event] = _sent(queue)
    kept = {"group-only": None, "pseudonymous": _pseudonym(EMILY), "named": EMILY}[people]
    assert (event["identity"], event["rule"], event["outcome"]) == (PRIYA, kept, "approval.opened")
    assert "Zx81kP-2qLmT" not in json.dumps(event)


def test_which_acts_name_who_made_them_follows_e3s_roles_table():
    """Each action is mapped to the action of E3's role table its route needs, and names who made it when only the
    admin or the security role may: as the table says, never a list of its own."""
    from latet.ee.roles import ACTIONS, Role

    assert set(admin_module.PERMISSIONS.values()) <= set(ACTIONS)
    assert {
        action for action, permission in admin_module.PERMISSIONS.items()
        if ACTIONS[permission] <= {Role.ADMIN, Role.SECURITY}
    } == admin_module.ADMINISTERING  # fmt: skip
    assert {"role.granted", "member.added", "key.rotated", "policy.changed", "license.installed"} <= (
        admin_module.ADMINISTERING
    )
    assert not {"token.added", "apps.unmasked", "reports.exported", "audit.exported"} & admin_module.ADMINISTERING
    lead = {**ACTIONS, "people.manage": frozenset({Role.ADMIN, Role.GROUP_LEAD})}  # a table that let group leads in
    assert "role.granted" not in admin_module.administering(lead) and "key.added" in admin_module.administering(lead)
    unknown = {name: roles for name, roles in ACTIONS.items() if name != "keys.manage"}  # one the table does not know
    assert "key.added" not in admin_module.administering(unknown)


def test_a_role_given_to_a_group_names_the_group_and_its_members_as_they_join():
    """A role given to a group names the group in `rule` (its id is no person); a person added to it later is named,
    as R3-2 says."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(
        Entry(1, T0 - 30, EMILY, "role.granted", "g_operations", {"role": "admin"}),
        Entry(2, T0 - 20, EMILY, "member.added", "g_operations", {"identity": PRIYA, "source": "local"}),
    )
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: identity == EMILY,
               empowers=lambda group_id: group_id == "g_operations").round()  # fmt: skip
    assert [e["rule"] for e in _sent(queue)] == ["g_operations", PRIYA]


def test_a_marker_whose_entry_the_log_still_keeps_is_checked_though_its_range_was_pruned():
    """A marker's range may end before the first entry the log keeps while its own `siem.gap` entry, written later, is
    still kept (the review): it is checked against that entry, and Latet's; only a marker no kept entry can name is
    said as too old to check."""
    audit = FakeAudit()
    audit.add(Entry(5000, T0, "server", "siem.gap", "hook", {"output": "hook", "from": 1, "to": 1000, "count": 1000}))
    audit.through = 2000
    kept = gap_event(1, 1000, 1000, "", "a" * 64, origin=ORIGIN, at=T0)
    gone = gap_event(1001, 1500, 500, "", "a" * 64, origin=ORIGIN, at=T0)
    judged = accounted([("hook", kept), ("hook", gone)], recorded(audit), audit.first_kept())
    assert judged["unrecorded"] == [] and [m["reason"] for m in judged["unchecked"]] == ["audit:1001-1500"]


@pytest.mark.server
def test_a_row_inserted_by_hand_at_the_last_id_never_stops_the_log_reaching_the_siem_in_postgresql(database, bus):
    """The review's probe 4 on a server's backends: a row inserted by hand with id 2^63 - 1, before the first round
    and between rounds, never moves the forwarder past the entries E0b writes next; its note is tagged with the log's
    key and holds."""
    from latet.ee.events.pg import PgEvents
    from latet.server.audit import AuditLog

    from ...server.conftest import AUDIT_KEY

    clock = Clock(T0)
    audit, queue = AuditLog(database, key=AUDIT_KEY, clock=clock), PgEvents(database)
    forwarder = AdminForwarder(audit, queue, queue, bus.lease, outputs=lambda: ["hook"], producing=lambda: True,
                               origin=ORIGIN, kind_of=_kind, group_of=lambda _: None, administers=lambda _: True,
                               people=lambda: "named", pseudonym=_pseudonym, clock=clock)  # fmt: skip

    def by_hand(row_id):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO audit (id, ts, actor, action, target, details, address, prev, hash)"
                " VALUES (%s, %s, 'x', 'y', NULL, '{}', NULL, '', 'not')",
                (row_id, T0 - 7200),
            )

    audit.record("admin:a_first00000", "person.added", EMILY)
    by_hand(2**63 - 1)  # the newest row by id, dated long ago
    forwarder.round()
    clock.now += 1
    by_hand(2**63 - 2)
    audit.record(EMILY, "role.granted", PRIYA, {"role": "admin"})
    audit.record(EMILY, "key.added", CI)
    while forwarder.round():
        pass
    assert [e["outcome"] for _, e in queue.peek("hook", 10)] == ["person.added", "role.granted", "key.added"]
    note = json.loads(queue.job(admin_module.JOB)[1])
    assert note["last"] == audit.newest_id() < 2**62 and audit.tagged(_body(note), note["tag"])


# --- fix round 2 (the re-review) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("gone", ["deleted", "null"])
def test_a_note_lost_after_the_forwarding_began_stops_it_and_skips_nothing(gone):
    """The first round records where the forwarding began (`siem.forwarding {from}`, by `server`, in the transaction
    that writes its first note). A note deleted, or set to nothing, by hand afterwards would make a round start again
    from the last hour, skipping what came before it unsaid (the re-review: four role grants two hours old): it is
    refused in fixed words instead, and nothing more is queued until it is put right."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0 - 10_000)
    _forwarder(audit, queue, clock, noted=audit.noted).round()  # the first round: nothing to read yet
    [began] = [e for e in audit.entries if e.action == "siem.forwarding"]
    assert (began.actor, began.target, began.details) == ("server", None, {"from": 0})
    clock.now = T0
    audit.add(*(Entry(i, T0 - 7200, EMILY, "role.granted", PRIYA, {"role": "admin"}) for i in range(2, 6)))
    if gone == "deleted":
        del queue._jobs[admin_module.JOB]
    else:
        queue.set_job(admin_module.JOB, T0, None)
    with pytest.raises(ValueError, match="the note of the administration log's forwarder is gone") as raised:
        _forwarder(audit, queue, clock, noted=audit.noted).round()
    assert "role" not in str(raised.value) and _sent(queue) == [] and queue.job(admin_module.JOB)[1] is None
    assert [e.action for e in audit.entries].count("siem.forwarding") == 1


def test_the_first_round_records_where_it_began_with_its_first_page(monkeypatch):
    """The forwarding's beginning is recorded with the first page queued, in its transaction, and once, however many
    pages the first round reads."""
    monkeypatch.setattr(admin_module, "PAGE", 1)
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 7200, "admin:3f9a", "key.added", CI), Entry(2, T0 - 30, "admin:3f9a", "person.added", EMILY),
              Entry(3, T0 - 20, "admin:3f9a", "key.added", CI))  # fmt: skip
    forwarder = _forwarder(audit, queue, clock, noted=audit.noted)
    while forwarder.round():
        pass
    assert [(e.id, e.action, e.details) for e in audit.entries if e.action == "siem.forwarding"] == [
        (4, "siem.forwarding", {"from": 1})
    ]
    assert [e["outcome"] for e in _sent(queue)] == ["person.added", "key.added", "siem.forwarding"]


def test_a_round_that_reads_nothing_past_the_newest_entry_says_so_once_a_minute(caplog):
    """The sequence's bound can be got around by setting the sequence by hand (`setval`): forward, so that a row past
    it is read, then back, so that the entries written next have ids below the place noted and are never read. A round
    that reads nothing while its place is past the newest entry the log holds says so in fixed words, once a minute."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY))
    audit.add(Entry(1000, T0 - 20, "x", "y", hash="not"), by_hand=True)
    audit.sequence = 1000  # set forward by hand: the row is read
    forwarder = _forwarder(audit, queue, clock)
    forwarder.round()
    audit.sequence = 1  # and back: the entries written next take ids the forwarder has passed
    audit.add(Entry(2, T0 - 10, EMILY, "role.granted", PRIYA, {"role": "admin"}))
    forwarder.round()
    forwarder.round()
    said = [r.getMessage() for r in caplog.records if r.getMessage() == admin_module.PASSED]
    assert len(said) == 1
    clock.now += 61
    forwarder.round()
    assert len([r for r in caplog.records if r.getMessage() == admin_module.PASSED]) == 2


def test_a_round_at_the_newest_entry_or_after_a_prune_of_every_entry_says_nothing(caplog):
    """No false word: a round at the newest entry, or after the log's retention pruned every entry (its anchor's last
    id kept), reads nothing and says nothing."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 30, "admin:3f9a", "person.added", EMILY), Entry(2, T0 - 20, "admin:3f9a", "key.added", CI))
    forwarder = _forwarder(audit, queue, clock)
    forwarder.round()
    forwarder.round()
    audit.entries.clear()
    audit.through = 2  # pruned: every entry
    forwarder.round()
    assert not [r for r in caplog.records if r.getMessage() == admin_module.PASSED]


def test_the_first_round_reads_the_newest_entry_before_its_window():
    """The first round's start: the newest entry is read before the window of the last hour, so that an entry
    committed between the two reads is never passed over (the re-review)."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(Entry(1, T0 - 7200, "admin:3f9a", "key.added", CI))
    real = audit.after

    def committing(after_id, limit=1000, **filters):
        found = real(after_id, limit, **filters)
        if filters.get("since") is not None and not any(e.id == 2 for e in audit.entries):
            audit.add(Entry(2, T0 - 1, "admin:3f9a", "person.added", EMILY))  # commits right after the window's read
        return found

    audit.after = committing
    _forwarder(audit, queue, clock).round()
    assert [e["outcome"] for e in _sent(queue)] == ["person.added"]


def test_an_administrator_who_adds_themselves_to_a_group_by_hand_is_named():
    """Only a sign-in records a person's own membership (source `oidc`): an administrator who adds or removes
    themselves by hand (source `local`) acts as an administrator, and is named whatever reports.people says."""
    audit, queue, clock = FakeAudit(), MemoryEvents(), Clock(T0)
    audit.add(
        Entry(1, T0 - 30, EMILY, "member.added", "g_platform00", {"identity": EMILY, "source": "local"}),
        Entry(2, T0 - 20, EMILY, "member.removed", "g_platform00", {"identity": EMILY, "source": "local"}),
        Entry(3, T0 - 10, PRIYA, "member.added", "g_ssogroup01", {"identity": PRIYA, "source": "oidc"}),
    )
    _forwarder(audit, queue, clock, people="group-only", administers=lambda identity: False).round()  # fmt: skip
    assert [e["identity"] for e in _sent(queue)] == [EMILY, EMILY, None]


@pytest.mark.server
def test_a_sequence_set_back_by_hand_is_said_in_postgresql(database, bus, caplog):
    """The re-review's `setval` on a server's backends: the sequence set forward, a row read past it, then set back;
    the round that then reads nothing says so."""
    from latet.ee.events.pg import PgEvents
    from latet.server.audit import AuditLog

    from ...server.conftest import AUDIT_KEY

    clock = Clock(T0)
    audit, queue = AuditLog(database, key=AUDIT_KEY, clock=clock), PgEvents(database)
    forwarder = AdminForwarder(audit, queue, queue, bus.lease, outputs=lambda: ["hook"], producing=lambda: True,
                               origin=ORIGIN, kind_of=_kind, group_of=lambda _: None, administers=lambda _: True,
                               people=lambda: "named", pseudonym=_pseudonym, clock=clock)  # fmt: skip
    audit.record("admin:a_first00000", "person.added", EMILY)
    with database.transaction() as conn:
        conn.execute("SELECT setval('audit_id_seq', 1000)")
    audit.record("admin:a_first00000", "key.added", CI)  # id 1001
    forwarder.round()
    with database.transaction() as conn:
        conn.execute("SELECT setval('audit_id_seq', 1)")
    audit.record(EMILY, "role.granted", PRIYA, {"role": "admin"})  # id 2: below the place noted
    forwarder.round()
    assert [r.getMessage() for r in caplog.records if r.getMessage() == admin_module.PASSED] == [admin_module.PASSED]
