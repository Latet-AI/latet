"""Egress verification: no sensitive value may leave in clear, whatever masked it (or failed to).

Used as the only enforcement on requests Latet must not modify (Claude Code, masked by its plugin)
and as a final check after rewriting. Blocks already sent earlier in the same session are reported
but never block: they were exposed already and blocking them would wedge the conversation. A block
counts as sent only once a request carrying it is allowed out; one the gateway rewrote, once it has
reached its provider (``left``).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from latet.engine.detectors.base import DetectContext
from latet.engine.escapes import read_escaped
from latet.engine.masker import Masker
from latet.engine.pipeline import LRU, WITHHELD_DUMP, WITHHELD_UNREAD, Scope
from latet.engine.review import ReviewPolicy
from latet.engine.types import EntityClass, Policy, SlotKind, Span
from latet.state.local import LocalGiven

if TYPE_CHECKING:
    from latet.state.protocols import Conversations, Given

# What one entry of the memory of texts found clean takes, at most (a key of 32 bytes in a tuple with the project's,
# a vault version, the memory's own links: measured at 266 bytes).
_CLEAN_BYTES = 320
# What the model wrote, as this process knows it at once: the texts of this many, the most recently written kept. One
# entry takes less than one of the texts found clean (a key of 32 bytes, True, the memory's links).
WRITTEN_HERE = 50_000
_WRITTEN = "w"  # what a store shared by processes keeps for a text the model wrote: that it wrote it
# A text the model wrote, found in this process's memory, has its record in the store shared by processes written again
# (its time to live counted again, or the record made again if it went) at most this often, in seconds.
WRITTEN_KEPT_EVERY = 60.0


@dataclass(frozen=True, slots=True)
class Leak:
    etype: str
    eclass: EntityClass
    rule: str
    kind: SlotKind
    preview: str  # never the value: its type and length only
    new: bool  # first time this block leaves (a leak in a new block blocks the request)
    detail: str | None = None  # auto mode review: the identity detail it is (user_name, paths...)
    allowed: bool = False  # auto mode review: a detail the user lets through to the reviewer
    slot: int = -1  # the index of the slot it is in, among those verified
    start: int = -1  # where the value is in that slot's text: its place only, never the value
    end: int = -1

    @property
    def blocks(self) -> bool:
        return self.new and not self.allowed


@dataclass(slots=True)
class VerifyResult:
    leaks: list[Leak] = field(default_factory=list)
    checked: int = 0
    skipped: int = 0  # blocks verified clean earlier, at the current vault version
    withheld: int = 0  # tool outputs the dump guard withheld, sent for the first time

    @property
    def blocking(self) -> list[Leak]:
        return [leak for leak in self.leaks if leak.blocks]


_DEFAULT_BLOCKING = frozenset({EntityClass.SECRET, EntityClass.CUSTOM, EntityClass.IDENTITY, EntityClass.PII})


class Verifier:
    def __init__(
        self, masker: Masker, block_classes: frozenset[EntityClass] = _DEFAULT_BLOCKING, given: Given | None = None
    ) -> None:
        self.masker = masker
        self.block_classes = block_classes
        # (project scope, a rewritten request's or not, block) → vault version it was verified clean at
        self._clean: LRU[tuple[str, bool, bytes], int] = LRU(100_000)
        # What left, by conversation: this process's memory on a computer, Valkey on a Latet server (a block that left
        # through one instance never blocks the conversation on another).
        self.given: Given = given if given is not None else LocalGiven()
        # What the model wrote, by conversation (``wrote``): this process's memory, and on a Latet server the store
        # every instance reads (``shared``, its conversations'), so that the next request finds it on any of them. This
        # process's memory keeps, for each, when that store was last told of it (``clock``).
        self._written: LRU[bytes, float] = LRU(WRITTEN_HERE)
        self.shared: Conversations | None = None
        self.clock: Callable[[], float] = time.monotonic

    @property
    def cached_bytes(self) -> int:
        """What the memory of blocks verified clean takes, at most, and that of what the model wrote (a server's budget
        of spaces counts them, through the mediator's ``checked_bytes``): their entries are each of one size at most."""
        return (len(self._clean) + len(self._written)) * _CLEAN_BYTES

    def use(self, masker: Masker) -> None:
        """New rules: blocks verified clean before are checked again (what was sent stays sent)."""
        self.masker = masker
        self._clean = LRU(100_000)

    def _key(self, text: str) -> bytes:
        return hmac.new(self.masker.vault.cache_key, b"V\x00" + text.encode(), hashlib.sha256).digest()

    def sent(self, session_key: str, text: str) -> bool:
        """Whether a request that carried ``text`` left in the conversation ``session_key``: this check let it out, or
        the gateway saw it reach its provider (``left``), here or in another process."""
        return self.given.checked(session_key, self._key(text))

    def left(self, session_key: str, texts: Iterable[str]) -> None:
        """A request carrying ``texts`` left in the conversation ``session_key``: the gateway rewrote it and its
        provider answered (no check stands between them to record it). Sent again there, each leaves as it left
        (``sent``)."""
        self.given.keep_checked(session_key, [self._key(text) for text in texts if text])

    def _written_key(self, session_key: str, text: str) -> bytes:
        named = session_key.encode()
        message = b"W" + len(named).to_bytes(4, "big") + named + text.encode()
        return hmac.new(self.masker.vault.cache_key, message, hashlib.sha256).digest()

    def wrote(self, session_key: str, texts: Iterable[str]) -> None:
        """The model wrote ``texts`` in the conversation ``session_key``, each as it is: a text of an answer Latet
        passed on as it came, or the input of a tool call the plugin had restored before the tool ran. Sent back there
        in the model's place, each leaves (``written``). Never waits on a network: a server's store writes off this
        thread, and this process knows them at once."""
        now = self.clock()
        for text in texts:
            if not text:
                continue
            key = self._written_key(session_key, text)
            self._written.put(key, now)
            if self.shared is not None:
                self.shared.remember(key, _WRITTEN)

    def written(self, session_key: str, text: str) -> bool:
        """Whether the model wrote ``text``, exactly so, in the conversation ``session_key`` (``wrote``). Found in this
        process's memory, its record in the store shared by processes is written again all the same, at most once in
        ``WRITTEN_KEPT_EVERY`` seconds and never waited for: a conversation that this instance alone relayed for an hour
        would otherwise find it gone on another (its time to live is counted again; the record is made again if it
        went). Found in that store, its time to live was counted again as it was read."""
        key = self._written_key(session_key, text)
        told, now = self._written.get(key), self.clock()
        if told is not None:
            if self.shared is not None and now - told >= WRITTEN_KEPT_EVERY:
                self.shared.remember(key, _WRITTEN)
                self._written.put(key, now)
            return True
        if self.shared is None or self.shared.recall(key) is None:
            return False
        self._written.put(key, now)
        return True

    def _leaks(
        self,
        masker: Masker,
        text: str,
        spans: Iterable[Span],
        kind: SlotKind,
        new: bool,
        review: ReviewPolicy | None = None,
        slot: int = -1,
    ) -> list[Leak]:
        """The leaks among ``spans`` found in ``text``. What leaves in clear by the person's choice or by Latet's own
        hand is none: a type your policy lets through (``allow``), whatever path its request takes; a word inside a fake
        (a drawn name may hold one of your terms, or your user name)."""
        spans = list(spans)
        details = review.details(text, spans) if review is not None else [None] * len(spans)
        through = review.let_through(details) if review is not None else [False] * len(spans)
        fakes: list[Span] | None = None
        out = []
        for span, detail, allowed in zip(spans, details, through, strict=True):
            value = span.of(text)
            if span.eclass not in self.block_classes:
                continue
            if masker.fake_or_let_through(text, span):  # as written, or as it reads where written in escapes
                continue
            if masker.policies.get(span.etype) == Policy.ALLOW:
                continue  # your settings let this type through: the masking left it so
            fakes = masker.surrogates_in(text) if fakes is None else fakes
            if any(f.start <= span.start and span.end <= f.end for f in fakes):
                continue  # a word of a fake (a drawn name holding one of your terms): nothing of yours
            preview = f"{span.etype}[{len(value)}]"
            out.append(
                Leak(
                    span.etype, span.eclass, span.rule, kind, preview, new, detail, allowed, slot, span.start, span.end
                )
            )
        return out

    def verify(
        self,
        slots: Iterable[tuple[str, SlotKind, bool]],
        session_key: str,
        review: ReviewPolicy | None = None,
        scope: Scope | None = None,
        *,
        record: bool = True,
        rewritten: bool = False,
        written: Callable[[str], bool] | None = None,
    ) -> VerifyResult:
        """``slots`` are (text, kind, from_model) of the request about to leave. ``review`` is given for
        Claude Code's auto mode reviewer: the identity details it allows are reported, never block.
        ``scope`` holds the rules of the session's project; with Latet off there nothing blocks.

        ``record``: a request this check lets out counts as sent here. Not for one the gateway rewrote, which records
        what it carried once its provider has answered (``left``): one that never left must not leave again as it was
        kept (``Mediator._leaves_as_it_left``).

        What leaves in clear by your choice or by Latet's own hand stops nothing, on either path (``_leaks``): a type
        your policy lets through, a word inside a fake.

        ``rewritten``: the texts of a request the gateway masked itself. The model's own texts are not read at all
        there: the gateway masks each again before it leaves, every value the detectors find in it, or sends back the
        very bytes the model wrote, the provider's own. A request Latet only checks sends the texts in the model's place
        as its client kept them, and each is read as a client's text is, unless ``written`` (given for Claude Code's
        requests: ``wrote``) knows it as exactly what the model wrote: its own words, written from the fakes it was
        shown, such as a password it made up for a test, which a tool's answer may have shown again for the vault to
        learn. Any other client's assistant turns are its own texts, whatever their role says (a prefill, a history
        it resumed)."""
        result = VerifyResult()
        masker = scope.masker if scope is not None else self.masker
        scope_key = scope.key if scope is not None else ""
        version = masker.vault.version
        unsent: list[bytes] = []
        for slot, (text, kind, from_model) in enumerate(slots):
            if not text or (rewritten and from_model):
                continue
            key = self._key(text)
            new = not self.given.checked(session_key, key)
            if new:
                unsent.append(key)
                if kind == SlotKind.TOOL_RESULT and text.startswith((WITHHELD_DUMP, WITHHELD_UNREAD)):
                    result.withheld += 1
            if scope is not None and not scope.enabled:
                continue  # it leaves as it is: sent all the same
            clean_at = self._clean.get((scope_key, rewritten, key))
            if clean_at == version:
                result.skipped += 1
                continue
            result.checked += 1
            if clean_at is not None:
                # Clean before: only values learnt since then can make it dirty.
                known, ctx = masker._known_values(), DetectContext(kind)
                spans = read_escaped(text, lambda t, k=known, c=ctx: list(k.detect(t, c)))
            else:
                # Names found by the local model never block (NER is best effort): no need to run it here.
                spans = masker.detect(text, DetectContext(kind), skip=("ner",))
            leaks = self._leaks(masker, text, spans, kind, new, review, slot)
            if leaks and from_model and written is not None and written(text):
                continue  # the model's own words, as it wrote them; not kept as clean: in a client's text it is not
            if leaks:
                result.leaks.extend(leaks)
            else:
                self._clean.put((scope_key, rewritten, key), version)
        if record and not result.blocking:
            # Only a request that leaves exposes its blocks: a blocked one keeps blocking when resent.
            self.given.keep_checked(session_key, unsent)
        return result
