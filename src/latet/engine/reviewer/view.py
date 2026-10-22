"""The text the reviewer reads: what is sent, with Latet's replacements shown as typed placeholders.

A surrogate looks like a real value, so a reviewer shown one reports it; a placeholder (``⟦EMAIL#1⟧``)
says the value is covered. Long texts are cut into pieces at line ends, which small models read
better than one long text.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from latet.engine import wellformed
from latet.engine.reviewer.prompt import CLOSE, OPEN

# A key written as a field's name (`exit_code`, `id`, `output`): not shown to the reviewer as a value.
_FIELD_NAME = re.compile(r"[a-z0-9_\-]*")


@dataclass(frozen=True, slots=True)
class View:
    text: str
    holes: tuple[tuple[int, int], ...] = ()  # where the placeholders are

    def outside_holes(self, start: int, end: int) -> bool:
        return not any(a < end and start < b for a, b in self.holes)


def view_of(text: str, covered: Sequence[tuple[int, int, str]]) -> View:
    """``text`` with each covered range (start, end, type) shown as a placeholder; the same value
    gets the same placeholder (``⟦PERSON#2⟧``) wherever it appears."""
    parts: list[str] = []
    holes: list[tuple[int, int]] = []
    names: dict[str, str] = {}
    counts: Counter[str] = Counter()
    pos = size = 0
    for start, end, etype in sorted(covered):
        if start < pos:
            continue  # overlapping ranges: the first one covers it
        clear = text[pos:start]
        parts.append(clear)
        size += len(clear)
        value = text[start:end]
        if value not in names:
            counts[etype] += 1
            names[value] = f"{OPEN}{etype}#{counts[etype]}{CLOSE}"
        parts.append(names[value])
        holes.append((size, size + len(names[value])))
        size += len(names[value])
        pos = end
    parts.append(text[pos:])
    return View("".join(parts), tuple(holes))


def _strings(value: Any, keys: set[str]) -> Iterator[str]:
    """The strings of a JSON value, and each key that may be a value (a name, an address: not one written as a field's
    name, `exit_code`) the first time it comes (``keys``: those come already), a table's thousand rows naming their
    fields a thousand times."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in keys and _FIELD_NAME.fullmatch(k) is None:
                keys.add(k)
                yield k
            yield from _strings(v, keys)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, keys)


def readable(text: str) -> str:
    """A tool's output as a model reads it: a JSON document as its strings, one per line, with each key that may be a
    value once (a command's output wrapped as ``{"output": "..."}`` comes out with its own lines)."""
    if text.lstrip()[:1] not in ("{", "["):
        return text
    try:
        doc = wellformed.loads(text)
    except ValueError:
        return text
    strings = [s for s in _strings(doc, set()) if s.strip()]
    return "\n".join(strings) if strings else text


def pieces(view: View, limit: int) -> list[View]:
    """Pieces of about ``limit`` characters, cut at line ends, never inside a placeholder."""
    if len(view.text) <= limit:
        return [view]
    out: list[View] = []
    start = 0
    text = view.text
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            cut = text.rfind("\n", start + 1, end)
            if cut > start:
                end = cut + 1
            else:  # one line longer than a piece: cut at a space, out of any placeholder
                space = text.rfind(" ", start + 1, end)
                end = space + 1 if space > start else end
                for a, b in view.holes:
                    if a < end < b:
                        end = b
        holes = tuple((a - start, b - start) for a, b in view.holes if a >= start and b <= end)
        out.append(View(text[start:end], holes))
        start = end
    return out
