"""The machines' calls to a Latet server, under ``/_latet/server/v1``, with a personal token or an API key in
``x-latet-token``, never from a web page: a computer enrols with its person's one-time code, asks for a sign-in link to
their dashboard (``LOGIN_CODES`` a minute for each person, R-28), says who it is, sends its person's identity, and
forgets its own token (which closes its person's space on this instance at once and ends their sign-ins, as any revoked
token does). An API key has no dashboard and no computer.

A computer gets two secrets at enrolment, each shown once: its token, which its agents hold (Claude Code's plugin calls
the bridge with it, so every command the agent runs inherits it), and its computer's key, which its CLI alone keeps
(R16). The token alone says who it is, opens the bridge, the space's proxy and the computer's two calls at an
agent's start (a destination added for an address, which lowers nothing; the user name's fake), and revokes itself; a
sign-in link to the dashboard and a change of the person's identity also ask for the key, in ``x-latet-computer-key``
(``keyed``), compared by its keyed hash in constant time with what PostgreSQL holds of the token's row now. A key
missing or wrong is refused (403, ``computer_key_required``) in words that never say which, and counts as a miss of its
address, as an unknown token does; a token made before computer keys is told to connect again (403,
``computer_key_missing``).

What a computer sends of its person's identity is added to what their other computers sent: their user names, emails
and host names united (each once, whatever its case; ``NAMES_KEPT`` of each at most), their full name taken where none
is held, and nothing ever taken away, nor their identity's masking turned off. A computer describes itself; it does not
lower its person's protection (R-28, the pre-read's M5): only their dashboard, or an administrator, removes a name. A
person has one such call under way at a time.

The limiter the gate, the machines' calls and the administration share (``checked``): a credential (a token, an API
key, a route key, a dashboard's session) that this instance holds in its memory costs no store; one found valid in the
last hour (``Known``, by keyed hash, on any instance: Valkey keeps the hashes for an hour) skips its bucket's limit and
is looked up all the same, so that a stale computer behind the same address locks out none of its colleagues, after a
restart or on a new instance too, and one revoked meanwhile is refused, forgotten and counted; any other takes a slot of
its bucket's window first (``Bus.take``, one step: however many come at once, no more lookups than the limit), past the
limit is refused with no lookup (429, ``Retry-After``), and gives its slot back when it proves valid. A credential is
looked up one request at a time (``OneAtATime``): one identity's flood under way when its few seconds in the memory end
takes one lookup, not one for each request. A bucket the shared limiter found past its limit is remembered so in this
instance's memory until its window ends (``Limits``): its credentials are refused there on the event loop, with no
thread taken and no round trip, but one this instance's own memory found valid in the last hour, and one found valid
on any instance, read in Valkey a batch at a time, a few reads a second (``KnownReads``, through ``admitted``), so that
a flood from one address costs the threads its limit's worth of lookups a window and a few reads a second, and shuts
out none of the colleagues who share the address. The limits count a client by its bucket (``address``): an IPv6
address by its /64, since a host is often given a whole one and would otherwise have 2^64 buckets, and its misses by
its /48 too, which counts as one host (``AGGREGATE_PREFIX``); an IPv4 address written as IPv6 by the IPv4 address it
is. One budget counts every kind of miss of a bucket (``miss_key``): an unknown address, token, dashboard session,
administration token, and a dashboard's sign-in link whose code opens nothing (in the server's app). So a sign-in link
opened from an address whose budget is spent is refused (429) for as long as anyone behind the address keeps spending
it, a stranger or a stale computer's retries alike, where a known token, address or session passes. When PostgreSQL or
Valkey does not answer, the call is refused (503), nothing done, and the error is said in the log by its type, once a
minute; and so is a call that finds ``CREDENTIALS_WAITING`` calls waiting for the credentials' threads already (503
too, with ``Retry-After``), whatever many addresses send them. Past half that (``CREDENTIALS_BUSY``), what this
instance does not hold, nor any instance found valid in the last hour, is refused at once on the event loop (503,
``Retry-After``: a sign-in link, an enrolment, a credential new to every instance), so that the other half stays for
what is held or known, and nothing is recorded in those threads for a call refused so. Only a flood that outpaces the
threads keeps it so, for as long as it lasts: some 690 buckets at their limits, as measured here, fewer with stores
on other hosts (``CREDENTIALS_BUSY``'s words). A stale computer's retries count under its own address like any miss.

A successful enrolment gives back the slot it took of its bucket's limit, so that an office behind one address enrols
its computers without waiting: the limit counts the codes that fail. A refusal of a person's settings (what would drop
the items set for everyone, a term too long) is answered as such (409), never as a store that did not answer (503).

Who calls: the event loop, each handler. The limiter and the lookups of credentials not yet known, which answer in
milliseconds, and nothing else, run in the few threads of ``off_loop`` (through ``pre_auth``, which fails closed), whose
calls waiting are bounded; what may wait on a person (their settings applied, under their runtime's lock) runs in
threads of its own (``in_work``); and what may wait on the administration log's chain or a row, never on a person (an
enrolment and a revoked token with their entries, a revoked token's keys ended, the entries of the server's own), in
threads of their own too (``in_store``), so that the people's locks held for seconds delay no entry. None of them is the
event loop, nor the default executor, which the spaces' builds hold for seconds. Readiness asks the stores in threads of
its own (``app._READINESS``), and a use of a token or an identity is noted in a thread of its own
(``identities.Notes``): neither waits behind the lookups, nor they behind a row that a long transaction holds."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from latet import __version__
from latet.api.rest import UI, _saved_change_pending
from latet.config import Config, IdentityConfig
from latet.http_inputs import bounded_body
from latet.logline import Paced, described
from latet.server.logs import REFUSED
from latet.server.settings import SERVER_API
from latet.server.shared import SettingsSavedChangePending, SharedItemsError
from latet.server.spaces import IdentityUnavailable, SpaceUnavailable
from latet.server.tokens import KEY_PREFIX, TOKEN_PREFIX, bearer_token, route_key

if TYPE_CHECKING:
    from latet.server.app import Server
    from latet.server.bus import Bus
    from latet.server.grants import Grant
    from latet.server.identities import Identity

log = logging.getLogger(__name__)

ENROL_LIMIT = 10  # codes tried from one bucket…
ENROL_WINDOW = 600.0  # …in ten minutes
# What a bucket presents that the server does not know, every kind together (an address, a token or an API key, a
# dashboard's session, an administration token, a sign-in link: ``miss_key``), one budget…
LOOKUP_LIMIT = 60
LOOKUP_WINDOW = 60.0  # …in a minute (a known one costs nothing: its slot is given back)
RETRY_AFTER = str(int(LOOKUP_WINDOW))  # what a 429 of a limit tells its client to wait, in seconds: a window at most
LOGIN_CODES = 10  # sign-in links asked for one person…
LOGIN_CODE_WINDOW = 60.0  # …in a minute (R-28)
KNOWN_SECONDS = 3600.0  # how long a credential found valid skips its bucket's limit
KNOWN_SIZE = 10_000  # credentials an instance's memory keeps so at most: the one found valid longest ago goes first
NAMES_KEPT = 32  # user names, emails and host names each, that a person's computers add to their identity at most
NAME_CHARS = 320  # characters of one of them, and of a full name, at most (an email's longest)
CODE_CHARS = 128  # characters of what is tried as a one-time code at most (one is 43: its prefix and 32; an older, 32)
LABEL_CHARS = 1_000  # characters of a computer's label read at most (Identities keeps 100 of them, cleaned)
# Bytes of a body read at most, and parsed on the event loop: a code and a label; a person's identity from a computer
# (NAMES_KEPT texts of each kind, in any script). Past them the call is refused unparsed (413), whatever max_request_mb
# lets through: a body that size would hold every identity's streams while it is parsed.
ENROL_BYTES = 16 * 1024
IDENTITY_BYTES = 256 * 1024
BUSY_SECONDS = 1  # what a computer is told to wait while its person's identity is being applied (Retry-After)
# What a sign-in link and a person's identity ask for besides the token (R16), and what their refusals say: the same
# words for a key missing and a key wrong, which never say which part was wrong.
COMPUTER_KEY_HEADER = b"x-latet-computer-key"
COMPUTER_KEY_WORDS = (
    "this call needs the computer key that `latet connect` kept with this computer's token, which `latet dashboard` "
    "and `latet identity send` send from the computer's own terminal"
)
KEYLESS_WORDS = (
    "this computer was connected before computer keys: `latet disconnect`, then `latet connect` with a new code from "
    "an administrator, which gives it its key"
)
# The threads of what is done before a credential is known: only what answers in milliseconds while the stores do (the
# limiter, the lookups of tokens, route keys and sessions, the sign-in, readiness). They start at the first call. The
# event loop's own (``asyncio.to_thread``) are every space's, whose builds hold them for seconds: a lookup that waited
# behind them would hold up every identity's requests.
CREDENTIAL_THREADS = 4
_CREDENTIALS = ThreadPoolExecutor(CREDENTIAL_THREADS, thread_name_prefix="latet-credentials")
# Calls waiting for those threads at most, the ones running included: a second of their work, about, when the stores
# answer in a millisecond or two. Past it a call is refused at once (503, ``Retry-After``), so that no flood, from
# however many addresses, makes every lookup wait behind it without end.
CREDENTIALS_WAITING = 1_000
# Calls waiting for those threads past which a credential this instance does not hold, and no instance found valid in
# the last hour, is refused at once on the event loop (503, ``Retry-After``): half the bound, the other half kept for
# what is held or known. Only arrivals that outpace the threads fill it. Measured here (a desktop whose stores run in
# local containers; stores on other hosts take longer), four threads take about 1,200 misses a second (a read, a slot
# and a lookup each: 0.83 ms of them) and about 4,900 round trips of Valkey's alone (0.20 ms: a slot, a take). A
# bucket's budget, spent at once if it likes, is 61 misses and 61 calls refused before their token is read a minute
# (the /48 of an IPv6 one counts as one bucket). Spent on the administration, which records a refused call within the
# server's ceiling of entries (a take more each), its misses cost 1.04 ms each and those refusals 0.41 ms (a slot and a
# take): about 0.088 s of the threads a minute. So some 690 buckets at their limits hold the mark a whole minute, some
# 345 half of it; one address's burst holds the mark some 50 ms a window. While they do, an enrolment and a credential
# new to every instance are refused (503, ``Retry-After``), and what is held or known passes.
CREDENTIALS_BUSY = 500
QUEUED_RETRY_AFTER = "1"  # what a call refused because those threads are busy is told to wait, in seconds
BUCKETS_KEPT = 10_000  # buckets past their limit an instance remembers at most: the one remembered longest ago goes
# An IPv6 address's misses counted under its /48 too, beside its /64 (``bucket_of``): a host given a /48, which tunnel
# brokers hand out free, has 65,536 /64 buckets, and counts as one for misses, as any other host does.
AGGREGATE_PREFIX = 48
# Valkey's reads of what was found valid on any instance, for the credentials refused on the event loop (their address
# past its limit, or the credentials' threads busy: ``CREDENTIALS_BUSY``) that this instance's memory does not hold:
# ``KNOWN_READS`` a second at most for each kind of credential, each for every one waiting, ``KNOWN_BATCH`` at most
# (past it, one is refused at once, as the flood it belongs to).
KNOWN_READS = 4
KNOWN_BATCH = 2_000
# The thread of those reads, never behind the credentials' threads: what a flood that fills them waits for is the
# read that refuses it (``CREDENTIALS_BUSY``). A few reads a second for each kind, each one round trip.
_READS = ThreadPoolExecutor(1, thread_name_prefix="latet-known-reads")
# The threads of what may wait on a person: their settings applied (their runtime's lock, held for seconds by a resync
# or a save). The lookups never wait behind them.
WORK_THREADS = 8
_WORK = ThreadPoolExecutor(WORK_THREADS, thread_name_prefix="latet-work")
# The threads of what may wait on the administration log's chain (one entry at a time for the server) or on a row,
# never on a person: an enrolment and a revoked token with their entries, a revoked token's keys ended, the server's
# own entries. Neither the lookups nor the people's settings wait behind them, nor they behind the people's locks.
STORE_THREADS = 4
_STORE = ThreadPoolExecutor(STORE_THREADS, thread_name_prefix="latet-store")
# The threads of the applications API's entries in the administration log (an unmask's, written before its answer):
# they wait on the log's chain as the store threads' entries do, apart from them, so that a key's burst of unmasks
# (``requests_in_flight`` of them) queues behind the applications' own entries alone, never ahead of an enrolment, a
# revocation or the server's own work. Two: one entry being written, the next waiting on the chain; the chain writes
# one at a time whatever waits on it.
APPS_LOG_THREADS = 2
_APPS_LOG = ThreadPoolExecutor(APPS_LOG_THREADS, thread_name_prefix="latet-apps-log")
# The threads of the applications API's forgets (Task 11b), each with its entry of the administration log in its own
# transaction: a forget gives the key's older reviewer answers and history rows their keyed hashes first (seconds, once
# after an upgrade), then waits on rows and on the log's chain. Apart from the unmasks' entries (review 11b's I2), so
# that a key's forgets never hold back any key's unmasks, nor an enrolment or a revocation: they queue behind each other
# alone, two at a time for the server.
FORGET_THREADS = 2
_FORGET = ThreadPoolExecutor(FORGET_THREADS, thread_name_prefix="latet-forget")
UNANSWERED = Paced()  # work that did not finish for the gate or a machine's call: said once a minute
UNBUILT = Paced()  # a person's space that could not be built for a machine's call: said once a minute
SHED = Paced()  # credentials refused because the credentials' threads were busy (``CREDENTIALS_BUSY``): once a minute
KNOWN_UNANSWERED = Paced()  # Valkey's failures to keep or read what was found valid: once a minute for the instance


class TooManyMisses(Exception):
    """A bucket presenting credentials this instance does not know, past its limit: refused for a while (429,
    ``Retry-After``), nothing looked up."""


class StoresUnavailable(Exception):
    """Work for the gate or a machine's call did not finish: the request is refused (503). Earlier work may have been
    kept; callers must handle known partial outcomes before this wrapper. Its words are the error's type alone: the
    driver's own can quote an address or a value. ``retry_after``: what the refusal tells its client to wait
    (``Retry-After``), when it knows."""

    retry_after: str | None = None


class CredentialsBusy(StoresUnavailable):
    """``CREDENTIALS_WAITING`` calls wait for the credentials' threads already: this one is refused at once (503, with
    ``Retry-After``), nothing of it looked up."""

    retry_after = QUEUED_RETRY_AFTER


def miss_key(bucket: str) -> str:
    """The limit that counts ``bucket``'s misses, one budget for every kind (``LOOKUP_LIMIT`` a ``LOOKUP_WINDOW``): an
    address, a token or an API key, a dashboard's session, an administration token, a sign-in link that the server does
    not know each spends one of them, so that no address spends five budgets at once. An IPv6 bucket's /48 counts too
    (``_counting``)."""
    return f"miss:{bucket}"


def _counting(key: str) -> tuple[str, ...]:
    """The limits a miss under ``key`` (``<kind>:<bucket>``) counts in: its bucket's, and for an IPv6 bucket its /48's
    too (``AGGREGATE_PREFIX``)."""
    kind, _, bucket = key.partition(":")
    if ":" not in bucket or "/" not in bucket:  # an IPv6 bucket is a network: an IPv4 one has no colon
        return (key,)
    try:
        network = ipaddress.IPv6Network(bucket)
    except ValueError:
        return (key,)
    if network.prefixlen <= AGGREGATE_PREFIX:
        return (key,)
    return key, f"{kind}:{network.supernet(new_prefix=AGGREGATE_PREFIX)}"


class Limits:
    """The buckets the shared limiter (``Bus.slot``) found past their limit, each remembered so in this instance's
    memory until its window there ends, an IPv6 address's /48 counted as one too (``_counting``): a credential that such
    a bucket presents is refused on the event loop, and in a credentials' thread that finds it so, with no thread taken
    and no round trip (``refused_here``), but one this instance's own memory found valid in the last hour
    (``Known.holds_here``), and one found valid on any instance, read in Valkey a batch at a time (``KnownReads``,
    through ``admitted``). ``BUCKETS_KEPT`` at most, the one remembered longest ago forgotten first: its next credential
    asks Valkey again. Safe across threads; ``clock``: seconds that never go back, for tests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until: OrderedDict[str, float] = OrderedDict()  # the bucket's key: until when it is past its limit

    def past(self, key: str, seconds: float) -> None:
        """The bucket of ``key`` is past its limit for ``seconds`` more, as the shared limiter says."""
        until = self._clock() + max(0.0, seconds)
        with self._lock:
            self._until.pop(key, None)
            self._until[key] = until
            while len(self._until) > BUCKETS_KEPT:
                self._until.popitem(last=False)

    def refuses(self, key: str) -> bool:
        """Whether a credential under ``key`` is refused now, as this instance remembers it: its bucket, or an IPv6
        bucket's /48, past its limit."""
        now = self._clock()
        with self._lock:
            return any(self._is_past(found, now) for found in _counting(key))

    def _is_past(self, key: str, now: float) -> bool:
        """Under the lock: whether the bucket of ``key`` is remembered past its limit at ``now`` (one whose window
        ended is forgotten)."""
        until = self._until.get(key)
        if until is None:
            return False
        if now < until:
            return True
        del self._until[key]
        return False


def counted(bus: Bus, limits: Limits, key: str, limit: int, window: float) -> bool:
    """In a worker thread: a slot of ``key``'s bucket taken in the shared limiter (``Bus.slot``), and of an IPv6
    bucket's /48 too (``_counting``), and whether each is within ``limit``; one past it is remembered so in ``limits``
    until its window ends."""
    for found in _counting(key):
        left = bus.slot(found, limit, window)
        if left is not None:
            limits.past(found, left)
            return False  # past here: the next is not asked
    return True


def given_back(bus: Bus, key: str) -> None:
    """In a worker thread: the slots ``counted`` took under ``key`` given back, what they guarded proved valid: no miss
    of its bucket's, nor of an IPv6 bucket's /48."""
    for found in _counting(key):
        bus.give_back(found)


class Known:
    """The credentials found valid in the last ``KNOWN_SECONDS``, kept by a keyed hash (the memory holds no credential,
    nor a hash anyone without the key could make), ``KNOWN_SIZE`` at most in this instance's memory. One of them skips
    its bucket's limit, so that strangers who share an address with its holder cannot shut it out; it is looked up all
    the same, so that one revoked or expired meanwhile is refused, forgotten, and counted as any miss.

    On a server (``bus`` given, and ``key``, the same on every instance: ``MasterKey.derive(b"known")``), each hash is
    kept in Valkey too, under ``known:<kind>:<hash>`` for ``KNOWN_SECONDS``, so that a credential found valid on one
    instance skips its limit on another, and on a new or restarted instance from its first try. This instance's memory
    answers first, and alone when Valkey does not answer (said in the log once a minute, by the error's type: nothing
    else changes, the limit applying to what the memory does not hold). Its methods then wait on Valkey: threads call
    them, never the event loop. ``seconds`` sets the life (an hour by default); ``memory=False`` keeps no local copy,
    as one-time codes require, and admission reads their own known set even before an address is limited. Without
    ``bus``, the memory is this instance's alone, under a key of its own. Safe across threads; ``clock``: seconds that
    never go back, for tests."""

    def __init__(
        self,
        bus: Bus | None = None,
        key: bytes | None = None,
        *,
        kind: str = "",
        seconds: float = KNOWN_SECONDS,
        memory: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = key if key is not None else secrets.token_bytes(32)
        self._bus, self._kind = bus, kind
        self.seconds, self.memory = seconds, memory
        self._clock = clock
        self._lock = threading.Lock()
        self._found: OrderedDict[bytes, float] = OrderedDict()  # the hash: when it was last found valid here
        self.reads = KnownReads(self)  # Valkey's reads for the credentials refused on the event loop, a batch at a time

    def _hash(self, secret: str) -> bytes:
        """The keyed hash of ``secret`` as a credential of this kind: the same secret of another kind hashes apart."""
        return hmac.new(self._key, f"{self._kind}\0{secret}".encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def hashed(self, secret: str) -> bytes:
        """The keyed hash ``secret`` is known by (what ``held_elsewhere`` is asked about): no store is asked."""
        return self._hash(secret)

    def held_elsewhere(self, hashes: list[bytes]) -> set[bytes]:
        """In a thread: those of ``hashes`` that Valkey's known set holds (found valid on any instance in the last
        ``KNOWN_SECONDS``), read in one round trip; none without Valkey, or when it does not answer (said once a
        minute)."""
        if self._bus is None or not hashes:
            return set()
        try:
            pipe = self._bus.client.pipeline(transaction=False)
            for hashed in hashes:
                pipe.exists(self._bus.key(self._name(hashed)))
            there = pipe.execute()
        except Exception as exc:  # its words can name Valkey's address: its type alone
            self._said("read from", exc)
            return set()
        return {hashed for hashed, found in zip(hashes, there, strict=True) if found}

    def _name(self, hashed: bytes) -> str:
        return f"known:{self._kind}:{hashed.hex()}"

    def _said(self, doing: str, exc: Exception) -> None:
        if count := KNOWN_UNANSWERED.due():
            log.warning(
                "the credentials found valid could not be %s Valkey (%s): %s%s",
                doing,
                type(exc).__name__,
                "this instance's memory answers alone" if self.memory else "the code is counted as unknown",
                f" ({count} times since the line before)" if count > 1 else "",
            )

    def holds_here(self, secret: str) -> bool:
        """Whether ``secret`` was found valid in the last ``KNOWN_SECONDS`` as this instance's memory holds it: no store
        is asked, so the event loop asks it too."""
        if not self.memory:
            return False
        hashed, now = self._hash(secret), self._clock()
        with self._lock:
            found = self._found.get(hashed)
            return found is not None and 0 <= now - found < self.seconds

    def holds(self, secret: str) -> bool:
        """Whether ``secret`` was found valid in the last ``KNOWN_SECONDS``: here, or on any instance (Valkey)."""
        if self.holds_here(secret):
            return True
        hashed = self._hash(secret)
        if self._bus is None:
            return False
        try:
            return self._bus.get(self._name(hashed)) is not None
        except Exception as exc:  # its words can name Valkey's address: its type alone
            self._said("read from", exc)
            return False

    def keep(self, secret: str) -> None:
        """``secret`` found valid now: kept for its configured life, in Valkey and optionally here."""
        hashed, now = self._hash(secret), self._clock()
        if self.memory:
            with self._lock:
                self._found[hashed] = now
                self._found.move_to_end(hashed)
                while len(self._found) > KNOWN_SIZE:
                    self._found.popitem(last=False)
        if self._bus is not None:
            try:
                self._bus.set(self._name(hashed), b"1", self.seconds)
            except Exception as exc:
                self._said("kept in", exc)

    def forget(self, secret: str) -> None:
        """``secret`` refused now (revoked, expired): it is limited again as any other, here and in Valkey."""
        hashed = self._hash(secret)
        with self._lock:
            self._found.pop(hashed, None)
        if self._bus is not None:
            try:
                self._bus.delete(self._name(hashed))
            except Exception as exc:
                self._said("forgotten in", exc)


class KnownReads:
    """For the credentials refused on the event loop (their address past its limit, or the credentials' threads busy:
    ``CREDENTIALS_BUSY``) that this instance's memory does not hold: whether one was found valid on any instance in the
    last hour, read in Valkey's known set a batch at a time, every credential waiting in one round trip
    (``Known.held_elsewhere``), ``KNOWN_READS`` a second at most, ``KNOWN_BATCH`` at most each (past it, refused at
    once). So a colleague's credential known only elsewhere (a dashboard session opened on another instance, every token
    after a restart or on a new instance) passes beside a stranger who floods the address they share at each window,
    and the flood costs a few round trips a second, whatever its size, in a thread of their own (``_READS``), never
    behind the credentials' threads it may fill. On the event loop; a new loop starts afresh."""

    def __init__(self, known: Known, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.known = known
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: dict[bytes, list[asyncio.Future[bool]]] = {}  # by keyed hash: what waits for the next read
        self._reader: asyncio.Task[None] | None = None
        self._last = -1.0  # when the last read began

    async def held(self, credential: str) -> bool:
        """Whether ``credential`` was found valid on any instance in the last hour, read with the others waiting; False
        at once when ``KNOWN_BATCH`` wait already, or when Valkey does not answer."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._waiting, self._reader, self._last = loop, {}, None, -1.0
        hashed = self.known.hashed(credential)
        if hashed not in self._waiting and len(self._waiting) >= KNOWN_BATCH:
            return False
        waiter: asyncio.Future[bool] = loop.create_future()
        self._waiting.setdefault(hashed, []).append(waiter)
        if self._reader is None or self._reader.done():
            self._reader = loop.create_task(self._read(), name="latet-known-reads")
        return await waiter

    async def _read(self) -> None:
        """The reads, one at a time, each at least ``1 / KNOWN_READS`` seconds after the one before, until nothing
        waits."""
        while self._waiting:
            if (wait := self._last + 1 / KNOWN_READS - self._clock()) > 0:
                await asyncio.sleep(wait)
            batch, self._waiting = self._waiting, {}
            self._last = self._clock()
            try:  # in a thread of its own (``_READS``): a flood that fills the credentials' threads delays it not
                held = await asyncio.get_running_loop().run_in_executor(_READS, self.known.held_elsewhere, list(batch))
            except Exception:  # read by nobody (the process stopping): none of them passes
                held = set()
            for hashed, waiters in batch.items():
                for waiter in waiters:
                    if not waiter.done():  # else its request went meanwhile
                        waiter.set_result(hashed in held)


class OneAtATime:
    """The lookups of credentials an instance's memory does not hold (a token or an API key, a route key, a dashboard's
    session), one at a time for each credential: a request that comes while one is under way waits for its answer on
    the event loop, so that a flood of one credential's requests, when its few seconds in the memory are over, takes one
    credentials' thread and not one for each request it has under way then. A request joins only the lookup of its own
    credential, by its keyed hash (``Known.hashed``: no request rides into another's), begun in the same generation of
    the memory it is held in (``Identities.generation``, ``ValkeySignIns.generation``: once a change or an end was
    heard, it joins none begun before, which may answer what that made old). The lookup runs on, its answer kept for the
    others, when the request that began it goes. An instance keeps one (``Server.lookups``); on the event loop, and a
    new loop starts afresh."""

    def __init__(self) -> None:
        # By kind and keyed hash: the generation each lookup under way began in, and the lookup.
        self._running: dict[tuple[str, bytes], tuple[int, asyncio.Task[Any]]] = {}

    async def run[T](
        self, kind: str, hashed: bytes, generation: int, lookup: Callable[[], Coroutine[Any, Any, T]]
    ) -> T:
        """What ``lookup()`` answers for the credential of ``kind`` whose keyed hash is ``hashed``: the answer of the
        lookup of it under way since ``generation``, or of a new one."""
        loop, key = asyncio.get_running_loop(), (kind, hashed)
        found = self._running.get(key)
        if found is not None and found[0] == generation and found[1].get_loop() is loop:
            running = found[1]
        else:
            running = loop.create_task(lookup(), name=f"latet-{kind}-lookup")
            self._running[key] = (generation, running)
            running.add_done_callback(functools.partial(self._done, key))
        return await asyncio.shield(running)

    def _done(self, key: tuple[str, bytes], task: asyncio.Task[Any]) -> None:
        """A lookup ended: the next request of its credential looks it up afresh (its error read here, so that a lookup
        whose requests all went says nothing of it)."""
        found = self._running.get(key)
        if found is not None and found[1] is task:
            del self._running[key]
        if not task.cancelled():
            task.exception()

    def __len__(self) -> int:
        """How many lookups are under way."""
        return len(self._running)


async def admitted(known: Known, limits: Limits, credential: str, key: str) -> bool | None:
    """On the event loop, before anything else is asked of a thread or a store, for a credential this instance's memory
    does not hold (``Known.holds_here``) whose bucket is past its limit as this instance remembers it, or that comes
    while the credentials' threads are busy (``CREDENTIALS_BUSY``): True when it was found valid on any instance in the
    last hour (``KnownReads``: it skips the limit, and is looked up all the same, ``checked``'s ``known_elsewhere``);
    else None for a bucket past its limit (429), and CredentialsBusy raised while the threads are busy (503, ``shed``).
    False for any other: it goes on to ``checked`` as ever. A memoryless known set (one-time codes) is always read here,
    so its credentials job needs no further known-set read and knows whether it took a slot."""
    if known.holds_here(credential):
        return False
    limited, busy = limits.refuses(key), credentials_busy()
    if not limited and not busy and known.memory:
        return False
    if await known.reads.held(credential):
        return True
    if limited:
        return None
    if busy:
        raise shed("a sign-in link" if known._kind == "code" else "a credential")
    return False


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _refused(
    request: Request, message: str, status: int, *, retry_after: str | None = None, reason: str | None = None
) -> JSONResponse:
    """A refusal the server makes of its own (a limit, a web page, a store that does not answer), marked as such in the
    request's scope: the log paces the 429s the server gives apart from those an upstream gives (Task 13).
    ``retry_after``: the seconds a limit tells its client to wait (``Retry-After``); ``reason``: a fixed word a client
    tells the refusal apart by."""
    request.scope[REFUSED] = True
    answer = _error(message, status) if reason is None else JSONResponse({"error": message, "reason": reason}, status)
    if retry_after is not None:
        answer.headers["retry-after"] = retry_after
    return answer


def client_of(scope: Mapping[str, Any]) -> str:
    """The client's address as the server sees it: the proxy's word, since uvicorn takes X-Forwarded-For from the
    addresses of ``forwarded_allow_ips`` alone. What the administration log records of a call."""
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def bucket_of(client: str) -> str:
    """The bucket the limits count ``client`` in: an IPv6 address's /64 (its zone left out), an IPv4 address written as
    IPv6 (``::ffff:198.51.100.7``) as that IPv4 address, any other address as it is written by Python, and what is no
    address (a test client's name, ``unknown``) as it is."""
    try:
        found = ipaddress.ip_address(client.partition("%")[0])
    except ValueError:
        return client
    if isinstance(found, ipaddress.IPv6Address):
        if found.ipv4_mapped is not None:
            return str(found.ipv4_mapped)
        return str(ipaddress.IPv6Network((found, 64), strict=False))
    return str(found)


def address(request: Request) -> str:
    """The bucket the limits count the request's client in (``bucket_of``)."""
    return bucket_of(client_of(request.scope))


def from_a_page(scope: Mapping[str, Any]) -> bool:
    """Whether a browser made the request: it says its origin, or how it stands to the page that made it
    (``sec-fetch-site``, whatever its value, which only a browser sends, for a page of the server's own as for a
    stranger's). Stricter than ``gateway.security.from_browser``, which lets a same-origin request through for a
    computer's dashboard: an identity's address and the machines' calls are no page's to call, the server's own pages
    included (the administration reads a request alike)."""
    return any(name in (b"origin", b"sec-fetch-site") for name, _ in scope.get("headers") or [])


def token_of(scope: Mapping[str, Any]) -> str:
    """The ``x-latet-token`` a request carries, read the same way by the gate and by every machine's call (the
    pre-read's M1): its value, or "" when it is not there or is there more than once (a request that names two tokens
    names none, so that no two readings of it disagree on whose it is)."""
    found = [value for name, value in scope.get("headers") or [] if name == b"x-latet-token"]
    return found[0].decode("latin-1") if len(found) == 1 else ""


def bearer_of(scope: Mapping[str, Any]) -> str:
    """The token of a request's one ``Authorization`` header (``Bearer`` in any case, one space or more as RFC 7235
    allows, the token: the applications API's), read as the metrics' scrape reads it (``tokens.bearer_token``), and
    decoded as ``token_of`` decodes ``x-latet-token``: "" when there is none, there are two, or it is written otherwise
    (another scheme, no token)."""
    token = bearer_token(scope)
    return token.decode("latin-1") if token is not None else ""


def key_of(scope: Mapping[str, Any]) -> str:
    """The computer's key a request carries (``x-latet-computer-key``), read as ``token_of`` reads the token: "" when
    it is not there or is there more than once."""
    found = [value for name, value in scope.get("headers") or [] if name == COMPUTER_KEY_HEADER]
    return found[0].decode("latin-1") if len(found) == 1 else ""


def routes_of(url: str, secret: str) -> dict[str, str]:
    """The addresses an agent or a program uses with ``secret``: its own, Anthropic's and OpenAI's under it."""
    base = f"{url}/u/{route_key(secret)}"
    return {"base": base, "anthropic": f"{base}/anthropic", "openai": f"{base}/openai/v1"}


def _object_of(raw: bytes) -> dict[str, Any] | None:
    """``raw`` when it is a JSON object; None for anything else."""
    try:
        found = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


async def _within(request: Request, limit: int) -> bytes | None:
    """The request's body when it is ``limit`` bytes at most, read no further than that: None past it."""
    body = await bounded_body(request, limit)
    return bytes(body) if body is not None else None


class _Waiting:
    """How many calls wait for the credentials' threads, the ones running included (every event loop of the process
    shares those threads: a lock counts them)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def enter(self) -> bool:
        """One call more, unless ``CREDENTIALS_WAITING`` wait already: whether it may wait."""
        with self._lock:
            if self.count >= CREDENTIALS_WAITING:
                return False
            self.count += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self.count -= 1

    def busy(self) -> bool:
        """Whether ``CREDENTIALS_BUSY`` calls wait already (a count read with no lock, on the event loop)."""
        return self.count >= CREDENTIALS_BUSY


_WAITING = _Waiting()
BUSY = Paced()  # calls refused because the credentials' threads were busy: said once a minute


def waiting_calls() -> int:
    """How many calls wait for the credentials' threads now, the ones running included (E0c's metrics)."""
    return _WAITING.count


def credentials_busy() -> bool:
    """Whether the credentials' threads are busy (``CREDENTIALS_BUSY`` calls waiting already): what this instance does
    not hold, nor any instance found valid in the last hour, is then refused at once (``shed``)."""
    return _WAITING.busy()


def shed(what: str) -> CredentialsBusy:
    """The refusal of ``what`` (a credential this instance does not hold, a sign-in link, an enrolment) while the
    credentials' threads are busy: 503 with ``Retry-After``, said in the log once a minute with how many."""
    if count := SHED.due():
        log.warning(
            "the credentials' threads are busy (%d calls waiting): %s this instance does not hold was refused (503)%s",
            CREDENTIALS_BUSY,
            what,
            f" ({count} times since the line before)" if count > 1 else "",
        )
    return CredentialsBusy(CredentialsBusy.__name__)


async def off_loop[T](work: Callable[..., T], *args: Any) -> T:
    """``work`` in the threads of what is done before a credential is known (``CREDENTIAL_THREADS``): the limiter and
    the lookups alone, which answer in milliseconds. Raises CredentialsBusy, nothing done, when ``CREDENTIALS_WAITING``
    calls wait for them already (said in the log once a minute)."""
    if not _WAITING.enter():
        if count := BUSY.due():
            log.warning(
                "%d calls wait for the credentials' threads: one more was refused (503)%s",
                CREDENTIALS_WAITING,
                f" ({count} times since the line before)" if count > 1 else "",
            )
        raise CredentialsBusy(CredentialsBusy.__name__)
    try:
        return await asyncio.get_running_loop().run_in_executor(_CREDENTIALS, functools.partial(work, *args))
    finally:
        _WAITING.leave()


async def _answered[T](call: Awaitable[T]) -> T:
    """What ``call`` answers. Raises StoresUnavailable, by the error's type alone, for whatever it raised (said in the
    log once a minute): the request is refused closed. CredentialsBusy goes on as it is (said by ``off_loop``)."""
    kind = ""
    try:
        return await call
    except CredentialsBusy:
        raise
    except Exception as exc:  # its words can name an address or quote a value: its type alone goes on
        kind = described(exc)
    if count := UNANSWERED.due():
        log.warning(
            "server work did not finish (%s): the request was refused%s",
            kind,
            f" ({count} times since the line before)" if count > 1 else "",
        )
    raise StoresUnavailable(kind)


async def pre_auth[T](work: Callable[..., T], *args: Any) -> T:
    """``off_loop`` for the gate and the machines' calls. Raises StoresUnavailable."""
    return await _answered(off_loop(work, *args))


async def in_work[T](work: Callable[..., T], *args: Any) -> T:
    """``work(*args)`` in the threads of what may wait on a person (``WORK_THREADS``). Raises StoresUnavailable."""
    loop = asyncio.get_running_loop()
    return await _answered(loop.run_in_executor(_WORK, functools.partial(work, *args)))


async def in_store[T](work: Callable[..., T], *args: Any) -> T:
    """``work(*args)`` in the threads of what may wait on the administration log's chain or a row, never on a person
    (``STORE_THREADS``). Raises StoresUnavailable."""
    loop = asyncio.get_running_loop()
    return await _answered(loop.run_in_executor(_STORE, functools.partial(work, *args)))


async def in_log[T](work: Callable[..., T], *args: Any) -> T:
    """``work(*args)`` in the threads of the applications API's entries in the administration log
    (``APPS_LOG_THREADS``), never in the store threads. Raises StoresUnavailable."""
    loop = asyncio.get_running_loop()
    return await _answered(loop.run_in_executor(_APPS_LOG, functools.partial(work, *args)))


async def in_forget[T](work: Callable[..., T], *args: Any) -> T:
    """``work(*args)`` in the threads of the applications API's forgets (``FORGET_THREADS``), never in those of its
    unmasks' entries nor in the store threads. Raises StoresUnavailable."""
    loop = asyncio.get_running_loop()
    return await _answered(loop.run_in_executor(_FORGET, functools.partial(work, *args)))


def refused_here(limits: Limits, known: Known, credential: str, key: str) -> bool:
    """Whether ``credential`` is refused before anything is asked of a thread or a store: its bucket (``key``) is past
    its limit as this instance remembers it (``Limits``), and this instance's own memory did not find it valid in the
    last hour. In the credentials' threads, for what was queued before the limit spoke; on the event loop, ``admitted``
    asks it, and reads what was found valid elsewhere."""
    return limits.refuses(key) and not known.holds_here(credential)


def checked[T](
    bus: Bus,
    known: Known,
    limits: Limits,
    credential: str,
    key: str,
    limit: int,
    window: float,
    lookup: Callable[[str], T | None],
    known_elsewhere: bool = False,
) -> tuple[T | None, bool]:
    """In the threads of ``off_loop``: what ``lookup`` finds of ``credential``, and whether the limit ``key`` counts
    refused it (then nothing is looked up). A bucket ``limits`` remembers past its limit is refused with no round trip
    (``refused_here``: a flood waiting for the threads costs them nothing once the limit has said so), but one
    ``admitted`` found valid elsewhere (``known_elsewhere``). One ``known`` holds skips the limit and is looked up all
    the same; any other takes a slot first (one past the limit has its bucket remembered so in ``limits``), and gives
    it back when it proves valid. One found valid is kept in ``known``; one it held and found gone (revoked, rotated,
    signed out, disabled) is forgotten, and counted as any miss."""
    if not known_elsewhere and refused_here(limits, known, credential, key):
        return None, True
    found_here = known_elsewhere or known.holds(credential)
    if not found_here and not counted(bus, limits, key, limit, window):
        return None, True
    found = lookup(credential)
    if found is None:
        if found_here:
            known.forget(credential)
            counted(bus, limits, key, limit, window)
        return None, False
    if not found_here:
        given_back(bus, key)
    known.keep(credential)
    return found, False


async def secret_grant(server: Server, token: str, bucket: str) -> Grant | None:
    """What ``token`` (a personal token or an API key) of an enabled identity grants (its identity, its id and what it
    may do), as the gate and every machine's call find it: from this instance's memory when it holds it (no store
    asked); otherwise checked by the limiter of the client's ``bucket`` (``checked``, with ``Server.known_secrets``;
    TooManyMisses past it, on the event loop when this instance remembers the bucket so, but for a token found valid
    elsewhere: ``admitted``), one lookup at a time for each token (``OneAtATime``). A text that is no token is none, at
    no cost. Raises StoresUnavailable."""
    if not token.startswith((TOKEN_PREFIX, KEY_PREFIX)):
        return None
    grant = server.identities.remembered_grant(secret=token)
    if grant is not None:
        return grant
    key = miss_key(bucket)

    async def looked_up() -> tuple[Grant | None, bool]:
        if (elsewhere := await admitted(server.known_secrets, server.limits, token, key)) is None:
            return None, True
        return await pre_auth(
            checked, server.bus, server.known_secrets, server.limits, token, key, LOOKUP_LIMIT, LOOKUP_WINDOW,
            server.identities.grant_by_secret, elsewhere,
        )  # fmt: skip

    hashed = server.known_secrets.hashed(token)
    found, limited = await server.lookups.run("secret", hashed, server.identities.generation, looked_up)
    if limited:
        raise TooManyMisses
    return found


async def secret_identity(server: Server, token: str, bucket: str) -> Identity | None:
    """The enabled identity of ``token``: ``secret_grant``'s, for a machine's call, which asks nothing of what the token
    may do. Raises TooManyMisses and StoresUnavailable as it does."""
    grant = await secret_grant(server, token, bucket)
    return grant.identity if grant is not None else None


def _new(held: Iterable[str | None], sent: Iterable[str | None]) -> list[str]:
    """What ``sent`` names that ``held`` does not: each text stripped, once whatever its case, none empty, in its
    order."""
    seen = {(text or "").strip().casefold() for text in held} - {""}
    out: list[str] = []
    for text in sent:
        name = (text or "").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def merged_identity(held: IdentityConfig, sent: IdentityConfig) -> IdentityConfig | None:
    """``held``, a person's identity, with what one of their computers ``sent`` adds to it: its user names, emails and
    host names that are new (the first host name goes to ``hostname`` when none is there, the others to ``hostnames``),
    and its full name when none is held. Nothing is taken away, nothing held is replaced, and ``enabled`` stays as it
    is. None when it adds nothing."""
    names = _new([*held.usernames, held.username], [*sent.usernames, sent.username])
    emails = _new(held.emails, sent.emails)
    hosts = _new([held.hostname, *held.hostnames], [sent.hostname, *sent.hostnames])
    full_name = held.full_name or (sent.full_name or "").strip() or None
    if not (names or emails or hosts) and full_name == held.full_name:
        return None
    hostname = held.hostname
    if hostname is None and hosts:
        hostname, hosts = hosts[0], hosts[1:]
    return held.model_copy(
        update={
            "usernames": [*held.usernames, *names],
            "emails": [*held.emails, *emails],
            "hostname": hostname,
            "hostnames": [*held.hostnames, *hosts],
            "full_name": full_name,
        }
    )


def _past_the_cap(identity: IdentityConfig) -> bool:
    """Whether ``identity`` holds more than ``NAMES_KEPT`` user names, emails or host names (the single user name and
    host name counted with their lists)."""
    names = len(identity.usernames) + (identity.username is not None)
    hosts = len(identity.hostnames) + (identity.hostname is not None)
    return max(names, len(identity.emails), hosts) > NAMES_KEPT


def _sent_identity(given: dict[str, Any] | None) -> IdentityConfig | None:
    """What a computer sends of its person's identity, when it is one: a JSON object of user names, emails and host
    names (lists, ``NAMES_KEPT`` at most each), a user name, a host name and a full name, each text ``NAME_CHARS``
    characters at most. None for anything else (``enabled`` is read and set aside: a computer does not decide it)."""
    if given is None:
        return None
    try:
        sent = IdentityConfig.model_validate(given)
    except ValidationError:
        return None
    lists = (sent.usernames, sent.emails, sent.hostnames)
    texts = (*sent.usernames, *sent.emails, *sent.hostnames, sent.username, sent.hostname, sent.full_name)
    if any(len(items) > NAMES_KEPT for items in lists) or any(t is not None and len(t) > NAME_CHARS for t in texts):
        return None
    return sent


def _who(identity: Identity) -> dict[str, Any]:
    return {"id": identity.id, "name": identity.name, "kind": identity.kind}


def machine_routes(server: Server) -> list[Route]:
    url = server.settings.url
    putting: set[str] = set()  # the people whose identity a computer is sending now: one call at a time each

    async def caller(request: Request) -> Identity | None:
        return await secret_identity(server, token_of(request.scope), address(request))

    async def enrol(request: Request) -> Response:
        slot = f"enrol:{address(request)}"
        if credentials_busy():  # a one-time code is never known: refused at once while the threads are busy
            busy = shed("an enrollment")
            message = "the server cannot answer now: try again in a moment"
            return _refused(request, message, 503, retry_after=busy.retry_after)
        # Past its limit as this instance remembers it: refused here, no thread taken, no round trip.
        if server.limits.refuses(slot) or not await pre_auth(
            counted, server.bus, server.limits, slot, ENROL_LIMIT, ENROL_WINDOW
        ):
            message = "too many codes tried from this address: wait a few minutes"
            return _refused(request, message, 429, retry_after=str(int(ENROL_WINDOW)))
        raw = await _within(request, ENROL_BYTES)
        if raw is None:
            return _error(f"a code and a label take {ENROL_BYTES} bytes at most", 413)
        given = _object_of(raw) or {}
        code, label = given.get("code"), given.get("label")
        unknown = "this code is unknown, used or late: ask your administrator for a new one"
        if not isinstance(code, str) or not code or len(code) > CODE_CHARS:
            return _error(unknown, 401)
        named, client = label[:LABEL_CHARS] if isinstance(label, str) else "", client_of(request.scope)

        def enrolled() -> tuple[Identity, str, str] | None:
            """In one transaction: the code spent, the computer's token and key made, and the entry that records it,
            last (all of it is kept, or none, and the code can then be tried again). Once kept, the token and its
            address are known valid from the computer's first call, and the slot the enrolment took of its bucket is
            given back: the limit counts the codes that fail."""
            with server.identities.changing() as changes:
                found = server.identities.enrol(code, named, within=changes)
                if found is not None:  # which token: its id, never what it is
                    added = {"token": server.identities.token_id(found[1], within=changes)}
                    server.audit.record(found[0].id, "token.added", found[0].id, added, client, conn=changes.conn)
            if found is not None:
                server.known_secrets.keep(found[1])
                server.known_routes.keep(route_key(found[1]))
                with contextlib.suppress(Exception):  # a slot not given back costs its bucket one try, no more
                    given_back(server.bus, slot)
            return found

        found = await in_store(enrolled)  # the chain's lock: never in the lookups' threads, nor behind a person's
        if found is None:
            return _error(unknown, 401)
        identity, token, key = found
        answer = {"token": token, "computer_key": key, "identity": _who(identity), "url": url}
        return JSONResponse({**answer, **routes_of(url, token)})

    async def keyed(request: Request, identity: Identity) -> Response | None:
        """None when the request carries the computer's key of the token it came with (``key_of``), as PostgreSQL has
        the token's row now; else its refusal: 401 for a token gone meanwhile, 403 ``computer_key_missing`` for one made
        before computer keys, 403 ``computer_key_required`` for a key missing or wrong, which counts as a miss of its
        address (``miss_key``). Raises TooManyMisses (429) for a key missing or wrong from an address past its budget
        of misses, as this instance remembers it or as this miss finds it. Its token is a valid one, which skips its
        address's limit: so its key is looked up all the same (one indexed read), as a token found valid elsewhere is,
        and a colleague behind an address a stranger floods gets a sign-in link with their own key."""
        slot = miss_key(address(request))
        token, key = token_of(request.scope), key_of(request.scope)

        def looked_up() -> tuple[str, bool]:
            found = server.identities.computer_key(identity.id, token, key)
            if found != "wrong":
                return found, True
            if server.limits.refuses(slot):  # past its limit already: refused, no slot taken
                return found, False
            return found, counted(server.bus, server.limits, slot, LOOKUP_LIMIT, LOOKUP_WINDOW)

        found, within = await pre_auth(looked_up)
        if not within:
            raise TooManyMisses
        if found == "held":
            return None
        if found == "gone":
            return _error("unauthorized", 401)
        if found == "keyless":
            return _refused(request, KEYLESS_WORDS, 403, reason="computer_key_missing")
        return _refused(request, COMPUTER_KEY_WORDS, 403, reason="computer_key_required")

    async def login_code(request: Request) -> Response:
        identity = await caller(request)
        if identity is None:
            return _error("unauthorized", 401)
        if identity.kind != "person":
            return _error("an API key has no dashboard", 403)
        if (refused := await keyed(request, identity)) is not None:  # before a sign-in link of the person's is spent
            return refused
        if not await pre_auth(server.bus.take, f"login-code:{identity.id}", LOGIN_CODES, LOGIN_CODE_WINDOW):
            message = "too many sign-in links asked for this person: wait a minute"
            return _refused(request, message, 429, retry_after=str(int(LOGIN_CODE_WINDOW)))
        # Bound to the token that asks for it, which this instance's memory may hold a few seconds past its revocation
        # elsewhere: the sign-in opens a session only while that token is there, as PostgreSQL has it then.
        bound = server.identities.secret_digest(token_of(request.scope))

        def issued() -> str:
            code = server.sign_ins.issue_code(identity.id, token=bound)
            server.known_codes.keep(code)
            return code

        code = await pre_auth(issued)
        return JSONResponse({"code": code, "url": f"{url}{UI}/login?code={code}"})

    async def whoami(request: Request) -> Response:
        secret = token_of(request.scope)
        grant = await secret_grant(server, secret, address(request))
        if grant is None:
            return _error("unauthorized", 401)
        # Its addresses call a model through Latet, which restores what comes back: a token without mask and unmask is
        # given none, as the administration gives it none (they would refuse it).
        routes = routes_of(url, secret) if grant.gateway else {}
        return JSONResponse({**_who(grant.identity), "url": url, "version": __version__, **routes})

    async def put_identity(request: Request) -> Response:
        identity = await caller(request)
        if identity is None:
            return _error("unauthorized", 401)
        if identity.kind != "person":
            return _error("an API key has no computer", 403)
        if (refused := await keyed(request, identity)) is not None:  # before anything of the body is read
            return refused
        raw = await _within(request, IDENTITY_BYTES)
        if raw is None:
            return _error(f"an identity takes {IDENTITY_BYTES} bytes at most", 413)
        sent = _sent_identity(_object_of(raw))
        if sent is None:
            return _error(
                f"not an identity: a JSON object of usernames, emails and hostnames (lists, {NAMES_KEPT} at most "
                f"each), username, hostname and full_name, each text {NAME_CHARS} characters at most",
                400,
            )
        # One at a time for each person: a call is applied under their runtime's lock, which a resync or a save holds
        # for seconds, and each call waiting for it would hold one more of the threads every person's work shares.
        if identity.id in putting:
            message = "this person's identity is being applied: try again in a moment"
            return _refused(request, message, 429, retry_after=str(BUSY_SECONDS))
        putting.add(identity.id)
        try:
            return await _applied(identity, sent)
        finally:
            putting.discard(identity.id)

    async def _applied(identity: Identity, sent: IdentityConfig) -> Response:
        try:
            runtime = await server.spaces.runtime(identity)
        except IdentityUnavailable:  # gone or disabled, as PostgreSQL has it now
            return _error("unauthorized", 401)
        except SpaceUnavailable:  # answered by ``machine``: the instance does not hear Valkey, or it closed meanwhile
            raise
        except Exception as exc:  # its build failed: a store that did not answer, said by its type, as the gate says it
            kind = described(exc)
            if count := UNBUILT.due():
                log.warning(
                    "a space could not be built on this instance (%s): its computer's call was refused%s",
                    kind,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            raise StoresUnavailable(kind) from None
        full: list[bool] = []

        def change(current: Config) -> Config | None:
            merged = merged_identity(current.identity, sent)
            if merged is None:
                return None
            if _past_the_cap(merged):
                full.append(True)
                return None
            return current.model_copy(update={"identity": merged})

        def applied() -> Response | None:
            """Under the person's runtime lock: the identity merged in. A settings refusal or kept-save conflict is
            answered here before the generic worker boundary can turn it into a retryable store failure."""
            try:
                runtime.update_config(change)
            except SettingsSavedChangePending:
                return _saved_change_pending()
            except SharedItemsError as exc:  # its words count and place, never quote
                return _error(f"this person's settings refuse the change: {exc}", 409)
            return None

        if (refused := await in_work(applied)) is not None:  # never in the lookups' threads
            return refused
        if full:
            return _error(
                f"this person's identity holds {NAMES_KEPT} user names, emails or host names already, the most their "
                "computers add together: nothing was added",
                409,
            )
        return JSONResponse({"ok": True})

    async def forget_token(request: Request) -> Response:
        identity = await caller(request)
        if identity is None:
            return _error("unauthorized", 401)
        if identity.kind != "person":
            return _error("an API key is rotated by an administrator", 403)
        token, client = token_of(request.scope), client_of(request.scope)

        def forgotten() -> str | None:
            """In one transaction: the token revoked, and the entry that records it (which token: its id), last; then
            forgotten as valid."""
            with server.identities.changing() as changes:
                found = server.identities.revoked_secret(token, within=changes)
                if found is not None:
                    owner, token_id = found
                    revoked = {"token": token_id}
                    server.audit.record(owner, "token.revoked", owner, revoked, client, conn=changes.conn)
            server.known_secrets.forget(token)
            server.known_routes.forget(route_key(token))
            return found[0] if found is not None else None

        await in_store(forgotten)
        # What the token opened ends here at once, whatever becomes of the revocation's message (Ruling 22, Task 12's
        # hand-off): its person's space on this instance, then their sign-ins and AWS sessions.
        await server.spaces.drop(identity.id, wait=False)
        try:
            await in_store(server._ended, identity.id)
        except StoresUnavailable:  # the beat's check tries again
            server._clean(identity.id, removed=False)
        return JSONResponse({"ok": True})

    def machine(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            if from_a_page(request.scope):
                return _refused(request, "browser requests are not accepted here", 403)
            try:
                return await handler(request)
            except TooManyMisses:
                message = "too many credentials refused from this address: wait a minute"
                return _refused(request, message, 429, retry_after=RETRY_AFTER)
            except SpaceUnavailable:  # the instance does not hear Valkey, or the space closed while it was built
                return _refused(request, "this person's space is not available here now: try again in a moment", 503)
            except StoresUnavailable as exc:  # a store that did not answer, or the credentials' threads busy
                message = "the server cannot answer now: try again in a moment"
                return _refused(request, message, 503, retry_after=exc.retry_after)

        return endpoint

    return [
        Route(f"{SERVER_API}/enrol", machine(enrol), methods=["POST"]),
        Route(f"{SERVER_API}/login-code", machine(login_code), methods=["POST"]),
        Route(f"{SERVER_API}/whoami", machine(whoami), methods=["GET"]),
        Route(f"{SERVER_API}/identity", machine(put_identity), methods=["PUT"]),
        Route(f"{SERVER_API}/token", machine(forget_token), methods=["DELETE"]),
    ]
