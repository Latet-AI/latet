"""The jobs of an instance: each runs on a thread of its own and again when woken, a failing one stopping none, none
before it is started; the license's changes become events once whatever the number of instances, and a policy that
cannot be read now changes nothing; the retention runs once a day on one instance."""

import threading
from types import SimpleNamespace

import pytest

from latet.ee.events.jobs import Jobs, LicenseWatch, Retention
from latet.ee.events.locks import MemoryLocks
from latet.ee.events.schema import checked
from latet.ee.events.store import MemoryEvents
from latet.events import make_event

from .fakes import Clock, standing
from .receivers import until

T0 = 1_790_000_000.0
DAY = 86_400.0


def test_jobs_run_on_their_own_thread_and_a_wake_runs_the_first_at_once():
    runs: list[str] = []

    def job(name):
        def run():
            runs.append(name)
            return False

        return run

    def failing():
        raise OSError("the database restarts")

    jobs = Jobs([(60.0, job("deliver")), (60.0, failing), (60.0, job("prune"))], idle=0.05)
    assert until(lambda: runs[:2] == ["deliver", "prune"])  # a failing job stops none of the others
    jobs.wake()
    assert until(lambda: runs.count("deliver") == 2) and runs.count("prune") == 1
    jobs.close()


def test_jobs_made_unstarted_run_nothing_until_started():
    ran = threading.Event()
    jobs = Jobs([(60.0, lambda: bool(ran.set()))], idle=0.05, started=False)
    assert not ran.wait(0.3)  # I7: a server's edition starts them last, once it is whole
    jobs.start()
    assert ran.wait(5)
    jobs.close()


def test_each_change_of_the_license_is_one_event_whatever_the_number_of_instances():
    store, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    current = [standing("L-2027-0001", "valid")]
    recorded: list[dict] = []
    licensing = SimpleNamespace(standing=lambda: current[0])
    watches = [LicenseWatch(licensing, store, locks.lock, recorded.append, clock=clock) for _ in range(2)]

    def look(now):
        current[0] = now
        for watch in watches:
            watch.round()

    look(standing("L-2027-0001", "valid"))  # the first look only notes it
    look(standing("L-2027-0001", "expiring", ("expiring",)))
    look(standing("L-2027-0001", "grace", ("grace", "users-over")))
    look(standing("L-2028-0001", "valid"))  # installed: E1's committed() recorded it, the job only notes it
    look(standing("L-2028-0001", "trial-ended", ("trial-ended",)))
    look(standing(None, "invalid", ("invalid",), reason_code="signature"))
    look(standing(None, "none"))  # removed: E1's committed() recorded it
    assert [(e["type"], e["outcome"], e["reason"]) for e in recorded] == [
        ("license", "expiring", None), ("license", "grace", "users-over"), ("license", "trial-ended", None),
        ("license", "invalid", "signature"),
    ]  # fmt: skip
    assert all(checked(event) for event in recorded)


def test_each_change_of_the_policy_in_force_is_one_event_whatever_the_number_of_instances():
    store, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    licensing = SimpleNamespace(standing=lambda: standing("L-2027-0001", "valid"))
    in_force: list[str | None] = [None]
    recorded: list[dict] = []
    watches = [LicenseWatch(licensing, store, locks.lock, recorded.append, clock=clock, policy=lambda: in_force[0])
               for _ in range(2)]  # fmt: skip

    def look(etag):
        in_force[0] = etag
        for watch in watches:
            watch.round()

    look(None)  # the first look only notes
    look("a" * 64)  # a policy set (on any instance: E2's look on every instance records nothing now, R9)
    look("a" * 64)
    look("b" * 64)  # replaced
    look(None)  # removed, or the license no longer lists locked rules
    assert [(e["type"], e["reason"], e["outcome"]) for e in recorded] == [
        ("policy", "changed", "set"), ("policy", "changed", "set"), ("policy", "changed", "removed"),
    ]  # fmt: skip
    assert all(checked(event) for event in recorded)


def test_a_policy_that_cannot_be_read_now_records_nothing_and_notes_nothing():
    store, clock = MemoryEvents(), Clock(T0)
    licensing = SimpleNamespace(standing=lambda: standing("L-2027-0001", "valid"))
    readable = [True]

    def policy():
        if not readable[0]:
            raise RuntimeError("the policy is not made yet")  # as at an edition's start, before its policy (I7)
        return "a" * 64

    recorded: list[dict] = []
    watch = LicenseWatch(licensing, store, MemoryLocks(clock=clock).lock, recorded.append, clock=clock, policy=policy)
    watch.round()  # notes the policy in force
    readable[0] = False
    assert watch.round() is False
    readable[0] = True
    watch.round()
    assert recorded == []  # never "removed" then "set" on a restart


def test_the_retention_runs_once_a_day_on_one_instance():
    store, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    old = checked(make_event("exchange", clock=lambda: T0 - 40 * DAY, masked={"EMAIL": 1}))
    store.write("a" * 32, [old], at=T0)
    store.put(["gone"], [old], at=T0 - 7200)  # waits for an output no longer set
    jobs = [
        Retention(store, store, locks.lock, days=lambda: 30, outputs=lambda: ["hook"], clock=clock) for _ in range(2)
    ]
    assert [job.round() for job in jobs] == [True, False]
    assert store.query(0, T0 + 1) == [] and store.state("gone").queued == 0
    clock.now += 3600
    assert [job.round() for job in jobs] == [False, False]  # a day has not passed
    clock.now += DAY
    assert jobs[1].round() is True


def test_a_retention_that_cannot_read_how_long_to_keep_prunes_nothing():
    store, clock = MemoryEvents(), Clock(T0)
    old = checked(make_event("exchange", clock=lambda: T0 - 40 * DAY, masked={"EMAIL": 1}))
    store.write("a" * 32, [old], at=T0)

    def unread():
        raise OSError("the database restarts")  # never the default 365 days in its place (M11)

    retention = Retention(store, store, MemoryLocks(clock=clock).lock, days=unread, outputs=lambda: [], clock=clock)
    with pytest.raises(OSError, match="the database restarts"):
        retention.round()  # the jobs' thread says it, by its type, and tries again at its next turn
    assert store.query(0, T0 + 1) != []


# --- beyond the brief ---------------------------------------------------------------------------------------------------


def test_a_failing_job_is_said_by_its_type_alone(caplog):
    """R14: a driver's words can name an address or a value; the jobs' thread says a failure by its type."""
    said = threading.Event()

    def failing():
        said.set()
        raise OSError("connection to 10.0.0.9 failed: password authentication failed for user latet")

    jobs = Jobs([(60.0, failing)], idle=0.05)
    assert said.wait(5) and until(lambda: any("(OSError)" in r.getMessage() for r in caplog.records))
    jobs.close()
    assert not any("10.0.0.9" in r.getMessage() or "password" in r.getMessage() for r in caplog.records)


def test_a_job_is_added_before_the_jobs_start_and_never_after(caplog):
    """R73 (E5): a later part adds its job (the record of approvals' prune) before the edition starts the jobs, named
    as the others in a failure; once they started, or once closed, none is added: the thread reads the list."""
    ran = threading.Event()

    def prune():
        ran.set()
        raise OSError("connection to 10.0.0.9 failed")

    prune.__qualname__ = "CentralApprovals.prune"
    jobs = Jobs([(60.0, lambda: False)], idle=0.05, started=False)
    jobs.add(3600.0, prune)
    assert [every for every, _ in jobs.jobs] == [60.0, 3600.0] and not ran.wait(0.2)
    jobs.start()
    assert ran.wait(5)
    assert until(lambda: any("job CentralApprovals.prune failed (OSError)" in r.getMessage() for r in caplog.records))
    with pytest.raises(RuntimeError):
        jobs.add(60.0, lambda: False)
    jobs.close()
    closed = Jobs([], started=False)
    closed.close()
    with pytest.raises(RuntimeError):
        closed.add(60.0, lambda: False)
    assert closed.jobs == []


def test_jobs_closed_stop_and_never_start_again():
    ran = threading.Event()
    jobs = Jobs([(60.0, lambda: bool(ran.set()))], idle=0.05, started=False)
    jobs.close()
    jobs.close()  # twice: nothing to stop
    jobs.start()  # closed: never starts
    assert not ran.wait(0.3)
    assert jobs._thread.ident is None  # no thread at all: one that started would only find itself closed


def test_a_license_note_of_another_shape_is_a_first_look_again():
    """A note no code of Latet writes (written by hand) says nothing of the license before: the look only notes the
    license as it is now, never an event made of a state it cannot read."""
    store, clock = MemoryEvents(), Clock(T0)
    licensing = SimpleNamespace(standing=lambda: standing("L-2027-0001", "grace", ("grace",)))
    recorded: list[dict] = []
    watch = LicenseWatch(licensing, store, MemoryLocks(clock=clock).lock, recorded.append, clock=clock)
    store.set_job("license", T0, "not JSON")
    assert watch.round() is False and recorded == []
    store.set_job("license", T0, '["id", "state"]')
    assert watch.round() is False and recorded == []
    assert watch.round() is False and recorded == []  # noted now: nothing changed since


def test_a_job_another_instance_holds_does_nothing_here():
    """One instance at a time: while another holds the license's lock, or the retention's, this instance's look notes
    nothing and records nothing, and its retention prunes nothing (on a server, Valkey's lock)."""
    store, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    recorded: list[dict] = []
    licensing = SimpleNamespace(standing=lambda: standing("L-2027-0001", "valid"))
    watch = LicenseWatch(licensing, store, locks.lock, recorded.append, clock=clock, policy=lambda: "a" * 64)
    old = checked(make_event("exchange", clock=lambda: T0 - 40 * DAY, masked={"EMAIL": 1}))
    store.write("a" * 32, [old], at=T0)
    retention = Retention(store, store, locks.lock, days=lambda: 30, outputs=lambda: [], clock=clock)
    with locks.lock("events:license", 60) as theirs, locks.lock("events:retention", 600) as also:
        assert theirs and also
        assert watch.round() is False and retention.round() is False
    assert store.job("license") == (None, None) and store.job("retention") == (None, None)
    assert store.query(0, T0 + 1) != []


# --- fix round 1 (the review) -----------------------------------------------------------------------------------------


class Delivery:
    """A job of the events, as the SIEM's delivery is one: a method of an object."""

    def __init__(self, said):
        self.said = said

    def round(self):
        self.said.set()
        raise OSError("connection to 10.0.0.9 failed")


def test_a_failing_job_is_named_in_fixed_words(caplog):
    """The jobs' thread names the job that failed (the review): by its function's name, a name of the code, or by its
    place when it has none of that form (a function made inside another); never by anything a job holds."""
    said = threading.Event()

    def failing():
        raise OSError("connection to 10.0.0.9 failed")

    jobs = Jobs([(60.0, failing), (60.0, Delivery(said).round)], idle=0.05)
    assert said.wait(5)
    assert until(lambda: len([r for r in caplog.records if "failed" in r.getMessage()]) >= 2)
    jobs.close()
    messages = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert "the events' job 1 failed (OSError): it is tried again at its next turn" in messages
    assert "the events' job Delivery.round failed (OSError): it is tried again at its next turn" in messages


class _Interleaved:
    """A store whose note another instance moves right after this one read it: the other's look runs between this
    look's read and its write (its lock ran out meanwhile)."""

    def __init__(self, store, other):
        self.store, self.other, self.done = store, other, False

    def __getattr__(self, name):
        return getattr(self.store, name)

    def job(self, name):
        found = self.store.job(name)
        if not self.done:
            self.done = True
            self.other.round()
        return found


def test_a_license_look_writes_its_note_only_over_the_note_it_read():
    """Two instances whose looks overlap (a lock that ran out) each read the note before either writes: the note is
    written only over the one read, as the forwarder's is, and the event recorded only by the look whose write won.
    Once whatever the number of instances, though the lock alone did not hold."""
    store, clock = MemoryEvents(), Clock(T0)
    current = [standing("L-2027-0001", "valid")]
    in_force: list[str | None] = [None]
    licensing = SimpleNamespace(standing=lambda: current[0])
    recorded: list[dict] = []

    def watch(where, locks):
        return LicenseWatch(licensing, where, locks.lock, recorded.append, clock=clock, policy=lambda: in_force[0])

    other = watch(store, MemoryLocks(clock=clock))
    watch(store, MemoryLocks(clock=clock)).round()  # the first look only notes
    current[0], in_force[0] = standing("L-2027-0001", "expiring", ("expiring",)), "a" * 64
    first = watch(_Interleaved(store, other), MemoryLocks(clock=clock))
    assert first.round() is False
    assert [(e["type"], e["outcome"]) for e in recorded] == [("policy", "set"), ("license", "expiring")]


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.server)])
def events(request):
    if request.param == "memory":
        return MemoryEvents()
    from latet.ee.events.pg import PgEvents

    return PgEvents(request.getfixturevalue("database"))


def test_a_jobs_note_is_written_only_over_the_note_read(events):
    """``swap_job``: a job's note written only while it is still the one read (None: no note yet), in memory and in
    PostgreSQL, where the first two writes of a note that was not there never both win."""
    assert events.swap_job("license", T0, "one", expected=None) is True
    assert events.swap_job("license", T0 + 1, "two", expected=None) is False
    assert events.job("license") == (T0, "one")
    assert events.swap_job("license", T0 + 2, "two", expected="one") is True
    assert events.job("license") == (T0 + 2, "two")
