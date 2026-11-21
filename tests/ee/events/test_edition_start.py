"""E4's threads start once a server's edition is whole (I7): never while its policy is being made, so that an instance
that starts again with a policy in force records no change of it; an edition that fails to start leaves none running;
and an edition made again on the same server binds the metrics already there (I10)."""

import json
import threading
import time

import pytest

from latet.edition import server_context
from latet.ee import edition as edition_module
from latet.ee.edition import EnterpriseEdition
from latet.ee.events.metrics import ServerMetrics

from ..licenses import Clock as Ticker
from ..people.server_support import BY_HAND, SERVER_TESTS
from .servers import quiet, ready

pytestmark = SERVER_TESTS
POLICY = "latet_rules: 1" + chr(10) + "profile: max" + chr(10)  # a policy as E2's tests write one


def _jobs_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == "latet-events-jobs" and thread.is_alive())


def _events_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == "latet-events" and thread.is_alive())


def test_e4s_threads_start_once_the_edition_is_whole_and_a_restart_records_no_policy_change(
    cluster, upstream, monkeypatch
):
    ready(cluster, upstream)
    quiet(cluster)  # the license's job is driven here alone, each look when the test says
    a, b = cluster.servers
    a.edition.events.watch.round()  # the first look: it only notes (no policy yet)
    policy = a.edition.policy
    floor = policy.read(POLICY)
    with a.db.transaction() as conn:
        policy.save(POLICY, floor, actor=BY_HAND, conn=conn)
    policy.committed()
    assert a.edition.events.watch.round()  # the policy in force noted, its `policy` event recorded (R9)
    assert a.edition.events.pipeline.flush()

    def policy_events() -> int:
        rows = a.edition.events.store.query(0, cluster.clock() + 3600, by=("type",))  # timed by the server's clock
        return sum(row["events"] for row in rows if row["type"] == "policy" and not row["category"])

    before = policy_events()
    assert before == 1  # one a change, whatever the number of instances
    real, seen = edition_module.make_policy, []

    def slow(server, **options):
        assert "record" not in options  # E2's policy made without a recorder (R9)
        seen.append(_jobs_threads())
        time.sleep(0.5)  # a policy made slowly: E4's jobs must not run meanwhile
        return real(server, **options)

    writers = _events_threads()
    b.edition.close()
    assert b.edition.events is None and _events_threads() == writers - 1  # closed first, its thread gone (R17)
    running = _jobs_threads()
    monkeypatch.setattr(edition_module, "make_policy", slow)
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))  # as E1's tests start one again
    assert seen == [running]  # its jobs not started while its policy was made (I7)
    assert _jobs_threads() == running + 1  # and started once it was whole
    b.edition.events.watch.round()
    assert b.edition.events.pipeline.flush() and policy_events() == before  # neither "removed" nor "set" again


def test_an_edition_that_fails_to_start_leaves_no_thread_of_e4_running(cluster, monkeypatch):
    b = cluster.servers[1]
    b.edition.close()
    jobs, writers = _jobs_threads(), _events_threads()

    def failing(server, **options):
        raise ConnectionError("the database at 10.0.0.5 does not answer")

    monkeypatch.setattr(edition_module, "make_policy", failing)
    made: list[EnterpriseEdition] = []
    real_e4 = EnterpriseEdition._e4_on_server

    def kept(self, server):
        made.append(self)
        real_e4(self, server)

    monkeypatch.setattr(EnterpriseEdition, "_e4_on_server", kept)
    with pytest.raises(ConnectionError):
        EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))
    [edition] = made
    assert edition.events is None  # closed by the block that closes the edition on a failure (I7)
    assert (_jobs_threads(), _events_threads()) == (jobs, writers)  # none started, none left behind


def test_an_edition_whose_e4_cannot_be_made_leaves_nothing_of_it_behind(cluster, monkeypatch):
    from latet.ee.events import api as api_module

    b = cluster.servers[1]
    b.edition.close()
    jobs, writers = _jobs_threads(), _events_threads()
    metrics = ServerMetrics.of(b.metrics)

    def refused(*args, **kwargs):
        raise TypeError("the reports' settings are a server's: kept in PostgreSQL")

    monkeypatch.setattr(api_module.EventsApi, "__init__", refused)
    with pytest.raises(TypeError):
        EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))
    assert (_jobs_threads(), _events_threads()) == (jobs, writers)  # none started, none left behind (I7)
    waiting = {"id": "0" * 32, "type": "exchange", "ts": "2027-03-01T00:00:00Z"}
    cluster.server.edition.events.store.put(["hook"], [waiting], at=cluster.clock())  # in the queue the server shares
    assert metrics._queued_now() == {}  # its metrics read the service that was not made no more


def test_an_edition_made_again_on_the_same_server_binds_the_metrics_already_there(cluster):
    b = cluster.servers[1]
    metrics = ServerMetrics.of(b.metrics)
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))  # registered once (I10)
    assert b.edition.events.metrics is metrics and ServerMetrics.of(b.metrics) is metrics
    assert "latet_events_kept_total" in b.metrics.render()


def test_a_delivery_constructor_failure_leaves_no_metrics_or_e4_worker_bound_to_the_edition(cluster, monkeypatch):
    from latet.ee.events import service as service_module

    b = cluster.servers[1]
    b.edition.close()
    jobs, writers = _jobs_threads(), _events_threads()
    metrics = ServerMetrics.of(b.metrics)
    failure = RuntimeError("the delivery could not be constructed")

    def refused(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service_module, "SiemDelivery", refused)
    with pytest.raises(RuntimeError) as raised:
        EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))
    assert raised.value is failure
    assert (_jobs_threads(), _events_threads()) == (jobs, writers)
    waiting = {"id": "0" * 32, "type": "exchange", "ts": "2027-03-01T00:00:00Z"}
    cluster.server.edition.events.store.put(["hook"], [waiting], at=cluster.clock())
    assert metrics._queued_now() == {}  # no incomplete service retained by the registry's callbacks


def test_a_listener_start_failure_closes_the_events_and_unbinds_their_metrics(cluster, monkeypatch):
    from latet.ee.listener import Listener

    b = cluster.servers[1]
    b.edition.close()
    jobs, writers = _jobs_threads(), _events_threads()
    metrics = ServerMetrics.of(b.metrics)
    made = []
    real_e4, real_start = EnterpriseEdition._e4_on_server, Listener.start
    failure = RuntimeError("the listener could not finish starting")

    def kept(self, server):
        real_e4(self, server)
        made.append((self, self.events))

    listeners = []

    def refused(self):
        listeners.append(self)
        real_start(self)  # exercise a real listener that needs stopping even though start raises
        raise failure

    monkeypatch.setattr(EnterpriseEdition, "_e4_on_server", kept)
    monkeypatch.setattr(Listener, "start", refused)
    try:
        with pytest.raises(RuntimeError) as raised:
            EnterpriseEdition(server_context(b), ticker=Ticker(0.0))
        assert raised.value is failure
        [edition, events] = made[0]
        waiting = {"id": "0" * 32, "type": "exchange", "ts": "2027-03-01T00:00:00Z"}
        cluster.server.edition.events.store.put(["hook"], [waiting], at=cluster.clock())
        assert metrics._queued_now() == {}
        assert edition.events is None and events.pipeline._closed and events.delivery._closed
        assert not listeners[0].alive()
        assert (_jobs_threads(), _events_threads()) == (jobs, writers)
    finally:
        for edition, _events in made:
            edition.close()  # also stop the intentionally leaked listener when the regression is red


def test_the_logs_forwarding_begins_once_the_license_lists_the_siem_and_goes_on_after(instances, licensed, monkeypatch):
    server = instances.open()
    events = server.edition.events
    events.jobs.close()  # its rounds driven here alone (its first ran without a license: nothing of it)
    forwarding = events.forward
    assert forwarding is not None

    def entries() -> list[str]:
        return [entry.actor for entry in server.audit.query(action="siem.forwarding")]

    rounds: list[int] = []
    monkeypatch.setattr(events.delivery, "round", lambda: rounds.append(1) or False)
    [deliver] = [run for _, run in events.jobs.jobs if run.__qualname__ == "SiemDelivery.round"]
    assert forwarding() is False and entries() == []  # no license: a server without the SIEM writes nothing of it
    assert deliver() is False and rounds == []  # nor asks its database for the SIEM's queues every few seconds
    server.edition.install_license(licensed.make(features=["roles", "reports"]), actor=BY_HAND)
    assert forwarding() is False and entries() == []  # reports alone: still nothing
    deliver()
    assert rounds == [1]  # events are kept: the delivery runs (and judges the outputs removed: R2-1)
    server.edition.install_license(licensed.make(), actor=BY_HAND)  # the SIEM listed
    forwarding()
    assert entries() == ["server"]  # where it begins, recorded once (Task 8's `noted`, by the server)

    def last_read() -> int:
        _, note = events.store.job("admin-to-siem")
        assert note is not None
        return json.loads(note)["last"]

    begun = last_read()
    server.edition.install_license(licensed.make(features=["roles", "reports"]), actor=BY_HAND)
    server.audit.record(BY_HAND, "person.added", "u_emilycarter", {})
    forwarding()  # begun: it goes on without the SIEM, noting what it does not send as one gap (R2-2)
    assert last_read() > begun and entries() == ["server"]
    _, note = events.store.job("admin-to-siem")
    assert note is not None and json.loads(note)["gap"]["count"] >= 2  # the license's entry and the person's


def test_without_a_listener_a_license_installed_here_is_weighed_by_e4_at_once(instances, licensed):
    server = instances.open()
    server.edition.close()
    server.edition = EnterpriseEdition(server_context(server), listen=False, ticker=Ticker(0.0))  # hears no message
    assert server.edition.keeps_events() is False  # no license
    server.edition.install_license(licensed.make(), actor=BY_HAND)
    assert server.edition.keeps_events() is True  # E1's refresh weighs E4's pipeline again, never a minute later
    assert server.edition.events.pipeline.siem_on() is True


def test_a_one_shot_commands_server_runs_no_job_of_e4_and_still_writes_its_events(instances):
    """`latet server init` opens its server as a one-shot command's and closes it: E4's jobs do not start (no round of
    each, no lock left to run out once the command ends), and its pipeline does, so that an event put meanwhile is
    written rather than counted lost."""
    from latet.server.app import open_server

    jobs, writers = _jobs_threads(), _events_threads()
    once = open_server(
        instances.settings, clock=instances.clock, schema=instances.schema, prefix=instances.prefix, one_shot=True
    )
    instances.opened.append(once)  # closed with the others
    events = once.edition.events
    assert events is not None and _jobs_threads() == jobs  # no job thread of its own
    assert _events_threads() == writers + 1  # its pipeline runs
    once.edition.close()
    assert (_jobs_threads(), _events_threads()) == (jobs, writers)
