"""What waits for an identity, for every instance of a Latet server (E0a's ``ApprovalsBackend``): the Live Reviewer's
batches, the decisions on them and the plugin's answers in Valkey, sealed with the identity's root key (their texts
hold values), each instance woken through the identity's ``approvals:<id>`` channel; the changes to its settings and
how they ended in PostgreSQL, sealed too. A batch's images and futures stay in the instance that opened it, which says
every few seconds that it still holds it: when that instance stops, the batch leaves the queue within ``HELD_MS``.

Whoever has Valkey's password reads and writes every key without the master key (Ruling 26's attacker). So each record
is sealed for its own key, a batch's, its decision's, its answer's and each of its images'
(``image:<id>:<batch>:<item>``): copied under another key it does not open, and no approval is moved onto another
batch, no picture onto another item. A decision is claimed before it is sealed: of two taken at once, the one that
loses is never sealed, nor sent. In the database a waiting change is sealed for its id.

A wait for a decision ends. It reads its decision again every ``KEEP_EVERY``, so that one whose wake-up was lost still
comes; a batch that left the queue with no decision, or whose decision does not open, ends it not approved
(``UNDECIDED``), and so does its space closing here (``close``, which asks Valkey nothing). Another instance's claim on
the decision's place holds a wait for ``CLAIM_MS`` and a round at most: one renewed by hand past that counts as a
decision that does not open.

What one identity keeps in Valkey has ceilings, on every instance together. ``BATCHES_KEPT`` batches wait at most; one
batch's record weighs ``BATCH_LIMIT`` at most (its texts, which its dashboards' event mirrors: texts that alone weigh
more are refused before the record is made) and its images ``IMAGES_LIMIT`` (each kept as it came, and as it would leave
only where that differs); and the identity's queue keeps ``QUEUE_BYTES`` at most, counted exactly in a ledger beside the
index (each entry's end, and its weight as Valkey takes it: each key's allocation and the key itself, ``live.weighed``):
its waiting batches, and what decided ones leave while it lives, their decisions, the plugin's answers, the images their
holder has not let go of yet. Past a ceiling nothing of the batch is kept or told, and the request is stopped and says
why (``BatchRefused``); an answer that would pass the budget is kept as an error (the plugin's call fails, nothing of it
leaves). Each key has a lifetime, and a poll or a dashboard counted on an instance that stopped no longer counts. What
does not open or cannot be read counts as absent, and is said in the log once a minute for its family.

Who calls: every method of ``ValkeyApprovals`` the event loop (``latet.state.ApprovalsBackend``), each a round trip to
Valkey or two: a Valkey that does not answer fails the call within the bus's ``SOCKET_TIMEOUT``, and the request it
serves with it. A batch's records, its texts and images as JSON and sealed, would hold the loop for a large one (a
quarter of a second with images, seconds for a burst): ``store``, which the Live Reviewer's requests await
(``Approvals.open``), makes them and the round trip in the queues' thread, and tells the dashboards there too, the loop
serving the other requests meanwhile; ``listed`` reads every batch there, each record opened, for what a dashboard asks
of the whole queue, and ``fetch`` and ``fetch_image`` one batch's record or one image's, for a dashboard's decision and
its view of an image; ``settle_all`` settles a dashboard's "approve all" there too, and the identity's feed reads there
the batch a "pending" event names (``live.ValkeyFeed``). The reaper reads no
record (``timings``: what it needs of the batches held here, from memory and the polls' small keys). ``changes`` and
``settled`` are read and written by worker threads under the runtime's lock, and read on the loop too (a command that
waits asks whether its change still waits): a round trip to PostgreSQL each, which waits ten seconds at most for a
connection of the pool. At most one change waits: one written supersedes, in its own transaction and under the
identity's lock of its changes, whatever else waits; and how a change ended first is how it ended."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import secrets
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from typing import Any, Final, cast, get_args

from cryptography.exceptions import InvalidTag

from latet.engine.vault import MaskingError
from latet.gateway.approvals import Batch, Change, Decision, HumanMask, ImageDecision, State
from latet.server import live, space_settings
from latet.server.bus import Bus
from latet.server.db import Database
from latet.server.sealing import Sealer
from latet.server.shared import _settings
from latet.state.protocols import Timing

log = logging.getLogger(__name__)
# The one thread where every space of this process makes its batches' records and writes them (``store``): the JSON of
# a large text or image holds the interpreter for tens of milliseconds at a time, and several threads doing it at once
# take turns with the event loop for it. One thread leaves the loop at most one such step to wait for; a burst of
# batches waits its turn there, as a person's decision on them would anyway, one identity's work one piece at a time
# (its batches, and the records its dashboards read: ``ValkeyApprovals._turn``), so that a burst of one identity's
# delays another identity's batch by one piece at most.
_STORING = ThreadPoolExecutor(1, thread_name_prefix="latet-queue")

HELD_MS = 30_000  # a batch's records live this long unless the instance holding it says it still does
KEEP_EVERY = 5.0  # seconds between two such sayings; a wait reads its decision again as often
DECISION_MS = 600_000  # a decision is kept ten minutes, for a wait that comes late
ANSWER_MS = 120_000  # the plugin's answer, two minutes
CLAIM_MS = 10_000  # the place of a decision being written, kept for its instance
VIEWERS_MS = 86_400_000  # the dashboards watching, counted by instance: a day after the last count
BATCHES_KEPT = 50  # batches waiting for one identity at most, on every instance together
BATCH_LIMIT = 4 * 1024 * 1024  # bytes of one batch's record, sealed: its texts, as its "pending" event shows them
# Bytes of one batch's images, sealed: three screenshots of 2 MB masked (each kept as it came and as it would leave),
# or six clean (kept once, since they leave as they came).
IMAGES_LIMIT = 16 * 1024 * 1024
QUEUE_BYTES = 64 * 1024 * 1024  # bytes one identity's queue keeps in Valkey at most, counted exactly (the ledger)
SETTLED_KEPT = 100  # how the latest changes ended: the backend keeps these, and forgets the oldest by itself
# Why a wait ends with no decision: its batch left the queue (its instance stopped holding it, Valkey lost it), its
# decision does not open, or its space closed.
UNDECIDED = "the Live Reviewer's queue lost it before your decision reached it"
# The plugin's answer kept in place of one that would pass the identity's budget: its call fails, nothing of it leaves.
TOO_HEAVY = (
    "this call's answer weighs more than the Live Reviewer's queue on the server keeps for you now: nothing of it was "
    "sent; decide on what waits first, or send fewer or smaller images at once"
)
# The families of keys this module keeps for an identity, each named ``<family>:<identity>`` or begun so: what goes when
# the identity is removed (Ruling 21's purge).
FAMILIES = ("batch", "batches", "seen", "polls", "image", "decision", "answer", "viewers", "ledger", "weights")
# What a batch keeps in the instance that holds it, never sent to another: its images, its future. How many poll it and
# when it was last seen alive travel in keys of their own.
_HELD_HERE = frozenset({"seen", "waiting", "images", "future"})
_BATCH_FIELDS = frozenset(f.name for f in fields(Batch) if f.init and f.name not in _HELD_HERE)
_STATES = frozenset(get_args(State))
_CLAIMED = b"claim:"  # what a decision's place holds while its instance writes it (a sealed value starts otherwise)
# What a sealed value raises when it does not open, or what it holds when it is not what was sealed: written by hand,
# moved from another key, sealed with another key.
_UNREADABLE = (InvalidTag, ValueError, TypeError, KeyError, AttributeError)

# The identity's ledger, shared by every script that keeps bytes or lets them go: each entry ("b:<batch>" a waiting
# batch's records, "i:<batch>" its images, "d:<batch>" a decision, "a:<batch>" an answer) has its weight in a hash
# (``weights:``, which holds their total too: what Valkey takes for each key, ``weighed``) and the moment it ends in a
# sorted set (``ledger:``). What has ended is let go of as entries are added, 256 at a time, so that no script's work
# grows with the identity.
_LEDGER: Final = (
    live.WEIGHED_LUA
    + """
local function clock()
  local t = redis.call('time')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local function forget(ledger, weights, entry)
  local weight = tonumber(redis.call('hget', weights, entry))
  if weight then
    redis.call('hincrby', weights, 'total', -weight)
    redis.call('hdel', weights, entry)
  end
  redis.call('zrem', ledger, entry)
end
local function sweep(ledger, weights, now)
  for _, entry in ipairs(redis.call('zrangebyscore', ledger, '-inf', now, 'limit', 0, 256)) do
    forget(ledger, weights, entry)
  end
end
local function used(weights)
  return math.max(0, tonumber(redis.call('hget', weights, 'total')) or 0)
end
local function enter(ledger, weights, entry, weight, ends, life)
  forget(ledger, weights, entry)
  redis.call('hset', weights, entry, weight)
  redis.call('hincrby', weights, 'total', weight)
  redis.call('zadd', ledger, ends, entry)
  redis.call('pexpire', ledger, life)
  redis.call('pexpire', weights, life)
end
"""
)
# A batch put: the index lets go of what nobody held in time; below the count and within the bytes, the batch is kept
# with its records, each living HELD_MS, and entered in the ledger; else nothing is written (0: the count, -1: the
# bytes). Valkey's own clock dates both, the same for every instance. KEYS: the index, the ledger, the weights, then the
# batch's records, then its images. ARGV: the batch's id, HELD_MS, BATCHES_KEPT, QUEUE_BYTES, DECISION_MS (the ledger's
# life), the records' weight, the images', then the values.
_PUT: Final = (
    _LEDGER
    + """
local now = clock()
local held = tonumber(ARGV[2])
redis.call('zremrangebyscore', KEYS[1], '-inf', now)
if redis.call('zcard', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
sweep(KEYS[2], KEYS[3], now)
local records, images = tonumber(ARGV[6]), tonumber(ARGV[7])
if used(KEYS[3]) + records + images > tonumber(ARGV[4]) then return -1 end
for i = 4, #KEYS do redis.call('set', KEYS[i], ARGV[i + 4], 'px', held) end
redis.call('zadd', KEYS[1], now + held, ARGV[1])
redis.call('pexpire', KEYS[1], held)
enter(KEYS[2], KEYS[3], 'b:' .. ARGV[1], records, now + held, ARGV[5])
if images > 0 then enter(KEYS[2], KEYS[3], 'i:' .. ARGV[1], images, now + held, ARGV[5]) end
return 1
"""
)
# A waiting batch's record made again (its previews, masked again while it waits): written only while the batch waits
# and no decision has claimed it, as long as it was to live, its weight in the ledger set again within the identity's
# bytes (1); else nothing is written (0: it no longer waits, or a decision claimed it; -1: past the bytes). KEYS: the
# record, its decision, the ledger, the weights. ARGV: the sealed record, the batch's id, QUEUE_BYTES, DECISION_MS (the
# ledger's life).
_REWRITE: Final = (
    _LEDGER
    + """
if redis.call('exists', KEYS[1]) == 0 or redis.call('exists', KEYS[2]) == 1 then return 0 end
local entry = 'b:' .. ARGV[2]
local ends = redis.call('zscore', KEYS[3], entry)
local weight = tonumber(redis.call('hget', KEYS[4], entry))
if not ends or not weight then return 0 end
local change = weighed(string.len(ARGV[1])) - weighed(redis.call('strlen', KEYS[1]))
if change > 0 and used(KEYS[4]) + change > tonumber(ARGV[3]) then return -1 end
redis.call('set', KEYS[1], ARGV[1], 'keepttl')
enter(KEYS[3], KEYS[4], entry, weight + change, tonumber(ends), ARGV[4])
return 1
"""
)
# The batches an instance holds, said held for HELD_MS more in the index and the ledger (one that left them is not
# entered again). KEYS: the index, the ledger, the weights. ARGV: HELD_MS, DECISION_MS (the ledger's life), the ids.
_KEEP: Final = (
    _LEDGER
    + """
local ends = clock() + tonumber(ARGV[1])
for i = 3, #ARGV do
  redis.call('zadd', KEYS[1], 'xx', ends, ARGV[i])
  redis.call('zadd', KEYS[2], 'xx', 'gt', ends, 'b:' .. ARGV[i])
  redis.call('zadd', KEYS[2], 'xx', 'gt', ends, 'i:' .. ARGV[i])
end
redis.call('pexpire', KEYS[1], ARGV[1])
redis.call('pexpire', KEYS[2], ARGV[2])
redis.call('pexpire', KEYS[3], ARGV[2])
return 1
"""
)
# The place of a batch's decision, claimed while the batch waits: the decision that loses it is never sealed. KEYS: the
# batch's record, its decision; ARGV: the claim, CLAIM_MS.
_CLAIM: Final = """
if redis.call('exists', KEYS[1]) == 0 then return 0 end
if redis.call('set', KEYS[2], ARGV[1], 'nx', 'px', ARGV[2]) then return 1 end
return 0
"""
# The decision written where its claim is still this instance's, and the batch out of the queue in the same step: the
# plugin's slot and its polls then live as long as an answer, and the ledger holds the decision in the batch's place.
# KEYS: the decision, the index, the record, its sign of life, the slot, the polls, the ledger, the weights. ARGV: the
# claim, the sealed decision, DECISION_MS, the batch's id, ANSWER_MS.
_DECIDE: Final = (
    _LEDGER
    + """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
local now = clock()
redis.call('set', KEYS[1], ARGV[2], 'px', ARGV[3])
redis.call('zrem', KEYS[2], ARGV[4])
redis.call('del', KEYS[3], KEYS[4])
redis.call('pexpire', KEYS[5], ARGV[5])
redis.call('pexpire', KEYS[6], ARGV[5])
forget(KEYS[7], KEYS[8], 'b:' .. ARGV[4])
enter(KEYS[7], KEYS[8], 'd:' .. ARGV[4], weighed(string.len(ARGV[2])), now + tonumber(ARGV[3]), ARGV[3])
return 1
"""
)
# The plugin's answer, kept where a call waits for it once the batch no longer does (-1: it still waits; 0: no call
# waits): itself within the identity's bytes (1), else what says the call failed (2). KEYS: the record, the slot, the
# ledger, the weights. ARGV: the sealed answer, the sealed error kept in its place, ANSWER_MS, QUEUE_BYTES, DECISION_MS
# (the ledger's life), the batch's id.
_GIVE: Final = (
    _LEDGER
    + """
if redis.call('exists', KEYS[1]) == 1 then return -1 end
if redis.call('exists', KEYS[2]) == 0 then return 0 end
local now = clock()
sweep(KEYS[3], KEYS[4], now)
local entry = 'a:' .. ARGV[6]
forget(KEYS[3], KEYS[4], entry)
local answer, kept = ARGV[1], 1
if used(KEYS[4]) + weighed(string.len(answer)) > tonumber(ARGV[4]) then answer, kept = ARGV[2], 2 end
redis.call('set', KEYS[2], answer, 'px', ARGV[3])
enter(KEYS[3], KEYS[4], entry, weighed(string.len(answer)), now + tonumber(ARGV[3]), ARGV[5])
return kept
"""
)
# The plugin's answer, handed over once: read and forgotten in one step, and only once it was given, its weight let go.
# KEYS: the slot, the ledger, the weights; ARGV: the batch's id.
_TAKE: Final = (
    _LEDGER
    + """
local v = redis.call('get', KEYS[1])
if v and v ~= '' then
  redis.call('del', KEYS[1])
  forget(KEYS[2], KEYS[3], 'a:' .. ARGV[1])
  return v
end
return false
"""
)
# A batch no decision can come for, out of the queue with its weight; the slot then lives as long as an answer. KEYS:
# the index, the record, its sign of life, the slot, the polls, the ledger, the weights, then the images held here.
# ARGV: the batch's id, ANSWER_MS.
_DROP: Final = (
    _LEDGER
    + """
redis.call('zrem', KEYS[1], ARGV[1])
redis.call('del', KEYS[2], KEYS[3])
redis.call('pexpire', KEYS[4], ARGV[2])
redis.call('pexpire', KEYS[5], ARGV[2])
forget(KEYS[6], KEYS[7], 'b:' .. ARGV[1])
if #KEYS > 7 then
  for i = 8, #KEYS do redis.call('del', KEYS[i]) end
  forget(KEYS[6], KEYS[7], 'i:' .. ARGV[1])
end
return 1
"""
)
# The images of a batch decided, let go of by the instance that holds them. KEYS: the ledger, the weights, then the
# images. ARGV: the batch's id.
_LET_GO: Final = (
    _LEDGER
    + """
for i = 3, #KEYS do redis.call('del', KEYS[i]) end
forget(KEYS[1], KEYS[2], 'i:' .. ARGV[1])
return 1
"""
)
# Members of the index whose record is gone (their instance stopped holding them), or that are no batch's id: out of the
# index and the ledger. KEYS: the index, the ledger, the weights. ARGV: the members.
_GONE: Final = (
    _LEDGER
    + """
for i = 1, #ARGV do
  redis.call('zrem', KEYS[1], ARGV[i])
  forget(KEYS[2], KEYS[3], 'b:' .. ARGV[i])
  forget(KEYS[2], KEYS[3], 'i:' .. ARGV[i])
end
return 1
"""
)


class BatchRefused(MaskingError):
    """A batch the queue does not keep: too heavy, or one more than an identity may keep. The request is stopped, never
    sent unasked; the words say why and what to do, and hold nothing of it."""


class UnreadableChange(KeyError, ValueError):
    """A waiting change that is there and cannot be read: it does not open with the identity's root key (written or
    moved by hand), or its settings are not valid (kept by another version of Latet). For the mapping it is none (a
    KeyError); its words name the change's id and, for settings, the fields that are wrong and how, never what they
    hold."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@dataclass(frozen=True, slots=True)
class _Records:
    """What putting a batch writes in Valkey: its keys and their sealed values (``repr`` holds neither), and what
    Valkey will take for its records and for its images."""

    batch_id: str
    keys: list[str] = field(repr=False)
    values: list[bytes] = field(repr=False)
    records: int
    images: int


def _too_much_text(weight: int, *, at_least: bool = False) -> str:
    return (
        f"the Live Reviewer of a server keeps {BATCH_LIMIT} bytes of a request's new texts at most, and this one's "
        f"weigh {weight}{' at least' if at_least else ''}: it was not sent; send less text at once"
    )


def _refuse_texts_past_the_limit(batch: Batch) -> None:
    """Raise BatchRefused, before the batch's record is made, when its texts alone pass ``BATCH_LIMIT``: the record
    holds each as it came and as it would leave, in UTF-8 and among the rest, so it would weigh more (a 13 MiB pasted
    log held the loop more than a second to be refused once made, sealed and weighed)."""
    texts = sum(
        len(item.get(side) or "")
        for item in batch.items
        if isinstance(item, dict)
        for side in ("original", "masked")
        if isinstance(item.get(side) or "", str)
    )
    if texts > BATCH_LIMIT:
        raise BatchRefused(_too_much_text(texts, at_least=True))


def _texts_held(batch: Batch) -> int:
    """What a batch's texts take in memory, in bytes: each as it came and as it would leave."""
    return sum(
        sys.getsizeof(text)
        for item in batch.items
        if isinstance(item, dict)
        for side in ("original", "masked")
        if isinstance(text := item.get(side), str)
    )


def _batch_data(batch: Batch) -> dict[str, Any]:
    """Every field of ``batch`` but what stays in the instance that holds it: what a later part adds to ``Batch``
    (E5's ``decides`` and ``deciders``) reaches the other instances too."""
    return {f.name: getattr(batch, f.name) for f in fields(batch) if f.name not in _HELD_HERE}


def _decision_data(decision: Decision) -> dict[str, Any]:
    return {
        "approved": decision.approved,
        "masks": [[m.item, m.value, m.category] for m in decision.masks],
        "images": [[i.item, [list(r) for r in i.rects], i.withhold] for i in decision.images],
        "reason": decision.reason,
        "state": decision.state,
    }


def _decision(data: Any) -> Decision:
    """The decision a record holds. Raises KeyError, TypeError or ValueError for what is not one."""
    approved, reason, state = data["approved"], data["reason"], data["state"]
    if type(approved) is not bool or not (reason is None or isinstance(reason, str)) or state not in _STATES:
        raise ValueError("not a decision")
    images = [
        ImageDecision(int(item), tuple((int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in rects), bool(withhold))
        for item, rects, withhold in data["images"]
    ]
    masks = [HumanMask(int(item), str(value), str(category)) for item, value, category in data["masks"]]
    return Decision(approved, masks, images, reason, state)


def _undecided() -> Decision:
    return Decision(False, reason=UNDECIDED, state="abandoned")


def _counts(found: Mapping[bytes, bytes], family: str) -> dict[str, int]:
    """A hash of counts by instance, but those that cannot be read (said for ``family``)."""
    counts: dict[str, int] = {}
    for name, value in found.items():
        try:
            counts[name.decode()] = int(value)
        except ValueError:  # a name that is not UTF-8, a count that is not a number
            live.UNREADABLE.met(family)
    return counts


def _seen_at(seen: bytes | None) -> float | None:
    """When a batch was last seen alive, as its ``seen:`` key says; None when it says nothing that can be read."""
    if seen is None:
        return None
    try:
        when = float(seen)
    except ValueError:
        when = math.nan
    if not math.isfinite(when):
        live.UNREADABLE.met("seen")
        return None
    return when


class ValkeyApprovals:
    def __init__(self, bus: Bus, db: Database, identity_id: str, root_key: bytes, *, instance: str) -> None:
        self.bus, self.identity, self.instance = bus, identity_id, instance
        self._sealer = Sealer(root_key, b"valkey")
        self.changes: MutableMapping[str, Change] = PgChanges(db, identity_id, root_key)
        self.settled: MutableMapping[str, str] = PgSettled(db, identity_id)
        self._held: dict[str, Batch] = {}  # batches opened here: their images, their futures
        self._decisions: dict[str, asyncio.Future[Decision]] = {}  # the waits for a decision here, by batch
        self._answers: dict[str, asyncio.Future[dict[str, Any] | None]] = {}  # the plugin's polls open here, by batch
        self._polling: dict[str, int] = {}  # how many of them, by batch
        self._claims: dict[str, float] = {}  # when a wait first saw another instance's claim, since the place was empty
        self._keeper: asyncio.Task[None] | None = None
        self._turn = asyncio.Lock()  # held while this identity's work is in the queues' thread (``_in_turn``)
        self._said: float | None = None  # when the log last said something of this queue
        self._unsaid = 0
        client = bus.client
        self._put_records = client.register_script(_PUT)
        self._rewrite_records = client.register_script(_REWRITE)
        self._keep_held = client.register_script(_KEEP)
        self._claim = client.register_script(_CLAIM)
        self._decide = client.register_script(_DECIDE)
        self._give = client.register_script(_GIVE)
        self._take = client.register_script(_TAKE)
        self._drop_batch = client.register_script(_DROP)
        self._let_go_of = client.register_script(_LET_GO)
        self._gone = client.register_script(_GONE)

    def open(self, change: Change) -> tuple[Change, list[str]]:
        return cast(PgChanges, self.changes).open(change)

    def end(self, change_id: str, state: str) -> bool:
        return cast(PgSettled, self.settled).end(change_id, state)

    def _key(self, kind: str, batch_id: str = "") -> str:
        return self.bus.key(f"{kind}:{self.identity}" + (f":{batch_id}" if batch_id else ""))

    def _image_key(self, batch_id: str, item: int) -> str:
        return self._key("image", f"{batch_id}:{item}")

    def _ledger(self) -> list[str]:
        return [self._key("ledger"), self._key("weights")]

    # --- the queue --------------------------------------------------------------------------------

    async def store(self, batch: Batch, announce: Callable[[], None] | None = None) -> None:
        """The batch waits, listed for every dashboard and decided from any: its records, its texts and images as
        JSON, sealed and weighed, and the round trip that keeps them, are made in the queues' own thread
        (``_STORING``), the loop serving the other requests meanwhile. The batch's future is made on the loop first;
        texts that alone weigh more than ``BATCH_LIMIT`` are refused (``BatchRefused``) before anything is made, and so
        is a batch the identity's ceilings leave out. The thread is every identity's: this identity's batches go there
        one at a time, the others waiting on the loop, so that the thread takes every identity's batches in turn. Once
        the batch is held here, ``announce`` (its "pending" event, a round trip to Valkey through the identity's feed)
        is called in that thread too, in its turn."""
        loop = asyncio.get_running_loop()
        batch.future = loop.create_future()
        _refuse_texts_past_the_limit(batch)
        async with self._turn:
            records = await loop.run_in_executor(_STORING, self._sealed_records, batch)
            kept = await loop.run_in_executor(_STORING, self._write_records, records)
            self._held_here(batch, kept)
            if announce is not None:
                await loop.run_in_executor(_STORING, announce)

    async def replace(self, batch_id: str, items: list[dict[str, Any]]) -> Batch | None:
        """The previews of a batch held here made again: sealed for its record in the queues' thread, in this
        identity's turn, and written only while the batch waits and no decision has claimed it (``_REWRITE``), as long
        as it was to live. A batch another instance holds is that instance's to make again (its texts are there): None.
        Previews past ``BATCH_LIMIT``, or past the identity's bytes, leave the record as it was: None."""
        batch = self._held.get(batch_id)
        if batch is None or batch.future.done():
            return None
        record = self._key("batch", batch_id)
        data = {**_batch_data(batch), "items": items}
        keys = [record, self._key("decision", batch_id), *self._ledger()]
        loop = asyncio.get_running_loop()
        async with self._turn:
            sealed = await loop.run_in_executor(_STORING, self._sealer.seal_json, data, record)
            if len(sealed) > BATCH_LIMIT:
                return None
            args = [sealed, batch_id, QUEUE_BYTES, DECISION_MS]
            written = await loop.run_in_executor(_STORING, lambda: int(self._rewrite_records(keys=keys, args=args)))
        if written != 1:
            return None
        batch.items = items
        return batch

    def _sealed_records(self, batch: Batch) -> _Records:
        """What putting ``batch`` writes: each record sealed for its own key, which is what it says it is (a batch's,
        an image's of one item), and what Valkey will take for them. Raises BatchRefused past ``BATCH_LIMIT`` or
        ``IMAGES_LIMIT``. Any thread: it reads the batch and asks Valkey nothing."""
        record = self._key("batch", batch.id)
        keys = [record, self._key("seen", batch.id)]
        values = [self._sealer.seal_json(_batch_data(batch), record), repr(time.time()).encode()]
        if len(values[0]) > BATCH_LIMIT:
            raise BatchRefused(_too_much_text(len(values[0])))
        if batch.polled:  # the plugin will ask for its answer: its slot, empty until the answer is given
            keys.append(self._key("answer", batch.id))
            values.append(b"")
        records, images, sizes = sum(live.weighed(len(value)) for value in values), 0, 0
        for item, work in batch.images.items():
            key = self._image_key(batch.id, item)
            original, sent = list(work.original), [work.ref.data(), work.ref.media_type]
            # As it would leave only where that is not as it came: a clean image is kept once.
            views = {"original": original, "sent": None if sent == original else sent}
            keys.append(key)
            values.append(self._sealer.seal_json(views, key))
            sizes += len(values[-1])
            images += live.weighed(len(values[-1]))
        if sizes > IMAGES_LIMIT:
            raise BatchRefused(
                f"the Live Reviewer of a server keeps {IMAGES_LIMIT} bytes of a request's images at most, and this "
                f"one's weigh {sizes}: it was not sent; send fewer or smaller images at once"
            )
        return _Records(batch.id, keys, values, records, images)

    def _write_records(self, records: _Records) -> int:
        """The records written where the identity's ceilings let them (1), or nothing: 0 past its count of batches, -1
        past its bytes. One round trip, from any thread."""
        ceilings = [HELD_MS, BATCHES_KEPT, QUEUE_BYTES, DECISION_MS]
        keys = [self._key("batches"), *self._ledger(), *records.keys]
        args = [records.batch_id, *ceilings, records.records, records.images, *records.values]
        return int(self._put_records(keys=keys, args=args))

    def _held_here(self, batch: Batch, kept: int) -> None:
        """A batch written (``kept`` 1) waits, held here: its future and images, and the keeper that says it is held.
        Raises BatchRefused for one the identity's ceilings left out. On the event loop."""
        if kept == 0:
            raise BatchRefused(
                f"{BATCHES_KEPT} requests already wait for a decision in the Live Reviewer, the most a server keeps "
                "for one identity: this one was not sent; decide on those first"
            )
        if kept < 0:
            raise BatchRefused(
                "what waits for a decision in the Live Reviewer, with what decided requests leave for a few minutes, "
                f"already weighs as much as a server keeps for one identity ({QUEUE_BYTES} bytes): this one was not "
                "sent; decide on those first, or send fewer or smaller images at once"
            )
        self._held[batch.id] = batch
        self._decisions[batch.id] = batch.future
        self._keep_going()

    def _opened(self, batch_id: str, raw: bytes) -> Batch | None:
        """The batch a record holds; None, said, when it does not open for its key."""
        try:
            data = self._sealer.open_json(raw, self._key("batch", batch_id))
            batch = Batch(**{key: value for key, value in data.items() if key in _BATCH_FIELDS})
        except _UNREADABLE:
            live.UNREADABLE.met("batch")
            return None
        held = self._held.get(batch.id)
        if held is not None:
            batch.images = held.images
        return batch

    def _alive(self, instances: Iterable[str]) -> set[str]:
        """Which of ``instances`` still say they are alive, as each does every few seconds: this one, always."""
        others = sorted({instance for instance in instances if instance != self.instance})
        if not others:
            return {self.instance}
        found = self.bus.client.mget([self.bus.key(f"instance:{instance}") for instance in others])
        return {instance for instance, flag in zip(others, found, strict=True) if flag is not None} | {self.instance}

    def _records(self, ids: Sequence[str]) -> tuple[list[Batch], list[str]]:
        """The batches of ``ids`` that still wait, as any instance sees them (their polls on instances that are alive,
        when they were seen last), and the ids whose record is gone."""
        if not ids:
            return [], []
        pipe = self.bus.client.pipeline()
        for batch_id in ids:
            pipe.get(self._key("batch", batch_id))
            pipe.get(self._key("seen", batch_id))
            pipe.hgetall(self._key("polls", batch_id))
        found = pipe.execute()
        polls = [_counts(found[3 * n + 2], "polls") for n in range(len(ids))]
        alive = self._alive(instance for counts in polls for instance, count in counts.items() if count)
        out, gone = [], []
        for n, batch_id in enumerate(ids):
            raw, seen = found[3 * n], found[3 * n + 1]
            if raw is None:
                gone.append(batch_id)  # its instance stopped holding it, or it was decided meanwhile
                continue
            batch = self._opened(batch_id, raw)
            if batch is None:
                continue
            batch.waiting = sum(max(0, count) for instance, count in polls[n].items() if instance in alive)
            if (when := _seen_at(seen)) is not None:  # on this process's clock, as the reaper reads it
                batch.seen = time.monotonic() - max(0.0, time.time() - when)
            out.append(batch)
        return out, gone

    def get(self, batch_id: str) -> Batch | None:
        found, _ = self._records([batch_id])
        return found[0] if found else None

    def waiting(self) -> list[Batch]:
        index = self._key("batches")
        ids: list[str] = []
        junk: list[bytes] = []
        for member in self.bus.client.zrevrange(index, 0, BATCHES_KEPT - 1):  # the ceiling, the latest held first
            try:
                ids.append(member.decode())
            except UnicodeDecodeError:
                junk.append(member)
                live.UNREADABLE.met("batches")
        found, gone = self._records(ids)
        if gone or junk:  # out of the index and the ledger
            self._gone(keys=[index, *self._ledger()], args=[*gone, *junk])
        return sorted(found, key=lambda batch: batch.created)

    async def listed(self) -> list[Batch]:
        """``waiting``, read in the queues' thread (``_STORING``), in this identity's turn: every batch's record opened
        there, its texts with it, which held the event loop for each (more than a tenth of a second for four batches
        near their limit, half a second at the identity's ``QUEUE_BYTES``). Nothing it reads here is the loop's own but
        what this instance holds (``_held``), read by its id."""
        return await self._in_turn(self.waiting)

    def timings(self) -> list[Timing]:
        """What the reaper reads, every few seconds, of the batches held here and still waiting: when each was made, its
        time limit and whether the plugin asks for it (never changing: as this instance holds them), how many ask now
        on the instances alive and when one last did (their small keys), in a round trip, and one more when another
        instance counted a poll (whether it is alive); no record, no text. The reaper read every batch's record in the
        queues' thread at each round (half a second for an identity's full queue, which every other identity's batch
        waited behind). A batch another instance holds is that instance's to give up, and leaves the queue within
        ``HELD_MS`` once that instance stops."""
        held = [batch for batch in list(self._held.values()) if not batch.future.done()]
        if not held:
            return []
        pipe = self.bus.client.pipeline()
        for batch in held:
            pipe.exists(self._key("batch", batch.id))
            pipe.get(self._key("seen", batch.id))
            pipe.hgetall(self._key("polls", batch.id))
        found = pipe.execute()
        polls = [_counts(found[3 * n + 2], "polls") for n in range(len(held))]
        alive = self._alive(instance for counts in polls for instance, count in counts.items() if count)
        out, clock = [], time.monotonic()
        for n, batch in enumerate(held):
            if not found[3 * n]:
                continue  # decided meanwhile, here or on another instance, or gone
            waiting = sum(max(0, count) for instance, count in polls[n].items() if instance in alive)
            when = _seen_at(found[3 * n + 1])  # on this process's clock; none readable: seen now, as ``get`` reads it
            seen = clock - max(0.0, time.time() - when) if when is not None else clock
            out.append(Timing(batch.id, batch.created, batch.timeout_s, batch.polled, waiting, seen))
        return out

    async def fetch(self, batch_id: str) -> Batch | None:
        """``get``, read in the queues' thread, in this identity's turn: the batch's record opened there, its texts with
        it, which held the event loop 35 ms for one near its limit at each decision a dashboard took."""
        return await self._in_turn(self.get, batch_id)

    async def fetch_image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        """``image``, read in the queues' thread, in this identity's turn: the image's record opened there (the image
        as it came and as it would leave, ``IMAGES_LIMIT`` sealed at most), at each view of it in a dashboard."""
        return await self._in_turn(self.image, batch_id, item, sent)

    async def _in_turn[T](self, work: Callable[..., T], *args: Any) -> T:
        """``work`` done in the queues' thread in this identity's turn (``_turn``): its work goes there one piece at a
        time, as its batches do (``store``), so that another identity's batch waits behind one piece of it at most,
        whatever one dashboard asks at once."""
        async with self._turn:
            return await asyncio.get_running_loop().run_in_executor(_STORING, work, *args)

    def settle(self, batch_id: str, decision: Decision) -> bool:
        claim = self._claimed(batch_id)
        if claim is None:
            return False  # not waiting, or decided first by another (on this instance or another)
        if not self._written(batch_id, decision, claim):
            return False  # its claim ended before the decision was written (the loop was held past CLAIM_MS)
        future = self._decisions.get(batch_id)
        if future is not None and not future.done():  # a wait here ends at once, and the batch's images go
            self._claims.pop(batch_id, None)
            future.set_result(decision)
            self._quietly(self._let_go, batch_id)
        self._tell({"decided": batch_id})
        return True

    async def settle_all(self, decisions: list[tuple[str, Decision]], told: Callable[[str, Decision], None]) -> int:
        """``settle`` for each of ``decisions``, made in the queues' thread in this identity's turn (``_in_turn``): a
        dashboard's "approve all" settles up to ``BATCHES_KEPT`` batches, each with its round trips (its claim, its
        decision, its images let go of, its wake-up), which held the event loop every identity shares. A wait here is
        woken on the loop (``call_soon_threadsafe``); ``told`` is called in that thread for each batch settled (the
        dashboards' "settled", through the identity's feed, which sends to Valkey and touches no viewer's queue). How
        many were settled."""
        loop = asyncio.get_running_loop()
        return await self._in_turn(self._settled_all, loop, decisions, told)

    def _settled_all(
        self,
        loop: asyncio.AbstractEventLoop,
        decisions: list[tuple[str, Decision]],
        told: Callable[[str, Decision], None],
    ) -> int:
        """In the queues' thread: each decision written where the batch still waits, the waits here woken on ``loop``,
        the batches' images let go of and the other instances woken."""
        settled = 0
        for batch_id, decision in decisions:
            claim = self._claimed(batch_id)
            if claim is None or not self._written(batch_id, decision, claim):
                continue  # decided first by another, or its claim ended before the decision was written
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (the server stopping)
                loop.call_soon_threadsafe(self._woken, batch_id, decision)
            self._quietly(self._let_go, batch_id)  # held here or not: only what this instance holds goes
            self._tell({"decided": batch_id})
            told(batch_id, decision)
            settled += 1
        return settled

    def _woken(self, batch_id: str, decision: Decision) -> None:
        """On the event loop: a wait here for ``batch_id`` ends with ``decision``."""
        future = self._decisions.get(batch_id)
        if future is not None and not future.done():
            self._claims.pop(batch_id, None)
            future.set_result(decision)

    def _claimed(self, batch_id: str) -> bytes | None:
        """The place of the batch's decision, claimed for this instance while the batch waits: the claim; None when it
        waits no more, or another holds the place."""
        claim = _CLAIMED + secrets.token_hex(16).encode()
        place = [self._key("batch", batch_id), self._key("decision", batch_id)]
        return claim if self._claim(keys=place, args=[claim, CLAIM_MS]) else None

    def _written(self, batch_id: str, decision: Decision, claim: bytes) -> bool:
        """``decision`` written where ``claim`` still holds the place, and the batch out of the queue in the same step.
        Only the decision that won the place is ever sealed."""
        place = self._key("decision", batch_id)
        sealed = self._sealer.seal_json(_decision_data(decision), place)
        keys = [
            place, self._key("batches"), self._key("batch", batch_id), self._key("seen", batch_id),
            self._key("answer", batch_id), self._key("polls", batch_id), *self._ledger(),
        ]  # fmt: skip
        return bool(self._decide(keys=keys, args=[claim, sealed, DECISION_MS, batch_id, ANSWER_MS]))

    async def decision(self, batch: Batch, hold: float | None) -> Decision | None:
        future = self._decisions.get(batch.id)
        if future is None:
            future = self._decisions[batch.id] = asyncio.get_running_loop().create_future()
        self._decided(batch.id)  # decided already, maybe, or with no decision to come
        self._keep_going()  # read again until it ends
        try:
            return await asyncio.wait_for(asyncio.shield(future), hold)
        except TimeoutError:
            return None

    @staticmethod
    def _ask(pipe: Any, place: str, record: str) -> None:
        """What says whether a wait can end: a batch's decision, how long it lives, and whether the batch waits."""
        pipe.get(place)
        pipe.pttl(place)
        pipe.exists(record)

    def _decided(self, batch_id: str) -> None:
        future = self._decisions.get(batch_id)
        if future is None or future.done():
            return
        pipe = self.bus.client.pipeline()
        self._ask(pipe, self._key("decision", batch_id), self._key("batch", batch_id))
        self._end(batch_id, *pipe.execute())

    def _writing(self, batch_id: str) -> bool:
        """Whether a claim in the batch's place may still be another instance's writing its decision: for ``CLAIM_MS``
        in all and a round, from the first this wait saw since it last found the place empty."""
        now = time.monotonic()
        return now - self._claims.setdefault(batch_id, now) <= CLAIM_MS / 1000 + KEEP_EVERY

    def _end(self, batch_id: str, raw: bytes | None, ttl: int, waits: int) -> None:
        """The wait here for ``batch_id`` ends when its decision is there, or when none can come: its batch left the
        queue with none, or what is in its place does not open (written by hand, moved from another batch, a claim
        held past its time). It goes on while the batch waits, and while another instance's claim holds the place."""
        future = self._decisions.get(batch_id)
        if future is None or future.done():
            return
        decision: Decision | None = None
        if raw is None:
            self._claims.pop(batch_id, None)
            if waits:
                return
        elif raw.startswith(_CLAIMED) and 0 < ttl <= CLAIM_MS and self._writing(batch_id):
            return  # another instance is writing its decision
        else:
            try:
                decision = _decision(self._sealer.open_json(raw, self._key("decision", batch_id)))
            except _UNREADABLE:
                live.UNREADABLE.met("decision")
        self._claims.pop(batch_id, None)
        if decision is not None:
            future.set_result(decision)
            self._quietly(self._let_go, batch_id)
            return
        future.set_result(_undecided())
        self._quietly(self._drop, batch_id)

    def _quietly(self, cleaning: Callable[[str], None], batch_id: str) -> None:
        """A cleaning after a wait has ended: Valkey failing it is said, and the keys end by their own lifetime."""
        try:
            cleaning(batch_id)
        except Exception as exc:
            self._trouble(f"Valkey did not answer ({type(exc).__name__}); what it keeps ends by its own lifetime")

    def _held_images(self, batch_id: str) -> list[str]:
        held = self._held.get(batch_id)
        return [self._image_key(batch_id, item) for item in held.images] if held is not None else []

    def _let_go(self, batch_id: str) -> None:
        """The images of a batch decided, held here, let go of with their weight."""
        if images := self._held_images(batch_id):
            self._let_go_of(keys=[*self._ledger(), *images], args=[batch_id])

    def _drop(self, batch_id: str) -> None:
        """A batch no decision can come for leaves the queue with its weight, and the plugin's slot lives as long as an
        answer: the answer that says it was not sent can be given."""
        keys = [
            self._key("batches"), self._key("batch", batch_id), self._key("seen", batch_id),
            self._key("answer", batch_id), self._key("polls", batch_id), *self._ledger(),
            *self._held_images(batch_id),
        ]  # fmt: skip
        self._drop_batch(keys=keys, args=[batch_id, ANSWER_MS])

    def polled(self, batch: Batch, delta: int) -> None:
        batch.waiting += delta
        self._count(batch.id, delta)

    def _count(self, batch_id: str, delta: int) -> None:
        """A poll on the batch begins or ends here: counted under this instance's name, for every instance, and a sign
        that the batch is alive."""
        polls = self._key("polls", batch_id)
        pipe = self.bus.client.pipeline()
        pipe.hincrby(polls, self.instance, delta)
        pipe.pexpire(polls, ANSWER_MS)
        pipe.set(self._key("seen", batch_id), repr(time.time()), px=HELD_MS, xx=True)  # while it waits
        counted, *_ = pipe.execute(raise_on_error=False)
        if isinstance(counted, Exception):  # this instance's count was written by hand: counted again from it
            live.UNREADABLE.met("polls")
            self.bus.client.hset(polls, self.instance, max(0, delta))

    def image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        key = self._image_key(batch_id, item)
        pipe = self.bus.client.pipeline()
        pipe.get(key)
        pipe.exists(self._key("batch", batch_id))
        raw, waits = pipe.execute()
        if raw is None or not waits:
            return None
        try:
            views = self._sealer.open_json(raw, key)
            view = views["sent"] if sent else None
            data, media = view if view is not None else views["original"]  # none as it leaves: it leaves as it came
        except _UNREADABLE:
            live.UNREADABLE.met("image")
            return None
        return str(data), str(media)

    # --- the plugin's answers ----------------------------------------------------------------------

    def give(self, batch_id: str, answer: dict[str, Any]) -> None:
        slot = self._key("answer", batch_id)
        sealed = self._sealer.seal_json(answer, slot)
        instead = self._sealer.seal_json({"error": TOO_HEAVY}, slot)
        args = [sealed, instead, ANSWER_MS, QUEUE_BYTES, DECISION_MS, batch_id]
        kept = self._give(keys=[self._key("batch", batch_id), slot, *self._ledger()], args=args)
        if kept == -1:  # the answer is what leaves: only your decision lets it
            raise ValueError("a batch that waits for you has no answer to give yet")
        if kept == 2:
            self._trouble(
                f"an answer of {len(sealed)} bytes would have passed the {QUEUE_BYTES} the queue keeps: the plugin was "
                "told its call failed"
            )
        if (
            kept
        ):  # kept only where a call waits for it: its slot was made when the batch was put, and is gone once taken
            self._tell({"answered": batch_id})

    async def answer(self, batch_id: str, hold: float) -> dict[str, Any] | None:
        if not self.bus.client.exists(self._key("answer", batch_id)):
            raise KeyError(batch_id)
        self._count(batch_id, 1)  # a poll: the batch is alive
        self._polling[batch_id] = self._polling.get(batch_id, 0) + 1
        future = self._answers.get(batch_id)
        if future is None:
            future = self._answers[batch_id] = asyncio.get_running_loop().create_future()
        try:
            self._answered(batch_id)  # given already, maybe
            return await asyncio.wait_for(asyncio.shield(future), hold)
        except TimeoutError:
            return None
        finally:
            self._polling[batch_id] -= 1
            if not self._polling[batch_id]:  # nobody asks here any more: an answer given later is another poll's
                del self._polling[batch_id]
                self._answers.pop(batch_id, None)
            self._count(batch_id, -1)

    def _answered(self, batch_id: str) -> None:
        future = self._answers.get(batch_id)
        if future is None or future.done():
            return
        slot = self._key("answer", batch_id)
        raw = self._take(keys=[slot, *self._ledger()], args=[batch_id])  # handed over once, its weight let go
        if raw is None:
            return  # not given yet
        answer: Any = None
        with contextlib.suppress(*_UNREADABLE):
            answer = self._sealer.open_json(raw, slot)
        if not isinstance(answer, dict):  # taken all the same: the next poll finds nothing
            live.UNREADABLE.met("answer")
            return
        future.set_result(answer)

    def heard(self, message: Mapping[str, Any]) -> None:
        """A message of the identity's ``approvals:<id>`` channel: a decision or an answer someone here waits for. It
        never raises: a wake-up that Valkey fails is made up by the wait's next reading, or the plugin's next poll."""
        try:
            if isinstance(decided := message.get("decided"), str):
                self._decided(decided)
            if isinstance(answered := message.get("answered"), str):
                self._answered(answered)
        except Exception as exc:
            self._trouble(f"Valkey did not answer ({type(exc).__name__}); it tries again")

    def _tell(self, message: dict[str, str]) -> None:
        """Wake whoever waits, on every instance. When Valkey fails it, what was done stands: a wait reads its decision
        again every ``KEEP_EVERY``, and the plugin asks again."""
        try:
            self.bus.publish(f"approvals:{self.identity}", message)
        except Exception as exc:
            self._trouble(f"Valkey did not answer ({type(exc).__name__}); it tries again")

    # --- viewers and the batches held here ----------------------------------------------------------

    def viewers(self, delta: int = 0) -> int:
        key = self._key("viewers")
        if delta:
            pipe = self.bus.client.pipeline()
            pipe.hincrby(key, self.instance, delta)
            pipe.pexpire(key, VIEWERS_MS)
            counted, _ = pipe.execute(raise_on_error=False)
            if isinstance(counted, Exception):  # this instance's count was written by hand: counted again from it
                live.UNREADABLE.met("viewers")
                self.bus.client.hset(key, self.instance, max(0, delta))
        found = self.bus.client.hgetall(key)
        counts = _counts(found, "viewers")
        alive = self._alive(counts)
        names = {name: name.decode(errors="replace") for name in found}
        dropped = [name for name, instance in names.items() if instance not in counts or instance not in alive]
        if dropped:  # the counts of instances that stopped, and those that cannot be read
            self.bus.client.hdel(key, *dropped)
        return sum(max(0, count) for instance, count in counts.items() if instance in alive)

    @property
    def holding(self) -> bool:
        """Whether this instance holds a batch (its images, its future: opened here, the plugin's waiting between two
        polls included), a wait for a decision or a poll: its space is then never closed to make room (a server's
        budget of spaces), which would end them not approved. Memory only: for the event loop."""
        return bool(self._held) or bool(self._polling) or any(not f.done() for f in self._decisions.values())

    @property
    def held_bytes(self) -> int:
        """What the batches held here keep in this process's memory, in bytes: their texts, each as it came and as it
        would leave, and their images (``ImageWork.held_bytes``: each as it came and as it would leave, and its pixels),
        until the keeper lets go of a batch decided. A space's weight counts it (``spaces.space_size``). Memory only:
        for the event loop."""
        return sum(
            _texts_held(batch) + sum(work.held_bytes for work in batch.images.values()) for batch in self._held.values()
        )

    def close(self) -> None:
        """The space closes on this instance: every wait here ends at once, a decision's not approved and a poll's with
        no answer yet (the plugin asks again, wherever it is served), and its batches are no longer said to be held
        here (they leave the queue within ``HELD_MS``, unless decided first, and the ledger counts their images until
        then). Valkey is asked nothing: it may be what does not answer."""
        if self._keeper is not None:
            self._keeper.cancel()
            self._keeper = None
        for future in self._decisions.values():
            if not future.done():
                future.set_result(_undecided())
        for answer in self._answers.values():
            if not answer.done():
                answer.set_result(None)
        self._held.clear()

    def _keep_going(self) -> None:
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.get_running_loop().create_task(self._keep())

    async def _keep(self) -> None:
        """While this instance holds batches, or a wait for a decision is open here: every ``KEEP_EVERY`` the batches'
        records are said held for ``HELD_MS`` more (never cutting a longer life short, as the plugin's slot has once
        decided), and each wait reads its decision again. A round that Valkey fails is said in the log, and the next
        one tries again."""
        while self._held or any(not future.done() for future in self._decisions.values()):
            await asyncio.sleep(KEEP_EVERY)
            try:
                self._kept()
            except Exception as exc:
                self._trouble(f"Valkey did not answer ({type(exc).__name__}); it tries again")

    def _kept(self) -> None:
        for batch_id, batch in list(self._held.items()):
            if batch.future.done():
                del self._held[batch_id]
        waits = [batch_id for batch_id, future in self._decisions.items() if not future.done()]
        if self._held or waits:
            pipe = self.bus.client.pipeline()
            for batch_id, batch in self._held.items():
                for kind in ("batch", "seen", "answer", "polls"):
                    pipe.pexpire(self._key(kind, batch_id), HELD_MS, gt=True)
                for item in batch.images:
                    pipe.pexpire(self._image_key(batch_id, item), HELD_MS, gt=True)
            if self._held:
                args = [HELD_MS, DECISION_MS, *self._held]
                self._keep_held(keys=[self._key("batches"), *self._ledger()], args=args, client=pipe)
            for batch_id in waits:
                self._ask(pipe, self._key("decision", batch_id), self._key("batch", batch_id))
            found = pipe.execute()
            asked = found[len(found) - 3 * len(waits) :]
            for n, batch_id in enumerate(waits):
                self._end(batch_id, *asked[3 * n : 3 * n + 3])
        self._decisions = {k: f for k, f in self._decisions.items() if not f.done() or k in self._held}
        self._answers = {k: f for k, f in self._answers.items() if not f.done() or k in self._polling}
        self._claims = {k: seen for k, seen in self._claims.items() if k in self._decisions}

    def _trouble(self, why: str) -> None:
        """Said in the log by its kind alone (an error's type, never its words, which can name Valkey's address; nothing
        of a batch), the first at once and then one line a minute at most."""
        now = time.monotonic()
        self._unsaid += 1
        if self._said is not None and now - self._said < live.SAY_EVERY:
            return
        self._said, count, self._unsaid = now, self._unsaid, 0
        log.warning(
            "the Live Reviewer's queue of %s: %s%s",
            self.identity,
            why,
            f"; {count} times since the line before" if count > 1 else "",
        )


def _change_data(change: Change) -> dict[str, Any]:
    """Every field of ``change``, its settings as JSON: what a later part adds to ``Change`` (E5's ``decides``,
    ``deciders`` and ``asked_by``) is kept with it too."""
    data = {f.name: getattr(change, f.name) for f in fields(change)}
    data["config"] = change.config.model_dump(mode="json") if change.config is not None else None
    return data


def _change(data: Any) -> Change:
    """The change a record holds. Raises ValueError naming the fields of its settings that are wrong and how, never
    what they hold (pydantic's own words would quote it); KeyError, TypeError or AttributeError for what is no
    change."""
    known = {f.name for f in fields(Change) if f.init}
    given = {key: value for key, value in data.items() if key in known}  # one an older Latet did not keep: its default
    config = data.get("config")
    given["config"] = _settings(config, "its settings") if config is not None else None
    return Change(**given)


def _changes_lock(schema: str, identity_id: str) -> int:
    """The advisory lock of an identity's changes in one schema: a change written holds it until its transaction ends,
    so that changes written at once on two instances take their turn."""
    digest = hashlib.sha256(f"latet-changes:{schema}:{identity_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# How the latest changes ended, the latest ``SETTLED_KEPT`` kept: the older ones go.
_FORGET_THE_OLDEST: Final = (
    "DELETE FROM changes WHERE identity_id = %s AND state IS NOT NULL AND id NOT IN (SELECT id FROM changes"
    " WHERE identity_id = %s AND state IS NOT NULL ORDER BY ended DESC, id LIMIT %s)"
)


class PgChanges(MutableMapping[str, Change]):
    """The change waiting for an identity's approval, in PostgreSQL, sealed for its id (it holds its settings). One that
    is there and cannot be read counts as none (``UnreadableChange``), said in the log once a minute. At most one waits,
    whatever instances write at once: a change written supersedes every other that waits, in its own transaction and
    under the identity's lock of its changes (a computer's queue supersedes then writes, which two instances proposing
    at once would each do before the other's write)."""

    def __init__(self, db: Database, identity_id: str, root_key: bytes) -> None:
        self.db, self.identity = db, identity_id
        self._sealer = Sealer(root_key, b"settings")

    def _aad(self, change_id: str) -> str:
        return f"{self.identity}:change:{change_id}"

    def _opened(self, change_id: str, sealed: bytes) -> Change:
        data: Any = None
        opened = False
        with contextlib.suppress(InvalidTag, ValueError):  # another root key, another change's data, written by hand
            data, opened = self._sealer.open_json(sealed, self._aad(change_id)), True
        why = "it does not open with this identity's root key (written or moved there by hand, or sealed with another)"
        if opened:
            try:
                return _change(data)
            except ValueError as exc:  # settings another version of Latet kept: named by their fields alone
                why = str(exc)
            except (KeyError, TypeError, AttributeError):
                why = "what opens is no change"
        space_settings.UNOPENED.met(self.identity, "change")
        raise UnreadableChange(f"the waiting change {change_id} cannot be read: {why}")

    def __getitem__(self, change_id: str) -> Change:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT data FROM changes WHERE identity_id = %s AND id = %s AND state IS NULL",
                (self.identity, change_id),
            ).fetchone()
        if row is None or row[0] is None:
            raise KeyError(change_id)
        return self._opened(change_id, row[0])

    def __setitem__(self, change_id: str, change: Change) -> None:
        # Compatibility for mapping callers: every waiting-row write still goes through atomic open.
        self.open(change if change.id == change_id else replace(change, id=change_id))

    def open(self, change: Change) -> tuple[Change, list[str]]:
        """Read, compare and insert under one identity's lock. Unreadable waiting rows are superseded too."""
        from latet.runtime import digest

        wanted = digest(change.config)
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_changes_lock(self.db.schema, self.identity),))
            rows = conn.execute(
                "SELECT id, data FROM changes WHERE identity_id = %s AND state IS NULL ORDER BY created, id FOR UPDATE",
                (self.identity,),
            ).fetchall()
            for change_id, sealed in rows:
                if sealed is None:
                    continue
                try:
                    waiting = self._opened(change_id, sealed)
                except UnreadableChange:
                    continue
                if waiting.base == change.base and digest(waiting.config) == wanted:
                    return waiting, []
            superseded = [change_id for change_id, _ in rows]
            conn.execute(
                "UPDATE changes SET state = 'superseded', ended = %s, data = NULL"
                " WHERE identity_id = %s AND state IS NULL",
                (time.time(), self.identity),
            )
            sealed = self._sealer.seal_json(_change_data(change), self._aad(change.id))
            conn.execute(
                "INSERT INTO changes (identity_id, id, data, created) VALUES (%s, %s, %s, %s)",
                (self.identity, change.id, sealed, change.created),
            )
            if superseded:
                conn.execute(_FORGET_THE_OLDEST, (self.identity, self.identity, SETTLED_KEPT))
        return change, superseded

    def __delitem__(self, change_id: str) -> None:
        with self.db.connection() as conn:
            done = conn.execute(
                "DELETE FROM changes WHERE identity_id = %s AND id = %s AND state IS NULL", (self.identity, change_id)
            ).rowcount
        if not done:
            raise KeyError(change_id)

    def _readable(self) -> list[str]:
        """The ids of the changes waiting that can be read, the oldest first."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, data FROM changes WHERE identity_id = %s AND state IS NULL AND data IS NOT NULL"
                " ORDER BY created",
                (self.identity,),
            ).fetchall()
        found = []
        for change_id, sealed in rows:
            with contextlib.suppress(UnreadableChange):
                self._opened(change_id, sealed)
                found.append(change_id)
        return found

    def __iter__(self) -> Iterator[str]:
        return iter(self._readable())

    def __len__(self) -> int:
        return len(self._readable())


class PgSettled(MutableMapping[str, str]):
    """How an identity's latest changes ended, in PostgreSQL (the change's settings dropped once it ended): the latest
    ``SETTLED_KEPT`` written, the oldest forgotten as one more is. How a change ended first is how it ended: one
    approved on one instance and superseded on another a moment later stays approved (no later state is written over
    it)."""

    def __init__(self, db: Database, identity_id: str) -> None:
        self.db, self.identity = db, identity_id

    def __getitem__(self, change_id: str) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT state FROM changes WHERE identity_id = %s AND id = %s AND state IS NOT NULL",
                (self.identity, change_id),
            ).fetchone()
        if row is None:
            raise KeyError(change_id)
        return str(row[0])

    def __setitem__(self, change_id: str, state: str) -> None:
        now = time.time()
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE changes SET state = %s, ended = %s, data = NULL"
                " WHERE identity_id = %s AND id = %s AND state IS NULL",
                (state, now, self.identity, change_id),
            ).rowcount
            if not done:  # ended already (the first state stays), or never written: kept as it ends now
                conn.execute(
                    "INSERT INTO changes (identity_id, id, data, created, state, ended)"
                    " VALUES (%s, %s, NULL, %s, %s, %s) ON CONFLICT (identity_id, id) DO NOTHING",
                    (self.identity, change_id, now, state, now),
                )
            conn.execute(_FORGET_THE_OLDEST, (self.identity, self.identity, SETTLED_KEPT))

    def end(self, change_id: str, state: str) -> bool:
        """The UPDATE's row count is the receipt for the only call allowed to apply or save this decision."""
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE changes SET state = %s, ended = %s, data = NULL"
                " WHERE identity_id = %s AND id = %s AND state IS NULL",
                (state, time.time(), self.identity, change_id),
            ).rowcount
            if done:
                conn.execute(_FORGET_THE_OLDEST, (self.identity, self.identity, SETTLED_KEPT))
        return bool(done)

    def __delitem__(self, change_id: str) -> None:
        with self.db.connection() as conn:
            done = conn.execute(
                "DELETE FROM changes WHERE identity_id = %s AND id = %s AND state IS NOT NULL",
                (self.identity, change_id),
            ).rowcount
        if not done:
            raise KeyError(change_id)

    def __iter__(self) -> Iterator[str]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM changes WHERE identity_id = %s AND state IS NOT NULL ORDER BY ended", (self.identity,)
            ).fetchall()
        return iter([row[0] for row in rows])

    def __len__(self) -> int:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM changes WHERE identity_id = %s AND state IS NOT NULL", (self.identity,)
            ).fetchone()
        assert row is not None  # COUNT answers a row
        return int(row[0])
