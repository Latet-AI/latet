"""A Latet server's bus where no Valkey is needed: a message is taken only when its signature, over the bytes as they came,
verifies for its channel, and then only if it is fresh and new; one that is not is dropped, counted and said in the log
without a word of it; the key is 32 bytes; a Valkey that does not answer fails a call after a short time. The tests that
need Valkey are in tests/server/test_bus.py."""

import asyncio
import contextlib
import json
import logging
import socket
import threading
import time
import traceback

import pytest
import redis
from hypothesis import assume, given
from hypothesis import strategies as st

from latet.server import bus as bus_module
from latet.server.bus import MESSAGE_WINDOW, Bus, _literal, _Refused, _Taken

HERE = "127.0.0.1"
ADDRESS = f"redis://:v4lkey-pw@{HERE}:1/0"  # nothing listens there, and nothing here connects
KEY = b"\x07" * 32
CHANNEL = b"latet:identity:u_emilycarte"
START = 1_800_000_000.0
ID = "0" * 32  # an id that is well made


class Clock:
    """A clock the test moves."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mine(clock):
    found = Bus(ADDRESS, prefix="latet:", key=KEY, clock=clock)
    yield found
    found.close()


def _refusal(bus, channel: bytes, raw, taken: _Taken | None = None) -> str:
    """The check that a message fails, after checking that it does fail."""
    with pytest.raises(_Refused) as refused:
        bus._open(channel, raw, taken if taken is not None else _Taken())
    assert refused.value.why in bus_module._WHY  # the log has words for it
    return refused.value.why


def _signed_text(bus, text: bytes, channel: bytes = CHANNEL) -> bytes:
    """What an instance with the key would put on the wire for a text of its own, however odd."""
    return bus._signature(channel, text) + b"." + text


def test_a_message_is_taken_when_it_is_signed_for_its_channel_fresh_and_new(mine):
    wire = mine._wire(CHANNEL, {"id": "e1", "kind": "entity"})
    signature = wire.partition(b".")[0]
    assert len(signature) == 64 and signature == signature.lower()  # HMAC-SHA256 in hex
    assert mine._open(CHANNEL, wire, _Taken()) == ("identity:u_emilycarte", {"id": "e1", "kind": "entity"})


def test_every_message_has_an_id_and_a_time_of_its_own_under_the_signature(mine):
    first, second = mine._wire(CHANNEL, {"a": 1}), mine._wire(CHANNEL, {"a": 1})
    assert first != second  # the same message, published twice, is two wires
    body = json.loads(first.partition(b".")[2])
    assert sorted(body) == ["i", "m", "t"] and body["m"] == {"a": 1} and body["t"] == int(START * 1000)
    assert len(bytes.fromhex(body["i"])) == 16
    assert json.loads(second.partition(b".")[2])["i"] != body["i"]


@given(st.binary(max_size=40), st.binary(max_size=40), st.binary(max_size=40), st.binary(max_size=40))
def test_no_two_different_channels_and_texts_are_signed_as_the_same_bytes(channel, text, other_channel, other_text):
    assume((channel, text) != (other_channel, other_text))
    assert Bus._signed(channel, text) != Bus._signed(other_channel, other_text)


@st.composite
def _cut_twice(draw):
    """Some bytes, among them the ones that a separator would be taken for, and two different places to cut them at."""
    whole = bytes(draw(st.lists(st.sampled_from([0, 1, 0x2E, 0x3A, 0x61]), min_size=1, max_size=24)))
    first, second = draw(st.lists(st.integers(0, len(whole)), min_size=2, max_size=2, unique=True))
    return whole, first, second


@given(_cut_twice())
def test_the_same_bytes_cut_at_another_place_are_signed_as_other_bytes(cut):
    whole, first, second = cut
    assert Bus._signed(whole[:first], whole[first:]) != Bus._signed(whole[:second], whole[second:])


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ((b"a\x00b", b"c"), (b"a", b"b\x00c")),
        ((b"ab", b"c"), (b"a", b"bc")),
        ((b"latet:identity:u_a", b"{}"), (b"latet:identity:u_a{", b"}")),
        ((b"", b"x"), (b"x", b"")),
    ],
)
def test_a_byte_moved_across_the_border_of_channel_and_text_changes_what_is_signed(one, other):
    assert Bus._signed(*one) != Bus._signed(*other)


# (a) Nothing of a message is read before its signature is checked.
@pytest.mark.parametrize(
    "raw",
    [
        b"[" * 200_000,  # a reader that parses first meets RecursionError, which ends the ear
        b"[" * 200_000 + b".{}",
        b"0" * 64 + b"." + b"[" * 200_000,
        b"0" * 64 + b"." + b'{"t":' * 100_000,
        b'{"m": "{}", "s": "00"}',  # the first wire, which was JSON
        b"",
        b".",
        b"junk",
        b"\xff\xfe\x00 not text",
    ],
    ids=lambda raw: repr(raw[:40]),  # an id of 200,000 characters is too long for Windows' environment
)
def test_what_is_not_signed_is_refused_before_a_byte_of_it_is_parsed(mine, raw):
    assert _refusal(mine, CHANNEL, raw) == "signature"


def test_a_channel_that_is_not_text_is_refused_and_does_not_raise(mine):
    assert _refusal(mine, b"latet:identity:\xff\xfe", b"junk") == "signature"
    assert _refusal(mine, b"latet:identity:\xff\xfe", mine._wire(CHANNEL, {"a": 1})) == "signature"


def test_a_message_signed_with_another_key_or_for_another_channel_does_not_verify(mine, clock):
    other = Bus(ADDRESS, key=b"\x09" * 32, clock=clock)
    assert _refusal(mine, CHANNEL, other._wire(CHANNEL, {"forged": True})) == "signature"
    wire = mine._wire(CHANNEL, {"a": 1})
    assert _refusal(mine, b"latet:identity:u_danielokaf", wire) == "signature"  # copied to another channel
    assert _refusal(mine, b"other:identity:u_emilycarte", wire) == "signature"  # or another prefix
    assert _refusal(mine, CHANNEL[:-1], wire) == "signature"
    assert mine._open(CHANNEL, wire, _Taken())[1] == {"a": 1}  # the original still verifies where it was made


def test_the_time_and_the_id_are_under_the_signature(mine):
    wire = mine._wire(CHANNEL, {"a": 1})
    signature, _, text = wire.partition(b".")
    body = json.loads(text)
    for changed in ({**body, "t": body["t"] + 1}, {**body, "i": ID}, {**body, "m": {"a": 2}}):
        # a later time, an id nobody took, another message: each is a new text under the old signature
        made = signature + b"." + json.dumps(changed, separators=(",", ":")).encode()
        assert _refusal(mine, CHANNEL, made) == "signature"
    assert _refusal(mine, CHANNEL, signature + b"." + text + b" ") == "signature"
    assert _refusal(mine, CHANNEL, signature[:-1] + b"." + text) == "signature"
    assert _refusal(mine, CHANNEL, signature.upper() + b"." + text) == "signature"
    assert _refusal(mine, CHANNEL, b"." + text) == "signature"
    assert _refusal(mine, CHANNEL, signature + text) == "signature"


@pytest.mark.parametrize(
    "text",
    [
        b"not json",
        b"[]",
        b'"text"',
        b"7",
        b"null",
        b"{}",
        b'{"i":"' + ID.encode() + b'","m":{}}',  # no time
        b'{"m":{},"t":1800000000000}',  # no id
        b'{"i":"' + ID.encode() + b'","t":1800000000000}',  # no message
        b'{"i":"zz","m":{},"t":1800000000000}',  # an id that is no hex
        b'{"i":"' + b"0" * 31 + b'","m":{},"t":1800000000000}',  # an id of 15 bytes
        b'{"i":"' + b"A" * 32 + b'","m":{},"t":1800000000000}',  # capitals: not what secrets.token_hex writes
        b'{"i":7,"m":{},"t":1800000000000}',
        b'{"i":"' + ID.encode() + b'","m":[],"t":1800000000000}',  # a message is an object
        b'{"i":"' + ID.encode() + b'","m":{},"t":"1800000000000"}',
        b'{"i":"' + ID.encode() + b'","m":{},"t":1800000000000.5}',
        b'{"i":"' + ID.encode() + b'","m":{},"t":true}',
        b"[" * 200_000,  # signed by an instance that should not have: a message that cannot be read ends nothing
        b"\xff\xfe",
    ],
    ids=lambda text: repr(text[:60]),
)
def test_a_signed_text_that_is_no_message_is_dropped_all_the_same(mine, text):
    assert _refusal(mine, CHANNEL, _signed_text(mine, text)) == "form"


@pytest.mark.parametrize(
    ("moved", "passes"),
    [
        (0, True),
        (59, True),
        (60, True),
        (61, False),
        (3600, False),
        (-59, True),
        (-60, True),
        (-61, False),
        (-86400, False),
    ],
)
def test_a_message_is_fresh_within_the_window_of_this_computers_clock_either_way(mine, clock, moved, passes):
    assert MESSAGE_WINDOW == 60
    wire = mine._wire(CHANNEL, {"a": 1})
    clock.now = START + moved  # this computer's time at the moment it is heard
    if passes:
        assert mine._open(CHANNEL, wire, _Taken())[1] == {"a": 1}
    else:
        assert _refusal(mine, CHANNEL, wire) == "time"


def test_a_publisher_whose_clock_is_far_off_is_not_heard(mine):
    behind, ahead, near = (Bus(ADDRESS, key=KEY, clock=Clock(START + offset)) for offset in (-61, 61, 30))
    assert _refusal(mine, CHANNEL, behind._wire(CHANNEL, {"a": 1})) == "time"
    assert _refusal(mine, CHANNEL, ahead._wire(CHANNEL, {"a": 1})) == "time"
    assert mine._open(CHANNEL, near._wire(CHANNEL, {"a": 1}), _Taken())[1] == {"a": 1}


def test_a_message_is_taken_once_by_a_call_and_a_copy_is_dropped(mine, clock):
    taken, wire = _Taken(), mine._wire(CHANNEL, {"a": 1})
    assert mine._open(CHANNEL, wire, taken)[1] == {"a": 1}
    assert _refusal(mine, CHANNEL, wire, taken) == "repeated"
    clock.now += 59  # inside its window still
    assert _refusal(mine, CHANNEL, wire, taken) == "repeated"
    clock.now += 2  # past it: the time check refuses it
    assert _refusal(mine, CHANNEL, wire, taken) == "time"
    clock.now = START
    again = mine._wire(CHANNEL, {"a": 1})  # the same message published again is a message of its own
    assert mine._open(CHANNEL, again, taken)[1] == {"a": 1}


# Important 1. The ids that the calls of a bus have taken are the bus's: a call that starts anew knows them.
def test_what_a_call_has_taken_a_call_that_starts_after_it_knows_of(mine):
    wire = mine._wire(CHANNEL, {"a": 1})
    first = mine._taken_so_far()
    assert mine._open(CHANNEL, wire, first)[1] == {"a": 1}
    anew = mine._taken_so_far()  # the connection dropped, and a call starts again
    assert _refusal(mine, CHANNEL, wire, anew) == "repeated"
    assert _refusal(mine, CHANNEL, wire, mine._taken_so_far()) == "repeated"  # and every one after that


def test_calls_that_run_together_take_each_message_on_their_own(mine):
    one, other = mine._taken_so_far(), mine._taken_so_far()
    wire = mine._wire(CHANNEL, {"a": 1})
    assert mine._open(CHANNEL, wire, one)[1] == {"a": 1}
    assert mine._open(CHANNEL, wire, other)[1] == {"a": 1}  # published after both began: each hears it
    assert _refusal(mine, CHANNEL, wire, one) == "repeated"  # and neither twice
    assert _refusal(mine, CHANNEL, wire, other) == "repeated"


def test_a_call_takes_what_another_call_took_after_its_own_memory_was_made(mine):
    late = mine._taken_so_far()  # it begins, and subscribes
    wire = mine._wire(CHANNEL, {"a": 1})  # published after its subscription
    assert mine._open(CHANNEL, wire, mine._taken_so_far())[1] == {"a": 1}  # another call took it first
    assert mine._open(CHANNEL, wire, late)[1] == {"a": 1}  # which loses it nothing: it was not in the copy


def test_a_call_starts_with_the_ids_that_are_still_to_be_known_and_no_more_than_the_bus_keeps(monkeypatch, clock):
    monkeypatch.setattr(bus_module, "MESSAGE_MEMORY", 4)
    small = Bus(ADDRESS, key=KEY, clock=clock)
    call = small._taken_so_far()
    for number in range(7):
        small._open(CHANNEL, small._wire(CHANNEL, {"n": number}), call)
    assert len(small._taken_so_far()) == 4  # the bus keeps four, the ones to be forgotten last
    clock.now += 2 * MESSAGE_WINDOW + 1  # no copy of a message that no check would let through
    assert len(small._taken_so_far()) == 0
    small.close()


def test_the_memory_of_the_bus_is_kept_under_its_lock(mine):
    wire, started, done = mine._wire(CHANNEL, {"a": 1}), threading.Event(), threading.Event()
    for work in (lambda: mine._taken_so_far(), lambda: mine._open(CHANNEL, wire, _Taken())):

        def worker(work=work):
            started.set()
            work()
            done.set()

        started.clear()
        done.clear()
        with mine._lock:  # while another thread holds it, the memory is not read nor written
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            assert started.wait(2) and not done.wait(0.2)
        assert done.wait(2)
        thread.join(2)


# Minor 2. A wall clock that steps back does not bring a message that was heard back into its window.
@pytest.mark.parametrize(
    ("later", "back", "refused"),
    [(60.002, 10, "repeated"), (100, 40, "repeated"), (119, 59, "repeated"), (121, 60, "time")],
)
def test_a_clock_that_steps_back_does_not_make_a_message_that_was_heard_new_again(mine, clock, later, back, refused):
    taken, wire = _Taken(), mine._wire(CHANNEL, {"kind": "disabled"})
    assert mine._open(CHANNEL, wire, taken)[1] == {"kind": "disabled"}
    clock.now += later  # past its window or not: another message comes, and the memory is looked at
    assert mine._open(CHANNEL, mine._wire(CHANNEL, {"other": 1}), taken)[1] == {"other": 1}
    clock.now -= back  # a step of NTP, or a virtual machine that was suspended and resumes
    assert _refusal(mine, CHANNEL, wire, taken) == refused
    assert _refusal(mine, CHANNEL, wire, mine._taken_so_far()) == refused  # and so for a call that starts then


def test_the_ids_taken_are_kept_for_two_windows_and_never_more_than_the_limit():
    taken = _Taken(limit=3)
    # "a" carries the time 1,000 ms. A copy of it passes the time check until 61,000 ms, and the id is kept for one
    # window more, so that a clock that steps back by up to a window does not bring it back: until 121,000 ms
    assert taken.take(b"a", 1_000, 1_000)
    assert not taken.take(b"a", 1_000, 61_001)  # past the time check's window, and still known
    assert not taken.take(b"a", 1_000, 121_000)  # at the last millisecond
    assert taken.take(b"b", 2_000, 121_001) and len(taken) == 1  # one millisecond later "a" is forgotten, and "b" taken
    assert taken.take(b"a", 121_000, 121_001)  # an id that is new here
    assert taken.take(b"c", 130_000, 121_001) and len(taken) == 3
    # the limit is reached: the id to be forgotten soonest ("b", at 122,000 ms) goes to make room
    assert taken.take(b"d", 140_000, 121_001) and len(taken) == 3
    assert not taken.take(b"c", 130_000, 121_001) and not taken.take(b"d", 140_000, 121_001)
    assert taken.take(b"b", 2_000, 121_001)  # which is why "b" is new again


def test_the_memory_of_ids_is_bounded_by_default():
    taken = _Taken()
    for number in range(bus_module.MESSAGE_MEMORY + 10):
        assert taken.take(number.to_bytes(8, "big"), 1_000, 1_000)
    assert len(taken) == bus_module.MESSAGE_MEMORY


def test_a_bus_takes_a_key_of_32_bytes_and_nothing_else():
    assert len(b"s3cret-bus-key-of-33-bytes-in-all") == 33  # one too many
    for wrong in (b"", b"k", b"s3cret-bus-key-of-33-bytes-in-all", b"\x01" * 31, "k" * 32, None, bytearray(32)):
        with pytest.raises(ValueError, match="a key of 32 bytes") as refused:
            Bus(ADDRESS, key=wrong)  # type: ignore[arg-type]
        shown = "".join(traceback.format_exception(refused.value))
        assert "s3cret-bus-key" not in shown and "v4lkey-pw" not in shown
    Bus(ADDRESS, key=b"\x01" * 32).close()


def test_a_bus_shows_neither_its_key_nor_its_address(mine):
    shown = repr(mine) + str(mine) + repr(mine.client) + repr(mine._release)
    assert "v4lkey-pw" not in shown and KEY.hex() not in shown and repr(KEY) not in shown


def test_what_is_dropped_is_counted_and_said_once_and_then_once_a_minute(mine, clock, caplog):
    with caplog.at_level(logging.WARNING, logger="latet.server.bus"):
        for _ in range(5):
            mine._dropped("signature", "identity")
        assert mine.rejected == 5 and len(caplog.records) == 1  # five dropped, one line
        assert "identity" in caplog.text and "signature" in caplog.text
        clock.now += 59
        mine._dropped("signature", "identity")
        assert mine.rejected == 6 and len(caplog.records) == 1  # a minute has not passed
        clock.now += 2
        mine._dropped("time", "shared")
        assert mine.rejected == 7 and len(caplog.records) == 2
        said = caplog.records[1].getMessage()
        assert "shared" in said and "time" in said
        assert "6 dropped since" in said  # the four before, the one at 59 seconds, and this one
        clock.now += 61
        mine._dropped("repeated", "shared")  # the first after a quiet minute: one, said as one
        assert len(caplog.records) == 3 and "dropped since" not in caplog.records[2].getMessage()


def test_the_line_of_a_minute_counts_the_drops_of_each_check(mine, clock, caplog):
    """The line that opens a minute describes the drop that opens it; the next counts each check's drops, not only
    the one that happens to open that minute."""
    with caplog.at_level(logging.WARNING, logger="latet.server.bus"):
        mine._dropped("signature", "identity")
        for why in ("signature", "signature", "time", "repeated", "repeated", "repeated"):
            mine._dropped(why, "identity")
        clock.now += 61
        mine._dropped("form", "shared")
    said = caplog.records[1].getMessage()
    assert len(caplog.records) == 2 and "7 dropped since the line before" in said  # the six after the first, and this
    assert "signature 2" in said and "time 1" in said and "repeated 3" in said and "form 1" in said


async def test_the_address_parameters_do_not_win_over_what_the_bus_sets(clock):
    """redis-py lets an address's parameters win over what the code gives it: the bus builds its connections from what
    the address says, its own settings over it (the settings refuse such an address too)."""
    odd = "?socket_timeout=99&socket_connect_timeout=99&max_connections=1&timeout=0&health_check_interval=0"
    bus = Bus(ADDRESS + odd + "&client_name=hers&socket_keepalive=false", key=KEY, clock=clock, connections=7)
    try:
        pool = bus.client.connection_pool
        kwargs = pool.connection_kwargs
        assert (kwargs["socket_timeout"], kwargs["socket_connect_timeout"], kwargs["health_check_interval"]) == (
            bus_module.SOCKET_TIMEOUT, bus_module.SOCKET_TIMEOUT, 30
        )  # fmt: skip
        assert (pool.max_connections, pool.timeout) == (7, bus_module.POOL_WAIT)
        assert (kwargs["host"], kwargs["port"], kwargs["password"], kwargs["db"]) == (HERE, 1, "v4lkey-pw", 0)
        listening = bus._listening_client()
        try:
            kwargs = listening.connection_pool.connection_kwargs
            assert (kwargs["client_name"], kwargs["socket_connect_timeout"], kwargs["socket_keepalive"]) == (
                "latet:listen", bus_module.SOCKET_TIMEOUT, True
            )  # fmt: skip
            assert "socket_timeout" not in kwargs and kwargs["password"] == "v4lkey-pw"
        finally:
            await listening.aclose()
    finally:
        bus.close()


async def test_the_listening_connection_has_a_time_to_connect_keepalive_and_no_time_to_wait_for_a_message(mine):
    client = mine._listening_client()
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == bus_module.SOCKET_TIMEOUT == 5
        assert kwargs["socket_keepalive"] is True
        # some redis-py versions cut the wait for a message at socket_timeout: the connection waits for messages
        assert "socket_timeout" not in kwargs
        assert kwargs["client_name"] == "latet:listen"
    finally:
        await client.aclose()


@contextlib.contextmanager
def _silent_peer():
    """A listener that takes connections into its queue and never says a word: a Valkey that does not answer."""
    with socket.socket() as listener:
        listener.bind((HERE, 0))
        listener.listen(16)
        yield listener.getsockname()[1]


def test_a_valkey_that_does_not_answer_fails_a_call_after_a_short_time(monkeypatch):
    monkeypatch.setattr(bus_module, "SOCKET_TIMEOUT", 0.3)
    failed = []

    def calls(bus):  # in a thread of its own: a call that is never answered must fail this test, not hold it
        for call in (lambda: bus.get("x"), lambda: bus.set("x", b"1", 5), lambda: bus.allow("x", 3, 5)):
            try:
                call()
            except redis.RedisError as error:
                failed.append(type(error).__name__)
        failed.append("ping says no" if not bus.ping() else "ping says yes")  # which raises nothing

    with _silent_peer() as port:
        silent = Bus(f"redis://:v4lkey-pw@{HERE}:{port}/0", key=KEY)
        worker = threading.Thread(target=calls, args=(silent,), daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(6)  # four calls, a third of a second each, and no more
        assert not worker.is_alive(), "a call to a Valkey that does not answer was still waiting after 6 seconds"
        assert failed == ["TimeoutError"] * 3 + ["ping says no"] and time.monotonic() - started < 6
        silent.close()


def _command(buffer: bytes) -> tuple[list[bytes] | None, bytes]:
    """The first command in ``buffer`` (RESP's array of bulk strings) and what follows it; none if it is not all there."""
    if not buffer.startswith(b"*") or b"\r\n" not in buffer:
        return None, buffer
    head, _, rest = buffer.partition(b"\r\n")
    words = []
    for _ in range(int(head[1:])):
        if not rest.startswith(b"$") or b"\r\n" not in rest:
            return None, buffer
        size, _, rest = rest.partition(b"\r\n")
        if len(rest) < int(size[1:]) + 2:
            return None, buffer
        words.append(rest[: int(size[1:])])
        rest = rest[int(size[1:]) + 2 :]
    return words, rest


@contextlib.contextmanager
def _mute_valkey():
    """A Valkey that takes a connection, a password and a name, and says nothing of a subscription: every command but
    SUBSCRIBE and PSUBSCRIBE is answered with OK (and HELLO, which redis-py 8 sends with the password, with the map
    that says the protocol is RESP3). It gives its port and the list of the commands it has received, in order."""
    stop, seen = threading.Event(), []

    def serve(connection):
        buffer = b""
        with connection:
            connection.settimeout(0.1)
            while not stop.is_set():
                try:
                    data = connection.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                while (found := _command(buffer))[0] is not None:
                    command, buffer = found
                    word = command[0].upper()
                    seen.append(word)
                    if word == b"HELLO":
                        connection.sendall(b"%1\r\n$5\r\nproto\r\n:3\r\n")
                    elif word not in (b"SUBSCRIBE", b"PSUBSCRIBE"):
                        connection.sendall(b"+OK\r\n")

    def accept(listener):
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    with socket.socket() as listener:
        listener.bind((HERE, 0))
        listener.listen(16)
        listener.settimeout(0.1)
        threading.Thread(target=accept, args=(listener,), daemon=True).start()
        try:
            yield listener.getsockname()[1], seen
        finally:
            stop.set()


async def _listened(bus, *channels, ready=None):
    """The first message of a call to ``listen``, if one comes."""
    async with contextlib.aclosing(bus.listen(*channels, ready=ready)) as messages:
        return await anext(messages)


# Minor 1. The call to Valkey that listens has SOCKET_TIMEOUT in all to connect, subscribe and be told so.
async def test_a_valkey_that_does_not_answer_fails_the_listening_call_and_does_not_hold_it(monkeypatch):
    monkeypatch.setattr(bus_module, "SOCKET_TIMEOUT", 0.3)  # redis-py's own time for the handshake is 5 seconds
    with _silent_peer() as port:
        silent = Bus(f"redis://:v4lkey-pw@{HERE}:{port}/0", key=KEY)
        started = time.monotonic()
        with pytest.raises(redis.TimeoutError, match="did not answer in time"):
            await asyncio.wait_for(_listened(silent, "shared"), 10)
        assert time.monotonic() - started < 2
        silent.close()


async def test_a_valkey_that_does_not_confirm_the_subscription_fails_the_listening_call_and_does_not_hold_it(
    monkeypatch,
):
    monkeypatch.setattr(bus_module, "SOCKET_TIMEOUT", 0.3)
    ready = asyncio.Event()
    with _mute_valkey() as (port, _):
        mute = Bus(f"redis://:v4lkey-pw@{HERE}:{port}/0", key=KEY)
        started = time.monotonic()
        with pytest.raises(redis.TimeoutError, match="did not answer in time"):  # a wait with no end, otherwise
            await asyncio.wait_for(_listened(mute, "identity:*", "shared", ready=ready), 10)
        assert time.monotonic() - started < 2 and not ready.is_set()
        mute.close()


async def test_a_call_copies_the_ids_taken_before_it_sends_the_subscription(monkeypatch):
    monkeypatch.setattr(bus_module, "SOCKET_TIMEOUT", 0.3)
    with _mute_valkey() as (port, seen):
        mute = Bus(f"redis://:v4lkey-pw@{HERE}:{port}/0", key=KEY)
        copy = mute._taken_so_far

        def copying():
            seen.append(b"COPY")  # in the order of what the Valkey receives and of what the bus does
            return copy()

        monkeypatch.setattr(mute, "_taken_so_far", copying)
        with pytest.raises(redis.TimeoutError):
            await asyncio.wait_for(_listened(mute, "shared"), 10)
        mute.close()
    assert b"COPY" in seen and b"SUBSCRIBE" in seen, seen
    assert seen.index(b"COPY") < seen.index(b"SUBSCRIBE"), seen


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("aws:u_emilycarte:", r"aws:u_emilycarte:"),
        ("a*b", r"a\*b"),
        ("a?b", r"a\?b"),
        ("a[b]", r"a\[b\]"),
        ("a\\b", r"a\\b"),
        ("*[?]\\", r"\*\[\?\]\\"),
        ("", ""),
    ],
)
def test_a_text_is_made_a_pattern_that_matches_exactly_it(text, pattern):
    assert _literal(text) == pattern


def test_a_purge_of_nothing_is_refused_before_anything_is_asked_of_valkey(mine):
    with pytest.raises(ValueError, match="needs a prefix"):  # nothing listens at the address: no ConnectionError
        mine.purge("")
