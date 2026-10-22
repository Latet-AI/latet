"""The Live Reviewer's queue over a backend that other queues share: a decision taken through one reaches a request
waiting through another, and the changes to the settings and how they ended are the same in each."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from latet.config import Config, IdentityConfig, Profile, TermConfig
from latet.engine.build import build
from latet.engine.types import EntityClass
from latet.engine.vault import Vault
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import Approvals, Change, Decision
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Broadcaster
from latet.runtime import digest, make_runtime
from latet.state import LocalApprovals

from ...conftest import ROOT_KEY
from .conftest import _text_waiting
from .contracts import _Image


@pytest.fixture
def queues():
    """A gateway's queue and a dashboard's, over one backend and one feed: two processes' queues, as a server's are."""
    backend, feed = LocalApprovals(), Broadcaster()
    return Approvals(backend, feed), Approvals(backend, feed)


async def test_a_decision_taken_through_one_queue_reaches_a_request_waiting_through_another(queues):
    gateway, dashboard = queues
    batch = await gateway.open(_text_waiting())
    waiting = asyncio.ensure_future(gateway.wait(batch, 10))
    await asyncio.sleep(0.05)
    assert not waiting.done() and batch.waiting == 1  # the request waits, and counts as a poll held open
    start = batch.items[0]["original"].index("Ada Byron")
    mask = {"item": 0, "start": start, "end": start + len("Ada Byron"), "category": "person"}
    assert await dashboard.decide(batch.id, True, [mask])
    decision = await asyncio.wait_for(waiting, 10)
    assert decision is not None and [m.value for m in decision.masks] == ["Ada Byron"]
    assert batch.waiting == 0


async def test_what_one_queue_opens_and_settles_is_told_to_a_dashboard_watching_through_another(queues):
    gateway, dashboard = queues
    waiting = await gateway.open(_text_waiting())
    events = await dashboard.watch()
    assert events.get_nowait()[1]["batches"][0]["id"] == waiting.id and dashboard.viewers == gateway.viewers == 1
    assert events.get_nowait() == ("viewers", {"viewers": 1})
    opened = await gateway.open(_text_waiting())
    assert events.get_nowait() == ("pending", opened.summary())
    assert await dashboard.decide(opened.id, False)
    assert events.get_nowait() == ("settled", {"id": opened.id, "state": "rejected"})
    gateway.abandon(opened)  # the client went away just after you decided: a decision is told once, and stands
    decision = await gateway.wait(opened, 1)
    assert events.empty() and decision is not None and decision.state == "rejected"
    dashboard.unwatch(events)
    assert gateway.viewers == dashboard.viewers == 0 and events.empty()  # it stopped watching: it hears no more


async def test_a_batch_decided_in_one_queue_leaves_every_queue_and_is_not_decided_twice(queues):
    gateway, dashboard = queues
    batch = _text_waiting()
    batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
    await gateway.open(batch)
    gateway.batches.clear()  # a copy of what waits: the queue loses nothing by it
    assert await dashboard.image(batch.id, 0, sent=True) == ("c2VudA==", "image/png")
    assert await dashboard.decide(batch.id, True)
    assert not await gateway.decide(batch.id, False)  # decided: not again
    assert not await dashboard.decide(batch.id, True)
    assert gateway.batches == {} and dashboard.batches == {}
    assert await gateway.pending() == await dashboard.pending() == []
    assert await gateway.image(batch.id, 0, sent=False) is None
    assert await dashboard.image(batch.id, 0, sent=True) is None
    decision = await gateway.wait(batch, 1)
    assert decision is not None and decision.approved  # the first decision stands


async def test_waiting_for_your_decision_through_decided_is_no_sign_of_life(monkeypatch):
    """The plugin's own polls (``answer``) keep a batch alive; a call that only awaits ``decided`` does not, and a batch
    nobody asks about is given up."""
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.2)
    queue = Approvals()
    batch = await queue.open(_text_waiting(polled=True))
    waiting = asyncio.ensure_future(queue.decided(batch))
    await asyncio.sleep(0)
    assert not waiting.done() and batch.waiting == 0  # it waits, and no poll is open
    decision = await asyncio.wait_for(waiting, 5)
    assert (decision.approved, decision.state) == (False, "abandoned")


async def test_the_plugin_asking_through_the_queue_keeps_its_batch_alive_and_reads_the_answer_given_there(monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.05)
    queue = Approvals()
    batch = await queue.open(_text_waiting(polled=True))
    asking = asyncio.ensure_future(queue.answer(batch.id, 5))
    await asyncio.sleep(0.2)  # four times ABANDON_AFTER, the plugin's poll held open all along
    assert not asking.done() and batch.waiting == 1 and list(queue.batches) == [batch.id]  # not given up
    answer = {"items": [{"text": "call Ada Byron", "count": 0}], "degraded": []}
    with pytest.raises(ValueError, match="wait"):  # what leaves is what you decided: nothing is kept before
        queue.give(batch.id, answer)
    await asyncio.sleep(0.05)
    assert not asking.done()  # the poll that is open did not get the answer before you decided
    assert await queue.decide(batch.id, True)
    queue.give(batch.id, answer)
    assert await asyncio.wait_for(asking, 5) == answer
    with pytest.raises(KeyError):  # handed over once
        await queue.answer(batch.id, 0.05)


class Raced(LocalApprovals):
    """A backend where another process decides a batch in the instant after this queue found it waiting."""

    def get(self, batch_id):
        batch = super().get(batch_id)
        if batch is not None:
            self.settle(batch_id, Decision(False, reason="you did not approve it", state="rejected"))
        return batch


async def test_a_decision_that_comes_second_is_refused_and_does_not_replace_the_first():
    queue = Approvals(Raced(), Broadcaster())
    batch = await queue.open(_text_waiting())
    events = await queue.watch()
    events.get_nowait()
    events.get_nowait()  # the snapshot and the viewers
    assert not await queue.decide(batch.id, True)  # the REST route answers 409: it is not waiting any more
    decision = await queue.wait(batch, 1)
    assert decision is not None and (decision.approved, decision.state) == (False, "rejected")
    assert events.empty()  # a settling that was not this queue's is not told by it


async def test_a_dashboard_that_begins_watching_hears_what_is_told_while_the_queue_is_read_after_the_snapshot():
    """A server reads what waits in a thread (``listed``): what another process tells meanwhile reaches the new viewer,
    after its snapshot, which the dashboard reads first; nothing is lost between the two."""
    feed = Broadcaster()

    class Read(LocalApprovals):
        async def listed(self):
            found = self.waiting()
            feed.publish({"id": "b_decided", "state": "approved"}, "settled")  # told while the queue is read
            await asyncio.sleep(0)
            return found

    queue = Approvals(Read(), feed)
    batch = await queue.open(_text_waiting())
    events = await queue.watch()
    told = [events.get_nowait() for _ in range(events.qsize())]
    assert [event for event, _ in told] == ["snapshot", "settled", "viewers"]
    assert [summary["id"] for summary in told[0][1]["batches"]] == [batch.id]
    assert told[1][1] == {"id": "b_decided", "state": "approved"}
    queue.unwatch(events)


def test_the_changes_to_the_settings_waiting_are_the_same_in_every_queue(queues):
    gateway, dashboard = queues
    older = gateway.open_change(Change(Config(), "base", "command", None, ["it lowers protection"], ["profile"]))
    assert dashboard.changes[older.id] is older and [c["id"] for c in dashboard.pending_changes()] == [older.id]
    newer = dashboard.open_change(
        Change(Config(terms=[TermConfig(value="Orion")]), "base", "file", None, ["a term"], ["terms"])
    )
    assert gateway.settled == {older.id: "superseded"} and list(gateway.changes) == [newer.id]  # one change waits
    gateway.settle_change(newer.id, "rejected")
    assert dashboard.changes == {} and dashboard.settled[newer.id] == "rejected"


def test_equivalent_local_changes_keep_the_first_id_and_are_announced_once(queues):
    """Break: reopen/announce a duplicate, or use source/id instead of config plus base for equivalence."""
    gateway, dashboard = queues
    events = dashboard.broadcaster.subscribe()
    first = gateway.open_change(Change(Config(), "base", "command", None, ["a change"], ["profile"]))
    kept = dashboard.open_change(Change(Config(), "base", "file", "claude-code", ["other words"], ["terms"]))
    assert kept.id == first.id and list(gateway.changes) == [first.id] and dict(gateway.settled) == {}
    assert events.get_nowait() == ("change", first.summary(full=True)) and events.empty()
    changed_base = gateway.open_change(Change(Config(), "another base", "command", None, ["a change"], ["profile"]))
    assert changed_base.id != first.id and list(gateway.changes) == [changed_base.id]
    assert events.get_nowait() == ("change_settled", {"id": first.id, "state": "superseded"})
    assert events.get_nowait() == ("change", changed_base.summary(full=True)) and events.empty()
    dashboard.broadcaster.unsubscribe(events)


def test_local_approval_and_rejection_act_once_under_the_runtime_lock(computer, monkeypatch):
    """Compatibility: local decisions remain serialized and keep the first state under the existing runtime lock."""
    config = Config(identity=IdentityConfig(enabled=False))
    runtime = make_runtime(
        config,
        data=computer / "task23a-data",
        config_file=computer / "task23a-config.yaml",
        root_key=ROOT_KEY,
        record=False,
    )
    proposal = config.model_copy(update={"profile": Profile.SECRETS})
    approvals = runtime.mediator.approvals
    change = approvals.open_change(Change(proposal, digest(config), "command", None, ["profile"], ["profile"]))
    ready = threading.Barrier(2, timeout=5)
    saved, save = [], runtime.settings.save

    def saving(value):
        saved.append(value)
        return save(value)

    def decide(approve):
        ready.wait()
        return runtime.decide_change(change.id, approve)

    monkeypatch.setattr(runtime.settings, "save", saving)
    try:
        with ThreadPoolExecutor(2) as pool:
            approve = pool.submit(decide, True)
            reject = pool.submit(decide, False)
            answers = [approve.result(15), reject.result(15)]
        winner = approvals.settled[change.id]
        # A caller whose initial locked read is after completion returns gone (T23.6 clarification).
        assert sorted(answers) == sorted([winner, "gone"])
        assert winner in ("approved", "rejected") and len(saved) == 1
        assert digest(runtime.config) == digest(proposal if winner == "approved" else config)
        assert digest(runtime.settings.load()) == digest(runtime.config)
    finally:
        runtime.close()


class NewestFirst(dict):
    """How changes ended, kept by a backend whose mapping iterates in an order of its own (a server's hash has none
    to rely on): here the newest first."""

    def __iter__(self):
        return reversed(list(super().__iter__()))


def test_how_a_change_ended_is_there_whatever_order_its_backend_keeps_them_in():
    """The queue writes how a change ended and leaves it to the backend to keep the latest: thinning them out itself,
    it would drop the first one the mapping gives, which need not be the oldest, and a command waiting for that
    change would find it gone with no answer."""
    backend = LocalApprovals()
    backend.settled = NewestFirst({f"older-{n}": "approved" for n in range(100)})
    queue = Approvals(backend, Broadcaster())
    change = queue.open_change(Change(Config(), "base", "command", None, ["it lowers protection"], ["profile"]))
    assert queue.settle_change(change.id, "rejected") is change
    assert queue.settled.get(change.id) == "rejected" and change.id not in queue.changes


def test_a_mediator_uses_the_queue_it_is_given_and_makes_its_own_otherwise():
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    queue = Approvals(LocalApprovals(), Broadcaster())
    given, own = Mediator(built.engine, built.verifier, queue), Mediator(built.engine, built.verifier)
    try:
        assert given.approvals is queue and own.approvals is not queue
        assert isinstance(own.approvals.backend, LocalApprovals) and isinstance(own.approvals.broadcaster, Broadcaster)
        fake = built.vault.intern("EMAIL", EntityClass.PII, "emily@gmail.com").surrogate
        assert queue.is_surrogate(fake) and not queue.is_surrogate("emily@gmail.com")  # the vault's, set on the queue
    finally:
        given.close()
        own.close()
