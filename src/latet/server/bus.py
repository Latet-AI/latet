"""Valkey for a Latet server: the messages between instances (publish and subscribe), the locks of the jobs that run on
one instance at a time, rate limits, and the short-lived keys the backends keep. redis-py speaks to Valkey and to Redis
alike; Latet uses only commands both have (strings with expiry, `SET NX PX`, hashes, sets, sorted sets, publish and
subscribe, `EVAL` for a lock's release and a limit's slots).

The messages are made for an attacker who has Valkey's password and not the master key: he can subscribe to every
channel and publish on any, and an instance must not act on what he writes. A message on the wire is
``<HMAC-SHA256 in hex>.<signed text>``. The signed text is the JSON of the message with the publisher's time
(milliseconds) and a random id of 16 bytes. The HMAC, made with the bus's key (derived from the master key), is over the
channel's length, the channel as Valkey names it (the prefix included) and that text: no two different (channel, time,
id, message) are signed as the same bytes, and a message copied to another channel, or one whose time or id was changed
to look new, does not verify.

``listen`` takes a message in this order, and nothing of it is read before the check that comes first: its signature,
over the bytes as they came and in constant time; then its text, which is parsed only now; then its time, which must be
within ``MESSAGE_WINDOW`` of this computer's clock either way; then its id, which the call, or one before it on this
bus, must not have taken yet. A message that fails a check is dropped, counted in ``rejected`` and said in the log,
without a word of it: once a minute at most, with how many each check dropped since the line before.

The connections are the bus's own: it reads the address for where Valkey is and as whom (redis-py would let the
address's parameters win over what the code gives it, and the settings refuse those), and keeps ``connections`` of them
at most for its calls, a call past them waiting ``POOL_WAIT`` for one before it fails as a Valkey that does not answer
fails it (redis-py's own pool raised at the hundred and first call at once). A call to ``listen`` has a connection of
its own, its patterns made of the bus's prefix taken as it is written, so that a prefix that holds a pattern's
character hears no other prefix's channels.

The bus keeps every id that any of its calls to ``listen`` has taken, and a call that begins starts with a copy of them,
made before it subscribes: when the connection drops and the instance listens again, a message published again by hand
is dropped as a repeat all the same, however many times the connection is dropped (the calls that run together take
each message on their own). An id is kept for two windows from the time its message carries, so that a wall clock
that steps back by up to a window does not make a message that was heard new again. What stays open is a restart of
the process, which the memory does not survive: someone with Valkey's password cannot cause it, and the instance
then builds every space afresh from PostgreSQL; and a clock that steps back by more than a window."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import heapq
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from typing import Any

import redis
import redis.asyncio
import redis.asyncio.client
import redis.asyncio.connection
import redis.connection

from latet.server.settings import VALKEY_CONNECTIONS, VALKEY_SCHEMES

log = logging.getLogger(__name__)
# Seconds from this computer's clock that the time a message carries may be, either way.
MESSAGE_WINDOW = 60.0
# How many ids one memory of ids taken keeps at most (a bus has one, and each call to ``listen`` its own): when there
# are more, the ones to be forgotten soonest go.
MESSAGE_MEMORY = 100_000
# Seconds a connection to Valkey has to be made, and an answer to come: a Valkey silent for longer fails the call.
SOCKET_TIMEOUT = 5.0
# Seconds a call waits for one of the bus's connections when all of them are taken (``connections``): past it, it fails
# with redis.ConnectionError, as a Valkey that does not answer fails it.
POOL_WAIT = SOCKET_TIMEOUT
# Bytes a message may weigh on the wire. By default Valkey drops a subscriber whose output buffer holds more than 8 MiB
# for a minute, or 32 MiB at once, and every instance's ear is one, hearing every identity's messages (E0c's guide says
# not to lower Valkey's limit): every message stays small, so that no burst of them comes near it. A dashboard's event
# heavier than ``live.FEED_INLINE`` goes by reference (``live.ValkeyFeed``), and this is the backstop.
MESSAGE_LIMIT = 96 * 1024
# Seconds between two lines in the log about messages that were dropped.
WARNING_EVERY = 60.0
_MESSAGE_WINDOW_MS = int(MESSAGE_WINDOW * 1000)
_KEPT_MS = 2 * _MESSAGE_WINDOW_MS  # how long after its time the id of a message is kept (see _Taken)
# What the log says of each check that a dropped message failed, after the name of the check.
_WHY = {
    "signature": "its signature does not verify for this channel with this server's key",
    "form": "it is signed, but it is not a message",
    "time": f"its time is more than {MESSAGE_WINDOW:.0f} seconds from this computer's: are the clocks right?",
    "repeated": "it was heard already",
}
_BATCH = 500  # keys deleted in one command by ``purge``
_LABEL = b"latet-bus\x00"  # what the signed bytes start with: the format they are in, and that they are the bus's
_NAMES_LABEL = b"latet-bus-names\x00"  # what the key that names a limit's count is derived from the bus's key with
_ID = re.compile(r"[0-9a-f]{32}")
# What a pattern of Valkey's SCAN reads in a text, and takes literally after a backslash.
_GLOB = re.compile(r"([\\*?\[\]])")
# A lock is released by its holder only: the value it was taken with.
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
# A slot taken under a limit, in one step: the count goes up, and a count with no lifetime is given the window's (the
# first of a window, or one left so by hand). The count is answered, and the milliseconds its window has left.
_TAKE = (
    "local n = redis.call('incr', KEYS[1]) redis.call('pexpire', KEYS[1], ARGV[1], 'NX')"
    " return {n, redis.call('pttl', KEYS[1])}"
)
# A slot given back: the count goes down, never below nothing, and nothing is made where nothing was taken.
_GIVE_BACK = (
    "local n = tonumber(redis.call('get', KEYS[1])) if n and n > 0 then return redis.call('decr', KEYS[1]) end return 0"
)


def redis_url(url: str) -> str:
    """A Valkey address as redis-py reads one: valkey:// is redis://, valkeys:// is rediss:// (TLS stays TLS), by the
    table of schemes that the settings judge an address with."""
    scheme, separator, rest = url.partition("://")
    return f"{VALKEY_SCHEMES.get(scheme, scheme)}{separator}{rest}" if separator else url


def _literal(text: str) -> str:
    """``text`` as a pattern of Valkey's SCAN that matches exactly it: a backslash before each character that a
    pattern reads (the backslash itself, ``*``, ``?``, ``[`` and ``]``)."""
    return _GLOB.sub(r"\\\1", text)


class _Refused(Exception):
    """A message that does not pass. ``why`` is the check it failed: signature, form, time or repeated."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


class _Taken:
    """The ids of messages that were taken, so that each is taken once. An id is kept for two windows from the time its
    message carries: the time check lets the message through for one, and the other keeps the id while the wall clock
    steps back by up to a window (an id that was forgotten would be unknown again, with its message in its window
    again). No more than ``limit`` are kept: when they are too many, the ones to be forgotten soonest go."""

    def __init__(self, limit: int = MESSAGE_MEMORY) -> None:
        self._limit = limit
        self._ids: set[bytes] = set()
        self._ends: list[tuple[int, bytes]] = []  # a heap: (the millisecond after which an id is forgotten, the id)

    def __len__(self) -> int:
        return len(self._ids)

    def prune(self, now: int) -> None:
        """Forget the ids whose time is over. Milliseconds."""
        while self._ends and self._ends[0][0] < now:
            self._ids.discard(heapq.heappop(self._ends)[1])

    def copy(self) -> _Taken:
        """Another memory with the same ids: what is taken in one is not taken in the other."""
        found = _Taken(self._limit)
        found._ids, found._ends = set(self._ids), list(self._ends)  # the copy of a heap is a heap
        return found

    def take(self, ident: bytes, sent: int, now: int) -> bool:
        """Whether ``ident``, of a message with the time ``sent``, was not taken yet; it now is. Milliseconds."""
        self.prune(now)
        if ident in self._ids:
            return False
        if len(self._ids) >= self._limit:
            self._ids.discard(heapq.heappop(self._ends)[1])
        self._ids.add(ident)
        heapq.heappush(self._ends, (sent + _KEPT_MS, ident))
        return True


class Bus:
    """Valkey for one instance of a server, under ``prefix``. ``key``: the bus's own, 32 bytes (on a server
    ``MasterKey.derive(b"bus")``): every message it publishes is signed with it and every message it hears is checked
    with it. ``clock``: seconds since 1970, which the bus stamps its messages with and judges the ones it hears by. It
    is this computer's clock, always: the keyword is for this module's own tests, ``open_server`` passes none, whatever
    clock a server's test gives the server to move (instances whose clocks are more than ``MESSAGE_WINDOW`` apart
    drop each other's messages). ``connections``: how many connections its calls share at most (the setting
    valkey_connections), past which a call waits ``POOL_WAIT`` for one."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "latet:",
        key: bytes,
        clock: Callable[[], float] = time.time,
        connections: int = VALKEY_CONNECTIONS,
    ) -> None:
        if not isinstance(key, bytes) or len(key) != 32:  # an empty key would let anyone sign
            raise ValueError("the bus needs a key of 32 bytes")
        if type(connections) is not int or connections < 1:
            raise ValueError("the bus keeps one connection to Valkey at least")
        self._url = redis_url(url)
        self.prefix = prefix
        self._key = key  # derived from the master key: every message is signed with it, and checked with it
        # What names a limit's count in Valkey (``rate_key``): a key of its own, derived from the bus's, the same on
        # every instance, so that a count's name holds no client's address nor anyone's id in clear.
        self._names = hmac.new(key, _NAMES_LABEL, hashlib.sha256).digest()
        self._clock = clock
        self.rejected = 0  # messages dropped (E0c's latet_bus_rejected gauge reads it)
        self._lock = threading.Lock()  # a call to ``listen`` may run in a thread of its own, and every one counts
        # Every id that any call of this bus has taken, for the calls that begin later (see ``_taken_so_far``).
        self._memory = _Taken(MESSAGE_MEMORY)
        self._said: float | None = None  # when the log last spoke of dropped messages
        self._unsaid: Counter[str] = Counter()  # messages dropped since, by the check each failed
        # redis-py types its commands for its asyncio client too (a value or an awaitable): the sync client's here.
        # The event loop calls it too: a Valkey that does not answer fails the call after SOCKET_TIMEOUT. Where Valkey
        # is and as whom are the address's; the rest is the bus's, over whatever the address says of it.
        options: dict[str, Any] = {
            **redis.connection.parse_url(self._url), "health_check_interval": 30, "socket_timeout": SOCKET_TIMEOUT,
            "socket_connect_timeout": SOCKET_TIMEOUT, "max_connections": connections, "timeout": POOL_WAIT,
        }  # fmt: skip
        self.client: Any = redis.Redis(connection_pool=redis.BlockingConnectionPool(**options))
        self.client.auto_close_connection_pool = True  # its pool closes with it
        self._release = self.client.register_script(_RELEASE)
        self._take = self.client.register_script(_TAKE)
        self._give_back = self.client.register_script(_GIVE_BACK)

    def key(self, name: str) -> str:
        return self.prefix + name

    @staticmethod
    def _signed(channel: bytes, text: bytes) -> bytes:
        """The bytes that the HMAC is made over. The channel's length sets the channel and the text apart, so that two
        different pairs never make the same bytes (a byte moved across their border changes them)."""
        return _LABEL + len(channel).to_bytes(4, "big") + channel + text

    def _signature(self, channel: bytes, text: bytes) -> bytes:
        return hmac.new(self._key, self._signed(channel, text), hashlib.sha256).hexdigest().encode()

    def _wire(self, channel: bytes, message: Mapping[str, Any]) -> bytes:
        """What goes on ``channel`` (as Valkey names it) for ``message``: the signature, a dot and the signed text."""
        body = {"i": secrets.token_hex(16), "m": dict(message), "t": int(self._clock() * 1000)}
        text = json.dumps(body, separators=(",", ":")).encode()
        return self._signature(channel, text) + b"." + text

    def publish(self, channel: str, message: Mapping[str, Any]) -> None:
        """``message`` on ``channel``, signed, stamped and given an id: an instance with this server's key takes it,
        nobody else can make one. Raises ValueError, and Valkey gets nothing, for a message that weighs more than
        ``MESSAGE_LIMIT`` on the wire: the words say how much, and nothing of what it holds."""
        full = self.key(channel)
        wire = self._wire(full.encode(), message)
        if len(wire) > MESSAGE_LIMIT:
            raise ValueError(
                f"a message on the bus weighs {MESSAGE_LIMIT} bytes at most, and this one on the "
                f"{channel.partition(':')[0]} channel {len(wire)}: it was not sent"
            )
        self.client.publish(full, wire)

    def _taken_so_far(self) -> _Taken:
        """The memory of ids of a call to ``listen`` that begins: a copy of every id that any call of this bus has
        taken. It is made before the call sends SUBSCRIBE, and only then, for Valkey gives a call what is published
        after its subscription: nothing that is new to the call is in the copy, and a message that another call takes
        from then on is still the call's to take. Calls that run together take each message on their own, and one that
        begins after a connection dropped does not hear again what the call before it heard."""
        with self._lock:
            self._memory.prune(int(self._clock() * 1000))
            return self._memory.copy()

    def _open(self, channel: Any, raw: Any, taken: _Taken) -> tuple[str, dict[str, Any]]:
        """The name (without the prefix) of ``channel`` and the message that ``raw`` says on it, both as they came
        from Valkey: the signature is checked over their bytes before anything of them is read, and only a text that
        verifies is parsed. What is taken is added to ``taken``, the call's memory, and to the bus's. Raises _Refused,
        with the check that failed, for what does not pass."""
        if not isinstance(channel, bytes) or not isinstance(raw, bytes):
            raise _Refused("signature")
        given, dot, text = raw.partition(b".")  # a signature is hex: there is no dot in it
        if not dot or not hmac.compare_digest(given, self._signature(channel, text)):
            raise _Refused("signature")
        name, ident, sent, data = "", b"", 0, None
        try:  # signed by an instance with the key, so this is a mistake of its own: a message that cannot be read
            body = json.loads(text.decode())
            name, wanted, sent, data = channel.decode().removeprefix(self.prefix), body["i"], body["t"], body["m"]
            ident = bytes.fromhex(wanted) if _ID.fullmatch(wanted) else b""
        except (ValueError, TypeError, KeyError, RecursionError):
            raise _Refused("form") from None
        if not ident or type(sent) is not int or not isinstance(data, dict):
            raise _Refused("form")
        now = int(self._clock() * 1000)
        if abs(now - sent) > _MESSAGE_WINDOW_MS:
            raise _Refused("time")
        if not taken.take(ident, sent, now):
            raise _Refused("repeated")
        with self._lock:  # the bus's memory is every call's, and a call may run in a thread of its own
            self._memory.take(ident, sent, now)
        return name, data

    def _dropped(self, why: str, kind: str) -> None:
        """A message did not pass: it is counted, and said in the log without a word of it. The first line comes at
        once, then one a minute at most, describing the drop that opens it and with how many each check dropped since
        the line before."""
        now = self._clock()
        with self._lock:
            self.rejected += 1
            self._unsaid[why] += 1
            if self._said is not None and abs(now - self._said) < WARNING_EVERY:
                return
            self._said, said, self._unsaid = now, self._unsaid, Counter()
        total = said.total()
        each = ", ".join(f"{check} {count}" for check, count in sorted(said.items()))
        log.warning(
            "a message on the %s channel was dropped (%s: %s)%s",
            kind,
            why,
            _WHY[why],
            f"; {total} dropped since the line before ({each})" if total > 1 else "",
        )

    def _listening_client(self) -> redis.asyncio.Redis:
        """The connection of one call to ``listen``, named ``<prefix>listen`` in Valkey's list of clients. It has a
        time to be made and TCP keepalive, so that a Valkey that does not answer fails the call and a peer that is gone
        is found by the system. It has no ``socket_timeout``, which redis-py's older versions apply to the wait for a
        message too: it waits for messages, and a silence is for the server's beat to notice, not the bus's. Where
        Valkey is and as whom are the address's; the rest is the bus's, over whatever the address says of it."""
        options: dict[str, Any] = dict(redis.asyncio.connection.parse_url(self._url))
        options.pop("socket_timeout", None)
        options |= {"client_name": f"{self.prefix}listen", "socket_connect_timeout": SOCKET_TIMEOUT}
        options["socket_keepalive"] = True
        client = redis.asyncio.Redis(connection_pool=redis.asyncio.ConnectionPool(**options))
        client.auto_close_connection_pool = True  # its pool closes with it
        return client

    async def _subscribe(
        self, pubsub: redis.asyncio.client.PubSub, messages: AsyncIterator[Any], patterns: list[str], plain: list[str]
    ) -> list[dict[str, Any]]:
        """Connect, subscribe, and wait until Valkey has confirmed every subscription, in ``SOCKET_TIMEOUT`` seconds
        in all: a Valkey that does not answer fails the call and does not hold it. (redis-py gives a time to the
        handshake only by a default of its own, and reads the confirmations with none.) The wait for a message that
        comes after has no limit. The messages that arrived between two confirmations are returned. Raises
        redis.TimeoutError, in words that say nothing of the address."""
        early: list[dict[str, Any]] = []
        unconfirmed = len(patterns) + len(plain)
        try:
            async with asyncio.timeout(SOCKET_TIMEOUT):
                if patterns:
                    await pubsub.psubscribe(*patterns)
                if plain:
                    await pubsub.subscribe(*plain)
                while unconfirmed:
                    message = await anext(messages, None)
                    if message is None:
                        raise redis.ConnectionError("the connection to Valkey ended before it confirmed")
                    if message.get("type") in ("subscribe", "psubscribe"):
                        unconfirmed -= 1
                    elif message.get("type") in ("message", "pmessage"):
                        early.append(message)
        except TimeoutError:
            raise redis.TimeoutError("the connection to Valkey did not answer in time") from None
        return early

    def _hear(
        self, message: dict[str, Any], taken: _Taken, kinds: dict[bytes, str]
    ) -> tuple[str, dict[str, Any]] | None:
        """What a message of a subscription says when it passes the checks; None for one that is dropped (it is counted
        and said) and for one that is no message. A subscription confirmed again means that redis-py made the
        connection anew, and that what was published meanwhile is lost: ConnectionError."""
        kind = message.get("type")
        if kind in ("subscribe", "psubscribe"):
            raise redis.ConnectionError("the connection to Valkey was made again: messages may be lost")
        if kind not in ("message", "pmessage"):
            return None
        channel = message["channel"]
        try:
            return self._open(channel, message["data"], taken)
        except _Refused as refused:
            self._dropped(refused.why, kinds.get(message.get("pattern") or channel, "?"))
            return None

    async def listen(
        self, *channels: str, ready: asyncio.Event | None = None
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """The messages of ``channels`` (one ending in `*` is a pattern), named without the prefix, from the moment
        ``ready`` is set until the caller stops. A connection of its own, made on the running event loop and named
        ``<prefix>listen`` in Valkey's list of clients. A message is given only when it passes the checks of the
        module's words; the others are dropped and counted in ``rejected``. Valkey tells a call twice of a channel that
        two of its subscriptions match (``shared`` and ``sh*``): the second is dropped as a repeat. The call starts
        with the ids that every call of this bus has taken (``_taken_so_far``). The connection and the subscriptions
        have ``SOCKET_TIMEOUT`` seconds, and the wait for messages none. When the connection drops, ConnectionError:
        what was published meanwhile is lost, and the caller decides what that means (a redis-py that connects again
        by itself is caught by the subscriptions it then confirms a second time). A Valkey that does not answer in
        time fails the call with a redis.TimeoutError."""
        # A pattern is the bus's prefix and the channel's name taken as they are written, then the `*`: a prefix that
        # holds a character a pattern reads matches no other prefix's channels.
        named = {_literal(self.key(c[:-1])) + "*" if c.endswith("*") else self.key(c): c for c in channels}
        patterns = [name for name, c in named.items() if c.endswith("*")]
        plain = [name for name, c in named.items() if not c.endswith("*")]
        if not patterns and not plain:
            return
        # What the log says of a message is a subscription of this call's, never a name that came with the message.
        kinds = {name.encode(): c.partition(":")[0] for name, c in named.items()}
        taken = self._taken_so_far()  # before anything is sent to Valkey
        client = self._listening_client()
        pubsub = client.pubsub()
        messages = pubsub.listen()
        try:
            early = await self._subscribe(pubsub, messages, patterns, plain)
            if ready is not None:  # Valkey has confirmed each: from now on, nothing is missed
                ready.set()
            for message in early:
                if (heard := self._hear(message, taken, kinds)) is not None:
                    yield heard
            async for message in messages:
                if (heard := self._hear(message, taken, kinds)) is not None:
                    yield heard
        finally:
            with contextlib.suppress(Exception):  # the connection may be gone already: that is why it ends
                await pubsub.aclose()
            with contextlib.suppress(Exception):
                await client.aclose()

    @contextlib.contextmanager
    def lock(self, name: str, seconds: float) -> Iterator[bool]:
        """True inside when this instance runs the job now; the lock ends by itself after ``seconds`` if this
        instance stops, and only its holder releases it."""
        key, token = self.key(f"lock:{name}"), secrets.token_hex(16)
        mine = bool(self.client.set(key, token, nx=True, px=max(1, int(seconds * 1000))))
        try:
            yield mine
        finally:
            if mine:
                self._release(keys=[key], args=[token])

    def hold(self, name: str, seconds: float) -> AbstractContextManager[bool]:
        """``lock``, under the name of E0a's ``JobLock``."""
        return self.lock(name, seconds)

    def rate_key(self, key: str) -> str:
        """The name, under the bus's prefix, that counts the limit ``key`` (``<kind>:<who>``: a client's bucket, an
        identity): ``rate:<kind>:<keyed hash of who>``, as ``known:`` names what was found valid, so that whoever reads
        Valkey's keys learns no address and no identity from them. A key with no ``:`` is a count of its own kind
        alone (``rate:<kind>``)."""
        kind, _, who = key.partition(":")
        if not who:
            return f"rate:{kind}"
        return f"rate:{kind}:{hmac.new(self._names, who.encode('utf-8', 'surrogatepass'), hashlib.sha256).hexdigest()}"

    def allow(self, key: str, limit: int, per: float) -> bool:
        """Whether one more is allowed: at most ``limit`` in a window of ``per`` seconds from the first."""
        name = self.key(self.rate_key(key))
        pipe = self.client.pipeline()
        pipe.incr(name)
        pipe.pexpire(name, max(1, int(per * 1000)), nx=True)
        count, _ = pipe.execute()
        return int(count) <= limit

    def take(self, key: str, limit: int, window: float) -> bool:
        """A slot under ``key``, taken before the work the limit guards (a credential's lookup): whether it is within
        ``limit`` in the window of ``window`` seconds from the first. One step in Valkey, so that calls made at the same
        moment each count, and no more than ``limit`` of them are let through, however many come at once. A slot taken
        for what proves known is given back (``give_back``). It counts under the name ``allow`` counts under. Raises
        what the driver raises (a count written by hand that is no number among it)."""
        return self.slot(key, limit, window) is None

    def slot(self, key: str, limit: int, window: float) -> float | None:
        """``take``, saying how long a bucket past its limit stays so: None when the slot is within ``limit``; else the
        seconds its window has left (what an instance remembers it past its limit for: ``machines.Limits``)."""
        name = self.key(self.rate_key(key))
        count, left = self._take(keys=[name], args=[max(1, int(window * 1000))])
        if int(count) <= limit:
            return None
        return max(0, int(left)) / 1000

    def give_back(self, key: str) -> None:
        """The slot ``take`` took under ``key``, given back: what it guarded proved known (a valid token), and costs its
        bucket nothing. The count never goes below nothing."""
        self._give_back(keys=[self.key(self.rate_key(key))])

    def get(self, key: str) -> bytes | None:
        return self.client.get(self.key(key))

    def set(self, key: str, value: bytes, seconds: float | None = None) -> None:
        """``value`` under ``key`` for ``seconds``, and for ever only when ``seconds`` is None. A lifetime of 0, or
        less, ends the key at once: a remaining lifetime that was rounded down to 0 must not keep a session or a code
        for ever."""
        self.client.set(self.key(key), value, px=None if seconds is None else max(1, int(seconds * 1000)))

    def delete(self, key: str) -> None:
        self.client.delete(self.key(key))

    def purge(self, prefix: str) -> int:
        """Delete every key under ``prefix`` (an identity's AWS sessions, say): how many. The bus's prefix and
        ``prefix`` are taken as they are written (a ``*`` or a ``[`` in either is no pattern), so that a name never
        reaches the keys of another, and the keys go in batches. An empty ``prefix`` is refused (ValueError): it would
        be every key of the bus."""
        if not prefix:
            raise ValueError("purge needs a prefix: an empty one is every key of the bus")
        pattern, removed, batch = _literal(self.key(prefix)) + "*", 0, []
        for found in self.client.scan_iter(match=pattern, count=1000):
            batch.append(found)
            if len(batch) >= _BATCH:
                removed += int(self.client.delete(*batch))
                batch.clear()
        return removed + (int(self.client.delete(*batch)) if batch else 0)

    def purge_families(self, owner: str, families: Iterable[str]) -> int:
        """Delete every key named ``<family>:<owner>`` or begun ``<family>:<owner>:``, for each of ``families`` (what an
        identity kept, once it is removed): how many. One scan of the keys whose name holds ``:<owner>``, each key's
        family and owner read from its name before it goes, so that ``owner`` met elsewhere in a name (another
        family's, a job's lock) keeps that key; the keys go in batches. ``owner`` is taken as it is written (no
        pattern), and one that is empty or holds a ``:`` is refused (ValueError): it would reach other owners' keys."""
        if not owner or ":" in owner:
            raise ValueError("purge_families needs an owner's name, with no colon in it")
        wanted = frozenset(families)
        pattern = _literal(self.prefix) + "*" + _literal(f":{owner}") + "*"
        removed, batch = 0, []
        for found in self.client.scan_iter(match=pattern, count=1000):
            name = found.decode(errors="replace").removeprefix(self.prefix)
            family, _, rest = name.partition(":")
            if family in wanted and (rest == owner or rest.startswith(owner + ":")):
                batch.append(found)
            if len(batch) >= _BATCH:
                removed += int(self.client.delete(*batch))
                batch.clear()
        return removed + (int(self.client.delete(*batch)) if batch else 0)

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except (redis.RedisError, OSError):
            return False

    def close(self) -> None:
        self.client.close()
