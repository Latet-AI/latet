"""Valkey for a Latet server: what one instance publishes the others hear, and only that: a message nobody signed, or
signed for another channel, or too old or too new, or heard already, is dropped; a job runs on one instance at a time and
its lock ends by itself; rate limits count in a window; short-lived keys end."""

import asyncio
import contextlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import redis

from latet.server import bus as bus_module
from latet.server.app import CHANNELS
from latet.server.bus import Bus

from .conftest import BUS_KEY, _valkey_client, drop_listeners

pytestmark = pytest.mark.server


@contextlib.asynccontextmanager
async def _ear(bus, *channels, dropped=False):
    """An instance's ear, in small: what it hears is collected in a list while the context lasts. With ``dropped``, the
    connection is to be dropped meanwhile: the ear then ends with a ConnectionError, which is expected."""
    ready, heard = asyncio.Event(), []

    async def listen():
        async with contextlib.aclosing(bus.listen(*channels, ready=ready)) as messages:
            async for channel, message in messages:
                heard.append((channel, message))

    task = asyncio.create_task(listen())
    await asyncio.wait_for(ready.wait(), 5)
    try:
        yield heard, task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, *((redis.ConnectionError,) if dropped else ())):
            await task


async def _until(heard, task, count):
    """Until the ear has heard ``count`` messages. If the ear ended meanwhile, what ended it is raised."""
    deadline = time.monotonic() + 5
    while len(heard) < count:
        if task.done():
            task.result()
            pytest.fail("the ear ended")
        assert time.monotonic() < deadline, f"{len(heard)} of {count} messages heard"
        await asyncio.sleep(0.01)


def _spy(bus, channel):
    """A plain subscription, such as someone with Valkey's password holds: what it hears is the wire itself."""
    spy = bus.client.pubsub()
    spy.subscribe(bus.key(channel))
    assert spy.get_message(timeout=5)["type"] == "subscribe"
    return spy


def _wire_of(spy) -> bytes:
    message = spy.get_message(timeout=5)
    assert message is not None and message["type"] == "message"
    return message["data"]


def _keys(bus, *names):
    pipe = bus.client.pipeline()
    for name in names:
        pipe.set(bus.key(name), b"1")
    pipe.execute()


async def test_what_one_instance_publishes_another_hears_in_order(buses):
    a, b = buses
    ready, heard = asyncio.Event(), []

    async def ear():
        async with contextlib.aclosing(b.listen("identity:*", "shared", ready=ready)) as messages:
            async for channel, message in messages:
                heard.append((channel, message))
                if len(heard) == 3:
                    return

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    a.publish("identity:u_emilycarte", {"kind": "settings"})
    a.publish("other", {"heard": False})  # nobody listens to it
    a.publish("shared", {"changed": True})
    a.publish("identity:u_danielokaf", {"kind": "entity", "id": "e1"})
    await asyncio.wait_for(task, 5)
    assert heard == [
        ("identity:u_emilycarte", {"kind": "settings"}),
        ("shared", {"changed": True}),
        ("identity:u_danielokaf", {"kind": "entity", "id": "e1"}),
    ]


async def test_a_listener_whose_connection_drops_says_so(bus):
    ready = asyncio.Event()

    async def ear():
        async with contextlib.aclosing(bus.listen("shared", ready=ready)) as messages:
            async for _ in messages:
                pass

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    assert drop_listeners(bus) == 1
    with pytest.raises(redis.ConnectionError):
        await asyncio.wait_for(task, 10)


def test_a_job_runs_on_one_instance_at_a_time_and_its_lock_ends_by_itself(buses):
    a, b = buses
    with a.lock("history-prune", 60) as mine:
        assert mine
        with b.hold("history-prune", 60) as theirs:
            assert not theirs
    stopped = b.lock("audit-prune", 0.1)
    assert stopped.__enter__()  # an instance that then stops while it holds the lock
    time.sleep(0.3)
    with a.lock("audit-prune", 60) as after:
        assert after  # it ended by itself
        stopped.__exit__(None, None, None)  # the stopped one's release takes nothing of the new holder's
        with b.lock("audit-prune", 60) as again:
            assert not again
    assert a.get("lock:audit-prune") is None


def test_rate_limits_count_in_a_window(bus):
    assert all(bus.allow("enrol:10.0.0.5", 3, 0.5) for _ in range(3))
    assert not bus.allow("enrol:10.0.0.5", 3, 0.5) and bus.allow("enrol:10.0.0.6", 3, 0.5)
    time.sleep(0.6)
    assert bus.allow("enrol:10.0.0.5", 3, 0.5)


def test_a_slot_is_taken_in_one_step_and_a_good_credential_gives_it_back(bus):
    """The limiter the gate and the administration share (Task 15's ruling): ``take`` counts and answers whether the
    count is within the limit, in one step of Valkey's, the window's end given at its first slot; ``give_back`` returns
    a slot, never below none, and makes nothing where nothing was taken."""
    key = "lookup-miss:10.0.0.5"
    assert [bus.take(key, 2, 60) for _ in range(3)] == [True, True, False]
    assert 58_000 < bus.client.pttl(bus.key(bus.rate_key(key))) <= 60_000
    bus.give_back(key)
    assert bus.get(bus.rate_key(key)) == b"2"  # the one past the limit was counted too
    for _ in range(5):
        bus.give_back(key)
    bus.give_back("lookup-miss:10.0.0.9")
    assert bus.get(bus.rate_key(key)) == b"0" and bus.get(bus.rate_key("lookup-miss:10.0.0.9")) is None  # none below
    assert bus.take(key, 2, 60) and bus.take("lookup-miss:10.0.0.6", 1, 60)  # another address's own count
    bus.set(bus.rate_key("route-miss:10.0.0.7"), b"not a count", 60)  # written by hand
    with pytest.raises(redis.ResponseError):
        bus.take("route-miss:10.0.0.7", 3, 60)  # fails, and the gate refuses (a store that does not answer)
    bus.give_back("route-miss:10.0.0.7")
    assert bus.get(bus.rate_key("route-miss:10.0.0.7")) == b"not a count"
    hand = bus.key(bus.rate_key("route-miss:10.0.0.8"))
    bus.client.set(hand, b"5")  # no lifetime, written by hand: it is given one
    assert not bus.take("route-miss:10.0.0.8", 3, 60)
    assert 0 < bus.client.pttl(hand) <= 60_000


def test_slots_taken_at_once_are_counted_each_once(bus):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(16) as pool:
        taken = list(pool.map(lambda _: bus.take("route-miss:10.0.0.5", 10, 60), range(64)))
    assert taken.count(True) == 10 and bus.get(bus.rate_key("route-miss:10.0.0.5")) == b"64"


def test_a_slot_past_its_limit_says_how_long_its_window_has_left(bus):
    """What an instance remembers a bucket past its limit for (``machines.Limits``): its window's end, as Valkey has
    it."""
    assert bus.slot("lookup-miss:10.0.0.5", 1, 60) is None
    left = bus.slot("lookup-miss:10.0.0.5", 1, 60)
    assert left is not None and 58 < left <= 60


def test_the_names_of_the_counts_hold_no_address_and_no_one(bus, prefix, settings):
    """S7 (area 6, M3): a count's name is its kind and a keyed hash of whom it counts, the same on every instance (the
    bus's key), so that whoever reads Valkey's keys learns no client's address and no identity from them."""
    for key in ("route-miss:10.0.0.5", "enrol:2001:db8:1:2::/64", "login-code:u_emilycarte", "admin-refusals"):
        assert bus.take(key, 5, 60) and bus.allow(key, 5, 60)
    names = b"\n".join(bus.client.scan_iter(match=prefix + "*", count=1000))
    for who in (b"10.0.0.5", b"2001:db8", b"u_emilycarte"):
        assert who not in names
    assert bus.key("rate:admin-refusals").encode() in names  # a count of its own kind alone
    other = Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY)  # another instance: the same names
    try:
        assert other.rate_key("route-miss:10.0.0.5") == bus.rate_key("route-miss:10.0.0.5")
        assert other.get(other.rate_key("route-miss:10.0.0.5")) == b"2"
    finally:
        other.close()


def test_short_lived_keys_end_and_valkey_answers(bus):
    bus.set("cache:x", b"1", 0.2)
    assert bus.get("cache:x") == b"1"
    time.sleep(0.3)
    assert bus.get("cache:x") is None
    bus.set("cache:y", b"2")
    bus.delete("cache:y")
    assert bus.get("cache:y") is None and bus.ping() and bus.key("x") == bus.prefix + "x"


# (d) A lifetime of nothing ends the key, and never keeps it for ever.
def test_a_lifetime_of_nothing_ends_the_key_at_once_and_only_none_keeps_it_for_ever(bus):
    for seconds in (0, 0.0, -5, 0.0004):
        bus.set("cache:z", b"1", seconds)
        time.sleep(0.02)
        assert bus.get("cache:z") is None and not bus.client.exists(bus.key("cache:z")), seconds
    bus.set("cache:forever", b"1")
    assert bus.client.pttl(bus.key("cache:forever")) == -1  # no end: the one lifetime that is for ever
    bus.set("cache:soon", b"1", 30)
    assert 0 < bus.client.pttl(bus.key("cache:soon")) <= 30_000


async def test_a_message_nobody_signed_or_signed_with_another_key_is_dropped_and_counted(settings, buses):
    a, b = buses
    other = Bus(settings.valkey_url, prefix=a.prefix, key=b"\x09" * 32)  # someone with Valkey's password, not the key
    ready, heard = asyncio.Event(), []

    async def ear():
        async with contextlib.aclosing(b.listen("shared", ready=ready)) as messages:
            async for _, message in messages:
                heard.append(message)
                if message.get("signed"):
                    return

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    a.client.publish(a.key("shared"), '{"changed": true}')  # written by hand: no signature
    other.publish("shared", {"forged": True})  # signed with another key
    a.publish("shared", {"signed": True})
    await asyncio.wait_for(task, 5)
    other.close()
    assert heard == [{"signed": True}] and b.rejected == 2 and a.rejected == 0


# (a) Nothing of a message is parsed before its signature is verified.
async def test_what_cannot_be_read_as_a_message_does_not_end_the_ear(buses):
    a, b = buses
    async with _ear(b, "identity:*", "shared") as (heard, task):
        a.client.publish(a.key("shared"), b"[" * 200_000)  # a reader that parses first meets RecursionError
        a.client.publish(a.key("shared"), b"0" * 64 + b"." + b"[" * 200_000)
        a.client.publish(a.key("identity:").encode() + b"\xff\xfe", b"junk")  # a channel whose name is no text
        a.client.publish(a.key("identity:u_emilycarte"), b'{"m": "{}", "s": "00"}')  # the first wire, which was JSON
        a.publish("shared", {"signed": True})
        await _until(heard, task, 1)
    assert heard == [("shared", {"signed": True})] and b.rejected == 4


# (b) A signed message is fresh, and heard once.
async def test_a_message_published_again_by_hand_is_dropped_and_counted(buses, caplog):
    a, b = buses
    spy = _spy(a, "shared")
    try:
        with caplog.at_level(logging.WARNING, logger="latet.server.bus"):
            async with _ear(b, "shared") as (heard, task):
                a.publish("shared", {"decided": "batch-1"})
                wire = _wire_of(spy)
                a.client.publish(a.key("shared"), wire)  # the very bytes, again
                a.client.publish(a.key("shared"), wire)
                a.publish("shared", {"after": True})
                await _until(heard, task, 2)
    finally:
        spy.close()
    assert heard == [("shared", {"decided": "batch-1"}), ("shared", {"after": True})]
    assert b.rejected == 2 and "repeated" in caplog.text


# Important 1. The attacker kills the connection of the ear, and publishes again what he recorded once it listens anew.
async def test_a_message_published_again_after_the_connection_dropped_is_dropped_all_the_same(buses, caplog):
    a, b = buses
    spy = _spy(a, "shared")
    try:
        with caplog.at_level(logging.WARNING, logger="latet.server.bus"):
            async with _ear(b, "shared", dropped=True) as (first, one):
                a.publish("shared", {"decided": "batch-1"})
                wire = _wire_of(spy)
                await _until(first, one, 1)
                assert drop_listeners(b) == 1  # as a restart of Valkey would, or someone who has its password
                with pytest.raises(redis.ConnectionError):
                    await asyncio.wait_for(one, 10)
            async with _ear(b, "shared") as (second, two):  # the instance's ear listens again, as Task 12's does
                a.client.publish(a.key("shared"), wire)  # the very bytes, by hand
                a.publish("shared", {"after": True})
                await _until(second, two, 1)
    finally:
        spy.close()
    assert first == [("shared", {"decided": "batch-1"})] and second == [("shared", {"after": True})]
    assert b.rejected == 1 and "repeated" in caplog.text


async def test_a_message_that_was_captured_does_not_become_new_with_another_id_or_time(buses):
    a, b = buses
    spy = _spy(a, "shared")
    try:
        async with _ear(b, "shared") as (heard, task):
            a.publish("shared", {"decided": "batch-1"})
            signature, _, text = _wire_of(spy).partition(b".")
            body = json.loads(text)
            for changed in ({**body, "i": "0" * 32}, {**body, "t": body["t"] + 5}):
                a.client.publish(
                    a.key("shared"), signature + b"." + json.dumps(changed, separators=(",", ":")).encode()
                )
            a.publish("shared", {"after": True})
            await _until(heard, task, 2)
    finally:
        spy.close()
    assert heard == [("shared", {"decided": "batch-1"}), ("shared", {"after": True})] and b.rejected == 2


async def test_a_message_stamped_more_than_a_minute_from_this_computers_time_is_dropped_and_counted(settings, buses):
    a, b = buses
    senders = {
        offset: Bus(settings.valkey_url, prefix=a.prefix, key=BUS_KEY, clock=lambda offset=offset: time.time() + offset)
        for offset in (-61, 61, -30, 30)  # seconds from the time of the computer that hears
    }
    try:
        async with _ear(b, "shared") as (heard, task):
            for offset, sender in senders.items():
                sender.publish("shared", {"offset": offset})
            a.publish("shared", {"offset": 0})
            await _until(heard, task, 3)
    finally:
        for sender in senders.values():
            sender.close()
    assert [message["offset"] for _, message in heard] == [-30, 30, 0] and b.rejected == 2


async def test_a_message_copied_to_another_channel_is_dropped_and_counted(buses):
    a, b = buses
    spy = _spy(a, "identity:u_emilycarte")
    try:
        async with _ear(b, "identity:*") as (heard, task):
            a.publish("identity:u_emilycarte", {"kind": "settings"})
            a.client.publish(a.key("identity:u_danielokaf"), _wire_of(spy))  # Emily's message, on Daniel's channel
            a.publish("identity:u_danielokaf", {"kind": "entity"})
            await _until(heard, task, 2)
    finally:
        spy.close()
    assert heard == [("identity:u_emilycarte", {"kind": "settings"}), ("identity:u_danielokaf", {"kind": "entity"})]
    assert b.rejected == 1


async def test_two_calls_on_one_bus_each_hear_what_is_published_and_one_call_hears_it_once(buses):
    a, b = buses
    async with _ear(b, "shared") as (first, one), _ear(b, "shared", "identity:*") as (second, two):
        a.publish("shared", {"n": 1})
        await _until(first, one, 1)
        await _until(second, two, 1)
    assert first == second == [("shared", {"n": 1})] and b.rejected == 0  # an ear of its own for each
    async with _ear(b, "shared", "sh*") as (heard, task):  # which Valkey tells twice: the second is a repeat
        a.publish("shared", {"n": 2})
        a.publish("shared", {"n": 3})
        await _until(heard, task, 2)
    assert heard == [("shared", {"n": 2}), ("shared", {"n": 3})] and b.rejected == 2


async def test_a_call_that_begins_while_another_runs_hears_what_is_published_after_it_began(buses):
    a, b = buses
    async with _ear(b, "shared") as (first, one):
        a.publish("shared", {"n": 1})
        await _until(first, one, 1)
        async with _ear(b, "shared") as (second, two):  # n=1 was heard by the call that runs: it is not for this one
            a.publish("shared", {"n": 2})  # which both hear, each on its own
            await _until(first, one, 2)
            await _until(second, two, 1)
    assert first == [("shared", {"n": 1}), ("shared", {"n": 2})] and second == [("shared", {"n": 2})]
    assert b.rejected == 0


# Minor 1. A call waits for messages as long as it takes: the time it has is for the subscription.
async def test_a_call_waits_for_messages_for_longer_than_the_time_it_has_to_subscribe(buses, monkeypatch):
    a, b = buses
    monkeypatch.setattr(bus_module, "SOCKET_TIMEOUT", 0.3)
    async with _ear(b, "shared") as (heard, task):
        await asyncio.sleep(1.0)  # three times as long
        assert not task.done()
        a.publish("shared", {"late": True})
        await _until(heard, task, 1)
    assert heard == [("shared", {"late": True})]


# (g) A flood of forged messages is counted, and says one line.
async def test_forged_messages_are_all_counted_and_say_one_line_that_holds_nothing_of_them(buses, caplog):
    a, b = buses
    with caplog.at_level(logging.WARNING, logger="latet.server.bus"):
        async with _ear(b, "identity:*", "shared") as (heard, task):
            for number in range(5):
                a.client.publish(a.key("identity:SENSITIVE-CHANNEL"), f"SENSITIVE-BODY-{number}".encode())
            a.publish("identity:u_emilycarte", {"signed": True})
            await _until(heard, task, 1)
    lines = [record for record in caplog.records if record.name == "latet.server.bus"]
    assert b.rejected == 5 and len(lines) == 1
    assert "identity" in lines[0].getMessage() and "signature" in lines[0].getMessage()
    assert "SENSITIVE" not in caplog.text  # nothing of the channel's name, nor of the message


# Task 10's I4. A message that would fill a subscriber's buffer, and so cut every instance's ear, never reaches Valkey.
async def test_a_message_heavier_than_the_buss_limit_is_refused_before_valkey_gets_it(buses, monkeypatch):
    a, b = buses
    monkeypatch.setattr(bus_module, "MESSAGE_LIMIT", 1000)
    async with _ear(b, "approvals:*") as (heard, task):
        with pytest.raises(ValueError, match="1000 bytes at most") as refused:
            a.publish("approvals:u_emilycarte", {"feed": "SENSITIVE-" + "x" * 1000})
        assert "SENSITIVE" not in str(refused.value)
        a.publish("approvals:u_emilycarte", {"feed": "light"})
        await _until(heard, task, 1)
    assert heard == [("approvals:u_emilycarte", {"feed": "light"})] and b.rejected == 0


def test_a_purge_of_nothing_is_refused_and_empties_nothing(bus):
    _keys(bus, "aws:u_emilycarte:s1", "conv:u_danielokaf:1", "lock:history-prune")
    with pytest.raises(ValueError, match="needs a prefix"):  # every key under the bus's prefix is what "" would reach
        bus.purge("")
    assert bus.get("aws:u_emilycarte:s1") == b"1" and bus.get("conv:u_danielokaf:1") == b"1"
    assert bus.get("lock:history-prune") == b"1"


def test_the_keys_under_a_prefix_are_purged(bus):
    bus.set("aws:u_emilycarte:s1", b"1")
    bus.set("aws:u_emilycarte:s2", b"2")
    bus.set("aws:u_danielokaf:s1", b"3")
    assert bus.purge("aws:u_emilycarte:") == 2 and bus.purge("aws:u_emilycarte:") == 0
    assert bus.get("aws:u_emilycarte:s1") is None and bus.get("aws:u_danielokaf:s1") == b"3"


NEIGHBOURS = ["aws:u_emilycarte:s1", "aws:u_danielokaf:s1", "aws:u_x:s1", "aws:u_e:s1", "aws:u_Z:s1"]


# (e) A prefix is taken as it is written, and the keys go in batches.
@pytest.mark.parametrize("name", ["aws:u_*:", "aws:u_?:", "aws:u_[ed]:", "aws:u_\\x:", "aws:u_*[?]\\:"])
def test_a_prefix_with_a_character_a_pattern_reads_reaches_nothing_but_its_own_keys(bus, name):
    _keys(bus, f"{name}s1", *NEIGHBOURS)
    assert bus.purge(name) == 1
    assert bus.get(f"{name}s1") is None
    assert all(bus.get(neighbour) == b"1" for neighbour in NEIGHBOURS)


def test_a_bus_prefix_with_a_character_a_pattern_reads_reaches_nothing_but_its_own_keys(settings, prefix):
    odd = Bus(settings.valkey_url, prefix=prefix + "x*[a]?:", key=BUS_KEY)
    neighbour = Bus(settings.valkey_url, prefix=prefix + "xza1:", key=BUS_KEY)  # which "x*[a]?:" would match
    try:
        odd.set("aws:s1", b"1")
        neighbour.set("aws:s1", b"2")
        assert odd.purge("aws:") == 1
        assert odd.get("aws:s1") is None and neighbour.get("aws:s1") == b"2"
    finally:
        odd.close()
        neighbour.close()


async def test_a_bus_prefix_with_a_character_a_pattern_reads_hears_nothing_but_its_own_channels(settings, prefix):
    """A pattern a call listens to is the bus's prefix taken as it is written: another prefix it would match as a
    pattern (another server sharing the Valkey) is not heard."""
    odd = Bus(settings.valkey_url, prefix=prefix + "x*[a]?:", key=BUS_KEY)
    neighbour = Bus(settings.valkey_url, prefix=prefix + "xza1:", key=BUS_KEY)  # which "x*[a]?:" would match
    try:
        async with _ear(odd, "identity:*") as (heard, task):
            neighbour.publish("identity:u_emilycarte", {"n": 1})
            odd.publish("identity:u_emilycarte", {"n": 2})
            await _until(heard, task, 1)
            await asyncio.sleep(0.3)  # time for the neighbour's to come, if it were heard
        assert heard == [("identity:u_emilycarte", {"n": 2})] and odd.rejected == 0
    finally:
        odd.close()
        neighbour.close()


def _held_for(bus, seconds: float, name: str) -> str:
    """A call that holds its connection ``seconds`` (a blocking pop on a list nobody fills): what it answered."""
    return repr(bus.client.blpop(bus.key(name), timeout=seconds))


def test_more_calls_at_once_than_the_connections_kept_wait_their_turn_and_are_answered(bus):
    """Task 16 measured 89 of 400 requests answered 500 at 200 identities: redis-py's pool of 100 connections raised
    MaxConnectionsError at the 101st call at once. A call past the connections kept waits for one."""
    calls = bus_module.VALKEY_CONNECTIONS + 10
    with ThreadPoolExecutor(calls) as pool:
        answers = list(pool.map(lambda n: _held_for(bus, 0.5, f"empty:{n}"), range(calls)))
    assert answers == ["None"] * calls


def test_a_call_that_waits_past_the_bound_for_a_connection_fails_as_a_store_that_does_not_answer(
    settings, prefix, monkeypatch
):
    """With a few connections kept: calls past them wait their turn, and one that waits longer than ``POOL_WAIT`` fails
    as a Valkey that does not answer fails a call (a ConnectionError, which the server answers 503), in words that name
    no address."""
    monkeypatch.setattr(bus_module, "POOL_WAIT", 1.0)
    turns = Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY, connections=2)
    try:
        with ThreadPoolExecutor(6) as pool:
            started = time.monotonic()
            answers = list(pool.map(lambda n: _held_for(turns, 0.2, f"empty:{n}"), range(6)))
        assert answers == ["None"] * 6 and time.monotonic() - started >= 0.55  # two at a time, three turns
    finally:
        turns.close()
    monkeypatch.setattr(bus_module, "POOL_WAIT", 0.3)
    few = Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY, connections=2)
    try:
        with ThreadPoolExecutor(2) as pool:
            holding = [pool.submit(_held_for, few, 1.5, f"held:{n}") for n in range(2)]
            time.sleep(0.2)  # both connections are held now
            with pytest.raises(redis.ConnectionError) as refused:
                few.get("x")
            assert not isinstance(refused.value, redis.exceptions.MaxConnectionsError)
            assert "latet-tests-only" not in str(refused.value) and "127.0.0.1" not in str(refused.value)
            assert [call.result(10) for call in holding] == ["None", "None"]
        assert few.get("x") is None  # the connections are free again
    finally:
        few.close()


def test_the_keys_of_a_big_purge_go_in_batches(bus, monkeypatch):
    _keys(bus, *(f"conv:u_emilycarte:{number}" for number in range(1_200)), "conv:u_danielokaf:1")
    sizes, delete = [], bus.client.delete

    def deleting(*keys):
        sizes.append(len(keys))
        return delete(*keys)

    monkeypatch.setattr(bus.client, "delete", deleting)
    assert bus.purge("conv:u_emilycarte:") == 1_200  # what was deleted: SCAN may give a key twice, DEL counts it once
    assert sum(sizes) >= 1_200 and len(sizes) >= 3 and max(sizes) <= 500
    assert bus.get("conv:u_danielokaf:1") == b"1" and bus.purge("conv:u_emilycarte:") == 0


# T17.5: the literal production recipe. Only the owned username/password and the
# complete namespace prefix are substituted in the service test below.
_DOCUMENTED_VALKEY_ACL = [
    "ACL",
    "SETUSER",
    "latet",
    "on",
    ">password",
    "~latet:*",
    "resetchannels",
    "&latet:identity:*",
    "&latet:approvals:*",
    "&latet:feed:*",
    "&latet:shared",
    "&latet:identities",
    "&latet:signins",
    "&latet:beat",
    "+@all",
    "-@dangerous",
    "-monitor",
    "-config",
    "-keys",
    "-debug",
]


def _documented_acl_rules(prefix, password):
    rules = []
    for token in _DOCUMENTED_VALKEY_ACL[3:]:
        if token == ">password":
            token = ">" + password
        elif token.startswith(("~latet:", "&latet:")):
            token = token.replace("latet:", prefix, 1)
        rules.append(token)
    return rules


def test_the_documented_valkey_acl_matches_the_servers_channels_and_prefix():
    """A new ear channel needs the same literal ACL pattern; no broad channel grant."""
    assert Bus.__init__.__kwdefaults__["prefix"] == "latet:"
    assert _DOCUMENTED_VALKEY_ACL[:7] == ["ACL", "SETUSER", "latet", "on", ">password", "~latet:*", "resetchannels"]
    assert _DOCUMENTED_VALKEY_ACL[7:14] == [
        "&latet:identity:*",
        "&latet:approvals:*",
        "&latet:feed:*",
        "&latet:shared",
        "&latet:identities",
        "&latet:signins",
        "&latet:beat",
    ]
    assert _DOCUMENTED_VALKEY_ACL[14:] == ["+@all", "-@dangerous", "-monitor", "-config", "-keys", "-debug"]
    channels = [token.removeprefix("&latet:") for token in _DOCUMENTED_VALKEY_ACL if token.startswith("&")]
    assert len(channels) == len(CHANNELS) == 7 and set(channels) == set(CHANNELS)
    assert _documented_acl_rules("latet:", "password") == _DOCUMENTED_VALKEY_ACL[3:]


async def test_the_documented_valkey_acl_runs_the_bus_and_refuses_admin_commands(settings):
    """Real test Valkey, one owned user/prefix: literal ear patterns, Lua and scoped purge."""
    username = "latet_t_" + uuid4().hex
    prefix, password = username + ":", "test-only-" + uuid4().hex
    address = urlsplit(bus_module.redis_url(settings.valkey_url))
    assert address.hostname in {"127.0.0.1", "::1"} and not address.query and not address.fragment
    host = f"[{address.hostname}]" if ":" in address.hostname else address.hostname
    url = address._replace(netloc=f"{username}:{password}@{host}:{address.port or 6379}").geturl()

    with contextlib.ExitStack() as cleanup:
        admin = _valkey_client(settings.valkey_url)
        cleanup.callback(admin.close)
        assert admin.acl_getuser(username) is None
        assert next(admin.scan_iter(match=prefix + "*", count=100), None) is None

        def remove_user():
            # Also runs if SETUSER's response is lost after the server created it.
            assert admin.acl_deluser(username) in (0, 1)
            assert admin.acl_getuser(username) is None

        def remove_keys():
            keys = list(admin.scan_iter(match=prefix + "*", count=100))
            if keys:
                admin.delete(*keys)
            assert next(admin.scan_iter(match=prefix + "*", count=100), None) is None

        cleanup.callback(remove_user)
        cleanup.callback(remove_keys)
        assert admin.execute_command("ACL SETUSER", username, *_documented_acl_rules(prefix, password))
        a = Bus(url, prefix=prefix, key=BUS_KEY)
        cleanup.callback(a.close)
        b = Bus(url, prefix=prefix, key=BUS_KEY)
        cleanup.callback(b.close)
        assert a.client.connection_pool.connection_kwargs["username"] == username
        assert b.client.connection_pool.connection_kwargs["username"] == username
        assert a.ping() and b.ping()

        ready, heard = asyncio.Event(), []

        async def ear():
            async with contextlib.aclosing(b.listen(*CHANNELS, ready=ready)) as messages:
                async for item in messages:
                    heard.append(item)

        listener = asyncio.create_task(ear())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            expected = [
                (channel[:-1] + "u_acl" if channel.endswith("*") else channel, {"kind": "acl", "number": n})
                for n, channel in enumerate(CHANNELS)
            ]
            for channel, message in expected:
                a.publish(channel, message)
            await _until(heard, listener, len(expected))
            assert heard == expected and b.rejected == 0
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(listener, 5)

        with a.lock("acl-history-prune", 30) as mine:
            assert mine
            with b.lock("acl-history-prune", 30) as other:
                assert not other
        with b.lock("acl-history-prune", 30) as after_release:
            assert after_release
        assert a.get("lock:acl-history-prune") is None

        assert [a.allow("acl-window:synthetic", 2, 30) for _ in range(3)] == [True, True, False]
        assert [a.take("acl-credential:synthetic", 1, 30) for _ in range(2)] == [True, False]
        a.give_back("acl-credential:synthetic")
        a.give_back("acl-credential:synthetic")
        assert a.get(a.rate_key("acl-credential:synthetic")) == b"0"
        assert a.take("acl-credential:synthetic", 1, 30)

        a.set("aws:u_acl:s1", b"one", 30)
        a.set("aws:u_acl:s2", b"two", 30)
        a.set("aws:u_other:s1", b"kept", 30)
        assert a.purge("aws:u_acl:") == 2 and a.purge("aws:u_acl:") == 0
        assert a.get("aws:u_acl:s1") is None and a.get("aws:u_other:s1") == b"kept"

        with pytest.raises(redis.exceptions.NoPermissionError):
            a.publish("outside-the-seven", {"synthetic": True})
        # MONITOR is last: if wrongly allowed, close its connection without reading a stream.
        for command in (("CONFIG GET", "maxmemory-policy"), ("KEYS", prefix + "*"), ("MONITOR",)):
            with pytest.raises(redis.exceptions.NoPermissionError):
                a.client.execute_command(*command)
