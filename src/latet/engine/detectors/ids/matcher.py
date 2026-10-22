"""Finding one kind of national ID in a text: pattern, bounds, check digits, then keywords."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import re2

from latet.engine.detectors.base import quiet_options
from latet.engine.detectors.context import ContextWords
from latet.engine.detectors.ids.spec import IdSpec, isolated
from latet.engine.types import EntityClass, Span

# A number confirmed by its check digits or a keyword names a span it shares with a phone number.
SCORE = 2.0


class IdMatcher:
    def __init__(self, spec: IdSpec) -> None:
        self.spec = spec
        self.regex = re2.compile(spec.pattern, quiet_options()) if spec.finder is None else None
        self.keywords = ContextWords(spec.words, spec.acronyms)

    def needs_keyword(self, natural: bool) -> bool:
        context = self.spec.context
        return context == "always" or (context == "code" and not natural)

    def find(self, text: str, natural: bool) -> Iterator[Span]:
        spec = self.spec
        if spec.finder is not None:
            for span in spec.finder(text, natural):
                yield dataclasses.replace(span, score=SCORE)
            return
        assert self.regex is not None
        required = self.needs_keyword(natural)
        if required and not self.keywords.anywhere(text):
            return  # no keyword anywhere: no need to look at the numbers (most code)
        for m in self.regex.finditer(text):
            start, end = m.span()
            if not isolated(text, start, end, spec.numeric):
                continue
            value = text[start:end]
            if not spec.validate(value):
                continue
            # Written without separators, a grouped-layout number is just digits: a keyword decides.
            needed = required or (spec.context == "bare" and value.isalnum())
            if needed and not self.keywords.near(text, start, end):
                continue
            yield Span(start, end, spec.etype, EntityClass.PII, spec.rule_id, SCORE)
