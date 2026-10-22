"""Masking of one text: detect, merge, replace each span with its surrogate."""

from __future__ import annotations

import bisect
import contextlib
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import accumulate

import re2

from latet.engine.detectors.base import DetectContext, Detector, DetectorSkipped, quiet_options
from latet.engine.detectors.fields import FieldDetector
from latet.engine.detectors.terms import VALUE_LONGEST, KnownValuesDetector, has_letter_or_digit, long_enough
from latet.engine.escapes import read_escaped
from latet.engine.known import PartedValues
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


class Masker:
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

    def _refresh(self) -> tuple[PartedValues, PartedValues]:
        """The vault's values and their fakes, as detectors, kept with the vault (``Vault.matchers``) for every masker
        of it, a project's included, which match them alike: a process holding many maskers holds one pair. Where the
        vault changed they are made again in part, never every value for a new one (``latet.engine.known``). The
        engine's thread, the plugin's and the final check read through them."""
        held = self.vault.matchers.current()
        return held.known, held.echo

    def surrogates_in(self, text: str) -> list[Span]:
        """Where issued surrogates occur in ``text``."""
        _, echo = self._refresh()
        return read_escaped(text, lambda t: list(echo.detect(t, DetectContext())))

    def known_in(self, text: str) -> list[Span]:
        """Where values in the vault are in ``text`` in clear: neither a surrogate nor a value you let through."""
        known = self._known_values()
        spans = read_escaped(text, lambda t: list(known.detect(t, DetectContext())))
        clear = [(s, text[s.start : s.end]) for s in spans]
        return [s for s, value in clear if not self.vault.is_surrogate(value) and not self.allow.allows(value)]

    def json_numbers(self, kind: SlotKind, tool: str | None, path: str | None = None) -> bool:
        """The numbers of a JSON document are masked too here (strict data mode, or field rules); ``path``: where the
        document came from (a file, a web address), when known."""
        ctx = DetectContext(kind, tool=tool, source=path)
        return any(isinstance(d, FieldDetector) and d.numbers_for(ctx) for d in self.detectors)

    def _known_values(self) -> PartedValues:
        """The vault's values, as a detector: what every masking and every check reads. A vault that lacks a value
        another process keeps (``Vault.lacking``) refuses each with its words: only its own detector would find that
        value, and no text leaves without it."""
        if (lacking := self.vault.lacking) is not None:
            raise MaskingError(lacking)
        known, _ = self._refresh()
        return known

    def detect(
        self, text: str, ctx: DetectContext, report: MaskReport | None = None, skip: tuple[str, ...] = ()
    ) -> list[Span]:
        """What the detectors find in ``text``. JSON text is read again with its escapes' letters blanked out
        (`escapes`), by every detector but the name model, whose reading is the one that costs. A value of signs
        alone (``----`` after ``Bearer``, ``!!!!`` as a password) is left out: a fake keeps the signs of a value, so it
        would be its own. The final check reads with this too, so it does not stop a request for one either.
        A Latet route key may consist of URL-safe signs alone; its dedicated generator draws every character,
        so it can still be masked and restored."""
        spans = read_escaped(
            text,
            lambda t: self._detect(t, ctx, report, skip),
            lambda t: self._detect(t, ctx, None, (*skip, "ner")),
        )
        return [s for s in spans if s.etype == "LATET_ROUTE_KEY" or has_letter_or_digit(text[s.start : s.end])]

    def _detect(self, text: str, ctx: DetectContext, report: MaskReport | None, skip: tuple[str, ...]) -> list[Span]:
        spans: list[Span] = []
        for detector in [*self.detectors, self._known_values()]:
            if detector.id in skip:
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
        """
        if not text:
            return MaskedText(text)
        skip_surrogates = from_model or keep_surrogates
        ctx = DetectContext(kind, tool=tool, field=field, source=path)
        spans = self.detect(text, ctx, report)
        spans.extend(self._echoes(text, skip_surrogates))
        spans.extend(_extra_spans(text, extra))
        kept = []
        for span in spans:
            value = text[span.start : span.end]
            if self.allow.allows(value):
                continue
            if skip_surrogates and self.vault.is_surrogate(value):
                continue  # already in surrogate space
            kept.append(span)
        # The rule that found each value, for the vault: a later occurrence of it (a repeat) may come first.
        origin: dict[str, str] = {}
        for span in kept:
            if span.rule not in AGAIN:
                origin.setdefault(text[span.start : span.end], span.rule)
        # A value let through stays whole: no other value's repeat is masked inside it.
        allowed = [(s.start, s.end) for s in spans if self.allow.allows(text[s.start : s.end])]
        kept.extend(
            r for r in self._repeats(text, kept, origin) if not any(a < r.end and r.start < b for a, b in allowed)
        )
        return self._apply(text, merge_spans(_whole_addresses(kept), text), origin, context)

    @staticmethod
    def _repeats(text: str, spans: list[Span], origin: dict[str, str]) -> Iterable[Span]:
        """Every other occurrence, in the same text, of a value detected once (e.g. in a comment).

        Not for the values of a structure (STRUCT): each one is found in its field, and the same
        word out of it is not the field's value. An e-mail address, written with `@` or out in words,
        brings its domain along (a company's, not a mail provider's), even one that is the value of
        a field typed EMAIL: `api.acme-intra.it` next to `ops@acme-intra.it` or
        `ops at acme-intra dot it` is masked in this text as it is in every later one, where the
        domain is a value already in the vault."""
        values = {
            text[s.start : s.end]: s
            for s in spans
            if long_enough(text[s.start : s.end], s.etype) and s.eclass != EntityClass.STRUCT
        }
        entries = [(v, s.etype, s.eclass) for v, s in values.items()]
        addresses = [(text[s.start : s.end], s) for s in spans if s.eclass == EntityClass.STRUCT and s.etype == "EMAIL"]
        for value, span in [*values.items(), *addresses]:
            domain = email_domain(value).lower() if span.etype == "EMAIL" else ""
            if len(domain) >= 4 and domain not in PUBLIC_MAIL_DOMAINS and domain not in values:
                entries.append((domain, "DOMAIN", EntityClass.PII))
                origin.setdefault(domain, span.rule)
        if not entries:
            return []
        detector = KnownValuesDetector(entries, rule="repeat")
        return read_escaped(text, lambda t: list(detector.detect(t, DetectContext())))

    def _echoes(self, text: str, from_model: bool) -> Iterable[Span]:
        """Issued surrogates found in client text become new originals of their own.

        A surrogate the client sends back (pasted, or written to a file earlier) must not be confused
        with the value it stands for: masking it again keeps every restore unambiguous.
        """
        if from_model:
            return []
        return self.surrogates_in(text)

    def _apply(
        self, text: str, spans: list[Span], origin: dict[str, str] | None = None, context: str = ""
    ) -> MaskedText:
        around = f"{text}\n{context}" if context else text  # what a fake drawn here must not hold
        pieces: list[str] = []
        replacements: list[Replacement] = []
        cursor = 0
        for span in spans:
            original = text[span.start : span.end]
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
                        generate=self.generators.get(span.etype),
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
