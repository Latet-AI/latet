"""Each identity's space on an instance of a Latet server: its runtime (E0a's ``make_runtime`` over the server's
backends) and its app, built at the identity's first request on this instance and kept by last use within a memory
budget. Any instance builds any space; none owns one.

A space is built for the identity as PostgreSQL has it (never this instance's memory of it, a few seconds late), and
only while the instance hears Valkey (``NotListening`` otherwise: a space built then would miss what the others
announce). What another instance announces is applied to the space as it runs, one change at a time in the order heard,
by a task of the space's own, its work in the spaces' threads (never the event loop's, nor the ear's: one space's slow
change holds no other identity's): a value masked or forgotten (PostgreSQL read first: a message out of order unmasks
nothing), a conversation's new folder, its own settings or the shared ones changed (read and applied under the
runtime's lock, composed with the settings approved as at a start: what the shared settings add applies at once, what
lowers protection waits for the person, ``Runtime.propose``; an API key's approved settings are its own part laid over
the shared settings of now, so that every shared change reaches it and a rollback of its own part waits). What a space
hears while it is built is applied before anyone has it. A change that cannot be applied closes the space, made again
from PostgreSQL at its next use: a space never runs on without it.

Every announced change carries its count from PostgreSQL: the identity's vault's, its settings', or the shared
settings'. Each space keeps the counts it has applied (heard, or made itself), and the beat's check (``Spaces.check``)
compares them with PostgreSQL's in one query: a space behind at two checks in a row is caught up by what moved alone
(its settings read again, never its vault for them), the space of an identity disabled or gone, or that lost a token
since the space was built, closed. Closing a space never holds the event loop (its recorder drains in a thread), and
spaces close side by side.

The budget weighs a space in bytes, from counts its parts keep (``space_size``), at each use and after each change, and
each new value as it is kept, its length too. A space never learns past its share of it (``SPACE_SHARE``): a request
that brings a value that would carry it past is stopped at that value (``runtime.SpaceFull``), and a value another
instance kept is not learnt past it either, whatever the route (heard, shown to a conversation, read by a catch-up):
its vault then refuses every text, none leaving without that value, and the space is made again from PostgreSQL at its
next use, every value known (``_unlearnt``). When the spaces are past the budget, one past its share goes first, its
long connections ended with the share's words (one serving a request goes as its last request ends, one holding a
batch of the Live Reviewer stays), then those used least recently that serve nothing; one past its share that stays
counts at its share, so that no space within its share is closed to make room for it. ``BUILDS_AT_ONCE`` spaces are
built at once at most: the others wait their turn on the event loop, which a build holds the GIL from in turns (two
hundred at once held it for seconds). Disabling or removing an identity, revoking a token and rotating a key close its
space on every instance (made again, or never, at its next use), and a space that closes, whatever closes it, ends its
long connections at once: its WebSockets closed (``CLOSED_CODE``), nothing more of them sent to their destination, and
its dashboards' streams ended. A rename updates it in place (Ruling 22).

Who calls: the event loop, each method here (none waits on a network on it: the work is in threads), but
``message``'s hand-over of the Live Reviewer's wake-ups and of the feeds' events, which may read Valkey once (the bus's
``SOCKET_TIMEOUT`` bounds it: R-7), a batch a "pending" event names read in the queue's thread."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import re
import secrets
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from latet.api.rest import API, _submit_publication
from latet.engine.known import LET_GO, value_chars
from latet.engine.stored import StoredVault
from latet.engine.vault import Vault
from latet.gateway.app import OnServer, create_app
from latet.gateway.provenance import LocalEchoes
from latet.gateway.request_audit import RequestAudit, _SweepLease
from latet.logline import Paced, described
from latet.projects import Projects, masker_weight
from latet.protection import loosened
from latet.runtime import Runtime, SpaceFull, make_runtime
from latet.server.backends import SpaceBackends, shown_forgotten, space_backends, vault_limit_of
from latet.server.index import INDEX_BUILDS_AT_ONCE
from latet.server.launch_binding import BindingCodec
from latet.server.launch_controls import LaunchHoldAdmission, launch_hold
from latet.server.launch_lifecycle import ProtectedLaunchControls
from latet.server.launch_mutations import LAUNCH_MUTATION, LaunchMutations, MutationAdmission
from latet.server.launch_open import ProtectedLaunchOpen
from latet.server.live import WRITER_CLOSE, ValkeyEchoes
from latet.server.master import root_key
from latet.server.shared import OwnSettings
from latet.server.space_settings import settings_owner
from latet.server.vault import INDEX_TOO_LARGE, PRINTS_UNMADE, Forgotten, NotRecorded, UnreadableEntry
from latet.waiting import shielded

if TYPE_CHECKING:
    from latet.engine.vault import Entity
    from latet.gateway.request_audit import _NativeRLock
    from latet.server.app import Server
    from latet.server.db import Database
    from latet.server.identities import Identity
    from latet.server.vault import PgVaultStore

log = logging.getLogger(__name__)

# A space idle, its engine and its built-in detectors (Task 16 measured 0.3 MB; the models are the process's, once).
# What it holds besides is weighed from counts its parts keep (``space_size``).
SPACE_BASE = 1024 * 1024
# Each value its vault knows: its entity and its share of the parts that match the values and their fakes, and each
# character those parts hold of it, its or its fake's, the longer (``Vault.chars``). Measured with the parts compiled
# and searched: 2,841 bytes for each of 50,000 values of 40 characters, 34 to 37 bytes a character from 400 to 40,000
# characters a value; weighed at 1,536 and 40.
_PER_VALUE = 1536
_PER_CHAR = 40
# Each entry of a lookup table of a whole read's older snapshot (``StoredVault.whole_tables``): a dict's slot, its key
# and a small tuple, its values shared with the newer snapshot.
_PER_TABLE_ENTRY = 120
SPACE_THREADS = 16  # the threads that apply what the spaces hear: each space's work one at a time, in one of them
BUILDS_AT_ONCE = 4  # spaces built at once at most (the module's words)
# How long closing every space is waited for: a space's app drains its recorder for five seconds at most
# (``gateway.app``), then its conversations' writer sends what it queued for ``WRITER_CLOSE`` at most, and a margin.
_RECORDER_DRAIN = 5.0
CLOSE_SECONDS = _RECORDER_DRAIN + WRITER_CLOSE + 2.0
# The values a catch-up of a vault handles between two lets-go of the GIL: the event loop takes its turn between.
_YIELD_EVERY = 256
QUEUE_LIMIT = 10_000  # changes waiting to be applied to one space at most: past it, it is closed and made again
# An API key's vault is counted by PostgreSQL at most this often, in seconds, as values another instance kept are heard
# (``_recount``): a count of a vault of millions takes a tenth of a second.
RECOUNT_SECONDS = 1.0
_RECOUNTED: weakref.WeakKeyDictionary[object, float] = weakref.WeakKeyDictionary()  # each store's last count, when
_TRAILING: weakref.WeakKeyDictionary[object, threading.Timer] = weakref.WeakKeyDictionary()  # a count put off
_TRAILING_LOCK = threading.Lock()
_ABOVE_KEPT = 10_000  # counts a space applied past one it never heard, kept until the beat's check catches it up
_DROP = "drop"  # what a space being built hears when it is closed meanwhile
# Seconds an instance holds the job of making a person's words (``_make_words``) before another may take it, should it
# stop meanwhile; more for a vault of many values (as ``fill_prints`` sizes its own): expiry lets a second instance walk
# it again, which writes nothing twice.
WORDS_HOLD_SECONDS = 600
# What a space is handed besides the messages of its channels: a re-application after a save refused as stale, the
# beat's catching up (its vault, its settings, the shared settings: each by its own count; its identity, by its vault
# limit), and a change of its identity's name, email or vault limit.
_REAPPLY, _UPDATED = "reapply", "updated"
_RESYNC, _SETTINGS_BEHIND, _SHARED_BEHIND = "resync", "settings-behind", "shared-behind"
_CATCH_UPS = frozenset({_RESYNC, _SETTINGS_BEHIND, _SHARED_BEHIND, _UPDATED})  # one of each queued at a time, at most
# A space's long connections, ended with it: WebSockets, dashboard streams, approval waits and AWS session holds.
_STREAMS = frozenset({f"{API}/stream", f"{API}/approvals/stream"})
STALL_SECONDS = 60  # a dashboard stream's client takes each send within this bound, or its subscription ends
# What closes a long connection of a space that closed (its identity disabled or removed, its token revoked, its key
# rotated, the instance not hearing Valkey): a WebSocket's close code (a policy's), and its reason (123 bytes at most).
CLOSED_CODE = 1008
_CLOSED = "latet: this identity's space closed on this instance"
# Its reason when the space closed past its share of the memory (``Spaces._fit``): made again at its next use.
_PAST_SHARE_CLOSED = f"{_CLOSED}, past its share of its memory"


class SpaceUnavailable(RuntimeError):
    """No space for the identity on this instance now: it was closed while it was being built (the identity disabled
    or removed, its token revoked, its key rotated, the spaces closed), or the instance does not hear Valkey. The
    request is refused, nothing of it read or sent, and its client tries again (the gate answers 503)."""


class IdentityUnavailable(LookupError):
    """The identity a space is asked for is gone or disabled, as PostgreSQL has it now: its request is refused as
    unauthorized. A class of its own, so that a ``KeyError`` or an ``IndexError`` of a build that failed is never read
    as an identity gone, which would make a computer drop a good token."""


class NotListening(SpaceUnavailable):
    """The instance does not hear Valkey (``Server.listening`` is clear): no space is built or handed out, since it
    would miss what the other instances announce meanwhile (the gate answers 503, as readiness does)."""


# A space's share of the budget: past it, it learns no new value (a request that brings one is stopped with these words,
# ``Runtime.new_values``), and no other space is closed to make room for it, so that one identity never starves the
# others. Half the budget: a full vault (``vault_limit`` values of a few dozen characters, about 550 MiB) with settings
# at their caps fits within it at the default budget, and a space of the same weight beside it.
SPACE_SHARE = 0.5
# What an API key's detectors may take past the ordinary RE2 budget (``StoredVault.detectors_extra_most``, Task 8's
# fix round 9), and a person's since Task 9 put it on the stored vault: this much of its space's share, at most. It is
# weighed in the space's size, counted against the instance's budget, and never in its share: a request is never
# refused for the speed of a space's detectors, where the release before masked it, slower (the owner's ruling of
# 2026-10-29).
DETECTORS_SHARE = 1 / 8
# How long the vaults' index bytes counted last are said again (``Spaces.index_bytes``, which the beat and the metrics
# ask): each print cache's count walks its entries, five thousand at most a space.
INDEX_BYTES_SECONDS = 60.0
# A space whose vault's index is not ready (its build found the vault's prints not all made, their fill under way here
# or on another instance, or failed; or a catch-up of the ready one failed since) is answered through PostgreSQL's
# lookup, and its index is built again: at once when a fill of the prints ends, its own space's
# (``Spaces._prints_space``) or another instance's (its ``prints`` message), and from the beat's check
# ``INDEX_AGAIN_SECONDS`` after each build that left it so, the last of them from then on, with
# ``INDEX_BUILDS_AT_ONCE`` builds under way at most and each under its turn (``index.BUILD_TURNS``), as the first.
# A ready index lost to a catch-up that failed is built again at the beat's next check; lost again within the last of
# them of the build that made it ready, it waits as a failed build does, each such loss in a row one more try
# (``Spaces._build_index_again``). One refused for its size (``index.IndexTooLarge``) is not built again while its
# space lasts: a vault's size only grows, and each try counts its print rows under a turn; a new space of the identity
# tries once again.
INDEX_AGAIN_SECONDS = (10.0, 20.0, 30.0, 60.0)
PAST_SHARE = (
    "this identity's space holds its share of the memory this instance of the server gives the spaces: it learns no "
    "new value until it holds less (values forgotten in the dashboard, fewer rules or projects) or the server has more"
)


def space_size(runtime: Runtime) -> int:
    """What a space takes in memory, in bytes, roughly, from counts its parts keep (nothing is copied): its engine and
    built-in detectors idle (``SPACE_BASE``), its own masker by its rules (its terms, its recognizers, its patterns,
    as a project's is weighed: ``projects.masker_weight``), each value of its vault, what its projects hold (each one's
    masker and copy of the settings: ``Projects.weight``), the bytes its caches hold, each bounded on a server: the
    engine's masked blocks and restores, the mediator's memory of texts fit to leave as they were kept and the final
    check's of the texts it found clean (``Mediator.checked_bytes``), the index of prints of a vault on its store (an
    API key's, a person's) and the words of what its calls found (``PgVaultStore.index_bytes``,
    ``StoredVault.words_bytes``), a person's whole vault read past a bound while it lives (``StoredVault.whole_count``),
    and the memories in front of Valkey of its conversations and of its texts as they left; the batches of the Live
    Reviewer it holds on this instance, their texts and their images, each image with its pixels
    (``ValkeyApprovals.held_bytes``). A value is weighed by its length too, from the moment it is kept: the characters
    the vault's parts hold of it (``Vault.chars``), a whole JSON string of strict data mode's weighing what it takes
    there. And what an API key's or a person's detectors were granted past the ordinary RE2 budget (``_detectors``),
    which its share leaves out."""
    projects = runtime.projects.weight if runtime.projects is not None else 0
    return _fixed(runtime) + projects + _values(runtime.built.vault) + _detectors(runtime)


def _detectors(runtime: Runtime) -> int:
    """What an API key's or a person's detectors were granted past the ordinary RE2 budget
    (``StoredVault.detectors_bytes``), at most its ceiling (``DETECTORS_SHARE`` of the share): in the space's size,
    never in its share, so that the speed of its detectors never refuses a value (Task 8's fix round 9)."""
    return getattr(runtime.built.vault, "detectors_bytes", 0)


def _fixed(runtime: Runtime, retained: int | None = None) -> int:
    """What a space weighs but its values and its projects (``space_size``): idle, its own masker, its caches and the
    batches of the Live Reviewer it holds."""
    held = runtime.built.engine.cached_bytes + runtime.mediator.checked_bytes
    held += getattr(runtime.store, "index_bytes", 0)
    held += runtime.request_retained_bytes if retained is None else retained
    launch_open = runtime.gateway.launch_controls
    if isinstance(launch_open, ProtectedLaunchOpen):
        held += launch_open.retained_bytes
    vault = runtime.built.vault
    held += getattr(vault, "words_bytes", 0)  # the words of what its calls found (an API key's vault, a person's)
    # A person's whole vault read past a bound of a call, while it lives (``StoredVault._whole_read``): as its values.
    held += _PER_VALUE * getattr(vault, "whole_count", 0) + _PER_CHAR * getattr(vault, "whole_chars", 0)
    held += _PER_TABLE_ENTRY * getattr(vault, "whole_tables", 0)  # an older snapshot's own lookup tables
    backends = runtime.backends
    if backends is not None:
        held += getattr(backends.given, "cached_bytes", 0) + getattr(backends.conversations, "cached_bytes", 0)
        held += getattr(backends.approvals, "held_bytes", 0)
    return SPACE_BASE + masker_weight(runtime.config) + held


def _values(vault: Vault, count: int = 0, chars: int = 0) -> int:
    """What ``vault``'s values weigh (``_PER_VALUE``, ``_PER_CHAR``), with ``count`` more of ``chars`` characters."""
    return _PER_VALUE * (vault.count + count) + _PER_CHAR * (vault.chars + chars)


class _Counts:
    """The change counts of one owner (an identity's state, or the shared settings) that a space has applied: every
    count up to ``applied``, and those of ``_above`` beyond it. A count never heard (its announcement lost at its
    source) holds ``applied`` back until the beat's check catches the space up from PostgreSQL. Safe across threads:
    the stores hand it what they count in the threads that make the changes."""

    def __init__(self, applied: int) -> None:
        self._lock = threading.Lock()
        self.applied = applied
        self._above: set[int] = set()
        self._ahead: int | None = None  # what PostgreSQL had beyond ``applied`` at the beat's check before

    def done(self, count: object) -> None:
        """``count`` applied (heard, or made by the space itself)."""
        if type(count) is not int:
            return
        with self._lock:
            if count <= self.applied:
                return
            self._above.add(count)
            self._advance()
            if len(self._above) > _ABOVE_KEPT:  # the check catches the space up, and every count with it
                self._above.clear()

    def caught_up(self, count: object) -> None:
        """Every change up to ``count`` applied: read from PostgreSQL after ``count`` was (the counts follow the order
        their changes commit in: a change's count row stays locked until it commits)."""
        if type(count) is not int:
            return
        with self._lock:
            if count > self.applied:
                self.applied = count
                self._above = {above for above in self._above if above > count}
            self._advance()
            self._ahead = None

    def behind(self, now: int) -> bool:
        """Whether the space is behind PostgreSQL's count ``now``, at this check and at the one before: a change
        committed a moment ago may be on its way to it at the first."""
        with self._lock:
            if now <= self.applied:
                self._ahead = None
                return False
            if self._ahead is not None and self.applied < self._ahead:
                return True
            self._ahead = now
            return False

    def highest(self) -> int:
        """The highest count the space has applied or made itself: what settings read by its build were read at, at
        least (a build's own writes included)."""
        with self._lock:
            return max(self.applied, *self._above) if self._above else self.applied

    def _advance(self) -> None:
        while self.applied + 1 in self._above:
            self.applied += 1
            self._above.discard(self.applied)


class _Hooks:
    """What a space's stores call before the space exists, and from their threads: a save of its settings refused as
    stale hands the space a re-application, made once the runtime's lock is free (never inside the save, which holds
    it), so that the person's next try passes."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.space: Space | None = None

    def stale(self) -> None:
        loop, space = self.loop, self.space
        if loop is None or space is None or space.closed:
            return
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (shutdown)
            loop.call_soon_threadsafe(space.enqueue, _REAPPLY, {})


@dataclass(eq=False)
class _Built:
    """What the thread that builds a space hands the event loop."""

    identity: Identity
    runtime: Runtime
    parts: SpaceBackends
    origin: str
    vault_counts: _Counts
    settings_counts: _Counts
    shared_counts: _Counts
    tokens: frozenset[str]
    hooks: _Hooks


@dataclass(eq=False, repr=False)
class Space:
    """An identity's space on this instance. Printed, it names the identity by its id, nothing of what it holds."""

    identity: Identity
    runtime: Runtime
    app: ASGIApp
    parts: SpaceBackends
    stack: contextlib.AsyncExitStack
    used: float
    origin: str = ""  # what its announcements say made them: its instance's id and its own
    vault_counts: _Counts = field(default_factory=lambda: _Counts(0))  # its vault's, applied
    settings_counts: _Counts = field(default_factory=lambda: _Counts(0))  # its settings', applied
    shared_counts: _Counts = field(default_factory=lambda: _Counts(0))  # the shared settings', applied
    # Its identity's tokens when it was built: one gone since (revoked, a key rotated) closes it, as the message would.
    tokens: frozenset[str] = frozenset()
    busy: int = 0  # requests it serves now: never closed for the budget then
    size: int = 0  # what it weighed last (space_size)
    # What it weighed last but its values and its projects (``_fixed``), over which each value is weighed as it comes.
    fixed: int = 0
    retained: int = 0
    # What its vault's detectors (an API key's, a person's) were granted past the ordinary RE2 budget
    # (``StoredVault.detectors_bytes``): in its size, never in its share (``DETECTORS_SHARE``).
    extra: int = 0
    closed: bool = False
    # Its changes, in order, each with its place in the queue (``seq``, the number queued so far).
    queue: asyncio.Queue[tuple[str, dict[str, Any], int]] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None  # what applies them
    seq: int = 0
    # Every change queued up to this place was queued before the last reading of its settings began, which read it too.
    covered: int = 0
    pending: set[str] = field(default_factory=set)  # the beat's catch-ups queued and not over: none queued again
    # The tasks of its long connections (its WebSockets, its dashboards' streams), ended with it (``end_long``); those
    # it ended, whose cancellation is its own; and the words they end with.
    long: set[asyncio.Task[Any]] = field(default_factory=set)
    long_scopes: dict[asyncio.Task[Any], Scope] = field(default_factory=dict, repr=False)
    ended: set[asyncio.Task[Any]] = field(default_factory=set)
    words: str = _CLOSED
    # Its vault's index (``Spaces._index_space``): the build under way, the builds since it was last ready that left it
    # not ready and when the last of them ended (``time.monotonic``), whether one more is wanted once the one under way
    # ends (a fill of its prints ended meanwhile), whether it is said not to be used (``Spaces._index_off``), and
    # whether it was refused for its size, never built again while the space lasts. When a build last made it ready,
    # and how many of its losses (a catch-up that failed) came in a row, each after the first within
    # ``INDEX_AGAIN_SECONDS[-1]`` of the build that made it ready (``Spaces._build_index_again``). The fill of its
    # vault's prints under way here (``Spaces._prints_space``), when its build found them not all made.
    index_build: asyncio.Task[None] | None = None
    prints_fill: asyncio.Task[None] | None = None
    index_tries: int = 0
    index_tried: float = 0.0
    index_ready_at: float = 0.0
    index_losses: int = 0
    index_wanted: bool = False
    index_off: bool = False
    index_refused: bool = False

    def __repr__(self) -> str:
        return f"Space({self.identity.id})"

    @property
    def serving(self) -> int:
        """The requests it serves now but its long connections: those a space past its share is closed after."""
        return self.busy - len(self.long)

    def enqueue(self, channel: str, message: dict[str, Any]) -> None:
        self.seq += 1
        if channel in _CATCH_UPS:
            self.pending.add(channel)
        self.queue.put_nowait((channel, message, self.seq))

    def end_long(self, words: str | None = None) -> None:
        """Its long connections ended at once: each one's task cancelled (``Spaces.app`` closes what it was sending,
        a WebSocket with ``CLOSED_CODE`` and ``words``, ``_CLOSED`` unless given before). On the event loop; once is
        enough."""
        if words is not None:
            self.words = words
        for task in list(self.long):
            if task not in self.ended:
                self.ended.add(task)
                if scope := self.long_scopes.get(task):
                    scope["latet.space_ended"] = True
                task.cancel()


def _long(scope: Scope) -> bool:
    """Whether a request is a space's WebSocket, dashboard stream or bridge long poll."""
    if scope["type"] == "websocket":
        return True
    if scope["type"] != "http":
        return False
    path = scope.get("path", "")
    if path in _STREAMS or launch_hold(scope):
        return True
    if scope.get("method") == "POST":
        return path == "/_latet/engine/v1/approvals/wait"
    return scope.get("method") == "GET" and re.fullmatch(r"/_latet/engine/v1/aws-sessions/[^/]+/hold", path) is not None


class _Sent:
    """What a long connection has sent of its answer: whether it began, and whether it ended."""

    def __init__(self) -> None:
        self.began = self.over = False

    def saw(self, message: Message) -> None:
        kind = message["type"]
        if kind in ("http.response.start", "websocket.accept", "websocket.http.response.start"):
            self.began = True
        elif kind == "websocket.close" or (kind.endswith("response.body") and not message.get("more_body", False)):
            self.over = True

    async def end(self, scope: Scope, send: Send, words: str = _CLOSED) -> None:
        """What was being sent, closed: a WebSocket with ``CLOSED_CODE`` and ``words``, accepted or not (refused, when
        not); a stream with its last, empty part, or 503 when it had not begun, saying ``words``. A client gone already
        is closed already."""
        if self.over:
            return
        with contextlib.suppress(Exception):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": CLOSED_CODE, "reason": words})
            elif self.began:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                body = json.dumps({"error": f"{words}: try again"}).encode()
                headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
                await send({"type": "http.response.start", "status": 503, "headers": headers})
                await send({"type": "http.response.body", "body": body})


class _StreamStalled(Exception):
    """Only this stream's send deadline passed; do not attempt a further send to end it."""


async def _held(space: Space, scope: Scope, receive: Receive, send: Send) -> None:
    """A long connection of ``space``: its task held in the space's (``Space.long``) while it runs. When the space ends
    it (``Space.end_long``: the space closes, whatever closes it), its cancellation is the space's own: it ends here,
    what it was sending closed (``_Sent.end``), and a close the app sends meanwhile carries ``CLOSED_CODE``; any other
    cancellation goes on. One that comes when the space has closed already is ended at once, nothing of it run."""
    task = asyncio.current_task()
    assert task is not None  # a request runs in a task of its own
    sent = _Sent()

    async def sending(message: Message) -> None:
        if task in space.ended and message["type"] == "websocket.close":
            message = {**message, "code": CLOSED_CODE, "reason": space.words}
        if scope["type"] == "http" and scope.get("path") in _STREAMS:
            try:
                await asyncio.wait_for(send(message), STALL_SECONDS)
            except TimeoutError:
                sent.over = True
                raise _StreamStalled from None
        else:
            await send(message)
        sent.saw(message)

    space.long.add(task)
    space.long_scopes[task] = scope
    try:
        try:
            if space.closed:  # closed while this request was handed it
                await sent.end(scope, sending, space.words)
                return
            try:
                await space.app(scope, receive, sending)
            except asyncio.CancelledError:
                if task not in space.ended or task.uncancel() > 0:
                    raise
                await sent.end(scope, sending, space.words)
        except _StreamStalled:
            pass
        except ExceptionGroup as exc:  # ASGI before 2.4 wraps a failed send in its response task group's exception
            _, other = exc.split(_StreamStalled)
            if other is not None:
                raise other from None
    finally:
        space.long.discard(task)
        space.long_scopes.pop(task, None)
        space.ended.discard(task)


def _retrieved(task: asyncio.Task[Any]) -> None:
    """A build's outcome taken, whoever awaited it: one that failed with nobody waiting any more is not left for asyncio
    to log with its traceback."""
    if not task.cancelled():
        task.exception()


def _indexed(space: Space) -> bool:
    """Whether ``space``'s vault finds its values through an index of its prints (a stored vault's, Task 8)."""
    return isinstance(space.runtime.built.vault, StoredVault) and space.parts.vault.index is not None


def _index_ready(space: Space) -> bool:
    """Whether ``space``'s vault's index is ready (built from a vault whose every word has its print, and caught up)."""
    index = space.parts.vault.index
    return index is not None and index.ready


def _since(count: int) -> str:
    """What a paced line adds (``logline.Paced``): how many times it happened since the line before."""
    return f" ({count} times since the line before)" if count > 1 else ""


def _read_counts(db: Database, owners: list[str]) -> dict[str, int]:
    """The change counts of ``owners`` (an identity's id, ``""`` for the shared settings): 0 for one with none yet."""
    with db.connection() as conn:
        rows = conn.execute("SELECT owner, counter FROM change_counters WHERE owner = ANY(%s)", (owners,)).fetchall()
    found = {str(owner): int(counter) for owner, counter in rows}
    return {owner: found.get(owner, 0) for owner in owners}


@dataclass(frozen=True, slots=True)
class _State:
    """What the beat's check reads of an identity (``""``: the shared settings, their count as ``vault``)."""

    present: bool
    disabled: bool
    vault: int
    settings: int
    tokens: frozenset[str]
    vault_limit: int | None = None  # its own limit of values in its vault; None: the server's


def _states(db: Database, identity_ids: list[str]) -> dict[str, _State]:
    """For each of ``identity_ids``, and for the shared settings (``""``): whether the identity is there, whether it is
    disabled, its vault's and its settings' change counts, its tokens and its own vault limit, in one query (the
    beat's check)."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT o.owner, i.id IS NOT NULL, COALESCE(i.disabled, FALSE), COALESCE(v.counter, 0),"
            " COALESCE(s.counter, 0), COALESCE((SELECT array_agg(t.id) FROM tokens t WHERE t.identity_id = o.owner),"
            " '{}'), i.vault_limit FROM unnest(%s::text[]) AS o(owner)"
            " LEFT JOIN identities i ON i.id = o.owner LEFT JOIN change_counters v ON v.owner = o.owner"
            " LEFT JOIN change_counters s ON s.owner = o.owner || ':settings'",
            ([*identity_ids, ""],),
        ).fetchall()
    return {
        str(row[0]): _State(bool(row[1]), bool(row[2]), int(row[3]), int(row[4]), frozenset(map(str, row[5])), row[6])
        for row in rows
    }


def _recount(vault: Vault, store: PgVaultStore, *, now: bool = False) -> None:
    """The store's count true to the vault, for its limit (and those of its entries that do not open). An API key's
    vault holds its working set alone in memory (``StoredVault``): the store counts it, at most once a second
    (``RECOUNT_SECONDS``: the values another instance keeps meanwhile counted a moment late, as instances that keep
    values at once may pass the limit), and at once when ``now``."""
    if not isinstance(vault, StoredVault):
        store.count = vault.count + store.unreadable
        return
    moment = time.monotonic()
    last = _RECOUNTED.get(store, -RECOUNT_SECONDS)
    if now or moment - last >= RECOUNT_SECONDS:
        _RECOUNTED[store] = moment
        store.count = store.count_entities()
    else:
        _recount_later(store, last + RECOUNT_SECONDS - moment)


def _recount_later(store: PgVaultStore, delay: float) -> None:
    """A count of an API key's vault once ``delay`` is over, one at a time for each store: the values heard within the
    second before are counted though no other value follows them (``_recount``)."""
    with _TRAILING_LOCK:
        if store in _TRAILING:
            return
        timer = threading.Timer(delay, _trailing_count, (store,))
        timer.daemon = True
        _TRAILING[store] = timer
    timer.start()


def _trailing_count(store: PgVaultStore) -> None:
    """In a timer's thread: the count ``_recount_later`` put off."""
    with _TRAILING_LOCK:
        _TRAILING.pop(store, None)
    try:
        _RECOUNTED[store] = time.monotonic()
        store.count = store.count_entities()
    except Exception as exc:  # its space closed meanwhile, or PostgreSQL away: the next value heard counts again
        log.debug("an API key's vault was not counted again (%s)", described(exc))


def _displaced(vault: Vault, entity: Entity) -> bool:
    """Whether knowing ``entity`` would put another entity of the vault out: its fake or its value held under another
    id (``_learned``), in memory. Fakes as written (``Vault.displaced_by``): PostgreSQL keeps one value per fake so,
    and `Nicola%20Greco` and another value's `Nicola Greco` are two fakes there."""
    return bool(vault.displaced_by(entity))


def _learned(vault: Vault, entity: Entity) -> bool:
    """``entity``, kept by another instance, known here. An entity of this vault with its fake or its value under
    another id was forgotten elsewhere, that fake or value given anew (PostgreSQL keeps one value per fake and one
    entity per value): it is forgotten here first, so that the fake restores the new value alone (R-21). Whether one was
    forgotten so: the engine's masked blocks may hold its fake, and its caller has the engine forget them
    (``rules_changed``), as ``_forget`` and ``Runtime.forget`` do. Raises SpaceFull for a new value the space is past
    its share for: not learnt, and the vault refuses every text from then on (``Vault.lacking``) until the space is
    made again (``Spaces._unlearnt``). Fakes are compared as written, as PostgreSQL keeps them (``_displaced``). One
    that this vault holds under its own id with another fake was forgotten elsewhere and kept again, in an API key's
    new generation of fakes (Task 11b), before this instance heard of the forget: its masked texts may hold the old
    fake, so it counts as one forgotten too."""
    forgot = False
    for other in vault.displaced_by(entity):
        forgot = vault.forget(other.id) is not None or forgot
    held = vault.in_memory(entity.id)  # memory alone: one not there holds no masked text of its own
    forgot = forgot or (held is not None and held.surrogate != entity.surrogate)
    vault.learn(entity)
    return forgot


def _on_a_loop() -> bool:
    """Whether an event loop runs in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _learn_ids(runtime: Runtime, store: PgVaultStore, ids: set[str]) -> None:
    """The values another instance showed a conversation, loaded before the conversation uses them (its conversations'
    ``learn``), under the runtime's lock as ``_learn`` (as ``Runtime.forget``: one forgotten here meanwhile is not
    learnt back, and the store's count stays the vault's). Values the vault knows ask for no lock. In the thread that
    masks, a save that holds the lock is waited for; on the event loop (the Live Reviewer's decision reads its batch's
    conversation there, ``Approvals._masks``) the lock is taken only when it is free, a save holding it for seconds
    while the loop serves every identity: held, the values are learnt as the instance hears of them, or at its next
    catch-up, never without the lock. One its space is past its share for is not learnt, nor any after it: the vault
    then refuses every text (``Vault.lacking``), this conversation's first, and the space is made again at its next
    weighing (``Spaces._unlearnt``)."""
    if isinstance(runtime.built.vault, StoredVault):
        # A vault on its store (an API key's, a person's) finds a value by the words of the texts that hold it, and a
        # conversation's values are brought back in one read before its answer (``StoredVault.hold``): nothing to load
        # here, and nothing asked of the store one value at a time, nor on the event loop (Task 9's I3).
        return
    if all(runtime.built.vault.get(entity_id) is not None for entity_id in ids):
        return
    if not runtime._lock.acquire(blocking=not _on_a_loop()):
        return
    try:
        vault = runtime.built.vault
        learnt = forgot = False
        for entity_id in ids:
            if vault.get(entity_id) is not None:
                continue
            try:
                entity = store.load_entity(entity_id)
            except UnreadableEntry:  # there and does not open: counted and said by the store
                continue
            if entity is not None:
                try:
                    forgot = _learned(vault, entity) or forgot
                except SpaceFull:  # past its share: the conversation's masking is refused (``Vault.lacking``)
                    forgot = True
                    break
                learnt = True
        if learnt or forgot:
            _recount(vault, store)
        if forgot:
            runtime.built.engine.rules_changed()  # cached masked blocks may hold the fake of the one put out
    finally:
        runtime._lock.release()


def _learn(space: Space, entity_id: str) -> None:
    """A value kept, or its rule, through another instance: read from PostgreSQL, under the runtime's lock (as
    ``Runtime.forget``: one forgotten here meanwhile is not learnt back). One its space is past its share for is not
    learnt: the space is made again at its next weighing (``Spaces._unlearnt``)."""
    runtime, store = space.runtime, space.parts.vault
    with runtime._lock:
        try:
            entity = store.load_entity(entity_id)
        except UnreadableEntry:
            return
        if entity is not None:  # None: forgotten since
            try:
                forgot = _learned(runtime.built.vault, entity)
            except SpaceFull:  # past its share: not learnt (``Vault.lacking``)
                forgot = True
            if forgot:
                runtime.built.engine.rules_changed()  # cached masked blocks may hold the fake of the one put out
            _recount(runtime.built.vault, store)


def _forget(space: Space, entity_id: str) -> None:
    """A value forgotten through another instance, or by the administration: dropped here only when PostgreSQL holds it
    no more (a message out of order, or published again by whoever has Valkey's password, never unmasks a value it still
    holds), under the runtime's lock. An API key's vault holds its working set alone (``StoredVault``): a text its
    engine masked before may hold the fake of a value that left its memory since, so its masked texts are dropped
    whether memory held the value or not, and when PostgreSQL holds an entry of that id again: the value was met again
    and kept since, with another fake, which is learnt here at once (``_learned``: review 11b's M2), never left to its
    own message, which may come late or not at all."""
    runtime, store = space.runtime, space.parts.vault
    store.drop_cached(entity_id)
    with runtime._lock:
        vault = runtime.built.vault
        entity, held = None, True
        with contextlib.suppress(UnreadableEntry):  # there, and does not open: kept as it is masked
            entity = store.load_entity(entity_id)
            held = entity is not None
        if held:
            forgot = isinstance(vault, StoredVault)  # a masked text may hold the fake it had before
            if entity is not None:
                try:
                    forgot = _learned(vault, entity) or forgot
                except SpaceFull:  # past its share: not learnt (``Vault.lacking``)
                    forgot = True
                _recount(vault, store)
            if forgot:
                runtime.built.engine.rules_changed()
            return
        # Out of the conversations this instance holds too: Valkey's forgot it, when its value was forgotten by itself
        # (Ruling 11b-R1), and an update would write it back. And an API key's local reviewer forgets the answers it
        # remembers in memory, every one: the value is not known here, its entry's id alone (review 11b's N3).
        space.parts.conversations.forgotten((entity_id,))
        if isinstance(vault, StoredVault) and runtime.mediator.reviewer is not None:
            runtime.mediator.reviewer.forget_answers()
        if vault.forget(entity_id) is not None or isinstance(vault, StoredVault):
            runtime.built.engine.rules_changed()  # cached masked blocks may hold its fake
            _recount(vault, store)


def _settle_obsolete(runtime: Runtime, *, every: bool = False) -> None:
    """A change still waiting with the very settings applied now would undo nothing: settled as superseded (applied
    through another instance, or read by a check between another instance's two writes). ``every``: an API key's, where
    nothing lowered means the administration saved its settings since, and no change of a key waits for anyone else.
    Settings are compared as values (the same as their digests are), with no dump of either."""
    approvals, applied = runtime.mediator.approvals, runtime.config
    for change_id in list(approvals.changes):
        change = approvals.changes.get(change_id)
        if change is not None and (every or (change.config is not None and change.config == applied)):
            approvals.settle_change(change_id, "superseded")


def _exception_logged(server: Server, identity_id: str, details: Mapping[str, Any]) -> None:
    """A permanent request exception the identity approved, in the administration log (R-108): its id and its scope's
    category alone, the identity its actor and its target."""
    server.audit.record(identity_id, "exception.permanent", identity_id, dict(details))


def _index_again(server: Server, identity_id: str, config: Any, read_at: int | None = None) -> None:
    """The index of the identity's approved request exceptions written again as its space is built, by what this
    version and this edition make of them (one a new version suspends, R-100; a permanent one an edition forbids): kept
    otherwise as its approved settings were last kept. ``read_at``: the settings count its build read before
    ``config``; an approval kept since wrote the index itself, which stands (``index_exceptions``). Its failure is said
    by its type: the space runs, and `latet server status` lists from what the index last held."""
    from latet.server.space_settings import index_exceptions

    allowed = server.edition.permanent_exceptions()
    try:
        with server.db.transaction() as conn:
            index_exceptions(conn, identity_id, config, permanent_allowed=allowed, read_at=read_at)
    except Exception as exc:
        log.warning(
            "the index of the request exceptions of %s was not written again: %s", identity_id, type(exc).__name__
        )


def _counts_and_now(db: Any, owner: str) -> tuple[datetime, int, int]:
    """RC-REC-3's one statement: PostgreSQL's clock, the identity's settings count and the shared settings' (owner
    ``''``), read together, so that what decides an exception and the time it is judged at are one reading."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT clock_timestamp(),"
            " COALESCE((SELECT counter FROM change_counters WHERE owner = %s), 0),"
            " COALESCE((SELECT counter FROM change_counters WHERE owner = ''), 0)",
            (owner,),
        ).fetchone()
    if row is None or not isinstance(row[0], datetime) or row[0].tzinfo is None:
        raise OSError("the database gave no time")
    return row[0], int(row[1]), int(row[2])


def database_now(db: Any) -> datetime:
    """PostgreSQL's clock, the one a request exception's lifetime is judged by on a server (D3, RC-REC-3): every
    instance reads the same time, whatever its own clock says."""
    with db.connection() as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None or not isinstance(row[0], datetime) or row[0].tzinfo is None:
        raise OSError("the database gave no time")
    return row[0]


def _reapply(space: Space) -> None:
    """The settings as PostgreSQL has them now, read and applied under the runtime's lock (so that the version its next
    save names is the one applied), weighed against those approved, as at a start: where they lower protection the
    approved ones apply (``shared.composed``: what the shared settings add applies at once, what they remove waits),
    and the change waits for the person (opened once, on whichever instance). An API key's approved settings are its own
    part laid over the shared settings of now: a shared change reaches it unweighed, a rollback of its own part waits.
    Nothing is written but the approved snapshot, the composed settings (never looser than it), and that only when it
    differs: a re-application announces nothing new."""
    runtime = space.runtime
    with runtime._lock:
        settings = runtime.settings
        found = settings.load()
        approved = settings.reviewed()
        target = found
        if approved is not None and isinstance(settings, OwnSettings):
            target = settings.composed(found, approved)
        elif approved is not None and loosened(approved, found):
            target = approved
        # Compared as values, no dump of either: under tens of thousands of shared terms each digest took tens of ms.
        if target != runtime.config:
            runtime.apply_config(target, save=False, as_is=True)  # as composed: laid over again, it would be looser
        if target is not found:
            # Asked again when none waits with these settings over those in use now: one made over settings that
            # changed since (her own save of a part of it, on any instance) would be found stale at her approval.
            if not runtime.waits(found, over_these=True):
                runtime.propose(found, "file")
            return
        _settle_obsolete(runtime, every=space.identity.kind == "key")


def _resync(space: Space, db: Database) -> int:
    """The space's vault caught up with PostgreSQL after a change of it the space never heard: the vault's count read
    first, then every value as PostgreSQL has it learnt (none forgotten here: a value kept stays masked, the safe side,
    until the space is made again). Its settings have a count of their own. Answers the count read: every change of the
    vault up to it is applied.

    The values are read outside the runtime's lock (the database and the opening of each take the time, seconds at the
    vault's limit: what else the space does meanwhile, a save of its settings, a value forgotten, waits for none of it),
    then learnt under it, the GIL let go every ``_YIELD_EVERY`` values so that the event loop serves every identity
    meanwhile. One that would put another out of the vault (its fake or its value given anew) is read again first, under
    the lock: what PostgreSQL holds now wins, never what the reading found a moment before. Past its share it learns no
    more: the space is made again at its next weighing (``Spaces._unlearnt``).

    An API key's vault holds its working set alone in memory (``StoredVault``), and finds the rest in PostgreSQL as its
    texts need it: what PostgreSQL holds now of its working set alone is read (``StoredVault.resync``), never the whole
    vault, and a value it no longer has, or holds otherwise, leaves memory."""
    runtime, store = space.runtime, space.parts.vault
    vault = runtime.built.vault
    count = _read_counts(db, [space.identity.id])[space.identity.id]
    if isinstance(vault, StoredVault):
        changed = vault.resync()
        with runtime._lock:
            _recount(vault, store, now=True)
            if changed:
                runtime.built.engine.rules_changed()  # cached masked blocks may hold a fake it no longer has
        vault.matchers.current()
        return count
    found = store.load_entities()
    forgot = False
    with runtime._lock:
        for n, entity in enumerate(found, 1):
            if n % _YIELD_EVERY == 0:
                time.sleep(LET_GO)  # the GIL let go: the event loop takes its turn (on Windows too, as no pause does)
            if vault.get(entity.id) == entity:
                continue
            if _displaced(vault, entity):  # what PostgreSQL holds now, not what was read a moment ago
                try:
                    fresh = store.load_entity(entity.id)
                except UnreadableEntry:
                    continue
                if fresh is None:
                    continue  # forgotten since: its fake may be another value's now
                entity = fresh
            try:
                forgot = _learned(vault, entity) or forgot
            except SpaceFull:  # past its share: not learnt (``Vault.lacking``)
                forgot = True
                break
        _recount(vault, store)
        if forgot:
            runtime.built.engine.rules_changed()
    vault.matchers.current()  # what it learnt matched here, in this thread, never by the next masking
    return count


def _made_prints(store: PgVaultStore) -> bool:
    """In the fills' thread: the prints of a vault kept by 0.10 made (``fill_prints``, on one instance at a time, none
    while another holds their lease), stopped at the next part once its space closes (``PgVaultStore.closed``); whether
    they are all made now. False when the fill found their lease held by another instance, which makes them."""
    if store.closed:
        return False
    store.fill_prints()
    return not store.closed and store.prints_complete()


def _make_words(store: PgVaultStore, jobs: Any) -> int:
    """In the fills' thread: the words of a person's vault kept before they were (0.10.0's, or an older version of
    the anchors), then their prints, on one instance at a time (``words:<identity>``), none while another makes them;
    stopped at the next part once the space closes (``PgVaultStore.closed``). Its space keeps its whole vault in memory
    meanwhile, as before; the space built after them is on its store. How many values got their words."""
    seconds = max(WORDS_HOLD_SECONDS, store.count_entities() * 0.002)
    with jobs.hold(f"words:{store.identity}", seconds) as mine:
        if not mine or store.closed:
            return 0
        filled = store.backfill_words()
        if filled:
            log.info(
                "the words of %d values of a person's vault were made, so that they are found in its texts", filled
            )
        if not store.closed:
            store.fill_prints()
        return filled


def _shut(runtime: Runtime) -> None:
    """A space's runtime closed, after any change it applies (under its lock)."""
    with runtime._lock:
        runtime.close()


def _flush(conversations: object) -> None:
    """What the space's conversations' store queued to write, written before the space goes, when it queues (a writer
    thread of its own, which waits a few seconds at most for them): in a thread, the event loop serving others."""
    close = getattr(conversations, "close", None)
    if callable(close):
        close()


async def _audited(
    server: Server, identity_id: str, action: str, details: Mapping[str, Any], address: str | None
) -> None:
    """An entry of the administration log that a space writes (its applications API's unmasks), with its identity as
    the actor and the target, and the client's address: written in the server's threads of those entries (``in_log``),
    where it waits on the log's chain (one entry at a time for the whole server), never in the space's one bridge
    thread, whose other calls would wait behind it (Ruling T5a), nor in the store threads, whose enrolments and
    revocations would (Task 8b's review). Raises StoresUnavailable when it is not written."""
    from latet.server.machines import in_log  # here: ``machines`` imports this module

    await in_log(server.audit.record, identity_id, action, identity_id, details, address)


async def _forgetting(
    server: Server,
    parts: SpaceBackends,
    identity_id: str,
    values: Sequence[str],
    details: Mapping[str, Any],
    address: str | None,
) -> Forgotten:
    """Values of an API key's vault forgotten by its applications API (Task 11b, ``OnServer.forget``): in its store
    (``PgVaultStore.forget_values``), with the rows of its history that name their entries (``gone``, the history's
    older rows given their entries first: Ruling 11b-R1), the entry of the administration log (``apps.forgotten``:
    ``details`` and the counts, the identity its actor and its target, the client's address) written last in the same
    transaction; then out of its conversations, in Valkey and in this instance's memory (``shown_forgotten``), in the
    server's threads of forgets (``in_forget``), where it waits on rows and on the log's chain, never in the space's one
    bridge thread (Ruling T5a) nor in the threads of the unmasks' entries, which every key's unmasks wait for (review
    11b's I2). The ids forgotten, how many of the values the vault does not hold, and the generation new fakes are
    drawn in from then on (``Forgotten``). Raises NotRecorded when the entry was not written, and ConnectionError (by
    the error's type alone) when the work did not finish otherwise: either way the transaction rolled back, and nothing
    was forgotten."""
    from latet.server.machines import StoresUnavailable, in_forget  # here: ``machines`` imports this module

    def recorded(conn: Any, forgotten: int, unknown: int) -> None:
        entry = {**details, "forgotten": forgotten, "unknown": unknown}
        try:
            server.audit.record(identity_id, "apps.forgotten", identity_id, entry, address, conn=conn)
        except Exception as exc:  # by its type alone: a driver's words can name an address
            raise NotRecorded(described(exc)) from None

    def forget() -> Forgotten | NotRecorded:
        history = parts.history
        try:
            history.index_entities()  # outside the forget's transaction: once, after an upgrade
            done = parts.vault.forget_values(values, gone=history.forget_entities, last=recorded)
        except NotRecorded as unrecorded:  # handed back, as ``in_forget`` makes every error it meets StoresUnavailable
            return unrecorded
        parts.conversations.forgotten(done.ids)
        shown_forgotten(server, identity_id, done.ids)
        return done

    try:
        done = await in_forget(forget)
    except StoresUnavailable as exc:  # a store that did not answer, or a fault: by its type alone (503)
        raise ConnectionError(str(exc)) from None
    if isinstance(done, NotRecorded):
        raise done
    return done


_RLOCK_TYPE = type(threading.RLock())


class _AuditFence:
    """No wait, retry or callback-only mutex: fence each real check-to-commit lock."""

    __slots__ = ("check", "locks")

    def __init__(self, locks: tuple[Any, ...], check: Callable[[int], str | None]) -> None:
        self.locks, self.check = locks, check

    def __call__(self, added: int) -> str | None:
        if added != 0 or not 2 <= len(self.locks) <= 3:
            return PAST_SHARE
        acquired = 0
        try:
            for lock in self.locks:
                if type(lock) is not _RLOCK_TYPE:
                    return PAST_SHARE
                owned = cast("_NativeRLock", lock)._is_owned()
                if type(owned) is not bool or owned:  # reentrant pending work is not committed
                    return PAST_SHARE
                if not lock.acquire(blocking=False):
                    return PAST_SHARE
                acquired += 1
            return self.check(0)  # Audit's entire positive delta is already published
        finally:
            for index in range(acquired - 1, -1, -1):
                self.locks[index].release()


def _audit_active(space: Space) -> bool:
    audit = space.runtime.request_audit
    return type(audit) is RequestAudit and audit.protects_idle


def _retire_audit(space: Space) -> bool:
    audit = space.runtime.request_audit
    # Unsupported ownership cannot be assumed idle. No foreign locks/callbacks
    # are acquired by this atomic A-only seal, and no cleanup is awaited here.
    return type(audit) is RequestAudit and audit.try_retire_if_idle()


class _StatusSweep:
    """Finite loop-owned traversal; actual carriers/Handles are strong owners."""

    __slots__ = ("__weakref__", "_cursor", "_lease", "_loop", "_phase", "_remaining", "_spaces")

    def __init__(
        self, lease: _SweepLease, spaces: Spaces, loop: asyncio.AbstractEventLoop, remaining: int, cursor: int
    ) -> None:
        self._lease = self._spaces = self._loop = None
        self._cursor = self._remaining = 0
        self._phase = 0
        self._spaces = weakref.ref(spaces)
        self._loop, self._remaining, self._cursor = loop, remaining, cursor
        self._lease = lease  # full construction before ownership transfers

    @property
    def _input(self) -> bool:
        return type(self._phase) is weakref.ReferenceType

    @property
    def _pending(self) -> bool:
        return self._phase is True

    @property
    def _stopped(self) -> bool:
        return self._remaining < 0

    def _schedule(self) -> None:
        if self._stopped or self._pending or self._input:
            return
        self._phase = True
        try:
            cast("asyncio.AbstractEventLoop", self._loop).call_soon_threadsafe(self, context=contextvars.Context())
        except BaseException as error:
            # An exception may follow enqueue. Keep pending ownership until the
            # actual Handle is consumed/discarded; never retry an ambiguous wake.
            self.stop()
            BaseException.__setattr__(error, "__traceback__", None)
            BaseException.__setattr__(error, "__context__", None)
            BaseException.__setattr__(error, "__cause__", None)

    def _input_ended(self) -> None:
        self._phase = 0  # weak Work marker ends BEFORE Work's charge can end

    def cancel(self, audit: object) -> None:
        phase = self._phase
        work = phase() if type(phase) is weakref.ReferenceType else None
        if work is not None and work._owner is audit:
            work.close()  # C ends; real N/Q keep every live byte charged

    def _disposed(self) -> None:
        self._phase = 0
        self._schedule()  # carrier/Work refs already cleared; never under A

    def stop(self) -> None:
        self._remaining = -1  # terminal sentinel, independent of input/pending; never refunds

    def __call__(self) -> None:
        self._phase = 0
        spaces = self._spaces() if self._spaces is not None else None
        if self._stopped or spaces is None or spaces._status_stopped or not self._remaining or not spaces._spaces:
            self.stop()
            return
        size = len(spaces._spaces)
        cursor = self._cursor % size
        space = next(space for index, space in enumerate(spaces._spaces.values()) if index == cursor)
        self._cursor = (cursor + 1) % size
        self._remaining -= 1
        if not space.closed and spaces._spaces.get(space.identity.id) is space:
            audit = space.runtime.request_audit
            if type(audit) is RequestAudit:
                work = audit.reserve_snapshot()
                if work is not None:
                    try:
                        self._phase = weakref.ref(work)  # paid by the LIVE Work, never a strong cycle
                    except MemoryError:
                        work.close()
                        self._schedule()
                        return
                    if _submit_publication(work, space.runtime.mediator, spaces._executor, self):
                        return
                    self._phase = 0
        self._schedule()  # yield ONE candidate; no retained iterator/Space/list

    def __del__(self) -> None:
        lease = self._lease
        self._lease = None
        spaces = self._spaces() if self._spaces is not None else None
        if spaces is not None and spaces._status_sweep is not None and spaces._status_sweep() is self:
            spaces._status_sweep = None
        self._spaces = self._loop = None
        self._cursor = self._remaining = 0
        self._phase = 0
        self._spaces = None
        spaces = None  # disarm foreign loop/weak/metadata owners OUTSIDE A
        if lease is not None:
            lease.close()


class Spaces:
    def __init__(
        self,
        server: Server,
        *,
        budget_mb: int = 2048,
        launch_holds: LaunchHoldAdmission | None = None,
        launch_mutations: LaunchMutations | None = None,
    ) -> None:
        self.server = server
        self._launch_holds = launch_holds
        self._launch_mutations = launch_mutations
        self._status_sweep: weakref.ReferenceType[_StatusSweep] | None = None
        self._status_stopped = False
        self.budget = budget_mb * 1024 * 1024
        self._spaces: dict[str, Space] = {}
        self._live_weight = 0
        self._building: dict[str, asyncio.Task[Space]] = {}
        self._missed: dict[str, list[tuple[str, dict[str, Any]]]] = {}  # what a space being built has not seen
        self._closing: set[asyncio.Task[None]] = set()
        self._executor = ThreadPoolExecutor(SPACE_THREADS, thread_name_prefix="latet-space")
        self._index_executor = ThreadPoolExecutor(INDEX_BUILDS_AT_ONCE, thread_name_prefix="latet-vault-index")
        # The background work of the vaults kept by 0.10, minutes for a large one, apart from the index builds' threads
        # and turns: the fills of an API key's prints (``_prints_space``) and a person's words (``_words_space``), one
        # at a time, since one instance makes each anyway (its lease).
        self._prints_executor = ThreadPoolExecutor(1, thread_name_prefix="latet-vault-prints")
        self._index_tasks: set[asyncio.Task[None]] = set()
        # The builds under way, each its turn: made for the event loop that runs them (one on a server).
        self._builds: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
        self._unapplied = Paced()  # catch-ups that failed, said once a minute
        self._unlearnt_said = Paced()  # spaces made again for a value they did not learn, said once a minute
        self._index_off_said = Paced()  # a space's index no longer used, said once a minute
        self._index_on_said = Paced()  # a space's index used again, said once a minute
        self._index_unjudged = Paced()  # a space the index pass could not judge, said once a minute
        self._prints_unmade = Paced()  # a fill of a vault's prints that failed, said once a minute
        self._index_bytes: tuple[float, int] | None = None  # the vaults' index bytes counted last, and when

    def count(self) -> int:
        return len(self._spaces)

    def index_bytes(self) -> int:
        """What the vaults of the spaces open here take in memory for their indexes, the prints and the cache of the
        entries fetched by print, with an index being built (``PgVaultStore.index_bytes``): counts they keep, in bytes,
        nothing read from PostgreSQL and no vault opened, from any thread (the beat's, the metrics'). The spaces are
        listed at once: one that opens or closes meanwhile is counted or not, never twice. Counted once in
        ``INDEX_BYTES_SECONDS`` at most, whoever asks: a cache's count walks its entries."""
        counted = self._index_bytes
        if counted is not None and 0 <= time.monotonic() - counted[0] < INDEX_BYTES_SECONDS:
            return counted[1]
        total = self._counted_index_bytes()
        self._index_bytes = (time.monotonic(), total)
        return total

    def _counted_index_bytes(self) -> int:
        for _ in range(3):  # a list taken while the event loop changes the dict, taken again
            try:
                spaces = list(self._spaces.values())
                break
            except RuntimeError:
                continue
        else:
            return 0
        total = 0
        for space in spaces:
            with contextlib.suppress(Exception):  # a space closing as it is weighed: counted as nothing
                total += int(getattr(space.runtime.store, "index_bytes", 0) or 0)
        return total

    def _build_turns(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._builds is None or self._builds[0] is not loop:
            self._builds = (loop, asyncio.Semaphore(BUILDS_AT_ONCE))
        return self._builds[1]

    async def runtime(self, identity: Identity) -> Runtime:
        """The identity's runtime on this instance, built at its first use. Raises NotListening, SpaceUnavailable, or
        IdentityUnavailable for an identity that is gone or disabled."""
        return (await self._space(identity)).runtime

    async def app(self, identity: Identity, *, launch_carrier: list[bytes] | None = None) -> ASGIApp:
        """The identity's app on this instance, which counts the requests it serves and weighs the space after each
        (past its share and the spaces past the budget, it goes as its last request ends: ``_fit``), and holds its long
        connections (its WebSockets, its dashboards' streams) to end them with the space (``_held``). Raises as
        ``runtime`` does."""
        space = await self._space(identity)

        async def counted(scope: Scope, receive: Receive, send: Send) -> None:
            mutation = scope.get(LAUNCH_MUTATION)
            if isinstance(mutation, MutationAdmission) and not mutation.attach(
                space.identity.id, self._request_fence(space)
            ):
                refused = JSONResponse(
                    {"error": "launch-capacity"}, status_code=503, headers={"cache-control": "no-store"}
                )
                await refused(scope, receive, send)
                return
            space.busy += 1
            try:
                if _long(scope):
                    await _held(space, scope, receive, send)
                else:
                    await space.app(scope, receive, send)
            finally:
                space.busy -= 1
                space.used = time.monotonic()
                if not space.closed:
                    self._weigh(space, ended=True)

        if launch_carrier is None:
            return counted

        async def with_launch(scope: Scope, receive: Receive, send: Send) -> None:
            from latet.engine.pipeline import RequestScope, request_scope
            from latet.gateway.launch_authority import REQUEST_LAUNCH, RequestLaunch

            requests = space.runtime.gateway.protected_launch
            if requests is None and launch_carrier:
                launch_carrier.clear()
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1011})
                    return
                refusal = JSONResponse(
                    {"error": "launch-unavailable"}, status_code=503, headers={"cache-control": "no-store"}
                )
                await refusal(scope, receive, send)
                return
            presented = requests.derive(launch_carrier.pop()) if requests is not None and launch_carrier else None
            scope[REQUEST_LAUNCH] = RequestLaunch(requests, presented)
            token = request_scope.set(RequestScope())
            try:
                await counted(scope, receive, send)
            finally:
                scope.pop(REQUEST_LAUNCH, None)
                request_scope.reset(token)

        return with_launch

    async def _space(self, identity: Identity) -> Space:
        if not self.server.listening.is_set():
            raise NotListening("this instance does not hear Valkey now: no space is built or handed out until it does")
        space = self._spaces.get(identity.id)
        if space is not None:
            space.used = time.monotonic()
            self._weigh(space)
            if not space.closed:  # else its vault lacked a value: made again below (``_unlearnt``)
                return space
        building = self._building.get(identity.id)
        if building is None:  # one build for every request that asks at once
            building = asyncio.create_task(self._open(identity), name="latet-space-build")
            building.add_done_callback(_retrieved)
            self._building[identity.id] = building
        return await shielded(building)

    def _build(self, identity: Identity) -> _Built:
        """In a worker thread: the identity as PostgreSQL has it now (IdentityUnavailable for one gone or disabled), the
        change counts its space starts from (read first: a change made from then on is heard, or seen as one not
        heard), its root key, its backends and its runtime, whose app will trust the server alone. The instance's copy
        of the shared settings is read again when PostgreSQL's count is ahead of it (its announcement lost), and the
        space counts the shared settings it is built on as the copy's: were it older all the same, the beat's check
        finds it behind."""
        server = self.server
        found = server.identities.current(identity.id)
        if found is None or found.disabled:
            raise IdentityUnavailable("there is no such identity, or it is disabled")
        tokens = frozenset(token.id for token in server.identities.tokens(found.id))
        counts = _read_counts(server.db, [found.id, settings_owner(found.id), ""])
        _, shared_count = server.shared.counted(at_least=counts[""])  # what the settings below are laid over
        vault_counts, settings_counts = _Counts(counts[found.id]), _Counts(counts[settings_owner(found.id)])
        shared_counts, hooks = _Counts(shared_count), _Hooks()

        def counted(what: str, count: int) -> None:
            (vault_counts if what == "vault" else settings_counts).done(count)

        origin = f"{server.instance}:{secrets.token_hex(6)}"
        key = root_key(server.db, server.master, found.id, clock=server.clock)
        parts = space_backends(
            server,
            found.id,
            key,
            origin=origin,
            counted=counted,
            stale=hooks.stale,
            kind=found.kind,
            vault_limit=found.vault_limit,
        )
        # Its images read through the instance's text recognitions, with the settings' bounds (Task 22).
        from latet.engine.reviewer.reviewer import fixed_key

        runtime = make_runtime(
            None,
            data=server.data,
            root_key=key,
            backends=parts.state,
            identity=found.ref(),
            reading=server.reading,
            reviewer_key=fixed_key(server.reviewer_key),
            ocr_unread=server.metrics.ocr_unread,
            working=server.settings.key_working_values,
            held=server.settings.key_held_secrets,
            detectors_most=int(self.budget * SPACE_SHARE * DETECTORS_SHARE),
            permanent_allowed=server.edition.permanent_exceptions,  # the edition's from its first build (Minor 1)
        )
        # Before its app is made (create_app): the identity's kind (an API key's space alone has the applications API),
        # and what that API writes in the administration log, under the identity's own name, in the log threads.
        audit = functools.partial(_audited, server, found.id)
        forget = functools.partial(_forgetting, server, parts, found.id)  # its values forgotten (Task 11b)
        runtime.gateway.on_server = OnServer(server.settings.url, found.id, audit, found.kind, forget)
        # A request exception's approval time is PostgreSQL's, the same on every instance, never this one's clock (D3).
        runtime.exception_clock = functools.partial(database_now, server.db)
        runtime.exception_log = functools.partial(_exception_logged, server, found.id)
        _index_again(server, found.id, runtime.config, read_at=settings_counts.highest())
        runtime.gateway.launch_prepare = server.launch_prepare
        if server.launch_authority is not None:
            from latet.server.launch_requests import ServerLaunchRequests
            from latet.server.plugin_bindings import PluginBindings

            codec = BindingCodec(key)
            runtime.gateway.launch_controls = ProtectedLaunchControls(
                server.launch_authority, codec, found.id, weakref.ref(runtime)
            )
            runtime.gateway.protected_launch = ServerLaunchRequests(
                server.launch_authority, codec, PluginBindings(server.db, found.id, key), runtime.mediator.run
            )
        if type(runtime.request_audit) is RequestAudit:
            runtime.request_audit.bind_status(server.bus, found.id, server.instance)
        # A computer's two calls do their work in the server's work threads, never the default executor, which the
        # spaces' builds hold for seconds (Task 9's T9.13). Imported here: ``machines`` imports this module.
        from latet.server.machines import in_work

        runtime.gateway.work = in_work
        # What a request's body may decode to: the server's bound on the wire, once decoded too (Task 14's C1).
        runtime.mediator.body_limit = server.settings.max_request_mb * 1024 * 1024
        parts.conversations.learn = functools.partial(_learn_ids, runtime, parts.vault)
        # Its values matched now, a part at a time, in this thread: no request of its waits for all to be compiled.
        runtime.built.vault.matchers.current()
        return _Built(found, runtime, parts, origin, vault_counts, settings_counts, shared_counts, tokens, hooks)

    async def _open(self, identity: Identity) -> Space:
        identity_id = identity.id
        self._missed[identity_id] = []
        try:
            async with self._build_turns():  # its turn: BUILDS_AT_ONCE at most (the module's words)
                built = await asyncio.to_thread(self._build, identity)
                space = await self._started(built)
            try:
                await self._catch_up(space)
                if space.runtime.built.vault.lacking is not None:  # what it heard meanwhile, not learnt past its share
                    raise SpaceUnavailable("the space could not learn what it heard while it was built: try again")
            except SpaceUnavailable:
                await self._close(space)
                raise
            except Exception as exc:  # what it missed could not be applied: its requests try again (503)
                await self._close(space)
                if count := self._unapplied.due():
                    log.warning(
                        "what the space of %s missed while it was built could not be applied on this instance (%s): it "
                        "is closed, and made again at its next use%s",
                        identity_id,
                        described(exc),
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
                raise SpaceUnavailable(
                    "the space could not take what it missed while it was built: try again"
                ) from None
            except BaseException:
                await self._close(space)
                raise
            # Nothing was awaited since what it missed was applied: it misses nothing from now on.
            self._publish_space(space)
            space.worker = asyncio.create_task(self._work(space), name="latet-space-changes")
            vault = space.runtime.built.vault
            if _indexed(space):
                self._build_index(space)
            elif not isinstance(vault, StoredVault) and space.identity.kind == "person":
                # Its words not all made yet: served from memory, as 0.10.0 served it, while they are made.
                task = asyncio.create_task(self._words_space(space), name="latet-space-words")
                self._index_tasks.add(task)
                task.add_done_callback(self._index_tasks.discard)
            self._weigh(space)
            return space
        finally:
            self._missed.pop(identity_id, None)
            self._building.pop(identity_id, None)

    def _build_index(self, space: Space) -> None:
        """``space``'s index built in the background (``_index_space``), unless one is under way already."""
        if space.closed or (space.index_build is not None and not space.index_build.done()):
            return
        task = asyncio.create_task(self._index_space(space), name="latet-space-index")
        space.index_build = task
        self._index_tasks.add(task)
        task.add_done_callback(self._index_tasks.discard)
        task.add_done_callback(self._index_ended)

    def _index_ended(self, task: asyncio.Task[None]) -> None:
        """A build ended (done: no longer counted under way): the next ones due start now (``_build_index_again``),
        not at the beat's next check, so that spaces that lost their indexes together (PostgreSQL restarted) are built
        again two at a time, one after another. None once the spaces' threads are let go, nor for a build cancelled."""
        if task.cancelled() or self._status_stopped:
            return
        self._build_index_again()

    async def _index_space(self, space: Space) -> None:
        """Its bounded, owned background build; no request waits for a legacy print backfill. What it left is judged
        once it ends (``_index_judged``)."""
        if space.closed:
            return
        try:
            future = asyncio.get_running_loop().run_in_executor(self._index_executor, space.parts.vault.build_index)
            while not future.done():
                await asyncio.wait({future}, timeout=0.1)
                if not space.closed:
                    self._weigh(space)  # published, building and cached memory all count while the build runs
            await future
        except asyncio.CancelledError:
            space.parts.vault.revoke_index()
            raise
        finally:
            if not space.closed:
                self._weigh(space)
        if not space.closed:
            self._index_judged(space)

    def _index_judged(self, space: Space) -> None:
        """What a build of ``space``'s index left. Ready: used, and said used again when it was said not to be (once a
        minute at most, by the identity's id alone); when it was made ready is kept, and its losses in a row are not
        cleared (``_build_index_again`` paces a loss right after this build). Not ready: said not to be used
        (``_index_off``), and built again, at once when a fill of its prints ended while it was built (due now: this
        build's own task is not done yet, so its end starts the next, ``_index_ended``, within
        ``INDEX_BUILDS_AT_ONCE``), else by the beat's check once due (``_build_index_again``), at the pace of a failed
        build; but never again while the space lasts when it was refused for its size (``INDEX_TOO_LARGE``). Its prints
        not all made (``PRINTS_UNMADE``): their fill started in the fills' thread (``_prints_space``), unless one is
        under way for the space already."""
        if _index_ready(space):
            space.index_tries, space.index_wanted = 0, False  # ``tries == 0``: ready, until it is found lost
            space.index_ready_at = time.monotonic()
            if space.index_off:
                space.index_off = False
                if count := self._index_on_said.due():
                    log.info("the vault index of %s is used again%s", space.identity.id, _since(count))
            return
        space.index_tries += 1
        space.index_tried = time.monotonic()
        failure = space.parts.vault.index_failure
        space.index_refused = failure == INDEX_TOO_LARGE
        self._index_off(space)
        if failure == PRINTS_UNMADE:
            self._fill_prints(space)
        if space.index_wanted and not space.index_refused:
            space.index_wanted = False
            space.index_tried = 0.0  # due at once: ``_build_index`` would find this very build under way

    def _index_off(self, space: Space) -> None:
        """``space``'s index not used from now on: said once, until it is used again, once a minute at most whatever
        the space, by the identity's id and why (``PgVaultStore.index_failure``: the prints its words lack, or an
        error's type), never a value, a word or a print."""
        if space.index_off:
            return
        space.index_off = True
        if count := self._index_off_said.due():
            why = space.parts.vault.index_failure or "not built"
            log.log(
                logging.INFO if why == PRINTS_UNMADE else logging.WARNING,
                "the vault index of %s is not used (%s): its lookups go to PostgreSQL %s%s",
                space.identity.id,
                why,
                "while its space lasts" if space.index_refused else "until it is built again",
                _since(count),
            )

    def _fill_prints(self, space: Space) -> None:
        """``space``'s vault's prints made in the background (``_prints_space``), unless a fill of them is under way
        here already."""
        if space.closed or (space.prints_fill is not None and not space.prints_fill.done()):
            return
        task = asyncio.create_task(self._prints_space(space), name="latet-space-prints")
        space.prints_fill = task
        self._index_tasks.add(task)
        task.add_done_callback(self._index_tasks.discard)

    async def _prints_space(self, space: Space) -> None:
        """The prints of ``space``'s vault, one kept by 0.10, made in the fills' thread (``_made_prints``), never under
        an index build's turn or in its threads. A fill that ends with them all made has the space's index built again
        at once (``_index_soon``): the ``prints`` message it tells reaches the other instances' spaces, never its own. A
        fill that found their lease held by another instance asks for nothing: that instance's ``prints`` message, or
        the space's next paced build, which starts a fill again, does. A failure is said by its type alone, once a
        minute at most, and the next build of the space's index starts a fill again."""
        if space.closed:
            return
        store = space.parts.vault
        try:
            made = await asyncio.get_running_loop().run_in_executor(self._prints_executor, _made_prints, store)
        except asyncio.CancelledError:
            store.revoke_index()
            raise
        except Exception as exc:  # by its type alone: a driver's words can quote what it was sent
            if count := self._prints_unmade.due():
                log.warning(
                    "the prints of the vault of %s were not made (%s): its lookups go to PostgreSQL, and the next "
                    "build of its index starts their fill again%s",
                    space.identity.id,
                    type(exc).__name__,
                    _since(count),
                )
            return
        if made:
            self._index_soon(space)

    def _index_soon(self, space: Space) -> None:
        """A fill of ``space``'s prints ended (its own, ``_prints_space``, or another instance's ``prints``): its index
        built again now when it is not ready (once the build under way ends, when one is), within
        ``INDEX_BUILDS_AT_ONCE`` builds under way; past them, at the beat's next check."""
        if space.closed or space.index_refused or not _indexed(space) or _index_ready(space):
            return
        if space.index_build is not None and not space.index_build.done():
            space.index_wanted = True
        elif self._index_builds() < INDEX_BUILDS_AT_ONCE:
            self._build_index(space)
        else:
            space.index_tried = 0.0  # due at once

    def _index_builds(self) -> int:
        """The index builds under way here."""
        return sum(
            1 for space in self._spaces.values() if space.index_build is not None and not space.index_build.done()
        )

    def _build_index_again(self) -> None:
        """In the beat's check, and as a build ends (``_index_ended``): each space whose index is not ready and is not
        being built has it built again, once due (``INDEX_AGAIN_SECONDS`` after the last build that left it so),
        ``INDEX_BUILDS_AT_ONCE`` under way at most. One that was ready (no build left it so since) lost it to a
        catch-up that failed (``PgVaultStore.current``): said not to be used now, and built again at once when it is
        its first loss in a row; a loss within ``INDEX_AGAIN_SECONDS[-1]`` of the build that made it ready is one more
        in a row, any other the first again. Lost again so (the pool of connections to PostgreSQL exhausted for a
        while, each catch-up failing right after each build), it waits as a failed build's try does, each loss in a row
        past the first counted as one: 10, 20, 30 seconds, then a minute, never built again at every check. One
        refused for its size is not built again. Each space is judged alone: one that raises is said by its error's
        type (once a minute at most) and the others, and the beat's check that called this, go on."""
        now, building = time.monotonic(), self._index_builds()
        for space in list(self._spaces.values()):
            if building >= INDEX_BUILDS_AT_ONCE:
                return
            try:
                if space.closed or space.index_refused or not _indexed(space) or _index_ready(space):
                    continue
                if space.index_build is not None and not space.index_build.done():
                    continue
                tries = space.index_tries
                if tries == 0:  # it was ready: lost since
                    self._index_off(space)
                    quick = now - space.index_ready_at < INDEX_AGAIN_SECONDS[-1]
                    space.index_losses = space.index_losses + 1 if quick else 1
                    if space.index_losses > 1:
                        space.index_tries, space.index_tried = space.index_losses - 1, now
                        continue
                elif now - space.index_tried < INDEX_AGAIN_SECONDS[min(tries, len(INDEX_AGAIN_SECONDS)) - 1]:
                    continue
                self._build_index(space)
            except Exception as exc:  # by its type alone: its words could quote what the space holds
                if count := self._index_unjudged.due():
                    log.warning(
                        "the vault index of %s could not be judged (%s): it is judged again at the next check%s",
                        space.identity.id,
                        type(exc).__name__,
                        _since(count),
                    )
                continue
            building += 1

    async def _words_space(self, space: Space) -> None:
        """The words and prints of a person's vault made in the background (``_make_words``), in the fills' thread,
        never in the index builds' threads: a failure is said by its type alone, and the space goes on from memory;
        they are made at a later build."""
        if space.closed:
            return
        store = space.parts.vault
        try:
            await asyncio.get_running_loop().run_in_executor(self._prints_executor, _make_words, store, self.server.bus)
        except asyncio.CancelledError:
            store.revoke_index()
            raise
        except Exception as exc:  # by its type alone: a driver's words can quote what it was sent
            log.warning(
                "the words of the vault of %s were not made (%s): its space is served from memory, and they are made "
                "at its next build",
                space.identity.id,
                type(exc).__name__,
            )

    async def _started(self, built: _Built) -> Space:
        """The built runtime's app, its lifespan entered, and the space."""
        app = create_app(built.runtime.gateway)
        stack = contextlib.AsyncExitStack()
        try:
            await stack.enter_async_context(app.router.lifespan_context(app))
        except BaseException:
            await asyncio.to_thread(_shut, built.runtime)
            raise
        space = Space(
            built.identity, built.runtime, app, built.parts, stack, time.monotonic(), built.origin, built.vault_counts,
            built.settings_counts, built.shared_counts, built.tokens,
        )  # fmt: skip
        built.hooks.loop, built.hooks.space = asyncio.get_running_loop(), space
        # RC-REC-3: a request that may be decided by a stored exception reads the settings' counts first.
        built.runtime.gateway.exception_fresh = functools.partial(self.exceptions_fresh, space)
        self._bind_request_budget(space)

        self._weighed(space)  # before it keeps, learns or builds anything
        return space

    async def _catch_up(self, space: Space) -> None:
        """What the space heard while it was built, applied in order, and what it hears meanwhile after it: nobody has
        the space before. Each change is numbered as the queue numbers them, all of those heard so far before the first
        is applied, so that a burst of changes of its settings costs one reading, never one each (``_read_settings``).
        Raises SpaceUnavailable when it was closed meanwhile, and what an application raised."""
        missed = self._missed[space.identity.id]
        while missed:
            numbered = []
            for channel, message in missed:
                if channel == _DROP:
                    raise SpaceUnavailable("the space was closed while it was being built: try again")
                space.seq += 1
                numbered.append((channel, message, space.seq))
            missed.clear()  # what is heard while these are applied comes next
            for channel, message, seq in numbered:
                await self._apply(space, channel, message, seq)

    async def message(self, channel: str, message: Mapping[str, Any]) -> None:
        """A message of the server's channels, for the spaces it concerns here: handed to each space's changes (kept
        for one being built), never applied on the ear, but the Live Reviewer's wake-ups and the feeds' events (no
        lock: one read of Valkey at most, R-7; a batch a "pending" names is read in the queue's thread)."""
        if channel == "shared":
            for waiting in self._missed.values():
                waiting.append((channel, dict(message)))
            for space in list(self._spaces.values()):
                self._enqueue(space, channel, message)
            return
        kind, _, identity_id = channel.partition(":")
        if identity_id in self._missed:
            if kind == "identity":
                self._missed[identity_id].append((channel, dict(message)))
            return
        space = self._spaces.get(identity_id)
        if space is None:
            return
        if kind == "approvals":
            space.parts.approvals.heard(message)
            space.parts.approvals_feed.heard(message)
        elif kind == "feed":
            space.parts.feed.heard(message)
        elif kind == "identity":
            self._enqueue(space, channel, message)

    def updated(self, identity_id: str) -> None:
        """The identity's name, email or vault limit changed: its space here takes them as PostgreSQL has them, in
        place."""
        if identity_id in self._missed:
            self._missed[identity_id].append((_UPDATED, {}))
        elif (space := self._spaces.get(identity_id)) is not None:
            self._enqueue(space, _UPDATED, {})

    def _enqueue(self, space: Space, channel: str, message: Mapping[str, Any]) -> None:
        if space.queue.qsize() >= QUEUE_LIMIT:
            log.warning(
                "the space of %s on this instance has %d changes waiting: it is closed, and made again from PostgreSQL "
                "at its next use",
                space.identity.id,
                QUEUE_LIMIT,
            )
            self._discard(space)
            return
        space.enqueue(channel, dict(message))

    async def _run[T](self, work: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """``work`` in the spaces' threads."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, functools.partial(work, *args, **kwargs)
        )

    async def _work(self, space: Space) -> None:
        """The space's changes, applied one at a time in the order heard. One that cannot be applied closes the space,
        made again from PostgreSQL at its next use: it never runs on without it."""
        while True:
            channel, message, seq = await space.queue.get()
            try:
                await self._apply(space, channel, message, seq)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "a change could not be applied to the space of %s on this instance (%s): it is closed, and made "
                    "again from PostgreSQL at its next use",
                    space.identity.id,
                    described(exc),
                )
                self._discard(space)
                return
            if not space.closed:
                self._weigh(space)

    async def _apply(self, space: Space, channel: str, message: Mapping[str, Any], seq: int | None = None) -> None:
        """One change of the space, at ``seq`` in its queue (None: one it missed while it was built)."""
        kind = channel.partition(":")[0]
        try:
            if kind == "shared":  # read again whole: every shared change up to its count
                await self._read_settings(space, seq)
                space.shared_counts.caught_up(message.get("counter"))
            elif kind == "identity":
                await self._changed(space, message, seq)
            elif kind == _REAPPLY:
                await self._read_settings(space, seq)
            elif kind == _RESYNC:
                space.vault_counts.caught_up(await self._run(_resync, space, self.server.db))
            elif kind == _SETTINGS_BEHIND:
                await self._read_settings(space, seq)
                space.settings_counts.caught_up(message.get("counter"))
            elif kind == _SHARED_BEHIND:
                await self._read_settings(space, seq)
                space.shared_counts.caught_up(message.get("counter"))
            elif kind == _UPDATED:
                found = await self._run(self.server.identities.current, space.identity.id)
                if found is not None and not found.disabled:  # else its own message closes the space
                    space.identity = found
                    space.runtime.identity = found.ref()
                    # The limit the vault keeps to from the next value it is asked to keep (a number: set in one step).
                    space.parts.vault.limit = vault_limit_of(self.server, found.vault_limit)
        finally:
            space.pending.discard(kind)  # over: the beat may queue another of its kind

    async def exceptions_fresh(self, space: Space) -> tuple[datetime, bool]:
        """RC-REC-3, for a request that may be decided by a stored exception: PostgreSQL's clock and the counts of the
        identity's settings and of the shared settings, in one statement; whichever is ahead of what the space applied
        is read again now (the shared settings, this instance's copy of them forgotten first), before the request is
        decided, so that an exception revoked, or added, on another instance or worker counts at once, its message
        lost or not. The time, and whether the settings were read again. A store that does not answer raises: the
        request is refused (503)."""
        now, settings, shared = await self._run(_counts_and_now, self.server.db, settings_owner(space.identity.id))
        settings_behind = settings > space.settings_counts.applied
        shared_behind = shared > space.shared_counts.applied
        if not settings_behind and not shared_behind:
            return now, False
        if shared_behind:
            self.server.shared.forget()
        await self._read_settings(space, None)
        space.settings_counts.caught_up(settings)
        space.shared_counts.caught_up(shared)
        return now, True

    async def _read_settings(self, space: Space, seq: int | None) -> None:
        """The space's settings read again whole and applied (``_reapply``), unless a reading began after this change
        was queued: that one read it too, so a burst of changes costs one reading after the one under way, never one
        each."""
        if seq is not None and seq <= space.covered:
            return
        space.covered = space.seq  # what is queued now was committed before this reading begins
        await self._run(_reapply, space)

    async def _changed(self, space: Space, message: Mapping[str, Any], seq: int | None = None) -> None:
        """A message of the identity's channel, applied to its space."""
        what, entity_id = message.get("kind"), message.get("id")
        if what == "where":  # a conversation's new project or folder: its conversations' store forgets what is older
            await self._run(space.parts.conversations.heard, message)
            return
        counts = space.settings_counts if what == "settings" else space.vault_counts
        if message.get("from") == space.origin:  # this space made it, and so applied it already
            counts.done(message.get("counter"))
            return
        if what == "settings":  # read again whole: every change of its settings up to the count, an untold one too
            await self._read_settings(space, seq)
            counts.caught_up(message.get("counter"))
            return
        if what in ("entity", "rule") and isinstance(entity_id, str):
            await self._run(space.parts.vault.current)
            await self._run(_learn, space, entity_id)
        elif what == "forgotten" and isinstance(entity_id, str):
            await self._run(_forget, space, entity_id)
        elif what in ("words", "prints"):  # a part of the vault's words or prints made (``backfill_words``,
            # ``fill_prints``), or the end of a fill of its prints, after which its index is built again when not ready
            await self._run(space.parts.vault.current)
            if what == "prints":
                self._index_soon(space)
        else:
            return  # not a change this Latet knows: its count stays unapplied, and the check catches the space up
        counts.done(message.get("counter"))

    def kick_status(self) -> None:
        """Post-security, finite best-effort sweep; no await/traffic context."""
        if self._status_stopped or (self._status_sweep is not None and self._status_sweep() is not None):
            return
        count = len(self._spaces)
        for cursor, space in enumerate(self._spaces.values()):
            if space.closed:
                continue
            audit = space.runtime.request_audit
            if type(audit) is not RequestAudit:
                continue
            lease = audit._reserve_sweep()
            if lease is None:
                continue
            sweep = None
            installed = False
            try:
                sweep = _StatusSweep(lease, self, asyncio.get_running_loop(), count - cursor, cursor)
                self._status_sweep = weakref.ref(sweep)
                installed = True
            except BaseException as error:
                BaseException.__setattr__(error, "__traceback__", None)
                BaseException.__setattr__(error, "__context__", None)
                BaseException.__setattr__(error, "__cause__", None)
            if not installed:
                sweep = None
                lease.close()
            else:
                cast("_StatusSweep", sweep)._schedule()
            return

    async def check(self) -> list[tuple[str, bool]]:
        """The beat's check (I5; Task 14 runs it every few seconds): each open space against PostgreSQL, in one query.
        The space of an identity disabled or gone, or that lost a token since the space was built (revoked, a key
        rotated: what it opened ends with it, as the message would have done), is closed and answered (its id, and
        whether the identity is gone) for the server to end what it kept. A space behind at two checks in a row is
        caught up by what moved alone: its settings read again by their own count, its vault only when its vault moved;
        one behind on the shared settings has them read again, this instance's copy of them forgotten first (their
        announcement may be what was lost). A copy of the shared settings older than PostgreSQL's count is forgotten at
        the first check, so that whatever reads them next (a space built here) reads them again. A space whose identity
        has another vault limit than it was built or last updated with (the announcement of the change lost) takes the
        limit at once: taking it twice does no harm."""
        spaces = dict(self._spaces)
        if not spaces:
            return []
        states = await self._run(_states, self.server.db, list(spaces))
        nothing = _State(False, False, 0, 0, frozenset())
        shared_now = states.get("", nothing).vault
        kept = self.server.shared.count
        if kept is not None and kept < shared_now:  # the instance's copy is older: its announcement was lost
            self.server.shared.forget()
        ended: list[tuple[str, bool]] = []
        behind: list[Space] = []
        for identity_id, space in spaces.items():
            if self._spaces.get(identity_id) is not space:
                continue  # closed meanwhile
            state = states.get(identity_id, nothing)
            if not state.present or state.disabled or not space.tokens <= state.tokens:
                await self.drop(identity_id, wait=False)
                ended.append((identity_id, not state.present))
                continue
            # Behind: caught up, but never twice at once (one of each kind queued or under way, as ``pending`` says).
            if state.vault_limit != space.identity.vault_limit and _UPDATED not in space.pending:
                self._enqueue(space, _UPDATED, {})  # its limit changed, and the announcement was lost
            if space.vault_counts.behind(state.vault) and _RESYNC not in space.pending:
                self._enqueue(space, _RESYNC, {})
            if space.settings_counts.behind(state.settings) and _SETTINGS_BEHIND not in space.pending:
                self._enqueue(space, _SETTINGS_BEHIND, {"counter": state.settings})
            if space.shared_counts.behind(shared_now) and _SHARED_BEHIND not in space.pending:
                behind.append(space)
        if behind:
            self.server.shared.forget()
            for space in behind:
                self._enqueue(space, _SHARED_BEHIND, {"counter": shared_now})
        self._build_index_again()
        return ended

    async def drop(self, identity_id: str, *, wait: bool = True) -> None:
        """Close the identity's space here at once (a build under way ends refused): made again, from PostgreSQL, at
        its next use. ``wait``: until it is closed; else it closes in the background, and the caller (the ear) waits for
        nothing."""
        if identity_id in self._missed:
            self._missed[identity_id].append((_DROP, {}))
        space = self._remove_space(identity_id)
        if space is None:
            return
        if wait:
            await self._close(space)
        else:
            self._close_later([space])

    async def close(self, *, wait: bool = True) -> None:
        """Close every space here (each made again at its next use), and end every build under way refused: the spaces
        close side by side. ``wait``: until they are closed, ``CLOSE_SECONDS`` at most; else in the background (the ear,
        which listens again meanwhile)."""
        for waiting in self._missed.values():
            waiting.append((_DROP, {}))
        spaces, self._spaces = list(self._spaces.values()), {}
        self._live_weight = 0
        if spaces:
            self._close_later(spaces)
        if wait and self._closing:
            await asyncio.wait(list(self._closing), timeout=CLOSE_SECONDS)

    def shutdown(self) -> None:
        """The spaces' threads let go (the server closes): what is queued is dropped, nothing new starts."""
        self._status_stopped = True
        sweep = self._status_sweep() if self._status_sweep is not None else None
        if sweep is not None:
            sweep.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._index_executor.shutdown(wait=False, cancel_futures=True)
        self._prints_executor.shutdown(wait=False, cancel_futures=True)

    def _discard(self, space: Space, words: str | None = None) -> None:
        """Close ``space`` in the background, while it is still the identity's here (its long connections ended with
        ``words``, when given)."""
        if self._spaces.get(space.identity.id) is space:
            self._remove_space(space.identity.id)
            self._close_later([space], words)

    def _unlearnt(self, space: Space) -> None:
        """``space``, whose vault did not learn a value another instance kept, past its share (``Vault.lacking``: every
        text it masks or checks is refused meanwhile), closed with the share's words: made again from PostgreSQL at its
        next use, every value known. Said once a minute at most, by the identity's id."""
        if count := self._unlearnt_said.due():
            log.warning(
                "the space of %s on this instance did not learn a value another instance kept, past its share of the "
                "memory: it is closed, and made again from PostgreSQL at its next use%s",
                space.identity.id,
                f" ({count} times since the line before)" if count > 1 else "",
            )
        self._discard(space, _PAST_SHARE_CLOSED)

    def _cancel_status(self, space: Space) -> None:
        sweep = self._status_sweep() if self._status_sweep is not None else None
        if sweep is not None:
            sweep.cancel(space.runtime.request_audit)

    def _close_later(self, spaces: list[Space], words: str | None = None) -> None:
        """``spaces`` closed in the background, their long connections ended at once (with ``words``, when given)."""
        for space in spaces:
            space.closed = True  # from now on, not weighed and handed nothing
            self._cancel_status(space)
            space.parts.vault.revoke_index()
            space.end_long(words)  # its WebSockets and streams end at once, whatever its close waits for
        task = asyncio.get_running_loop().create_task(self._close_all(spaces), name="latet-spaces-close")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_all(self, spaces: list[Space]) -> None:
        await asyncio.gather(*(self._close(space) for space in spaces))

    async def _close(self, space: Space) -> None:
        """The space closed: its long connections end at once (its WebSockets closed, ``CLOSED_CODE``, nothing more of
        them sent on; its dashboards' streams ended), every wait of its Live Reviewer ends at once (Valkey asked
        nothing), its reaper, its feeds and its changes stop; then its app's lifespan ends (its clients, its recorder
        drained in a thread), what its conversations' store queued is written (in a thread), and its runtime closes in
        a thread, whatever went before. One that does not close cleanly is said by the error's type, and keeps no other
        open."""
        space.closed = True
        self._cancel_status(space)
        space.parts.vault.revoke_index()
        space.end_long()
        space.parts.approvals.close()
        space.runtime.mediator.approvals.close()
        space.parts.approvals_feed.close()
        space.parts.feed.close()
        if space.worker is not None:
            space.worker.cancel()
        try:
            try:
                await asyncio.to_thread(space.parts.vault.close)
                await space.stack.aclose()
                await asyncio.to_thread(_flush, space.parts.conversations)
            finally:
                await asyncio.to_thread(_shut, space.runtime)
        except Exception as exc:  # one space that does not close cleanly keeps none of the others open
            log.warning(
                "the space of %s did not close cleanly on this instance (%s)", space.identity.id, described(exc)
            )

    def _bind_request_budget(self, space: Space) -> None:
        """Bind the actual owners before first weighing or runtime publication."""
        runtime = space.runtime
        if runtime.new_values is not None:
            runtime.new_values.ceiling = functools.partial(self._past_share, space)
        runtime.retained_ceiling = functools.partial(self._retained_past_share, space)
        if runtime.projects is not None:
            runtime.projects.past_share = functools.partial(self._projects_past_share, space)
        if type(runtime.request_audit) is RequestAudit:
            runtime.request_audit.bind_ceiling(self._request_fence(space))

    def _request_fence(self, space: Space) -> _AuditFence:
        runtime = space.runtime
        vault, projects, echoes = runtime.built.vault, runtime.projects, runtime.mediator.echoes
        supported = type(vault) in (Vault, StoredVault) and (projects is None or type(projects) is Projects)
        locks = [vault._lock] if supported else []
        if supported and projects is not None:
            locks.append(projects._lock)
        if type(echoes) is LocalEchoes:
            locks.append(echoes._lock)
        elif type(echoes) is ValkeyEchoes and type(echoes._front) is LocalEchoes:
            locks.append(echoes._front._lock)
        else:
            supported = False
        return _AuditFence(tuple(locks) if supported else (), functools.partial(self._retained_past_share, space))

    def _publish_space(self, space: Space) -> None:
        self._remove_space(space.identity.id)
        self._spaces[space.identity.id] = space
        self._live_weight += space.size

    def _remove_space(self, identity_id: str) -> Space | None:
        space = self._spaces.pop(identity_id, None)
        if space is not None:
            self._live_weight -= space.size
        return space

    def _mutation_share(self, space: Space) -> int:
        return self._launch_mutations.issuer_bytes(space.identity.id) if self._launch_mutations is not None else 0

    def _weighed(self, space: Space) -> None:
        """``space`` weighed afresh (``space_size``): what it holds but its values and its projects first, over which
        each value and each project's masker is weighed as it comes (``_past_share``, ``_projects_past_share``), then
        the whole."""
        runtime = space.runtime
        retained = runtime.request_retained_bytes
        space.fixed = _fixed(runtime, retained)
        space.retained = retained
        space.extra = _detectors(runtime)
        projects = runtime.projects.weight if runtime.projects is not None else 0
        size = space.fixed + projects + _values(runtime.built.vault) + space.extra
        if self._spaces.get(space.identity.id) is space:
            self._live_weight += size - space.size
        space.size = size

    def _retained_past_share(self, space: Space, added: int) -> str | None:
        runtime = space.runtime
        projects = runtime.projects.weight if runtime.projects is not None else 0
        weight = (
            space.fixed
            - space.retained
            + runtime.request_retained_bytes
            + added
            + self._mutation_share(space)
            + projects
            + _values(runtime.built.vault)
        )
        return PAST_SHARE if weight > self.budget * SPACE_SHARE else None

    def _projects_past_share(self, space: Space, projects: int) -> str | None:
        """The share's words when ``space``, its projects weighing ``projects`` (the masker they would build counted),
        would weigh past its share of the budget (``SPACE_SHARE``); None when it fits. Asked by its projects under their
        lock before they build a masker (``Projects._scope``): builds at once, each in its turn of the lock, are weighed
        one after another, never against a weight read before the other's masker was counted."""
        runtime = space.runtime
        weight = (
            space.fixed
            - space.retained
            + runtime.request_retained_bytes
            + projects
            + _values(runtime.built.vault)
            + self._mutation_share(space)
        )
        return PAST_SHARE if weight > self.budget * SPACE_SHARE else None

    def _past_share(self, space: Space, entity: Entity) -> str | None:
        """The share's words when ``space``, holding ``entity`` too, would weigh past its share of the budget
        (``SPACE_SHARE``): what it held at its last weighing but its values and its projects, its projects as they
        weigh now, and its values with this one, by its length too; None when it fits. Asked as the vault keeps a value
        (``runtime.NewValues``), under the vault's lock, in the thread that masks: so one text, however many values it
        brings, never carries the space past its share between two weighings."""
        runtime = space.runtime
        projects = runtime.projects.weight if runtime.projects is not None else 0
        weight = (
            space.fixed
            - space.retained
            + runtime.request_retained_bytes
            + self._mutation_share(space)
            + projects
            + _values(runtime.built.vault, 1, value_chars(entity))
        )
        return PAST_SHARE if weight > self.budget * SPACE_SHARE else None

    def _retained_total(self) -> int:
        shared = self._launch_holds.retained_bytes if self._launch_holds is not None else 0
        mutations = self._launch_mutations.retained_bytes if self._launch_mutations is not None else 0
        return shared + mutations + self._live_weight

    def launch_fits(self) -> bool:
        return self._retained_total() <= self.budget

    def launch_background_fits(self, issuer: str) -> bool:
        mutations = self._launch_mutations
        if mutations is None or not self.launch_fits():
            return False
        space = self._spaces.get(issuer)
        if space is not None:
            return self._request_fence(space)(0) is None
        return mutations.issuer_bytes(issuer) <= self.budget * SPACE_SHARE

    def _weigh(self, space: Space, *, ended: bool = False) -> None:
        """The space weighed again (it may have grown), and past the budget room is made: around ``space``, in use, or,
        ``ended`` (a request of its own just ended) and past its share, with it first in line. One whose vault lacks a
        value another instance kept is made again instead (``_unlearnt``)."""
        if space.runtime.built.vault.lacking is not None:
            self._unlearnt(space)
            return
        self._weighed(space)
        if self._retained_total() > self.budget:
            past = space.size - space.extra > self.budget * SPACE_SHARE
            self._fit(keep=None if ended and past else space.identity.id)

    def _fit(self, keep: str | None) -> None:
        """Over the memory budget, spaces go, closed in the background: those past their share of it first, then those
        used least recently. One past its share goes even while its long connections run (a dashboard's stream, a
        WebSocket: they end with the share's words), but not while it serves a request (it goes as its last one ends:
        ``app``), nor while it holds a batch of the Live Reviewer, whose wait would end not approved, nor when it is
        ``keep`` (in use); while it stays it counts at its share, so that no space within its share goes for what it
        holds past it: one identity never closes the others to make room. One within its share goes only idle (no
        request, no long connection, no batch held), and never ``keep``. A share is judged without what a key's or a
        person's detectors were granted past the ordinary RE2 budget (``Space.extra``), which counts against the budget
        alone.
        Every space is weighed afresh, and nothing is awaited between choosing a space and taking it out: none is
        closed twice, nor one dropped meanwhile."""
        share = self.budget * SPACE_SHARE
        for space in self._spaces.values():
            self._weighed(space)
        total = self._retained_total()
        by_use = sorted(self._spaces.values(), key=lambda space: space.used)
        past, gone = [], []
        for space in (space for space in by_use if space.size - space.extra > share):
            if total <= self.budget:
                break
            if (
                space.identity.id == keep
                or space.serving
                or space.parts.approvals.holding
                or _audit_active(space)
                or not _retire_audit(space)
            ):
                total -= space.size - space.extra - share  # it stays, counted at its share (and its detectors' extra)
                continue
            total -= space.size
            past.append(space)
        for space in (space for space in by_use if space.size - space.extra <= share):
            if total <= self.budget:
                break
            if (
                space.identity.id == keep
                or space.busy
                or space.parts.approvals.holding
                or _audit_active(space)
                or not _retire_audit(space)
            ):
                continue
            total -= space.size
            gone.append(space)
        for space in (*past, *gone):
            self._remove_space(space.identity.id)
        if past:
            self._close_later(past, _PAST_SHARE_CLOSED)
        if gone:
            self._close_later(gone)
