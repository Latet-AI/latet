"""The local reviewer: when it reads, and what of its answer is kept.

- **Inline** reviews run before a request leaves, ahead of everything else, using every slot of the
  server: the pieces of a long text are read side by side. One arriving while every slot is busy with
  a background review cuts that one off (closing its connection makes the server drop it), and it is
  sent again afterwards.
- **Background** reviews run after a request left, one at a time.
- A text reviewed once is not reviewed again: its answer is remembered by the text as the client sent
  it (also across restarts, sealed in the vault's database), so resent history costs nothing.

An answer is kept only where it holds: a value must appear verbatim in the text, outside the
placeholders, have a letter or a digit (signs alone, such as ``---``, would be their own fake), and
be of a known category. The reviewer can only add to what is masked, never take anything away, so a
text with instructions for it can at worst make it find less; and when a text addresses the reviewer
and its answer is empty, the review counts as failed instead.

With ``llm_review.images`` the text read in an image is reviewed with the image itself (a model that
sees images): a value it sees there is looked for in the text read, also misread a little (any case,
any spacing); one that is nowhere in it is kept apart (``unlocated``), and its image is withheld.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from latet.config import LlmReview, data_dir
from latet.engine.detectors.terms import has_letter_or_digit
from latet.engine.masker import ExtraValue
from latet.engine.pipeline import LRU
from latet.engine.reviewer import certificate as certificates
from latet.engine.reviewer.client import ReviewClient, ReviewError
from latet.engine.reviewer.prompt import CLOSE, OPEN, PROMPT_VERSION, SCHEMA, system_for
from latet.engine.reviewer.view import View, pieces
from latet.engine.types import EntityClass
from latet.fileio import FileReadError, read_regular_bytes
from latet.logline import Paced
from latet.secret_text import KEY_BYTES, key_problem
from latet.storage.keys import load_reviewer_key

log = logging.getLogger(__name__)
_KEY_UNAVAILABLE = Paced()

# The type and class a category's values are masked as. The reviewer is best effort, like the name
# model: what it finds never blocks a request, except a secret, which blocks like any other.
CATEGORY_TYPES: dict[str, tuple[str, EntityClass]] = {
    "person": ("PERSON", EntityClass.LLM),
    "address": ("STREET_ADDRESS", EntityClass.LLM),
    "phone": ("PHONE", EntityClass.LLM),
    "email": ("EMAIL", EntityClass.LLM),
    "id": ("ID_NUMBER", EntityClass.LLM),
    "health": ("HEALTH", EntityClass.LLM),
    "sensitive": ("SENSITIVE", EntityClass.LLM),
    "financial": ("FINANCIAL", EntityClass.LLM),
    "secret": ("SECRET", EntityClass.SECRET),
    "org": ("ORG", EntityClass.LLM),
    "confidential": ("CONFIDENTIAL", EntityClass.LLM),
}
_MAX_QUEUED = 500  # background pieces waiting; past this a new one is not reviewed (and says so)
_MAX_SLOTS = 4

# A text that speaks to the reviewer: an instruction addressed to it, an answer ready for it to copy,
# or the tokens that end a chat turn.
_VERBS = r"(?:output|reply|respond|return|answer|report|print|say|ignore|skip|leave)"
_ADDRESSED = re.compile(
    rf"(?i)\b(?:privacy\s+)?(?:reviewer|ai|assistant|llm)\s*[:,]\s*{_VERBS}\b"
    r"|\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+instructions\b"
    r"|\b(?:reply|respond|answer)\s+with\s+(?:an?\s+)?empty\s+(?:list|answer|array)\b"
    r'|"findings"\s*:\s*\[\s*\]'
    r"|<\|im_(?:start|end)\|>|\[/?INST\]|<\|(?:start|end)_header_id\|>"
)


# A title before a name ("dottor Ferri"): the name alone is masked, and its fake reads as a name.
_TITLE = re.compile(
    r"(?i)^(?:dott(?:or|ore|\.)?|dr\.?|sig(?:\.ra|\.|nora|nore|nor)?|avv(?:\.|ocato)?|ing\.|"
    r"prof(?:\.|essor|essore|essoressa)?|mrs?\.?|ms\.?|miss)\s+(?=\S)"
)


def addresses_reviewer(text: str) -> bool:
    return _ADDRESSED.search(text) is not None


@dataclass(frozen=True, slots=True)
class Finding:
    value: str
    category: str

    def extra(self) -> ExtraValue:
        etype, eclass = CATEGORY_TYPES[self.category]
        return ExtraValue(self.value, etype, eclass, f"llm:{self.category}")


@dataclass(frozen=True, slots=True)
class Picture:
    """The image a text was read in, as the reviewer is shown it: ``key``, the image's own (its answer is
    remembered with it), and ``url``, a data URL."""

    key: str
    url: str


@dataclass(slots=True)
class Answer:
    findings: list[Finding] = field(default_factory=list)
    block: bool = False
    # Values the reviewer saw in an image and Latet could not find in the text read in it.
    unlocated: list[Finding] = field(default_factory=list)
    # Why the review did not happen, or is not to be trusted: "timeout", "unreachable", "unreadable",
    # "suspect" (the text speaks to the reviewer and it found nothing), "busy" (too much queued).
    failed: str | None = None
    seconds: float = 0.0
    cached: bool = False

    def merge(self, other: Answer) -> None:
        seen = {f.value for f in self.findings}
        self.findings += [f for f in other.findings if f.value not in seen]
        lost = {f.value for f in self.unlocated}
        self.unlocated += [f for f in other.unlocated if f.value not in lost]
        self.block = self.block or other.block
        self.failed = self.failed or other.failed
        self.seconds = max(self.seconds, other.seconds)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"findings": [[f.value, f.category] for f in self.findings], "block": self.block}
        if self.unlocated:
            out["unlocated"] = [[f.value, f.category] for f in self.unlocated]
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Answer:
        """An answer remembered: one from before values of signs alone were left out may hold one."""
        found = [Finding(str(v), str(c)) for v, c in data.get("findings") or [] if _holds(str(v), c)]
        lost = [Finding(str(v), str(c)) for v, c in data.get("unlocated") or [] if _holds(str(v), c)]
        return cls(found, bool(data.get("block")), lost, cached=True)


def _holds(value: str, category: Any) -> bool:
    """A value of a known category, with a letter or a digit: signs alone ("---") would be their own fake."""
    return category in CATEGORY_TYPES and has_letter_or_digit(value)


class ReviewMemory(Protocol):
    """Answers kept across restarts (sealed, like the vault): by a keyed hash of the reviewed text."""

    def load_review(self, key: bytes) -> dict[str, Any] | None: ...

    def save_review(self, key: bytes, answer: dict[str, Any]) -> None: ...


def _parse(content: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(content)
    except ValueError:
        found = re.search(r"\{.*\}", content, re.S)
        if found is None:
            return None
        try:
            obj = json.loads(found.group(0))
        except ValueError:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("findings"), list):
        return None
    return obj


# A label painted over a value masked in an image, as the reviewer is shown it: [EMAIL#1].
_LABEL = re.compile(r"^\[[A-Z_]+#\d+\]$")


def _loosely(value: str) -> re.Pattern[str]:
    """``value`` in any case and with any spacing between its characters: as text recognition may
    have read it (``IT60 X054`` for ``IT60X054``)."""
    chars = [re.escape(c) for c in value if not c.isspace()]
    return re.compile(r"\s*".join(chars), re.IGNORECASE)


def read_answer(content: str, view: View, image: bool = False) -> Answer:
    """The findings of an answer that hold for ``view``: verbatim, outside the placeholders. ``image``:
    the reviewer was shown the image the text was read in; a value not in the text verbatim is looked
    for loosely, and one that is nowhere in it goes to ``unlocated``."""
    obj = _parse(content)
    if obj is None:
        return Answer(failed="unreadable")
    out: list[Finding] = []
    lost: list[Finding] = []
    seen: set[str] = set()
    for item in obj["findings"]:
        if not isinstance(item, dict):
            continue
        value = str(item.get("text") or "").strip()
        category = str(item.get("category") or "")
        if category not in CATEGORY_TYPES or not 2 <= len(value) <= 200 or OPEN in value or CLOSE in value:
            continue
        if category == "person" and len(name := _TITLE.sub("", value)) >= 2:
            value = name
        if value in seen or not has_letter_or_digit(value):
            continue  # signs alone ("---", or "--" after a title) would be their own fake
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(value), view.text)]
        if not spans and image and not _LABEL.match(value):
            spans = [(m.start(), m.end()) for m in _loosely(value).finditer(view.text)]
            if not spans:
                seen.add(value)
                lost.append(Finding(value, category))
                continue
            value = next((view.text[a:b] for a, b in spans if view.outside_holes(a, b)), value)
        if not any(view.outside_holes(a, b) for a, b in spans):
            continue  # not in the text, or only where Latet masked it already
        seen.add(value)
        out.append(Finding(value, category))
    answer = Answer(out, bool(obj.get("block")), lost)
    if not out and not lost and addresses_reviewer(view.text):
        answer.failed = "suspect"
    return answer


KeySource = Literal["file", "env", "saved"]
KeyReader = Callable[[LlmReview], str | None]
_clear_key = Paced()


def fixed_key(key: str | None) -> KeyReader:
    """The server's key, independent of identity settings; withheld from a remote address reached in clear."""

    def given(config: LlmReview) -> str | None:
        if key is not None and config.in_clear():
            if _clear_key.due():
                log.warning("the server's reviewer key is withheld: set an https address for the shared reviewer")
            return None
        return key

    return given


def _key_and_source(config: LlmReview) -> tuple[str | None, KeySource | None]:
    if config.api_key_file:
        try:
            content = read_regular_bytes(Path(config.api_key_file).expanduser(), limit=KEY_BYTES, follow=True)
            key = content.decode("utf-8-sig").strip()
        except (FileReadError, UnicodeDecodeError):
            key = None
        if key == "":
            return None, None
        if key is not None and key_problem(key) is None:
            return key, "file"
        if _KEY_UNAVAILABLE.due():
            log.warning("the local reviewer's configured file key is unusable")
        return None, None
    if key := os.environ.get(config.api_key_env):
        if key_problem(key) is None:
            return key, "env"
        if _KEY_UNAVAILABLE.due():
            log.warning("the local reviewer's environment key is unusable")
        return None, None
    key = load_reviewer_key(data_dir())
    return (key, "saved") if key else (None, None)


def certificate_of(config: LlmReview) -> str | None:
    """The certificate you trusted for the server (None: none trusted, or it went missing)."""
    if not config.certificate:
        return None
    pem = certificates.load(data_dir(), config.certificate)
    if pem is None:
        log.warning("the certificate trusted for the local reviewer is missing: trust it again")
    return pem


def client_for(config: LlmReview, timeout: float = 300.0, *, key_of: KeyReader | None = None) -> ReviewClient:
    """A client with the given key reader; omitted only on a computer, which reads its own key."""
    return ReviewClient(
        config.url,
        config.model,
        (key_of if key_of is not None else api_key_of)(config),
        timeout=timeout,
        pinned=config.certificate,
        certificate=certificate_of(config),
    )


def api_key_of(config: LlmReview) -> str | None:
    """The server's API key: from the file named in the configuration, from its variable, or the one
    saved from the dashboard."""
    return _key_and_source(config)[0]


def key_source(config: LlmReview) -> KeySource | None:
    """Where the server's API key comes from (never the key itself), or None without one."""
    return _key_and_source(config)[1]


@dataclass(slots=True)
class _Batch:
    """The pieces of one background review, and who is told when they are all done."""

    left: int
    done: Callable[[Answer], None]
    remember: Callable[[Answer], None]
    answer: Answer = field(default_factory=Answer)

    def deliver(self, part: Answer) -> None:
        self.answer.merge(part)
        self.left -= 1
        if self.left:
            return
        if not self.answer.failed:
            self.remember(self.answer)
        try:
            self.done(self.answer)
        except Exception as exc:  # by its type: its words can quote what the reviewer found
            log.error("a background review's result could not be applied: %s", type(exc).__name__)


@dataclass(slots=True)
class _Job:
    piece: View
    batch: _Batch
    guidance: str | None = None
    picture: Picture | None = None


class Reviewer:
    def __init__(
        self,
        config: LlmReview,
        cache_key: bytes,
        client: ReviewClient,
        memory: ReviewMemory | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.memory = memory
        self._cache_key = cache_key
        self._pieces: LRU[bytes, Answer] = LRU(20_000)  # by the piece as the reviewer read it
        self._texts: LRU[bytes, Answer] = LRU(20_000)  # by the whole text as the client sent it
        self._slots: asyncio.Semaphore | None = None
        self.slots = config.slots  # the server's, once known
        self._waiting = 0  # inline reviews waiting for a slot or on the server
        self._background: set[asyncio.Future[str]] = set()  # background reviews on the server now
        self._queue: deque[_Job] = deque()
        self._wake: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._quiet: asyncio.Event | None = None  # set when nothing is queued or on the server
        self.preempted = 0  # background reviews cut off for inline ones (tests, the dashboard)

    def _key(self, tag: str, text: str) -> bytes:
        data = f"{tag}{PROMPT_VERSION}|{self.config.model or ''}|{text}".encode()
        return hmac.new(self._cache_key, data, hashlib.sha256).digest()

    def _piece_key(self, text: str, guidance: str | None, picture: Picture | None = None) -> bytes:
        """A piece's answer depends on the user's guidance it was read with (a whole text's does not:
        see ``known``), and on the image it was shown with."""
        tag = f"I{picture.key}|" if picture is not None else ""
        if guidance:
            tag += f"G{hashlib.sha256(guidance.encode()).hexdigest()[:32]}|"
        return self._key(f"{tag}P", text)

    @staticmethod
    def _text_of(original: str, picture: str | None) -> str:
        """What a whole text's answer is remembered by: the text, and the image it was read in when the
        reviewer saw that too (``picture``: the image's key)."""
        return original if picture is None else f"{original}\0image:{picture}"

    # --- what was reviewed before ---------------------------------------------------------------

    def known(self, original: str, picture: str | None = None) -> Answer | None:
        """The answer for a text reviewed before, by the text as the client sent it (any thread). A
        text is read once, with the guidance of the day: when the guidance changes, the history an
        agent sends again is not read again (it would wait for its review, and leave masked anew).
        ``picture``: the key of the image the text was read in, when the reviewer is shown it."""
        key = self._key("T", self._text_of(original, picture))
        hit = self._texts.get(key)
        if hit is None and self.memory is not None:
            try:
                data = self.memory.load_review(key)
            except Exception as exc:  # by its type: a store's words can quote what it holds
                log.error("a remembered review could not be read: %s", type(exc).__name__)
                data = None
            if data is not None:
                hit = Answer.from_json(data)
                self._texts.put(key, hit)
        if hit is None:
            return None
        return Answer(list(hit.findings), hit.block, list(hit.unlocated), cached=True)

    def forget_answers(self, values: Iterable[str] | None = None) -> None:
        """Values forgotten by themselves (an API key's, Task 11b): the answers remembered in memory, by text and by
        piece, that found one of ``values`` (its findings, or those it could not place, as written) dropped, or every
        one when the values are not known here (None: an instance that heard of the forget by its entries' ids alone,
        review 11b's N3). A text sent again finds its answer in the store again (``memory``), where the forget took
        those that held a value; a piece is read again by the reviewer only within a text it never answered."""
        asked = None if values is None else set(values)

        def holds(answer: Answer) -> bool:
            return any(f.value in asked for f in (*answer.findings, *answer.unlocated)) if asked is not None else True

        gone = holds if asked is not None else None
        self._texts.discard_if(gone)
        self._pieces.discard_if(gone)

    def remember(self, original: str, answer: Answer, picture: str | None = None) -> None:
        key = self._key("T", self._text_of(original, picture))
        self._texts.put(key, answer)
        if self.memory is not None:
            try:
                self.memory.save_review(key, answer.to_json())
            except Exception as exc:  # by its type: a store's words can quote what it was given
                log.error("a review could not be remembered: %s", type(exc).__name__)

    # --- inline -----------------------------------------------------------------------------------

    async def _semaphore(self) -> asyncio.Semaphore:
        if self._slots is None:
            slots = self.slots
            if slots is None:
                props = await self.client.props()
                slots = int((props or {}).get("total_slots") or 1)
            if self._slots is None:
                self.slots = max(1, min(slots, _MAX_SLOTS))
                self._slots = asyncio.Semaphore(self.slots)
        return self._slots

    async def review_all(
        self,
        items: Sequence[tuple[View, str]],
        budget: float | None = None,
        guidance: str | None = None,
        pictures: Sequence[Picture | None] = (),
    ) -> list[Answer]:
        """Review each (view, original) now, its pieces side by side up to the server's slots, ahead
        of any background review. Past ``budget`` seconds what is not done counts as failed.
        ``guidance``: the user's own, after Latet's instructions. ``pictures``: for each item, the
        image its text was read in, shown to the reviewer with it (None: a text alone)."""
        shown = [*pictures, *[None] * (len(items) - len(pictures))]
        tasks = [
            asyncio.ensure_future(self._review_one(view, original, guidance, picture))
            for (view, original), picture in zip(items, shown, strict=True)
        ]
        if not tasks:
            return []
        done, pending = await asyncio.wait(tasks, timeout=budget)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return [t.result() if t in done else Answer(failed="timeout") for t in tasks]

    async def review(
        self,
        view: View,
        original: str,
        budget: float | None = None,
        guidance: str | None = None,
        picture: Picture | None = None,
    ) -> Answer:
        return (await self.review_all([(view, original)], budget, guidance, [picture]))[0]

    def _pieces_of(self, view: View, picture: Picture | None) -> list[View]:
        """A text shown with its image is read whole, with it."""
        return [view] if picture is not None else pieces(view, self.config.chunk_chars)

    async def _review_one(self, view: View, original: str, guidance: str | None, picture: Picture | None) -> Answer:
        key = picture.key if picture is not None else None
        known = self.known(original, key)
        if known is not None:
            return known
        parts = self._pieces_of(view, picture)
        answers = await asyncio.gather(*(self._piece(p, guidance, picture) for p in parts))
        total = Answer()
        for answer in answers:
            total.merge(answer)
        if not total.failed:
            self.remember(original, total, key)
        return total

    async def _piece(self, piece: View, guidance: str | None, picture: Picture | None = None) -> Answer:
        key = self._piece_key(piece.text, guidance, picture)
        hit = self._pieces.get(key)
        if hit is not None:
            return hit
        started = time.perf_counter()
        try:
            image = picture.url if picture is not None else None
            content = await self._ask_first(system_for(guidance, image is not None), piece.text, image)
        except ReviewError as exc:
            log.info("the local reviewer did not answer: %s", exc)
            return Answer(failed="unreachable")
        answer = read_answer(content, piece, picture is not None)
        answer.seconds = time.perf_counter() - started
        if answer.failed != "unreadable":
            self._pieces.put(key, answer)
        return answer

    async def _ask_first(self, system: str, text: str, image: str | None = None) -> str:
        slots = await self._semaphore()
        self._waiting += 1
        try:
            if slots.locked():  # every slot busy: make room by cutting a background review off
                for task in self._background:
                    if not task.done():
                        task.cancel()  # its connection closes and the server drops it; it is sent again later
                        self.preempted += 1
                        break
            async with slots:
                return await self.client.ask(system, text, SCHEMA, image)
        finally:
            self._waiting -= 1
            if self._waiting == 0 and self._wake is not None:
                self._wake.set()

    # --- background -------------------------------------------------------------------------------

    def submit(
        self,
        view: View,
        original: str,
        done: Callable[[Answer], None],
        guidance: str | None = None,
        picture: Picture | None = None,
    ) -> None:
        """Review ``view`` when the server is free (call on the event loop); ``done`` is called with
        the answer, also when the review could not happen. ``guidance``: the user's own; ``picture``:
        the image the text was read in, shown with it."""
        key = picture.key if picture is not None else None
        known = self.known(original, key)
        if known is not None:
            done(known)
            return
        parts = self._pieces_of(view, picture)
        batch = _Batch(len(parts), done, lambda answer: self.remember(original, answer, key))
        for piece in parts:
            hit = self._pieces.get(self._piece_key(piece.text, guidance, picture))
            if hit is not None:
                batch.deliver(hit)
            elif len(self._queue) >= _MAX_QUEUED:
                batch.deliver(Answer(failed="busy"))
            else:
                self._queue.append(_Job(piece, batch, guidance, picture))
        if self._queue:
            self._start()

    def _start(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._quiet is None:
            self._quiet = asyncio.Event()
        self._quiet.clear()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work(), name="latet-reviewer")
        self._wake.set()

    async def _work(self) -> None:
        assert self._wake is not None
        while True:
            if not self._queue and self._quiet is not None:
                self._quiet.set()
            if not self._queue or self._waiting:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = self._queue[0]
            key = self._piece_key(job.piece.text, job.guidance, job.picture)
            hit = self._pieces.get(key)
            if hit is not None:
                self._queue.popleft()
                job.batch.deliver(hit)
                continue
            slots = await self._semaphore()
            started = time.perf_counter()
            async with slots:
                if self._waiting:
                    continue  # an inline review came first
                image = job.picture.url if job.picture is not None else None
                system = system_for(job.guidance, image is not None)
                task = asyncio.ensure_future(self.client.ask(system, job.piece.text, SCHEMA, image))
                self._background.add(task)
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    task.cancel()
                    raise
                finally:
                    self._background.discard(task)
            if task.cancelled():
                continue  # cut off for an inline review: sent again, from the front of the queue
            self._queue.popleft()
            try:
                content = task.result()
            except ReviewError as exc:
                log.info("the local reviewer did not answer: %s", exc)
                job.batch.deliver(Answer(failed="unreachable"))
                continue
            answer = read_answer(content, job.piece, job.picture is not None)
            answer.seconds = time.perf_counter() - started
            if answer.failed != "unreadable":
                self._pieces.put(key, answer)
            job.batch.deliver(answer)

    @property
    def queued(self) -> int:
        return len(self._queue)

    async def idle(self, within: float = 30.0) -> bool:
        """Wait until nothing is queued or on the server (tests)."""
        if self._quiet is None:
            return True
        try:
            async with asyncio.timeout(within):
                await self._quiet.wait()
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
        await self.client.aclose()
