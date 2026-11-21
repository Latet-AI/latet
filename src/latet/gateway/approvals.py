"""The Live Reviewer: new texts wait for you in the dashboard before they leave.

A batch is what one request (gateway mode) or one call of the Claude Code plugin brings that was not
sent before. It waits until you approve it, mask more in it, or stop it. You can only add masking: a
value you mask is masked with a fake from the vault, and nothing already masked can be put back in
clear. Nothing of a batch is written to disk; the originals stay in the daemon's memory, as always.
What waits is kept by an ``ApprovalsBackend``: this process's memory on a computer, Valkey on a Latet
server, where a decision taken in one process wakes a request waiting in another.

With Latet Enterprise on a server a batch or a change may wait for someone else (``decides``): the company's security
team, or the people who control an API key. Whoever waits for it can stop it, never approve it, and the runtime keeps
the company's record of it (``Central``).

Everything here runs on the event loop, but the changes to your settings: the runtime opens and settles
them in a worker thread, and their events reach the dashboards on the loop (``publish_change``). And a
batch that ``open`` awaits is kept by its backend where that costs the loop nothing: a server's queue
works in a thread of its own, and tells the dashboards from there; what a decision (``decide``), a view of
an image (``image``) and a dashboard's list (``pending``) read is read there too.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from latet import observe
from latet.engine.detectors.terms import has_letter_or_digit
from latet.engine.reviewer.reviewer import CATEGORY_TYPES
from latet.engine.vault import MaskingError
from latet.gateway.recorder import Broadcaster
from latet.logline import Paced

if TYPE_CHECKING:
    from latet.state.protocols import ApprovalsBackend, Feed, IdentityRef, Timing

log = logging.getLogger(__name__)

# Claude Code gives up a fetch after 30 s: the plugin asks again and again, each call held this long.
HOLD = 20.0
# A plugin's batch nobody asks about for this long is given up: you stopped Claude Code (Esc), or closed it.
ABANDON_AFTER = 60.0
_REAP_EVERY = 5.0
_MAX_MASKS = 200  # masks in one decision
_MAX_RECTS = 100  # rectangles drawn on one image
_MIN_RECT = 4  # pixels: a rectangle thinner than this covers nothing
_MAX_VALUE = 500  # characters of one masked value
_WORD = re.compile(r"\w")

State = Literal["approved", "rejected", "abandoned", "expired"]
REJECTED = "you did not approve it in the Live Reviewer"
EXPIRED = "it waited for your approval in the Live Reviewer longer than live_review.timeout_s"
ABANDONED = "the agent stopped waiting for it"
REJECTED_BY_SECURITY = "your company's security team did not approve it in the Live Reviewer"
EXPIRED_FOR_SECURITY = "it waited for your company's security team longer than live_review.timeout_s"

# Who decides on what waits (Latet Enterprise, on a server): the person whose space it is (self), the company's
# security team, or, for an API key's change, the people who control the key, never the one who made it.
Decides = Literal["self", "security", "controllers"]
NOT_YOURS: dict[str, str] = {
    "security": "your company's security team decides on this: you can stop it or undo it, not approve it",
    "controllers": (
        "another person who controls this API key decides on this, in Company approvals: you can undo it, not "
        "approve it"
    ),
}
WAITS: dict[str, str] = {  # what waits for the person is said by the Community's ChangeWaits (R42)
    "security": (
        "it lowers protection, so your company's security team decides on it: it waits for them (Approvals), and the "
        "settings in use stay as they are"
    ),
    "controllers": (
        "it lowers protection, so another person who controls this API key decides on it: it waits for them (Company "
        "approvals), and the settings in use stay as they are"
    ),
}
_UNTOLD = Paced()  # a hook that failed to keep how an item ended: said once a minute


class MaskError(ValueError):
    """A mask the Live Reviewer does not take: it would not add protection, or is not in the text."""


class NotYours(Exception):
    """An approval of what someone else decides on (Latet Enterprise), asked by whoever waits for it: they may stop
    it, not approve it."""


class Changed(Exception):
    """An approval of a text as it was read (its ``batch_version``), after it was masked again (its previews made anew
    while it waited: the rules or the vault changed, and it may mask less now): nothing is decided, and it still waits.
    A decider of the company approves only what they saw (the review of Task 10, I1)."""


APPROVED_CHANGED = (
    "it would leave masked otherwise than your company's decider approved it (the rules changed meanwhile): it was "
    "not sent, and waits for them again when the agent sends it again"
)


class ApprovedChanged(MaskingError):
    """Texts a decider of the company approved (``Decision.version``) that masking them where they are sent gives
    otherwise (the rules or the vault changed between the approval and the sending): withheld, through the path of any
    text that cannot be masked, never taken as approved (the review of Task 10, fix round 2). Fixed words."""


def batch_version(items: Sequence[dict[str, Any]]) -> str:
    """A batch's version: a digest of how each of its texts would leave (its masked text, and where each value sits),
    which changes whenever its previews are made anew. Never of its values in clear: the originals are not in it."""
    digest = hashlib.sha256()
    for item in items:
        shown = [item.get("masked"), item.get("replacements")]
        digest.update(json.dumps(shown, sort_keys=True, separators=(",", ":"), default=str).encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


class Waits(Exception):
    """A save in the dashboard lowers protection, and someone else decides on it (the company's security team, or
    another person who controls the API key): it waits for them instead of applying."""

    def __init__(self, change: Change) -> None:
        super().__init__(WAITS.get(change.decides, WAITS["security"]))
        self.change = change


@dataclass(frozen=True, slots=True)
class HumanMask:
    """A value you masked by hand, in one text of a batch."""

    item: int
    value: str
    category: str


@dataclass(frozen=True, slots=True)
class ImageDecision:
    """What you did to one image of a batch: rectangles drawn by hand (covered with a bar), or withheld."""

    item: int
    rects: tuple[tuple[int, int, int, int], ...] = ()
    withhold: bool = False


@dataclass(slots=True)
class Decision:
    approved: bool
    masks: list[HumanMask] = field(default_factory=list)
    images: list[ImageDecision] = field(default_factory=list)
    reason: str | None = None  # why it is not sent
    state: State = "approved"
    by: IdentityRef | None = None  # who decided when it was not whoever waits (kept on the instance that decided)
    # In an API key's space, the person working there who stopped it (the whole-branch review's m1): who ended it, in
    # the company's record and the event, never the agent's reason nor the dashboards' (kept on the instance too).
    stopped_by: str | None = None
    # The batch as a decider of the company approved it (its ``batch_version``), carried to the instance that sends it:
    # there it leaves only while masking it then gives that version (``Mediator.apply_live``). None for whoever waits.
    version: str | None = None


@dataclass(slots=True)
class Batch:
    """New texts of one request, as the dashboard shows them: each with its original, what Latet and
    the local reviewer made of it, and what the reviewer suggested without masking it."""

    session: str
    project: str | None
    agent: str
    # Per text: kind, path, tool, original, masked, replacements (offsets on both sides), suggestions
    # ([{start, end, category}] in the original), failed (the local reviewer could not read it).
    items: list[dict[str, Any]]
    polled: bool = False  # the plugin asks for it again and again: given up when it stops asking
    timeout_s: int | None = None
    id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    created: float = field(default_factory=time.time)
    seen: float = field(default_factory=time.monotonic)
    waiting: int = 0  # calls waiting on it right now (a poll held open)
    images: dict[int, Any] = field(default_factory=dict)  # item → its image (an ImageWork), in memory only
    # The rules' and the vault's versions its previews were made with, and the synced terms' tag, in memory only
    # (``Mediator.hold_live``).
    made_at: tuple[int, int, str] | None = None
    # Who decides on it (Latet Enterprise, on a server): you, or your company's security team, named. Set before it is
    # kept (``Approvals.route_batch``), and kept with it: every instance reads it.
    decides: Decides = "self"
    deciders: list[str] = field(default_factory=list)
    # Your decision, once it is in the queue (made on the event loop: a batch is built in the engine thread).
    future: asyncio.Future[Decision] = field(init=False)

    def summary(self) -> dict[str, Any]:
        expires = self.created + self.timeout_s if self.timeout_s else None
        return {
            "id": self.id,
            "session": self.session,
            "project": self.project,
            "agent": self.agent,
            "created": self.created,
            "expires": expires,
            "items": self.items,
            "decides": self.decides,
            "deciders": self.deciders,
        }


@dataclass(slots=True)
class Change:
    """A change to your settings that lowers protection: it applies once you approve it."""

    config: Any  # the settings it would apply (a Config)
    base: str  # a digest of the settings it was made against: approved over others, it would undo them
    # A latet command, config.yaml edited (on a server: the settings as PostgreSQL has them), found at start, saved in
    # the dashboard.
    source: Literal["command", "file", "start", "dashboard"]
    agent: str | None  # the agent that ran the command, when one did
    loosened: list[str]  # what it lowers, never a value
    keys: list[str]  # the top-level keys it changes
    # The local reviewer's guidance before and after, where it changes: free text, which you read
    # before approving it. For the dashboard only (unlike the reasons, it is the value).
    guidance: list[dict[str, str | None]] = field(default_factory=list)
    # The destinations it moves or removes, with their addresses: where requests would go.
    destinations: list[dict[str, str | None]] = field(default_factory=list)
    # The request exceptions it adds, widens, renews or makes permanent, as the dashboard shows them before you approve
    # (``protection.exception_changes``): what they name, where and for whom. For the dashboard only.
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    created: float = field(default_factory=time.time)
    # Who decides on it: you, your company's security team, or an API key's controllers (Latet Enterprise), named.
    decides: Decides = "self"
    deciders: list[str] = field(default_factory=list)
    # In an API key's space, the person who made it (E3's latet.acting): one of those acting for the key, who never
    # approves it. None in one's own space.
    asked_by: str | None = None

    def summary(self, full: bool = False) -> dict[str, Any]:
        """What the change is; ``full`` for the dashboard: with the guidance it writes."""
        out: dict[str, Any] = {
            "id": self.id,
            "created": self.created,
            "source": self.source,
            "agent": self.agent,
            "loosened": self.loosened,
            "keys": self.keys,
            "decides": self.decides,
            "deciders": self.deciders,
            "asked_by": self.asked_by,
        }
        if full:
            out["guidance"] = self.guidance
            out["destinations"] = self.destinations
            out["exceptions"] = self.exceptions
        return out


def lowered_lines(change: dict[str, Any]) -> list[str]:
    """What a change (its ``Change.summary()``) lowers, as the command that made it says it."""
    return ["This change lowers protection:", *(f"  - {reason}" for reason in change.get("loosened") or [])]


def waiting_lines(change: dict[str, Any], where: str = "`latet dashboard`, Approvals") -> list[str]:
    """What the agent that made a change is told while it waits for the user in the dashboard: the CLI prints it,
    and so does the tutorial's pretend agent, with its own dashboard as ``where``."""
    return [
        f"Not applied yet: it waits for the user's approval in the dashboard ({where}). "
        "Tell them what you changed and why. If they do not approve it, config.yaml goes back as it was.",
        f"Change id: {change['id']} (`latet config set ... --wait` waits for the answer).",
    ]


class Central(Protocol):
    """The company's record of what waits for someone other than whoever asked (Latet Enterprise, on a server): the
    runtime asks it who decides (never whoever asked: the identity, or ``asked_by`` in an API key's space), and tells
    it what opens, what never waited, and how each ended, by its id."""

    def deciders(self, identity: IdentityRef | None, decides: Decides, asked_by: str | None = None) -> list[str]: ...
    def opened(self, identity: IdentityRef | None, item: Change | Batch) -> None: ...
    # ``stopped_by``: in an API key's space, the person working there who stopped it and did not ask for it (an id).
    def ended(
        self, identity: IdentityRef | None, item_id: str, kind: Literal["change", "batch"], state: str,
        by: IdentityRef | None, stopped_by: str | None = None,
    ) -> None: ...  # fmt: skip
    def dropped(self, identity: IdentityRef | None, item_id: str) -> None: ...
    # A text a decider approved, withheld where it was sent: masked then, it was not what they approved.
    def withheld(self, identity: IdentityRef | None, item_id: str) -> None: ...


def _quietly(hook: Callable[..., object], *args: Any) -> None:
    """A hook that keeps how an item ended (the event, the company's record): never in the way of the decision, which
    stands. Its failure is said by its type, once a minute."""
    try:
        hook(*args)
    except Exception as exc:  # its words could name the record's database
        _untold(exc)


def _untold(exc: Exception) -> None:
    """A hook's failure to keep how an item ended, or that it never waited: said by its type, once a minute."""
    if count := _UNTOLD.due():
        log.warning(
            "how an item that waited ended was not kept (%s)%s",
            type(exc).__name__,
            f" ({count} times since the line before)" if count > 1 else "",
        )


async def _nothing_to_route(batch: Batch) -> None:
    return None


async def _nothing_dropped(batch_id: str) -> None:
    return None


class Approvals:
    """The batches waiting for you, the changes to your settings waiting for you, and the dashboards watching them:
    kept by ``backend``, told through ``broadcaster``."""

    def __init__(self, backend: ApprovalsBackend | None = None, broadcaster: Feed | None = None) -> None:
        if backend is None:
            from latet.state.local import LocalApprovals

            backend = LocalApprovals()
        self.backend: ApprovalsBackend = backend
        self.broadcaster: Feed = broadcaster if broadcaster is not None else Broadcaster()
        # How a change's events reach the dashboards: the runtime, which decides on changes in worker threads,
        # hands them over to the event loop (the broadcaster works there only).
        self.publish_change: Callable[[dict[str, Any], str], None] = self.broadcaster.publish
        self.is_surrogate: Callable[[str], bool] = lambda _value: False  # the vault's, once there is one
        # Whether a value is one never to mask (Not sensitive) where a batch comes from.
        self.is_allowed: Callable[[Batch, str], bool] = lambda _batch, _value: False
        # Latet Enterprise on a server (the runtime's, ``Runtime.__post_init__``): who decides on what opens, said and
        # recorded before anyone sees it (raising, it does not open); what was recorded and never waited; how each item
        # ended, once, where it was decided. On the Community Edition they do nothing.
        self.route_change: Callable[[Change], None] = lambda _change: None
        self.route_batch: Callable[[Batch], Awaitable[None]] = _nothing_to_route
        self.dropped_change: Callable[[str], None] = lambda _change_id: None
        self.dropped_batch: Callable[[str], Awaitable[None]] = _nothing_dropped
        # How a change ended, with who decided (``by``) or, in an API key's space, who stopped it there (an id).
        self.on_change_settled: Callable[[str, Change | None, str, IdentityRef | None, str | None], None] = (
            lambda *_args: None
        )
        # On the event loop, or in a server's queue thread (``settle_all``): it must not wait on a network.
        self.on_batch_settled: Callable[[str, Decision, Decides, str | None], None] = lambda *_args: None
        # A text a decider approved, withheld where it was sent (``ApprovedChanged``): from the engine's thread or the
        # event loop, it must not wait on a network either.
        self.on_batch_withheld: Callable[[str, Decides], None] = lambda *_args: None
        self._reaper: asyncio.Task[None] | None = None
        self._unreaped = Paced()  # rounds of the reaper that failed, said once a minute

    @property
    def batches(self) -> dict[str, Batch]:
        """The batches waiting, by id (a copy)."""
        return {batch.id: batch for batch in self.backend.waiting()}

    @property
    def changes(self) -> MutableMapping[str, Change]:
        return self.backend.changes

    @changes.setter
    def changes(self, value: MutableMapping[str, Change]) -> None:
        self.backend.changes = value

    @property
    def settled(self) -> MutableMapping[str, str]:
        return self.backend.settled

    @settled.setter
    def settled(self, value: MutableMapping[str, str]) -> None:
        self.backend.settled = value

    @property
    def viewers(self) -> int:
        return self.backend.viewers()

    # --- the agents' side ------------------------------------------------------------------------

    async def open(self, batch: Batch) -> Batch:
        """``batch`` waits for you, or for whoever decides on it (``route_batch``: Latet Enterprise), and the
        dashboards are told ("pending") once any process can read it. The backend keeps it where that costs the event
        loop nothing (``ApprovalsBackend.store``): a server's queue makes, seals and writes its records in a thread of
        its own, and tells the dashboards from there, where keeping it on the loop held every other request for a
        quarter of a second, seconds for a burst. Raises what keeping it raises (a server's ``BatchRefused``), and tells
        nobody then."""
        await self.route_batch(batch)  # who decides on it, before anyone sees it; raising, it never waits
        try:
            await self.backend.store(batch, lambda: self.broadcaster.publish(batch.summary(), "pending"))
        except BaseException:
            if batch.decides != "self":
                try:
                    await self.dropped_batch(batch.id)  # recorded, and never waited: the record forgets it
                except Exception as exc:  # never in place of why it was not kept, which is raised (Task 1's C2)
                    _untold(exc)
            raise
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())
        return batch

    async def update(
        self, batch_id: str, items: list[dict[str, Any]], pictures: Mapping[int, tuple[str, str]] | None = None
    ) -> bool:
        """A batch opened here shown masked again (the rules or the vault changed while it waited): its previews stored
        with its images painted again (``pictures``, by item: base64 and media type, as each would leave now;
        ``ApprovalsBackend.replace``), and the dashboards told ("updated", the batch as it is now). False: it no longer
        waits here, and nobody is told."""
        batch = await self.backend.replace(batch_id, items, pictures)
        if batch is None:
            return False
        self.broadcaster.publish(batch.summary(), "updated")
        return True

    async def wait(self, batch: Batch, hold: float | None = None) -> Decision | None:
        """Your decision on ``batch``; None when ``hold`` seconds pass first (the plugin asks again)."""
        self.backend.polled(batch, 1)
        observe.waiting(1)  # a server's metrics: requests held here for a person's decision
        try:
            return await self.backend.decision(batch, hold)
        finally:
            observe.waiting(-1)  # first: a backend that fails to end the wait (Valkey away) leaves none counted
            self.backend.polled(batch, -1)

    async def decided(self, batch: Batch) -> Decision:
        """Your decision on ``batch``, however long it takes. Not a poll: it keeps nothing waiting (the plugin's own
        polls do, through ``answer``)."""
        observe.waiting(1)  # a server's metrics: the plugin's calls held here for a person's decision
        try:
            decision = await self.backend.decision(batch, None)
        finally:
            observe.waiting(-1)
        assert decision is not None  # no time limit: there is one
        return decision

    async def answer(self, batch_id: str, hold: float) -> dict[str, Any] | None:
        """What the plugin gets for a batch, once you decided; None after ``hold`` seconds (it asks again). Raises
        KeyError when nothing waits with this id. The answer is handed over once: a later call for the same batch raises
        KeyError too."""
        return await self.backend.answer(batch_id, hold)

    def give(self, batch_id: str, answer: dict[str, Any]) -> bool:
        """What the plugin gets for a batch you decided. Raises ValueError while the batch still waits. True when it was
        kept as given for the plugin's call (``ApprovalsBackend.give``)."""
        return self.backend.give(batch_id, answer)

    async def image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        """An image of a batch waiting, as it came or as it would leave (``sent``), read where it costs the event loop
        nothing (``fetch_image``: a server opens the image's record in its queue's thread)."""
        return await self.backend.fetch_image(batch_id, item, sent)

    def withheld(self, batch: Batch) -> None:
        """``batch``, approved by a decider of the company, was withheld where it was sent (``ApprovedChanged``: masked
        then, it was not what they approved): the dashboards told ("withheld", its id: they say it in their own words,
        the plugin's path recording no exchange), and how it ended kept anew (``on_batch_withheld``), never in the
        way."""
        self.broadcaster.publish({"id": batch.id}, "withheld")
        _quietly(self.on_batch_withheld, batch.id, batch.decides)

    def abandon(self, batch: Batch) -> None:
        """The agent stopped waiting (the client went away): the batch leaves the queue unsent."""
        self._settle(batch.id, Decision(False, reason=ABANDONED, state="abandoned"), batch.decides, batch.agent)

    # --- your side -------------------------------------------------------------------------------

    async def pending(self) -> list[dict[str, Any]]:
        """The batches waiting, as a dashboard lists them, read where it costs the event loop nothing (``listed``: a
        server opens each batch's record in its queue's thread)."""
        return [b.summary() for b in await self.backend.listed()]

    async def decide(
        self,
        batch_id: str,
        approve: bool,
        masks: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
        by: IdentityRef | None = None,
        version: str | None = None,
        stopped_by: str | None = None,
    ) -> bool:
        """Approve a batch (with the values you masked in it, the rectangles you drew on its images and the images you
        withheld), or stop it. False: it is not waiting. ``by``: who decided when it is not whoever waits, one of the
        company's deciders (``CentralApprovals``); what they decide on, whoever waits may stop, never approve
        (``NotYours``). ``version``: the batch as its decider read it (``batch_version``), approved only while it is
        still so (``Changed`` otherwise, nothing decided), and named by every decider's approval (``Changed`` without
        one: the whole-branch security review's L4); stopping it is never refused for it. ``stopped_by``: in an API
        key's space, the person working there, named as who stopped it in the record and the event alone (m1): a stop
        there is whoever waits stopping it. The batch is read where it costs the event loop nothing (``fetch``: a
        server opens its record, its texts with it, in its queue's thread)."""
        batch = await self.backend.fetch(batch_id)
        if batch is None:
            return False
        if approve and batch.decides != "self" and by is None:
            raise NotYours(NOT_YOURS.get(batch.decides, NOT_YOURS["security"]))
        if approve and by is not None and not version:  # a decider's: never taken for the person's own where it leaves
            raise Changed("a decider's approval names the version of the text they read: read it, then decide")
        if approve and version is not None and batch_version(batch.items) != version:
            raise Changed("it was masked again since it was read: read it again, then decide")
        if not approve:
            reason = REJECTED_BY_SECURITY if by is not None else REJECTED
            stopper = stopped_by if by is None else None
            decision = Decision(False, reason=reason, state="rejected", by=by, stopped_by=stopper)
            return self._settle(batch.id, decision, batch.decides, batch.agent)
        # A decider masks what the text holds, never a value of their own in another person's vault (R45).
        masked = self._masks(batch, masks or [], in_text=by is not None)
        # The version they approved goes with their decision: where the text is sent, it leaves only as they read it.
        decision = Decision(True, masked, self._images(batch, images or []), by=by, version=version)
        return self._settle(batch.id, decision, batch.decides, batch.agent)

    async def approve_all(self, project: str | None = None, *, every: bool = True) -> int:
        """Approve every batch waiting, as Latet and the local reviewer masked it; with ``every``
        false, only those of ``project`` (None: those of no project). Settled where it costs the event loop nothing
        (``ApprovalsBackend.settle_all``: a server's queue in its own thread, fifty batches' round trips), and the
        dashboards told from there (a server's feed sends to Valkey, as ``open`` tells them). Only those you decide on
        (never those someone else decides on)."""
        batches = [b for b in await self.backend.listed() if b.decides == "self" and (every or b.project == project)]
        agents = {batch.id: batch.agent for batch in batches}
        return await self.backend.settle_all(
            [(batch.id, Decision(True)) for batch in batches],
            lambda batch_id, decision: self._told(batch_id, decision, "self", agents.get(batch_id)),
        )

    def _told(self, batch_id: str, decision: Decision, decides: Decides = "self", agent: str | None = None) -> None:
        """The dashboards told that a batch was settled (``settled``, with who decided when it was someone else), and
        how it ended kept (``on_batch_settled``), once: where it was settled."""
        by = decision.by.name if decision.by is not None else None
        self.broadcaster.publish({"id": batch_id, "state": decision.state, "by": by}, "settled")
        _quietly(self.on_batch_settled, batch_id, decision, decides, agent)

    # --- changes to your settings ----------------------------------------------------------------

    def waiting_changes(self) -> list[Change]:
        """The changes waiting now, the oldest first, each read by its id: one settled meanwhile (on a server, by
        another instance) is no longer among them."""
        changes = self.changes
        return [change for change_id in list(changes) if (change := changes.get(change_id)) is not None]

    def open_change(self, change: Change) -> Change:
        """``change`` waits (in a worker thread): routed first (``route_change``: who decides, recorded; raising, it
        never waits), then kept by the backend, which keeps an equivalent change already waiting instead (this one then
        never waited: ``dropped_change``) or supersedes the one waiting. How a superseded one ended is told and kept."""
        self.route_change(change)
        waiting = {found.id: found for found in self.waiting_changes()}  # what this one may supersede, read first
        try:
            opened, superseded = self.backend.open(change)
        except BaseException:
            if change.decides != "self":
                _quietly(self.dropped_change, change.id)
            raise
        if opened is not change and change.decides != "self":
            _quietly(self.dropped_change, change.id)  # an equivalent change waits already: this one never did
        for change_id in superseded:
            self.publish_change({"id": change_id, "state": "superseded", "by": None}, "change_settled")
            _quietly(self.on_change_settled, change_id, waiting.get(change_id), "superseded", None, None)
        if opened is change:  # the backend returns the supplied object only for this call's committed insert
            self.publish_change(opened.summary(full=True), "change")
        return opened

    def settle_change(
        self, change_id: str, state: str, by: IdentityRef | None = None, stopped_by: str | None = None
    ) -> Change | None:
        """``change_id`` ended as ``state``, decided ``by`` someone else when given; None when it no longer waits.
        ``stopped_by``: in an API key's space, the person working there who stopped it and did not ask for it, for how
        it ended alone (``on_change_settled``: the record, the event), never told the dashboards as a decider (m1).
        Whether whoever asks may decide on it is checked by its callers, the runtime and a server's spaces alone
        (``Runtime.decide_change``: ``not-yours``): no route calls it (Task 1's review, C3)."""
        change = self.changes.get(change_id)
        if change is None or not self.backend.end(change_id, state):
            return None
        told = {"id": change_id, "state": state, "by": by.name if by is not None else None}
        self.publish_change(told, "change_settled")
        _quietly(self.on_change_settled, change_id, change, state, by, stopped_by)
        return change

    def pending_changes(self) -> list[dict[str, Any]]:
        return [c.summary(full=True) for c in self.waiting_changes()]  # a copy: a worker thread may change them

    async def watch(self) -> asyncio.Queue[Any]:
        """A dashboard's feed of the queue: what waits now, then what changes. The batches are read where it costs the
        event loop nothing (``pending``); what is told meanwhile comes after them, as it would have come after them had
        it been told a moment later, and the dashboard lists each batch once."""
        queue = self.broadcaster.subscribe()
        try:
            batches = await self.pending()
        except BaseException:
            self.broadcaster.unsubscribe(queue)
            raise
        told = [queue.get_nowait() for _ in range(queue.qsize())]
        viewers = self.backend.viewers(1)
        queue.put_nowait(("snapshot", {"batches": batches, "changes": self.pending_changes(), "viewers": viewers}))
        for event in told:
            with contextlib.suppress(asyncio.QueueFull):  # a feed that filled meanwhile misses events, as a slow one
                queue.put_nowait(event)
        self.broadcaster.publish({"viewers": viewers}, "viewers")
        return queue

    def unwatch(self, queue: asyncio.Queue[Any]) -> None:
        self.broadcaster.unsubscribe(queue)
        self.broadcaster.publish({"viewers": self.backend.viewers(-1)}, "viewers")

    # --- inside ----------------------------------------------------------------------------------

    def _masks(self, batch: Batch, raw: list[dict[str, Any]], *, in_text: bool = False) -> list[HumanMask]:
        """Your masks, checked: inside one text, over clear text only, whole words (a secret may be part
        of one), of a known category, one category for each value, never a fake already in use nor a
        value marked Not sensitive. A mask that would leave its value in clear is refused here. ``in_text``: a
        decider's, whose value must be in its text (R45)."""
        if len(raw) > _MAX_MASKS:
            raise MaskError(f"at most {_MAX_MASKS} masks at a time")
        out: dict[tuple[int, str], HumanMask] = {}
        categories: dict[str, str] = {}
        for mask in raw:
            try:
                index, start, end = int(mask["item"]), int(mask["start"]), int(mask["end"])
                category = str(mask["category"])
            except (KeyError, TypeError, ValueError):
                raise MaskError("a mask is {item, start, end, category}") from None
            if not 0 <= index < len(batch.items):
                raise MaskError(f"there is no text {index} in this batch")
            item = batch.items[index]
            text: str = item["original"]
            if category not in CATEGORY_TYPES:
                raise MaskError(f"unknown category {category!r}")
            given = mask.get("value")
            if given is not None and not isinstance(given, str):
                raise MaskError("a mask's value is text")
            # With its value (the dashboard's), a mask is taken by it: the text may have been shown anew while you
            # looked (masked again with the rules of now), the place you chose moved or a part of it masked since.
            # Your value is masked all the same, by itself wherever it is (``Mediator.apply_live``, which stops a text
            # that would leave it in clear), never what came to sit at the old place. Without it, by its place.
            by_place = given is None
            if by_place and not 0 <= start < end <= len(text):
                raise MaskError("a mask must lie inside its text")
            value = text[start:end] if given is None else given
            if in_text and value not in text:
                raise MaskError("this value is not in the text")
            if value != value.strip() or len(value) < 2:
                raise MaskError("a masked value has two characters or more, with no space around it")
            if len(value) > _MAX_VALUE:
                raise MaskError(f"a masked value has at most {_MAX_VALUE} characters")
            if not has_letter_or_digit(value):  # signs alone ("--", "__") would be their own fake
                raise MaskError("a masked value has letters or digits")
            if by_place:  # where you chose it, as the text shows it: whole words, over clear text
                cut = (start > 0 and _WORD.match(text[start - 1])) or (end < len(text) and _WORD.match(text[end]))
                if cut and category != "secret":
                    raise MaskError("select whole words: Latet masks a value wherever it appears as a word")
                if any(r["start"] < end and start < r["end"] for r in item["replacements"]):
                    raise MaskError("this part is masked already")
            if self.is_surrogate(value):
                raise MaskError("this value is a fake Latet already uses")
            if self.is_allowed(batch, value):
                raise MaskError("this value is marked Not sensitive: take it out of Rules first")
            if categories.setdefault(value, category) != category:
                raise MaskError(f"{value[:40]!r} is masked as two different things: choose one")
            out.setdefault((index, value), HumanMask(index, value, category))
        return list(out.values())

    def _images(self, batch: Batch, raw: list[dict[str, Any]]) -> list[ImageDecision]:
        """Your decisions on images, checked: an image of the batch, rectangles inside it and big enough."""
        out: dict[int, ImageDecision] = {}
        for entry in raw:
            try:
                index = int(entry["item"])
                withhold = bool(entry.get("withhold"))
                rects = [tuple(int(v) for v in rect) for rect in entry.get("rects") or []]
            except (KeyError, TypeError, ValueError):
                raise MaskError("an image decision is {item, rects?: [[x0, y0, x1, y1]], withhold?}") from None
            if not 0 <= index < len(batch.items) or not isinstance(batch.items[index].get("image"), dict):
                raise MaskError(f"there is no image {index} in this batch")
            if len(rects) > _MAX_RECTS:
                raise MaskError(f"at most {_MAX_RECTS} rectangles on one image")
            image = batch.items[index]["image"]
            width, height = int(image["width"]), int(image["height"])
            checked = []
            for rect in rects:
                if len(rect) != 4:
                    raise MaskError("a rectangle is [x0, y0, x1, y1]")
                x0, y0, x1, y1 = rect
                if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                    raise MaskError("a rectangle must lie inside its image")
                if x1 - x0 < _MIN_RECT or y1 - y0 < _MIN_RECT:
                    raise MaskError(f"a rectangle is at least {_MIN_RECT} pixels wide and high")
                checked.append((x0, y0, x1, y1))
            out[index] = ImageDecision(index, tuple(checked), withhold)
        return list(out.values())

    def _settle(self, batch_id: str, decision: Decision, decides: Decides = "self", agent: str | None = None) -> bool:
        if not self.backend.settle(batch_id, decision):
            return False  # decided already, here or in another process
        self._told(batch_id, decision, decides, agent)
        return True

    def close(self) -> None:
        """Its space closes (an identity's on a Latet server): the reaper stops, so that it neither keeps the closed
        space's batches, and what masks them, in memory nor goes on reading the queue. On the event loop."""
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None

    async def _reap(self) -> None:
        """Give up the batches nobody waits for any more, and those past their time limit, every few seconds while this
        process holds any. It reads of them what it needs alone (``timings``: when each was made, its time limit, its
        polls), never their texts: a server read every batch's record at each round, each opened in its queue's thread
        (half a second at an identity's limit, which every other identity's batch waited behind). A round that fails (a
        server's Valkey not answering) is said in the log by its type, once a minute at most, and the next one tries
        again. Nothing of the batches is held between two rounds."""
        while True:
            await asyncio.sleep(_REAP_EVERY)
            try:
                if not self._reaped(self.backend.timings()):
                    return
            except Exception as exc:
                if count := self._unreaped.due():
                    log.warning(
                        "the Live Reviewer's reaper could not give up what waits too long (%s); it tries again%s",
                        type(exc).__name__,
                        f" ({count} times since the line before)" if count > 1 else "",
                    )

    def _reaped(self, timings: list[Timing]) -> bool:
        """One round of the reaper over the batches this process holds; False when it holds none."""
        now, clock = time.time(), time.monotonic()
        for timing in timings:
            if timing.timeout_s and now - timing.created > timing.timeout_s:
                reason = EXPIRED_FOR_SECURITY if timing.decides == "security" else EXPIRED
                expired = Decision(False, reason=reason, state="expired")
                self._settle(timing.id, expired, cast(Decides, timing.decides), timing.agent)
            elif timing.polled and not timing.waiting and clock - timing.seen > ABANDON_AFTER:
                abandoned = Decision(False, reason=ABANDONED, state="abandoned")
                self._settle(timing.id, abandoned, cast(Decides, timing.decides), timing.agent)
        return bool(timings)
