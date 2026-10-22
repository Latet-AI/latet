"""The tutorial's session: where the chapter in progress is, and the steps that make it go on.

One chapter at a time, in the language it started in; the menu and a chapter not started come in the page's language
(English where it has no texts). A chapter says what it still needs of this computer (`missing`: text recognition's
models, which the sandbox looks for again each time) and does not start without it. `start` puts the sandbox back to
the chapter's beginning. `next` runs a step in the background (the agent sends its requests through the real gateway,
in-process) and answers at once: a request the Live Reviewer holds waits for the user while the page polls `state`. A
`do` step waits for the user's action first: a value opened (`pin`: the page sees it), or what the sandbox sees in its
own rules, queue and settings (the guidance of the local reviewer saved, too); but an `approve`, which is met while the
step's own request waits. Once a step ran, its exchanges are read back from the history in the order the agent sent
them, and what the page shows is worked out from them as the page renders them.

The agent's `latet config set` is made here as the CLI makes it: config.yaml written, then the daemon's reload,
where a change that lowers protection waits for the user.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import yaml
from pydantic import ValidationError
from starlette.types import ASGIApp

from latet.api.rest import restore_detail
from latet.config import Config, load_config, load_yaml, save_config
from latet.engine.detectors.fields import FieldDetector
from latet.gateway.approvals import lowered_lines, waiting_lines
from latet.tutorial.agent import AGENT, PaneLine, StepRun, resumed
from latet.tutorial.chapters import (
    ENGLISH,
    IMAGE_TYPES,
    LAST,
    ORDER,
    SANDBOX_OWNS,
    Chapter,
    NotReceived,
    Show,
    Step,
    Wait,
    asset,
    languages,
    load_all,
    render,
)
from latet.tutorial.checks import detected, forms
from latet.tutorial.model import NAME

if TYPE_CHECKING:
    from latet.tutorial.progress import Progress
    from latet.tutorial.sandbox import Sandbox

log = logging.getLogger(__name__)

AGENT_ID: Final = "tutorial-agent"  # how Latet names the agent: in its records, and in the changes it asks for
_STOPPING: Final = 10.0  # seconds a step is given to end once what it waits for in the Live Reviewer is not sent
_USAGE: Final = "Usage: latet config set KEY VALUE | latet config set KEY --unset | latet config get KEY"
# Where a change waits, as the agent's `latet config set` says it: this tutorial's dashboard. The CLI names
# `latet dashboard`, which would open the user's own, where the sandbox's changes are not.
_DASHBOARD: Final = "here: Latet's pane"
# What a step shows when its request was stopped before the answer it names came: why it was stopped.
STOPPED: Final = Show(component="why", exchange=LAST, part="request", focus="why")
# What it says then, when its own explanation names what the model would have received.
NOT_SENT: Final = "This request was not sent, so the model received nothing. Latet shows why it was stopped."


class Conflict(Exception):
    """A call that does not fit where the chapter is: why (`not started`, `not this step`, `waiting`, `busy`,
    `needs ocr`: the chapter needs text recognition's models, which the sandbox lacks), and the state to go on from
    (None: that chapter is not the one in progress)."""

    def __init__(self, error: str, state: dict[str, Any] | None) -> None:
        super().__init__(error)
        self.error = error
        self.state = state


@dataclass
class Ran:
    """What one step did: the agent's run, what the model received meanwhile, the step's exchanges in the order they
    were sent (stopped ones too), and what the page shows of it."""

    run: StepRun
    received: list[str] = field(default_factory=list)
    exchanges: list[str] = field(default_factory=list)
    explain: str = ""
    shown: dict[str, Any] | None = None
    not_sent: bool = False  # its request was not sent: the dashboard says so in the reader's language


class TutorialSession:
    def __init__(self, sandbox: Sandbox, progress: Progress, requested: str | None = None) -> None:
        self.sandbox = sandbox
        self.progress = progress
        self.chapters = load_all()  # in English: which chapters there are
        self._languages: dict[str, dict[str, Chapter]] = {ENGLISH: self.chapters}  # the others, once asked for
        self.requested = requested if requested in self.chapters else None  # named on the command line: given once
        self.chapter: Chapter | None = None
        self.lang = ENGLISH  # the language of the chapter started: the one it runs in, whatever the page asks for
        self.run_id = ""  # changes at each start: a page can tell the chapter started over
        self.at = 0  # the step that runs next
        self.lines: list[PaneLine] = []  # the agent's pane, of the steps that ran
        self.ran: dict[int, Ran] = {}
        self.turns: dict[str, str] = {}  # a turn of the model → the exchange it answered
        self.looks: dict[int, dict[str, Any] | None] = {}  # what a `do` step shows before it runs
        self.error: str | None = None
        self.error_at: int | None = None  # the step the error is about: the one `next` ran last
        self.task: asyncio.Task[None] | None = None
        self.running: StepRun | None = None  # the step running, as far as it went
        self._starting = False
        self._stopping = False  # the step at work is being stopped (Start over, the end): it does not count
        self._app: ASGIApp | None = None  # the gateway a step runs through: the agent's commands reach it too
        self._events: asyncio.Queue[Any] | None = None  # the Live Reviewer's, while a step that waits on it runs
        self._batches: dict[str, dict[str, Any]] = {}  # the agent's batches: the texts they hold, how they ended
        self._changes: list[tuple[str, list[str]]] = []  # the changes the agent asked for: id, the keys they change

    @property
    def busy(self) -> bool:
        return self._starting or (self.task is not None and not self.task.done())

    # --- what the page reads ----------------------------------------------------------------------------------

    def chapters_in(self, lang: str) -> dict[str, Chapter]:
        """The chapters in ``lang``, read the first time it is asked for; English for a language with no texts."""
        lang = _language(lang)
        if lang not in self._languages:
            self._languages[lang] = load_all(lang)
        return self._languages[lang]

    def menu(self, lang: str = ENGLISH) -> dict[str, Any]:
        """The chapters in the page's language ``lang``, the one named on the command line (once), and the one in
        progress: until its last step ran, then it is done (its card says so). A card says what the chapter still
        needs of this computer (`missing`), looked for again at each call."""
        requested, self.requested = self.requested, None
        done = set(self.progress.done())
        cards = [
            _card(chapter, done, self.sandbox.missing(chapter.needs)) for chapter in self.chapters_in(lang).values()
        ]
        chapter = self.chapter
        current = None
        if chapter is not None and self.at < len(chapter.steps):
            current = {"chapter": chapter.id, "step": self.at}
        return {"chapters": cards, "requested": requested, "current": current}

    def chapter_view(self, cid: str, lang: str = ENGLISH) -> dict[str, Any] | None:
        """A chapter as the page needs it before it runs, in the page's language ``lang``; the chapter started, in the
        language it runs in (its state speaks that one). None when there is no such chapter. A step that resumes a
        conversation from before Latet was on has its lines in the agent's pane (`resumed`): the page shows them
        above the step's prompt from the start, as /resume does, and the step's run puts the same lines in the pane.
        Like a card of the menu, it says what the chapter still needs of this computer (`missing`)."""
        if self.chapter is not None and self.chapter.id == cid:
            lang = self.lang
        chapter = self.chapters_in(lang).get(cid)
        if chapter is None:
            return None
        steps = [
            {
                "index": index,
                "kind": step.kind,
                "title": step.title,
                "prompt": step.prompt,
                "button": step.button,
                "wait": _wait(step.wait),
                "resumed": [line.as_json() for line in resumed(step, index)],
            }
            for index, step in enumerate(chapter.steps)
        ]
        card = _card(chapter, set(self.progress.done()), self.sandbox.missing(chapter.needs))
        return {**card, "agent": {"name": AGENT, "model": NAME}, "steps": steps}

    def me(self) -> dict[str, Any]:
        """What `/me` says of the tutorial: the chapter in progress (until its last step ran) and its step; and
        whether the local reviewer is on (the sandbox's settings say so). The local reviewer is here in a chapter with
        the Tutorial Reviewer, and stays until another chapter starts."""
        chapter = self.chapter
        current = None
        if chapter is not None and self.at < len(chapter.steps):
            current = {"chapter": chapter.id, "number": _number(chapter), "title": chapter.title, "step": self.at}
        return {"current": current, "reviewer": self.sandbox.runtime.config.llm_review.enabled}

    def state(self, cid: str) -> dict[str, Any]:
        self._current(cid)
        return self._state()

    def picture(self, cid: str, path: str) -> tuple[bytes, str] | None:
        """A picture of chapter ``cid``'s project, as it is on this computer (the pane shows it above the image as it
        was sent), and its type; None when the chapter has no such picture."""
        chapter = self.chapters.get(cid)
        name = chapter.pictures.get(path) if chapter is not None else None
        if name is None:
            return None
        return asset(name), IMAGE_TYPES[PurePosixPath(path).suffix.lower()]

    # --- what the page does -----------------------------------------------------------------------------------

    async def start(self, cid: str, lang: str = ENGLISH) -> dict[str, Any]:
        """Chapter ``cid`` from its beginning (it may be the one in progress: Start over), in ``lang``, which it keeps
        until the next start (English for a language with no texts). A request the agent still waits on in the Live
        Reviewer is not sent; a step otherwise at work is let finish (409 busy). A chapter that needs what the sandbox
        lacks (text recognition's models) does not start (409 `needs ocr`); the sandbox looks for it again here."""
        lang = _language(lang)
        chapter = self.chapters_in(lang).get(cid)
        if chapter is None:
            raise LookupError(cid)
        if missing := self.sandbox.missing(chapter.needs):
            raise Conflict(f"needs {', '.join(missing)}", self._state() if self.chapter is not None else None)
        if self._starting or (self.busy and not self._held()):
            raise Conflict("busy", self._state() if self.chapter is not None else None)
        self._starting = True
        try:
            await self._stop()
            await self.sandbox.reset(chapter)
            self._begin(chapter)
            self.lang = lang
            await self._look()
        finally:
            self._starting = False
        return {"chapter": self.chapter_view(cid), "state": self._state()}

    async def next(self, app: ASGIApp, cid: str, index: int, seen: Sequence[str] = ()) -> dict[str, Any]:
        """Run step ``index`` of ``cid`` through the gateway ``app``, in the background: the state at once (busy).
        ``seen``: what the page saw the user do (`pin`)."""
        chapter = self._current(cid)
        if self.busy:
            raise Conflict("busy", self._state())
        if index != self.at or index >= len(chapter.steps):
            raise Conflict("not this step", self._state())
        wait = chapter.steps[index].wait
        if wait is not None and wait.kind != "approve" and not self._met(wait, seen):
            raise Conflict("waiting", self._state())
        self.error, self.error_at = None, index
        self.running = StepRun()
        self._batches = {}  # the step's own requests only: not one an earlier step, or this one stopped, sent
        self.task = asyncio.create_task(self._run(app, chapter, index, self.running))
        await asyncio.sleep(0)  # the step begins: what the pane shows first (its prompt) is in this answer
        return self._state()

    async def end(self) -> None:
        """The tutorial ends: a request the agent still waits on is not sent, and its step ends."""
        await self._stop()

    # --- a step ---------------------------------------------------------------------------------------------

    async def _run(self, app: ASGIApp, chapter: Chapter, index: int, run: StepRun) -> None:
        step = chapter.steps[index]
        model, agent = self.sandbox.model, self.sandbox.agent
        approvals = self.sandbox.runtime.mediator.approvals
        if step.wait is not None and step.wait.kind == "approve":  # what the Live Reviewer does with its request
            self._events = approvals.broadcaster.subscribe()
        self._app = app
        before = len(model.received)
        try:
            approved = self._approved(chapter, index)
            if step.sends:
                turns = step.model
                if approved:  # the change waited for was approved: the model says what it says then
                    turns = [t.model_copy(update={"text": t.approved}) if t.approved else t for t in turns]
                model.expect(turns, chapter.received)
                # The tool calls a step left are run by the next one that sends: they no longer wait.
                self.lines = [dataclasses.replace(line, pending=False) if line.pending else line for line in self.lines]
            await agent.run_step(app, step, model, index, run)
            if self._stopping:  # stopped by Start over or the end: the step did not run, the chapter stays
                return
            ran = Ran(run, [r.text for r in model.received[before:]], await self._exchanges_of(run))
            for sent, exchange in zip(run.sent, ran.exchanges, strict=False):
                if sent.turn is not None:
                    self.turns[sent.turn] = exchange
            if step.agent.expect == "blocked" and (ran.received or run.error is None):
                self.error = (
                    f"step {index + 1}: the agent's request was meant to be stopped by Latet, and it reached "
                    f"the model ({len(ran.received)} received)"
                )
            try:
                ran.explain = _otherwise(step, ran, approved) or render(step.explain, model.captured)
            except NotReceived as missing:
                # Answered, yet not received: a mistake of the chapter's. A request the user stopped (in the Live
                # Reviewer) never reached the model: then it is no surprise, and the step says it was not sent.
                if run.error is None:
                    name = missing.name
                    self.error = f"step {index + 1}: its explanation names {name}, which the model never received"
                else:
                    ran.explain, ran.not_sent = NOT_SENT, True
            ran.shown = await self._resolve(chapter.show_of(index), ran.exchanges, _guidance(step))
            if ran.shown is None and run.error is not None and ran.exchanges:  # it names an answer that never came
                ran.shown = await self._resolve(STOPPED, ran.exchanges)
            # The step is done: the pane and the chapter move on together.
            self.lines = [*self.lines, *run.lines]
            self.ran[index] = ran
            self.at = index + 1
            self.running = None
            if self.at < len(chapter.steps):
                await self._look()
            else:
                await asyncio.to_thread(self.progress.mark, chapter.id)
        except Exception as exc:
            log.error("the tutorial's step %d of %s could not run: %s", index + 1, chapter.id, type(exc).__name__)
            self.error = f"step {index + 1} could not run: {exc}"
        finally:
            self._watch()
            if self._events is not None:
                approvals.broadcaster.unsubscribe(self._events)
                self._events = None
            self._app = None
            self.running = None

    async def _exchanges_of(self, run: StepRun) -> list[str]:
        """The step's exchanges, in the order the agent sent them, stopped ones too: once every one is recorded, the
        last of its conversation, as many as it sent. The store gives them in the order they were recorded, which is
        the order they were sent: an agent sends a conversation's requests one after the other (their start times
        can be equal, a tick of Windows' clock being 15.6 ms)."""
        rt = self.sandbox.runtime
        if not run.sent or rt.recorder is None or rt.exchanges is None:
            return []
        if not await asyncio.to_thread(rt.recorder.drain, 10):
            self.error = "the dashboard's record of this step is late: what it shows may be missing"
            return []
        recorded = await asyncio.to_thread(rt.exchanges.in_order, self.sandbox.agent.session_key)
        if len(recorded) < len(run.sent):  # history: off keeps none
            self.error = f"The agent sent {len(run.sent)} requests in this step, and {len(recorded)} were recorded"
            return []
        return recorded[len(recorded) - len(run.sent) :]

    async def _stop(self) -> None:
        """The step at work ends: a request the agent waits on in the Live Reviewer is not sent (`start`, `end`)."""
        task = self.task
        if task is None or task.done():
            return
        approvals = self.sandbox.runtime.mediator.approvals
        deadline = time.monotonic() + _STOPPING
        self._stopping = True
        try:
            while not task.done() and time.monotonic() < deadline:
                for batch in self._held_batches():
                    await approvals.decide(batch, False)
                await asyncio.wait({task}, timeout=0.05)
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        finally:
            self._stopping = False

    def _held_batches(self) -> list[str]:
        key = self.sandbox.agent.session_key
        return [b.id for b in list(self.sandbox.runtime.mediator.approvals.batches.values()) if b.session == key]

    def _held(self) -> bool:
        """A request of the agent waits for the user in the Live Reviewer."""
        return bool(self._held_batches())

    def _begin(self, chapter: Chapter) -> None:
        self.chapter = chapter
        self.run_id = secrets.token_hex(4)
        self.at = 0
        self.lines, self.ran, self.turns, self.looks = [], {}, {}, {}
        self.error, self.error_at = None, None
        self.running = None
        self._batches, self._changes = {}, []

    def _current(self, cid: str) -> Chapter:
        if self.chapter is None or self.chapter.id != cid:
            raise Conflict("not started", None)
        return self.chapter

    # --- what a step waits for ------------------------------------------------------------------------------

    def _met(self, wait: Wait, seen: Sequence[str] = ()) -> bool:
        """Whether the user did what ``wait`` asks, as the sandbox sees it (a `pin` as the page saw it)."""
        rt = self.sandbox.runtime
        target = wait.target
        if isinstance(target, list):  # fields: each column kept in clear, by the first of your rules that names it
            rules = FieldDetector(rt.config.fields)
            return all((rule := rules._rule(name)) is not None and not rule.mask for name in target)
        if wait.kind == "pin":
            return "pin" in seen
        if wait.kind == "term":
            return detected(rt.built.masker, target, "terms:")
        if wait.kind == "allow":
            return rt.built.masker.allow.allows(target)
        if wait.kind == "approve":  # the batch that holds it decided (it is not kept once it is), while the step runs
            if self._events is None:  # no step waits on the Live Reviewer: its request is not there to decide
                return False
            self._watch()
            return any(
                batch["state"] is not None and any(target in text for text in batch["texts"])
                for batch in self._batches.values()
            )
        if wait.kind == "guidance":  # any guidance saved: the sentence written in, or the reader's own
            return bool(rt.config.llm_review.guidance)
        asked = [change for change, keys in self._changes if target in keys]  # change: the last the agent asked for
        return bool(asked) and asked[-1] in rt.mediator.approvals.settled

    def _approved(self, chapter: Chapter, index: int) -> bool:
        """Whether the change to the settings step ``index`` follows (the last one a step up to it waits for) was
        approved, where the chapter advises Undo it: the step then says its `approved` texts."""
        waits = [s.wait for s in chapter.steps[: index + 1] if s.wait is not None and s.wait.kind == "change"]
        if not waits:
            return False
        asked = [change for change, keys in self._changes if waits[-1].target in keys]
        return bool(asked) and self.sandbox.runtime.mediator.approvals.settled.get(asked[-1]) == "approved"

    def _watch(self) -> None:
        """What the Live Reviewer said since the last look: the agent's batches, and how each ended."""
        events = self._events
        if events is None:
            return
        key = self.sandbox.agent.session_key
        while True:
            try:
                event = events.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not isinstance(event, tuple):
                continue
            kind, payload = event
            if kind == "pending" and payload.get("session") == key:
                texts = [str(item.get("original", "")) for item in payload.get("items") or []]
                self._batches[payload["id"]] = {"texts": texts, "state": None}
            elif kind == "settled" and payload.get("id") in self._batches:
                self._batches[payload["id"]]["state"] = payload.get("state")

    # --- what the page shows --------------------------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        chapter = self.chapter
        assert chapter is not None
        lines = [*self.lines, *(self.running.lines if self.running is not None else [])]
        finished = self.at >= len(chapter.steps)
        return {
            "chapter": chapter.id,
            "run": self.run_id,
            "next": self.at,
            "busy": self.busy,
            "finished": finished,
            "lines": [line.as_json() for line in lines],
            "steps": [self._step_view(chapter, index) for index in range(len(chapter.steps))],
            "waiting": self._waiting(chapter),
            "end": self._end(chapter) if finished else None,
            "error": self.error,
            "error_step": self.error_at if self.error is not None else None,
        }

    def _step_view(self, chapter: Chapter, index: int) -> dict[str, Any]:
        """A step that ran: its explanation and what it shows. A `do` step says what to do and shows what to act on
        before it runs (its explanation names nothing the model receives: a chapter that does is refused at load); a
        step to watch, nothing until it ran."""
        ran = self.ran.get(index)
        if ran is not None:
            view = {"index": index, "ran": True, "explain": ran.explain, "shown": ran.shown}
            return view | {"not_sent": True} if ran.not_sent else view
        if chapter.steps[index].kind != "do":
            return {"index": index, "ran": False, "explain": "", "shown": None}
        return {"index": index, "ran": False, "explain": chapter.steps[index].explain, "shown": self.looks.get(index)}

    def _waiting(self, chapter: Chapter) -> dict[str, Any] | None:
        """The wait of the step that runs next, and whether the user did it (never, for a `pin`: the page sees it)."""
        if self.at >= len(chapter.steps):
            return None
        wait = chapter.steps[self.at].wait
        if wait is None:
            return None
        return {"step": self.at, "kind": wait.kind, "satisfied": wait.kind != "pin" and self._met(wait)}

    def _end(self, chapter: Chapter) -> dict[str, Any]:
        """What the chapter taught, and where it is in the dashboard: an exchange is a page of its own."""

        def path(to: str) -> str:
            if not to.startswith("exchange:"):
                return to
            exchange = self.turns.get(to.removeprefix("exchange:"))
            return f"/exchanges/{exchange}" if exchange is not None else "/live"

        where = [{"label": link.label, "to": path(link.to)} for link in chapter.end.where]
        return {"learned": list(chapter.end.learned), "where": where, "next": chapter.end.next}

    async def _look(self) -> None:
        """What the step that runs next shows before it runs, when it is a `do` step: what the user acts on."""
        chapter = self.chapter
        if chapter is not None and self.at < len(chapter.steps) and chapter.steps[self.at].kind == "do":
            step = chapter.steps[self.at]
            self.looks[self.at] = await self._resolve(chapter.look_of(self.at), text=_guidance(step))

    async def _resolve(
        self, show: Show | None, own: Sequence[str] = (), text: str | None = None
    ) -> dict[str, Any] | None:
        """``show`` as the page needs it: the exchange by its id, and where the values to point at are in it, read
        from the exchange as the dashboard renders it; a picture of the chapter it shows as it is on this computer
        (`original`), by the route that serves it. ``own``: the step's exchanges (`last`); ``text``: the guidance
        the step writes in for the reader, which a `guidance` part shows. None when it shows nothing, or an exchange
        there is none of (yet)."""
        if show is None:
            return None
        view: dict[str, Any] = {"component": show.component}
        if show.component == "guidance" and text is not None:
            view["text"] = text
        if show.exchange is not None:
            exchange = (own[-1] if own else None) if show.exchange == LAST else self.turns.get(show.exchange)
            detail = await asyncio.to_thread(self._detail, exchange) if exchange is not None else None
            if exchange is None or detail is None:
                return None
            view["exchange"] = exchange
            columns = show.component == "columns"
            part = "request" if columns else show.part or "request"
            if not columns:
                view["part"] = part
            slot = _slot(detail, part, show.highlight, columns)
            if slot is not None:
                view["slot"] = slot
        if show.highlight:
            view["highlight"] = list(show.highlight)
        if show.original is not None and self.chapter is not None:  # a picture of the chapter running, as it is here
            # Encoded as the route decodes it: a `#` or a `%` in the picture's path is part of its name.
            view["original"] = f"/tutorial/chapters/{self.chapter.id}/pictures/{quote(show.original)}"
        for name in ("focus", "command", "snippet", "rules"):
            if (value := getattr(show, name)) is not None:
                view[name] = value
        return view

    def _detail(self, exchange: str) -> dict[str, Any] | None:
        rt = self.sandbox.runtime
        detail = rt.exchanges.detail(exchange) if rt.exchanges is not None else None
        return restore_detail(detail, rt.built.vault) if detail is not None else None

    # --- the agent's `latet config` -------------------------------------------------------------------------

    async def config_command(self, command: str) -> str:
        """`latet config set KEY VALUE` (or `--unset`), and `latet config get KEY`, run by the agent's Bash. A
        change is made as the CLI makes it, with what the CLI prints: config.yaml written (named by its file name:
        the sandbox's folder is this computer's), then the daemon's reload, and the dashboard where a change waits
        (this tutorial's)."""
        try:
            words = shlex.split(command)
        except ValueError as exc:
            return f"Error: {exc}"
        args = words[2:]  # after `latet config`
        if args[:1] == ["get"] and len(args) == 2:
            return await asyncio.to_thread(self._config_get, args[1])
        if args[:1] != ["set"]:
            return _USAGE
        unset = "--unset" in args
        given = [arg for arg in args[1:] if arg != "--unset"]
        if any(arg.startswith("--") for arg in given):  # --wait, --project
            return "Error: in the tutorial's sandbox, `latet config set` takes a KEY and a VALUE, or --unset"
        if len(given) != (1 if unset else 2):
            return "Error: give a VALUE, or --unset"
        refused = await asyncio.to_thread(self._config_set, given[0], None if unset else given[1])
        if refused is not None:
            return refused
        return "\n".join(["saved config.yaml", *await self._reload()])

    def _config_set(self, key: str, value: str | None) -> str | None:
        """config.yaml with ``key`` set to ``value`` (None: back to its default), checked and saved as the CLI does
        it; why not, in the CLI's words. A setting the sandbox keeps for itself (its port and only route, no folder
        outside it, nobody's identity, no reviewer) is refused, before anything is written."""
        if (top := key.split(".", 1)[0]) in SANDBOX_OWNS:
            return f"Error: not in the tutorial's sandbox: `{top}` is the sandbox's own"
        rt = self.sandbox.runtime
        path = rt.config_file
        data = load_config(path).model_dump(mode="json")
        try:
            node, last = _at(data, key)
        except LookupError as exc:
            return f"Error: {exc}"
        if value is None:
            if isinstance(node, list):
                return f"Error: {key}: `config remove` takes an item out of a list"
            del node[last]
        else:
            try:
                node[last] = load_yaml(value)
            except yaml.YAMLError as exc:
                return f"Error: not valid YAML: {exc}"
        try:
            config = Config.model_validate(data)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
            return "\n".join(f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in errors)
        save_config(config, path)
        return None

    def _config_get(self, key: str) -> str:
        """A setting of config.yaml, as `latet config get` prints it."""
        data = load_config(self.sandbox.runtime.config_file).model_dump(mode="json")
        try:
            node, last = _at(data, key)
        except LookupError as exc:
            return f"Error: {exc}"
        text = yaml.safe_dump(node[last], sort_keys=False, allow_unicode=True)
        return text.rstrip().removesuffix("...").rstrip()

    async def _reload(self) -> list[str]:
        """The daemon's reload, asked for as the CLI asks for it (its own call, with the local token, as the agent's
        command): what lowers protection waits for the user. What the CLI prints of the answer."""
        rt = self.sandbox.runtime
        if self._app is None:
            return ["Error: the daemon did not answer"]
        headers = {"x-latet-token": rt.gateway.bridge_token or "", "x-latet-command": "1"}
        headers["x-latet-agent"] = AGENT_ID
        transport = httpx.ASGITransport(app=self._app)
        base = f"http://127.0.0.1:{rt.gateway.port}"
        async with httpx.AsyncClient(transport=transport, base_url=base, trust_env=False) as client:
            answer = await client.post("/_latet/admin/reload", headers=headers)
        body = answer.json() if answer.headers.get("content-type", "").startswith("application/json") else {}
        if answer.status_code != 200 or "error" in body:
            return [f"Error: {body.get('error') or answer.status_code}"]
        pending = body.get("pending")
        if pending:  # it waits for the user: the CLI says so to the agent that ran it
            self._changes.append((str(pending["id"]), [str(key) for key in pending.get("keys") or []]))
            return [*lowered_lines(pending), *waiting_lines(pending, _DASHBOARD)]
        lines = ["Rules reloaded."]
        for problem in body.get("recognizers") or []:
            lines += [f"  recognizer {problem['name']} turned off: {error}" for error in problem.get("errors", [])]
            lines += [f"  recognizer {problem['name']}: {warning}" for warning in problem.get("warnings", [])]
        return lines


def _language(lang: str) -> str:
    """``lang`` when the tutorial has texts in it, else English."""
    return lang if lang in languages() else ENGLISH


def _number(chapter: Chapter) -> int:
    return ORDER.index(chapter.id) + 1


def _card(chapter: Chapter, done: set[str], missing: list[str]) -> dict[str, Any]:
    """A chapter on the menu; ``missing``: what it needs of this computer that the sandbox does not have yet."""
    return {
        "id": chapter.id,
        "number": _number(chapter),
        "title": chapter.title,
        "minutes": chapter.minutes,
        "learn": list(chapter.learn),
        "phase": chapter.phase,
        "done": chapter.id in done,
        "missing": missing,
    }


def _wait(wait: Wait | None) -> dict[str, Any] | None:
    """A step's wait as the page needs it: its kind and hint, and the guidance written in for the reader."""
    if wait is None:
        return None
    view: dict[str, Any] = {"kind": wait.kind, "hint": wait.hint or ""}
    if wait.guidance is not None:
        view["text"] = wait.guidance
    return view


def _guidance(step: Step) -> str | None:
    """The guidance a step writes in for the reader, which its `guidance` part shows."""
    return step.wait.guidance if step.wait is not None else None


def _otherwise(step: Step, ran: Ran, approved: bool = False) -> str | None:
    """What a step says when it did not end as the chapter asks, in the chapter's words: the change to the settings
    approved, where the chapter advises Undo it; or, for a step whose request waited in the Live Reviewer, the request
    not sent (Don't send), sent with the value to mask by hand still in clear (approved as it was), or with a word of
    it in clear (only part of it masked). None: it ended as asked, or the chapter has no words for how it ended."""
    otherwise, wait = step.otherwise, step.wait
    if otherwise is None:
        return None
    if approved and otherwise.approved is not None:
        return otherwise.approved
    if wait is None or wait.kind != "approve" or not isinstance(target := wait.target, str):
        return None
    if ran.run.error is not None and not ran.received:
        return otherwise.not_sent
    if ran.run.error is not None:
        return None

    def sent(value: str) -> bool:
        return any(form in text for text in ran.received for form in forms(value))

    if sent(target):
        return otherwise.in_clear
    if any(sent(word) for word in target.split()):
        return otherwise.part_in_clear
    return None


def _slot(detail: dict[str, Any], part: str, values: Sequence[str], columns: bool = False) -> int | None:
    """Where ``values`` are in an exchange as the dashboard shows it: the request's block, or the answer's tool call
    (-1: its text), that holds the most of them (the latest of those that hold as many). The tool outputs with values
    strict data mode masked only, for their `columns`. With no value: the newest such tool output, or the request's
    newest text the model did not write."""
    if part == "response":
        response = detail.get("response") or {}
        calls = enumerate(response.get("tool_calls") or [])
        texts = [(-1, response.get("text_restored", "")), *((i, call.get("args_restored", "")) for i, call in calls)]
    else:
        slots = list(enumerate(detail["request"]["slots"]))
        if columns:
            slots = [(i, s) for i, s in slots if s["kind"] == "tool_result" and _strict(s)]
        elif not values:  # the step's own text: the newest the model did not write (it may have been sent before)
            slots = [(i, s) for i, s in slots if not s["from_model"]]
        texts = [(i, s["original"]) for i, s in slots]
        if not values:
            return texts[-1][0] if texts else None
    best, most = None, 0
    for at, text in texts:
        found = sum(1 for value in values if any(form in text for form in forms(value)))
        if found and found >= most:
            best, most = at, found
    return best


def _strict(slot: dict[str, Any]) -> bool:
    """A block with values strict data mode masked: what its columns panel is for."""
    return any(replacement.get("module") == "data" for replacement in slot["replacements"])


def _at(data: Any, key: str) -> tuple[Any, str | int]:
    """The container of ``key`` (dotted; a number picks a list item) and the last part of it, as `latet config`
    finds it; LookupError, in its words, when there is none."""
    parts = key.split(".")
    node = data
    for i, part in enumerate(parts[:-1]):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and isinstance(node.get(part), dict | list):
            node = node[part]
        else:
            raise LookupError(f"{'.'.join(parts[: i + 1])}: no such key, or not one with keys inside")
    last = parts[-1]
    if isinstance(node, list):
        if not last.isdigit() or int(last) >= len(node):
            raise LookupError(f"{key}: no such item")
        return node, int(last)
    if not isinstance(node, dict) or last not in node:
        raise LookupError(f"{key}: no such key (`latet config schema` lists them)")
    return node, last
