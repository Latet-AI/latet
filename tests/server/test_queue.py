"""What waits for an identity on a server: the Live Reviewer's queue in Valkey, where a decision taken on one instance
wakes a request waiting on another, and the plugin's answer reaches any; the changes to its settings and its own
settings in PostgreSQL; all of it sealed, and held to the contracts a computer's memory and files meet.

Valkey and the database are written by whoever has their password, without the master key: a record copied under
another key opens nothing, a value written by hand counts as absent, and a wait whose decision cannot come ends, not
approved. What one identity keeps waiting has ceilings; another identity sees none of it; a save made on a copy older
than another instance's save is refused; and an identity's settings are announced once their approved snapshot is
kept, never when what waits for the person is saved."""

import asyncio
import base64
import contextlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import redis

from latet.config import Config, Destination, TermConfig
from latet.engine.vault import MaskingError
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import Approvals, Change, Decision, HumanMask
from latet.gateway.recorder import Broadcaster
from latet.runtime import make_runtime
from latet.server import live, space_settings
from latet.server import queue as queue_module
from latet.server.db import Database
from latet.server.live import ValkeyFeed
from latet.server.master import MasterKey
from latet.server.queue import HELD_MS, VIEWERS_MS, BatchRefused, PgChanges, PgSettled, ValkeyApprovals
from latet.server.sealing import Sealer
from latet.server.shared import SharedSettings, own_part
from latet.server.space_settings import PgSettings, settings_owner
from latet.state import ApprovalsBackend, IdentityRef, local_backends

from ..conftest import ROOT_KEY
from ..unit.state.contracts import ApprovalsContract, SettingsContract, _batch, _Image
from .conftest import deliver, dump, identity_in, valkey_dump

pytestmark = pytest.mark.server

# How long the event loop may wait while the queue's thread keeps a burst of large batches: 0.03 to 0.1 s on a Windows
# computer that runs other work, 0.6 s when that work ran on the loop, as it did before Task 16.
LOOP_HELD = 0.25

MASTER = MasterKey([b"\x05" * 32])
OTHER_KEY = b"\x08" * 32  # another identity's root key
EMILY, DANIEL = "u_emilycarte", "u_danielokaf"
CANARY = "emily.carter@acme.com"
ORION = TermConfig(value="Orion", type="PROJECT")


class TestValkeyApprovals(ApprovalsContract):
    @pytest.fixture
    async def pair(self, db, buses):
        identity = identity_in(db)
        a_bus, b_bus = buses
        for bus, instance in ((a_bus, "a"), (b_bus, "b")):
            bus.client.set(bus.key(f"instance:{instance}"), b"1", px=60_000)  # alive, as each says every 10 s
        a = ValkeyApprovals(a_bus, db, identity, ROOT_KEY, instance="a")
        b = ValkeyApprovals(b_bus, db, identity, ROOT_KEY, instance="b")
        async with deliver(a_bus, f"approvals:{identity}", a), deliver(b_bus, f"approvals:{identity}", b):
            yield a, b
        a.close()
        b.close()


class TestPgSettings(SettingsContract):
    @pytest.fixture
    def pair(self, db, bus):
        identity = identity_in(db)
        shared = SharedSettings(db, bus, MasterKey([b"\x05" * 32]))
        return PgSettings(db, shared, identity, ROOT_KEY), PgSettings(db, shared, identity, ROOT_KEY)


async def test_a_batch_its_instance_stopped_holding_leaves_the_queue(db, buses, monkeypatch):
    monkeypatch.setattr(queue_module, "HELD_MS", 300)
    identity = identity_in(db)
    a = ValkeyApprovals(buses[0], db, identity, ROOT_KEY, instance="a")
    b = ValkeyApprovals(buses[1], db, identity, ROOT_KEY, instance="b")
    batch = _batch(polled=True)
    await a.store(batch)
    assert a._keeper is not None and not a._keeper.done()
    a.close()  # its instance stops: nobody says any more that it holds the batch
    assert [x.id for x in b.waiting()] == [batch.id]
    await asyncio.sleep(0.5)
    assert b.waiting() == [] and not b.settle(batch.id, Decision(True))
    with pytest.raises(KeyError):
        await b.answer(batch.id, 0.05)


async def test_the_texts_and_masks_of_a_batch_never_reach_valkey_in_clear(db, bus, prefix):
    queue = ValkeyApprovals(bus, db, identity_in(db), ROOT_KEY, instance="a")
    batch = _batch(polled=True)
    await queue.store(batch)
    queue.settle(batch.id, Decision(True, [HumanMask(0, "Ada Byron", "person")]))
    queue.give(batch.id, {"items": [{"text": "call Ada Byron on Monday"}]})
    seen = valkey_dump(bus, prefix)
    for clear in ("Ada Byron", "Orion", "Monday"):
        assert clear.encode() not in seen
    queue.close()


async def test_a_batch_is_masked_again_by_its_own_instance_alone_sealed_as_long_as_it_was_to_live(db, buses, prefix):
    """``replace``: the instance that opened a batch holds its texts, so it alone makes its previews again; the record
    stays sealed, lives as long as it was to, and weighs in the ledger what it weighs now."""
    identity = identity_in(db)
    a = ValkeyApprovals(buses[0], db, identity, ROOT_KEY, instance="a")
    b = ValkeyApprovals(buses[1], db, identity, ROOT_KEY, instance="b")
    batch = _batch(polled=True)
    await a.store(batch)
    record = a._key("batch", batch.id)
    lives = buses[0].client.pttl(record)
    again = [dict(batch.items[0], masked="call Eva Moore on Friday about the Halcyon contract " + "x" * 2000)]
    assert await b.replace(batch.id, again) is None  # b does not hold it
    assert await a.replace(batch.id, again) is not None
    assert 0 < buses[0].client.pttl(record) <= lives
    weight = int(buses[0].client.hget(a._key("weights"), f"b:{batch.id}"))
    assert weight >= len(buses[0].client.get(record))  # what the record weighs now, at least
    seen = valkey_dump(buses[0], prefix)
    for clear in ("Eva Moore", "Halcyon", "Ada Byron"):
        assert clear.encode() not in seen
    a.close()
    b.close()


async def test_a_batch_a_decision_claimed_is_never_masked_again(db, bus):
    queue = ValkeyApprovals(bus, db, identity_in(db), ROOT_KEY, instance="a")
    batch = _batch(polled=True)
    await queue.store(batch)
    # another instance's decision has claimed the place of the batch's decision, as ``_CLAIM`` does
    assert bus.client.set(queue._key("decision", batch.id), b"someone", nx=True, px=5_000)
    assert await queue.replace(batch.id, [dict(batch.items[0], masked="anything")]) is None
    assert queue.get(batch.id).items == batch.items
    queue.close()


async def test_a_servers_queue_keeps_a_batch_with_store_alone(db, bus):
    """S11 and F-S3 (area 4, M-2): ``put`` sealed and wrote a batch's records on the event loop, dead on the server's
    path: it is gone from the server's queue and from the backends' protocol, and a batch is kept with ``store``, off
    the loop."""
    queue = ValkeyApprovals(bus, db, identity_in(db), ROOT_KEY, instance="a")
    assert not hasattr(queue, "put") and "put" not in vars(ApprovalsBackend)
    batch = _batch()
    await queue.store(batch)
    assert [x.id for x in queue.waiting()] == [batch.id]
    queue.close()


def test_a_change_waiting_is_sealed_in_the_database(db, bus):
    queue = ValkeyApprovals(bus, db, identity_in(db), ROOT_KEY, instance="a")
    change = Change(Config(terms=[TermConfig(value="Mercury Labs")]), "base", "command", None, ["a term"], ["terms"])
    queue.changes[change.id] = change
    assert b"Mercury" not in dump(db) and queue.changes[change.id] == change  # every field, whoever reads it


def _a_change(term: str) -> Change:
    return Change(Config(terms=[TermConfig(value=term)]), "base", "command", None, ["a term"], ["terms"])


def test_at_most_one_change_waits_whichever_instance_writes_one(db):
    """A computer's queue supersedes the change that waits, then writes the new one: two instances proposing at once
    each supersede nothing and both write. The change written supersedes, in its own transaction, whatever else
    waits."""
    identity_in(db)
    here, there, settled = PgChanges(db, EMILY, ROOT_KEY), PgChanges(db, EMILY, ROOT_KEY), PgSettled(db, EMILY)
    first, second = _a_change("Mercury Labs"), _a_change("Orion")
    here[first.id] = first
    there[second.id] = second  # another instance's, which saw none waiting
    assert list(here) == list(there) == [second.id] and settled[first.id] == "superseded"
    here[second.id] = second  # the same one written again supersedes nothing
    assert list(here) == [second.id]


def test_changes_written_at_once_take_their_turn(db):
    """Each write holds the identity's lock of its changes until it commits: one that comes meanwhile waits, then
    supersedes what was written before it."""
    identity_in(db)
    here, there = PgChanges(db, EMILY, ROOT_KEY), PgChanges(db, EMILY, ROOT_KEY)
    first, second = _a_change("Mercury Labs"), _a_change("Orion")
    here[first.id] = first
    with ThreadPoolExecutor(1) as pool:
        with db.transaction() as conn:  # another write of the identity's, not committed yet
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (queue_module._changes_lock(db.schema, EMILY),))
            racing = pool.submit(there.__setitem__, second.id, second)
            time.sleep(0.5)
            assert not racing.done()  # it waits for that one to commit
        racing.result(30)
    assert list(here) == [second.id] and PgSettled(db, EMILY)[first.id] == "superseded"


def test_how_a_change_ended_first_is_how_it_ended(db):
    """A change approved on one instance and superseded on another a moment later stays approved: the first way it
    ended wins, and one written for a change that is not there is kept."""
    identity_in(db)
    changes, settled, elsewhere = PgChanges(db, EMILY, ROOT_KEY), PgSettled(db, EMILY), PgSettled(db, EMILY)
    change = _a_change("Mercury Labs")
    changes[change.id] = change
    settled[change.id] = "approved"
    elsewhere[change.id] = "superseded"
    assert settled[change.id] == elsewhere[change.id] == "approved" and change.id not in list(changes)
    elsewhere["c_unknown0001"] = "rejected"  # never waited here
    assert settled["c_unknown0001"] == "rejected"


@pytest.mark.timing
async def test_storing_a_large_batch_leaves_the_event_loop_to_the_other_requests(db, queues, monkeypatch):
    """Task 16 measured the loop held 0.24 s by a batch with images, and up to 2.6 s by a burst of them: their JSON,
    their sealing and their weighing, and the round trip that sends them, run in worker threads (``store``), the event
    loop serving the other requests meanwhile. The same refusals come from it."""
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 512 * 1024 * 1024)  # this test weighs the loop, not the budget
    a, b = queues
    text = "y" * (queue_module.BATCH_LIMIT // 2 - 4_000)  # a record near its limit: the text as it came, as it leaves
    burst = []
    for _ in range(3):
        batch = _pictured(_screenshots(3, True))  # three screenshots kept as they came and as they would leave
        batch.items = [dict(batch.items[0], original=text, masked=text)]
        burst.append(batch)
    gaps, ticking = [], True

    async def ticker():
        last = time.perf_counter()
        while ticking:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beating = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        await asyncio.gather(*(a.store(batch) for batch in burst))
    finally:
        ticking = False
        await beating
    assert max(gaps) < LOOP_HELD, f"the loop was held {max(gaps):.3f} s"
    assert sorted(x.id for x in b.waiting()) == sorted(batch.id for batch in burst)
    assert all(not batch.future.done() for batch in burst) and a.holding
    heavy = _batch()
    heavy.items = [dict(heavy.items[0], original="x" * (queue_module.BATCH_LIMIT + 1), masked="")]
    with pytest.raises(BatchRefused, match="less text"):
        await a.store(heavy)
    assert b.get(heavy.id) is None


@pytest.mark.timing
async def test_a_request_opening_large_batches_leaves_the_loop_to_the_others_and_tells_the_dashboards_from_the_queue(
    db, queues, monkeypatch
):
    """The Live Reviewer's requests open their batches through ``Approvals.open``, which kept each one on the event loop
    (``put``): it awaits the queue's ``store``, whose thread makes, seals and writes the records and then tells the
    dashboards ("pending"), a round trip to Valkey that waited on the loop too. The reaper ``open`` starts reads what it
    needs of them alone, never a record. A batch the queue refuses is told to nobody."""
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 512 * 1024 * 1024)  # this test weighs the loop, not the budget
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.05)  # the reaper's rounds, again and again meanwhile
    a, b = queues
    loop_thread = threading.current_thread()
    feed = ValkeyFeed(a.bus, f"approvals:{EMILY}", ROOT_KEY, batches=a.fetch)  # as a space's runtime makes it
    told: list[tuple[str, bool]] = []
    publish = feed.publish

    def noted(payload, event="exchange"):  # which event, and whether on the loop's thread
        told.append((event, threading.current_thread() is loop_thread))
        publish(payload, event)

    monkeypatch.setattr(feed, "publish", noted)
    approvals = Approvals(a, feed)
    text = "y" * (queue_module.BATCH_LIMIT // 2 - 4_000)
    burst = []
    for _ in range(3):
        batch = _pictured(_screenshots(3, True))
        batch.items = [dict(batch.items[0], original=text, masked=text)]
        burst.append(batch)
    gaps, ticking = [], True

    async def ticker():
        last = time.perf_counter()
        while ticking:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beating = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        opened = await asyncio.gather(*(approvals.open(batch) for batch in burst))
        await asyncio.sleep(0.5)  # the reaper's rounds
    finally:
        ticking = False
        await beating
        approvals.close()
    assert max(gaps) < LOOP_HELD, f"the loop was held {max(gaps):.3f} s"
    assert opened == burst and sorted(x.id for x in b.waiting()) == sorted(batch.id for batch in burst)
    assert told == [("pending", False)] * 3  # each told once, from the queue's thread
    heavy = _batch()
    heavy.items = [dict(heavy.items[0], original="x" * (queue_module.BATCH_LIMIT + 1), masked="")]
    with pytest.raises(BatchRefused, match="less text"):
        await approvals.open(heavy)
    monkeypatch.setattr(queue_module, "BATCHES_KEPT", 3)  # one more is refused once its records are made
    one_too_many = _batch()
    with pytest.raises(BatchRefused, match="already wait"):
        await approvals.open(one_too_many)
    assert len(told) == 3 and b.get(heavy.id) is None and b.get(one_too_many.id) is None


async def test_what_a_dashboard_asks_of_the_whole_queue_is_read_off_the_loop(db, queues, monkeypatch):
    """What a dashboard asks of the queue as a whole, what waits, its feed's first snapshot and approving every batch,
    opened each batch's record on the event loop: up to the identity's 64 MiB at each ask. Each is read in the queue's
    thread, and what is told while the snapshot is read comes after it."""
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 512 * 1024 * 1024)
    a, b = queues
    text = "y" * (queue_module.BATCH_LIMIT // 2 - 4_000)
    burst = []
    for _ in range(4):
        batch = _batch()
        batch.items = [dict(batch.items[0], original=text, masked=text)]
        await a.store(batch)
        burst.append(batch)
    dashboard = Approvals(b, ValkeyFeed(b.bus, f"approvals:{EMILY}", ROOT_KEY, batches=b.fetch))  # another instance's
    gaps, ticking = [], True

    async def ticker():
        last = time.perf_counter()
        while ticking:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beating = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        listed = await dashboard.pending()
        events = await dashboard.watch()
        approved = await dashboard.approve_all()
    finally:
        ticking = False
        await beating
    assert max(gaps) < 0.1, f"the loop was held {max(gaps):.3f} s"
    ids = [batch.id for batch in burst]
    assert [summary["id"] for summary in listed] == ids and approved == 4
    event, snapshot = events.get_nowait()
    assert event == "snapshot" and [summary["id"] for summary in snapshot["batches"]] == ids
    dashboard.unwatch(events)
    assert b.waiting() == []


async def test_a_pending_batch_is_read_in_the_queues_thread_and_what_is_heard_behind_it_comes_after_it(
    db, queues, monkeypatch
):
    """S4 (open item 6; area 2, M-3): a viewer's "pending" event reads its batch's record (4 MiB at most) in the
    queue's thread, in the identity's turn, never on the event loop the ear hears every identity on; what is heard
    behind it reaches the viewers after it, as it was heard."""
    a, b = queues
    channel = f"approvals:{EMILY}"
    batch = _batch()
    await a.store(batch)
    read, get = [], b.get
    monkeypatch.setattr(b, "get", lambda batch_id: read.append(threading.current_thread()) or get(batch_id))
    feed = ValkeyFeed(b.bus, channel, ROOT_KEY, batches=b.fetch)  # as a space wires it
    viewer = feed.subscribe()
    settled = ValkeyFeed(a.bus, channel, ROOT_KEY)._message({"id": batch.id, "state": "approved"}, "settled")
    try:
        feed.heard({"pending": batch.id})
        feed.heard(settled)  # heard behind it, before its batch is read
        first, second = [await asyncio.wait_for(viewer.get(), 5) for _ in range(2)]
    finally:
        feed.close()
    assert first[0] == "pending" and first[1]["id"] == batch.id
    assert second == ("settled", {"id": batch.id, "state": "approved"})
    assert read and all(thread.name.startswith("latet-queue") for thread in read)


async def test_a_batch_masked_again_reaches_another_instances_viewers_by_its_id_alone(db, queues):
    """An "updated" batch (its previews made again while it waits) goes on the channel as its id alone, as a "pending"
    does: an instance with viewers reads the sealed record from the queue and hands them its previews of now; no text
    of it is on the channel."""
    a, b = queues
    channel = f"approvals:{EMILY}"
    batch = _batch()
    await a.store(batch)
    again = [dict(batch.items[0], masked="call Eva Moore on Friday about the Halcyon contract")]
    assert await a.replace(batch.id, again) is not None
    message = ValkeyFeed(a.bus, channel, ROOT_KEY, batches=a.fetch)._message(batch.summary(), "updated")
    assert message == {"updated": batch.id}
    feed = ValkeyFeed(b.bus, channel, ROOT_KEY, batches=b.fetch)
    viewer = feed.subscribe()
    try:
        feed.heard(message)
        event, summary = await asyncio.wait_for(viewer.get(), 5)
    finally:
        feed.close()
    assert event == "updated" and summary["id"] == batch.id and summary["items"] == again


async def test_what_is_heard_while_a_pending_batch_is_read_comes_after_it(db, queues, monkeypatch):
    """R5 (the re-review's Minor on S4): the batch a "pending" names is read in the queue's thread, which takes a while;
    a "settled" heard meanwhile (the batch decided on another instance) reaches the viewers after the "pending" it
    followed, never ahead of it, or a batch decided would show as waiting after its "settled"."""
    a, b = queues
    channel = f"approvals:{EMILY}"
    batch = _batch()
    await a.store(batch)
    reading, release, get = threading.Event(), threading.Event(), b.get

    def held(batch_id):  # the read takes a while: the settled is heard meanwhile
        reading.set()
        assert release.wait(10)
        return get(batch_id)

    monkeypatch.setattr(b, "get", held)
    feed = ValkeyFeed(b.bus, channel, ROOT_KEY, batches=b.fetch)
    viewer = feed.subscribe()
    settled = ValkeyFeed(a.bus, channel, ROOT_KEY)._message({"id": batch.id, "state": "approved"}, "settled")
    try:
        feed.heard({"pending": batch.id})
        assert await asyncio.to_thread(reading.wait, 10)
        feed.heard(settled)  # heard while the batch is read
        await asyncio.sleep(0.1)
        release.set()
        first, second = [await asyncio.wait_for(viewer.get(), 5) for _ in range(2)]
    finally:
        release.set()
        feed.close()
    assert first[0] == "pending" and first[1]["id"] == batch.id
    assert second == ("settled", {"id": batch.id, "state": "approved"})


async def test_approving_every_batch_settles_them_in_the_queues_thread_and_wakes_the_waits_here(
    db, queues, monkeypatch
):
    """S4 (open item 6): "approve all" settles up to fifty batches, each with its round trips (its claim, its decision,
    its images let go of, its wake-up): in the queue's thread, in the identity's turn, as its reading is; the waits here
    are woken on the event loop, and the dashboards told from that thread through the identity's feed."""
    a, b = queues
    feed = ValkeyFeed(a.bus, f"approvals:{EMILY}", ROOT_KEY, batches=a.fetch)
    approvals = Approvals(a, feed)
    burst = [_batch(polled=True) for _ in range(5)]
    for batch in burst:
        await a.store(batch)
    waits = [asyncio.ensure_future(a.decision(batch, 10)) for batch in burst]
    written, write = [], a._written
    monkeypatch.setattr(a, "_written", lambda *args: written.append(threading.current_thread()) or write(*args))
    told, publish = [], feed.publish
    monkeypatch.setattr(
        feed, "publish", lambda payload, event="exchange": told.append((event, threading.current_thread()))
        or publish(payload, event)
    )  # fmt: skip
    try:
        assert await approvals.approve_all() == len(burst)
        decisions = await asyncio.wait_for(asyncio.gather(*waits), 10)
    finally:
        approvals.close()
    assert all(decision is not None and decision.approved for decision in decisions)
    loop = threading.current_thread()
    assert len(written) == len(burst) and all(thread is not loop for thread in written)
    assert [event for event, _ in told] == ["settled"] * len(burst) and all(t is not loop for _, t in told)
    assert b.waiting() == [] and await approvals.approve_all() == 0


async def test_a_batch_whose_texts_alone_pass_the_limit_is_refused_before_its_record_is_made(db, queues, monkeypatch):
    """A 13 MiB pasted log held the loop 1.17 s to be refused (its record made and sealed, then weighed): texts that
    alone weigh more than ``BATCH_LIMIT`` are refused as they are."""
    a, _ = queues
    heavy = _batch()
    heavy.items = [dict(heavy.items[0], original="x" * (queue_module.BATCH_LIMIT + 1), masked="")]
    sealed = []
    monkeypatch.setattr(a._sealer, "seal_json", lambda value, aad: sealed.append(aad) or b"")
    with pytest.raises(BatchRefused, match="less text"):
        await a.store(heavy)
    assert sealed == []


async def test_one_identitys_burst_of_batches_delays_another_identitys_by_one_of_them_at_most(
    db, queues, buses, monkeypatch
):
    """The queues' thread is every identity's: an identity's batches take it one at a time, so that a burst of one
    identity's batches delays another identity's batch by one of them at most, never by the whole burst."""
    emily, _ = queues
    daniel = ValkeyApprovals(buses[0], db, DANIEL, ROOT_KEY, instance="a")
    started, go = threading.Event(), threading.Event()
    sealed = emily._sealed_records

    def first_held(batch):  # Emily's first batch holds the thread until Daniel's has asked for it
        if not started.is_set():
            started.set()
            go.wait(10)
        return sealed(batch)

    monkeypatch.setattr(emily, "_sealed_records", first_held)
    kept = []

    async def stored(queue, batch, whose):
        await queue.store(batch)
        kept.append(whose)

    try:
        burst = asyncio.gather(*(stored(emily, _batch(), f"Emily's {n + 1}") for n in range(4)))
        await _until(started.is_set)
        asked = asyncio.ensure_future(stored(daniel, _batch(), "Daniel's"))
        await asyncio.sleep(0.1)  # Daniel's batch has asked for the thread
        go.set()
        await asyncio.gather(burst, asked)
    finally:
        go.set()
        daniel.close()
    assert kept.index("Daniel's") == 1, kept  # after the batch that held the thread, before the rest of the burst


async def test_what_one_dashboard_reads_at_once_delays_another_identitys_batch_by_a_read_or_two_at_most(
    db, queues, buses, monkeypatch
):
    """What a dashboard reads goes to the queues' thread in its identity's turn, as its batches do: many reads asked at
    once (lists of what waits, a decision's batch, views of an image) delay another identity's batch by one of them for
    each of its two steps at most, never by all of them."""
    emily, _ = queues
    daniel = ValkeyApprovals(buses[0], db, DANIEL, ROOT_KEY, instance="a")
    batch = _batch()
    batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
    await emily.store(batch)
    started, go = threading.Event(), threading.Event()
    get = emily.get

    def first_held(batch_id):  # Emily's first read holds the thread until Daniel's batch has asked for it
        if not started.is_set():
            started.set()
            go.wait(10)
        return get(batch_id)

    monkeypatch.setattr(emily, "get", first_held)
    done = []

    async def read(n, reading):
        await reading
        done.append(f"Emily's read {n + 1}")

    async def stored():
        await daniel.store(_batch())
        done.append("Daniel's")

    try:
        kinds = (lambda: emily.fetch(batch.id), emily.listed, lambda: emily.fetch_image(batch.id, 0, sent=True))
        reads = asyncio.gather(*(read(n, kinds[n % 3]()) for n in range(6)))
        await _until(started.is_set)
        asked = asyncio.ensure_future(stored())
        await asyncio.sleep(0.1)  # Daniel's batch has asked for the thread
        go.set()
        await asyncio.gather(reads, asked)
    finally:
        go.set()
        daniel.close()
    assert done.index("Daniel's") <= 2, done  # after the read that held the thread and one between its steps at most


async def test_with_a_full_queue_another_identitys_batch_is_stored_without_waiting_behind_the_reaper(
    db, queues, buses, monkeypatch
):
    """The reaper read every batch waiting, each record opened with its texts, in the queues' thread at each round: half
    a second for an identity's full queue, which every other identity's batch waited behind. It reads what it needs
    alone (``timings``: when each batch held here was made, its time limit, its polls), and opens no record."""
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)  # the reaper's rounds, again and again meanwhile
    emily, _ = queues
    daniel = ValkeyApprovals(buses[0], db, DANIEL, ROOT_KEY, instance="a")
    approvals = Approvals(emily, ValkeyFeed(emily.bus, f"approvals:{EMILY}", ROOT_KEY, batches=emily.fetch))
    text = "y" * (queue_module.BATCH_LIMIT // 2 - 4_000)  # a record near its limit: as it came, as it leaves
    try:
        while True:  # Emily's queue, filled to its bytes
            batch = _batch()
            batch.items = [dict(batch.items[0], original=text, masked=text)]
            try:
                await approvals.open(batch)
            except BatchRefused:
                break
        opened = []
        open_json = emily._sealer.open_json
        monkeypatch.setattr(emily._sealer, "open_json", lambda raw, aad: opened.append(aad) or open_json(raw, aad))
        waits = []
        for _ in range(8):
            started = time.perf_counter()
            await daniel.store(_batch())
            waits.append(time.perf_counter() - started)
            await asyncio.sleep(0.03)
    finally:
        approvals.close()
        daniel.close()
    assert max(waits) < 0.1, f"Daniel's batch waited {max(waits):.3f} s"
    assert opened == [], f"the reaper opened {len(opened)} records"
    assert len(emily.waiting()) >= 12  # a full queue: near 64 MiB of records


async def test_each_instance_reaps_the_batches_it_holds_counting_the_polls_of_every_instance(db, queues, monkeypatch):
    """A batch is held by the instance that opened it, whose reaper gives it up (another instance's never reads it): past
    its time limit, and once nobody asks for it on any instance; a poll held open on another instance keeps it."""
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.3)
    a, b = queues
    approvals = Approvals(a, Broadcaster())
    try:
        expiring = _batch(timeout_s=60)
        expiring.created -= 61
        polled = await approvals.open(_batch(polled=True))
        there = b.get(polled.id)
        assert there is not None and b.timings() == []  # held by instance a: b's reaper never reads it
        b.polled(there, 1)  # the plugin's poll, held open on instance b
        await approvals.open(expiring)
        decision = await asyncio.wait_for(expiring.future, 5)
        assert decision.state == "expired"
        await asyncio.sleep(0.6)  # twice ABANDON_AFTER, with the poll on b open all along
        assert not polled.future.done() and [t.id for t in a.timings()] == [polled.id]
        a.bus.client.hset(a._key("polls", polled.id), "c", 1)  # a poll counted on an instance that stopped
        b.polled(there, -1)  # it ends: nobody asks for it on any instance alive
        decision = await asyncio.wait_for(polled.future, 5)
        assert decision.state == "abandoned" and a.timings() == []
        ended = await approvals.open(_batch(polled=True))
        ended.future.set_result(Decision(False, reason=queue_module.UNDECIDED, state="abandoned"))
        assert a.timings() == []  # its wait ended here, its record left behind (Valkey failed the drop): not reaped
    finally:
        approvals.close()


async def test_what_the_queue_counts_of_each_entry_is_what_valkey_takes_for_it(db, queues):
    """A value's allocation is rounded up to a size class (up to a quarter more: 56 MiB counted were 70 MiB held), and
    a key takes a few hundred bytes beside it, which a flood of tiny decisions is made of: what the queue counts of a
    batch's records, a decision and an answer bounds what Valkey holds for them."""
    a, _ = queues
    client = a.bus.client
    batch = _batch(polled=True)
    await a.store(batch)
    keys = [a._key(kind, batch.id) for kind in ("batch", "seen", "answer")]
    weights = client.hgetall(a._key("weights"))
    assert int(weights[f"b:{batch.id}".encode()]) == sum(live.weighed(client.strlen(key)) for key in keys)
    assert sum(client.memory_usage(key, samples=0) for key in keys) <= int(weights[b"total"])
    assert a.settle(batch.id, Decision(True))
    decision = a._key("decision", batch.id)
    assert int(client.hget(a._key("weights"), f"d:{batch.id}")) == live.weighed(client.strlen(decision))
    a.give(batch.id, {"items": [{"text": "ok", "count": 0}], "degraded": []})
    slot = a._key("answer", batch.id)
    answer = int(client.hget(a._key("weights"), f"a:{batch.id}"))
    assert answer == live.weighed(client.strlen(slot)) >= client.memory_usage(slot, samples=0)


# --- helpers ----------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def first_lines_said(monkeypatch):
    """Each test's own count of what does not open: the first line of each family is said at once."""
    monkeypatch.setattr(live, "UNREADABLE", live._Unreadable())
    monkeypatch.setattr(space_settings, "UNOPENED", space_settings.Unopened())


@pytest.fixture
def queues(db, buses):
    """Emily's queue on two instances, both alive, each hearing nothing: a wake-up is lost unless a test delivers it.
    Closed after the test."""
    identity_in(db)
    made = []
    for bus, instance in zip(buses, ("a", "b"), strict=True):
        bus.client.set(bus.key(f"instance:{instance}"), b"1", px=60_000)
        made.append(ValkeyApprovals(bus, db, EMILY, ROOT_KEY, instance=instance))
    yield made
    for queue in made:
        queue.close()


async def _until(check, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "not in time"
        await asyncio.sleep(0.02)


def _write(db, query, *args):
    """A change made by hand: what someone who writes the database without the master key can do."""
    with db.connection() as conn:
        conn.execute(query, args)


def _read(db, query, *args):
    with db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _counter(db, owner: str) -> int | None:
    found = _read(db, "SELECT counter FROM change_counters WHERE owner = %s", owner)
    return found[0][0] if found else None


def _kept(db, kind: str) -> bytes | None:
    """What Emily's settings row of ``kind`` holds, sealed."""
    found = _read(db, "SELECT data FROM settings WHERE owner = %s AND kind = %s", EMILY, kind)
    return bytes(found[0][0]) if found else None


def _keep(db, kind: str, data: bytes) -> None:
    """Emily's settings row of ``kind``, written by hand."""
    _write(
        db,
        "INSERT INTO settings (owner, identity_id, kind, data, changed) VALUES (%s, %s, %s, %s, 0)"
        " ON CONFLICT (owner, kind) DO UPDATE SET data = EXCLUDED.data",
        EMILY, EMILY, kind, data,
    )  # fmt: skip


def _waiting_change(db, change_id: str, data: bytes) -> None:
    _write(
        db, "INSERT INTO changes (identity_id, id, data, created) VALUES (%s, %s, %s, %s)", EMILY, change_id, data, 1.0
    )


def _terms(config: Config) -> list[str]:
    return [term.value for term in config.terms]


def _with(*terms: TermConfig):
    return lambda current: current.model_copy(update={"terms": [*current.terms, *terms]})


@contextlib.contextmanager
def _space(db, shared, folder):
    """Emily's runtime over her settings in PostgreSQL (what Task 12 builds over all of a server's backends), the rest
    of its state in ``folder``."""
    backends = replace(
        local_backends(folder, None, ROOT_KEY, record=False), settings=PgSettings(db, shared, EMILY, ROOT_KEY)
    )
    try:
        runtime = make_runtime(
            None, data=folder, record=False, root_key=ROOT_KEY, backends=backends,
            identity=IdentityRef(EMILY, "Emily Carter", "person"),
        )  # fmt: skip
    except BaseException:
        backends.vault.close()
        raise
    try:
        yield runtime
    finally:
        runtime.close()


class _NoValkey:
    """A client of Valkey that notes whatever it is asked, and answers nothing."""

    def __init__(self):
        self.asked = []

    def __getattr__(self, name):
        self.asked.append(name)
        raise AssertionError(f"Valkey was asked {name}")


# --- each record bound to its own key (R-1, I3) -----------------------------------------------------------------------


async def test_a_batch_a_decision_or_an_answer_copied_under_another_batchs_key_does_not_open(db, queues):
    a, b = queues
    client = a.bus.client
    shown, hidden = _batch(), _batch()
    await a.store(shown)
    await a.store(hidden)
    client.set(a._key("batch", hidden.id), client.get(a._key("batch", shown.id)), px=60_000)
    assert [x.id for x in b.waiting()] == [shown.id] and b.get(hidden.id) is None  # not listed under another's key

    approved, waiting = _batch(), _batch()
    await a.store(approved)
    await a.store(waiting)
    assert b.settle(approved.id, Decision(True))
    client.set(a._key("decision", waiting.id), client.get(a._key("decision", approved.id)))
    decision = await asyncio.wait_for(a.decision(waiting, None), 5)
    assert decision is not None and not decision.approved and decision.state == "abandoned"  # never let out
    assert not b.settle(waiting.id, Decision(True))  # nor decided as if the copy were its own

    first, second = _batch(polled=True), _batch(polled=True)
    await a.store(first)
    await a.store(second)
    a.settle(first.id, Decision(True))
    a.settle(second.id, Decision(True))
    a.give(first.id, {"items": [{"text": "first", "count": 0}], "degraded": []})
    client.set(a._key("answer", second.id), client.get(a._key("answer", first.id)), xx=True)
    assert await b.answer(second.id, 0.2) is None  # not its answer: none given
    with pytest.raises(KeyError):
        await b.answer(second.id, 0.05)
    assert await b.answer(first.id, 5) == {"items": [{"text": "first", "count": 0}], "degraded": []}


async def test_an_image_moved_to_another_item_of_its_batch_does_not_open(db, queues):
    """A screenshot you would withhold, swapped with a harmless picture of the same batch: the dashboard shows neither,
    so that your choice is never applied to the other one."""
    a, b = queues
    batch = _batch()
    batch.items = [batch.items[0], dict(batch.items[0])]
    batch.images = {0: _Image("c2NyZWVuc2hvdA==", "c2NyZWVuc2hvdA=="), 1: _Image("Y2F0", "Y2F0")}
    await a.store(batch)
    client = a.bus.client
    first, second = a._image_key(batch.id, 0), a._image_key(batch.id, 1)
    screenshot, cat = client.get(first), client.get(second)
    client.set(first, cat, px=60_000)
    client.set(second, screenshot, px=60_000)
    assert [b.image(batch.id, item, sent) for item in (0, 1) for sent in (False, True)] == [None] * 4


# --- a wait ends (I5) -------------------------------------------------------------------------------------------------


async def test_a_decision_whose_wake_up_was_lost_still_reaches_the_request_waiting_for_it(db, queues, monkeypatch):
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.2)
    a, b = queues  # neither hears the other: the wake-up is lost
    batch = _batch(polled=True)
    await a.store(batch)
    waiting = asyncio.ensure_future(a.decision(batch, None))  # as the plugin's answer waits: with no time limit
    await asyncio.sleep(0.05)
    masks = [HumanMask(0, "Ada Byron", "person")]
    assert b.settle(batch.id, Decision(True, masks))
    decision = await asyncio.wait_for(waiting, 0.2 + 1)
    assert decision is not None and decision.approved and decision.masks == masks


async def test_a_wait_ends_not_approved_once_its_batch_left_the_queue_or_its_decision_was_written_by_hand(
    db, queues, monkeypatch
):
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.2)
    a, b = queues
    gone, forged = _batch(), _batch(polled=True)
    await a.store(gone)
    await a.store(forged)
    waits = [asyncio.ensure_future(a.decision(batch, None)) for batch in (gone, forged)]
    await asyncio.sleep(0.05)
    client = a.bus.client
    client.delete(a._key("batch", gone.id))  # lost: the loop held past HELD_MS, or Valkey restarted
    client.set(a._key("decision", forged.id), b"junk")  # written before anyone decided
    for wait in waits:
        decision = await asyncio.wait_for(wait, 0.2 + 1)
        assert decision is not None and not decision.approved and decision.state == "abandoned"
    assert not b.settle(forged.id, Decision(True)) and b.waiting() == []  # nobody decides it any more
    withheld = {"items": [{"text": "[withheld by Latet]", "count": 0}], "degraded": []}
    a.give(forged.id, withheld)  # the plugin is told it was not sent
    assert await b.answer(forged.id, 5) == withheld


async def test_closing_ends_every_wait_here_at_once_and_asks_valkey_nothing(db, queues, monkeypatch):
    a, _ = queues
    batch = _batch(polled=True)
    await a.store(batch)
    decided = asyncio.ensure_future(a.decision(batch, None))
    polled = asyncio.ensure_future(a.answer(batch.id, 30))
    await _until(lambda: batch.id in a._answers)  # the poll waits: it made its future, then it awaits it
    bus, client = a.bus, a.bus.client
    monkeypatch.setattr(bus, "client", _NoValkey())
    a.close()  # Valkey may be what does not answer
    assert bus.client.asked == []
    monkeypatch.setattr(bus, "client", client)
    decision = await asyncio.wait_for(decided, 1)
    assert decision is not None and not decision.approved and decision.state == "abandoned"
    assert await asyncio.wait_for(polled, 1) is None  # the plugin asks again, wherever it is served


async def test_the_keeper_and_a_wake_up_go_on_when_valkey_fails_them(db, queues, monkeypatch, caplog):
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.1)
    monkeypatch.setattr(queue_module, "HELD_MS", 1_000)
    a, _ = queues
    batch = _batch()
    await a.store(batch)
    client = a.bus.client
    real, failed = client.pipeline, []

    def once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise redis.ConnectionError("Error 10061 connecting to valkey.internal:6379.")
        return real(*args, **kwargs)

    caplog.set_level(logging.WARNING, logger="latet.server.queue")
    monkeypatch.setattr(client, "pipeline", once)
    await asyncio.sleep(0.6)
    assert failed and a._keeper is not None and not a._keeper.done()
    assert client.pttl(a._key("batch", batch.id)) > 600  # renewed after the round that failed
    waiting = asyncio.ensure_future(a.decision(batch, None))
    await asyncio.sleep(0.05)

    def broken(*args, **kwargs):
        raise redis.ConnectionError("Error 10061 connecting to valkey.internal:6379.")

    monkeypatch.setattr(client, "pipeline", broken)
    a.heard({"decided": batch.id})  # never raises: the keeper reads the decision again
    monkeypatch.setattr(client, "pipeline", real)
    waiting.cancel()
    [line] = [record.getMessage() for record in caplog.records if record.name == "latet.server.queue"]
    assert "ConnectionError" in line and EMILY in line and "valkey.internal" not in line


async def test_a_claim_renewed_by_hand_ends_the_wait_not_approved_and_a_real_claim_is_still_delivered(
    db, queues, monkeypatch
):
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.2)
    monkeypatch.setattr(queue_module, "CLAIM_MS", 1_000)
    a, b = queues
    forged = _batch(polled=True)
    await a.store(forged)
    wait = asyncio.ensure_future(a.decision(forged, None))  # as the plugin's answer waits: with no time limit
    place, started = a._key("decision", forged.id), time.monotonic()
    while not wait.done() and time.monotonic() - started < 5:
        b.bus.client.set(place, b"claim:" + b"0" * 32, px=1_000)  # Valkey's writer renews it before it ends
        await asyncio.sleep(0.3)
    decision = await asyncio.wait_for(wait, 1)
    assert decision is not None and not decision.approved and decision.state == "abandoned"
    assert time.monotonic() - started < 2 * 1.0 + 0.2 + 0.5  # two claims' lives and a round, from the first seen

    real = _batch()
    await a.store(real)
    waiting = asyncio.ensure_future(a.decision(real, None))
    claim = b._claimed(real.id)  # another instance claims the place, and writes its decision a moment later
    assert claim is not None
    await asyncio.sleep(0.5)  # the wait sees the claim for two rounds
    assert b._written(real.id, Decision(True), claim)
    decision = await asyncio.wait_for(waiting, 1)
    assert decision is not None and decision.approved

    again = _batch()  # a claim whose instance stopped before writing, then another's, later: counted afresh
    await a.store(again)
    waiting = asyncio.ensure_future(a.decision(again, None))
    b.bus.client.set(a._key("decision", again.id), b"claim:" + b"1" * 32, px=400)
    await asyncio.sleep(1.5)  # seen, then gone for a round; the first claim was seen longer ago than its time
    claim = b._claimed(again.id)
    assert claim is not None
    await asyncio.sleep(0.3)
    assert not waiting.done() and b._written(again.id, Decision(True), claim)
    decision = await asyncio.wait_for(waiting, 1)
    assert decision is not None and decision.approved


# --- ceilings (I4, I6) ------------------------------------------------------------------------------------------------


async def test_a_batch_heavier_than_the_limit_is_refused_and_nothing_of_it_is_kept_or_told(
    db, queues, prefix, monkeypatch
):
    monkeypatch.setattr(queue_module, "BATCH_LIMIT", 64 * 1024)
    monkeypatch.setattr(queue_module, "IMAGES_LIMIT", 64 * 1024)
    a, b = queues
    heavy = _batch()
    text = "x" * (64 * 1024 + 1)
    heavy.items = [dict(heavy.items[0], original=text, masked=text)]
    pictured = _batch()
    pictured.images = {0: _Image("x" * 40_000, "y" * 40_000)}  # its images have a limit of their own
    light = _batch()
    approvals = Approvals(a, ValkeyFeed(a.bus, f"approvals:{EMILY}", ROOT_KEY))
    heard = []

    class Ear:
        def heard(self, message):
            heard.append(message)

    async with deliver(b.bus, f"approvals:{EMILY}", Ear()):
        for refused, advice in ((heavy, "less text"), (pictured, "fewer or smaller images")):
            with pytest.raises(MaskingError, match="65536") as raised:  # the request is stopped, and says why
                await approvals.open(refused)
            assert advice in str(raised.value) and " off" not in str(raised.value)  # never: turn the reviewer off
        await approvals.open(light)
        await _until(lambda: heard)
        await asyncio.sleep(0.1)
    assert len(heard) == 1 and "feed" in heard[0]  # the pending event of the light one alone
    assert [x.id for x in b.waiting()] == [light.id]
    seen = valkey_dump(a.bus, prefix)
    assert heavy.id.encode() not in seen and pictured.id.encode() not in seen


def _screenshots(count: int, masked: bool) -> dict[int, _Image]:
    """``count`` screenshots of 2 MB, each as it came and, ``masked``, as it would leave once masked."""
    found = {}
    for item in range(1, count + 1):
        data = base64.b64encode(os.urandom(2_000_000)).decode()
        found[item] = _Image(data, base64.b64encode(os.urandom(2_000_000)).decode() if masked else data)
    return found


def _pictured(images: dict[int, _Image]):
    batch = _batch(polled=True)
    batch.images = images
    return batch


def _strings(bus, prefix: str) -> int:
    """What the test's prefix holds in strings (records, images, decisions, answers), in bytes."""
    found = [key for key in bus.client.scan_iter(match=prefix + "*", count=1000) if bus.client.type(key) == b"string"]
    return sum(bus.client.strlen(key) for key in found)


async def test_ordinary_screenshots_are_kept_and_a_requests_images_have_a_limit_of_their_own(db, queues):
    a, _ = queues
    for count, masked in ((1, False), (3, True), (6, False)):  # a clean image is kept once: it leaves as it came
        batch = _pictured(_screenshots(count, masked))
        await a.store(batch)
        assert a.settle(batch.id, Decision(True))
        assert not a.bus.client.exists(a._image_key(batch.id, 1))  # decided here: its images go with it
    with pytest.raises(BatchRefused, match="fewer or smaller images") as refused:
        await a.store(_pictured(_screenshots(7, False)))
    assert " off" not in str(refused.value)


async def test_what_one_identity_keeps_has_a_byte_budget_whichever_instance_puts_it(db, queues, prefix, monkeypatch):
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 7 * 1024 * 1024)  # two screenshots, as Valkey takes them
    a, b = queues
    first, second = _pictured(_screenshots(1, False)), _pictured(_screenshots(1, False))
    await a.store(first)
    await b.store(second)
    for queue in (a, b):
        refused = _pictured(_screenshots(1, False))
        with pytest.raises(BatchRefused, match="decide on those first") as raised:
            await queue.store(refused)
        assert " off" not in str(raised.value) and refused.id.encode() not in valkey_dump(queue.bus, prefix)
    assert a.settle(first.id, Decision(True))  # decided: its weight goes
    await a.store(_pictured(_screenshots(1, False)))
    assert _strings(a.bus, prefix) <= 7 * 1024 * 1024


async def test_a_batch_decided_on_another_instance_stops_weighing_at_once(db, queues, monkeypatch):
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 3 * 1024 * 1024)
    a, b = queues

    def wordy():
        batch = _batch()
        batch.items = [dict(batch.items[0], original="w" * 800_000, masked="w" * 800_000)]  # its texts: 1.6 MB
        return batch

    first = wordy()
    await a.store(first)
    with pytest.raises(BatchRefused, match="decide on those first"):
        await b.store(wordy())
    assert b.settle(first.id, Decision(True))  # decided where it is not held: its record goes with its weight
    await b.store(wordy())


async def test_what_nobody_holds_any_more_stops_weighing_once_its_time_is_over(db, queues, monkeypatch):
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(queue_module, "HELD_MS", 300)
    a, b = queues
    await a.store(_pictured(_screenshots(1, False)))
    a.close()  # its instance stops: nobody holds it, and nobody lets go of its weight
    with pytest.raises(BatchRefused, match="decide on those first"):
        await b.store(_pictured(_screenshots(1, False)))
    await asyncio.sleep(0.5)
    await b.store(_pictured(_screenshots(1, False)))  # its time is over: what it weighed no longer counts


async def test_an_answer_past_the_byte_budget_is_kept_as_an_error_and_one_taken_frees_its_weight(
    db, queues, prefix, monkeypatch
):
    monkeypatch.setattr(queue_module, "QUEUE_BYTES", 3 * 1024 * 1024)
    a, b = queues
    first, second = _batch(polled=True), _batch(polled=True)
    for batch in (first, second):
        await a.store(batch)
        assert a.settle(batch.id, Decision(True))
    large = {"items": [{"text": "x" * 2_000_000, "count": 0}], "degraded": []}  # a call's items as they leave
    a.give(first.id, large)
    a.give(second.id, large)  # past the budget: the plugin is told it failed, and nothing of it leaves
    instead = await b.answer(second.id, 5)
    assert instead is not None and set(instead) == {"error"} and " off" not in instead["error"]
    assert _strings(a.bus, prefix) <= 3 * 1024 * 1024
    heavy = _batch()
    heavy.items = [dict(heavy.items[0], original="z" * 600_000, masked="z" * 600_000)]
    with pytest.raises(BatchRefused, match="decide on those first"):
        await a.store(heavy)
    assert await b.answer(first.id, 5) == large  # taken: its weight goes
    await a.store(heavy)


async def test_a_burst_of_large_batches_keeps_every_message_small_and_every_ear_listening(db, queues):
    """Many large batches opened at once: each "pending" event names its batch, whose record an instance with viewers
    reads from the queue, so that no message on the bus comes near what Valkey lets a subscriber fall behind by (every
    instance hears every identity's); the other instance's viewer still gets every event, whole."""
    a, b = queues
    channel = f"approvals:{EMILY}"
    text = "y" * (
        queue_module.BATCH_LIMIT // 2 - 4_000
    )  # each record near the limit: its text as it came, as it leaves
    spy = a.bus.client.pubsub()
    spy.psubscribe(a.bus.key("approvals:*"))
    assert spy.get_message(timeout=5)["type"] == "psubscribe"
    theirs = ValkeyFeed(b.bus, channel, ROOT_KEY, batches=b.fetch)
    viewer = theirs.subscribe()
    approvals = Approvals(a, ValkeyFeed(a.bus, channel, ROOT_KEY, batches=a.fetch))
    opened, wires = [], []
    try:
        async with deliver(b.bus, channel, theirs, b):
            burst = []
            for _ in range(14):  # the review's probe: ten cut every ear
                batch = _batch()
                batch.items = [dict(batch.items[0], original=text, masked=text)]
                burst.append(batch)
            await asyncio.gather(*(approvals.open(batch) for batch in burst))  # all at once, told in their turn
            opened = [batch.id for batch in burst]
            heard = [await asyncio.wait_for(viewer.get(), 30) for _ in opened]
        while (message := spy.get_message(timeout=1)) is not None:
            if message["type"] == "pmessage":
                wires.append(len(message["data"]))
    finally:
        spy.close()
        if approvals._reaper is not None:
            approvals._reaper.cancel()
    assert [(event, summary["id"]) for event, summary in heard] == [("pending", batch_id) for batch_id in opened]
    assert all(summary["items"][0]["original"] == text for _, summary in heard)
    assert len(wires) == len(opened) and max(wires) < 1024


async def test_what_one_identity_keeps_waiting_has_a_ceiling_whichever_instance_puts_it(
    db, queues, prefix, monkeypatch
):
    monkeypatch.setattr(queue_module, "BATCHES_KEPT", 3)
    a, b = queues
    kept = [_batch(), _batch(), _batch()]
    await a.store(kept[0])
    await b.store(kept[1])
    await a.store(kept[2])
    for queue in (a, b):
        refused = _batch()
        with pytest.raises(BatchRefused, match="3 requests"):
            await queue.store(refused)
        assert refused.id.encode() not in valkey_dump(queue.bus, prefix)
    assert len(b.waiting()) == 3
    assert b.settle(kept[0].id, Decision(True))
    await a.store(_batch())  # one was decided: there is room again
    monkeypatch.setattr(queue_module, "BATCHES_KEPT", 2)
    assert len(b.waiting()) == 2  # what is read is at most the ceiling


def test_settings_heavier_than_the_limit_are_not_kept(db, bus, monkeypatch):
    identity_in(db)
    monkeypatch.setattr(space_settings, "SETTINGS_LIMIT", 4096)
    store = PgSettings(db, SharedSettings(db, bus, MASTER), EMILY, ROOT_KEY)
    store.load()
    heavy = store.prepare(Config(terms=[TermConfig(value=f"Client {n:04}") for n in range(200)]))
    with pytest.raises(OSError, match="4096"):
        store.save(heavy)
    with pytest.raises(OSError, match="8192"):  # a person's snapshot: both parts' limits
        store.save_reviewed(heavy)
    assert _kept(db, "own") is None and _kept(db, "reviewed") is None


def test_a_persons_approval_is_kept_when_both_parts_of_her_settings_are_near_their_limits(db, bus, monkeypatch):
    """A person's approved snapshot holds the shared settings whole and her own part: bound at the sum of both parts'
    limits, her approval of settings whose two parts each weigh near theirs is kept, where it was refused at every
    apply (whatever lowered protection was then composed back and asked again). An API key's holds its own part alone,
    bound as that part is."""
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    gateways = [Destination(name=f"gateway-{n:04}", url=f"https://gateway-{n:04}.acme.com") for n in range(150)]
    shared.set(Config(upstreams=gateways), actor="admin:t_test")  # the company's destinations: what weighs there
    store = PgSettings(db, shared, EMILY, ROOT_KEY)
    found = store.load()
    mine = [TermConfig(value=f"Her client {n:04}") for n in range(150)]
    config = store.prepare(found.model_copy(update={"terms": [*found.terms, *mine]}))
    sealer = Sealer(ROOT_KEY, b"settings")
    own = len(sealer.seal_json(own_part(found, config), f"{EMILY}:own"))
    theirs = len(MASTER.seal(json.dumps(shared.get().model_dump(mode="json")).encode(), "settings:shared"))
    whole = len(sealer.seal_json(config.model_dump(mode="json"), f"{EMILY}:reviewed"))
    limit = max(own, theirs)
    assert limit < whole <= 2 * limit  # each part within the limit, the snapshot past it
    monkeypatch.setattr(space_settings, "SETTINGS_LIMIT", limit)
    store.save(config)
    store.save_reviewed(config)
    assert _kept(db, "reviewed") is not None and store.reviewed() == config
    key = PgSettings(db, shared, identity_in(db, DANIEL, "Billing CI", kind="key"), ROOT_KEY, kind="key")
    key.load()
    monkeypatch.setattr(space_settings, "SETTINGS_LIMIT", own - 1)  # a key's snapshot is its own part
    with pytest.raises(OSError, match=str(own - 1)):
        key.save_reviewed(key.prepare(config))


# --- what does not open or cannot be read (M1) ------------------------------------------------------------------------


async def test_what_does_not_open_or_parse_counts_as_absent_and_is_said_once_for_its_family(db, queues, caplog):
    a, b = queues
    caplog.set_level(logging.WARNING)
    client = a.bus.client
    damaged_life, damaged, fine = _batch(polled=True), _batch(), _batch()
    for batch in (damaged_life, damaged, fine):
        await a.store(batch)
    client.set(a._key("seen", damaged_life.id), b"junk", px=60_000)
    client.hset(a._key("polls", damaged_life.id), "b", "junk")
    client.set(a._key("batch", damaged.id), b"junk", px=60_000)
    client.zadd(a._key("batches"), {b"\xff\xfe": time.time() * 1000 + 60_000})
    listed = b.waiting()
    assert [x.id for x in listed] == [damaged_life.id, fine.id] and listed[0].waiting == 0
    assert b.get(damaged.id) is None

    client.hset(a._key("viewers"), "a", "junk")
    assert a.viewers(1) == 1  # its own count, written by hand: counted again from it
    client.hset(a._key("viewers"), "b", "junk")
    assert b.viewers() == 1 and client.hget(a._key("viewers"), "b") is None  # one that cannot be read: none, dropped

    pictured = _batch()
    pictured.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
    await a.store(pictured)
    client.set(a._image_key(pictured.id, 0), b"junk", px=60_000)
    assert b.image(pictured.id, 0, sent=False) is None

    asked = _batch(polled=True)
    await a.store(asked)
    a.settle(asked.id, Decision(True))
    client.set(a._key("answer", asked.id), b"junk", xx=True)
    assert await b.answer(asked.id, 0.1) is None
    with pytest.raises(KeyError):
        await b.answer(asked.id, 0.05)

    for message in ({"decided": 5}, {"answered": ["x"]}, {"decided": "nothing-with-this-id"}, {"feed": "x"}):
        a.heard(message)

    change = Change(Config(), "base", "command", None, ["a term"], ["terms"])
    a.changes[change.id] = change
    _waiting_change(db, "written-by-hand", b"junk")
    assert list(b.changes) == [change.id] and b.changes.get("written-by-hand") is None and len(b.changes) == 1

    lines = [record.getMessage() for record in caplog.records]
    for family in ("seen", "polls", "batch", "batches", "viewers", "image", "answer"):
        assert sum(f"Valkey's {family} family" in line for line in lines) == 1, family
    assert sum("waiting changes" in line and EMILY in line for line in lines) == 1
    assert "junk" not in caplog.text


# --- lifetimes, a stopped instance's counts (M2) ----------------------------------------------------------------------


async def test_the_index_and_the_viewers_live_and_what_a_stopped_instance_counted_does_not_count(db, queues, buses):
    a, b = queues
    client = a.bus.client
    stopped = ValkeyApprovals(buses[0], db, EMILY, ROOT_KEY, instance="c")  # its instance no longer says it is alive
    batch = _batch(polled=True)
    await a.store(batch)
    assert 0 < client.pttl(a._key("batches")) <= HELD_MS
    stopped.polled(batch, 1)  # a poll began there, and never ended
    stopped.viewers(1)  # a dashboard watched there
    seen = b.get(batch.id)
    assert seen is not None and seen.waiting == 0 and b.viewers() == 0
    assert client.hget(a._key("viewers"), "c") is None  # its count is dropped
    assert a.viewers(1) == 1 and HELD_MS < client.pttl(a._key("viewers")) <= VIEWERS_MS
    client.set(a.bus.key("instance:c"), b"1", px=60_000)
    seen = b.get(batch.id)
    assert seen is not None and seen.waiting == 1  # alive: its poll counts
    stopped.close()


async def test_the_keeper_never_cuts_the_time_the_plugins_answer_has(db, queues, monkeypatch):
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.1)
    a, b = queues
    batch = _batch(polled=True)
    await a.store(batch)
    assert b.settle(batch.id, Decision(True))  # a has not heard: its keeper still renews what it holds
    slot = a._key("answer", batch.id)
    assert a.bus.client.pttl(slot) > HELD_MS
    await asyncio.sleep(0.35)
    assert a.bus.client.pttl(slot) > HELD_MS and batch.future.done()


# --- two decisions at once (M3) ---------------------------------------------------------------------------------------


async def test_of_two_decisions_taken_at_once_only_the_one_kept_is_ever_sealed(db, queues, monkeypatch):
    a, b = queues
    batch = _batch()
    await a.store(batch)
    sealed, raced = [], []
    masks = [HumanMask(0, "Ada Byron", "person")]

    def counted(queue):
        real = queue._sealer.seal_json

        def seal_json(value, aad):
            if "decision:" in aad:
                sealed.append(queue.instance)
                if queue is a and not raced:  # another tab, on another instance, decides at this very moment
                    raced.append(b.settle(batch.id, Decision(True)))
            return real(value, aad)

        monkeypatch.setattr(queue._sealer, "seal_json", seal_json)

    counted(a)
    counted(b)
    assert a.settle(batch.id, Decision(True, masks))
    assert raced == [False] and sealed == ["a"]  # the other one never reached Valkey, sealed or not
    decision = await a.decision(batch, 5)
    assert decision is not None and decision.masks == masks


# --- what the database keeps (R-12, M4, M5, I1, I2, the counter) ------------------------------------------------------


def test_settings_and_a_change_another_version_kept_are_refused_naming_fields_and_never_a_value(db, bus):
    identity_in(db)
    sealer = Sealer(ROOT_KEY, b"settings")
    _keep(db, "own", sealer.seal_json({"identity": {"emails": CANARY}}, f"{EMILY}:own"))
    with pytest.raises(ValueError, match=r"identity\.emails") as raised:
        PgSettings(db, SharedSettings(db, bus, MASTER), EMILY, ROOT_KEY).load()
    assert "emily" not in str(raised.value) and raised.value.__context__ is None
    changes = PgChanges(db, EMILY, ROOT_KEY)
    change = Change(Config(), "base", "command", None, ["a term"], ["terms"])
    data = queue_module._change_data(change) | {"config": {"identity": {"emails": CANARY}}}
    _waiting_change(db, change.id, sealer.seal_json(data, f"{EMILY}:change:{change.id}"))
    with pytest.raises(ValueError, match=r"identity\.emails") as raised:
        changes[change.id]
    assert "emily" not in str(raised.value) and raised.value.__context__ is None
    assert changes.get(change.id) is None and list(changes) == []


def test_a_change_copied_under_another_id_does_not_open(db):
    identity_in(db)
    changes = PgChanges(db, EMILY, ROOT_KEY)
    first = Change(Config(), "base-1", "start", None, ["terms: 1 value no longer masked"], ["terms"])
    second = Change(Config(), "base-2", "start", None, ["profile: from max to balanced"], ["profile"])
    changes[first.id] = first
    assert changes[first.id] == first  # in its own row it opens
    sealed = _read(db, "SELECT data FROM changes WHERE id = %s", first.id)[0][0]
    changes[second.id] = second  # the first is superseded: one waits
    _write(db, "UPDATE changes SET data = %s WHERE id = %s", sealed, second.id)
    assert changes.get(second.id) is None and list(changes) == []  # under another id it does not


def test_settings_whose_database_fails_say_so_by_the_type_of_the_error_alone(db, bus, settings):
    identity_in(db)
    closed = Database(settings.database_url, schema=db.schema, max_size=1)
    closed.close()
    store = PgSettings(closed, SharedSettings(closed, bus, MASTER), EMILY, ROOT_KEY)
    attempts = {
        "load": store.load,
        "reviewed": store.reviewed,
        "prepare": lambda: store.prepare(Config()),
        "save": lambda: store.save(Config()),
        "save_reviewed": lambda: store.save_reviewed(Config()),
    }
    for name, attempt in attempts.items():
        with pytest.raises(OSError, match="PoolClosed") as raised:
            attempt()
        said = str(raised.value)
        assert "latet-tests-only" not in said and "55432" not in said and raised.value.__context__ is None, name


def test_settings_sealed_with_another_key_do_not_open_and_are_said_once_by_identity_and_kind(db, bus, caplog):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    mine = PgSettings(db, shared, EMILY, ROOT_KEY)
    config = mine.prepare(Config(terms=[ORION]))
    mine.save(config)
    mine.save_reviewed(config)
    theirs = PgSettings(db, shared, EMILY, OTHER_KEY)
    caplog.set_level(logging.WARNING, logger="latet.server.space_settings")
    for _ in range(2):
        with pytest.raises(ValueError, match="do not open"):
            theirs.load()
        with pytest.raises(ValueError, match="do not open"):
            theirs.reviewed()  # never None: that would approve whatever the own part holds
    with pytest.raises(OSError, match="do not open") as refused:  # a store's failure, as SettingsStore says
        theirs.save(config)
    assert refused.value.__context__ is None and _kept(db, "own") is not None
    lines = [record.getMessage() for record in caplog.records if record.name == "latet.server.space_settings"]
    assert len(lines) == 2 and all(EMILY in line for line in lines) and "Orion" not in caplog.text
    _keep(db, "reviewed", Sealer(ROOT_KEY, b"settings").seal_json({"profile": "not-a-profile"}, f"{EMILY}:reviewed"))
    assert mine.reviewed() is None  # it opens, and is not valid settings (another version's): none, as on a computer


def _version(db) -> int | None:
    found = _read(db, "SELECT version FROM settings WHERE owner = %s AND kind = 'own'", EMILY)
    return found[0][0] if found else None


def test_every_write_of_the_own_part_is_counted_with_it_and_only_an_approved_snapshot_is_told(db, bus):
    identity_in(db)
    told = []
    store = PgSettings(db, SharedSettings(db, bus, MASTER), EMILY, ROOT_KEY, changed=told.append)
    store.load()
    store.save(store.prepare(Config(retention_days=30)))  # a save alone (what waits for the person): counted, not told
    assert told == [] and _counter(db, settings_owner(EMILY)) == 1
    config = store.prepare(Config(terms=[ORION]))
    store.save(config)
    store.save_reviewed(config)  # an apply: the own part's count, then the snapshot's, which is told
    assert told == [3] and _counter(db, settings_owner(EMILY)) == 3
    store.save_reviewed(config)  # the same snapshot again: nothing written, nothing counted or told
    assert told == [3] and _counter(db, settings_owner(EMILY)) == 3
    store.save(store.prepare(config.model_copy(update={"retention_days": 7})))  # what waits, again: counted
    applied = store.prepare(config)  # the approved settings applied again: the own part goes back to them
    store.save(applied)
    store.save_reviewed(applied)  # its snapshot is the one kept: counted, and told to nobody
    assert told == [3] and _counter(db, settings_owner(EMILY)) == 5


def test_an_own_part_equal_to_the_one_kept_is_not_written_and_neither_moves_its_version_nor_counts(db, bus):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    store, other = PgSettings(db, shared, EMILY, ROOT_KEY), PgSettings(db, shared, EMILY, ROOT_KEY)
    store.load()
    config = store.prepare(Config(terms=[ORION]))
    store.save(config)
    other_copy = other.load()
    kept = _kept(db, "own")
    store.save(store.prepare(config))  # the same settings saved again
    assert _kept(db, "own") == kept and _version(db) == 1 and _counter(db, settings_owner(EMILY)) == 1
    other.save(other_copy.model_copy(update={"retention_days": 30}))  # read before it, still current: it passes


def test_a_save_refused_as_stale_counts_and_writes_nothing_and_calls_stale_once_after_it(db, bus, caplog):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    writer = PgSettings(db, shared, EMILY, ROOT_KEY)
    read = []

    def stale():
        """Called once the save's transaction is over: the own part's row it held is free (a plain read would pass
        inside that transaction too), and the store can be read."""
        with db.transaction() as conn:
            conn.execute("SELECT version FROM settings WHERE owner = %s AND kind = 'own' FOR UPDATE NOWAIT", (EMILY,))
        read.append(reader.load())

    reader = PgSettings(db, shared, EMILY, ROOT_KEY, stale=stale)
    writer.load()
    copy = reader.load()
    writer.save(writer.prepare(Config(terms=[ORION])))
    with pytest.raises(OSError, match="another instance"):
        reader.save(copy.model_copy(update={"retention_days": 30}))
    assert len(read) == 1 and _terms(read[0]) == ["Orion"]  # called once, outside the save's transaction
    assert _counter(db, settings_owner(EMILY)) == 1 and _version(db) == 1 and _terms(writer.load()) == ["Orion"]

    def fails():
        raise RuntimeError("the re-apply failed")

    broken = PgSettings(db, shared, EMILY, ROOT_KEY, stale=fails)
    copy = broken.load()
    writer.load()
    writer.save(writer.prepare(Config(terms=[ORION], retention_days=7)))
    with pytest.raises(OSError, match="another instance") as refused:  # the save's error, as it was
        broken.save(copy.model_copy(update={"retention_days": 30}))
    assert refused.value.__context__ is None and "RuntimeError" in caplog.text


def test_the_count_and_the_write_of_the_own_part_are_one(db, bus, monkeypatch):
    identity_in(db)
    store = PgSettings(db, SharedSettings(db, bus, MASTER), EMILY, ROOT_KEY)
    store.load()
    store.save(store.prepare(Config(terms=[ORION])))
    kept = _kept(db, "own")

    def fails(conn):  # the count fails after the own part was written, in its transaction
        conn.execute("SELECT 1 / 0")
        return 0

    monkeypatch.setattr(store, "_count", fails)
    with pytest.raises(OSError, match="DivisionByZero"):
        store.save(store.prepare(Config(terms=[ORION], retention_days=30)))
    assert _kept(db, "own") == kept and _version(db) == 1 and _counter(db, settings_owner(EMILY)) == 1


def test_a_snapshot_that_cannot_be_kept_still_tells_the_own_part_written_for_it(db, bus, monkeypatch):
    identity_in(db)
    told = []
    store = PgSettings(db, SharedSettings(db, bus, MASTER), EMILY, ROOT_KEY, changed=told.append)
    store.load()
    store.save(store.prepare(Config(retention_days=30)))  # what waits for the person: counted, and told to nobody
    config = store.prepare(Config(terms=[ORION]))  # other settings are applied
    monkeypatch.setattr(space_settings, "SETTINGS_LIMIT", 500)  # their own part fits, their snapshot does not
    with pytest.raises(OSError, match="1000"):  # a person's snapshot: both parts' limits
        store.save_reviewed(config)
    assert told == [] and _counter(db, settings_owner(EMILY)) == 1  # what waits is still told to nobody
    store.save(config)
    with pytest.raises(OSError, match="1000"):
        store.save_reviewed(config)
    assert told == [2] and _counter(db, settings_owner(EMILY)) == 2  # the own part's own count, and no second one
    assert _kept(db, "reviewed") is None


def test_a_save_of_what_waits_leaves_the_count_ahead_of_an_instance_that_is_refused_until_it_loads(db, bus):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    told, refused = [], []
    mine = PgSettings(db, shared, EMILY, ROOT_KEY, changed=told.append, stale=lambda: refused.append(True))
    theirs = PgSettings(db, shared, EMILY, ROOT_KEY)
    mine.load()
    config = mine.prepare(Config(terms=[ORION]))
    mine.save(config)
    mine.save_reviewed(config)  # an apply here, told
    theirs.load()
    theirs.save(theirs.prepare(Config(terms=[ORION], retention_days=30)))  # what waits, saved on the other instance
    with pytest.raises(OSError, match="another instance"):
        mine.save(mine.prepare(config.model_copy(update={"retention_days": 7})))
    assert refused == [True] and (_counter(db, settings_owner(EMILY)) or 0) > told[-1]  # the check finds it behind
    mine.load()  # what the re-apply does (Task 12)
    mine.save(mine.prepare(config.model_copy(update={"retention_days": 7})))


def test_a_space_whose_approved_settings_do_not_open_does_not_start_and_the_log_names_no_setting(
    db, bus, tmp_path, caplog
):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    with _space(db, shared, tmp_path / "first") as first:
        first.update_config(_with(ORION))
    damaged = bytearray(_kept(db, "reviewed") or b"")
    damaged[-1] ^= 1  # one byte changed by hand
    _keep(db, "reviewed", bytes(damaged))
    caplog.set_level(logging.WARNING, logger="latet.server.space_settings")
    with pytest.raises(ValueError, match="do not open"), _space(db, shared, tmp_path / "second"):
        pass
    [line] = [record.getMessage() for record in caplog.records if record.name == "latet.server.space_settings"]
    assert EMILY in line and "approved" in line and "Orion" not in caplog.text


def test_a_save_made_on_a_copy_read_before_another_instances_save_is_refused(db, bus):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    a, b = PgSettings(db, shared, EMILY, ROOT_KEY), PgSettings(db, shared, EMILY, ROOT_KEY)
    a_copy, b_copy = a.load(), b.load()
    a.save(a_copy.model_copy(update={"terms": [ORION]}))
    with pytest.raises(OSError, match="another instance"):
        b.save(b_copy.model_copy(update={"retention_days": 30}))  # made on its copy, which has no Orion
    kept = PgSettings(db, shared, EMILY, ROOT_KEY).load()
    assert _terms(kept) == ["Orion"] and kept.retention_days == 14
    b.save(b.load().model_copy(update={"retention_days": 30}))  # read again, it goes
    kept = PgSettings(db, shared, EMILY, ROOT_KEY).load()
    assert _terms(kept) == ["Orion"] and kept.retention_days == 30
    with pytest.raises(OSError, match="another instance"):
        a.save(a_copy)  # now a's copy is the old one


def test_a_runtime_whose_save_was_refused_runs_what_postgresql_holds_once_it_reloads(db, bus, tmp_path):
    identity_in(db)
    shared = SharedSettings(db, bus, MASTER)
    with _space(db, shared, tmp_path / "one") as one, _space(db, shared, tmp_path / "two") as two:
        one.update_config(_with(ORION))  # in a dashboard served by one instance
        with pytest.raises(OSError, match="another instance"):  # another tab, served by an instance that has not heard
            two.update_config(lambda current: current.model_copy(update={"retention_days": 30}))
        kept = PgSettings(db, shared, EMILY, ROOT_KEY).load()
        assert _terms(kept) == ["Orion"] and kept.retention_days == 14
        two.apply_config(two.settings.load(), save=False)  # what an instance does when it hears (Task 12)
        assert _terms(two.config) == ["Orion"]
        two.update_config(lambda current: current.model_copy(update={"retention_days": 30}))
        kept = PgSettings(db, shared, EMILY, ROOT_KEY).load()
        assert _terms(kept) == ["Orion"] and kept.retention_days == 30


# --- another identity (R-26) ------------------------------------------------------------------------------------------


async def test_another_identity_sees_nothing_of_an_identitys_queue_changes_and_settings(db, queues, buses):
    a, _ = queues
    identity_in(db, DANIEL, "Daniel Okafor")
    batch = _batch(polled=True)
    batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
    await a.store(batch)
    answered = _batch(polled=True)
    await a.store(answered)
    a.settle(answered.id, Decision(True))
    a.give(answered.id, {"items": [{"text": "masked", "count": 1}], "degraded": []})
    a.changes["change-1"] = Change(Config(terms=[ORION]), "base", "command", None, ["a term"], ["terms"])
    a.settled["change-0"] = "approved"
    shared = SharedSettings(db, a.bus, MASTER)
    mine = PgSettings(db, shared, EMILY, ROOT_KEY)
    config = mine.prepare(Config(terms=[ORION]))
    mine.save(config)
    mine.save_reviewed(config)
    for key in (
        OTHER_KEY,
        ROOT_KEY,
    ):  # Daniel's own root key, as on a server; or Emily's: the identity alone parts them
        theirs = ValkeyApprovals(buses[1], db, DANIEL, key, instance="b")
        assert theirs.get(batch.id) is None and theirs.waiting() == []
        assert theirs.image(batch.id, 0, sent=False) is None
        with pytest.raises(KeyError):
            await theirs.answer(answered.id, 0.05)
        assert dict(theirs.changes) == {} and dict(theirs.settled) == {}
        others = PgSettings(db, shared, DANIEL, key)
        assert others.reviewed() is None and _terms(others.load()) == []
        theirs.close()
    assert await a.answer(answered.id, 5) == {"items": [{"text": "masked", "count": 1}], "degraded": []}
