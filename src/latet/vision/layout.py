"""The text read in an image: its lines, in reading order, joined into one text that is masked like
any other; and the rectangles that cover a stretch of that text.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in image pixels
Rect = tuple[float, float, float, float]

COLUMN_GAP = 1.5  # a gap wider than this many line heights separates columns (a tab), not words
PADDING = 0.3  # a cover reaches this many characters beyond the stretch, for the recognizer's rounding


@dataclass(frozen=True, slots=True)
class Cover:
    """What to paint: over ``rect``, in the text line ``line`` (its colours, the height of its letters),
    the fake ``text``, or a bar when there is none."""

    rect: Rect
    line: Rect
    text: str | None = None
    reference: str = "Hg"  # letters whose height matches the line's ink: with descenders, or not
    start: float | None = None  # where the value began (the box reaches a little further): the fake starts there


DESCENDERS = set("gjpqyQ,;()[]{}|/_@$")


def reference_for(text: str) -> str:
    """The letters that span what the ink of a line with this text spans: "Hg" when some of its
    letters go below the line, "H" when none does (capitals and digits only, say)."""
    return "Hg" if any(c in DESCENDERS for c in text) else "H"


@dataclass(frozen=True, slots=True)
class OcrLine:
    box: Box
    text: str
    edges: tuple[float, ...]  # character i spans edges[i]..edges[i + 1], in image pixels
    score: float = 1.0

    @property
    def height(self) -> int:
        return self.box[3] - self.box[1]

    def char_width(self) -> float:
        return (self.edges[-1] - self.edges[0]) / max(1, len(self.text)) if self.text else 0.0


@dataclass(frozen=True, slots=True)
class OcrResult:
    width: int
    height: int
    lines: tuple[OcrLine, ...]  # in reading order
    text: str  # the lines joined: by a space or a tab within a row, by a newline between rows
    starts: tuple[int, ...]  # where each line starts in ``text``

    def cover(self, start: int, end: int) -> list[tuple[int, Rect]]:
        """The rectangles over ``text[start:end]``, one for each line it touches, with that line's index (the lines
        looked at from the one ``start`` is in, the lines of a tall page being thousands)."""
        out: list[tuple[int, Rect]] = []
        for i in range(max(0, bisect_right(self.starts, start) - 1), len(self.lines)):
            line, at = self.lines[i], self.starts[i]
            if at >= end:
                break
            a, b = max(start, at), min(end, at + len(line.text))
            if a >= b:
                continue
            pad = PADDING * line.char_width()
            x0 = max(0.0, line.edges[a - at] - pad)
            x1 = min(float(self.width), line.edges[b - at] + pad)
            out.append((i, (x0, float(line.box[1]), x1, float(line.box[3]))))
        return out


def reading_order(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Lines grouped into rows, top to bottom, each left to right.

    A line joins a row when its middle is within half a line of the middle of the row's first line:
    a sidebar with taller lines does not swallow two lines of the text next to it. Lines come from the
    top, so a row whose first line is more than half this line above it cannot take it: those are not
    looked at, the rows of a tall page being thousands.
    """
    rows: list[list[OcrLine]] = []
    middles: list[float] = []  # the middle of each row's first line, never smaller than the row's before
    for line in sorted(lines, key=lambda x: ((x.box[1] + x.box[3]) / 2, x.box[0])):
        middle = (line.box[1] + line.box[3]) / 2
        for r in range(bisect_left(middles, middle - 0.5 * line.height), len(rows)):
            first = rows[r][0]
            if abs(middle - middles[r]) <= 0.5 * min(first.height, line.height):
                rows[r].append(line)
                break
        else:
            rows.append([line])
            middles.append(middle)
    return [sorted(row, key=lambda x: x.box[0]) for row in rows]


def join(lines: list[OcrLine], width: int, height: int) -> OcrResult:
    ordered: list[OcrLine] = []
    starts: list[int] = []
    text = ""
    for r, row in enumerate(reading_order([line for line in lines if line.text])):
        for i, line in enumerate(row):
            if i:
                before = row[i - 1]
                gap = line.box[0] - before.box[2]
                text += "\t" if gap > COLUMN_GAP * max(before.height, line.height) else " "
            elif r:
                text += "\n"
            starts.append(len(text))
            ordered.append(line)
            text += line.text
    return OcrResult(width, height, tuple(ordered), text, tuple(starts))
