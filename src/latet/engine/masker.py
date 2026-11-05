"""Masking of one text: detect, merge, replace each span with its surrogate."""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import logging
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import accumulate

import re2

from latet.engine.detectors.base import DetectContext, Detector, DetectorSkipped, quiet_options
from latet.engine.detectors.fields import FieldDetector
from latet.engine.detectors.terms import (
    VALUE_LONGEST,
    KnownValuesDetector,
    NotPlain,
    PlainTerms,
    has_letter_or_digit,
    long_enough,
)
from latet.engine.escapes import read_escaped
from latet.engine.known import Matched, PartedValues
from latet.engine.merge import merge_spans
from latet.engine.modules import AGAIN, module_of
from latet.engine.surrogates.data.names import PUBLIC_MAIL_DOMAINS
from latet.engine.surrogates.generators import Drbg, email_domain
from latet.engine.types import EntityClass, MaskedText, Policy, Replacement, SlotKind, Span
from latet.engine.vault import MaskingError, Vault
from latet.logline import Paced


@dataclass(frozen=True, slots=True)
class ExtraValue:
    """A value to mask besides what the detectors find: one the local reviewer reported."""

    value: str
    etype: str
    eclass: EntityClass
    rule: str


log = logging.getLogger(__name__)

# Whose values the vault keeps whatever their type's policy: the local reviewer's and yours (in the Live Reviewer). None
# longer than ``VALUE_LONGEST`` is kept (``detectors.terms``, beside the terms' own bound, where the settings read it).
_TAUGHT = frozenset({"reviewer", "live"})


# Never masked, whatever finds them: Latet's own name, the tool the agent talks about (its commands, its notices), never
# a value of yours.
_ALWAYS = frozenset({"latet"})

# The detectors of whole values, as given or kept (your terms, your identity, the vault's values): what reads a text
# again as its escapes read, whatever they write (``Masker.detect``).
_WHOLE_VALUES = frozenset({"terms", "identity", "known-values"})

# An applications call's texts read as one text (``Masker.joined``): joined by a blank line, which ends every structure
# a detector reads over lines (a CSV's rows, a table's, a key's block), in parts of at most this many characters.
JOIN = "\n\n"
JOINED_CHARS = 262_144
# What ``str.splitlines`` breaks a line at: a text holding one is read alone.
_LINE_BREAK = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")
# What ``_repeats`` reads at most with ``str.find`` (``PlainTerms.strict_spans``), in characters: the text once for
# each value. The detector it would compile instead costs more up to here: for a call's 500 log lines read joined
# (100,000 characters, 650 values) 12 ms to compile and 33 ms to read, where ``str.find`` read them in 18 ms, the same
# spans; at the longest part (``JOINED_CHARS``, 1,063 values, this many characters read) 18 and 66 ms against 70.
_REPEATS_READ = 1 << 28
# How many times a part read joined looks for its repeats again, each time its values masked together named a group the
# vault would keep that none of them named before (``Masker._found``): a secret found in one text, inside an IBAN of
# another, makes the IBAN a secret's group; a third one may grow from that. Past it, the part's texts are read alone.
_REGROUPED = 8


class _GroupsGrowing(Exception):
    """A part read joined whose groups still grew after ``_REGROUPED`` readings of its repeats: read alone instead."""


def _group_keys(text: str, groups: Iterable[Span]) -> set[tuple[str, str, EntityClass]]:
    """Each group as the vault keeps its value, and as a part's repeats look for it (``Masker._repeats``): as written,
    with its type and class (an IBAN's string is a secret's once a secret is masked inside it)."""
    return {
        (written, g.etype, g.eclass)
        for g in groups
        if g.eclass != EntityClass.STRUCT and long_enough(written := text[g.start : g.end], g.etype)
    }


@dataclass(slots=True)
class AllowList:
    """Values the user marked as not sensitive: exact strings (case-insensitive) and regexes."""

    values: set[str] = field(default_factory=set)
    patterns: list[str] = field(default_factory=list)
    _compiled: list[re2._Regexp] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.values = {v.lower() for v in self.values}
        self._compiled = []
        for place, pattern in enumerate(self.patterns, 1):
            try:
                self._compiled.append(re2.compile(pattern, quiet_options()))
            except re2.error:
                # Settings refuse such a pattern (config.py); one held anyway lets nothing through: more is masked. By
                # its place: a pattern is made of the values it lets through.
                log.warning("allow_patterns: entry %d is not a valid RE2 pattern and is left out", place)

    def allows(self, value: str) -> bool:
        lowered = value.lower()
        return lowered in _ALWAYS or lowered in self.values or any(p.fullmatch(value) for p in self._compiled)


@dataclass(slots=True)
class MaskReport:
    """What happened while masking, besides the replacements."""

    degraded: list[str] = field(default_factory=list)  # optional detectors that failed


# What ``spans`` answers for a text of a call read joined: its spans and the rules that found its values (``origin``,
# the joined reading's); None for a text read alone.
_Answer = tuple[list[Span], dict[str, str]]


@dataclass(slots=True)
class _Joined:
    """A call ``Masker.joined`` reads, in one thread: its texts in parts, each part read joined at the first text of it
    asked for, and what each text read so far is answered, taken once; and how many texts were read alone while it was
    open, each of which may have taught the vault a value that a part read before it did not know."""

    kind: SlotKind
    parts: list[list[str]]
    part_of: dict[str, int]  # each text read joined, its part
    answers: dict[str, _Answer | None] = field(default_factory=dict)
    read_at: dict[int, int] = field(default_factory=dict)  # each part read, how many texts were read alone before
    # Each part read, the vault's version as its reading began: what its texts' values were read at (``MaskedText``).
    versions: dict[int, int] = field(default_factory=dict)
    again: set[int] = field(default_factory=set)  # the parts read a second time, or whose reading answered nothing
    asked: set[str] = field(default_factory=set)  # the texts of the parts asked for: answered, or read alone
    taken: set[str] = field(default_factory=set)  # those answered from a reading
    alone: int = 0  # texts read alone while the call is open


class Masker:
    # The other occurrences of a value found in a text (``_repeats``) looked for with ``str.find``; off, by an RE2
    # detector compiled for each text, as 0.10.0 found them (the bench's switch, ``scripts/bench_engine.py``).
    plain_repeats: bool = True
    # An applications call's texts read joined (``joined``); off, each alone, as 0.10.0 read them (the bench's switch).
    joins: bool = True

    def __init__(
        self,
        vault: Vault,
        detectors: Sequence[Detector],
        policies: dict[str, Policy] | None = None,
        allow: AllowList | None = None,
        generators: dict[str, Callable[[str, Drbg], str]] | None = None,
    ) -> None:
        self.vault = vault
        self.detectors = list(detectors)
        self.policies = policies or {}
        self.allow = allow or AllowList()
        # Fakes for the types of the user's own recognizers: masker by masker, as projects have their own.
        self.generators = dict(generators or {})
        self._skips: dict[str, Paced] = {}  # each detector's texts not read, as the log says them
        self._joining = threading.local()  # the call ``joined`` reads, in each thread
        self._unjoined = Paced()  # joined readings that failed, their texts read alone, as the log says them

    def _refresh(self) -> tuple[PartedValues, PartedValues]:
        """The vault's values and their fakes, as detectors, kept with the vault (``Vault.matchers``) for every masker
        of it, a project's included, which match them alike: a process holding many maskers holds one pair. Where the
        vault changed they are made again in part, never every value for a new one (``latet.engine.known``). Those it
        holds whatever the text: a text is read with what it answers for that text (``_matched``)."""
        held = self.vault.matchers.current()
        return held.known, held.echo

    def _matched(self, text: str, pieces: Iterable[str] | None = None) -> Matched:
        """The vault's values and fakes ``text`` is read for (``Matchers.for_text``): every reading of it is read with
        them, asked once. A vault that lacks a value another process keeps (``Vault.lacking``) refuses it with its
        words: only its own detector would find that value, and no text leaves without it. ``pieces``: the texts
        ``text`` is joined of (``joined``)."""
        if (lacking := self.vault.lacking) is not None:
            raise MaskingError(lacking)
        return self.vault.matchers.for_text(text, pieces=pieces)

    def surrogates_in(self, text: str, *, joined: bool = False) -> list[Span]:
        """Where issued surrogates occur in ``text``, as 0.9.8 found them (composed or decomposed alike), and how fakes
        written in escapes read; but a reading that is a value the vault knows too, in either form (the value wins:
        ``Vault.a_value_over_a_reading``), and a fake found where it is written and again where its escapes read, at
        the same place, once. A short fake (``vault.short_fake``, 0.10.0's of a short secret) inside a word too, as a
        conversation's restore puts it back there; in an applications call only where it stands as a whole word, as an
        unmask of the whole vault restores it (``Vault.whole_vault_call``): an id that holds one by chance is no fake
        there. ``joined``: a text joined of lines (``DetectContext.joined``). The fakes ``text`` is read for are those
        the vault answers for it (``Matchers.for_text``)."""
        held = self.vault.matchers.for_text(text)
        echo, inside = held.echo, None if self.vault.whole_vault_call() else held.echo_inside
        ctx = DetectContext(joined=joined)

        def read(t: str) -> list[Span]:
            found = list(echo.detect(t, ctx))
            if inside is not None:
                found = sorted([*found, *inside.detect(t, ctx)], key=lambda s: (s.start, -s.end))
            return found

        spans = read_escaped(text, read)
        held.seen()
        value_wins = self.vault.a_value_over_a_reading
        places: set[tuple[int, int]] = set()
        out = []
        for span in spans:
            if (span.start, span.end) in places:
                continue
            found = text[span.start : span.end]
            if value_wins(_composed_or_not(found, span.value)):
                continue
            places.add((span.start, span.end))
            out.append(span)
        return out

    def known_in(self, text: str) -> list[Span]:
        """Where values in the vault are in ``text`` in clear: neither a surrogate nor a value you let through. The
        values ``text`` is read for are those the vault answers for it (``Matchers.for_text``)."""
        held = self._matched(text)
        known = held.known
        spans = read_escaped(text, lambda t: list(known.detect(t, DetectContext())))
        held.seen()
        return [s for s in spans if not self.fake_or_let_through(text, s)]

    def known_outside(self, text: str, replacements: Iterable[Replacement]) -> bool:
        """Whether a value in the vault is in ``text`` outside every value ``replacements`` masked in it (their masking
        of ``text``, by its places): in clear, or around or across a value masked (an IBAN the vault learnt as a
        secret's group after ``text`` was masked with that secret alone; `Mario Rossi` learnt whole, `Rossi` masked
        alone). Its masking holds a fake where part of such a value was, so it shows no value in clear there
        (``known_in``), and the rest of that value is in clear. A value inside one value masked needs nothing, a fake
        or a value you let through neither; one reading of ``text``, as ``known_in``'s."""
        places = sorted((r.start, r.end) for r in replacements if r.policy != Policy.ALLOW)
        starts = [start for start, _ in places]
        reach = list(accumulate((end for _, end in places), max))  # the furthest end of the places begun up to each
        known = self._known_values(text)
        for span in read_escaped(text, lambda t: list(known.detect(t, DetectContext()))):
            at = bisect.bisect_right(starts, span.start) - 1
            if at >= 0 and span.end <= reach[at]:
                continue  # inside a value masked (the one begun at or before it that reaches furthest)
            if not self.fake_or_let_through(text, span):
                return True
        return False

    def fake_or_let_through(self, text: str, span: Span) -> bool:
        """Whether what ``span`` found is a fake, or a value you let through: as it is written, or as it reads where
        written in escapes (`Mario%20Rossi` is `Mario Rossi`)."""
        forms = {text[span.start : span.end], span.of(text)}
        return any(self.vault.is_surrogate(form) or self.allow.allows(form) for form in forms)

    def json_numbers(self, kind: SlotKind, tool: str | None, path: str | None = None) -> bool:
        """The numbers of a JSON document are masked too here (strict data mode, or field rules); ``path``: where the
        document came from (a file, a web address), when known."""
        ctx = DetectContext(kind, tool=tool, source=path)
        return any(isinstance(d, FieldDetector) and d.numbers_for(ctx) for d in self.detectors)

    def _known_values(self, text: str | None = None) -> PartedValues:
        """The vault's values, as a detector: what every masking and every check reads, those it answers for ``text``
        (``_matched``; none given: those it holds whatever the text). A vault that lacks a value another process keeps
        (``Vault.lacking``) refuses each with its words: only its own detector would find that value, and no text leaves
        without it."""
        if text is not None:
            return self._matched(text).known
        if (lacking := self.vault.lacking) is not None:
            raise MaskingError(lacking)
        known, _ = self._refresh()
        return known

    def detect(
        self,
        text: str,
        ctx: DetectContext,
        report: MaskReport | None = None,
        skip: tuple[str, ...] = (),
        *,
        parts: Sequence[tuple[int, int]] | None = None,
    ) -> list[Span]:
        """What the detectors find in ``text``. JSON text is read again with its escapes' letters blanked out
        (`escapes`), by every detector but the name model, whose reading is the one that costs. A text with escapes
        (`Niccol\\u00f2`, `%40`, `&#242;`, a quote, a form's `+`) is read once more as they read
        (``escapes.decoded``) by your terms, your identity and the vault's values, which match whole values; where an
        escape writes a character, by the detectors of personal data and your recognizers too, whose secrets and
        values of a structure are left out there (a base64 run's `%3D` makes no new key, a `%26` no new field). A
        value found there of a type your policies let through is left out too: it never names what it merges with. A
        value found there is kept as it is written (``_apply``). A value of signs
        alone (``----`` after ``Bearer``, ``!!!!`` as a password) is left out: a fake keeps the signs of a value, so it
        would be its own. The final check reads with this too, so it does not stop a request for one either.
        A Latet route key may consist of URL-safe signs alone; its dedicated generator draws every character,
        so it can still be masked and restored.

        ``parts``: where the texts ``text`` is joined of stand in it (``joined``): each is read as its escapes read on
        its own, by the detectors as they read a text alone (``escapes.read_escaped``).

        The vault's values every reading is read for are those it answers for ``text`` (``Matchers.for_text``, asked
        once), and it is told once the reading is over (``Matched.seen``)."""
        alone = dataclasses.replace(ctx, joined=False) if parts is not None else ctx
        held = self._matched(text, None if parts is None else [text[start:end] for start, end in parts])
        known = held.known

        def deep(t: str, characters: bool) -> list[Span]:
            found = self._detect(t, alone, None, (*skip, "ner"), _WHOLE_VALUES, known)
            if characters:
                others = self._detect(t, alone, None, (*skip, "ner", *_WHOLE_VALUES), known=known)
                found += [s for s in others if s.eclass not in (EntityClass.SECRET, EntityClass.STRUCT)]
            return [s for s in found if self.policies.get(s.etype) != Policy.ALLOW]

        spans = read_escaped(
            text,
            lambda t: self._detect(t, ctx, report, skip, known=known),
            lambda t: self._detect(t, ctx, None, (*skip, "ner"), known=known),
            deep,
            parts,
        )
        held.seen()
        return [s for s in spans if s.etype == "LATET_ROUTE_KEY" or has_letter_or_digit(s.of(text))]

    def _detect(
        self,
        text: str,
        ctx: DetectContext,
        report: MaskReport | None,
        skip: tuple[str, ...],
        only: frozenset[str] | None = None,
        known: PartedValues | None = None,
    ) -> list[Span]:
        """What the detectors find in ``text`` (one reading of a text), and the vault's values ``known`` (those the
        vault answers for the text, ``_matched``; none given: those it holds whatever the text)."""
        spans: list[Span] = []
        for detector in [*self.detectors, known if known is not None else self._known_values()]:
            if detector.id in skip or (only is not None and detector.id not in only):
                continue
            try:
                spans.extend(detector.detect(text, ctx))
            except DetectorSkipped as exc:
                self._skipped(detector.id, str(exc))
                if report is not None:
                    report.degraded.append(detector.id)
            except Exception as exc:
                if detector.mandatory and isinstance(exc, MaskingError):
                    raise  # a refusal in Latet's own words, which name no value (a space past its share)
                if detector.mandatory:
                    raise MaskingError(f"detector {detector.id} failed") from exc
                # By its type: an error raised while a text is read can quote it, and this goes to the daemon's log.
                log.warning("optional detector %s failed: %s", detector.id, type(exc).__name__)
                if report is not None:
                    report.degraded.append(detector.id)
        return spans

    @contextlib.contextmanager
    def reading_ahead(self) -> Iterator[None]:
        """While it is open, what the detectors that read ahead (the local name model) read in this thread is kept for
        the texts this thread masks then (``read_ahead``); forgotten when it closes."""
        with contextlib.ExitStack() as stack:
            for detector in self.detectors:
                reading = getattr(detector, "reading", None)
                if reading is not None:
                    stack.enter_context(reading())
            yield

    def reads_ahead(self, kind: SlotKind) -> bool:
        """Whether a detector reads texts of ``kind`` ahead (the local name model, where it reads that kind)."""
        return any(hasattr(d, "read_ahead") and kind in getattr(d, "kinds", ()) for d in self.detectors)

    def read_ahead(self, texts: Sequence[str], kind: SlotKind, deadline: float) -> bool:
        """``texts`` of ``kind`` read ahead, at once, by the detectors that can (``reading_ahead``), before they are
        masked one by one. False when ``deadline`` (``time.monotonic``) passed before they were all read."""
        readers = [read for d in self.detectors if (read := getattr(d, "read_ahead", None)) is not None]
        return all(read(texts, kind, deadline) for read in readers)

    @contextlib.contextmanager
    def joined(self, texts: Sequence[str], kind: SlotKind, report: MaskReport | None = None) -> Iterator[None]:
        """While it is open, in this thread, ``spans`` asked for one of ``texts`` of ``kind`` as an applications call
        asks (no path, tool or field, nor ``from_model``, ``keep_surrogates`` or an extra value; a report or none)
        answers, once, what it found for that text in the call's texts read as one: those of them that hold no line
        break (``str.splitlines``'), joined by a blank line (``JOIN``) in parts of at most ``JOINED_CHARS`` characters,
        each part read at the first of its texts asked for, its spans given back to each text by their places, with the
        rules its values were found by (the part's). The detectors that read a pattern over lines read a part line by
        line (``DetectContext.joined``), as each text alone; each text's escapes read on its own (``detect``). Each
        detector reads a part's lines in one pass, where 0.10.0 read them one by one.

        A text is read alone, as 0.10.0 read each: one that holds a line break; one a detector reads with the lines
        around it (``apart``: an HTML table's rows, a snapshot's cells); one longer than a part; each one that a span
        found in its part reaches past its own end (a value read across the blank line, which no text holds alone);
        each one of a part whose reading failed, or ran weaker (an optional detector that failed on it, or did not read
        it); and every one asked for again (a text a later one made stale, ``settle``). Nothing is joined
        where a name model reads ``kind`` (``reads_ahead``: it reads a text as a whole), nor where a detector cannot
        read a part line by line (``joinable``: your recognizer of a quoted run, say), nor with the switch off
        (``joins``).

        A text read alone teaches the vault what it finds there, which a later text may hold bare: read alone, that
        text finds it in the vault. So a part never runs past a text read alone (its texts are those between two, in
        the call's order); and where a text is read alone after a part was read (one a span across split, one asked out
        of order), the part's texts not asked for yet are read again, joined, once, then each alone.

        ``report`` is accepted and left unused (the plan's interface keeps it): the joined reading reports to a report
        of its own, and one that ran weaker answers none of its texts, each of which, read alone, says what ran weaker
        on it in the report its masking is given, as it always did."""
        parts = self._parts(texts, kind)
        if not parts:
            yield
            return
        before = getattr(self._joining, "call", None)
        self._joining.call = _Joined(kind, parts, {text: k for k, part in enumerate(parts) for text in part})
        try:
            yield
        finally:
            self._joining.call = before

    def _parts(self, texts: Sequence[str], kind: SlotKind) -> list[list[str]]:
        """``texts`` that ``joined`` reads joined, each once, in parts of two or more (one alone is read alone), each
        of texts next to each other in the call's order, but for the empty ones and those asked before (a text asked
        again teaches the vault nothing new): a text read alone ends a part. None where nothing is joined."""
        if not self.joins or self.reads_ahead(kind) or not all(getattr(d, "joinable", True) for d in self.detectors):
            return []
        apart = [check for d in self.detectors if (check := getattr(d, "apart", None)) is not None]
        parts: list[list[str]] = []
        part: list[str] = []
        size = 0
        seen: set[str] = set()
        for text in texts:
            if not text or text in seen:
                continue
            seen.add(text)
            alone = len(text) > JOINED_CHARS or _LINE_BREAK.search(text) or any(check(text) for check in apart)
            if part and (alone or size + len(JOIN) + len(text) > JOINED_CHARS):
                parts.append(part)
                part, size = [], 0
            if alone:
                continue
            size += (len(JOIN) if part else 0) + len(text)
            part.append(text)
        if part:
            parts.append(part)
        return [part for part in parts if len(part) > 1]

    def _answer(self, call: _Joined, text: str) -> tuple[_Answer, int] | None:
        """``text``'s answer in ``call``, taken once, and the vault's version its part's reading began at; its part read
        first where it was not, and read again (once) where a text was read alone since; None: it is read alone."""
        k = call.part_of.get(text)
        if k is None or text in call.asked:
            return None
        call.asked.add(text)
        if k not in call.read_at or (call.alone > call.read_at[k] and k not in call.again):
            if k in call.read_at:
                call.again.add(k)  # what a text read alone since taught the vault: the rest read again, once
            rest = [t for t in call.parts[k] if t not in call.asked or t == text]
            call.read_at[k] = call.alone
            call.versions[k] = self.vault.version  # first: a value learnt while it is read has its texts checked again
            call.answers.update(dict.fromkeys(rest))
            if len(rest) > 1:
                read = self._read_joined(rest, call.kind)
                if read is None:
                    call.again.add(k)  # failed, or ran weaker: each read alone, never again joined
                else:
                    call.answers.update(read)
        elif call.alone > call.read_at[k]:
            return None  # read twice already, and a text was read alone since: alone
        answer = call.answers.pop(text, None)
        if answer is None:
            return None
        call.taken.add(text)
        return answer, call.versions[k]

    def _read_joined(self, texts: list[str], kind: SlotKind) -> dict[str, _Answer | None] | None:
        """Each of ``texts`` (a part) and its answer, the part read as one text (``joined``): None for a text a span
        found there reaches past its own end or before its start. None for them all where the reading failed or ran
        weaker; but a masking failure (``MaskingError``: in Latet's own words, a vault that lacks values, a mandatory
        detector that failed) fails the call, as it fails each text."""
        starts = list(accumulate((len(t) + len(JOIN) for t in texts[:-1]), initial=0))
        ends = [start + len(t) for start, t in zip(starts, texts, strict=True)]
        text = JOIN.join(texts)
        weaker = MaskReport()
        try:
            found, origin, seen = self._found(
                text, DetectContext(kind, joined=True), weaker, False, (), parts=list(zip(starts, ends, strict=True))
            )
        except MaskingError:
            raise
        except Exception as exc:  # by its type: its words can quote a text; each text read alone meets it, or not
            if count := self._unjoined.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.info("a call's texts read joined failed (%s), and were read alone%s", type(exc).__name__, many)
            return None
        if weaker.degraded:
            return None
        alone: set[int] = set()
        for span in (*seen, *found):
            k = bisect.bisect_right(starts, span.start) - 1
            if span.start < ends[k] and span.end <= ends[k]:
                continue
            # Past its text, into the blank line and those after it: every text it reaches, the next one at least.
            last = bisect.bisect_left(starts, span.end) - 1
            alone.update(range(k, min(max(last, k + 1), len(texts) - 1) + 1))
        answers: list[list[Span]] = [[] for _ in texts]
        for span in found:
            k = bisect.bisect_right(starts, span.start) - 1
            if k not in alone:
                answers[k].append(dataclasses.replace(span, start=span.start - starts[k], end=span.end - starts[k]))
        return {t: None if k in alone else (answers[k], origin) for k, t in enumerate(texts)}

    def _skipped(self, detector: str, why: str) -> None:
        """A detector that did not read a text, in the log: its first time at once, then once a minute with how many,
        since a client sends as many texts as it likes (the reason, fixed words, is the last one's)."""
        paced = self._skips.get(detector) or self._skips.setdefault(detector, Paced())
        count = paced.due()
        if count == 1:
            log.info("%s did not read a text: %s", detector, why)
        elif count > 1:
            log.info("%s did not read a text: %s (%d since the line before)", detector, why, count)

    def mask(
        self,
        text: str,
        kind: SlotKind = SlotKind.OTHER,
        path: str | None = None,
        from_model: bool = False,
        report: MaskReport | None = None,
        keep_surrogates: bool = False,
        tool: str | None = None,
        field: str | None = None,
        extra: Sequence[ExtraValue] = (),
        context: str = "",
    ) -> MaskedText:
        """Mask ``text``.

        ``from_model`` marks text the model produced (it may carry surrogates). ``keep_surrogates``
        makes masking idempotent (client mode, where one content can pass through several hooks):
        surrogates already in the text stay as they are instead of being masked again. ``context``: the text around
        ``text``, which a fake drawn here must not hold, as it must not hold ``text`` (``Vault.intern``).

        A text of a call read joined (``joined``) says the vault's version its part was read at
        (``MaskedText.read_at``): a value another thread taught the vault after that, before it was masked, is not in
        its answer, and a cache that keeps it checks it again once the vault has moved past that version
        (``Engine.mask_text``).
        """
        if not text:
            return MaskedText(text)
        spans, origin, read_at = self._read(text, kind, path, from_model, report, keep_surrogates, tool, field, extra)
        masked = self._apply(text, spans, origin, context)
        if read_at is not None:
            masked.read_at = read_at
        return masked

    def spans(
        self,
        text: str,
        kind: SlotKind = SlotKind.OTHER,
        path: str | None = None,
        from_model: bool = False,
        report: MaskReport | None = None,
        keep_surrogates: bool = False,
        tool: str | None = None,
        field: str | None = None,
        extra: Sequence[ExtraValue] = (),
    ) -> tuple[list[Span], dict[str, str]]:
        """What ``mask`` replaces in ``text``: the values found, merged into disjoint spans in the order of the text
        (``_merged``: a group named by a value of a type let through gives way to the masked values inside it), and the
        rule each value was found by (a repeat of a value is kept by the rule that found it first). The vault learns
        nothing here: ``mask`` keeps the values (``_apply``). The arguments are ``mask``'s. A text of a call read
        joined (``joined``) is answered from there, once."""
        spans, origin, _ = self._read(text, kind, path, from_model, report, keep_surrogates, tool, field, extra)
        return spans, origin

    def _read(
        self,
        text: str,
        kind: SlotKind,
        path: str | None,
        from_model: bool,
        report: MaskReport | None,
        keep_surrogates: bool,
        tool: str | None,
        field: str | None,
        extra: Sequence[ExtraValue],
    ) -> tuple[list[Span], dict[str, str], int | None]:
        """``spans``' answer, and the vault's version it was read at where that is a part's reading (``joined``), else
        None (read now)."""
        if not text:
            return [], {}, None
        call = getattr(self._joining, "call", None)
        if call is not None:
            if (
                call.kind == kind
                and path is None
                and tool is None
                and field is None
                and not (from_model or keep_surrogates or extra)
            ):
                answer = self._answer(call, text)
                if answer is not None:
                    (found, origin), version = answer
                    return found, origin, version
            if text not in call.taken:
                call.alone += 1  # read alone: what it finds, a part read before it does not know (``joined``)
        ctx = DetectContext(kind, tool=tool, field=field, source=path)
        found, origin, _ = self._found(text, ctx, report, from_model or keep_surrogates, extra)
        return found, origin, None

    def _found(
        self,
        text: str,
        ctx: DetectContext,
        report: MaskReport | None,
        skip_surrogates: bool,
        extra: Sequence[ExtraValue],
        parts: Sequence[tuple[int, int]] | None = None,
    ) -> tuple[list[Span], dict[str, str], list[Span]]:
        """``spans``' answer for ``text``, and every span it weighed on the way (each value found, each repeat):
        ``parts``, where the texts ``text`` is joined of stand (``joined``)."""
        spans = self.detect(text, ctx, report, parts=parts)
        spans.extend(self._echoes(text, skip_surrogates, ctx.joined))
        spans.extend(_extra_spans(text, extra))
        kept = []
        for span in spans:
            forms = {text[span.start : span.end], span.of(text)}  # as written, and as it reads
            if any(self.allow.allows(form) for form in forms):
                continue
            if skip_surrogates and any(self.vault.is_surrogate(form) for form in forms):
                continue  # already in surrogate space
            kept.append(span)
        # The rule that found each value, for the vault: a later occurrence of it (a repeat) may come first.
        origin: dict[str, str] = {}
        for span in kept:
            if span.rule not in AGAIN:
                origin.setdefault(text[span.start : span.end], span.rule)
                origin.setdefault(span.of(text), span.rule)
        # A value let through stays whole: no other value's repeat is masked inside it.
        allowed = [
            (s.start, s.end) for s in spans if any(self.allow.allows(f) for f in (text[s.start : s.end], s.of(text)))
        ]
        # In a call read joined, every text's values are looked for in every other text of its part too: what each text
        # alone finds in the vault (a value an earlier text taught it) or masks again (``settle``, for a later one).
        groups = self._merged(text, kept) if ctx.joined else None
        repeats = list(self._repeats(text, kept, origin, ctx.joined, groups))

        def masked_with(found: list[Span]) -> list[Span]:
            out = [*kept, *(r for r in found if not any(a < r.end and r.start < b for a, b in allowed))]
            # A value of a type your policies let through, found where escapes read as what they stand for (a repeat,
            # the reviewer's), never names what it merges with: ``detect`` leaves such a finding out, and so does every
            # path.
            return [s for s in out if s.value is None or self.policies.get(s.etype) != Policy.ALLOW]

        merged = self._merged(text, masked_with(repeats))
        if groups is not None:
            # A group the repeats formed, a secret repeated inside an IBAN of another text: the vault keeps the IBAN as
            # a secret, which a later text read alone finds glued too. Looked for again, as the vault keeps it, until
            # the groups stop growing (the integration review).
            looked = _group_keys(text, groups)
            for _ in range(_REGROUPED):
                grown = _group_keys(text, merged) - looked
                if not grown:
                    break
                looked |= grown
                repeats = list(self._repeats(text, [*kept, *repeats], origin, True, merged))
                merged = self._merged(text, masked_with(repeats))
            else:
                if _group_keys(text, merged) - looked:
                    raise _GroupsGrowing
        return merged, origin, [*spans, *repeats]

    def findings(self, text: str, kind: SlotKind = SlotKind.OTHER, *, tool: str | None = None) -> list[Span]:
        """What masking ``text`` would replace, without masking it: the spans ``mask`` replaces, at the same places and
        as the same types, those of a type let through (its policy ``allow``) left out. The fakes the vault issued count
        as values, as ``mask`` counts them when it does not keep them (``keep_surrogates`` off). Nothing is kept: the
        vault learns no value, and its version stays."""
        spans, _ = self.spans(text, kind, tool=tool, keep_surrogates=False)
        return [s for s in spans if self.policies.get(s.etype, Policy.MASK) != Policy.ALLOW]

    def _merged(self, text: str, spans: list[Span]) -> list[Span]:
        """``spans`` merged (``merge_spans``: a group of values that overlap is named by its strongest, and masked
        whole). A group named by a value of a type your policies let through, which ``_apply`` leaves in clear, leaves
        in clear that value and nothing more: a value of a type that is masked in it (an IBAN, a card number, a value
        the vault knows, written in an address let through) is masked where it sits, the rest as written; 0.9.9 let the
        whole group through. There an address keeps its own domain, which it brings along (``_repeats``): a domain
        after its `@` that it outranks (a repeat of it, one the vault knows) is the address's. A group named by a value
        that is masked (a secret, a term of yours, one typed DOMAIN among them) is masked whole, as before, a value let
        through in it included."""
        whole = _whole_addresses(spans)
        masked = [s for s in whole if self.policies.get(s.etype) != Policy.ALLOW]
        own = _own_domains(text, [s for s in whole if self.policies.get(s.etype) == Policy.ALLOW])
        out: list[Span] = []
        for group in merge_spans(whole, text):
            if self.policies.get(group.etype) != Policy.ALLOW:
                out.append(group)
                continue
            inside = [s for s in masked if group.start <= s.start and s.end <= group.end and not _inside(s, own)]
            out.extend(merge_spans(inside, text))
        return out

    def _repeats(
        self,
        text: str,
        spans: list[Span],
        origin: dict[str, str],
        joined: bool = False,
        groups: list[Span] | None = None,
    ) -> Iterable[Span]:
        """Every other occurrence, in the same text, of a value detected once (e.g. in a comment).

        Not for the values of a structure (STRUCT): each one is found in its field, and the same
        word out of it is not the field's value. An e-mail address, written with `@` or out in words,
        brings its domain along (a company's, not a mail provider's), even one that is the value of
        a field typed EMAIL: `api.acme-intra.it` next to `ops@acme-intra.it` or
        `ops at acme-intra dot it` is masked in this text as it is in every later one, where the
        domain is a value already in the vault.

        Found as ``KnownValuesDetector`` finds them, with ``str.find`` (``PlainTerms.strict_spans``): an RE2 detector
        compiled for the few values of one text cost more than reading it, and for the many of a call's lines read
        joined too, up to ``_REPEATS_READ``. A reading of the text it does not answer for (``NotPlain``: a value not
        ASCII found, two found that overlap, a long text of many values) is read by that detector, made once, as
        before; in a text joined of lines (``joined``), each line within its own bound on overlaps, as each text alone.

        In a text joined of lines, each value is looked for as the vault keeps it too (``_apply``): as written, named
        as the group of values that overlap it is masked in is named (``_merged``), and found so as it reads there. A
        later text of the call read alone finds it so in the vault: an address found where escapes read as what they
        stand for (`&#242;Mario.neri%40acme.it`), in a text whose own escapes read otherwise
        (`1234&#64;Mario.neri%40acme.it`, its escapes a log's bytes); an IBAN or a phone number that holds a secret the
        vault knows (`1234`), kept as a secret, inside a word (`Corp12347IT60X…`) as a secret is found. Read joined, the
        part's other texts find it as their repeat, the same value with the same fake. ``groups``: ``spans`` merged,
        where the caller has them (``_found``, which looks again for the groups the repeats formed).
        """
        values = {s.of(text): s for s in spans if long_enough(s.of(text), s.etype) and s.eclass != EntityClass.STRUCT}
        entries = [(v, s.etype, s.eclass) for v, s in values.items()]
        addresses = [(s.of(text), s) for s in spans if s.eclass == EntityClass.STRUCT and s.etype == "EMAIL"]
        for value, span in [*values.items(), *addresses]:
            domain = email_domain(value).lower() if span.etype == "EMAIL" else ""
            if len(domain) >= 4 and domain not in PUBLIC_MAIL_DOMAINS and domain not in values:
                entries.append((domain, "DOMAIN", EntityClass.PII))
                origin.setdefault(domain, span.rule)
        # In a text joined of lines, each value as the vault keeps it, its group: looked for as written, with the
        # group's type and class (last: a string is named as the vault keeps it); and a group that reads through
        # escapes, by the string as written, which a repeat of it found so reads as.
        kept_as: dict[str, Span] = {}
        if joined:
            named = set(entries)
            for group in groups if groups is not None else self._merged(text, spans):
                written = text[group.start : group.end]
                if group.eclass == EntityClass.STRUCT or not long_enough(written, group.etype):
                    continue
                if group.value is not None:
                    kept_as.setdefault(written, group)
                if (key := (written, group.etype, group.eclass)) not in named:
                    named.add(key)
                    entries.append(key)
        if not entries:
            return []
        ctx = DetectContext(joined=joined)
        if not self.plain_repeats:
            detector = KnownValuesDetector(entries, rule="repeat")
            found = read_escaped(text, lambda t: list(detector.detect(t, ctx)))
        else:
            plain = PlainTerms(entries, rule="repeat")
            compiled: list[KnownValuesDetector] = []

            def read(reading: str) -> list[Span]:
                try:
                    return plain.strict_spans(reading, _REPEATS_READ)
                except NotPlain:
                    if not compiled:
                        compiled.append(KnownValuesDetector(entries, rule="repeat"))
                    return list(compiled[0].detect(reading, ctx))

            found = read_escaped(text, read)
        if not kept_as:
            return found
        return [
            dataclasses.replace(s, value=kept_as[w].value)
            if s.value is None and (w := text[s.start : s.end]) in kept_as
            else s
            for s in found
        ]

    def _echoes(self, text: str, from_model: bool, joined: bool = False) -> Iterable[Span]:
        """Issued surrogates found in client text become new originals of their own.

        A surrogate the client sends back (pasted, or written to a file earlier) must not be confused
        with the value it stands for: masking it again keeps every restore unambiguous. ``joined``: a text joined of
        lines (``DetectContext.joined``).
        """
        if from_model:
            return []
        return self.surrogates_in(text, joined=joined)

    def _apply(
        self, text: str, spans: list[Span], origin: dict[str, str] | None = None, context: str = ""
    ) -> MaskedText:
        around = f"{text}\n{context}" if context else text  # what a fake drawn here must not hold
        pieces: list[str] = []
        replacements: list[Replacement] = []
        cursor = 0
        for span in spans:
            # Kept as it is written, escapes and all, so that it comes back exactly: what it reads (``Span.value``,
            # where written in escapes) is what its fake is drawn from, written in the same escapes (``Vault.intern``).
            original, reads = text[span.start : span.end], span.value
            policy = self.policies.get(span.etype, Policy.MASK)
            if policy == Policy.ALLOW:
                continue
            kept = len(original) <= VALUE_LONGEST
            if not kept:
                policy = Policy.REDACT  # too long for a vault to keep: redacted, never kept nor restored
            if policy == Policy.REDACT:
                if module_of(span.rule) in _TAUGHT and kept:
                    # Found by you or the local reviewer: in the vault all the same, with a fake of its own that never
                    # leaves, so that it is found again wherever it appears and the checks of what leaves know it.
                    self.vault.intern(
                        span.etype, span.eclass, original, context=around, rule=span.rule,
                        generate=self.generators.get(span.etype), reads=reads,
                    )  # fmt: skip
                replacement, entity_id = f"[REDACTED:{span.etype}]", None
            else:
                rule = (origin or {}).get(original, span.rule) if span.rule in AGAIN else span.rule
                entity = self.vault.intern(
                    span.etype,
                    span.eclass,
                    original,
                    context=around,
                    rule=rule,
                    generate=self.generators.get(span.etype),
                    reads=reads,
                )
                replacement, entity_id = entity.surrogate, entity.id
            pieces.append(text[cursor : span.start])
            pieces.append(replacement)
            cursor = span.end
            replacements.append(
                Replacement(
                    span.start, span.end, original, replacement, span.etype, span.eclass, span.rule, policy, entity_id
                )
            )
        if not replacements:
            return MaskedText(text)
        pieces.append(text[cursor:])
        return MaskedText("".join(pieces), replacements)


def _composed_or_not(found: str, reads: str | None) -> set[str]:
    """How a text found as a fake is written and how it reads, each composed and decomposed too: the fakes and the
    values are matched in both forms, and the vault keeps each in one."""
    forms = {found} if reads is None else {found, reads}
    return forms | {unicodedata.normalize(how, form) for form in forms if not form.isascii() for how in ("NFC", "NFD")}


def _own_domains(text: str, through: list[Span]) -> list[tuple[int, int]]:
    """Where the domain of each address of ``through`` (let through) is written, after its last `@`: the address
    brings it along (``Masker._repeats``), and there it stays the address's, found as a repeat or as a value the vault
    knows."""
    found = []
    for span in through:
        if span.etype == "EMAIL" and (at := text.rfind("@", span.start, span.end)) >= 0:
            found.append((at + 1, span.end))
    return found


def _inside(span: Span, domains: list[tuple[int, int]]) -> bool:
    """Whether ``span`` is a domain written in one of ``domains`` (``_own_domains``)."""
    return span.etype == "DOMAIN" and any(start <= span.start and span.end <= end for start, end in domains)


def _whole_addresses(spans: list[Span]) -> list[Span]:
    """``spans`` without the domains found in an address of a field typed EMAIL (STRUCT): its own, which it brings
    along and a later text finds in the vault. `merge_spans` would cut the address around them, as it cuts a value of
    a structure around what is found in it, and the address's fake has the domain's fake in it already."""
    addresses = sorted((s.start, s.end) for s in spans if s.etype == "EMAIL" and s.eclass == EntityClass.STRUCT)
    if not addresses:
        return spans
    starts = [start for start, _ in addresses]
    reach = list(accumulate((end for _, end in addresses), max))  # the furthest end of the addresses so far

    def inside(span: Span) -> bool:
        i = bisect.bisect_right(starts, span.start) - 1
        return i >= 0 and span.end <= reach[i]

    return [s for s in spans if s.etype != "DOMAIN" or not inside(s)]


def _extra_spans(text: str, extra: Sequence[ExtraValue]) -> list[Span]:
    """Every occurrence of the reviewer's values (whole words, but for secrets)."""
    if not extra:
        return []
    by_rule: dict[str, list[tuple[str, str, EntityClass]]] = {}
    for item in extra:
        by_rule.setdefault(item.rule, []).append((item.value, item.etype, item.eclass))
    spans: list[Span] = []
    for rule, values in by_rule.items():
        detector = KnownValuesDetector(values, min_length=2, rule=rule)
        spans += read_escaped(text, lambda t, d=detector: list(d.detect(t, DetectContext())))
    return spans


def strongest_class(replacements: Iterable[Replacement]) -> EntityClass | None:
    classes = [r.eclass for r in replacements]
    return max(classes) if classes else None
