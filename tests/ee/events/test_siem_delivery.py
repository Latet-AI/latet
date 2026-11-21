"""Each SIEM output's queue reaches its receiver in order, after a pause in the network too; a batch refused is sent
again in halves until the event refused is alone, dropped and counted once the receiver took another, but an entry of
the administration log, which is never dropped, and a batch it refuses whole is a failure tried again later; past its bound the log's oldest entries collapse into one recorded gap marker, once an outage; nothing
goes while the license pauses the SIEM; an output removed sends nothing more and takes its queue along, the log's
entries it held counted apart (R2-1); one output's fault stops none of the others; a send that outlives its lock takes
nothing off the queue; and with two instances each event goes once and in order, even when one dies holding the
lock."""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from latet.ee.events import delivery as delivery_module
from latet.ee.events.delivery import RETRY_DELAYS, TRIM_EVERY, SiemDelivery
from latet.ee.events.locks import MemoryLocks
from latet.ee.events.pipeline import SettingsUnread
from latet.ee.events.siem import SecretElsewhere, SendError, make_output
from latet.ee.events.store import MemoryEvents, QueueState
from latet.ee.settings import SiemOutput

from .fakes import Clock, RecordingMetrics
from .receivers import HttpsReceiver, SyslogReceiver, certificate, trusting, until

T0 = 1_790_000_000.0
TOKEN = "5f0c" + "9a2e-1b7d-4c3f-8e6a-2d9b0c1e7f45"
NAMES = ["hook", "lines", "splunk", "syslog"]
HOOK = SiemOutput(name="hook", kind="webhook", url="https://siem.acme.com/in")
ORIGIN = "https://latet.acme.lan"


def _events(n, start=0):
    return [
        {"id": f"{i:032x}", "type": "exchange", "ts": "2026-09-21T14:13:20Z", "n": i} for i in range(start, start + n)
    ]


def _admin(i):
    return {"id": f"{i:032x}", "type": "admin", "ts": "2026-09-21T14:13:20Z", "n": i}


class Recording:
    """An output that keeps the ids it is sent: it refuses for good a batch holding one of ``refused``, raises ``fault``
    when set, and runs ``on_send`` first; ``calls`` counts its sends and ``closed`` says whether it was closed."""

    def __init__(self, name, *, refused=(), fault=None, on_send=None):
        self.name, self.refused, self.fault, self.on_send = name, set(refused), fault, on_send
        self.sent: list[list[str]] = []
        self.calls, self.closed = 0, False

    def send(self, events):
        self.calls += 1
        if self.on_send is not None:
            self.on_send()
        if self.fault is not None:
            raise self.fault
        ids = [event["id"] for event in events]
        if self.refused & set(ids):
            raise SendError("it answered 400", retry=False)
        self.sent.append(ids)

    def close(self):
        self.closed = True


def _faked(monkeypatch, *outputs):
    """The delivery's outputs are these, by their names."""
    by_name = {output.name: output for output in outputs}
    monkeypatch.setattr(
        delivery_module, "make_output", lambda settings, secret, context, host=None: by_name[settings.name]
    )


@pytest.fixture
def receivers(tmp_path):
    cert, key = certificate(tmp_path)
    made = {
        "json": HttpsReceiver(cert, key).start(),
        "ndjson": HttpsReceiver(cert, key).start(),
        "splunk": HttpsReceiver(cert, key).start(),
        "syslog": SyslogReceiver(cert, key).start(),
    }
    yield made, trusting(cert)
    for receiver in made.values():
        receiver.stop()


def _outputs(made) -> list[SiemOutput]:
    return [
        SiemOutput(name="hook", kind="webhook", url=made["json"].url),
        SiemOutput(name="lines", kind="webhook", url=made["ndjson"].url, format="ndjson"),
        SiemOutput(name="splunk", kind="splunk", url=made["splunk"].url, secret="siem-splunk"),
        SiemOutput(name="syslog", kind="syslog", url=made["syslog"].url),
    ]


def _received(made) -> dict[str, list[str]]:
    """The ids each receiver took, in order."""
    return {
        "json": [e["id"] for e in made["json"].events("json")],
        "ndjson": [e["id"] for e in made["ndjson"].events("ndjson")],
        "splunk": [e["event"]["id"] for e in made["splunk"].events("splunk")],
        "syslog": [json.loads(m[m.index("{") :])["id"] for m in made["syslog"].messages],
    }


def _secret(output: SiemOutput) -> str | None:
    return {"siem-splunk": TOKEN}.get(output.secret or "")


def _delivery(queue, outputs, context, clock, lease=None, **options) -> SiemDelivery:
    return SiemDelivery(queue, lambda: outputs, _secret, lease or MemoryLocks(clock=clock).lease, context=context,
                        host="latet.acme.lan", clock=clock, delays=(5.0,), **options)  # fmt: skip


def _deliver_all(*deliveries, rounds=20) -> None:
    for _ in range(rounds):
        if not any([delivery.round() for delivery in deliveries]):  # a list: each one takes its turn
            return


def test_each_receiver_gets_every_event_in_order_after_a_pause_in_the_network_too(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    delivery = _delivery(queue, _outputs(made), context, clock)
    first, second = _events(5), _events(7, start=5)
    queue.put(NAMES, first, at=T0)
    _deliver_all(delivery)
    assert until(lambda: all(len(ids) == 5 for ids in _received(made).values()))
    for receiver in made.values():
        receiver.stop()  # the network goes away
    queue.put(NAMES, second, at=T0)
    _deliver_all(delivery)
    states = delivery.state()
    assert all(state.failing_since == T0 and state.queued == 7 for state in states.values()), states
    for receiver in made.values():
        receiver.start()  # and comes back, at the same address
    _deliver_all(delivery)
    assert all(len(ids) == 5 for ids in _received(made).values())  # not before the time to try again
    clock.now += 10
    _deliver_all(delivery)
    expected = [e["id"] for e in first + second]
    assert until(lambda: all(ids == expected for ids in _received(made).values())), _received(made)
    assert all(s.queued == 0 and s.failing_since is None for s in delivery.state().values())
    delivery.close()


def test_a_receiver_that_refuses_every_event_is_tried_again_later_and_loses_nothing(receivers):
    made, context = receivers
    queue, clock, metrics = MemoryEvents(), Clock(T0), RecordingMetrics()
    hook = SiemOutput(name="hook", kind="webhook", url=made["json"].url)
    delivery = _delivery(queue, [hook], context, clock, metrics=metrics)
    made["json"].status = 400  # set to take something else: it refuses each event, alone too
    queue.put(["hook"], _events(2), at=T0)
    _deliver_all(delivery)
    state = delivery.state()["hook"]
    assert state.queued == 2 and state.dropped == 0 and "it answered 400" in (state.last_error or ""), state
    assert len(made["json"].paths) == 3 and queue.schedule("hook") == (T0 + 5, 1)  # the batch, each alone: one try
    _deliver_all(delivery)
    assert len(made["json"].paths) == 3  # and no other before its time
    clock.now += 5
    _deliver_all(delivery)
    assert len(made["json"].paths) == 6 and queue.schedule("hook") == (T0 + 10, 2)
    made["json"].status = 200  # set right
    clock.now += 5
    _deliver_all(delivery)
    assert [e["id"] for e in made["json"].events()] == [e["id"] for e in _events(2)]  # nothing lost
    assert delivery.state()["hook"].last_error is None and metrics.counts["dropped", "hook"] == 0
    assert metrics.counts["sent", "hook"] == 2 and metrics.labels == {"hook"}  # an output's name, never a value
    delivery.close()


def test_a_mixed_batch_drops_only_what_is_refused_alone_once_the_receiver_took_another(monkeypatch, caplog):
    queue, clock, metrics = MemoryEvents(), Clock(T0), RecordingMetrics()
    events, later = _events(3), _events(2, start=3)
    fake = Recording("hook", refused={events[0]["id"]})  # the first refused, the others taken
    _faked(monkeypatch, fake)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock, metrics=metrics)
    with caplog.at_level(logging.WARNING, logger="latet.ee.events.delivery"):
        _deliver_all(delivery)
        fake.refused = {later[0]["id"]}
        queue.put(["hook"], later, at=T0)
        _deliver_all(delivery)
    assert fake.sent == [[events[1]["id"], events[2]["id"]], [later[1]["id"]]]  # in order, each refused one dropped
    state = queue.state("hook")
    assert state.dropped == 2 and state.queued == 0 and metrics.counts["dropped", "hook"] == 2
    assert caplog.text.count("each alone") == 1  # said once a minute, never a line a refused event
    delivery.close()


def test_a_refused_event_is_dropped_when_a_later_part_fails_once_the_receiver_took_another(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    events = _events(4)
    fake = Recording("hook", refused={events[0]["id"]})

    def on_send():
        if fake.calls == 5:  # the batch, its first half, the first event, the second: then the second half fails
            fake.fault = SendError("it could not be reached")

    fake.on_send = on_send
    _faked(monkeypatch, fake)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    assert delivery.round() is False and fake.sent == [[events[1]["id"]]]
    state = queue.state("hook")
    assert state.dropped == 1 and state.queued == 2 and queue.schedule("hook") == (T0 + 5, 1)  # the rest tried later
    assert [event["id"] for _, event in queue.peek("hook", 5)] == [events[2]["id"], events[3]["id"]]
    delivery.close()


@pytest.mark.parametrize("taken_first", [False, True])
def test_an_entry_refused_after_another_refused_event_never_holds_the_queue_for_good(monkeypatch, taken_first):
    """The receiver refuses an `apps` event and an entry of the administration log, each alone. When it took nothing
    before them, the head is the `apps` event, so the queue is held by a run of refused events, not blocked at the
    entry; when it took one before them, the `apps` event is dropped and the entry is the head, blocked. Either way the
    administrator's skip frees it, the entry going behind a marker of its own, never dropped (fix round 2)."""
    queue, clock = MemoryEvents(), Clock(T0)
    taken, x, a, y = _events(1)[0], {"id": "1" * 32, "type": "apps", "n": 1}, _admin(2), _events(1, start=3)[0]
    batch = [taken, x, a, y] if taken_first else [x, a, y]
    fake = Recording("hook", refused={x["id"], a["id"]})
    _faked(monkeypatch, fake)
    queue.put(["hook"], batch, at=T0, refs=[2 if event is a else None for event in batch])
    delivery = _delivery(queue, [HOOK], None, clock)
    _deliver_all(delivery)
    entry = next(seq for seq, event in queue.peek("hook", 10) if event["id"] == a["id"])
    state = queue.state("hook")
    if taken_first:
        assert state.blocked and state.dropped == 1 and state.queued == 2 and state.held_to == entry
    else:
        assert not state.blocked and state.dropped == 0 and state.queued == 3 and state.held_to == entry
        assert "took none" in (state.last_error or "")
    assert queue.skip("hook", origin=ORIGIN, at=T0) == (2, 2, 1, 0 if taken_first else 1)  # never a dead end
    clock.now += 10
    _deliver_all(delivery)
    marker = uuid.uuid5(uuid.NAMESPACE_URL, f"{ORIGIN}#gap:2-2").hex
    assert fake.sent[-1] == [marker, y["id"]] and queue.state("hook").queued == 0
    delivery.close()


def test_a_run_of_refused_events_the_administrator_skips_lets_the_rest_go(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    refused, good = [{"id": f"{i:032x}", "type": "apps", "n": i} for i in range(500)], _events(3, start=500)
    fake = Recording("hook", refused={event["id"] for event in refused})
    _faked(monkeypatch, fake)
    queue.put(["hook"], refused + good, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    _deliver_all(delivery)
    state = queue.state("hook")
    assert state.queued == 503 and state.dropped == 0 and not state.blocked and fake.sent == []
    assert fake.calls == 999  # the batch, then each half down to each event alone: one try
    assert queue.skip("hook", origin=ORIGIN, at=T0) == (None, None, 0, 500)  # the run dropped and counted
    clock.now += 10
    _deliver_all(delivery)
    assert fake.sent == [[event["id"] for event in good]] and queue.state("hook").dropped == 500
    delivery.close()


def test_a_refused_event_is_not_dropped_once_the_lease_ran_out(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    events, sends = _events(2), []

    def on_send():
        sends.append(1)
        if len(sends) == 3:  # the lease runs out while the refused event is sent alone
            clock.now += 61

    fake = Recording("hook", refused={events[1]["id"]}, on_send=on_send)
    _faked(monkeypatch, fake)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    assert delivery.round() is False and fake.sent == [[events[0]["id"]]]
    state = queue.state("hook")
    assert state.queued == 1 and state.dropped == 0 and state.last_error is None  # the next holder's to judge (C1)
    delivery.close()


def test_a_refused_batch_is_halved_until_the_refused_event_is_alone_and_the_logs_entry_is_never_dropped(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    events = _events(8)
    events[2] = _admin(2)
    fake = Recording("hook", refused={events[5]["id"]})
    _faked(monkeypatch, fake)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    _deliver_all(delivery)
    assert [i for ids in fake.sent for i in ids] == [e["id"] for n, e in enumerate(events) if n != 5]  # in order (I5)
    assert queue.state("hook").dropped == 1
    entry, after = _admin(20), _events(1, start=21)[0]
    fake.refused = {entry["id"]}  # now it refuses an entry of the administration log
    queue.put(["hook"], [entry, after], at=T0)
    _deliver_all(delivery)
    state = queue.state("hook")
    assert state.blocked and state.queued == 2 and state.dropped == 1  # held at it: nothing after it goes
    assert "administration log" in (state.last_error or "")
    fake.refused = set()
    clock.now += 10
    _deliver_all(delivery)
    assert fake.sent[-1] == [entry["id"], after["id"]] and not queue.state("hook").blocked
    delivery.close()


def test_nothing_is_sent_while_the_license_pauses_the_siem_and_what_waits_goes_out_after(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    paused = [True]
    hook = SiemOutput(name="hook", kind="webhook", url=made["json"].url)
    delivery = _delivery(queue, [hook], context, clock, producing=lambda: not paused[0])
    events = _events(3)
    queue.put(["hook"], events, at=T0)
    _deliver_all(delivery)
    assert made["json"].received == [] and delivery.state()["hook"].queued == 3
    paused[0] = False
    _deliver_all(delivery)
    assert [e["id"] for e in made["json"].events()] == [e["id"] for e in events]
    delivery.close()


def test_an_output_removed_takes_what_waits_for_it_along(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    outputs = [SiemOutput(name="old", kind="webhook", url="https://127.0.0.1:1/in"),
               SiemOutput(name="hook", kind="webhook", url=made["json"].url)]  # fmt: skip
    delivery = _delivery(queue, outputs, context, clock)
    queue.put(["old", "hook"], _events(1), at=T0)
    _deliver_all(delivery)
    assert delivery.state()["old"].failing_since == T0
    outputs.pop(0)
    _deliver_all(delivery)
    assert queue.state("old").queued == 0 and set(delivery.state()) == {"hook"}
    delivery.close()


def test_an_output_removed_sends_nothing_once_an_instance_sees_it_removed(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    old = Recording("old", fault=SendError("it could not be reached"))
    _faked(monkeypatch, old)
    outputs = [SiemOutput(name="old", kind="webhook", url="https://siem.acme.com/in")]
    queue.put(["old"], [_admin(1), *_events(2, start=5)], at=T0)
    delivery = _delivery(queue, outputs, None, clock)
    _deliver_all(delivery)  # down: everything waits
    outputs.clear()  # removed, say because it points at a wrong host (R2-1)
    old.fault = None  # it would take them now
    clock.now += 10
    _deliver_all(delivery)
    assert old.sent == [] and queue.state("old").queued == 0  # nothing more goes there, its object (and secret) dropped
    assert old.closed  # closed as the removal is seen, the secret it held with it
    assert queue.admin_dropped() == 1  # the log's entry it held, counted apart and said by the status
    delivery.close()


def test_past_its_bound_the_logs_entries_collapse_into_one_marker_and_the_collapse_is_recorded(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    down = Recording("hook", fault=SendError("it could not be reached"))
    _faked(monkeypatch, down)
    noted: list[tuple[str, dict]] = []
    queue.put(["hook"], [_admin(i) for i in range(1, 6)], at=T0, refs=list(range(1, 6)))
    delivery = _delivery(queue, [HOOK], None, clock, admin_kept=3, slack=0, origin="https://latet.acme.lan",
                         noted=lambda conn, action, details: noted.append((action, dict(details))))  # fmt: skip
    _deliver_all(delivery)
    assert noted == [("siem.gap", {"output": "hook", "from": 1, "to": 3, "count": 3})]  # its entry, with it (R3-1)
    assert queue.state("hook").collapsed == 3 and queue.admin_waiting("hook") == 3
    down.fault = None
    clock.now += 10
    _deliver_all(delivery)
    marker = uuid.uuid5(uuid.NAMESPACE_URL, "https://latet.acme.lan#gap:1-3").hex
    assert [i for ids in down.sent for i in ids] == [marker, _admin(4)["id"], _admin(5)["id"]]  # the head kept first
    delivery.close()


def test_the_bound_collapses_once_down_to_its_slack_and_not_again_until_past_it(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    down = Recording("hook", fault=SendError("it could not be reached"))
    _faked(monkeypatch, down)
    asked: list[int | None] = []
    collapse = queue.collapse
    monkeypatch.setattr(queue, "collapse", lambda *a, **k: asked.append(k.get("keep")) or collapse(*a, **k))
    noted: list[tuple[str, dict]] = []
    queue.put(["hook"], [_admin(i) for i in range(1, 7)], at=T0, refs=list(range(1, 7)))  # six: past five
    delivery = _delivery(queue, [HOOK], None, clock, admin_kept=5, slack=2, origin="https://latet.acme.lan",
                         noted=lambda conn, action, details: noted.append((action, dict(details))))  # fmt: skip
    _deliver_all(delivery)
    assert noted == [("siem.gap", {"output": "hook", "from": 1, "to": 4, "count": 4})] and asked == [3]
    assert queue.admin_waiting("hook") == 3  # five less its slack: room for the collapse's own entry and more
    queue.put(["hook"], [_admin(7), _admin(8)], at=T0, refs=[7, 8])  # five: at the bound, not past it
    clock.now += TRIM_EVERY
    _deliver_all(delivery)
    assert len(noted) == 1 and asked == [3] and queue.admin_waiting("hook") == 5  # not even asked within the bound
    queue.put(["hook"], [_admin(9)], at=T0, refs=[9])  # past it again
    clock.now += TRIM_EVERY
    _deliver_all(delivery)
    assert noted[1:] == [("siem.gap", {"output": "hook", "from": 1, "to": 7, "count": 7})]  # the marker extended
    assert queue.admin_waiting("hook") == 3
    delivery.close()


def test_the_bound_collapses_before_the_batch_is_read(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    noted: list[tuple[str, dict]] = []
    queue.put(["hook"], [_admin(i) for i in range(1, 6)], at=T0, refs=list(range(1, 6)))
    delivery = _delivery(queue, [HOOK], None, clock, admin_kept=3, slack=0, origin="https://latet.acme.lan",
                         noted=lambda conn, action, details: noted.append((action, dict(details))))  # fmt: skip
    _deliver_all(delivery)
    marker = uuid.uuid5(uuid.NAMESPACE_URL, "https://latet.acme.lan#gap:1-3").hex
    assert hook.sent == [[marker, _admin(4)["id"], _admin(5)["id"]]] and len(noted) == 1  # the marker sent, never lost
    assert queue.state("hook").queued == 0 and queue.state("hook").collapsed == 0  # it delivers again: FAIL ends
    delivery.close()


def test_without_a_way_to_record_it_the_bound_collapses_nothing(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    down = Recording("hook", fault=SendError("it could not be reached"))
    _faked(monkeypatch, down)
    queue.put(["hook"], [_admin(i) for i in range(1, 6)], at=T0, refs=list(range(1, 6)))
    delivery = _delivery(queue, [HOOK], None, clock, admin_kept=3, slack=0, origin="https://latet.acme.lan")
    _deliver_all(delivery)
    assert queue.admin_waiting("hook") == 5 and queue.markers() == []  # a marker and its entry, or neither (R3-1)
    delivery.close()


def test_a_bound_that_cannot_be_kept_now_is_said_by_its_type_and_the_batch_still_goes(monkeypatch, caplog):
    queue, clock = MemoryEvents(), Clock(T0)
    hook = Recording("hook")
    _faked(monkeypatch, hook)

    def trim(output, limit):
        raise ConnectionError("the database at 10.0.0.5 is gone")

    def noted(conn, action, details):
        raise RuntimeError("the log at 10.0.0.5 refused it")

    monkeypatch.setattr(queue, "trim", trim)
    queue.put(["hook"], [_admin(i) for i in range(1, 6)], at=T0, refs=list(range(1, 6)))
    delivery = _delivery(queue, [HOOK], None, clock, admin_kept=3, slack=0, origin="https://latet.acme.lan",
                         noted=noted)  # fmt: skip
    with caplog.at_level(logging.WARNING, logger="latet.ee.events.delivery"):
        _deliver_all(delivery)
    assert hook.sent == [[_admin(i)["id"] for i in range(1, 6)]] and queue.state("hook").last_error is None
    assert queue.markers() == []  # the collapse whose entry failed made no marker (R3-1)
    assert "ConnectionError" in caplog.text and "RuntimeError" in caplog.text and "10.0.0.5" not in caplog.text
    delivery.close()


def test_one_output_that_raises_anything_stops_none_of_the_others(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    first = Recording("first", fault=UnicodeEncodeError("ascii", "Z", 0, 1, "not ASCII"))  # what httpx raised (I8)
    second = Recording("second")
    _faked(monkeypatch, first, second)
    outputs = [SiemOutput(name=name, kind="webhook", url="https://siem.acme.com/in") for name in ("first", "second")]
    queue.put(["first", "second"], _events(2), at=T0)
    delivery = _delivery(queue, outputs, None, clock)
    _deliver_all(delivery)
    assert len(second.sent) == 1 and queue.state("first").last_error == "it could not be sent (UnicodeEncodeError)"
    assert queue.schedule("first") == (T0 + 5, 1)  # tried again later, as any failure
    delivery.close()


def test_a_slow_receiver_ends_its_round_past_the_budget_and_the_next_round_begins_after_it(monkeypatch):
    # One receiver that takes its time (a send may last 20 seconds) never holds the other outputs' turns behind it
    # round after round: the round ends once its budget is spent, and the next one begins with the output after it.
    queue, clock = MemoryEvents(), Clock(T0)
    names = ["slow", "fast-1", "fast-2"]
    slow, fast = Recording("slow", on_send=lambda: time.sleep(0.3)), [Recording("fast-1"), Recording("fast-2")]
    _faked(monkeypatch, slow, *fast)
    outputs = [SiemOutput(name=name, kind="webhook", url=f"https://{name}.acme.com/in") for name in names]
    queue.put(names, _events(1), at=T0)
    delivery = _delivery(queue, outputs, None, clock, budget=0.1)
    assert delivery.round() is True  # its budget spent on the slow one: more to do, at once
    assert slow.calls == 1 and [output.calls for output in fast] == [0, 0]
    delivery.round()
    assert [len(output.sent) for output in fast] == [1, 1] and slow.calls == 1  # after it, then its empty queue
    queue.put(names, _events(1, start=1), at=T0)
    assert delivery.round() is True  # it begins one past where the last began (fast-2), then the slow one: ended
    assert slow.calls == 2 and [len(output.sent) for output in fast] == [1, 2]
    delivery.close()


def test_an_outputs_lease_or_queue_that_fails_stops_none_of_the_others(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    first, second = Recording("first"), Recording("second")
    _faked(monkeypatch, first, second)
    outputs = [SiemOutput(name=name, kind="webhook", url="https://siem.acme.com/in") for name in ("first", "second")]
    queue.put(["first", "second"], _events(2), at=T0)
    locks = MemoryLocks(clock=clock)

    def lease(name, seconds):
        if name == "siem:first":
            raise ConnectionError("Valkey at 10.0.0.7 did not answer")
        return locks.lease(name, seconds)

    delivery = _delivery(queue, outputs, None, clock, lease)
    assert delivery.round() is True and first.calls == 0 and len(second.sent) == 1
    assert queue.state("first").queued == 2  # nothing of it taken off, nothing said for it as the receiver's
    delivery.close()


def test_a_secret_that_cannot_be_read_now_is_that_outputs_failure_alone(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    first, second = Recording("first"), Recording("second")
    _faked(monkeypatch, first, second)
    outputs = [SiemOutput(name=name, kind="webhook", url="https://siem.acme.com/in", secret=f"siem-{name}")
               for name in ("first", "second")]  # fmt: skip

    def secret(output):
        if output.name == "first":
            raise OSError("the database at 10.0.0.5 is gone")
        return "a-signing-key"

    delivery = SiemDelivery(queue, lambda: outputs, secret, MemoryLocks(clock=clock).lease, clock=clock, delays=(5.0,))
    queue.put(["first", "second"], _events(2), at=T0)
    _deliver_all(delivery)
    assert first.calls == 0 and len(second.sent) == 1
    assert queue.state("first").last_error == "its secret could not be read now (OSError)"  # by its type alone
    assert queue.schedule("first") == (T0 + 5, 1) and queue.state("first").queued == 2
    delivery.close()


def test_a_secret_missing_or_unfit_for_a_header_holds_the_queue_and_drops_nothing():
    queue, clock = MemoryEvents(), Clock(T0)
    splunk = SiemOutput(name="splunk", kind="splunk", url="https://siem.acme.com:8088/services/collector",
                        secret="siem-splunk")  # fmt: skip
    signed = SiemOutput(name="signed", kind="webhook", url="https://siem.acme.com/in",
                        headers={"authorization": "Bearer {secret}"}, secret="siem-signed")  # fmt: skip
    kept = {"siem-signed": "tok\N{LATIN SMALL LETTER E WITH ACUTE}n"}  # no HEC token kept; one no header can carry
    delivery = SiemDelivery(queue, lambda: [splunk, signed], lambda output: kept.get(output.secret or ""),
                            MemoryLocks(clock=clock).lease, clock=clock, delays=(5.0,))  # fmt: skip
    queue.put(["splunk", "signed"], _events(2), at=T0)
    _deliver_all(delivery)
    for name, words in (("splunk", "its HEC token is not set"), ("signed", "cannot go in a header")):
        state = queue.state(name)
        assert state.queued == 2 and state.dropped == 0 and words in (state.last_error or ""), state
        assert queue.schedule(name) == (T0 + 5, 1)  # held, tried again once it is set (retry=True)
    delivery.close()


def test_an_output_that_keeps_failing_waits_longer_each_time_then_five_minutes(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    down = Recording("hook", fault=SendError("it could not be reached"))
    _faked(monkeypatch, down)
    queue.put(["hook"], _events(1), at=T0)
    locks, leases = MemoryLocks(clock=clock), []

    def lease(name, seconds):
        leases.append(name)
        return locks.lease(name, seconds)

    delivery = SiemDelivery(queue, lambda: [HOOK], _secret, lease, clock=clock)
    waits = []
    for tried in range(1, 8):
        assert delivery.round() is False and down.calls == tried
        retry_at, tries = queue.schedule("hook")
        assert retry_at is not None and tries == tried
        waits.append(retry_at - clock.now)
        clock.now = retry_at - 1
        asked = len(leases)
        assert delivery.round() is False and down.calls == tried  # not a moment before its time
        assert len(leases) == asked  # nor its lease taken: Valkey is not asked for an output that waits
        clock.now = retry_at
    assert waits == [*RETRY_DELAYS, 300.0, 300.0] == [5.0, 10.0, 30.0, 60.0, 300.0, 300.0, 300.0]
    delivery.close()


@pytest.mark.parametrize("slow", ["send", "bound"])
def test_a_send_or_its_done_needs_five_seconds_of_the_lease_left(monkeypatch, slow):
    queue, clock = MemoryEvents(), Clock(T0)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    if slow == "send":
        hook.on_send = lambda: setattr(clock, "now", clock.now + 56)  # 4 s of the lease left for its done
    else:
        trim = queue.trim
        monkeypatch.setattr(queue, "trim", lambda *a: setattr(clock, "now", clock.now + 56) or trim(*a))  # 4 s to send
    queue.put(["hook"], _events(2), at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    assert delivery.round() is False and hook.calls == (1 if slow == "send" else 0) and queue.state("hook").queued == 2
    assert queue.schedule("hook") == (T0 + 61, 1)  # still its lease: a failure, tried again later (never at once)
    assert "longer than its lock" in (queue.state("hook").last_error or "")
    delivery.close()


def test_a_receiver_refusing_everything_slowly_is_tried_again_later_never_at_once(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    events = _events(4)
    slow = Recording("hook", refused={e["id"] for e in events}, on_send=lambda: setattr(clock, "now", clock.now + 19))
    _faked(monkeypatch, slow)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    assert delivery.round() is False and slow.calls == 3  # the batch, its first half, its first event: 3 s left
    state = queue.state("hook")
    assert state.queued == 4 and state.dropped == 0 and queue.schedule("hook") == (T0 + 57 + 5, 1)
    delivery.close()


def test_a_collapse_made_between_a_send_and_its_done_stays_in_the_queue(monkeypatch):
    monkeypatch.setattr(delivery_module, "BATCH", 2)
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    queue.put(["hook"], [_admin(i) for i in range(1, 7)], at=T0, refs=list(range(1, 7)))
    noted: list[tuple[str, dict]] = []
    other = _delivery(queue, [HOOK], None, clock, locks.lease, admin_kept=3, slack=0, origin="https://latet.acme.lan",
                      noted=lambda conn, action, details: noted.append((action, dict(details))))  # fmt: skip

    class Late:
        """The first instance's queue: its lease runs out right after it asked, and the other instance's round runs
        before its done lands."""

        def __getattr__(self, name):
            return getattr(queue, name)

        def done(self, output, seqs, **options):
            clock.now += 61
            hook.fault = SendError("it could not be reached")
            other.round()  # it takes the lease, collapses the entries 1 to 4, and fails to send them
            return queue.done(output, seqs, **options)

    first = _delivery(Late(), [HOOK], None, clock, locks.lease)
    first.round()
    assert hook.sent == [[_admin(1)["id"], _admin(2)["id"]]] and noted[0][1]["from"] == 1  # sent, then collapsed
    head = queue.peek("hook", 1)[0][1]
    assert head["outcome"] == "siem.gap" and head["reason"] == "audit:1-4" and queue.admin_waiting("hook") == 3
    first.close()
    other.close()


def test_a_send_that_outlives_its_lock_takes_nothing_off_the_queue(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    slow = Recording("hook", on_send=lambda: setattr(clock, "now", clock.now + 61))  # longer than the lock (C1)
    _faked(monkeypatch, slow)
    queue.put(["hook"], _events(3), at=T0)
    first = _delivery(queue, [HOOK], None, clock, locks.lease)
    assert first.round() is False and queue.state("hook").queued == 3  # left to the lock's next holder
    slow.on_send = None
    second = _delivery(queue, [HOOK], None, clock, locks.lease)
    assert second.round() is True and queue.state("hook").queued == 0 and len(slow.sent) == 2  # at least once
    first.close()
    second.close()


@pytest.mark.parametrize(("answer", "n"), [("refused", 4), ("refused", 1), ("failed", 4)])
def test_a_refusal_or_a_failure_that_outlives_its_lock_is_left_to_the_next_holder(monkeypatch, answer, n):
    queue, clock = MemoryEvents(), Clock(T0)
    events = _events(n)
    late = Recording("hook", on_send=lambda: setattr(clock, "now", clock.now + 61))
    if answer == "refused":
        late.refused = {events[-1]["id"]}
    else:
        late.fault = SendError("it could not be reached")
    _faked(monkeypatch, late)
    queue.put(["hook"], events, at=T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    assert delivery.round() is False and late.calls == 1  # no halves sent once the lease is another's (C1)
    assert queue.state("hook") == QueueState(n)  # nothing dropped, no failure written: the next holder's to say
    delivery.close()


def test_an_output_whose_secret_was_set_for_another_address_sends_nothing_and_says_so(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    splunk = SiemOutput(name="splunk", kind="splunk", url=made["splunk"].url, secret="siem-splunk")

    def moved(output):
        raise SecretElsewhere  # sealed for the address it had before (I1)

    delivery = SiemDelivery(queue, lambda: [splunk], moved, MemoryLocks(clock=clock).lease, context=context,
                            clock=clock, delays=(5.0,))  # fmt: skip
    queue.put(["splunk"], _events(1), at=T0)
    _deliver_all(delivery)
    assert made["splunk"].received == [] and "set for another address" in (queue.state("splunk").last_error or "")
    assert delivery.test("splunk") == (
        False,
        "its secret was set for another address: set it again (`latet server siem secret splunk`)",
    )
    delivery.close()


def test_settings_or_a_license_that_cannot_be_read_send_nothing_and_forget_no_queue(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    readable, licensed = [True], [True]

    def outputs():
        if not readable[0]:
            raise SettingsUnread("OperationalError")
        return [HOOK]

    def producing():
        if not licensed[0]:
            raise ConnectionError("the license could not be weighed")
        return True

    delivery = SiemDelivery(queue, outputs, _secret, MemoryLocks(clock=clock).lease, producing=producing, clock=clock)
    assert delivery.round() is False  # the outputs seen once, nothing waiting
    queue.put(["hook"], _events(2), at=T0)
    readable[0] = False
    assert delivery.round() is False and queue.state("hook").queued == 2  # never read as every output removed
    readable[0], licensed[0] = True, False
    assert delivery.round() is False and hook.calls == 0  # a license that cannot be weighed sends nothing
    licensed[0] = True
    assert delivery.round() is True and hook.sent == [[e["id"] for e in _events(2)]]
    delivery.close()


def test_a_delivery_closed_sends_nothing_more(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    read = []
    delivery = SiemDelivery(queue, lambda: read.append(1) or [HOOK], _secret, MemoryLocks(clock=clock).lease,
                            clock=clock)  # fmt: skip
    queue.put(["hook"], _events(1), at=T0)
    _deliver_all(delivery)
    queue.put(["hook"], _events(1, start=1), at=T0)
    delivery.close()
    reads = len(read)
    assert hook.closed and delivery.round() is False and len(hook.sent) == 1 and queue.state("hook").queued == 1
    assert len(read) == reads  # nothing read, nothing forgotten: a closed delivery does nothing


def test_a_close_during_a_round_ends_it_after_the_output_it_sends(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    first, second = Recording("first"), Recording("second")
    _faked(monkeypatch, first, second)
    outputs = [SiemOutput(name=name, kind="webhook", url="https://siem.acme.com/in") for name in ("first", "second")]
    delivery = _delivery(queue, outputs, None, clock)
    closing = threading.Thread(target=delivery.close)  # the instance stops while the round sends to the first
    first.on_send = lambda: (closing.start(), until(lambda: delivery._closed))
    queue.put(["first", "second"], _events(1), at=T0)
    assert delivery.round() is True
    closing.join(10)
    assert len(first.sent) == 1 and second.calls == 0 and first.closed and not closing.is_alive()


async def test_the_delivery_never_runs_on_an_event_loop():
    queue, clock = MemoryEvents(), Clock(T0)
    delivery = _delivery(queue, [HOOK], None, clock)
    queue.put(["hook"], _events(1), at=T0)
    with pytest.raises(RuntimeError, match="worker thread"):
        delivery.round()  # its sends run event loops of their own (asyncio.run): never on the server's
    with pytest.raises(RuntimeError, match="worker thread"):
        delivery.test("hook")
    assert queue.state("hook") == QueueState(1, admin=0)
    delivery.close()


def test_two_instances_send_each_event_once_and_in_order(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    outputs = [SiemOutput(name="hook", kind="webhook", url=made["json"].url)]
    instances = [_delivery(queue, outputs, context, clock, locks.lease) for _ in range(2)]
    events = _events(1200)  # three batches of 500 at most
    queue.put(["hook"], events, at=T0)
    with ThreadPoolExecutor(2) as pool:
        for _ in range(10):
            list(pool.map(lambda delivery: delivery.round(), instances))
    assert [e["id"] for e in made["json"].events()] == [e["id"] for e in events]
    for delivery in instances:
        delivery.close()


def test_an_outputs_failure_is_waited_out_by_every_instance(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    hook = SiemOutput(name="hook", kind="webhook", url=made["json"].url)
    first, second = (_delivery(queue, [hook], context, clock, locks.lease) for _ in range(2))
    made["json"].status = 503
    queue.put(["hook"], _events(1), at=T0)
    assert first.round() is False and queue.schedule("hook") == (T0 + 5, 1)
    made["json"].status = 200
    assert second.round() is False and made["json"].received == []  # it waits, as the first would
    clock.now += 6
    assert second.round() is True and len(made["json"].events()) == 1
    first.close()
    second.close()


def test_an_instance_that_dies_while_sending_hands_over_and_nothing_is_lost(receivers):
    made, context = receivers
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    hook = SiemOutput(name="hook", kind="webhook", url=made["json"].url)
    events = _events(700)
    queue.put(["hook"], events, at=T0)
    dying = locks.lock("siem:hook", 60.0)
    assert dying.__enter__()  # an instance takes the lock...
    in_flight = [event for _, event in queue.peek("hook", 500)]
    output = make_output(hook, None, context)
    output.send(in_flight)  # ...sends its batch, and dies before it takes the batch off the queue
    output.close()
    other = _delivery(queue, [hook], context, clock, locks.lease)
    assert other.round() is False  # the lock is the dead instance's until it runs out
    clock.now += 61
    _deliver_all(other)
    received = [e["id"] for e in made["json"].events()]
    assert received == [e["id"] for e in in_flight] + [e["id"] for e in events]  # the batch in flight twice, in order
    other.close()


def test_an_instance_without_the_lease_leaves_the_queue_alone(monkeypatch):
    queue, clock = MemoryEvents(), Clock(T0)
    locks = MemoryLocks(clock=clock)
    hook = Recording("hook")
    _faked(monkeypatch, hook)
    queue.put(["hook"], _events(1), at=T0)
    touched: list[str] = []
    for method in ("trim", "admin_waiting", "collapse", "peek_rows"):
        real = getattr(queue, method)
        monkeypatch.setattr(queue, method, lambda *a, _real=real, _m=method, **k: touched.append(_m) or _real(*a, **k))
    delivery = _delivery(queue, [HOOK], None, clock, locks.lease)
    with locks.lock("siem:hook", 60.0) as another:  # another instance sends this output's queue now
        assert another and delivery.round() is False
    assert touched == [] and hook.calls == 0  # neither kept short, collapsed nor read by this one
    assert delivery.round() is True and "peek_rows" in touched  # its turn, once the lock is free
    delivery.close()


def test_a_memory_lease_says_when_it_is_no_longer_its_holders():
    clock = Clock(T0)
    locks = MemoryLocks(clock=clock)
    later = locks.lease("siem:hook", 60.0)
    with locks.lease("siem:hook", 60.0) as lease:
        assert lease and lease.held() and lease.held(5.0)
        with locks.lease("siem:hook", 60.0) as other:
            assert not other and not other.held()  # held by the first
        clock.now += 56
        assert lease.held() and not lease.held(5.0)  # 4 s left: too few for a send
        clock.now += 5  # it ran out
        assert not lease.held()
        taken = later.__enter__()  # and another holder takes it
        assert taken and taken.held() and not lease.held()
    with locks.lock("siem:hook", 60.0) as free:
        assert not free  # the first holder left: the lock it lost stays the second's
    later.__exit__(None, None, None)
    with locks.lock("siem:hook", 60.0) as free:
        assert free


@pytest.mark.server
def test_a_valkey_lease_says_when_it_is_no_longer_its_holders(bus):
    with bus.lease("siem:hook", 0.3) as lease:
        assert lease and lease.held() and not lease.held(5.0)  # its time left, from Valkey
        time.sleep(0.5)  # it ran out: Valkey dropped it
        with bus.lease("siem:hook", 5.0) as other:
            assert other and other.held() and other.held(1.0) and not other.held(6.0) and not lease.held()


@pytest.mark.server
def test_two_instances_over_postgresql_and_valkey_send_each_event_once_and_in_order(database, bus, receivers):
    from latet.ee.events.pg import PgEvents

    made, context = receivers
    outputs = [SiemOutput(name="hook", kind="webhook", url=made["json"].url)]
    instances = [_delivery(PgEvents(database), outputs, context, time.time, bus.lease) for _ in range(2)]
    events = _events(1200)
    PgEvents(database).put(["hook"], events, at=time.time())
    with ThreadPoolExecutor(2) as pool:
        for _ in range(10):
            list(pool.map(lambda delivery: delivery.round(), instances))
    assert [e["id"] for e in made["json"].events()] == [e["id"] for e in events]
    for delivery in instances:
        delivery.close()
