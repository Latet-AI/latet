"""The Live Reviewer: new texts wait for you in the dashboard before they leave.

A batch is what one request (gateway mode) or one call of the Claude Code plugin brings that was not
sent before. It waits until you approve it, mask more in it, or stop it. You can only add masking: a
value you mask is masked with a fake from the vault, and nothing already masked can be put back in
clear. Nothing of a batch is written to disk; the originals stay in the daemon's memory, as always.
What waits is kept by an ``ApprovalsBackend``: this process's memory on a computer, Valkey on a Latet
server, where a decision taken in one process wakes a request waiting in another.

Everything here runs on the event loop, but the changes to your settings: the runtime opens and settles
them in a worker thread, and their events reach the dashboards on the loop (``publish_change``). And a
batch that ``open`` awaits is kept by its backend where that costs the loop nothing: a server's queue
works in a thread of its own, and tells the dashboards from there; what a decision (``decide``), a view of
an image (``image``) and a dashboard's list (``pending``) read is read there too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from latet import observe
from latet.engine.detectors.terms import has_letter_or_digit
from latet.engine.reviewer.reviewer import CATEGORY_TYPES
from latet.gateway.recorder import Broadcaster
from latet.logline import Paced

if TYPE_CHECKING:
    from latet.state.protocols import ApprovalsBackend, Feed, Timing

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


class MaskError(ValueError):
    """A mask the Live Reviewer does not take: it would not add protection, or is not in the text."""


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
    # The rules' and the vault's versions its previews were made with, in memory only (``Mediator.hold_live``).
    made_at: tuple[int, int] | None = None
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
        }


@dataclass(slots=True)
class Change:
    """A change to your settings that lowers protection: it applies once you approve it."""

    config: Any  # the settings it would apply (a Config)
    base: str  # a digest of the settings it was made against: approved over others, it would undo them
    source: Literal["command", "file", "start"]  # a latet command, config.yaml edited, found at start
    agent: str | None  # the agent that ran the command, when one did
    loosened: list[str]  # what it lowers, never a value
    keys: list[str]  # the top-level keys it changes
    # The local reviewer's guidance before and after, where it changes: free text, which you read
    # before approving it. For the dashboard only (unlike the reasons, it is the value).
    guidance: list[dict[str, str | None]] = field(default_factory=list)
    # The destinations it moves or removes, with their addresses: where requests would go.
    destinations: list[dict[str, str | None]] = field(default_factory=list)
    id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    created: float = field(default_factory=time.time)

    def summary(self, full: bool = False) -> dict[str, Any]:
        """What the change is; ``full`` for the dashboard: with the guidance it writes."""
        out: dict[str, Any] = {
            "id": self.id,
            "created": self.created,
            "source": self.source,
            "agent": self.agent,
            "loosened": self.loosened,
            "keys": self.keys,
        }
        if full:
            out["guidance"] = self.guidance
            out["destinations"] = self.destinations
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
        """``batch`` waits for you, and the dashboards are told ("pending") once any process can read it. The backend
        keeps it where that costs the event loop nothing (``ApprovalsBackend.store``): a server's queue makes, seals
        and writes its records in a thread of its own, and tells the dashboards from there, where keeping it on the loop
        held every other request for a quarter of a second, seconds for a burst. Raises what keeping it raises (a
        server's ``BatchRefused``), and tells nobody then."""
        await self.backend.store(batch, lambda: self.broadcaster.publish(batch.summary(), "pending"))
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())
        return batch

    async def update(self, batch_id: str, items: list[dict[str, Any]]) -> bool:
        """A batch opened here shown masked again (the rules or the vault changed while it waited): its previews stored
        (``ApprovalsBackend.replace``), and the dashboards told ("updated", the batch as it is now). False: it no longer
        waits here, and nobody is told."""
        batch = await self.backend.replace(batch_id, items)
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

    def give(self, batch_id: str, answer: dict[str, Any]) -> None:
        """What the plugin gets for a batch you decided. Raises ValueError while the batch still waits."""
        self.backend.give(batch_id, answer)

    async def image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        """An image of a batch waiting, as it came or as it would leave (``sent``), read where it costs the event loop
        nothing (``fetch_image``: a server opens the image's record in its queue's thread)."""
        return await self.backend.fetch_image(batch_id, item, sent)

    def abandon(self, batch: Batch) -> None:
        """The agent stopped waiting (the client went away): the batch leaves the queue unsent."""
        self._settle(batch.id, Decision(False, reason=ABANDONED, state="abandoned"))

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
    ) -> bool:
        """Approve a batch (with the values you masked in it, the rectangles you drew on its images and
        the images you withheld), or stop it. False: it is not waiting. The batch is read where it costs the
        event loop nothing (``fetch``: a server opens its record, its texts with it, in its queue's thread)."""
        batch = await self.backend.fetch(batch_id)
        if batch is None:
            return False
        if not approve:
            return self._settle(batch.id, Decision(False, reason=REJECTED, state="rejected"))
        decision = Decision(True, self._masks(batch, masks or []), self._images(batch, images or []))
        return self._settle(batch.id, decision)

    async def approve_all(self, project: str | None = None, *, every: bool = True) -> int:
        """Approve every batch waiting, as Latet and the local reviewer masked it; with ``every``
        false, only those of ``project`` (None: those of no project). Settled where it costs the event loop nothing
        (``ApprovalsBackend.settle_all``: a server's queue in its own thread, fifty batches' round trips), and the
        dashboards told from there (a server's feed sends to Valkey, as ``open`` tells them)."""
        batches = [b for b in await self.backend.listed() if every or b.project == project]
        return await self.backend.settle_all([(batch.id, Decision(True)) for batch in batches], self._told)

    def _told(self, batch_id: str, decision: Decision) -> None:
        """The dashboards told that a batch was settled (``settled``)."""
        self.broadcaster.publish({"id": batch_id, "state": decision.state}, "settled")

    # --- changes to your settings ----------------------------------------------------------------

    def waiting_changes(self) -> list[Change]:
        """The changes waiting now, the oldest first, each read by its id: one settled meanwhile (on a server, by
        another instance) is no longer among them."""
        changes = self.changes
        return [change for change_id in list(changes) if (change := changes.get(change_id)) is not None]

    def open_change(self, change: Change) -> Change:
        opened, superseded = self.backend.open(change)
        for change_id in superseded:
            self.publish_change({"id": change_id, "state": "superseded"}, "change_settled")
        if opened is change:  # the backend returns the supplied object only for this call's committed insert
            self.publish_change(opened.summary(full=True), "change")
        return opened

    def settle_change(self, change_id: str, state: str) -> Change | None:
        change = self.changes.get(change_id)
        if change is None or not self.backend.end(change_id, state):
            return None
        self.publish_change({"id": change_id, "state": state}, "change_settled")
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

    def _masks(self, batch: Batch, raw: list[dict[str, Any]]) -> list[HumanMask]:
        """Your masks, checked: inside one text, over clear text only, whole words (a secret may be part
        of one), of a known category, one category for each value, never a fake already in use nor a
        value marked Not sensitive. A mask that would leave its value in clear is refused here."""
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

    def _settle(self, batch_id: str, decision: Decision) -> bool:
        if not self.backend.settle(batch_id, decision):
            return False  # decided already, here or in another process
        self._told(batch_id, decision)
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
                self._settle(timing.id, Decision(False, reason=EXPIRED, state="expired"))
            elif timing.polled and not timing.waiting and clock - timing.seen > ABANDON_AFTER:
                self._settle(timing.id, Decision(False, reason=ABANDONED, state="abandoned"))
        return bool(timings)
