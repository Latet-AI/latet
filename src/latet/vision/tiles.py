"""Reading a tall, a wide or a large image in tiles.

The detector reads an image scaled down to a long side of 2048 pixels at most (``OnnxOcr.MAX_SIDE``). A full-HD or a
QHD screen's screenshot keeps its text that way, but a full page's, 1280 by 12,288, would be read at a sixth of its
size, and a 4K or 5K screen's at a half or less: its text would shrink below what the detector finds or reads right,
and the image would leave with its values unread. Such an image is read in tiles, each at its own size (one wider than
2048, at the scale a square as wide is read at) and at ``MIN_SCALE`` at least: along its long side, each tile holding
the image across, or, when its short side too is longer than the detector reads at ``MIN_SCALE``, in a grid. Adjacent
tiles share a band of ``OVERLAP`` of the detector's pixels, so that a line of text up to that height (across a tile's
side, that length) is whole in one of them. What each tile finds is joined back into the image's boxes (``stitch``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in image pixels

# An image is read whole when it is at most ELONGATED times as long as it is wide (or as wide as it is high) and the
# detector reads it at MIN_SCALE at least, where it reads text of 12 pixels right: the screenshot of a full-HD or a QHD
# screen, a MacBook's, a phone's, an iPad's. A longer one is a page, or screens side by side, and a larger one (a 4K or
# 5K screen's, a photograph) would be read too small: in tiles.
ELONGATED = 2.5
MIN_SCALE = 0.667
OVERLAP = 256  # the band adjacent tiles share, in the detector's pixels
EDGE = 2  # a box this close to a tile's edge, in the detector's pixels, was cut by it
STRIPS = 32  # the boxes in a band are looked up by the cells of this many pixels a side they span


@dataclass(frozen=True, slots=True)
class Tiling:
    """Where the detector reads an image: its regions row by row, each row from the left (one: the whole image), how
    many make a row, and, in image pixels, the band adjacent regions share at least, either way, and how close to a
    region's inner edge a box was cut by it."""

    regions: tuple[Box, ...]
    columns: int
    overlap: int
    margin: int

    @property
    def rows(self) -> int:
        return len(self.regions) // self.columns

    @property
    def axis(self) -> int:
        """The axis the tiles of one row or one column follow: 0, side by side (a wide image's); 1, one under the other
        (a tall one's)."""
        return 0 if self.rows == 1 else 1


def _spans(total: int, length: int, overlap: int) -> list[tuple[int, int]]:
    """The fewest stretches of ``total`` pixels, each ``length`` long at most, that follow one another sharing
    ``overlap`` at least, evenly spaced, the last ending at the end."""
    if total <= length:
        return [(0, total)]
    count = math.ceil((total - overlap) / (length - overlap))
    size = math.ceil((total + (count - 1) * overlap) / count)
    return [((total - size) * k // (count - 1), (total - size) * k // (count - 1) + size) for k in range(count)]


def tiling(width: int, height: int, side: int) -> Tiling:
    """The regions in which the detector reads an image of ``width`` by ``height``, reading each with its long side
    scaled down to ``side`` at most: the whole image; or the fewest tiles along its long side, each holding the image
    across, no longer than ``side`` (or than the image is wide, when that is more); or, when the image is too wide
    across for that, the fewest tiles both ways, none longer than the detector reads at ``MIN_SCALE``. Evenly spaced,
    sharing the band."""
    long, short = max(width, height), min(width, height)
    whole = Tiling(((0, 0, width, height),), 1, 0, 0)
    if long <= side or long <= min(ELONGATED * short, side / MIN_SCALE):
        return whole
    largest = math.floor(side / MIN_SCALE)  # the longest a tile is read at MIN_SCALE
    # A tile holding the image across is read at its own size, or as a square as wide is; in a grid, at MIN_SCALE.
    length = max(side, short) if short <= largest else largest
    overlap = math.ceil(OVERLAP * length / side)
    along = _spans(long, length, overlap)
    across = [(0, short)] if short <= largest else _spans(short, length, overlap)
    xs, ys = (along, across) if width >= height else (across, along)
    regions = tuple((x0, y0, x1, y1) for y0, y1 in ys for x0, x1 in xs)
    return Tiling(regions, len(xs), overlap, math.ceil(EDGE * length / side))


@dataclass(slots=True)
class _Found:
    box: Box
    tile: int
    cuts: tuple[bool, bool, bool, bool]  # cut by its tile's left, top, right and bottom edge

    def at_start(self, axis: int) -> bool:
        """Cut by its tile's edge before it along ``axis`` (0: its left, 1: its top)."""
        return self.cuts[axis]

    def at_end(self, axis: int) -> bool:
        return self.cuts[axis + 2]

    def cut(self, axis: int) -> bool:
        return self.cuts[axis] or self.cuts[axis + 2]


def _area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _shared(a: Box, b: Box) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _along(a: Box, b: Box, axis: int) -> int:
    """How much of an axis (0: x, 1: y) two boxes share."""
    return min(a[axis + 2], b[axis + 2]) - max(a[axis], b[axis])


def _overlapping(
    here: list[int], there: list[int], entries: list[_Found], axis: int, band: tuple[int, int]
) -> list[tuple[int, int]]:
    """The pairs of a box of ``here`` (a tile's) and a box of ``there`` (the next tile's along ``axis``) that overlap,
    which they can only do in the ``band`` the two tiles share: each box of ``there`` looked up by the cells of
    ``STRIPS`` pixels a side it spans in the band, so that a band full of small words is gone through in a time that
    grows with them, not their square."""
    low, high = band

    def cells(box: Box) -> list[tuple[int, int]]:
        along = range(int(max(box[axis], low) // STRIPS), int(min(box[axis + 2], high) // STRIPS) + 1)
        return [(s, t) for s in range(int(box[1 - axis] // STRIPS), int(box[3 - axis] // STRIPS) + 1) for t in along]

    lookup: dict[tuple[int, int], list[int]] = {}
    for j in there:
        for cell in cells(entries[j].box):
            lookup.setdefault(cell, []).append(j)
    pairs = []
    for i in here:
        box = entries[i].box
        seen: set[int] = set()
        for cell in cells(box):
            for j in lookup.get(cell, ()):
                if j not in seen:
                    seen.add(j)
                    if _shared(box, entries[j].box) > 0:
                        pairs.append((i, j))
    return pairs


def _one_line(a: Box, b: Box, axis: int, band: int) -> bool:
    """Whether two parts, cut by the edges of the band two tiles share along ``axis``, are of one line: they hold the
    same stretch of the band, and each lies mostly across from the other."""
    across = _along(a, b, 1 - axis)
    return (
        _along(a, b, axis) * 2 >= band
        and across * 2 >= a[3 - axis] - a[1 - axis]
        and across * 2 >= b[3 - axis] - b[1 - axis]
    )


# A part of a line the other tile holds whole is left out. Where two rows of tiles meet, the edge cuts a line through
# its letters: the part is half letters, left out once the other tile's lines hold half of it. Where two tiles of a row
# meet, it cuts a line between letters: the part is whole letters, joined with the rest of its line (``db.merge_rows``),
# and left out only when held all but a sliver, lest the start of a line the other tile reads in words, which that tile
# never sees, be lost. By axis: 0 where two tiles of a row meet, 1 where two rows meet.
HELD = (0.9, 0.5)


def stitch(plan: Tiling, found: Sequence[Sequence[Box]]) -> list[Box]:
    """The boxes each region of ``plan`` found (in the image's coordinates), joined into the image's.

    Two adjacent tiles both read the band they share. A line in it is found by both, and kept once. A line a tile's edge
    cuts is whole in the other tile, which reads it, and the cut part is left out (``HELD``). A line no tile holds whole
    (taller than the band, or longer across a tile's side) is joined from its parts, tile to tile."""
    if len(plan.regions) == 1:
        return list(found[0])
    columns, rows = plan.columns, plan.rows
    entries: list[_Found] = []
    by_tile: list[list[int]] = []
    for k, (region, boxes) in enumerate(zip(plan.regions, found, strict=True)):
        row, column = divmod(k, columns)
        inner = (column > 0, row > 0, column < columns - 1, row < rows - 1)  # its left, top, right, bottom edges
        by_tile.append([])
        for box in boxes:
            cuts = (
                inner[0] and box[0] <= region[0] + plan.margin,
                inner[1] and box[1] <= region[1] + plan.margin,
                inner[2] and box[2] >= region[2] - plan.margin,
                inner[3] and box[3] >= region[3] - plan.margin,
            )
            by_tile[k].append(len(entries))
            entries.append(_Found(box, k, cuts))
    neighbours = [(k, k + 1, 0) for k in range(len(plan.regions)) if k % columns < columns - 1]
    neighbours += [(k, k + columns, 1) for k in range(len(plan.regions) - columns)]
    meeting = []  # for each pair of adjacent tiles, the axis they follow and the boxes of one that overlap the other's
    for k, n, a in neighbours:
        low, high = plan.regions[n][a], plan.regions[k][a + 2]  # the band they share
        here = [i for i in by_tile[k] if entries[i].box[a + 2] > low]
        there = [j for j in by_tile[n] if entries[j].box[a] < high]
        meeting.append((a, _overlapping(here, there, entries, a, (low, high))))
    covered = ([0] * len(entries), [0] * len(entries))  # by axis: how much of a cut part the other tile's lines hold
    for a, pairs in meeting:
        for i, j in pairs:
            mine, theirs = entries[i], entries[j]
            if mine.at_end(a) and not theirs.cut(a):
                covered[a][i] += _shared(mine.box, theirs.box)
            if theirs.at_start(a) and not mine.cut(a):
                covered[a][j] += _shared(mine.box, theirs.box)
    dropped = {
        i for i, e in enumerate(entries) if any(e.cut(a) and covered[a][i] >= HELD[a] * _area(e.box) for a in (0, 1))
    }
    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, pairs in meeting:
        for i, j in pairs:
            mine, theirs = entries[i], entries[j]
            if i in dropped or j in dropped:
                continue
            if not mine.cut(a) and not theirs.cut(a):  # one line, found by both
                same = _shared(mine.box, theirs.box) * 2 >= min(_area(mine.box), _area(theirs.box))
            else:  # the parts of a line that neither holds whole
                same = mine.at_end(a) and theirs.at_start(a) and _one_line(mine.box, theirs.box, a, plan.overlap)
            if same:
                first, second = sorted((find(i), find(j)))
                parent[second] = first
    groups: dict[int, list[Box]] = {}
    for i, entry in enumerate(entries):
        if i not in dropped:
            groups.setdefault(find(i), []).append(entry.box)
    return [
        (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        for boxes in groups.values()
    ]
