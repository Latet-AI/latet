"""What a Latet server keeps in Valkey for an identity, for every instance (E0a's protocols): its conversations, their
texts as they left and what the final check let leave, the blocks they recorded, its live feeds, its AWS sessions and
its jobs' locks; and the dashboard's sign-ins.

Every identity shares Valkey, and whoever has its password reads and writes every key of it without the master key
(Ruling 26's attacker). So what could hold a value or a text is sealed, with the identity's root key (the sign-ins and
the AWS sessions with keys derived from the master key), each record bound to its own key: copied under another, it
does not open. A name or a member made from a conversation's key or a response id is a keyed hash of it, and so is the
tag that says an entity was shown to a conversation, or that a block left in it: nobody without the key makes one, and
one copied to another conversation matches nothing there. What is in clear is ids, keyed hashes and times.

Each family has a ceiling for each identity, enforced as it is written (past it the least recently used go, with their
records), and a lifetime, so that what one identity keeps cannot fill Valkey for the others: the texts as they left and
the conversations are bounded in what they take too (``GIVEN_BYTES``, ``CONVERSATION_ENTRIES``), counted exactly beside
them. What does not open or cannot be read (written by hand, moved, sealed with another key) counts as absent, and is
said in the log once a minute at most for its family, by the family's name alone.

What sealing cannot stop, someone who writes Valkey can still do: delete what he finds, or let it expire (the instances
then mask anew, or ask again; a conversation whose project and folder are gone has none on an instance that did not
hold it, until a client reports its folder there: the global rules apply meanwhile); write back a record he saved, which
opens again within its own limits (a text as it left is asked again before it is sent so,
``Mediator._leaves_as_it_left``; a dashboard's code within its two minutes, a session within the twelve hours from its
opening, and disabling or removing the identity holds through the dashboard's check of it; a conversation's project
and folder from its past do not open for an instance that holds later ones, and an instance that holds none takes them,
but what that project lowers applies there only once a client of the identity reports its folder to that instance,
``Projects.scope_for``: an older project that only adds protection is applied there until then); hold a job's lock (the
history is then pruned without it of what is a day past its retention, ``Recorder``). A conversation's new project or
folder is said on the identity's channel, which he can neither forge nor replay (``Bus``): an instance that hears of a
later one forgets the folder its clients reported for that conversation and, until it reads the new one, writes back
no older one it knows. What it heard stays with the conversation it holds, whatever its bounded memory of notes
forgets, and an update of a conversation it no longer holds anything of (gone from both memories while its request
ran) writes none of its project and folder. So rolling back or deleting the new one in Valkey leaves no instance
applying what the older folder lowers, nor the older folder in place of the new one where the new one is held, and
that instance writes the new one back at its next update. Meanwhile what the new one only adds applies there alone:
elsewhere an older project's safe side, or the global rules, until the new one is read there or a client reports a
folder there. What remains is an instance that does not hear (its ear down, its connection to Valkey silenced): it
forgets its memories then, spaces closed (Ruling 19, R-14).

Each method makes one round trip to Valkey, or two (three when a family is past its ceiling); a conversation's update
makes none when there is nothing new to write. Those the event loop makes (``latet.state``'s "who calls") wait at most
the bus's ``SOCKET_TIMEOUT``: a Valkey that does not answer fails the request closed. But what the model wrote and the
conversation of a response id, which a small restore writes on the event loop, are written off the caller's thread
(R-7): queued, within bounds of each space (``WRITES_QUEUED``, ``WRITES_QUEUED_BYTES``: past them the oldest is dropped,
counted and said once a minute), and sent by the space's writer, many to a round trip. Another instance learns them a
moment later. Until then, and for good when one was dropped, an instance masks anew a text the model wrote when it
comes back, the vault giving a known value the same fake, and finds the conversation of a request that follows a
response by the request's other marks, or starts one: never a value left in clear."""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from cryptography.exceptions import InvalidTag

from latet.engine.pipeline import LRU, Session, WeighedLRU
from latet.engine.types import EntityClass, Policy, Replacement
from latet.gateway.aws import AwsError
from latet.gateway.mediate import APPROVED_KEPT
from latet.gateway.sigv4 import AwsCredentials
from latet.logline import Paced, described
from latet.server.bus import Bus
from latet.server.master import MasterKey
from latet.server.sealing import Sealer
from latet.state import Left
from latet.state.local import CODE_TTL, SESSION_TTL

if TYPE_CHECKING:
    from latet.server.audit import ChainCheck

log = logging.getLogger(__name__)

CONVERSATION_TTL = 7 * 86400  # a conversation is remembered a week after its last use
GIVEN_TTL = CONVERSATION_TTL  # a text as it left, and what the final check let leave: a week after its last use
REVERSE_TTL = 3600  # what the model wrote, by its restored form: re-sent within the providers' cache times
RESPONSE_TTL = 7 * 86400
SENT_TTL = 7 * 86400
HELD_TTL = 3600  # an AWS session, an hour after its last use, and never past its end
SESSIONS_LISTED = 10_000
# What each identity keeps in Valkey at most: past it the least recently used go first.
CONVERSATIONS_KEPT = 10_000  # conversations, each with its approved texts (``APPROVED_KEPT`` at most)
# Fields of one conversation (its key, its project, an entity each): past it, no new one. At it, with every approved
# text a conversation keeps and a folder as long as a client may report, an instance's memory holds it still
# (``CONVERSATIONS_LARGEST``): about 8,900 entities would fit.
ENTITIES_KEPT = 8_000
CONVERSATION_ENTRIES = 1_000_000  # fields and approved texts of all its conversations (about 130 MB)
GIVEN_KEPT = 50_000  # texts as they left
GIVEN_BYTES = 32 * 1024 * 1024  # what Valkey takes for them, sealed (``weighed``)
GIVEN_LARGEST = 1024 * 1024  # bytes of one text as it left, sealed: a larger one is not kept (masked anew when sent)
CHECKED_KEPT = 200_000  # blocks the final check let leave, in all the identity's conversations
SENT_KEPT = 200_000  # blocks recorded, in all its conversations
REVERSE_KEPT = 50_000  # texts of the model
RESPONSES_KEPT = 50_000  # response ids
AWS_SESSIONS_KEPT = 200
# An AWS session's end further than this counts as none (no credentials of AWS's last longer): the session is then kept
# an hour after its last use, as one with no end.
AWS_LONGEST = 36 * 3600.0
# What an instance keeps in its memory in front of Valkey, for each identity's space: bounded in bytes too, since a
# server holds many spaces (``cached_bytes`` says what each holds). What it read is asked of Valkey again after a day,
# which also keeps it alive there for as long as it is used.
GIVEN_HERE = 5_000
GIVEN_HERE_BYTES = 8 * 1024 * 1024
CHECKED_HERE = 20_000
CONVERSATIONS_HERE = 10_000
CONVERSATIONS_HERE_BYTES = 8 * 1024 * 1024
CONVERSATIONS_LARGEST = 4 * 1024 * 1024  # a heavier one is not held (read whole at each use) rather than flush the rest
HERE_FOR = 86400.0
# What the model wrote and the conversations of response ids, written off the caller's thread (a small restore writes
# them on the event loop, which never waits on Valkey: R-7). Each space queues this many at most, taking this much at
# most: past either the oldest goes, counted and said once a minute. Its writer sends this many to a round trip, tries
# again this long after Valkey failed one, ends after this long with nothing to write (the next write starts it again),
# and when the space closes waits this long at most for what is queued.
WRITES_QUEUED = 1_000
WRITES_QUEUED_BYTES = 4 * 1024 * 1024
WRITES_AT_ONCE = 256
WRITER_RETRY = 1.0
WRITER_IDLE = 30.0
WRITER_CLOSE = 5.0
# A feed's events: one whose sealed value weighs more than this goes by reference, never in its message (each instance's
# ear hears every identity's, and Valkey drops an ear that falls 32 MiB behind); one so kept lives a minute, for the
# instances whose viewers read it, and what one identity keeps so weighs EVENTS_BYTES at most.
FEED_INLINE = 64 * 1024
# The events that name a batch of the Live Reviewer's queue by its id alone: a new one ("pending"), and one whose
# previews were made again while it waits ("updated"). The queue keeps the batch, sealed: an instance with viewers
# reads it there.
_BY_BATCH: Final = ("pending", "updated")
# The events heard behind a "pending" whose batch is being read in the queue's thread, waiting to be handed over in
# their order, at most: as many as a viewer's queue holds.
INBOX_LIMIT = 500
# A dashboard's session an instance found open, answered for from its memory (``ValkeySignIns.held``) for so long, as a
# token's identity is (``identities.CACHE_SECONDS``): one person's flood of dashboard requests takes no credentials'
# thread past its first. Its end, on any instance, is told on ``SIGNINS`` and drops it everywhere at once.
SESSION_HELD = 5.0
SESSIONS_HELD = 10_000  # sessions an instance holds so at most: the one used longest ago goes first
SIGNINS = "signins"  # the bus's channel of the sign-ins' ends
# A person's dashboard sessions kept at most, as an identity's AWS sessions are (``AWS_SESSIONS_KEPT``): opening one
# more ends the oldest. A person signs in once in each browser they use, and again each time a link is opened (``latet
# dashboard``), each session lasting twelve hours: ten are a few browsers and a day's sign-ins, as many as a minute of
# sign-in links opens (``machines.LOGIN_CODES``). Without it, ten links a minute for twelve hours kept 7,200 sessions,
# which used in turn, one request each every few seconds (``SESSION_HELD``), took some 1,400 lookups a second of the
# credentials' threads; ten take two.
SESSIONS_KEPT = 10
# Seconds a verdict on the administration log's chain is believed (the beat walks the chain once an hour): two of the
# checks, one of them missed; and how far ahead of an instance's clock another instance's verdict may be stamped (the
# bus drops the messages of instances whose clocks are further apart).
CHAIN_FRESH = 2 * 3600.0
CHAIN_SKEW = 60.0
EVENT_MS = 60_000
EVENTS_BYTES = 16 * 1024 * 1024
# What a key takes in Valkey beside its value's allocation: its name, its object, its entries in the dict of keys and
# of lifetimes, and its members in the identity's index and ledger (measured near 200 bytes).
ENTRY_BYTES = 256


def weighed(size: int) -> int:
    """What Valkey takes for a value of ``size`` bytes under a key of its own: its allocation, which jemalloc rounds up
    to a size class (four to each power of two: up to a quarter more, a value of 4 MiB and a byte taking 5 MiB), and
    ``ENTRY_BYTES`` for the rest. What the byte budgets count, so that they bound memory and not values (56 MiB of
    values counted were 70 MiB held)."""
    # The string's header and its end, which grow with it (Valkey takes the next one a few bytes short of each bound).
    header = 4 if size < 1 << 8 else 6 if size <= (1 << 16) - 8 else 10 if size <= (1 << 32) - 16 else 18
    allocated = size + header
    spacing = max(16, 1 << (allocated.bit_length() - 3))
    return -(-allocated // spacing) * spacing + ENTRY_BYTES


# ``weighed`` for the scripts that weigh what they keep (Lua 5.1: numbers are doubles, exact this far).
WEIGHED_LUA: Final = f"""
local function weighed(size)
  local header = 18
  if size < 256 then header = 4 elseif size <= 65528 then header = 6 elseif size <= 4294967280 then header = 10 end
  local allocated, bits = size + header, 0
  local left = allocated
  while left > 0 do left, bits = math.floor(left / 2), bits + 1 end
  local spacing = 16
  if bits - 3 > 4 then spacing = 2 ^ (bits - 3) end
  return math.ceil(allocated / spacing) * spacing + {ENTRY_BYTES}
end
"""


# The families of keys this module keeps for an identity, each named ``<family>:<identity>`` or begun so: what goes when
# the identity is removed (Ruling 21's purge). Its jobs' locks (``lock:<job>:<identity>``) end by themselves.
FAMILIES = (
    "conv", "convs", "convs-size", "approved", "reverse", "reverses", "response", "responses", "given", "givens",
    "givens-size", "checked", "sent", "aws", "aws-sessions", "event", "events",
)  # fmt: skip
# Seconds Valkey keeps a conversation or a text as it left past the week this module applies itself: a backstop, so
# that what nobody writes or reads any more goes even when nothing of the identity is written again to sweep it.
_BACKSTOP = 86400
SAY_EVERY = 60.0  # seconds between two lines of the log about one family's values that do not open
# What a memory in front of Valkey takes, at most, for one block the final check let leave (its keyed hash, the time,
# the memory's own entry): measured near 200 bytes.
_CHECKED_BYTES = 256
# What a conversation an instance holds takes, at most: its objects, then each entity id or approved text in its sets
# (the string and the set's slot; the marks hold each a second time: measured at 160 bytes at most a membership).
_CONVERSATION_BYTES = 1024
_ITEM_BYTES = 192
# What an instance's note of one conversation takes, at most (its name, the note, two digests, the memory's entry).
_NOTE_BYTES = 512
# What a write waiting for the space's writer takes beside its value and its record's name: its objects, its key in the
# queue, the queue's entry.
_WRITE_BYTES = 256
# What a sealed value raises when it does not open, or what it holds when it is not what was sealed: written by hand,
# moved from another key, sealed with another key.
_UNREADABLE = (InvalidTag, ValueError, TypeError, KeyError)
_EVENT_ID = re.compile(r"[0-9a-f]{32}")
# An event kept by reference, within its identity's budget: those whose minute is over are let go, the weights of the
# others (each in its member's name, each over FEED_INLINE: they are few; what Valkey takes for each, ``weighed``)
# summed, and past the budget nothing is kept. KEYS: the identity's events (their ids and weights, by the moment each
# ends), the event's own key. ARGV: the event's id, its sealed value, EVENT_MS, EVENTS_BYTES.
_REFER: Final = (
    WEIGHED_LUA
    + """
local t = redis.call('time')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('zremrangebyscore', KEYS[1], '-inf', now)
local used = 0
for _, member in ipairs(redis.call('zrange', KEYS[1], 0, -1)) do
  used = used + (tonumber(string.match(member, ':(%d+)$')) or 0)
end
local weight = weighed(string.len(ARGV[2]))
if used + weight > tonumber(ARGV[4]) then return 0 end
redis.call('set', KEYS[2], ARGV[2], 'px', ARGV[3])
redis.call('zadd', KEYS[1], now + tonumber(ARGV[3]), ARGV[1] .. ':' .. weight)
redis.call('pexpire', KEYS[1], ARGV[3])
return 1
"""
)


def _hash(text: str) -> str:
    """A plain SHA-256, 32 hexadecimal characters: the name of a random secret's record (a dashboard's code or session,
    an AWS session), which gives nothing of the secret away. A name made of anything else is keyed."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# The families the code names to ``UNREADABLE`` (this module's and the queue's), the set of the metrics' ``family``
# label (``latet_valkey_unreadable``): a family outside it is counted as ``other``.
UNREADABLE_FAMILIES = frozenset(
    {"answer", "approved", "audit-chain", "aws", "batch", "batches", "conv", "decision", "event", "feed", "given",
     "image", "polls", "response", "reverse", "seen", "signin", "viewers"}
)  # fmt: skip


class _Unreadable:
    """What was met in Valkey that does not open or cannot be read, by family (one of ``UNREADABLE_FAMILIES``, else
    ``other``): counted, and said in the log, the first at once and then one line a minute at most for each family, with
    how many since the line before. A line names the family alone: whoever writes Valkey can put anything in a value."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self._said: dict[str, float] = {}
        self._unsaid: dict[str, int] = {}

    def snapshot(self) -> dict[str, int]:
        """The counts by family, copied under the lock (the metrics read them while other threads add to them)."""
        with self._lock:
            return dict(self.counts)

    def met(self, family: str) -> None:
        family = family if family in UNREADABLE_FAMILIES else "other"
        now = time.monotonic()
        with self._lock:
            self.counts[family] = self.counts.get(family, 0) + 1
            self._unsaid[family] = self._unsaid.get(family, 0) + 1
            said = self._said.get(family)
            if said is not None and now - said < SAY_EVERY:
                return
            self._said[family], count, self._unsaid[family] = now, self._unsaid[family], 0
        log.warning(
            "a value of Valkey's %s family does not open or cannot be read: it counts as absent (written or moved "
            "there by hand, or sealed with another key)%s",
            family,
            f"; {count} since the line before" if count > 1 else "",
        )


UNREADABLE = _Unreadable()


def _drop_oldest(bus: Bus, index: str, count: int, records: Callable[[str], Iterable[str]]) -> None:
    """The ``count`` oldest of ``index`` taken out of it, with the keys ``records`` names for each."""
    popped = [member.decode(errors="replace") for member, _ in bus.client.zpopmin(index, count)]
    names = [name for member in popped for name in records(member)]
    if names:
        bus.client.delete(*names)


@dataclass(slots=True)
class _Write:
    """A record the space's writer keeps: a text of the model's, or the conversation of a response id, sealed. Its value
    goes under ``prefix + member`` for ``ttl`` seconds, listed in ``index`` by when it was written (``at``), and past
    ``most`` the oldest go with their records."""

    index: str
    prefix: str
    member: str
    value: bytes
    ttl: int
    most: int
    at: float

    @property
    def weight(self) -> int:
        """What it takes while it waits, at most."""
        return len(self.value) + len(self.prefix) + len(self.member) + _WRITE_BYTES


class _Writer:
    """What a space writes off its caller's thread (``ValkeyConversations.remember`` and ``name``).

    Each write is queued, the same record queued again replacing what waited for it, ``WRITES_QUEUED`` at most taking
    ``WRITES_QUEUED_BYTES`` at most (past either the oldest goes). A thread of the space's own writes them,
    ``WRITES_AT_ONCE`` to a round trip: it starts at the first write and ends once nothing came to write for
    ``WRITER_IDLE`` seconds. A round trip that Valkey fails goes back to the queue and is tried again ``WRITER_RETRY``
    seconds later, said once a minute by the error's type. What is dropped is counted (``dropped``) and said once a
    minute by number alone."""

    def __init__(self, bus: Bus, identity_id: str) -> None:
        self.bus, self.identity = bus, identity_id
        self._lock = threading.Condition()
        self._queued: OrderedDict[tuple[str, str], _Write] = OrderedDict()  # by list and member, the oldest first
        self._bytes = 0  # what the queued writes take
        self._sending = 0  # writes the thread took and Valkey has not answered for yet
        self._thread: threading.Thread | None = None
        self._closing = False
        self.dropped = 0  # writes dropped since the space opened
        self._dropped_said, self._failed_said = Paced(), Paced()

    @property
    def queued(self) -> int:
        """How many writes wait."""
        return len(self._queued)

    @property
    def weight(self) -> int:
        """What the writes that wait take."""
        return self._bytes

    def put(self, write: _Write) -> None:
        """``write`` queued, in place of what waited for its record: it never waits on Valkey."""
        with self._lock:
            key = (write.index, write.member)
            replaced = self._queued.pop(key, None)
            if replaced is not None:
                self._bytes -= replaced.weight
            self._queued[key] = write
            self._bytes += write.weight
            dropped = self._trim()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"latet-writer-{self.identity}", daemon=True)
                self._thread.start()
            else:
                self._lock.notify_all()
        self._say_dropped(dropped)

    def _trim(self) -> int:
        """Past the bounds, the oldest queued go (under the lock): how many went."""
        dropped = 0
        while self._queued and (len(self._queued) > WRITES_QUEUED or self._bytes > WRITES_QUEUED_BYTES):
            self._bytes -= self._queued.popitem(last=False)[1].weight
            dropped += 1
        self.dropped += dropped
        return dropped

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queued and not self._closing:
                    self._lock.wait_for(lambda: bool(self._queued) or self._closing, WRITER_IDLE)
                if not self._queued:  # nothing came, or the space closes with nothing left: the thread ends
                    self._thread = None
                    self._lock.notify_all()
                    return
                batch = [self._queued.popitem(last=False)[1] for _ in range(min(WRITES_AT_ONCE, len(self._queued)))]
                self._bytes -= sum(write.weight for write in batch)
                self._sending = len(batch)
            failed: Exception | None = None
            try:
                self._send(batch)
            except Exception as exc:  # Valkey did not answer: the batch waits again (or goes, the space closing)
                failed = exc
            dropped = 0
            with self._lock:
                self._sending = 0
                if failed is not None and self._closing:
                    dropped = len(batch)
                    self.dropped += dropped
                elif failed is not None:
                    for write in reversed(batch):  # back at the front, but for a record queued again since
                        key = (write.index, write.member)
                        if key not in self._queued:
                            self._queued[key] = write
                            self._queued.move_to_end(key, last=False)
                            self._bytes += write.weight
                    dropped = self._trim()
                self._lock.notify_all()
            self._say_dropped(dropped)
            if failed is not None:
                if said := self._failed_said.due():
                    log.warning(
                        "the writer of %s's model texts and response ids could not reach Valkey (%s): it tries again%s",
                        self.identity,
                        described(failed),
                        f"; {said} times since the line before" if said > 1 else "",
                    )
                with self._lock:
                    self._lock.wait_for(lambda: self._closing, WRITER_RETRY)

    def _send(self, batch: list[_Write]) -> None:
        """The batch in one round trip, each record kept and listed and each list rid of what expired; one more round
        trip for each list past its ceiling, whose oldest go with their records."""
        pipe = self.bus.client.pipeline()
        latest: dict[str, _Write] = {}
        for write in batch:
            pipe.set(write.prefix + write.member, write.value, ex=write.ttl)
            pipe.zadd(write.index, {write.member: write.at})
            latest[write.index] = write
        for index, write in latest.items():
            pipe.zremrangebyscore(index, "-inf", write.at - write.ttl)
            pipe.zcard(index)
            pipe.expire(index, write.ttl)
        counts = pipe.execute()[2 * len(batch) + 1 :: 3]
        for (index, write), count in zip(latest.items(), counts, strict=True):
            if int(count) > write.most:
                prefix = write.prefix
                _drop_oldest(self.bus, index, int(count) - write.most, lambda member, p=prefix: (p + member,))

    def _say_dropped(self, count: int) -> None:
        """Writes dropped: said by number alone, once a minute at most."""
        for _ in range(count):
            if said := self._dropped_said.due():
                log.warning(
                    "Valkey did not take %d of %s's writes of the model's texts and response ids in time: dropped "
                    "(where they would be read, those texts are masked anew)",
                    said,
                    self.identity,
                )

    def flush(self, timeout: float) -> bool:
        """Waits until what was queued is in Valkey, or dropped: True then; False when ``timeout`` ran out first."""
        with self._lock:
            return self._lock.wait_for(lambda: not self._queued and not self._sending, timeout)

    def close(self) -> None:
        """What is queued goes to Valkey, waited for ``WRITER_CLOSE`` seconds at most; what is left then is dropped. A
        write that comes after is written still, by a thread that ends as soon as nothing is left."""
        with self._lock:
            self._closing = True
            self._lock.notify_all()
            self._lock.wait_for(lambda: not self._queued and not self._sending, WRITER_CLOSE)
            dropped = len(self._queued)
            self._queued.clear()
            self._bytes = 0
            self.dropped += dropped
        self._say_dropped(dropped)


# A conversation's update, in one step with the identity's count of what its conversations hold (``convs-size:``: each
# one's fields and approved texts, and their total under ``#total``, which no conversation's name can be). It drops the
# conversations last used more than a week ago; writes the fields it is given and, while the conversation has room
# under ``ENTITIES_KEPT``, the entities, 256 to a call and never a call for one field (the conversation's size is read
# once, and what its writes add to it counted); counts the conversation as it is now, so that what expiry or a hand took
# from it is not counted twice; and drops the least recently used conversations, whole, while the identity is past its
# budget or its number. It answers how many of the entities it wrote, and how many fields the conversation has. It
# names the keys of those it drops from their prefixes: Latet's Valkey is one server.
_WRITE_CONVERSATION = """
local conv, approved, index, counts = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local name, now, stale = ARGV[1], ARGV[2], ARGV[3]
local life, room, budget = tonumber(ARGV[4]), tonumber(ARGV[5]), tonumber(ARGV[6])
local most, texts_kept, hashes, sets = tonumber(ARGV[7]), tonumber(ARGV[8]), ARGV[9], ARGV[10]
local plain, entities, texts = tonumber(ARGV[11]), tonumber(ARGV[12]), tonumber(ARGV[13])
local function drop(other)
  local held = tonumber(redis.call('HGET', counts, other) or '0')
  redis.call('HDEL', counts, other)
  redis.call('HINCRBY', counts, '#total', -held)
  redis.call('ZREM', index, other)
  redis.call('DEL', hashes .. other, sets .. other)
end
local function hset(first, count)
  local done, added = 0, 0
  while done < count do
    local size, named = math.min(256, count - done), {}
    for i = 1, 2 * size do named[i] = ARGV[first + 2 * done + i - 1] end
    added = added + redis.call('HSET', conv, unpack(named))
    done = done + size
  end
  return added
end
for _, other in ipairs(redis.call('ZRANGEBYSCORE', index, '-inf', stale, 'LIMIT', 0, 1000)) do
  if other ~= name then drop(other) end
end
hset(14, plain)
local fields = redis.call('HLEN', conv)
local written = math.min(entities, math.max(0, room - fields))
fields = fields + hset(14 + 2 * plain, written)
local first, done = 14 + 2 * (plain + entities), 0
while done < texts do
  local size, scored = math.min(256, texts - done), {}
  for i = 1, size do
    scored[2 * i - 1] = now
    scored[2 * i] = ARGV[first + done + i - 1]
  end
  redis.call('ZADD', approved, unpack(scored))
  done = done + size
end
redis.call('ZREMRANGEBYRANK', approved, 0, -(texts_kept + 1))
redis.call('EXPIRE', conv, life)
redis.call('EXPIRE', approved, life)
redis.call('ZADD', index, now, name)
local before = tonumber(redis.call('HGET', counts, name) or '0')
local holds = fields + redis.call('ZCARD', approved)
redis.call('HSET', counts, name, holds)
local total = redis.call('HINCRBY', counts, '#total', holds - before)
while total > budget or redis.call('ZCARD', index) > most do
  local oldest = redis.call('ZRANGE', index, 0, 0)[1]
  if not oldest or oldest == name then break end
  drop(oldest)
  total = tonumber(redis.call('HGET', counts, '#total') or '0')
end
redis.call('EXPIRE', index, life)
redis.call('EXPIRE', counts, life)
return {written, fields}
"""
# A text as it left kept (or, given an empty record, forgotten), in one step with the identity's count of what its texts
# take (``givens-size:``: what Valkey takes for each record, ``weighed``, and their total under ``#total``). It drops
# the texts last kept or read more than a week ago and what was kept for this text before, keeps it, and drops the
# least recently used while the identity is past its budget or its number. A record deleted by hand stays counted until
# its turn to go comes.
_KEEP_GIVEN: Final = (
    WEIGHED_LUA
    + """
local index, sizes = KEYS[1], KEYS[2]
local member, record, now, stale = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local life, budget, most, prefix = tonumber(ARGV[5]), tonumber(ARGV[6]), tonumber(ARGV[7]), ARGV[8]
local function drop(other)
  local size = tonumber(redis.call('HGET', sizes, other) or '0')
  redis.call('HDEL', sizes, other)
  redis.call('HINCRBY', sizes, '#total', -size)
  redis.call('ZREM', index, other)
  redis.call('DEL', prefix .. other)
end
for _, other in ipairs(redis.call('ZRANGEBYSCORE', index, '-inf', stale, 'LIMIT', 0, 1000)) do drop(other) end
drop(member)
if record ~= '' then
  redis.call('SET', prefix .. member, record, 'EX', life)
  redis.call('ZADD', index, now, member)
  redis.call('HSET', sizes, member, weighed(#record))
  redis.call('HINCRBY', sizes, '#total', weighed(#record))
end
local total = tonumber(redis.call('HGET', sizes, '#total') or '0')
while total > budget or redis.call('ZCARD', index) > most do
  local oldest = redis.call('ZRANGE', index, 0, 0)[1]
  if not oldest then break end
  drop(oldest)
  total = tonumber(redis.call('HGET', sizes, '#total') or '0')
end
redis.call('EXPIRE', index, life)
redis.call('EXPIRE', sizes, life)
return total
"""
)
# A text as it left, read: nothing for one last kept or read more than a week ago (its record outlives that by a day);
# otherwise it is used now, and kept so.
_READ_GIVEN = """
local index, record, sizes = KEYS[1], KEYS[2], KEYS[3]
local member, now, stale, life = ARGV[1], ARGV[2], tonumber(ARGV[3]), tonumber(ARGV[4])
local score = redis.call('ZSCORE', index, member)
if not score or tonumber(score) <= stale then return false end
local value = redis.call('GET', record)
if not value then return false end
redis.call('ZADD', index, 'XX', now, member)
redis.call('EXPIRE', record, life)
redis.call('EXPIRE', index, life)
redis.call('EXPIRE', sizes, life)
return value
"""


@dataclass(eq=False)
class _Kept:
    """A conversation as an instance holds it: the one object its requests change, the entities and approved texts the
    instance knows Valkey holds of it, which it does not write again (its marks), and its note (``_Note``), which lasts
    as long as the conversation is held whatever the memory of notes forgets: what the instance heard of a later project
    and folder is never forgotten while it holds an older one."""

    session: Session
    entities: set[str] = field(default_factory=set)
    approved: set[str] = field(default_factory=set)
    note: _Note | None = None

    def forget(self) -> None:
        """Valkey lost something the marks say it holds: they go, the read that found it short marks what Valkey still
        holds, and the next ``update`` writes what it lacks."""
        self.entities, self.approved = set(), set()


@dataclass(slots=True)
class _Note:
    """What an instance knows of a conversation in Valkey, small, of one size, and kept apart from the conversation it
    may hold (a heavy one it does not): how many fields the last read or write found there, whether its key is there,
    its project and folder there (a keyed digest) and their version, the project and folder of the latest version the
    instance knows, which stay when Valkey turns out to have lost them (a new one is announced against them), and the
    latest version the identity's channel said was written (``heard``)."""

    fields: int = 0
    key: bool = False
    where: bytes | None = None
    version: int = 0
    known: bytes | None = None
    heard: int = 0

    def forget(self) -> None:
        """Valkey lost what this says is there: written again at the next ``update`` (the version and the project and
        folder known stay: what an older one is judged by)."""
        self.key, self.where = False, None


def _kept_weight(kept: _Kept) -> int:
    """What a conversation an instance holds takes in its memory, at most."""
    session = kept.session
    items = len(session.entity_ids) + len(session.approved) + len(kept.entities) + len(kept.approved)
    texts = len(session.key) + len(session.cwd or "") + len(session.project_id or "")
    return _CONVERSATION_BYTES + _ITEM_BYTES * items + texts


class ValkeyConversations:
    """An identity's conversations, so that any instance restores what another masked: each a hash in Valkey named by a
    keyed hash of the conversation's key (``conv:<identity>:<name>``) that holds the key itself (``k``) and its project
    and folder (``w``, with the version they were written at), both sealed and bound to the conversation, and each
    entity it was shown (``e:<id>``) with a tag only the identity's key makes, bound to the conversation; its approved
    texts beside it (``approved:``, the latest ``APPROVED_KEPT``). A conversation is kept a week after its last use,
    each instance's reading or writing it renewing that (Valkey's own lifetimes a day longer, a backstop); it holds
    ``ENTITIES_KEPT`` fields at most (an entity past them is restored only where the conversation is held); and an index
    by last use (``convs:``) keeps at most ``CONVERSATIONS_KEPT`` of the identity's conversations and
    ``CONVERSATION_ENTRIES`` fields and approved texts in all, counted exactly beside it (``convs-size:``): past them
    the least recently used go, whole.

    Here, by the conversation's name, one object per conversation, which only grows (the engine's caches count its
    entities), with the entities and approved texts this instance knows Valkey holds, so that what is there is not
    written again; and apart, a small note of what the last read or write found (how many fields; the key; the project
    and folder and their version). An update sends only what Valkey lacks: what this instance's marks lack, or, for a
    conversation it does not hold (one heavier than ``CONVERSATIONS_LARGEST``, which ``ENTITIES_KEPT`` leaves room
    under), the entities the call just showed it; and no entity once the note says the conversation is full. When Valkey
    turns out to have lost some of it, the next ``update`` writes what it lacks.

    An entity id or a project that does not hold (written by hand, copied from another conversation) is not taken, nor
    a project and folder older than those this instance knows (written back: they do not open for it). One this instance
    takes from Valkey alone lowers no protection (``Projects.scope_for``). A project and folder that change are said on
    the identity's channel (``identity:<identity>``, signed: the conversation's name and the version, no folder), and an
    instance that holds an older version forgets the folder a client reported to it for that conversation and, until it
    reads the new one, writes back no older one it knows (``heard``).

    What the model wrote (``reverse:``, by a keyed hash of its restored form) and the conversation of each response id
    (``response:``, by a keyed hash of the id), each sealed and listed by when it was written, are written by the
    space's writer, off the caller's thread (``_Writer``): ``flush`` waits for them, ``close`` sends what is left."""

    def __init__(self, bus: Bus, identity_id: str, root_key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self.bus, self.identity, self._clock = bus, identity_id, clock
        self._sealer = Sealer(root_key, b"valkey")
        self._lock = threading.Lock()  # what this instance holds of its conversations, and its notes
        self._here: WeighedLRU[str, _Kept] = WeighedLRU(
            CONVERSATIONS_HERE, _kept_weight, CONVERSATIONS_HERE_BYTES, CONVERSATIONS_LARGEST
        )
        self._notes: LRU[str, _Note] = LRU(CONVERSATIONS_HERE)
        self._write = bus.client.register_script(_WRITE_CONVERSATION)
        self._index = bus.key(f"convs:{identity_id}")
        self._counts = bus.key(f"convs-size:{identity_id}")
        self._reverses = bus.key(f"reverses:{identity_id}")
        self._responses = bus.key(f"responses:{identity_id}")
        self._writer = _Writer(bus, identity_id)  # what the model wrote and the response ids, off the caller's thread
        # Told the entities a conversation was shown on another instance, the first time they are seen here: the space
        # loads those its vault does not know yet, so they are restored even before the instance hears of them.
        self.learn: Callable[[set[str]], None] | None = None

    @property
    def cached_bytes(self) -> int:
        """What the conversations this instance holds take, with its notes and the writes that wait, roughly (a
        server's budget of spaces counts it)."""
        return self._here.weight + len(self._notes) * _NOTE_BYTES + self._writer.weight

    def _name(self, key: str) -> str:
        return self._sealer.keyed(f"conversation\x00{key}").hex()[:32]

    def _digest(self, where: tuple[str | None, str | None]) -> bytes:
        """A project and folder as a note keeps them: a keyed digest, of one size whatever the folder's length."""
        return self._sealer.keyed("where\x00" + json.dumps(where))[:16]

    def _conv(self, name: str) -> str:
        return self.bus.key(f"conv:{self.identity}:{name}")

    def _approved(self, name: str) -> str:
        return self.bus.key(f"approved:{self.identity}:{name}")

    def _aad(self, kind: str, name: str) -> str:
        return f"{self.identity}:{kind}:{name}"

    def _tag(self, key: str, entity: str) -> bytes:
        """What says that the conversation ``key`` was shown ``entity``: only the identity's key makes it."""
        return self._sealer.keyed(f"entity\x00{key}\x00{entity}").hex().encode()

    def _opened(self, raw: bytes, kind: str, name: str, family: str) -> str | None:
        """A sealed text of this module's; None, counted and said, when it does not open for its place."""
        try:
            return self._sealer.open(raw, self._aad(kind, name)).decode()
        except _UNREADABLE:
            UNREADABLE.met(family)
            return None

    def _where(self, name: str, raw: bytes) -> tuple[str | None, str | None, int] | None:
        """A conversation's project, folder and their version; None when what is there does not open for it."""
        try:
            data = self._sealer.open_json(raw, self._aad("where", name))
            project, cwd, version = data["p"], data["c"], data["v"]
        except _UNREADABLE:
            UNREADABLE.met("conv")
            return None
        if not all(part is None or isinstance(part, str) for part in (project, cwd)) or type(version) is not int:
            UNREADABLE.met("conv")
            return None
        return project, cwd, version

    def _merge(
        self, kept: _Kept, note: _Note, name: str, fields: Mapping[bytes, bytes], approved: Iterable[bytes]
    ) -> set[str]:
        """What Valkey holds of a conversation, added to the object this instance holds of it and to its note (under
        the lock, but for a list's own objects); answers the entities new here. An entity is taken when its tag holds,
        each checked once here; a project and folder written since by another instance, at a later version, replace
        this one's, and an older one counts as unreadable. The marks are forgotten when the hash lacks a field they say
        is there, and those of the approved texts when none is left while they say some are."""
        session = kept.session
        present: set[str] = set()
        where: tuple[str | None, str | None, int] | None = None
        has_key, texts = False, list(approved)
        for raw, value in fields.items():
            try:
                found = raw.decode()
            except UnicodeDecodeError:
                UNREADABLE.met("conv")
                continue
            if found.startswith("e:"):
                entity = found[2:]
                if entity in kept.entities or hmac.compare_digest(value, self._tag(session.key, entity)):
                    present.add(entity)
                else:  # written by hand, or copied from another conversation
                    UNREADABLE.met("conv")
            elif found == "w":
                where = self._where(name, value)
                if where is not None and where[2] < note.version:  # older than what this instance knows: written back
                    UNREADABLE.met("conv")
                    where = None
            elif found == "k":
                has_key = True
        if (note.key and not has_key) or (note.where is not None and where is None) or kept.entities - present:
            kept.forget()  # gone, or short of what this instance wrote: the next update writes what it lacks
            note.forget()
        if kept.approved and not texts:  # its approved texts gone (expired, deleted): written again at the next update
            kept.approved = set()
        note.key, note.fields = note.key or has_key, len(fields)
        if where is not None:
            project, cwd, version = where
            digest, later = self._digest((project, cwd)), where[2] > note.version
            if later or (session.project_id, session.cwd) == (None, None):  # written since elsewhere, or new here
                session.project_id, session.cwd = project, cwd
            note.where, note.version = digest, version
            if later or note.known is None:
                note.known = digest
        new = present - session.entity_ids
        session.add(new)
        kept.entities |= present
        for raw in texts:
            try:
                text = raw.decode()
            except UnicodeDecodeError:
                UNREADABLE.met("approved")
                continue
            session.approved.add(text)
            kept.approved.add(text)
        return new

    def session(self, key: str) -> Session:
        name, life = self._name(key), CONVERSATION_TTL + _BACKSTOP
        with self._lock:
            kept = self._here.get(name)
            if kept is None or kept.session.key != key:
                kept = _Kept(Session(key))
                self._here.put(name, kept)
            # The note the memory of notes holds, else the one the conversation held here kept (S-09): what this
            # instance heard of a later project and folder stays with the older one it holds.
            note = self._notes.get(name) or kept.note
            if note is None:
                note = _Note()
            kept.note = note
            self._notes.put(name, note)
        conv, approved = self._conv(name), self._approved(name)
        pipe = self.bus.client.pipeline()
        pipe.hgetall(conv)
        pipe.zrevrange(approved, 0, APPROVED_KEPT - 1)
        pipe.expire(conv, life)  # in use: kept a week from now, wherever it was written
        pipe.expire(approved, life)
        pipe.zadd(self._index, {name: self._clock()}, xx=True)
        pipe.expire(self._index, life)
        pipe.expire(self._counts, life)
        fields, texts, *_ = pipe.execute()
        with self._lock:
            new = self._merge(kept, note, name, fields, texts)
            self._here.put(name, kept)  # weighed again: it may have grown
            self._notes.put(name, note)
        if new and self.learn is not None:
            self.learn(new)
        return kept.session

    def update(self, session: Session, shown: Iterable[str] = ()) -> None:
        key = session.key
        name, now = self._name(key), self._clock()
        with self._lock:
            kept = self._here.get(name)
            if kept is not None and kept.session is not session:
                kept = None  # no marks of it here
            note = self._notes.get(name) or (kept.note if kept is not None else None)
            # Neither memory knows the conversation any more (it left both while its request ran): nothing here says
            # at which version its project and folder were read, or whether a later one was heard of, so they are not
            # written (S-09); its next read and update do.
            knows = note is not None
            if note is None:
                note = _Note()
            if kept is not None:
                entities = [e for e in tuple(session.entity_ids) if e not in kept.entities]
                approved = [text for text in tuple(session.approved) if text not in kept.approved]
            else:  # what the call just showed it, or else its approved texts
                entities = list(dict.fromkeys(shown))
                approved = [] if entities else list(tuple(session.approved))
            if note.fields >= ENTITIES_KEPT:
                entities = []  # full when last read or written: an entity past it is restored where it is held
            where = (session.project_id, session.cwd)
            digest = self._digest(where) if where != (None, None) else None
            write_where = knows and digest is not None and digest != note.where
            if write_where and digest == note.known and note.heard > note.version:
                write_where = False  # what it knew, over a later one it heard of and never read: no write-back
            write_key = not note.key
            # Later than any this instance read or heard of, and than its past.
            version = max(note.version + 1, note.heard + 1, int(now * 1000))
        if not (write_key or write_where or entities or approved):
            return
        plain: list[str | bytes] = []
        if write_key:
            plain += ["k", self._sealer.seal(key.encode(), self._aad("key", name))]
        if write_where:
            sealed = self._sealer.seal_json({"p": where[0], "c": where[1], "v": version}, self._aad("where", name))
            plain += ["w", sealed]
        tags: list[str | bytes] = []
        for entity in entities:
            tags += [f"e:{entity}", self._tag(key, entity)]
        written, fields = (
            int(n)
            for n in self._write(
                keys=[self._conv(name), self._approved(name), self._index, self._counts],
                args=[
                    name, now, now - CONVERSATION_TTL, CONVERSATION_TTL + _BACKSTOP, ENTITIES_KEPT,
                    CONVERSATION_ENTRIES, CONVERSATIONS_KEPT, APPROVED_KEPT, self.bus.key(f"conv:{self.identity}:"),
                    self.bus.key(f"approved:{self.identity}:"), len(plain) // 2, len(entities), len(approved), *plain,
                    *tags, *approved,
                ],
            )
        )  # fmt: skip
        with self._lock:
            note.key, note.fields = True, fields
            moved = write_where and digest != note.known  # a project or folder other than the one this replaces
            if write_where:
                note.where, note.version, note.known = digest, version, digest
            self._notes.put(name, note)
            if kept is not None:
                kept.entities.update(entities[:written])
                kept.approved.update(approved)
                kept.approved.intersection_update(session.approved)  # no more marks than the conversation has texts
                self._here.put(name, kept)  # weighed again
        if moved:  # the other instances' clients reported another folder: what they reported no longer holds
            self.bus.publish(f"identity:{self.identity}", {"kind": "where", "conversation": name, "version": version})

    def heard(self, message: Mapping[str, Any]) -> None:
        """What the identity's channel says (the server's ear hands each of its messages here): a conversation's
        project and folder written at a later version through another instance (``{"kind": "where", "conversation":
        <name>, "version": <int>}``, signed by the bus). An instance that holds an older one forgets the folder a
        client reported to it for that conversation: whatever Valkey says of it next (written back or deleted by its
        writer), what a project lowers waits for a client to report its folder here again (``Projects.scope_for``).
        The note keeps the version: until the instance reads it, ``update`` writes back no older project and folder it
        knows, and writes a new one at a later version. Other messages are not this store's. No round trip: for the
        event loop."""
        if message.get("kind") != "where":
            return
        name, version = message.get("conversation"), message.get("version")
        if not isinstance(name, str) or type(version) is not int:
            return
        with self._lock:
            kept, note = self._here.get(name), self._notes.get(name)
            if note is None and kept is not None:  # the memory of notes forgot it: the conversation held kept its own
                note = kept.note
            if note is not None:
                note.heard = max(note.heard, version)
            if kept is not None and (note is None or note.version < version):
                kept.session.reported = None

    def sessions(self) -> list[Session]:
        """The conversations of the identity, the most recently used first, each with the entities it was shown (what
        the vault page counts), its project and its folder; one whose key does not open is left out."""
        found = self.bus.client.zrevrange(self._index, 0, SESSIONS_LISTED - 1)
        names = [name.decode(errors="replace") for name in found]
        if not names:
            return []
        pipe = self.bus.client.pipeline()
        for name in names:
            pipe.hgetall(self._conv(name))
        out = []
        for name, fields in zip(names, pipe.execute(), strict=True):
            raw = fields.get(b"k")
            key = self._opened(raw, "key", name, "conv") if raw is not None else None
            if key is None:
                continue
            kept = _Kept(Session(key))
            self._merge(kept, _Note(), name, fields, ())
            out.append(kept.session)
        return out

    def remember(self, key: bytes, masked: str) -> None:
        member = key.hex()
        sealed = self._sealer.seal(masked.encode(), self._aad("reverse", member))
        self._writer.put(
            _Write(self._reverses, self._reverse(""), member, sealed, REVERSE_TTL, REVERSE_KEPT, self._clock())
        )

    def recall(self, key: bytes) -> str | None:
        member = key.hex()
        pipe = self.bus.client.pipeline()
        pipe.getex(self._reverse(member), ex=REVERSE_TTL)
        pipe.zadd(self._reverses, {member: self._clock()}, xx=True)
        found, _ = pipe.execute()
        return self._opened(found, "reverse", member, "reverse") if found is not None else None

    def _reverse(self, member: str) -> str:
        return self.bus.key(f"reverse:{self.identity}:{member}")

    def _response(self, member: str) -> str:
        return self.bus.key(f"response:{self.identity}:{member}")

    def _response_member(self, response_id: str) -> str:
        return self._sealer.keyed(f"response\x00{response_id}").hex()[:32]

    def _response_key(self, response_id: str) -> str:
        return self._response(self._response_member(response_id))

    def name(self, response_id: str, key: str) -> None:
        member = self._response_member(response_id)
        sealed = self._sealer.seal(key.encode(), self._aad("response", member))
        self._writer.put(
            _Write(self._responses, self._response(""), member, sealed, RESPONSE_TTL, RESPONSES_KEPT, self._clock())
        )

    def named(self, response_id: str) -> str | None:
        member = self._response_member(response_id)
        found = self.bus.client.get(self._response(member))
        return self._opened(found, "response", member, "response") if found is not None else None

    def flush(self, timeout: float = WRITER_CLOSE) -> bool:
        """Waits until what ``remember`` and ``name`` queued is in Valkey, or dropped: True then; False when ``timeout``
        ran out first. It waits: never on the event loop."""
        return self._writer.flush(timeout)

    def close(self) -> None:
        """The space closes: what ``remember`` and ``name`` queued goes to Valkey, waited for ``WRITER_CLOSE`` seconds
        at most (what is left then is dropped, counted and said). It waits: never on the event loop."""
        self._writer.close()


class ValkeySentBlocks:
    """Which blocks each of an identity's conversations recorded already: one sorted set for the identity, each member
    a keyed hash of the conversation and the block, scored by when it was last recorded; a week, ``SENT_KEPT`` at
    most."""

    def __init__(self, bus: Bus, identity_id: str, root_key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self.bus, self.identity, self._clock = bus, identity_id, clock
        self._sealer = Sealer(root_key, b"valkey")
        self._key = bus.key(f"sent:{identity_id}")

    def first(self, session: str, block: str) -> bool:
        member = self._sealer.keyed(f"sent\x00{session}\x00{block}").hex()
        now = self._clock()
        pipe = self.bus.client.pipeline()
        pipe.zadd(self._key, {member: now})
        pipe.zremrangebyscore(self._key, "-inf", now - SENT_TTL)
        pipe.zremrangebyrank(self._key, 0, -(SENT_KEPT + 1))
        pipe.expire(self._key, SENT_TTL)
        added, *_ = pipe.execute()
        return bool(added)


def _left_data(left: Left) -> dict[str, Any]:
    return {
        "t": left.text,
        "x": left.exact,
        "r": [
            [r.start, r.end, r.original, r.replacement, r.etype, r.eclass.name, r.rule, r.policy.value, r.entity_id]
            for r in left.replacements
        ],
    }


def _left(data: Any) -> Left:
    """The text as it left that a record holds. Raises KeyError, TypeError or ValueError for what is not one."""
    replacements = tuple(
        Replacement(
            int(s), int(e), str(o), str(m), str(t), EntityClass[c], str(u), Policy(p), None if i is None else str(i)
        )
        for s, e, o, m, t, c, u, p, i in data["r"]
    )
    text = data["t"]
    if not isinstance(text, str):
        raise TypeError("a text as it left is a text")
    return Left(text, replacements, bool(data["x"]))


@dataclass(frozen=True, slots=True)
class _Read:
    """A text as it left in an instance's memory: when it was read from Valkey or kept there, and how large its sealed
    record is."""

    left: Left
    at: float
    size: int


def _read_weight(read: _Read) -> int:
    """What a text as it left takes in an instance's memory, roughly, and never less than its sealed record: so that
    the memory keeps no text that Valkey would not."""
    left = read.left
    held = sys.getsizeof(left.text) + sum(
        sys.getsizeof(r) + sys.getsizeof(r.original) + sys.getsizeof(r.replacement) for r in left.replacements
    )
    return max(held, read.size)


class ValkeyGiven:
    """What a conversation's texts were when they left, and what the final check let leave, for every instance.

    Each text sealed with the identity's root key and bound to the mediator's key it is kept under
    (``given:<identity>:<key>``), each replacement without its value (its entity's id is there; where it has none, a
    keyed hash of the value, which is all the dump guard counts); none larger than ``GIVEN_LARGEST`` (masked anew when
    sent again); listed by when it was last kept or read (``givens:``), ``GIVEN_KEPT`` at most and ``GIVEN_BYTES`` in
    all, the least recently used going first. What they take is counted beside them, exactly (``givens-size:``, what
    Valkey takes for each record, ``weighed``, changed in the one step that keeps or drops it), so that expiry and
    deletions cannot make the count drift; a text is gone a week after its last use, this module's own count of time,
    its record kept by Valkey a day longer as a backstop. The blocks the final check let leave are one sorted set for
    the identity (``checked:``), each member a keyed hash of the conversation and the block, which nobody makes without
    the key and which matches nothing in another conversation; ``CHECKED_KEPT`` at most, a week after their last use.

    A memory in front keeps the texts this instance read or kept, and the blocks it knows left (a block that left stays
    left: asked again, it costs no round trip), each asked of Valkey again after ``HERE_FOR``."""

    def __init__(self, bus: Bus, identity_id: str, root_key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self.bus, self.identity, self._clock = bus, identity_id, clock
        self._sealer = Sealer(root_key, b"valkey")
        self._here: WeighedLRU[bytes, _Read] = WeighedLRU(GIVEN_HERE, _read_weight, GIVEN_HERE_BYTES, GIVEN_LARGEST)
        self._checked: LRU[bytes, float] = LRU(CHECKED_HERE)  # a block's keyed hash → when Valkey said it left
        self._keep = bus.client.register_script(_KEEP_GIVEN)
        self._read = bus.client.register_script(_READ_GIVEN)
        self._index = bus.key(f"givens:{identity_id}")
        self._sizes = bus.key(f"givens-size:{identity_id}")
        self._blocks = bus.key(f"checked:{identity_id}")

    @property
    def cached_bytes(self) -> int:
        """What the memory in front takes, roughly (a server's budget of spaces counts it)."""
        return self._here.weight + len(self._checked) * _CHECKED_BYTES

    def _record(self, member: str) -> str:
        return self.bus.key(f"given:{self.identity}:{member}")

    def _aad(self, member: str) -> str:
        return f"{self.identity}:given:{member}"

    def _kept(self, left: Left) -> Left:
        """``left`` as it is kept: each replacement without its value."""
        return Left(
            left.text,
            tuple(
                replace(r, original="" if r.entity_id else self._sealer.keyed(f"value\x00{r.original}").hex()[:32])
                for r in left.replacements
            ),
            left.exact,
        )

    def masked(self, key: bytes) -> Left | None:
        now = self._clock()
        found = self._here.get(key)
        if found is not None and now - found.at < HERE_FOR:
            return found.left
        member = key.hex()
        keys, life = [self._index, self._record(member), self._sizes], GIVEN_TTL + _BACKSTOP
        raw = self._read(keys=keys, args=[member, now, now - GIVEN_TTL, life])
        if raw is None:
            return None
        try:
            left = _left(self._sealer.open_json(raw, self._aad(member)))
        except _UNREADABLE:  # written by hand, or moved from another key: a text as not left
            UNREADABLE.met("given")
            return None
        self._here.put(key, _Read(left, now, len(raw)))
        return left

    def keep(self, key: bytes, left: Left) -> None:
        kept, member, now = self._kept(left), key.hex(), self._clock()
        sealed = self._sealer.seal_json(_left_data(kept), self._aad(member))
        # A larger one is not kept, and what was kept for the text before goes too: this replaced it.
        record = sealed if len(sealed) <= GIVEN_LARGEST else b""
        self._keep(
            keys=[self._index, self._sizes],
            args=[
                member, record, now, now - GIVEN_TTL, GIVEN_TTL + _BACKSTOP, GIVEN_BYTES, GIVEN_KEPT,
                self.bus.key(f"given:{self.identity}:"),
            ],
        )  # fmt: skip
        self._here.put(key, _Read(kept, now, len(sealed)))  # in place of what it held; nothing when too large

    def _member(self, session: str, key: bytes) -> bytes:
        return self._sealer.keyed(f"checked\x00{session}\x00{key.hex()}")

    def checked(self, session: str, key: bytes) -> bool:
        member, now = self._member(session, key), self._clock()
        at = self._checked.get(member)
        if at is not None and now - at < HERE_FOR:
            return True
        pipe = self.bus.client.pipeline()
        pipe.zscore(self._blocks, member.hex())
        pipe.zadd(self._blocks, {member.hex(): now}, xx=True)  # in use: kept a week from now
        score, _ = pipe.execute()
        if score is None:
            return False
        self._checked.put(member, now)
        return True

    def keep_checked(self, session: str, keys: Iterable[bytes]) -> None:
        members = {self._member(session, key) for key in keys}
        if not members:
            return
        now = self._clock()
        pipe = self.bus.client.pipeline()
        pipe.zadd(self._blocks, {member.hex(): now for member in members})
        pipe.zremrangebyscore(self._blocks, "-inf", now - GIVEN_TTL)
        pipe.zremrangebyrank(self._blocks, 0, -(CHECKED_KEPT + 1))
        pipe.expire(self._blocks, GIVEN_TTL)
        pipe.execute()
        for member in members:
            self._checked.put(member, now)


class ValkeyFeed:
    """A dashboard's live feed that every instance hears: published on Valkey, sealed with the identity's key and bound
    to its channel, and handed to this instance's viewers when it comes back from Valkey (``heard``), on this instance
    too. A message that does not open (another identity's, another channel's) reaches no viewer.

    Every message stays small, whatever one identity sends: each instance's ear hears every identity's, and Valkey drops
    an ear that falls 32 MiB behind, closing every space of that instance. An event whose sealed value weighs more than
    ``FEED_INLINE`` goes by reference: kept ``EVENT_MS`` under a key of its own (``event:<identity>:<id>``), sealed for
    that key, within the identity's ``EVENTS_BYTES`` (past it, the event reaches no viewer, as a stalled viewer misses
    events), the message naming it. A batch's "pending" event names the batch, whose record the queue keeps already
    (``batches``: the queue's ``fetch``, which opens it in the queue's thread in the identity's turn, never on the event
    loop: a record weighs 4 MiB). Either is read only on an instance where viewers watch. The events reach the viewers
    in the order heard: from a "pending" on, until what was heard behind it is handed over, a task of the feed's own
    hands them over one at a time (``_read``), ``INBOX_LIMIT`` waiting at most (past it, an event reaches no viewer, as
    a stalled viewer misses events), and ``close`` ends it with its space. A publish that Valkey fails is said in the
    log, never raised: the viewers miss that event, and the request that made it goes on."""

    def __init__(
        self,
        bus: Bus,
        channel: str,
        root_key: bytes,
        *,
        batches: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.bus, self.channel = bus, channel
        self._sealer = Sealer(root_key, b"valkey")
        self._queues: set[asyncio.Queue[Any]] = set()
        self._batches = batches
        identity = channel.partition(":")[2]
        self._events, self._event = bus.key(f"events:{identity}"), f"event:{identity}:"
        self._identity = identity
        self._refer = bus.client.register_script(_REFER)
        self._said: float | None = None  # when the log last said that an event reached no viewer
        self._unsaid = 0
        # The messages heard behind one whose batch is read in the queue's thread, in order, and what hands them over.
        self._inbox: collections.deque[Mapping[str, Any]] = collections.deque()
        self._reader: asyncio.Task[None] | None = None

    def subscribe(self) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        self._queues.discard(queue)

    def _event_key(self, event_id: str) -> str:
        return self.bus.key(self._event + event_id)

    def publish(self, payload: Any, event: str = "exchange") -> None:
        try:
            message = self._message(payload, event)
            if message is not None:
                self.bus.publish(self.channel, message)
        except Exception as exc:  # Valkey did not answer: the viewers miss this event
            self._missed(type(exc).__name__)

    def _message(self, payload: Any, event: str) -> dict[str, str] | None:
        """What goes on the channel for an event; None when it reaches no viewer (the identity's events by reference
        weigh as much as they may)."""
        if event in _BY_BATCH and self._batches is not None and isinstance(payload, Mapping):
            batch_id = payload.get("id")
            if isinstance(batch_id, str):
                return {event: batch_id}  # the queue keeps the batch: an instance with viewers reads it there
        data = {"event": event, "payload": payload}
        sealed = self._sealer.seal_json(data, self.channel)
        if len(sealed) <= FEED_INLINE:
            return {"feed": base64.b64encode(sealed).decode()}
        event_id = secrets.token_hex(16)
        key = self._event_key(event_id)
        sealed = self._sealer.seal_json(data, key)  # bound to its own key: copied under another, it does not open
        if not self._refer(keys=[self._events, key], args=[event_id, sealed, EVENT_MS, EVENTS_BYTES]):
            self._missed(f"the events kept by reference weigh {EVENTS_BYTES} bytes at most")
            return None
        return {"event": event_id}

    def _missed(self, why: str) -> None:
        """An event that reaches no viewer: said by why alone, never what it holds, once a minute at most."""
        now = time.monotonic()
        self._unsaid += 1
        if self._said is not None and now - self._said < SAY_EVERY:
            return
        self._said, count, self._unsaid = now, self._unsaid, 0
        log.warning(
            "an event of %s's dashboards reached no viewer (%s)%s",
            self._identity,
            why,
            f"; {count} since the line before" if count > 1 else "",
        )

    def heard(self, message: Mapping[str, Any]) -> None:
        """A message of the feed's channel, on the event loop (the ear): handed to the viewers here at once, but a
        "pending" whose batch is read in the queue's thread, and whatever is heard behind it until it is handed over,
        which the feed's own task hands over in order."""
        if not self._queues:  # nobody watches here: nothing is read
            return
        if not self._inbox and not self._read_elsewhere(message):
            self._handed(message)
            return
        if len(self._inbox) >= INBOX_LIMIT:
            self._missed(f"{INBOX_LIMIT} events wait for a batch to be read already")
            return
        self._inbox.append(message)
        if self._reader is None or self._reader.done():
            self._reader = asyncio.get_running_loop().create_task(self._read(), name="latet-feed")

    def _read_elsewhere(self, message: Mapping[str, Any]) -> bool:
        """Whether ``message`` names a batch the queue reads in its own thread."""
        return self._batches is not None and any(isinstance(message.get(event), str) for event in _BY_BATCH)

    async def _read(self) -> None:
        """The messages waiting, one at a time in the order heard, each handed to the viewers watching then: a batch a
        "pending" names read in the queue's thread meanwhile (decided since: none). Each stays first in the inbox until
        it is handed, so that what is heard while its batch is read waits behind it (``heard``), never overtakes it."""
        while self._inbox:
            message = self._inbox[0]
            try:
                if self._queues:  # else nobody watches here any more: nothing is read
                    await self._hand(message)
            finally:
                if self._inbox and self._inbox[0] is message:  # ``close`` may have dropped it meanwhile
                    self._inbox.popleft()

    async def _hand(self, message: Mapping[str, Any]) -> None:
        """``message`` handed to the viewers here, its batch read first in the queue's thread when it is a "pending"."""
        if not self._read_elsewhere(message):
            self._handed(message)
            return
        assert self._batches is not None  # ``_read_elsewhere`` says so
        event = next(event for event in _BY_BATCH if isinstance(message.get(event), str))
        try:
            batch = await self._batches(message[event])
        except Exception as exc:  # Valkey did not answer a read: the viewers here miss this event
            self._missed(type(exc).__name__)
            return
        if batch is not None:
            self._put((event, batch.summary()))

    def _handed(self, message: Mapping[str, Any]) -> None:
        """``message`` handed to the viewers here, read on the event loop (at most one small read of Valkey: R-7)."""
        try:
            item = self._item(message)
        except Exception as exc:  # Valkey did not answer a read: the viewers here miss this event
            self._missed(type(exc).__name__)
            return
        if item is not None:  # else another kind of message on the channel, or one that reaches no viewer
            self._put(item)

    def _put(self, item: Any) -> None:
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):  # a stalled viewer misses events, nothing more
                queue.put_nowait(item)

    def close(self) -> None:
        """Its space closes on this instance: the events waiting are dropped and the task that hands them over ends.
        On the event loop."""
        self._inbox.clear()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None

    def _item(self, message: Mapping[str, Any]) -> Any:
        """What a message gives the viewers here; None for none."""
        if (blob := message.get("feed")) is not None:
            return self._opened(lambda: base64.b64decode(blob, validate=True), self.channel, "feed")
        if isinstance(message.get("pending"), str):  # a feed the queue was not given to (another version's): said
            self._missed("a batch's pending event names it in the queue, which this feed is not given to read")
            return None
        if isinstance(event_id := message.get("event"), str) and _EVENT_ID.fullmatch(event_id):
            key = self._event_key(event_id)
            raw = self.bus.client.get(key)
            return self._opened(lambda: raw, key, "event") if raw is not None else None  # past its minute: none
        return None

    def _opened(self, sealed: Callable[[], bytes], aad: str, family: str) -> Any:
        try:
            data = self._sealer.open_json(sealed(), aad)
            event, payload = data["event"], data["payload"]
        except _UNREADABLE:
            UNREADABLE.met(family)
            return None
        return payload if event == "exchange" else (event, payload)


class ChainVerdicts:
    """The beat's verdict on the administration log's chain (``AuditLog.verify``, walked once an hour on one instance),
    kept in Valkey for every instance's status to read. It is sealed with a key derived from the master key, bound to
    its own Valkey key, and carries the moment it was checked: whoever has Valkey's password makes no verdict, moves
    none, and can write back an old one only within its freshness. What the status says of it (``read``): ``ok`` or
    ``broken`` while it is fresh, ``CHAIN_FRESH`` seconds (two of the hourly checks); ``unknown`` when there is none,
    when it does not open, when it is older than that or its moment is ahead of this clock by more than the bus allows
    instances to differ. Never ``ok`` in those cases. It is the beat's word: ``GET admin/log/verify`` walks the chain
    itself.

    An instance keeps one (``Server.chain``), which remembers the newest fresh verdict it kept or read: a verdict older
    than that one, written back by whoever has Valkey's password, is never believed again, and taking the verdict away
    or writing over it what does not open hides nothing that instance read, while it is fresh. What that leaves: an
    instance that never read the newer verdict (one started since) believes an older one written back while it is
    fresh. Safe across threads."""

    def __init__(self, bus: Bus, master: MasterKey, *, clock: Callable[[], float] = time.time) -> None:
        self.bus, self._clock = bus, clock
        self._sealer = master.sealer(b"audit-chain")
        self._key = bus.key("audit:chain")
        self._lock = threading.Lock()
        self._newest: tuple[float, bool] | None = None  # the newest fresh verdict kept or read: its moment, whether ok

    def keep(self, check: ChainCheck) -> None:
        """The verdict of a walk of the chain just made: whether it holds, and where it breaks (an entry's number)."""
        verdict = {"ok": bool(check.ok), "at": self._clock(), "broken_at": check.broken_at}
        self.bus.client.set(self._key, self._sealer.seal_json(verdict, self._key), ex=int(CHAIN_FRESH) + 60)
        self._seen(verdict["at"], verdict["ok"])

    def read(self) -> Literal["ok", "broken", "unknown"]:
        raw = self.bus.client.get(self._key)
        if raw is not None:
            try:
                data = self._sealer.open_json(raw, self._key)
                ok, at = data["ok"], float(data["at"])
            except _UNREADABLE:  # written by hand, moved from another key, or sealed by another server
                UNREADABLE.met("audit-chain")
            else:
                if type(ok) is bool and self._fresh(at):
                    self._seen(at, ok)  # one older than the newest read so far changes nothing
        with self._lock:
            newest = self._newest
        if newest is None or not self._fresh(newest[0]):
            return "unknown"
        return "ok" if newest[1] else "broken"

    def _fresh(self, at: float) -> bool:
        """Whether a verdict of the moment ``at`` is believed now: not older than ``CHAIN_FRESH``, nor ahead of this
        clock by more than instances may differ."""
        age = self._clock() - at
        return math.isfinite(age) and -CHAIN_SKEW <= age <= CHAIN_FRESH

    def _seen(self, at: float, ok: bool) -> None:
        """A fresh verdict of the moment ``at``: the newest from now on unless one newer was kept or read already."""
        with self._lock:
            if self._newest is None or at >= self._newest[0]:
                self._newest = (at, ok)


class ValkeySignIns:
    """The dashboard's sign-ins for every instance: one-time codes and sessions by the hash of their secret, each with
    its subject and when it ends, which the server's clock judges (Valkey forgets them a little after). A session ends
    twelve hours after it was opened, whatever its use: a browser drops its cookie then anyway, and a copied cookie does
    not last longer. Each record is sealed with a key derived from the master key and bound to its own Valkey key, so
    that whoever can write Valkey makes no session and moves none; a record he saved and writes back opens again within
    its own limits (a code its two minutes, a session its twelve hours), and disabling or removing the identity holds
    all the same through the dashboard's check of it. A code carries, sealed with it, the keyed hash of the token that
    asked for it (``issue_code``'s ``token``), for the server's sign-in to open a session only while that token is there
    (``redeemed``): one revoked meanwhile, whatever any instance's memory held of it, opens nothing. Each subject's
    records are listed, by when they end, for ending them all at once; those that ended leave the list as the next is
    listed."""

    def __init__(self, bus: Bus, master: MasterKey, *, clock: Callable[[], float] = time.time) -> None:
        self.bus, self._clock = bus, clock
        self._sealer = master.sealer(b"sign-ins")
        self.code_redeemed: Callable[[str], None] | None = None  # after GETDEL answers, before opening its record
        self._lock = threading.Lock()
        # The sessions this instance found open, by their record's name: their subject, and until when (as
        # ``time.monotonic`` reads) its memory answers for them without asking Valkey (``held``). A session's or a
        # subject's end, on any instance, drops them at once (``end``, ``heard``); a read made while one was dropped
        # keeps nothing (``_generation``), as the identities' memory does.
        self._held: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._generation = 0

    @property
    def generation(self) -> int:
        """Counted by every ``forget``: a session's lookup begun before it may answer what an end has made old."""
        return self._generation

    def held(self, sid: str) -> str | None:
        """The subject of a session this instance found open in the last ``SESSION_HELD`` seconds, from its memory
        alone: no store is asked (the gate, on the event loop). None when it does not hold it."""
        if not sid:
            return None
        name, now = _hash(sid), time.monotonic()
        with self._lock:
            found = self._held.get(name)
            if found is None or found[1] <= now:
                return None
            self._held.move_to_end(name)
            return found[0]

    def heard(self, message: Mapping[str, Any]) -> None:
        """A sign-in's end told on the bus's ``signins`` channel by an instance, this one too: what this instance's
        memory held of it dropped (``session``: a record's name; ``subject``: every session of an identity)."""
        session, subject = message.get("session"), message.get("subject")
        self.forget(
            session=session if isinstance(session, str) else None, subject=subject if isinstance(subject, str) else None
        )

    def forget(self, *, session: str | None = None, subject: str | None = None) -> None:
        """Drop from this instance's memory the session of that record's name, and every session of ``subject``; a
        read made meanwhile keeps nothing."""
        with self._lock:
            self._generation += 1
            if session is not None:
                self._held.pop(session, None)
            if subject is not None:
                for name in [name for name, found in self._held.items() if found[0] == subject]:
                    del self._held[name]

    def _hold(self, name: str, subject: str, generation: int) -> None:
        """A session found open, held ``SESSION_HELD`` seconds, unless something was dropped since its read began."""
        with self._lock:
            if self._generation != generation:
                return
            self._held[name] = (subject, time.monotonic() + SESSION_HELD)
            self._held.move_to_end(name)
            while len(self._held) > SESSIONS_HELD:
                self._held.popitem(last=False)

    def index(self, subject: str) -> str:
        """The name, under the bus's prefix, of the list of ``subject``'s records: a keyed hash of the subject (an
        identity's id has 50 bits, which a plain hash would give back), so that whoever reads Valkey's keys learns from
        them nobody who holds a dashboard."""
        return f"signin:subject:{self._sealer.keyed(f'subject{chr(0)}{subject}').hex()[:32]}"

    def _put(self, kind: str, secret: str, subject: str, ttl: float, token: str | None = None) -> None:
        key = self.bus.key(f"signin:{kind}:{_hash(secret)}")
        index = self.bus.key(self.index(subject))
        now = self._clock()
        record: dict[str, Any] = {"s": subject, "e": now + ttl} | ({"t": token} if token is not None else {})
        pipe = self.bus.client.pipeline()
        pipe.set(key, self._sealer.seal_json(record, key), ex=int(ttl) + 60)
        pipe.zremrangebyscore(index, "-inf", f"({now}")  # those that ended
        pipe.zadd(index, {key: now + ttl})
        pipe.expire(index, int(SESSION_TTL) + 60)  # longer than any record it lists
        pipe.execute()

    def _record(self, key: str, raw: bytes | None) -> tuple[str, str | None] | None:
        """The subject a record names while it lasts, and the token it is bound to (None: none)."""
        if raw is None:
            return None
        try:
            data = self._sealer.open_json(raw, key)
            subject, end, token = data["s"], float(data["e"]), data.get("t")
        except _UNREADABLE:  # written by hand, moved from another key, or sealed by another server: no session
            UNREADABLE.met("signin")
            return None
        if not isinstance(subject, str) or not (token is None or isinstance(token, str)):
            UNREADABLE.met("signin")
            return None
        return (subject, token) if end >= self._clock() else None

    def issue_code(self, subject: str, *, token: str | None = None) -> str:
        """A one-time code for ``subject``, good for two minutes; ``token``: the keyed hash of the token that asked for
        it (``Identities.secret_digest``), sealed with it."""
        code = secrets.token_urlsafe(24)
        self._put("code", code, subject, CODE_TTL, token)
        return code

    def redeemed(self, code: str) -> tuple[str, str | None] | None:
        """The code's subject and the token that asked for it (None: none), once; None for a code unknown, used or
        late."""
        if not code:
            return None
        key = self.bus.key(f"signin:code:{_hash(code)}")
        raw = self.bus.client.getdel(key)
        if self.code_redeemed is not None:
            self.code_redeemed(code)
        return self._record(key, raw)

    def redeem(self, code: str) -> str | None:
        found = self.redeemed(code)
        return found[0] if found is not None else None

    def open(self, subject: str) -> str:
        """A session for ``subject``, the newest of the ``SESSIONS_KEPT`` it keeps (``_keep_newest``)."""
        sid = secrets.token_urlsafe(32)
        self._put("session", sid, subject, SESSION_TTL)
        self._keep_newest(subject)
        return sid

    def _keep_newest(self, subject: str) -> None:
        """``subject``'s newest ``SESSIONS_KEPT`` sessions kept, its codes left as they are: the older ended in Valkey,
        dropped from this instance's memory and told on the bus (``signins``), as ``end`` does, so that no instance
        answers for them from its memory."""
        index = self.bus.key(self.index(subject))
        prefix = self.bus.key("signin:session:").encode()
        sessions = [m for m in self.bus.client.zrange(index, 0, -1) if m.startswith(prefix)]  # by end: oldest first
        older = sessions[:-SESSIONS_KEPT] if len(sessions) > SESSIONS_KEPT else []
        if not older:
            return
        pipe = self.bus.client.pipeline()
        pipe.delete(*older)
        pipe.zrem(index, *older)
        pipe.execute()
        for member in older:
            name = member[len(prefix) :].decode()
            self.forget(session=name)
            self._tell({"session": name})

    def subject_of(self, sid: str) -> str | None:
        """The session's subject while its twelve hours from its opening last: read, and nothing written (a look neither
        renews it nor ends it); held in this instance's memory for ``SESSION_HELD`` seconds (``held``)."""
        if not sid:
            return None
        name = _hash(sid)
        with self._lock:
            generation = self._generation  # before anything is read
        key = self.bus.key(f"signin:session:{name}")
        found = self._record(key, self.bus.client.get(key))
        if found is None:
            return None
        self._hold(name, found[0], generation)
        return found[0]

    def end(self, sid: str | None = None, subject: str | None = None) -> None:
        """The session ``sid``, and every session and code of ``subject``, ended in Valkey; then dropped from this
        instance's memory and told on the bus (``signins``), so that every instance drops what it holds at once, as a
        token's revocation is told. A tell that fails is said in the log by its type: the others hold it
        ``SESSION_HELD`` seconds at most."""
        name = _hash(sid) if sid else None
        if name is not None:
            self.bus.client.delete(self.bus.key(f"signin:session:{name}"))
        if subject is not None:
            index = self.bus.key(self.index(subject))
            keys = list(self.bus.client.zrange(index, 0, -1))
            self.bus.client.delete(index, *keys)
        if name is None and subject is None:
            return
        self.forget(session=name, subject=subject)
        told: dict[str, str] = {"session": name} if name is not None else {}
        if subject is not None:
            told["subject"] = subject
        self._tell(told)

    def _tell(self, told: dict[str, str]) -> None:
        """A sign-in's end told on the bus (``signins``): every instance drops it from its memory. A tell that fails is
        said in the log by its type: the others hold it ``SESSION_HELD`` seconds at most."""
        try:
            self.bus.publish(SIGNINS, told)
        except Exception as exc:  # its words can name Valkey's address: its type alone
            log.warning(
                "a sign-in's end could not be told to the other instances (%s): they hold it %g seconds at most",
                type(exc).__name__,
                SESSION_HELD,
            )


class ValkeyAwsSessions:
    """The AWS credentials `latet claude` hands over for an identity's sessions, for every instance: each sealed with a
    key derived from the master key and bound to its own Valkey key, named by the hash of the session's id (who holds
    the id is given them; whoever reads Valkey learns no id). A session is kept an hour after its last use (the request
    that holds it asks every 15 seconds), never past the end of its credentials, which counts as none when it is not a
    number or further than ``AWS_LONGEST``; the server's clock judges. The identity's sessions are listed by last use,
    ``AWS_SESSIONS_KEPT`` at most, so that they can be ended at once, whatever instance does it (``drop_all``)."""

    def __init__(
        self, bus: Bus, master: MasterKey, identity_id: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.bus, self.identity, self._clock = bus, identity_id, clock
        self._sealer = master.sealer(b"aws-sessions")
        self._index = bus.key(f"aws-sessions:{identity_id}")

    def _key(self, member: str) -> str:
        return self.bus.key(f"aws:{self.identity}:{member}")

    @staticmethod
    def _end(expires: float | None, now: float) -> float | None:
        """When credentials end, as the store judges it: none for an end that is not a finite number, or that is further
        than ``AWS_LONGEST`` from now."""
        if expires is None or not math.isfinite(expires) or expires - now > AWS_LONGEST:
            return None
        return expires

    @staticmethod
    def _lifetime(end: float | None, now: float) -> int:
        """Seconds Valkey keeps a session after a use: ``HELD_TTL``, never past its end, and a minute more."""
        left = HELD_TTL if end is None else min(HELD_TTL, max(1.0, end - now))
        return int(left) + 60

    def add_session(self, credentials: AwsCredentials) -> str:
        session = secrets.token_hex(16)
        member, now = _hash(session), self._clock()
        end = self._end(credentials.expires, now)
        data = {"a": credentials.access_key, "s": credentials.secret_key, "t": credentials.token, "e": end}
        key = self._key(member)
        pipe = self.bus.client.pipeline()
        pipe.set(key, self._sealer.seal_json(data, key), ex=self._lifetime(end, now))
        pipe.zadd(self._index, {member: now})
        pipe.zremrangebyscore(self._index, "-inf", now - HELD_TTL)  # unused for an hour: gone
        pipe.zcard(self._index)
        pipe.expire(self._index, HELD_TTL + 60)
        count = int(pipe.execute()[3])
        if count > AWS_SESSIONS_KEPT:
            _drop_oldest(self.bus, self._index, count - AWS_SESSIONS_KEPT, lambda m: (self._key(m),))
        return session

    def session(self, session: str) -> AwsCredentials:
        member, now = _hash(session), self._clock()
        key = self._key(member)
        pipe = self.bus.client.pipeline()
        pipe.get(key)
        pipe.zscore(self._index, member)
        raw, used = pipe.execute()
        credentials = None
        if raw is not None:
            try:
                data = self._sealer.open_json(raw, key)
                token, end = data["t"], data["e"]
                credentials = AwsCredentials(
                    str(data["a"]),
                    str(data["s"]),
                    None if token is None else str(token),
                    None if end is None else float(end),
                )
            except _UNREADABLE:  # written by hand, or moved from another session's key
                UNREADABLE.met("aws")
        ended = credentials is not None and credentials.expires is not None and credentials.expires <= now
        if credentials is None or used is None or now - float(used) > HELD_TTL or ended:
            if raw is not None or used is not None:
                pipe = self.bus.client.pipeline()
                pipe.delete(key)
                pipe.zrem(self._index, member)
                pipe.execute()
            raise AwsError(
                "aws-session-gone",
                "the AWS credentials of this session are gone (they ended, or were dropped): start `latet claude` "
                "again",
            )
        pipe = self.bus.client.pipeline()
        pipe.expire(key, self._lifetime(credentials.expires, now))  # in use: kept
        pipe.zadd(self._index, {member: now}, xx=True)
        pipe.expire(self._index, HELD_TTL + 60)
        pipe.execute()
        return credentials

    def has_session(self, session: str) -> bool:
        try:
            self.session(session)
        except AwsError:
            return False
        return True

    def drop_session(self, session: str) -> bool:
        member = _hash(session)
        pipe = self.bus.client.pipeline()
        pipe.delete(self._key(member))
        pipe.zrem(self._index, member)
        dropped, _ = pipe.execute()
        return bool(dropped)

    def drop_all(self) -> int:
        """Every session of the identity dropped (its tokens revoked, the identity disabled or removed), wherever it
        runs: one read of the identity's list and one deletion, no scan of Valkey's keys. How many were there."""
        members = [member.decode(errors="replace") for member in self.bus.client.zrange(self._index, 0, -1)]
        pipe = self.bus.client.pipeline()
        if members:
            pipe.delete(*(self._key(member) for member in members))
        pipe.delete(self._index)
        return int(pipe.execute()[0]) if members else 0


class SpaceJobs:
    """An identity's jobs, each run on one instance at a time: a Valkey lock named for the job and the identity. Whoever
    writes Valkey can hold one for ever: the history's pruning then goes on without it once it was refused for a day
    (``Recorder``), pruning twice being harmless."""

    def __init__(self, bus: Bus, identity_id: str) -> None:
        self.bus, self.identity = bus, identity_id

    def hold(self, name: str, seconds: float) -> AbstractContextManager[bool]:
        return self.bus.lock(f"{name}:{self.identity}", seconds)
