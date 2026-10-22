"""Keywords near a value: what tells a national ID from any other number of the same length.

A bare nine-digit number is everywhere in code; "BSN: 123456782" is a Dutch citizen number. A word
counts when it is on the same line as the value, within a few characters, or when it names the
column the value sits in (the first line of a CSV, TSV or Markdown table).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

import re2

from latet.engine.detectors.base import line_bounds, quiet_options

# The line-number prefix Claude Code's Read tool puts in front of every line it returns.
_READ_PREFIX = re.compile(r"^\s*\d+(?:\t|\u2192)")
_SEPARATORS = ("\t", "|", ";", ",")


def _escape(word: str) -> str:
    return re2.escape(word) if hasattr(re2, "escape") else re.escape(word)


class ContextWords:
    """Words (any case, whole words) and acronyms (exact case) that mark a kind of value.

    ``mode="substring"`` matches the words anywhere, even inside other words; it reproduces the
    original Italian VAT check, which looks for "iva" in "p.iva" and "partita iva".
    """

    def __init__(
        self,
        words: Iterable[str] = (),
        acronyms: Iterable[str] = (),
        mode: Literal["word", "substring"] = "word",
    ) -> None:
        self.words = tuple(dict.fromkeys(w.lower() for w in words if w))
        self.acronyms = tuple(dict.fromkeys(a for a in acronyms if a))
        self.mode = mode
        parts = []
        if self.words:
            alternatives = "|".join(_escape(w) for w in sorted(self.words, key=len, reverse=True))
            parts.append(f"(?i:{alternatives})" if mode == "substring" else rf"(?i:\b(?:{alternatives})\b)")
        if self.acronyms:
            alternatives = "|".join(_escape(a) for a in sorted(self.acronyms, key=len, reverse=True))
            parts.append(rf"\b(?:{alternatives})\b")
        self._pattern = re2.compile("|".join(parts), quiet_options()) if parts else None

    def __bool__(self) -> bool:
        return self._pattern is not None

    def anywhere(self, text: str) -> bool:
        """A quick test before looking for the values at all."""
        return self._pattern is not None and self._pattern.search(text) is not None

    def near(self, text: str, start: int, end: int, before: int = 48, after: int = 24, header: bool = True) -> bool:
        """A word within ``before``/``after`` characters on the value's line, or in its column header."""
        if self._pattern is None:
            return False
        line_start, line_end = line_bounds(text, start, end)
        if self._pattern.search(text[max(line_start, start - before) : start]):
            return True
        if after and self._pattern.search(text[end : min(line_end, end + after)]):
            return True
        return header and self._in_header(text, start, line_start, line_end)

    def _in_header(self, text: str, start: int, line_start: int, line_end: int) -> bool:
        line = text[line_start:line_end]
        prefix = _READ_PREFIX.match(line)
        offset = prefix.end() if prefix else 0
        separator = next((s for s in _SEPARATORS if s in line[offset:]), None)
        if separator is None or line_start == 0:
            return False
        column = line[offset : start - line_start].count(separator)
        first = text[: text.find("\n")] if "\n" in text else text
        first_prefix = _READ_PREFIX.match(first)
        cells = first[first_prefix.end() if first_prefix else 0 :].split(separator)
        pattern = self._pattern
        return pattern is not None and column < len(cells) and pattern.search(cells[column]) is not None
