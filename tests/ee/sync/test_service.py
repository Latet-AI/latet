"""The term sync: a run masks its terms at once, one that fails, gives nothing or passes the bounds changes nothing,
one instance reads at a time and a run whose lease ran out applies nothing, a sync asked for runs where the lease is,
every instance takes a change at once or at its next tick, no space is built before the terms and the license were
read, and the events hold names and counts only."""

import json
import logging
import threading
import time
from contextlib import contextmanager

import pytest

from latet.ee.events.locks import MemoryLocks
from latet.ee.sync import service
from latet.ee.sync.records import MAX_BYTES, SourceError, bounds
from latet.ee.sync.service import CHANNEL, LEASE, LEASE_SECONDS, SyncPaused
from latet.ee.sync.sources import TIMEOUT
from latet.ee.sync.store import MemoryTermStore
from latet.engine.detectors.base import DetectContext
from latet.floor import TermsUnavailable
from latet.server.egress import RESOLVE_SECONDS

from .fakes import START, Clock, Published, make_sync, sources

CUSTOMERS = {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
PROJECTS = {"name": "projects", "kind": "url", "url": "https://intranet.acme.com/api/projects",
            "mapping": {"code_name": "PROJECT"}}  # fmt: skip
BOTH = [{"company": "Halden Insurance"}, {"company": "Kestrel Analytics"}]


def masked(sync, text):
    return [text[s.start : s.end] for s in sync.live.detect(text, DetectContext())]


def tag_of(store, setup):
    """The tag another instance gives the terms ``store`` holds now: its first load of them."""
    other = make_sync(store, locks=setup.locks, clock=setup.clock)
    other.reload()
    other.close()
    return other.live.tag


class Setup:
    """One instance's term sync whose source `customers` gives what ``records`` holds: records, an error to raise, or a
    function that makes them."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.locks = MemoryLocks(self.clock)
        self.events, self.published = [], Published()
        self.records = BOTH
        self.settings = {"now": sources(CUSTOMERS)}
        self.sync = make_sync(locks=self.locks, settings=self.settings, events=self.events, clock=self.clock,
                              readers={"file": self.reader}, published=self.published)  # fmt: skip

    def reader(self, source, secret):
        given = self.records
        if isinstance(given, Exception):
            raise given
        return given() if callable(given) else given

    def other(self, **changes):
        return make_sync(self.sync.store, locks=self.locks, settings=self.settings, clock=self.clock,
                         readers={"file": self.reader}, **changes)  # fmt: skip


@pytest.fixture
def setup():
    made = Setup()
    yield made
    made.sync.close()


def test_a_run_masks_its_terms_at_once_and_one_gone_from_its_source_stays_masked(setup):
    [run] = setup.sync.tick()
    assert (run.outcome, run.found, run.added, run.kind) == ("ok", 2, 2, "file")
    assert masked(setup.sync, "Halden Insurance and KESTREL ANALYTICS") == ["Halden Insurance", "KESTREL ANALYTICS"]
    setup.records = [{"company": "Kestrel Analytics"}]
    setup.clock.now += 3600
    [run] = setup.sync.tick()
    assert run.gone == 1 and masked(setup.sync, "Halden Insurance renewed") == ["Halden Insurance"]
    assert setup.sync.status()["sources"][0]["gone"] == 1


@pytest.mark.parametrize(
    ("given", "code"),
    [
        (SourceError("refused", "Microsoft Entra ID answered 401 (invalid_client)"), "refused"),
        (SourceError("unreachable", "the directory did not answer (LDAPSocketOpenError)"), "unreachable"),
        ([], "empty"),
        ([{"company": ""}], "empty"),
    ],
)
def test_a_source_that_fails_or_suddenly_gives_nothing_changes_nothing(setup, given, code):
    setup.sync.tick()
    version = setup.sync.store.version()
    setup.records = given
    setup.clock.now += 3600
    [run] = setup.sync.tick()
    assert (run.outcome, run.code) == ("failed", code) and run.reason
    assert masked(setup.sync, "Halden Insurance and Kestrel Analytics") == ["Halden Insurance", "Kestrel Analytics"]
    assert setup.sync.store.gone()[1] == 0 and setup.sync.store.version() == version
    assert [e["outcome"] for e in setup.events] == ["ok", "failed"] and setup.events[1]["reason"] == code
    setup.records = BOTH
    setup.clock.now += 15 * 60  # tried again after 15 minutes, not after its hour
    assert [r.outcome for r in setup.sync.tick()] == ["ok"]


def test_a_run_past_the_terms_kept_at_most_changes_nothing(setup, monkeypatch):
    setup.sync.tick()
    monkeypatch.setattr(service, "MOST_KEPT", 2)
    setup.records = [{"company": "Halden Insurance"}, {"company": "Kestrel Analytics"}, {"company": "Maple Freight"}]
    setup.clock.now += 3600
    [run] = setup.sync.tick()
    assert (run.outcome, run.code) == ("failed", "too-many") and "Maple" not in run.reason
    assert masked(setup.sync, "Maple Freight") == []


def test_the_events_carry_counts_and_never_a_term_and_name_people_only(setup):
    setup.sync.ask(None, by="u_sec0000001")
    setup.sync.tick()
    setup.clock.now += 1
    setup.sync.ask(None, by=None)  # the administration's: its token is in the administration log
    setup.clock.now += 1
    setup.sync.tick()
    person, token = setup.events
    assert (person["type"], person["rule"], person["by"]) == ("sync", "term-sync:customers", "u_sec0000001")
    assert token["by"] is None and person["masked"] == {"CLIENT": 2}
    assert "Halden" not in json.dumps(setup.events)


def test_the_scheduler_syncs_a_source_when_it_is_due_or_asked_for(setup):
    assert len(setup.sync.tick()) == 1  # never synced: due at once
    setup.clock.now += 59 * 60
    assert setup.sync.tick() == []
    assert setup.sync.ask("customers", by=None) == ["customers"]
    setup.clock.now += 1
    assert len(setup.sync.tick()) == 1 and setup.sync.store.asks() == {}  # asked: due now, then no longer asked
    with pytest.raises(KeyError):
        setup.sync.ask("crm", by=None)


def test_a_sync_asked_for_names_the_sources_its_caller_read_never_this_instances_copy(setup):
    """Task 11: a route asks with the sources as its own transaction reads them, so that a source another instance has
    just set is found before this instance's copy of the settings has it, and one just taken out is not."""
    from latet.ee.settings import TermSyncSettings

    now = TermSyncSettings.model_validate({"sources": [CUSTOMERS, PROJECTS]}).sources
    assert setup.sync.ask("projects", by=None, sources=now) == ["projects"]
    assert setup.sync.ask(None, by=None, sources=now) == ["customers", "projects"]
    with pytest.raises(KeyError):
        setup.sync.ask("customers", by=None, sources=now[1:])
    assert set(setup.sync.store.asks()) == {"customers", "projects"}


def test_while_the_license_pauses_term_sync_nothing_is_read_and_the_terms_stay(setup):
    setup.sync.tick()
    setup.sync.licensing.producing_ = False
    setup.clock.now += 3600
    assert setup.sync.tick() == []
    with pytest.raises(SyncPaused):
        setup.sync.ask(None, by=None)
    assert masked(setup.sync, "Halden Insurance") == ["Halden Insurance"] and setup.sync.status()["paused"]


def test_without_a_license_for_term_sync_no_synced_term_is_masked(setup):
    setup.sync.tick()
    setup.sync.licensing.licensed_ = False  # a license removed, or one without term-sync installed (R29)
    setup.sync.tick()
    assert masked(setup.sync, "Halden Insurance") == [] and setup.sync.live.tag == ""  # no term: 0.11's keys


def test_one_instance_reads_at_a_time(setup):
    other = setup.other()
    started, release, reads = threading.Event(), threading.Event(), []

    def slow():
        reads.append("customers")
        started.set()
        release.wait(5)
        return BOTH

    setup.records = slow
    first = threading.Thread(target=setup.sync.tick)
    first.start()
    assert started.wait(5)
    assert other.tick() == [] and reads == ["customers"]  # the first instance holds the lease
    release.set()
    first.join(5)
    other.close()


def test_a_run_whose_lease_ran_out_applies_nothing_and_another_instance_syncs_after(setup):
    other = setup.other()

    def outlived():
        setup.clock.now += LEASE_SECONDS  # the read took the whole lease: another may hold it now
        return BOTH

    setup.records = outlived
    [run] = setup.sync.tick()
    assert (run.outcome, run.code) == ("failed", "timeout") and setup.sync.store.kept() == 0
    setup.records = BOTH
    setup.clock.now += 15 * 60
    assert [r.outcome for r in other.tick()] == ["ok"]
    other.close()


def test_an_instance_that_dies_holding_the_lease_frees_it_when_it_ends(setup):
    other = setup.other()
    with setup.locks.lease(LEASE, LEASE_SECONDS) as dead:  # an instance took it and stopped
        assert dead
        assert other.tick() == []
        setup.clock.now += LEASE_SECONDS
        assert [r.outcome for r in other.tick()] == ["ok"]
    other.close()


def test_an_instance_that_missed_the_message_takes_the_terms_at_its_next_tick(setup):
    other = setup.other()
    setup.sync.tick()
    assert masked(other, "Halden Insurance") == []
    other.tick()  # it does not hold the lease if the first still did: it reloads all the same
    assert masked(other, "Halden Insurance") == ["Halden Insurance"]
    other.close()


def test_each_run_is_masked_on_every_instance_before_the_next_source_is_read(setup):
    """Fix round 1's I5: the instance that syncs publishes and reloads after each run it applies, and an instance
    woken by another's change reloads before it runs any source of its own."""
    other = setup.other()
    started, release = threading.Event(), threading.Event()

    def blocked(source, secret):
        started.set()
        release.wait(5)
        return [{"code_name": "Project Falcon"}]

    setup.settings["now"] = sources(CUSTOMERS, PROJECTS)
    setup.sync.readers["url"] = blocked
    first = threading.Thread(target=setup.sync.tick)
    first.start()
    assert started.wait(5)  # customers applied; projects being read
    assert masked(setup.sync, "Halden Insurance") == ["Halden Insurance"]
    assert setup.published and setup.published[0][1].keys() == {"version"}
    other.tick()  # the lease is the first's: the other reloads all the same, before anything of its own
    assert masked(other, "Halden Insurance") == ["Halden Insurance"]
    release.set()
    first.join(5)
    other.close()


def test_a_change_is_announced_with_its_version_alone(setup):
    setup.sync.tick()
    assert setup.published == [(CHANNEL, {"version": setup.sync.store.version()})]
    setup.sync.ask(None, by=None)
    setup.sync.asked()
    assert setup.published[-1] == (CHANNEL, {"asked": 1})


class _Starting(MemoryTermStore):
    """A database that does not answer yet."""

    answering = False

    def version(self):
        if not self.answering:
            raise ConnectionError("the database is starting")
        return super().version()


def test_no_space_is_built_before_the_terms_and_the_license_were_read(setup):
    store = _Starting()
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    sync = make_sync(store, locks=setup.locks, clock=setup.clock)
    with pytest.raises(TermsUnavailable):
        sync.ready()  # the database does not answer
    store.answering = True
    sync.licensing.licensed_, sync.licensing.knows_ = False, False  # the license never read on this instance
    with pytest.raises(TermsUnavailable):
        sync.ready()
    sync.licensing.licensed_, sync.licensing.knows_ = True, True
    assert sync.ready() is sync.live and masked(sync, "Halden Insurance") == ["Halden Insurance"]
    store.answering = False
    assert sync.ready() is sync.live  # once loaded, a database that stops answering keeps the terms in use
    assert masked(sync, "Halden Insurance") == ["Halden Insurance"]
    sync.close()


def test_removing_takes_terms_no_longer_in_their_source_and_is_recorded_with_counts_only(setup):
    setup.sync.tick()
    setup.records = [{"company": "Kestrel Analytics"}]
    setup.clock.now += 3600
    setup.sync.tick()
    removed = setup.sync.remove(source="customers")
    setup.sync.removed(removed, source="customers", by="u_sec0000001")
    setup.sync.reload()  # what the woken thread does (its own, not started in these tests)
    assert removed == {"CLIENT": 1}
    assert masked(setup.sync, "Halden Insurance and Kestrel Analytics") == ["Kestrel Analytics"]
    assert (setup.events[-1]["outcome"], setup.events[-1]["masked"]) == ("removed", {"CLIENT": 1})


def test_a_source_taken_out_leaves_its_terms_gone_at_once_and_still_masked(setup):
    setup.sync.tick()
    setup.settings["now"] = sources(PROJECTS)
    assert setup.sync.retire(["projects"]) == 2
    setup.sync.changed()
    assert setup.published[-1][1].keys() == {"version"}  # told; this instance's thread reloads, woken
    setup.sync.reload()
    assert setup.sync.store.gone()[1] == 2 and masked(setup.sync, "Halden Insurance") == ["Halden Insurance"]


def test_a_source_taken_out_and_set_up_again_runs_at_once_never_at_its_old_runs_hour(setup):
    """The final review's M4: the route forgets the last run of a source taken out, changed or added, in the change's
    transaction (``forget``): set up again, it runs at the next look, as the docs say (within a minute)."""
    [first] = setup.sync.tick()
    setup.clock.now += 60  # well within its hour
    setup.settings["now"] = sources()
    setup.sync.retire([])
    setup.sync.forget(["customers"])
    assert setup.sync.store.runs() == {} and setup.sync.tick() == []
    setup.clock.now += 1
    setup.settings["now"] = sources(CUSTOMERS | {"mapping": {"company": "PERSON"}})
    [run] = setup.sync.tick()
    assert run.started > first.started and setup.sync.store.counts()["customers"]["by_type"] == {"PERSON": 2}


def test_the_status_says_each_source_and_never_a_term(setup):
    setup.sync.tick()
    state = setup.sync.status()
    assert state["sources"][0] | {"last": None, "next": None} == {
        "name": "customers", "kind": "file", "every_minutes": 60, "configured": True, "terms": 2,
        "by_type": {"CLIENT": 2}, "gone": 0, "asked": None, "last": None, "next": None,
    }  # fmt: skip
    assert (state["masked"], state["kept"], state["most"]) == (2, 2, 50_000) and "Halden" not in json.dumps(state)
    assert state["behind"] is None


def test_an_instance_whose_read_hangs_still_takes_what_another_applies(setup, monkeypatch):
    """The design security review's B-I1: the terms are reloaded on a thread of their own, never behind a read."""
    monkeypatch.setattr(service, "TICK", 0.05)
    started, release = threading.Event(), threading.Event()

    def hung():
        started.set()
        release.wait(10)
        return BOTH

    setup.records = hung
    first = threading.Thread(target=setup.sync.tick)
    first.start()
    assert started.wait(5)
    setup.clock.now += LEASE_SECONDS  # the first instance's turn ran out while it reads
    other = make_sync(setup.sync.store, locks=setup.locks, settings=setup.settings, clock=setup.clock,
                      readers={"file": lambda source, secret: [{"company": "Maple Freight"}]})  # fmt: skip
    assert [r.outcome for r in other.tick()] == ["ok"]
    setup.sync.start_reloading()
    setup.sync.wake()  # what E1's listener does on ee:terms
    deadline = time.monotonic() + 5
    while masked(setup.sync, "Maple Freight") != ["Maple Freight"]:
        assert time.monotonic() < deadline, "the term another instance applied was not taken while the read hung"
        time.sleep(0.01)
    release.set()
    first.join(5)
    other.close()


def test_a_read_that_never_ends_fails_timeout_at_the_end_of_its_turn(setup):
    """The design security review's B-I1: a run reads under its lease's turn less the margin to apply, checked inside
    the reader (`bounds()`), whatever each socket's own timeout."""

    def endless():
        for _ in range(100):  # a page a minute; 100 pages pass the turn many times over
            bounds().check()
            setup.clock.now += 60
        # fails the test (code `error`), never hangs
        raise AssertionError("no deadline: the run read without its bounds")

    setup.records = endless
    [run] = setup.sync.tick()
    assert (run.outcome, run.code) == ("failed", "timeout") and setup.sync.store.kept() == 0


def test_a_read_that_ends_at_its_deadline_is_still_applied_within_its_turn(setup):
    """Root's ruling on B-I1's margin: a reader checks the deadline between two reads, so past it a read may still end
    one request (its timeout, after egress's name lookup) and parse what it read (YAML, the slowest, at about 0.4 MB a
    second: the largest document in about 2 minutes). The deadline leaves the lease room for both and for the apply:
    such a read is applied while the lease is still its own, and no other instance reads meanwhile."""

    def last_request_at_the_deadline():
        limits = bounds()
        setup.clock.now = limits.until - 1  # the last request begins with a second left
        limits.check()
        setup.clock.now += TIMEOUT + RESOLVE_SECONDS  # its name looked up, its answer read at the last moment
        setup.clock.now += MAX_BYTES / 400_000  # the largest document parsed, as YAML
        return BOTH

    other = setup.other()
    setup.records = last_request_at_the_deadline
    [run] = setup.sync.tick()
    assert run.outcome == "ok" and masked(setup.sync, "Halden Insurance") == ["Halden Insurance"]
    other.close()


class _Healing(MemoryTermStore):
    """A store whose `apply` heals the rows the last `terms()` could not open, as PgTermStore's does (B-R5): it notes
    whether that read was of the store as it stands when a run applies."""

    def __init__(self) -> None:
        super().__init__()
        self.read: int | None = None  # the version the last terms() read
        self.reads = 0
        self.current: list[bool] = []

    def terms(self):
        self.read, self.reads = self.version(), self.reads + 1
        return super().terms()

    def apply(self, source, terms, now, **kwargs):
        self.current.append(self.read == self.version())
        return super().apply(source, terms, now, **kwargs)


def test_a_run_reads_the_terms_again_before_it_applies_when_the_store_moved_since_the_reload(setup):
    """Root's ruling on B-R5: one store serves the reload and the runs, and a run reads the terms again before it
    applies when the reload's read is older than the store (a route took a source out while the run read), so that
    `apply` heals what no key opened in the rows as they are; a reload as new as the store is not read again."""
    store = _Healing()
    MemoryTermStore.apply(store, "projects", [("Project Falcon", "PROJECT")], now=1.0)
    taken_out = []

    def read(source, secret):
        if taken_out:  # a route takes the source `projects` out while this run reads
            store.retire(["customers"], now=setup.clock.now)
        return BOTH

    sync = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock, readers={"file": read})
    sync.tick()
    assert store.current == [True] and store.reads == 2  # the reload's read before the run, and after it
    taken_out.append(True)
    setup.clock.now += 3600
    sync.tick()
    assert store.current == [True, True]
    sync.close()


def test_a_fault_in_latet_fails_the_run_in_fixed_words_that_quote_nothing_it_read(setup, caplog):
    """Root's ruling: anything a reader raises but a SourceError is `error`, in Latet's own words: the exception's may
    quote a record, a host or a path, so neither the run's reason, nor its event, nor a log line holds them."""
    setup.records = RuntimeError("Halden Insurance, read from intranet.acme.com/exports/customers.csv")
    with caplog.at_level(logging.INFO):
        [run] = setup.sync.tick()
    assert (run.outcome, run.code, run.reason) == ("failed", "error", "an error in Latet (RuntimeError)")
    said = json.dumps([setup.events, setup.published, run.reason, caplog.text])
    assert "RuntimeError" in caplog.text and not any(w in said for w in ("Halden", "acme", "exports", "customers.csv"))


def test_started_it_syncs_what_is_due_on_its_own_and_its_threads_end_when_closed(setup, monkeypatch):
    monkeypatch.setattr(service, "TICK", 0.05)
    setup.sync.start()
    deadline = time.monotonic() + 5
    while masked(setup.sync, "Halden Insurance") != ["Halden Insurance"]:
        assert time.monotonic() < deadline, "the started term sync did not sync its source"
        time.sleep(0.01)
    threads = [t for t in threading.enumerate() if t.name in ("latet-term-sync", "latet-term-reload")]
    setup.sync.close()
    assert sorted(t.name for t in threads) == ["latet-term-reload", "latet-term-sync"]
    assert not any(t.is_alive() for t in threads)


class _OneSealedElsewhere(MemoryTermStore):
    """A store holding a term this instance's master key cannot open (a version its key file lacks)."""

    hiding = False

    def terms(self):
        found = super().terms()
        self.unreadable = 1 if self.hiding else 0
        return [t for t in found if not (self.hiding and t.value == "Kestrel Analytics")]


def test_terms_this_instance_cannot_open_keep_the_terms_in_use_and_their_tag(setup):
    """The design security review's B-I2: never fewer terms, under any tag; a first load builds no space."""
    store = _OneSealedElsewhere()
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    sync = make_sync(store, locks=setup.locks, clock=setup.clock)
    sync.reload()
    tag = sync.live.tag
    store.apply("customers", [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")], now=2.0)
    store.hiding = True
    with pytest.raises(TermsUnavailable):
        sync.reload()
    assert sync.live.tag == tag and masked(sync, "Halden Insurance") == ["Halden Insurance"]
    assert sync.status()["unreadable"] == 1
    fresh = make_sync(store, locks=setup.locks, clock=setup.clock)
    with pytest.raises(TermsUnavailable):
        fresh.ready()  # never loaded: no space is built
    sync.close()
    fresh.close()


class _ChangedWhileRead(MemoryTermStore):
    """A store another instance changes between this one's read of the version and its read of the terms (once)."""

    change: list | None = None

    def terms(self):
        if self.change is not None:
            change, self.change = self.change, None
            self.apply("customers", change, now=9.0)
        return super().terms()


def test_a_reload_never_names_newer_terms_with_an_older_tag(setup):
    """B-M3, pinned (A-R6): the store's version is read again after its terms, and the swap waits until they agree."""
    store = _ChangedWhileRead()
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    sync = make_sync(store, locks=setup.locks, clock=setup.clock)
    store.change = [("Halden Insurance", "CLIENT"), ("Maple Freight", "CLIENT")]
    sync.reload()
    assert masked(sync, "Maple Freight") == ["Maple Freight"]
    assert sync.live.tag == tag_of(store, setup)  # the tag of the terms it masks, as every instance names them
    sync.close()


def test_an_instance_whose_terms_trail_the_store_says_so(setup, monkeypatch):
    """The design security review's B-I1 (c): `status()["behind"]`, which `latet server status` says past two ticks."""
    setup.sync.tick()
    loaded = setup.sync.store.version()
    setup.sync.store.apply("customers", [("Maple Freight", "CLIENT")], now=setup.clock.now)
    monkeypatch.setattr(setup.sync.store, "terms", lambda: (_ for _ in ()).throw(OSError("the database went away")))
    with pytest.raises(OSError, match="went away"):
        setup.sync.reload()
    setup.clock.now += 2 * service.TICK + 1
    behind = setup.sync.status()["behind"]
    assert (behind["loaded"], behind["store"]) == (loaded, setup.sync.store.version())
    assert behind["seconds"] > 2 * service.TICK and masked(setup.sync, "Halden Insurance") == ["Halden Insurance"]


# --- fix round 1: what the review of this task's code found ----------------------------------------------------------


class _Moving(MemoryTermStore):
    """A store another instance changes during each read of its terms (a burst of runs), until ``moving`` is False."""

    moving = True
    changes = 0

    def terms(self):
        found = super().terms()
        if self.moving:
            self.changes += 1
            extra = (f"Extra {self.changes:04d} Ltd", "CLIENT")
            self.apply("customers", [("Halden Insurance", "CLIENT"), extra], now=float(self.changes))
        return found


def test_a_space_is_never_given_terms_never_loaded_while_the_store_keeps_moving(setup):
    """Important 1: a reload that gives up while the store moves under every read leaves the terms unloaded, and
    `ready()` refuses the space rather than hand it no synced term at all."""
    store = _Moving()
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=0.5)
    sync = make_sync(store, locks=setup.locks, clock=setup.clock)
    with pytest.raises(TermsUnavailable):
        sync.ready()
    assert not sync.loaded and sync.live.tag == ""
    store.moving = False
    assert sync.ready() is sync.live and masked(sync, "Halden Insurance") == ["Halden Insurance"]
    sync.close()


def test_a_source_taken_out_while_it_is_read_gets_none_of_its_terms_back(setup):
    """Important 2: a route takes the source out (its terms retired) while it is read; what the read gave is dropped,
    no run recorded, and its terms stay gone, so that a security person can remove them."""
    setup.sync.tick()

    def taken_out():
        setup.settings["now"] = sources()
        setup.sync.retire([])
        return BOTH

    setup.records = taken_out
    setup.clock.now += 3600
    assert setup.sync.tick() == []
    assert setup.sync.store.counts() == {"customers": {"terms": 0, "by_type": {}, "gone": 2}}
    assert setup.sync.store.runs()["customers"].started == START  # the first run's: none recorded since
    assert setup.sync.remove(source="customers") == {"CLIENT": 2}


def test_a_source_changed_while_it_is_read_is_read_again_as_it_now_stands(setup):
    """Important 2: a source whose definition changed while it was read: what the read gave is not applied, and the
    source, still due, is read at the next tick under the definition it now has."""
    setup.sync.tick()

    def changed():
        setup.settings["now"] = sources(CUSTOMERS | {"mapping": {"name": "CLIENT"}})
        setup.records = [{"name": "Maple Freight"}]
        return BOTH

    setup.records = changed
    setup.clock.now += 3600
    version = setup.sync.store.version()
    assert setup.sync.tick() == [] and setup.sync.store.version() == version
    [run] = setup.sync.tick()
    assert (run.outcome, run.found, run.gone) == ("ok", 1, 2)
    assert masked(setup.sync, "Maple Freight") == ["Maple Freight"]


class _TakenOutBeforeApply(MemoryTermStore):
    """A store a route changes once just before a run applies: ``hook`` runs then."""

    hook = None

    def apply(self, source, terms, now, **kwargs):
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return super().apply(source, terms, now, **kwargs)


def test_a_source_taken_out_just_before_its_run_applies_is_taken_out_again_after(setup):
    """Important 2: a removal that lands between the check of the settings and the apply, which marks the source's
    terms back in it, is followed by the source's terms marked gone again, and no run recorded."""
    store = _TakenOutBeforeApply()
    store.apply("projects", [("Project Falcon", "PROJECT")], now=1.0)  # another source's terms: left alone
    sync = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock,
                     readers={"file": setup.reader})  # fmt: skip
    sync.tick()

    def route():
        setup.settings["now"] = sources()
        store.retire(["projects"], now=setup.clock.now)

    store.hook = route
    setup.clock.now += 3600
    assert sync.tick() == []
    counts = store.counts()
    assert counts["customers"] == {"terms": 0, "by_type": {}, "gone": 2}
    assert counts["projects"] == {"terms": 1, "by_type": {"PROJECT": 1}, "gone": 0}
    assert store.runs()["customers"].started == START
    sync.close()


class _SlowRead(MemoryTermStore):
    """A store whose read of the terms takes ``slow`` seconds of the test's clock (a database under load)."""

    def __init__(self, clock) -> None:
        super().__init__()
        self.clock, self.slow = clock, 0.0

    def terms(self):
        self.clock.now += self.slow
        return super().terms()


def test_a_run_whose_lease_runs_out_while_the_terms_are_read_again_applies_nothing_and_is_not_recorded(setup):
    """Minors 3 and 4: the lease is asked again right before the apply, and a run is recorded as its source's last only
    while the lease is still this instance's (a late one never overwrites a newer holder's); its event still says it."""
    store, events = _SlowRead(setup.clock), []

    def read(source, secret):
        # the store moved meanwhile: the run reads the terms again before it applies
        MemoryTermStore.apply(store, "projects", [("Project Falcon", "PROJECT")], now=setup.clock.now)
        store.slow = LEASE_SECONDS  # and that read takes the rest of the lease
        return BOTH

    sync = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock, events=events,
                     readers={"file": read})  # fmt: skip
    [run] = sync.tick()
    assert (run.outcome, run.code) == ("failed", "timeout") and "customers" not in store.counts()
    assert store.runs() == {} and (events[-1]["outcome"], events[-1]["reason"]) == ("failed", "timeout")
    sync.close()


class _Failover:
    """Valkey's lease, which a failover loses while it is held: once ``lost``, ``held()`` says so."""

    def __init__(self, clock) -> None:
        self.clock, self.lost, self.asked = clock, False, []

    @contextmanager
    def lease(self, name, seconds):
        yield _FailoverLease(self)


class _FailoverLease:
    def __init__(self, locks) -> None:
        self._locks = locks

    def __bool__(self):
        return True

    def held(self, margin=0.0):
        self._locks.asked.append(margin)  # each question is one call to Valkey
        return not self._locks.lost

    def release(self):
        self._locks.lost = True


def test_a_tick_reads_no_further_source_once_its_lease_is_lost(setup):
    """Minor 4: each source's turn begins by asking whether the lease is still this instance's, with a whole read's
    time left: after a failover another instance holds it and reads, so this one stops."""
    locks, read = _Failover(setup.clock), []

    def customers(source, secret):
        locks.lost = True
        return BOTH

    def projects(source, secret):
        read.append("projects")
        return [{"code_name": "Project Falcon"}]

    setup.settings["now"] = sources(CUSTOMERS, PROJECTS)
    sync = make_sync(locks=locks, settings=setup.settings, clock=setup.clock,
                     readers={"file": customers, "url": projects})  # fmt: skip
    [run] = sync.tick()
    assert (run.outcome, run.code) == ("failed", "timeout") and read == [] and sync.store.runs() == {}
    sync.close()


def test_a_tick_asks_whether_its_turn_goes_on_only_before_a_source_that_is_due(setup):
    """The re-review's nit: Valkey is asked once for each source read, never for one that is not due."""
    locks = _Failover(setup.clock)
    setup.settings["now"] = sources(CUSTOMERS, PROJECTS | {"every_minutes": 15})
    sync = make_sync(locks=locks, settings=setup.settings, clock=setup.clock,
                     readers={"file": setup.reader, "url": lambda source, secret: [{"code_name": "Project Falcon"}]})  # fmt: skip
    assert len(sync.tick()) == 2
    setup.clock.now += 15 * 60  # projects is due again, customers not yet
    locks.asked.clear()
    assert [r.source for r in sync.tick()] == ["projects"]
    assert locks.asked.count(service.APPLY_MARGIN + service.READ_MARGIN) == 1
    sync.close()


DIRECTORY = {"name": "directory", "kind": "ldap", "url": "ldaps://dc1.acme.lan", "base": "OU=Staff,DC=acme,DC=lan",
             "mapping": {"cn": "PERSON"}}  # fmt: skip


def test_an_instance_without_the_ldap_extra_leaves_an_ldap_sources_turn_to_one_that_has_it(setup):
    """The final review's M5: an instance whose install lacks Latet's `ldap` extra takes the lease all the same, and
    leaves an LDAP source's turn to an instance that reads it, so that a mixed fleet never flaps between a run and
    `not-installed`; once no instance has taken it for three ticks (none has the extra), it runs it, and the source
    fails `not-installed`, said where the source is shown."""
    setup.settings["now"] = sources(DIRECTORY)

    def people(source, secret):
        return [{"cn": "Grace Whitfield"}]

    def missing(source, secret):
        raise SourceError("not-installed", "LDAP needs Latet's `ldap` extra")

    store = setup.sync.store
    able = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock, readers={"ldap": people})
    without = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock, readers={"ldap": missing})
    without.installed = lambda kind: kind != "ldap"
    assert without.installed("file") and not without.installed("ldap")
    assert without.tick() == [] and store.runs() == {}  # never synced: left to an instance that reads it
    assert [r.outcome for r in able.tick()] == ["ok"]
    setup.clock.now += 3600  # due again: left again, and taken by the other
    assert without.tick() == [] and store.runs()["directory"].outcome == "ok"
    assert [r.outcome for r in able.tick()] == ["ok"]
    setup.clock.now += 3600  # due again, and the instance with the extra gone
    assert without.tick() == []
    setup.clock.now += 3 * service.TICK - 1
    assert without.tick() == []
    setup.clock.now += 1  # three ticks and none took it: no instance reads LDAP
    [run] = without.tick()
    assert (run.outcome, run.code) == ("failed", "not-installed") and store.runs()["directory"].code == "not-installed"
    setup.clock.now += 15 * 60  # tried again: an instance with the extra takes it first
    assert without.tick() == [] and [r.outcome for r in able.tick()] == ["ok"]
    able.close()
    without.close()


def test_a_tick_reads_no_further_source_once_the_license_pauses_term_sync(setup):
    """Minor 4: a pause during a tick stops it before the next source, not for the rest of the turn."""
    read = []

    def paused():
        setup.sync.licensing.producing_ = False
        return BOTH

    def projects(source, secret):
        read.append("projects")
        return [{"code_name": "Project Falcon"}]

    setup.settings["now"] = sources(CUSTOMERS, PROJECTS)
    setup.sync.readers["url"] = projects
    setup.records = paused
    assert [r.source for r in setup.sync.tick()] == ["customers"] and read == []


class _NoRecord(MemoryTermStore):
    """A database that takes a run's terms and then stops answering, before the run is recorded."""

    def record_run(self, run):
        raise ConnectionError("the database went away")


def test_a_run_whose_bookkeeping_fails_is_still_told_and_masked(setup, caplog):
    """Minor 5: the terms applied are told and used first; a failure to record the run is logged by its type alone."""
    store, published = _NoRecord(), Published()
    sync = make_sync(store, locks=setup.locks, settings=setup.settings, clock=setup.clock, published=published,
                     readers={"file": setup.reader})  # fmt: skip
    with caplog.at_level(logging.INFO):
        assert [r.outcome for r in sync.tick()] == ["ok"]
    assert published == [(CHANNEL, {"version": store.version()})]
    assert masked(sync, "Halden Insurance") == ["Halden Insurance"] and "ConnectionError" in caplog.text
    assert "went away" not in caplog.text
    sync.close()


def test_closed_in_the_middle_of_a_read_it_gives_its_lease_back_and_applies_nothing(setup, monkeypatch):
    """An instance that stops while a source is read (its read waited for CLOSE_WAIT at most) gives the lease back at
    once, so that another instance syncs at its next look rather than once the lease ran out (15 minutes); what the
    read gives then is never applied nor recorded, and a closed term sync never takes the lease again."""
    monkeypatch.setattr(service, "CLOSE_WAIT", 0.2)
    reading, go_on = threading.Event(), threading.Event()

    def stuck():
        reading.set()
        go_on.wait(10)
        return BOTH

    setup.records = stuck
    setup.sync.start()
    assert reading.wait(5)
    setup.sync.close()
    with setup.locks.lease(LEASE, LEASE_SECONDS) as other:
        assert other  # given back while the read still goes on
        go_on.set()
        assert setup.sync._thread is not None
        setup.sync._thread.join(5)
        assert not setup.sync._thread.is_alive()
    assert (
        setup.sync.store.kept() == 0 and setup.sync.store.runs() == {} and masked(setup.sync, "Halden Insurance") == []
    )
    taken = []
    real = setup.sync._lease
    monkeypatch.setattr(setup.sync, "_lease", lambda name, seconds: taken.append(name) or real(name, seconds))
    setup.clock.now += 3600
    assert setup.sync.tick() == [] and taken == []  # closed: no turn taken


# --- the final review's I1: the tag names the terms masked, never the store's version --------------------------------

HALDEN, KESTREL, MAPLE = ("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT"), ("Maple Freight", "CLIENT")


def test_the_tag_names_the_terms_masked_and_stays_while_they_do(store):
    """No term: no tag, so that a server that syncs nothing keeps 0.11's keys and stamps (A-M4). A run that only marks
    terms gone or back in their source, and a source taken out whose terms stay masked, move the store's version and
    leave the tag as it was: no WebSocket ends and no request is refused for nothing. A term added, removed or of
    another type changes it, and two instances that load the same terms at other versions of the store agree on it."""
    clock = Clock()
    locks = MemoryLocks(clock)
    sync = make_sync(store, locks=locks, clock=clock)
    sync.reload()
    assert (sync.live.tag, sync.live.count) == ("", 0)
    store.apply("customers", [HALDEN, KESTREL], now=1.0)
    sync.reload()
    first = sync.live.tag
    assert first and sync.live.count == 2
    for change in (
        lambda: store.apply("customers", [KESTREL], now=2.0),  # Halden gone from its source: still masked
        lambda: store.apply("customers", [HALDEN, KESTREL], now=3.0),  # back in it
        lambda: store.retire([], now=4.0),  # the source taken out: its terms gone, still masked
    ):
        version = store.version()
        change()
        assert store.version() != version
        assert sync.reload() is False and sync.live.tag == first and masked(sync, "Halden Insurance") == [HALDEN[0]]
    later = make_sync(store, locks=locks, clock=clock)  # an instance that loads them at this version first
    later.reload()
    assert later.live.tag == first
    store.apply("customers", [HALDEN, KESTREL, MAPLE], now=5.0)  # a new term
    assert sync.reload() is True and sync.live.tag not in ("", first)
    added = sync.live.tag
    store.apply("customers", [HALDEN, KESTREL, MAPLE, ("Halden Insurance", "PERSON")], now=6.0)  # of another type too
    sync.reload()
    assert sync.live.tag not in (first, added)
    store.apply("customers", [HALDEN, KESTREL, MAPLE], now=7.0)
    store.remove(source="customers")  # the one gone removed: no longer masked
    sync.reload()
    assert sync.live.tag == added
    store.retire([], now=8.0)
    store.remove()
    sync.reload()
    assert (sync.live.tag, sync.live.count) == ("", 0)  # every term removed: 0.11's keys again
    later.close()
    sync.close()


def test_without_a_license_for_term_sync_the_tag_is_empty_and_comes_back_with_the_terms(store):
    clock = Clock()
    sync = make_sync(store, locks=MemoryLocks(clock), clock=clock)
    store.apply("customers", [HALDEN], now=1.0)
    sync.reload()
    tag = sync.live.tag
    sync.licensing.licensed_ = False
    sync.reload()
    assert (sync.live.tag, sync.live.count) == ("", 0)
    sync.licensing.licensed_ = True
    sync.reload()
    assert sync.live.tag == tag and masked(sync, "Halden Insurance") == [HALDEN[0]]
    sync.close()
