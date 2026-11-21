"""Egress verification: no sensitive value may leave in clear, whatever masked it (or failed to).

Used as the only enforcement on requests Latet must not modify (Claude Code, masked by its plugin)
and as a final check after rewriting. Blocks already sent earlier in the same session are reported
but never block: they were exposed already and blocking them would wedge the conversation. A block
counts as sent only once a request carrying it is allowed out; one the gateway rewrote, once it has
reached its provider (``left``).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from latet.engine.detectors.base import DetectContext
from latet.engine.escapes import read_escaped
from latet.engine.masker import Masker
from latet.engine.merge import merge_spans
from latet.engine.pipeline import LRU, WITHHELD_DUMP, WITHHELD_UNREAD, Scope
from latet.engine.review import ReviewPolicy
from latet.engine.types import EntityClass, SlotKind, Span
from latet.state.local import LocalGiven

if TYPE_CHECKING:
    from latet.state.protocols import Conversations, Given

# What one entry of the memory of texts found clean takes, at most (a key of 32 bytes in a tuple with the project's and
# the synced terms' tag, a vault version, the memory's own links: measured at 274 bytes, the tag one reference to a
# string every masker of the runtime shares).
_CLEAN_BYTES = 320
# What the model wrote, as this process knows it at once: the texts of this many, the most recently written kept. One
# entry takes less than one of the texts found clean (a key of 32 bytes, True, the memory's links).
WRITTEN_HERE = 50_000
_WRITTEN = "w"  # what a store shared by processes keeps for a text the model wrote: that it wrote it
# A text the model wrote, found in this process's memory, has its record in the store shared by processes written again
# (its time to live counted again, or the record made again if it went) at most this often, in seconds.
WRITTEN_KEPT_EVERY = 60.0
# The time the local name model has to read a request's new members, in seconds, when the caller gives none: the time
# masking gives it for a document (``mediate.DOCUMENT_READ_SECONDS``, which the gateway gives here).
NEW_MEMBERS_READ_SECONDS = 5.0


class NewMembersUnread(Exception):
    """The local name model could not read a request's new members (``verify``'s ``new_members``) in the time it has:
    the request does not leave, as a document masking cannot read in time does not (it fails closed). Its words name
    nothing of the request."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"the local name model could not read {count} texts of new request members in time")


def _field(text: str) -> tuple[str, str] | None:
    """The field and the value a text of ``new_member_fields`` holds (``new_members.field``: ``{"<field>": <value>}``,
    one member, a string or a number), the number as its text; None for any other text."""
    try:
        found = json.loads(text)
    except ValueError:
        return None
    if not isinstance(found, dict) or len(found) != 1:
        return None
    [(name, value)] = found.items()
    if isinstance(value, str):
        return name, value
    if type(value) in (int, float):
        return name, str(value)
    return None


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
    rules: set[str] = field(default_factory=set)
    authority: set[str] = field(default_factory=set)
    withheld: int = 0  # tool outputs the dump guard withheld, sent for the first time
    # How many texts were verified: the slots, then the new members' and their fields, then ``checked_only``'s last,
    # which a leak's ``slot`` counts in (``checked_only[slot - (texts - len(checked_only))]``).
    texts: int = 0

    @property
    def blocking(self) -> list[Leak]:
        return [leak for leak in self.leaks if leak.blocks]


_DEFAULT_BLOCKING = frozenset({EntityClass.SECRET, EntityClass.CUSTOM, EntityClass.IDENTITY, EntityClass.PII})


def _one_per_value(found: list[tuple[Span, Leak]]) -> list[Leak]:
    """One leak for the findings of one class that are the same text in the same place, named by the strongest of them
    as the masking merge names it (``merge_spans``). The pair of a secret's name and gitleaks' generic rule find one
    key; a request stopped for it names it once (``GENERIC_API_KEY[40]``), not as two values. Allowing that text allows
    all of them. Findings that only overlap stay apart (a key read whole by one rule and by its body by another: the
    page marks the two as one value and allows both), and so do those of different classes (an address found as
    personal data and as a part of a git author are each read by the auto mode's review). The leaks come in the order
    of their first finding."""
    groups: dict[tuple[EntityClass, int, int], list[tuple[Span, Leak]]] = {}
    for span, leak in found:
        groups.setdefault((span.eclass, span.start, span.end), []).append((span, leak))
    out: list[Leak] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0][1])
            continue
        [named] = merge_spans(span for span, _ in members)
        leak = next(leak for span, leak in members if (span.etype, span.rule) == (named.etype, named.rule))
        out.append(replace(leak, allowed=all(other.allowed for _, other in members)))
    return out


class Verifier:
    def __init__(
        self, masker: Masker, block_classes: frozenset[EntityClass] = _DEFAULT_BLOCKING, given: Given | None = None
    ) -> None:
        self.masker = masker
        self.block_classes = block_classes
        # (scope, rewritten, the terms' stamp, block) → (vault version << 1) | configured-policy evidence bit. The stamp
        # (``Masker.stamp``: this process's change of the terms a company syncs, which change in place) is the one read
        # before the block was checked: a block found clean under other terms, or across a change of them, is not found
        # here once they changed, the same terms back included, and is checked whole.
        # The existing bounded integer entry retains evidence on a clean-cache hit; no second cache. The last of the key
        # says a new member's value read as its field (``new_member_fields``), apart from a text read as it is.
        self._clean: LRU[tuple[str, bool, str, bytes, SlotKind, bool], int] = LRU(100_000)
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

    def _written_key(self, session_key: str, text: str, source: str = "") -> bytes:
        named = session_key.encode()
        message = b"W" + source.encode() + len(named).to_bytes(4, "big") + named + text.encode()
        return hmac.new(self.masker.vault.cache_key, message, hashlib.sha256).digest()

    def wrote(self, session_key: str, texts: Iterable[str], *, reported: bool = False) -> None:
        """Retain source-scoped evidence in the existing bounded conversation adapter.

        A bridge report cannot overwrite provider-observed authority, in either event
        order or from another worker. Old unscoped records remain ambiguous.
        """
        now = self.clock()
        for text in texts:
            if not text:
                continue
            key = self._written_key(session_key, text, "r" if reported else "o")
            self._written.put(key, now)
            if self.shared is not None:
                self.shared.remember(key, _WRITTEN)

    def _written_at(self, key: bytes) -> bool:
        told, now = self._written.get(key), self.clock()
        if told is not None:
            if self.shared is not None and now - told >= WRITTEN_KEPT_EVERY:
                self.shared.remember(key, _WRITTEN)
                self._written.put(key, now)
            return True
        if self.shared is None or self.shared.recall(key) != _WRITTEN:
            return False
        self._written.put(key, now)
        return True

    def written_source(self, session_key: str, text: str) -> str | None:
        """Check observed first, including shared evidence newer than a local report."""
        if self._written_at(self._written_key(session_key, text, "o")):
            return "observed"
        if self._written_at(self._written_key(session_key, text, "r")) or self._written_at(
            self._written_key(session_key, text)
        ):
            return "reported"
        return None

    def written(self, session_key: str, text: str) -> bool:
        return self.written_source(session_key, text) is not None

    def _leaks(
        self,
        masker: Masker,
        text: str,
        spans: Iterable[Span],
        kind: SlotKind,
        new: bool,
        review: ReviewPolicy | None = None,
        slot: int = -1,
        authority: set[str] | None = None,
    ) -> list[Leak]:
        """The leaks among ``spans`` found in ``text``. What leaves in clear by the person's choice or by Latet's own
        hand is none: a type your policy lets through (``allow``), whatever path its request takes; a word inside a fake
        (a drawn name may hold one of your terms, or your user name)."""
        spans = list(spans)
        details = review.details(text, spans) if review is not None else [None] * len(spans)
        through = review.let_through(details) if review is not None else [False] * len(spans)
        fakes: list[Span] | None = None
        out: list[tuple[Span, Leak]] = []
        for span, detail, allowed in zip(spans, details, through, strict=True):
            value = span.of(text)
            if span.eclass not in self.block_classes and kind != SlotKind.NEW_MEMBER:
                if authority is not None:
                    authority.add("configured policy")
                continue
            # As written, or as it reads where written in escapes; a term your company locks is never let through.
            if masker.fake_or_let_through(text, span):
                if authority is not None and any(
                    masker.allow.allows(form) for form in (text[span.start : span.end], span.of(text))
                ):
                    authority.add("configured policy")
                continue
            if masker.lets_type_through(span):
                if authority is not None:
                    authority.add("configured policy")
                continue  # your settings let this type through, never for a company's term: the masking left it so
            fakes = masker.surrogates_in(text) if fakes is None else fakes
            if any(f.start <= span.start and span.end <= f.end for f in fakes):
                continue  # a word of a fake (a drawn name holding one of your terms): nothing of yours
            preview = f"{span.etype}[{len(value)}]"
            leak = Leak(
                span.etype, span.eclass, span.rule, kind, preview, new, detail, allowed, slot, span.start, span.end
            )
            out.append((span, leak))
        return _one_per_value(out)

    def verify(
        self,
        slots: Iterable[tuple[str, SlotKind, bool]],
        session_key: str,
        review: ReviewPolicy | None = None,
        scope: Scope | None = None,
        *,
        record: bool = True,
        rewritten: bool = False,
        written: Callable[[str], bool | str | None] | None = None,
        checked_only: Iterable[str] = (),
        new_members: Iterable[str] = (),
        new_member_fields: Iterable[str] = (),
        deadline: float | None = None,
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
        it resumed).

        A request the gateway only checks has the values its texts may hold brought into memory first, in one call to
        the vault's store for all of them, and held while they are checked (an API key's vault: ``Vault.prefetch``): a
        value found once stops it, whatever its age. One the gateway masked had them brought in as it was masked, and
        is checked against memory alone (``Vault.holding(memory_only=True)``), except a shared store: its final
        check observes commits and queries the text's words again under the same outer call's budget.

        ``new_members``: the texts of a request's members no profile declares (the new rule's), each read once, as
        masking reads a person's text, the local name model included: those not found clean before are read by it
        first, at once, in as few runs as hold them, by ``deadline`` (``time.monotonic``; ``NEW_MEMBERS_READ_SECONDS``
        from now when None), as masking reads a document; past it, ``NewMembersUnread`` is raised and nothing is
        checked or recorded. ``new_member_fields``: their values as their fields (``{"<field>": <value>}``), each read
        once by every detector but the name model (the value is among ``new_members``, read by it there), and its value
        by its field too, as masking reads a JSON document's value: a field rule finds it by its key or its path."""
        masker = scope.masker if scope is not None else self.masker
        slots = list(slots)
        members = list(dict.fromkeys(text for text in new_members if text))
        slots.extend((text, SlotKind.NEW_MEMBER, False) for text in members)
        fields_from = len(slots)
        slots.extend((text, SlotKind.NEW_MEMBER, False) for text in dict.fromkeys(new_member_fields) if text)
        record_count = len(slots)
        slots.extend((text, SlotKind.OTHER, False) for text in checked_only)
        ahead = masker.reading_ahead() if members else contextlib.nullcontext()
        with masker.vault.holding(memory_only=rewritten), ahead:
            if not rewritten or getattr(masker.vault, "rechecks_store", False):
                masker.vault.prefetch([text for text, _, _ in slots if text])
            self._read_ahead(masker, members, scope, rewritten, deadline)
            result = self._checked(
                slots,
                session_key,
                review,
                scope,
                record=record,
                rewritten=rewritten,
                written=written,
                record_count=record_count,
                fields_from=fields_from,
            )
        result.texts = len(slots)
        return result

    def _read_ahead(
        self, masker: Masker, texts: list[str], scope: Scope | None, rewritten: bool, deadline: float | None
    ) -> None:
        """``texts`` of new members, those the check will read with the local name model (none found clean before),
        read by it at once (``Masker.read_ahead``), in ``verify``'s ``reading_ahead``: one at a time, a request of a
        thousand distinct values cost minutes of the engine's thread. Past ``deadline``, ``NewMembersUnread``."""
        if not texts or (scope is not None and not scope.enabled) or not masker.reads_ahead(SlotKind.USER_TEXT):
            return
        scope_key, tag = (scope.key if scope is not None else ""), masker.stamp
        unread = [
            text
            for text in texts
            if self._clean.get((scope_key, rewritten, tag, self._key(text), SlotKind.NEW_MEMBER, False)) is None
        ]
        by = time.monotonic() + NEW_MEMBERS_READ_SECONDS if deadline is None else deadline
        if unread and not masker.read_ahead(unread, SlotKind.USER_TEXT, by):
            raise NewMembersUnread(len(unread))

    def _checked(
        self,
        slots: list[tuple[str, SlotKind, bool]],
        session_key: str,
        review: ReviewPolicy | None,
        scope: Scope | None,
        *,
        record: bool,
        rewritten: bool,
        written: Callable[[str], bool | str | None] | None,
        record_count: int,
        fields_from: int,
    ) -> VerifyResult:
        """``verify``'s check of ``slots``, the values they may hold in memory: those from ``fields_from`` to
        ``record_count`` new members' values as their fields (``new_member_fields``)."""
        result = VerifyResult()
        masker = scope.masker if scope is not None else self.masker
        scope_key = scope.key if scope is not None else ""
        version = masker.vault.version
        # Read once, before any detection: a sync while the blocks are read never has one kept as clean under terms it
        # was not checked with (another conversation would skip it), nor under a tag that comes back (the stamp never
        # does: the re-review's R1).
        tag = masker.stamp
        unsent: list[bytes] = []
        # A new member's values found, each said once of its type, whatever texts of the new rule hold it (a key with
        # its value, the value as its fields): (its type, the value, whether it stops the request).
        said: set[tuple[str, str, bool]] = set()
        for slot, (text, kind, from_model) in enumerate(slots):
            if not text or (rewritten and from_model):
                continue
            key = self._key(text)
            new = slot >= record_count or not self.given.checked(session_key, key)
            if new and slot < record_count:
                unsent.append(key)
                if kind == SlotKind.TOOL_RESULT and text.startswith((WITHHELD_DUMP, WITHHELD_UNREAD)):
                    result.withheld += 1
            if scope is not None and not scope.enabled:
                continue  # it leaves as it is: sent all the same
            field = fields_from <= slot < record_count
            cache = (scope_key, rewritten, tag, key, kind, field)
            clean_at = self._clean.get(cache)
            authority = {"configured policy"} if clean_at is not None and clean_at & 1 else set()
            result.authority.update(authority)
            if clean_at is not None and clean_at >> 1 == version:
                result.skipped += 1
                continue
            result.checked += 1
            # A new member is read as a person's text, names included, and every class of what it holds blocks.
            reads = SlotKind.USER_TEXT if kind == SlotKind.NEW_MEMBER else kind
            if clean_at is not None:
                # Clean before: only values learnt since then can make it dirty.
                known, ctx = masker._known_values(text), DetectContext(reads)
                spans = read_escaped(text, lambda t, k=known, c=ctx: list(k.detect(t, c)))
            else:
                # Names found by the local model never block (NER is best effort): no need to run it here. A new
                # member's value as its field is read by it as the value itself (``new_members``).
                skip = ("ner",) if field or reads is kind else ()
                spans = masker.detect(text, DetectContext(reads), skip=skip)
            found = [(leak, text) for leak in self._leaks(masker, text, spans, kind, new, review, slot, authority)]
            if field and clean_at is None and (pair := _field(text)) is not None:
                # Its value by its field, as masking reads a JSON document's: a field rule names it by its key or its
                # path (``items[].sku``, which no text of a JSON object shows the fields detector).
                name, value = pair
                spans = masker.detect(value, DetectContext(reads, field=name), skip=("ner",))
                found += [
                    (leak, value) for leak in self._leaks(masker, value, spans, kind, new, review, slot, authority)
                ]
            leaks = [leak for leak, _ in found]
            if kind == SlotKind.NEW_MEMBER:
                leaks = []
                for leak, source in found:
                    mark = (leak.etype, source[leak.start : leak.end], leak.blocks)
                    if mark not in said:
                        said.add(mark)
                        leaks.append(leak)
            result.authority.update(authority)
            if found and from_model and written is not None and (source := written(text)):
                if source != "observed":
                    result.rules.add("reported as written by the model")
                else:
                    result.authority.add("provider-observed text")
                continue  # the model's own words, as it wrote them; not kept as clean: in a client's text it is not
            if found:  # never kept as clean, a value said of another text of the request included
                if any(leak.allowed for leak, _ in found):
                    result.authority.add("auto-mode identity detail")
                if any(not leak.new and not leak.allowed for leak, _ in found):
                    result.rules.add("sent before in this conversation")
                result.leaks.extend(leaks)
            else:
                self._clean.put(cache, (version << 1) | bool(authority))
        if record and not result.blocking:
            # Only a request that leaves exposes its blocks: a blocked one keeps blocking when resent.
            self.given.keep_checked(session_key, unsent)
        return result
