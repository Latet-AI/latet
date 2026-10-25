"""Turn overlapping detections into disjoint spans, so no value is ever replaced partially."""

from __future__ import annotations

import bisect
from collections.abc import Iterable

from latet.engine.escapes import breaks, decoded
from latet.engine.types import EntityClass, Span


def _rank(span: Span) -> tuple[int, int, float, bool]:
    # Alike otherwise, a value found as it is written names the result over one found where escapes read as what they
    # stand for: a value of a structure stays as written in it.
    return (int(span.eclass), span.length, span.score, span.value is None)


def merge_spans(spans: Iterable[Span], text: str | None = None) -> list[Span]:
    """Group spans whose ranges overlap and emit one span per group, covering all of it.

    Within a group the strongest finding (entity class, then length, then score) names the result;
    the result always covers the union of the group, so every detected character stays masked.

    With ``text``, a value of a structure (STRUCT) holding findings of their own, like a note with a
    phone number in it, is cut around them first: the phone number keeps its type, and the rest of
    the note is masked as the note. And a group that holds a value written in escapes (``Span.value``) reads
    as its escapes read, where its ends are not inside one.
    """
    if text is not None:
        spans = _carve(list(spans), text)
    return _merged(spans, text)


def _merged(spans: Iterable[Span], text: str | None) -> list[Span]:
    ordered = sorted(spans, key=lambda s: (s.start, -s.end))
    merged: list[Span] = []
    group: list[Span] = []
    group_end = -1
    for span in ordered:
        if group and span.start < group_end:
            group.append(span)
            group_end = max(group_end, span.end)
            continue
        if group:
            merged.append(_collapse(group, text))
        group = [span]
        group_end = span.end
    if group:
        merged.append(_collapse(group, text))
    return merged


def _carve(spans: list[Span], text: str) -> list[Span]:
    structs = [s for s in spans if s.eclass == EntityClass.STRUCT]
    if not structs:
        return spans
    # A line break or a tab that JSON text writes as an escape stays as written: a value's pieces go
    # around it as around a space, so the model still reads valid JSON and each piece is restored.
    gaps = breaks(text)
    if len(structs) == len(spans) and not gaps:
        return spans
    gap_ends = [end for _, end in gaps]
    others = _merged((s for s in spans if s.eclass != EntityClass.STRUCT), text)
    ends = [o.end for o in others]  # disjoint and in order: their ends are in order too
    out = list(others)
    for s in structs:
        cursor = s.start
        i = bisect.bisect_right(ends, s.start)
        while i < len(others) and others[i].start < s.end:
            out += _piece(s, cursor, others[i].start, text, gaps, gap_ends)
            cursor = max(cursor, others[i].end)
            i += 1
        out += _piece(s, cursor, s.end, text, gaps, gap_ends)
    return out


def _piece(
    struct: Span, start: int, end: int, text: str, gaps: list[tuple[int, int]], gap_ends: list[int]
) -> list[Span]:
    """``text[start:end]`` of a value of a structure, without the whitespace at its ends, in pieces around
    ``gaps`` (in order, with their ends in ``gap_ends``)."""
    stretches: list[tuple[int, int]] = []
    i = bisect.bisect_right(gap_ends, start)
    while i < len(gaps) and gaps[i][0] < end:
        stretches.append((start, max(start, gaps[i][0])))
        start = max(start, gaps[i][1])
        i += 1
    stretches.append((start, end))
    out = []
    for a, b in stretches:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a and any(c.isalnum() for c in text[a:b]):
            out.append(Span(a, b, struct.etype, struct.eclass, struct.rule, struct.score))
    return out


def _collapse(group: list[Span], text: str | None) -> Span:
    if len(group) == 1:
        return group[0]
    start = min(s.start for s in group)
    end = max(s.end for s in group)
    best = max(group, key=_rank)
    if best.start == start and best.end == end:
        return best
    value = None
    if text is not None and any(s.value is not None for s in group) and (reading := decoded(text)) is not None:
        # Values written in escapes among them: the whole reads as its escapes read (its ends are where escapes begin
        # and end, or as written).
        value = reading.read(start, end)
        value = None if value == text[start:end] else value
    return Span(start, end, best.etype, best.eclass, best.rule, best.score, value)
