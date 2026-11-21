"""The pipeline of an instance: an event kept at once and never waited for, with its kind and group, the person as the
company chose and an API key by its id, nothing without a license for it, the database's failures waited out with each
batch counted once, and the SIEM's queue filled only while the license lets events go there."""

import re
import threading
import time

import pytest

from latet.ee.events import pipeline as pipeline_module
from latet.ee.events.pipeline import EventPipeline, Pseudonyms, SettingsCache, SettingsUnread
from latet.ee.events.schema import NotAnEvent, checked
from latet.ee.events.store import MOST_PROJECTS, OTHER, MemoryEvents
from latet.ee.settings import EnterpriseSettings, ReportSettings, SiemOutput, SiemSettings
from latet.events import make_event

from .fakes import Clock, Features, RecordingMetrics

T0 = 1_790_000_000.0
DAY = 86_400.0
EMILY, JAMES, CI = "u_emilycarte", "u_jameswilso", "u_billingci2"
KEY = b"k" * 32


def _settings(people="group-only", siem=()):
    outputs = [SiemOutput(name=name, kind="syslog", url="tls://siem.acme.com:6514") for name in siem]
    return EnterpriseSettings(reports=ReportSettings(people=people), siem=SiemSettings(outputs=outputs))


def _pipeline(store=None, *, people="group-only", siem=(), license=None, **options):
    settings = _settings(people, siem)
    return EventPipeline(store or MemoryEvents(), settings=lambda: settings, licensing=license or Features("reports", "siem"),
                         pseudonyms=Pseudonyms(lambda: KEY), clock=lambda: T0, every=0.01, **options)  # fmt: skip


def _rows(pipeline, by):
    return pipeline.store.query(T0 - DAY, T0 + DAY, by=by)


def _event(type_="exchange", /, **fields):
    # The type positional alone: `kind=` is the event's field (a person or a key), never its type.
    return make_event(type_, clock=lambda: T0, **fields)


def test_an_event_is_kept_with_its_identitys_kind_and_first_group():
    metrics = RecordingMetrics()
    pipeline = _pipeline(people="named", group_of=lambda identity: "g_payments", kind_of=lambda identity: "person",
                         metrics=metrics)  # fmt: skip
    pipeline.put(_event(identity=EMILY, masked={"EMAIL": 1}))
    assert pipeline.flush()
    [row] = [r for r in _rows(pipeline, ("group", "kind", "identity")) if not r["category"]]
    assert (row["group"], row["kind"], row["identity"], row["events"]) == ("g_payments", "person", EMILY, 1)
    assert metrics.counts["kept", "exchange"] == 1
    pipeline.close()


@pytest.mark.parametrize(("people", "kept"), [("group-only", ""), ("pseudonymous", "p_"), ("named", EMILY)])
def test_a_person_is_kept_as_the_company_chose_and_an_api_key_by_its_id(people, kept):
    pipeline = _pipeline(people=people)
    pipeline.put(_event(identity=EMILY, kind="person"))
    pipeline.put(_event("approval", outcome="approved", by=EMILY))  # E5's decisions
    pipeline.put(_event(identity=CI, kind="key"))
    assert pipeline.flush()
    persons = {r["identity"] for r in _rows(pipeline, ("kind", "identity")) if r["kind"] == "person"}
    deciders = {r["decider"] for r in _rows(pipeline, ("type", "decider")) if r["type"] == "approval"}
    keys = {r["identity"] for r in _rows(pipeline, ("kind", "identity")) if r["kind"] == "key"}
    assert keys == {CI}
    [person], [decider] = persons, deciders
    for shown in (person, decider):
        assert shown.startswith(kept) if kept else shown == ""
    # One pseudonym for her requests, another for her decisions (M1 of the final security review); an id is an id.
    assert (person != decider) if people == "pseudonymous" else (person == decider)
    pipeline.close()


def test_a_person_whose_kind_is_unknown_is_kept_as_a_person():
    # Neither the event nor the server says what the identity is: it is kept as people are, never by its id.
    pipeline = _pipeline(kind_of=lambda identity: None)
    pipeline.put(_event(identity=EMILY))
    assert pipeline.flush()
    assert [r["identity"] for r in _rows(pipeline, ("identity",)) if not r["category"]] == [""]
    pipeline.close()


def test_a_pseudonym_given_is_a_persons_and_group_only_keeps_none():
    pipeline = _pipeline()
    pipeline.put(_event(kind="person") | {"identity": "p_abcdefghij", "by": "p_abcdefghij"})
    assert pipeline.flush()
    [row] = [r for r in _rows(pipeline, ("identity", "decider")) if not r["category"]]
    assert (row["identity"], row["decider"]) == ("", "")
    pipeline.close()


def test_a_person_has_the_same_pseudonym_on_every_instance():
    one, other = Pseudonyms(lambda: KEY), Pseudonyms(lambda: KEY)
    assert one.of(EMILY) == other.of(EMILY) != one.of(JAMES)
    assert re.fullmatch(r"p_[a-z2-7]{10}", one.of(EMILY))
    assert Pseudonyms(lambda: b"j" * 32).of(EMILY) != one.of(EMILY)  # another server's key, another pseudonym


def test_the_administration_logs_pseudonyms_are_of_a_domain_of_their_own():
    """M1 of the whole-branch review: the SIEM's `admin` events name a person by a pseudonym that is the same on every
    instance and in every such event, and never the one of their exchange events and report rows, which the Audit
    page's names would otherwise give away."""
    one, other = Pseudonyms(lambda: KEY), Pseudonyms(lambda: KEY)
    assert one.admin_of(EMILY) == other.admin_of(EMILY) != one.admin_of(JAMES)
    assert re.fullmatch(r"p_[a-z2-7]{10}", one.admin_of(EMILY))
    people = [f"u_person{n:05d}" for n in range(500)]
    assert not {one.admin_of(p) for p in people} & {one.of(p) for p in people}  # no admin one is an exchange one
    assert Pseudonyms(lambda: b"j" * 32).admin_of(EMILY) != one.admin_of(EMILY)


def test_the_events_that_are_not_requests_have_pseudonyms_of_a_domain_of_their_own():
    """M1 of the final security review: a sign-in, a decision, a license, a sync or a change of the locked rules is
    written in the same second as an entry of the administration log that names the person, so its pseudonym is never
    the one of that person's requests (their `exchange`, `stopped` and `apps` events and the reports' people), nor the
    `admin` events' one."""
    one, other = Pseudonyms(lambda: KEY), Pseudonyms(lambda: KEY)
    assert one.acts_of(EMILY) == other.acts_of(EMILY) != one.acts_of(JAMES)
    assert re.fullmatch(r"p_[a-z2-7]{10}", one.acts_of(EMILY))
    people = [f"u_person{n:05d}" for n in range(500)]
    acts = {one.acts_of(p) for p in people}
    assert not acts & {one.of(p) for p in people} and not acts & {one.admin_of(p) for p in people}
    assert Pseudonyms(lambda: b"j" * 32).acts_of(EMILY) != one.acts_of(EMILY)


def test_a_project_hidden_for_a_term_has_one_keyed_name_for_every_person_and_folder():
    """R3 of the final security review's re-review: a project whose name holds a term the space masks is named by
    `masked-` and 8 base32 characters of an HMAC of its name under the server's events key, with a label of its own:
    the same for everyone and every folder of that name, on every instance, never confirmed without the key, and none
    a pseudonym of a person."""
    one, other = Pseudonyms(lambda: KEY), Pseudonyms(lambda: KEY)
    hidden = one.project_of("falcon-acme-merger")
    assert re.fullmatch(r"masked-[a-z2-7]{8}", hidden) and hidden == other.project_of("falcon-acme-merger")
    assert hidden != one.project_of("falcon-acme-merger-2") and "falcon" not in hidden
    assert Pseudonyms(lambda: b"j" * 32).project_of("falcon-acme-merger") != hidden  # another server's key
    names = [f"project-{n:05d}" for n in range(500)]
    assert len({one.project_of(name) for name in names}) == 500


def test_the_enterprise_edition_hides_a_project_with_its_events_key_and_with_one_code_without_them():
    """R3: the edition names a hidden project with its pipeline's key; while E4 has no events (no license, or not a
    server's edition), with one fixed code, never the name."""
    from types import SimpleNamespace

    from latet.ee.edition import EnterpriseEdition
    from latet.events import HIDDEN_PROJECT

    pseudonyms = Pseudonyms(lambda: KEY)
    started = SimpleNamespace(events=SimpleNamespace(pipeline=SimpleNamespace(pseudonyms=pseudonyms)))
    assert EnterpriseEdition.hidden_project(started, "falcon-acme-merger") == pseudonyms.project_of(
        "falcon-acme-merger"
    )  # type: ignore[arg-type]
    assert EnterpriseEdition.hidden_project(SimpleNamespace(events=None), "falcon-acme-merger") == HIDDEN_PROJECT  # type: ignore[arg-type]
    assert HIDDEN_PROJECT == "masked"


def test_under_pseudonymous_a_decision_or_a_sign_in_never_carries_the_pseudonym_of_a_persons_requests():
    """M1 of the final security review: Bob decides Emily's change, Emily signs in, both make requests. The SIEM's
    `change` and `sign-in` events, and the reports' approvers, never carry the pseudonym their requests carry: the Audit
    page names who decided and on whose change, at that second, which would name every request of theirs."""
    bob = "u_bobsecurity"
    pipeline = _pipeline(people="pseudonymous", siem=("hook",))
    pipeline.put(_event("sign-in", identity=EMILY, kind="person", outcome="signed-in"))
    pipeline.put(_event("change", identity=EMILY, kind="person", outcome="approved", by=bob, reason="dashboard"))
    pipeline.put(_event("approval", identity=EMILY, kind="person", outcome="approved", by=bob))
    for identity in (EMILY, bob):
        pipeline.put(_event(identity=identity, kind="person", masked={"EMAIL": 1}))
    assert pipeline.flush()
    sent = [event for _, event in pipeline.store.peek("hook", 50)]
    requests = {e["identity"] for e in sent if e["type"] == "exchange"}
    acts = {e["identity"] for e in sent if e["type"] != "exchange"} | {e["by"] for e in sent if e["by"]}
    assert len(requests) == 2 and len(acts) == 2 and all(p.startswith("p_") for p in requests | acts)
    assert not requests & acts  # no decision nor sign-in names a person's requests
    persons = {r["identity"] for r in _rows(pipeline, ("type", "identity")) if r["type"] == "exchange"}
    deciders = {r["decider"] for r in _rows(pipeline, ("type", "decider")) if r["type"] in ("approval", "change")}
    assert persons == requests and deciders and not deciders & persons  # in the reports too
    pipeline.close()


def test_a_change_of_people_applies_from_then_on():
    current = [_settings("named")]
    pipeline = EventPipeline(MemoryEvents(), settings=SettingsCache(lambda: current[0], every=0.0),
                             licensing=Features("reports"), pseudonyms=Pseudonyms(lambda: KEY), clock=lambda: T0,
                             every=0.01)  # fmt: skip
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush()
    current[0] = _settings("group-only")
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush()
    assert sorted(r["identity"] for r in _rows(pipeline, ("identity",)) if not r["category"]) == ["", EMILY]
    pipeline.close()


def test_nothing_is_kept_without_a_license_for_reports_or_the_siem():
    pipeline = _pipeline(license=Features("locked-rules"))
    assert not pipeline.keeps()
    pipeline.put(_event())
    assert pipeline.flush() and _rows(pipeline, ("type",)) == []
    pipeline.close()


def test_an_event_put_while_nothing_is_kept_is_dropped_at_once_whatever_its_type():
    # What the edition's record does on the event loop: no event waits, of any type, while no license keeps them
    # (Task 2's carry: an exchange's event comes from the recorder's thread after `wanted` was asked).
    metrics = RecordingMetrics()
    pipeline = _pipeline(license=Features("locked-rules"), metrics=metrics, started=False)
    for kind, fields in [("exchange", {}), ("stopped", {"reason": "leak"}), ("policy", {"outcome": "refused"}),
                         ("apps", {"identity": CI, "kind": "key"}), ("license", {"outcome": "installed"})]:  # fmt: skip
        pipeline.put(_event(kind, **fields))
    assert pipeline.flush(timeout=0.2)  # nothing waits for the writer, which has not started
    assert (pipeline.lost, metrics.counts["lost"]) == (0, 0)
    pipeline.close()


def test_an_event_that_is_not_one_of_the_schema_is_refused_and_the_rest_kept():
    pipeline = _pipeline()
    pipeline.put(_event() | {"value": "emily.carter@acme.com"})
    pipeline.put(_event())
    assert pipeline.flush() and pipeline.refused == 1
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 1
    pipeline.close()


def test_what_the_server_fills_in_is_checked_again_and_a_bad_event_is_refused_alone():
    # A group the lookup gives that is no group's id: that event is refused, never the batch it came in.
    groups = {EMILY: "g_payments", JAMES: "Payments Team"}
    pipeline = _pipeline(people="named", group_of=groups.get)
    pipeline.put(_event(identity=JAMES, kind="person"))
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush() and pipeline.refused == 1
    assert [(r["identity"], r["group"]) for r in _rows(pipeline, ("identity", "group")) if not r["category"]] == [
        (EMILY, "g_payments")
    ]
    pipeline.close()


def test_a_batch_the_store_refuses_as_not_an_event_is_dropped_never_tried_again_and_the_thread_lives_on(caplog):
    class Refusing(MemoryEvents):
        def __init__(self):
            super().__init__()
            self.batches: list[str] = []

        def write(self, batch, events, outputs=(), *, at):
            self.batches.append(batch)
            if self.batches[0] == batch:  # the first batch, refused each time it is given
                raise NotAnEvent("type is one of the event types")
            return super().write(batch, events, outputs, at=at)

    store, metrics = Refusing(), RecordingMetrics()
    pipeline = _pipeline(store, metrics=metrics, retry=(0.01,), started=False)
    pipeline.put(_event())
    pipeline.put(_event())
    pipeline.start()  # both in its first batch
    assert pipeline.flush(timeout=5)
    assert len(store.batches) == 1 and (pipeline.refused, metrics.counts["lost"]) == (2, 2)
    assert "NotAnEvent" in caplog.text and "type is one of" not in caplog.text  # said by its type
    pipeline.put(_event())  # the thread is alive: a later batch is kept
    assert pipeline.flush(timeout=5)
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 1
    pipeline.close()


def test_an_event_of_the_administration_log_never_reaches_the_store():
    # The log's events go to the SIEM's queue with their entry's id (Task 8), never through the pipeline's write,
    # which would queue them with none and count them in the reports.
    pipeline = _pipeline(siem=("hook",))
    pipeline.put(_event("admin", outcome="person.added", reason="server"))
    pipeline.put(_event())
    assert pipeline.flush() and pipeline.refused == 1
    assert [event["type"] for _, event in pipeline.store.peek("hook", 10)] == ["exchange"]
    assert {r["type"] for r in _rows(pipeline, ("type",))} == {"exchange"}
    pipeline.close()


def test_put_never_waits_even_when_the_database_is_stuck(monkeypatch):
    monkeypatch.setattr(pipeline_module, "WAITING", 2)
    entered, released = threading.Event(), threading.Event()

    class Stuck(MemoryEvents):
        def write(self, *args, **kwargs):
            entered.set()
            released.wait(5)
            return super().write(*args, **kwargs)

    metrics = RecordingMetrics()
    pipeline = _pipeline(Stuck(), metrics=metrics)
    pipeline.put(_event())
    assert entered.wait(5)  # the writer waits on the database
    started = time.monotonic()
    for _ in range(10):
        pipeline.put(_event())
    assert time.monotonic() - started < 0.5 and pipeline.lost == 8 and metrics.counts["lost"] == 8
    released.set()
    assert pipeline.flush()
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 3
    pipeline.close()


def test_put_never_raises_when_the_metrics_fail(monkeypatch):
    monkeypatch.setattr(pipeline_module, "WAITING", 1)

    class Broken(RecordingMetrics):
        def lost(self, n):
            raise OSError("the registry failed")

    pipeline = _pipeline(metrics=Broken(), started=False)
    pipeline.put(_event())
    pipeline.put(_event())  # no room: lost, and the metric's failure stays the pipeline's
    assert pipeline.lost == 1
    pipeline.close()


def test_a_database_that_fails_for_a_while_counts_each_batch_once():
    class Flaky(MemoryEvents):
        def __init__(self):
            super().__init__()
            self.batches: list[str] = []

        def write(self, batch, events, outputs=(), *, at):
            self.batches.append(batch)
            if len(self.batches) <= 2:
                raise OSError("the database restarts")
            written = super().write(batch, events, outputs, at=at)
            if len(self.batches) == 3:  # it wrote, and the answer was lost on the way back
                raise OSError("the connection closed after the commit")
            return written

    store = Flaky()
    pipeline = _pipeline(store, retry=(0.01,))
    pipeline.put(_event(masked={"EMAIL": 1}))
    assert pipeline.flush(timeout=5)
    assert len(store.batches) == 4 and len(set(store.batches)) == 1  # the same batch each time
    [row] = [r for r in _rows(pipeline, ("type",)) if r["category"] == "EMAIL"]
    assert (row["events"], row["masked"], pipeline.lost) == (1, 1, 0)
    pipeline.close()


class _Counted(MemoryEvents):
    """The memory store, keeping the id of each batch it is given."""

    def __init__(self):
        super().__init__()
        self.batches: list[str] = []

    def write(self, batch, events, outputs=(), *, at):
        self.batches.append(batch)
        return super().write(batch, events, outputs, at=at)


def test_metrics_that_fail_after_a_batch_is_written_never_hold_it_nor_count_it_twice(caplog):
    class Broken(RecordingMetrics):
        def kept(self, counts):
            raise OSError("the registry failed")  # each time: a batch written is never tried again for it (R2-6)

    store = _Counted()
    pipeline = _pipeline(store, metrics=Broken(), retry=(0.01,), started=False)
    pipeline.put(_event() | {"value": "emily.carter@acme.com"})
    pipeline.put(_event(masked={"EMAIL": 1}))
    pipeline.start()  # both in its first batch
    assert pipeline.flush(timeout=5)
    assert len(store.batches) == 1 and pipeline.refused == 1  # written once, its refusal counted once
    pipeline.put(_event(masked={"EMAIL": 1}))  # the next event is counted too
    assert pipeline.flush(timeout=5)
    assert len(set(store.batches)) == len(store.batches) == 2
    [row] = [r for r in _rows(pipeline, ("type",)) if r["category"] == "EMAIL"]
    assert (row["events"], pipeline.refused, pipeline.lost) == (2, 1, 0)
    assert "OSError" in caplog.text and "the registry failed" not in caplog.text  # said by its type
    pipeline.close()


def test_events_refused_in_a_batch_held_are_counted_once():
    class Flaky(_Counted):
        def write(self, batch, events, outputs=(), *, at):
            if not self.batches:
                self.batches.append(batch)
                raise OSError("the database restarts")
            return super().write(batch, events, outputs, at=at)

    store = Flaky()
    pipeline = _pipeline(store, retry=(0.01,), started=False)
    pipeline.put(_event() | {"value": "emily.carter@acme.com"})
    pipeline.put(_event())
    pipeline.start()
    assert pipeline.flush(timeout=5)
    assert len(store.batches) == 2 and len(set(store.batches)) == 1 and pipeline.refused == 1
    pipeline.close()


def test_a_failure_while_a_batch_is_shaped_holds_it_and_the_thread_lives_on():
    calls: list[int] = []

    def key() -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the database restarts")  # what a failover gives, psycopg's OperationalError among it (I6)
        return KEY

    pipeline = _pipeline(people="pseudonymous", retry=(0.01,))
    pipeline.pseudonyms = Pseudonyms(key)
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush(timeout=5)
    pipeline.put(_event(identity=EMILY, kind="person"))  # the thread is alive: a later batch is kept too
    assert pipeline.flush(timeout=5)
    persons = [r for r in _rows(pipeline, ("identity",)) if not r["category"]]
    assert len(calls) == 2 and sum(r["events"] for r in persons) == 2 and pipeline.lost == 0
    pipeline.close()


@pytest.mark.parametrize("lookup", ["group_of", "kind_of"])
def test_a_lookup_that_fails_holds_the_batch_until_it_answers(lookup):
    # The server's answer is part of the event: it is waited for, never left out (I6).
    calls: list[int] = []
    answer = "g_payments" if lookup == "group_of" else "key"

    def find(identity):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the database restarts")
        return answer

    pipeline = _pipeline(people="named", retry=(0.01,), **{lookup: find})
    pipeline.put(_event(identity=CI))
    assert pipeline.flush(timeout=5)
    [row] = [r for r in _rows(pipeline, ("group", "kind")) if not r["category"]]
    assert row["group" if lookup == "group_of" else "kind"] == answer and len(calls) == 2 and pipeline.lost == 0
    pipeline.close()


def test_a_lookup_that_fails_answers_from_its_last_answer_when_there_is_one(monkeypatch):
    monkeypatch.setattr(pipeline_module, "CACHED", -1.0)  # every answer is out of date at once
    calls: list[int] = []

    def group_of(identity):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("the database restarts")  # each time after the first: never answered again
        return "g_payments"

    pipeline = _pipeline(people="named", group_of=group_of, retry=(0.01,))
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush(timeout=5)
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush(timeout=5)
    [row] = [r for r in _rows(pipeline, ("group",)) if not r["category"]]
    assert (row["group"], row["events"], len(calls)) == ("g_payments", 2, 2)
    pipeline.close()


def test_a_value_error_while_a_batch_is_shaped_holds_it_too():
    # Only the schema's refusal drops an event: any other error, a ValueError among them, holds the batch (I6).
    calls: list[int] = []

    def key() -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("the key could not be opened")
        return KEY

    pipeline = _pipeline(people="pseudonymous", retry=(0.01,))
    pipeline.pseudonyms = Pseudonyms(key)
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush(timeout=5)
    assert pipeline.refused == 0 and sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 1
    pipeline.close()


def test_a_batch_waits_for_settings_never_read_and_never_takes_their_defaults():
    answers: list = [OSError("the database restarts"), _settings("named", siem=("hook",))]

    def load():
        found = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(found, Exception):
            raise found
        return found

    pipeline = EventPipeline(MemoryEvents(), settings=SettingsCache(load, every=0.0), licensing=Features("reports", "siem"),
                             pseudonyms=Pseudonyms(lambda: KEY), clock=lambda: T0, every=0.01, retry=(0.01,))  # fmt: skip
    pipeline.put(_event(identity=EMILY, kind="person"))
    assert pipeline.flush(timeout=5)
    # Named and sent to the SIEM as the settings say, never kept by group only with no output (their defaults: M11).
    assert [r["identity"] for r in _rows(pipeline, ("identity",)) if not r["category"]] == [EMILY]
    assert len(pipeline.store.peek("hook", 10)) == 1
    pipeline.close()


def test_settings_that_cannot_be_read_again_keep_the_last_good_ones():
    answers: list = [_settings("named"), OSError("the database restarts")]

    def load():
        found = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(found, Exception):
            raise found
        return found

    cache = SettingsCache(load, every=0.0)
    assert cache().reports.people == "named"
    assert cache().reports.people == "named"  # the read failed: the last good ones, never the defaults


def test_the_last_good_settings_answer_five_minutes_at_most():
    answers: list = [_settings("named"), OSError("the database restarts")]

    def load():
        found = answers[0]
        if isinstance(found, Exception):
            raise found
        return found

    clock = Clock(1000.0)
    cache = SettingsCache(load, every=5.0, clock=clock)
    assert cache().reports.people == "named"
    answers.pop(0)
    clock.now += 300.0
    assert cache().reports.people == "named"  # unread for five minutes: the last good ones still
    clock.now += 1.0
    with pytest.raises(SettingsUnread):
        cache()  # older than that: a batch waits for them (M11), as for settings never read
    answers[0] = _settings("pseudonymous")
    assert cache().reports.people == "pseudonymous"


def test_the_applications_api_takes_half_the_room_at_most(monkeypatch):
    monkeypatch.setattr(pipeline_module, "WAITING", 10)
    pipeline = _pipeline(started=False)  # nothing writes yet: what waits stays (I7's start, used here)
    for _ in range(10):
        pipeline.put(_event("apps", identity=CI, kind="key", agent="apps", outcome="mask"))
    assert pipeline.lost == 5  # past half the room, the applications API's event is the one lost (I13)
    for _ in range(5):
        pipeline.put(_event("stopped", stopped=1, reason="leak"))
    assert pipeline.lost == 5  # a person's stopped request still finds room
    pipeline.put(_event("stopped", stopped=1, reason="leak"))
    assert pipeline.lost == 6  # the room is full now: whatever comes is lost, and counted
    pipeline.start()
    assert pipeline.flush()
    counted = {r["type"]: r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]}
    assert counted == {"apps": 5, "stopped": 5}
    pipeline.close()


def test_a_client_naming_a_new_project_at_each_request_adds_fifty_an_hour_at_most():
    metrics = RecordingMetrics()
    pipeline = _pipeline(metrics=metrics)
    for n in range(60):
        pipeline.put(_event(identity=EMILY, kind="person", project=f"p{n}"))
    assert pipeline.flush()
    projects = {r["project"] for r in _rows(pipeline, ("project",)) if not r["category"]}
    assert len(projects) == 51 and "other" in projects  # M6: each name a row a day for the retention, else
    assert metrics.counts["projects_full"] == 10  # the bucket full: counted once each, in the server's metrics
    assert pipeline_module.MOST_PROJECTS is MOST_PROJECTS  # the store's bound, never a second one
    pipeline.close()


def test_a_batch_tried_again_counts_its_full_projects_bucket_once():
    class Failing(MemoryEvents):
        tries = 0

        def write(self, batch, events, outputs=(), *, at):
            self.tries += 1
            if self.tries < 3:  # the database does not answer twice: the batch is shaped again at each try
                raise OSError("the database at 10.0.0.5 does not answer")
            return super().write(batch, events, outputs, at=at)

    metrics, store = RecordingMetrics(), Failing()
    pipeline = _pipeline(store, metrics=metrics, retry=(0.01,), started=False)
    for n in range(60):
        pipeline.put(_event(identity=EMILY, kind="person", project=f"p{n}"))
    pipeline.start()  # one batch, tried three times
    assert pipeline.flush(timeout=5) and store.tries == 3
    assert metrics.counts["projects_full"] == 10  # counted once it was written, never once a try
    pipeline.close()


def test_each_persons_projects_are_bounded_before_the_person_is_stripped():
    # With group-only the rows keep no person: each person's own fifty an hour are weighed before, so that two people
    # naming thirty projects each keep all sixty.
    pipeline = _pipeline()
    for n in range(30):
        pipeline.put(_event(identity=EMILY, kind="person", project=f"emily-{n}"))
        pipeline.put(_event(identity=JAMES, kind="person", project=f"james-{n}"))
    assert pipeline.flush()
    projects = {r["project"] for r in _rows(pipeline, ("project",)) if not r["category"]}
    assert len(projects) == 60 and OTHER not in projects
    pipeline.close()


def test_a_project_reaches_the_siem_as_json_whatever_it_holds():
    project = 'billing "api"]=|x'
    pipeline = _pipeline(siem=("hook",))
    pipeline.put(_event(identity=EMILY, kind="person", project=project))
    assert pipeline.flush()
    [(_, event)] = pipeline.store.peek("hook", 10)
    assert event["project"] == project
    pipeline.close()


def test_events_wait_for_the_siem_only_while_the_license_lets_them_go_there():
    woken: list[int] = []
    pipeline = _pipeline(siem=("hook",), wake=lambda: woken.append(1))
    pipeline.put(_event())
    assert pipeline.flush() and len(pipeline.store.peek("hook", 10)) == 1 and woken
    paused = _pipeline(siem=("hook",), license=Features("reports", "siem", paused=True))
    paused.put(_event())
    assert paused.flush() and paused.store.peek("hook", 10) == []
    assert sum(r["events"] for r in _rows(paused, ("type",)) if not r["category"]) == 1  # counted all the same
    pipeline.close()
    paused.close()


def test_a_batch_held_across_a_pause_never_reaches_the_siem():
    # The license is weighed again before each try that follows a failure: a pause that began meanwhile keeps the
    # batch from the SIEM's queue, and it is counted all the same (R2).
    license = Features("reports", "siem")

    class Flaky(_Counted):
        def write(self, batch, events, outputs=(), *, at):
            if not self.batches:
                self.batches.append(batch)
                license.paused = True  # the license pauses while the database does not answer
                raise OSError("the database restarts")
            return super().write(batch, events, outputs, at=at)

    store = Flaky()
    pipeline = _pipeline(store, siem=("hook",), license=license, retry=(0.01,))
    pipeline.put(_event())
    assert pipeline.flush(timeout=5)
    assert len(store.batches) == 2 and store.peek("hook", 10) == []
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 1
    pipeline.close()


def test_an_event_put_during_a_pause_never_reaches_the_siem_after_it():
    license = Features("reports", "siem", paused=True)
    pipeline = _pipeline(siem=("hook",), license=license, started=False)
    during = _event()
    pipeline.put(during)  # what happens while nothing is produced is never sent afterwards (R2)
    license.paused = False
    pipeline.look()
    after = _event()
    pipeline.put(after)
    pipeline.start()  # the two in batches of their own: each batch keeps one id and one answer
    assert pipeline.flush(timeout=5)
    assert [event["id"] for _, event in pipeline.store.peek("hook", 10)] == [after["id"]]
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 2
    pipeline.close()


@pytest.mark.parametrize("started", [True, False])
def test_an_event_put_once_the_pipeline_is_closed_is_lost_and_counted(started):
    metrics = RecordingMetrics()
    pipeline = _pipeline(metrics=metrics, started=started)
    pipeline.close()
    pipeline.put(_event())
    assert (pipeline.lost, metrics.counts["lost"]) == (1, 1) and pipeline.flush(timeout=0.2)


def test_events_waiting_in_a_pipeline_never_started_are_lost_and_counted_when_it_closes():
    metrics = RecordingMetrics()
    pipeline = _pipeline(metrics=metrics, started=False)
    pipeline.put(_event())
    pipeline.put(_event("apps", identity=CI, kind="key", agent="apps", outcome="scan"))
    pipeline.close()
    assert (pipeline.lost, metrics.counts["lost"]) == (2, 2) and pipeline.flush(timeout=0.2)


def test_an_event_put_as_the_thread_ends_is_lost_and_counted_when_the_pipeline_closes():
    metrics = RecordingMetrics()
    pipeline = _pipeline(metrics=metrics)
    pipeline._stop.set()  # the thread ends, as close() makes it, before close() goes on
    pipeline._thread.join(5)
    pipeline.put(_event())  # put meanwhile: nothing will write it
    pipeline.close()
    assert (pipeline.lost, metrics.counts["lost"]) == (1, 1) and pipeline.flush(timeout=0.2)


def test_the_license_is_not_weighed_again_while_the_instance_stops():
    # Each closing try would add a connection's timeout while PostgreSQL does not answer: the last answer stays.
    tried, stopping = threading.Event(), threading.Event()

    class Counting(Features):
        weighed_while_stopping = 0

        def licensed(self, feature):
            if stopping.is_set():
                self.weighed_while_stopping += 1
            return super().licensed(feature)

        def producing(self, feature):
            if stopping.is_set():
                self.weighed_while_stopping += 1
            return super().producing(feature)

    class Down(_Counted):
        def write(self, batch, events, outputs=(), *, at):
            self.batches.append(batch)
            tried.set()
            raise OSError("the database does not answer")

    store, license = Down(), Counting("reports", "siem")
    pipeline = _pipeline(store, license=license, retry=(10.0,))
    pipeline.put(_event())
    assert tried.wait(5)  # failed once: the thread waits ten seconds for its next try
    stopping.set()
    pipeline.close()  # wakes it: its closing tries, with the license as it was last weighed
    assert len(store.batches) == 3 and pipeline.lost == 1 and license.weighed_while_stopping == 0


def test_batches_split_by_their_stamps_and_failing_each_twice_are_each_counted_once():
    # The review's probe P1: stamps off, on, on, off; each batch's write fails once before its commit and once after.
    class Twice(_Counted):
        def write(self, batch, events, outputs=(), *, at):
            self.batches.append(batch)
            tries = self.batches.count(batch)
            if tries == 1:
                raise OSError("the database restarts")
            written = super(_Counted, self).write(batch, events, outputs, at=at)
            if tries == 2:
                raise OSError("the connection closed after the commit")
            return written

    license = Features("reports", "siem", paused=True)
    store = Twice()
    pipeline = _pipeline(store, siem=("hook",), license=license, retry=(0.01,), started=False)
    events = [_event(identity=EMILY, kind="person") for _ in range(4)]
    pipeline.put(events[0])  # paused: off
    license.paused = False
    pipeline.look()
    pipeline.put(events[1])  # on
    pipeline.put(events[2])  # on
    license.paused = True
    pipeline.look()
    pipeline.put(events[3])  # off
    license.paused = False  # producing again by the time each batch is written
    pipeline.look()
    pipeline.start()
    assert pipeline.flush(timeout=10)
    runs = [batch for n, batch in enumerate(store.batches) if n == 0 or store.batches[n - 1] != batch]
    assert len(runs) == len(set(runs)) == 3 and len(store.batches) == 9  # each id's writes one after the other
    assert sum(r["events"] for r in _rows(pipeline, ("type",)) if not r["category"]) == 4 and pipeline.lost == 0
    assert [event["id"] for _, event in store.peek("hook", 10)] == [events[1]["id"], events[2]["id"]]
    pipeline.close()


def test_a_field_the_schema_does_not_have_is_counted_never_named(caplog):
    event = _event() | {"emily.carter@acme.com": 1, "value": "x"}
    with pytest.raises(NotAnEvent) as refused:
        checked(event)
    assert "acme" not in str(refused.value) and "value" not in str(refused.value) and "2" in str(refused.value)
    pipeline = _pipeline()
    pipeline.put(event)
    assert pipeline.flush() and pipeline.refused == 1
    assert "fields the schema does not have" in caplog.text and "acme" not in caplog.text
    pipeline.close()


def test_a_wake_that_fails_is_said_and_never_tried_again():
    calls: list[int] = []

    def wake():
        calls.append(1)
        raise OSError("the jobs' thread is gone")

    store = MemoryEvents()
    pipeline = _pipeline(store, siem=("hook",), wake=wake, retry=(0.01,))
    pipeline.put(_event())
    assert pipeline.flush(timeout=5)
    assert len(calls) == 1 and len(store.peek("hook", 10)) == 1
    pipeline.close()
