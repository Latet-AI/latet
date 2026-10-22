r"""JSON text as an agent reads it, in a file or a tool's output: its escapes glue a value to a letter.

In `"Anna\nluca.neri@acme.it"` the line break is written as an escape, a backslash and an `n`. The `n` is a word
character, so as it is written the address has no word boundary before it: a detector that wants one misses the
value (an IBAN, a card number, a term) or takes the `n` with it (`nluca.neri@…`), and a restore that wants whole
words leaves a fake name at the start of a line. Read again with the escapes' letters blanked out (the same length,
so every place holds), the value stands on its own.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from itertools import accumulate

from latet.engine.types import EntityClass, Span

# A backslash that is not itself escaped (`\\`), then the letter of an escape, or `u` and four hex digits.
_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\([nrtbf]|u[0-9A-Fa-f]{4})")
_AT_END = re.compile(r"(?<!\\)(?:\\\\)*\\(?:[nrtbf]|u[0-9A-Fa-f]{4})\Z")
TAIL = 16  # enough of a text's end to see an escape there, six characters at most, and the backslashes before it


def escapes(text: str) -> list[tuple[int, int]]:
    """Where the letters of ``text``'s escapes are."""
    return [m.span(1) for m in _ESCAPE.finditer(text)] if "\\" in text else []


def breaks(text: str) -> list[tuple[int, int]]:
    """Where ``text`` writes whitespace as an escape (a line break, a tab...): its backslash and letter."""
    return [(start - 1, end) for start, end in escapes(text) if end - start == 1]


def blanked(text: str, places: list[tuple[int, int]]) -> str:
    """``text`` with the characters at ``places`` replaced by spaces."""
    parts: list[str] = []
    last = 0
    for start, end in places:
        parts += [text[last:start], " " * (end - start)]
        last = end
    parts.append(text[last:])
    return "".join(parts)


def ends_with_escape(text: str) -> bool:
    """``text`` ends with an escape: what follows it starts a word."""
    tail = text[-TAIL:]
    return "\\" in tail and _AT_END.search(tail) is not None


def read_escaped(
    text: str, read: Callable[[str], list[Span]], again: Callable[[str], list[Span]] | None = None
) -> list[Span]:
    """What ``read`` finds in ``text``, with what it finds (or ``again``, when given) where the letters of the
    text's escapes are blanked out. A value found there stands for its place: a finding of no stronger class
    that lies inside it, or only adds escape letters to it (`nluca.neri@…`), gives way."""
    spans = read(text)
    places = escapes(text)
    if not places:
        return spans
    found = sorted((again or read)(blanked(text, places)), key=lambda f: f.start)
    after = {p: end for start, end in places for p in range(start, end)}
    # By class: a finding gives way only to one at least as strong (a key, not the table cell around it).
    tables: dict[EntityClass, tuple[list[int], list[int]]] = {}

    def covered(span: Span) -> bool:
        if span.eclass not in tables:
            strong = [f for f in found if f.eclass >= span.eclass]
            # Their starts, and the furthest end of those starting so far.
            tables[span.eclass] = ([f.start for f in strong], list(accumulate((f.end for f in strong), max)))
        starts, reach = tables[span.eclass]
        i = bisect_right(starts, after.get(span.start, span.start))
        return i > 0 and reach[i - 1] >= span.end

    return list(dict.fromkeys([*(s for s in spans if not covered(s)), *found]))
