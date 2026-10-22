"""From the text detector's probability map to boxes of text lines (DB post-processing, NumPy only).

Screenshots hold horizontal text, so boxes are axis-aligned: the bounding box of each connected
region of the map, expanded as PaddleOCR expands its polygons. Boxes the detector split at wide word
gaps are joined back into their line.
"""

from __future__ import annotations

import numpy as np

Box = tuple[float, float, float, float]


def components(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (x0, y0, x1, y1) of the 8-connected regions of a boolean mask.

    The runs of each row come from NumPy; runs that touch across rows are joined with a union-find.
    """
    height, width = mask.shape
    padded = np.zeros((height, width + 2), np.int8)
    padded[:, 1:-1] = mask
    step = np.diff(padded, axis=1)
    rows, starts = np.nonzero(step == 1)
    _, ends = np.nonzero(step == -1)
    if not len(rows):
        return []
    starts_l, ends_l = starts.tolist(), ends.tolist()
    first = np.searchsorted(rows, np.arange(height + 1)).tolist()
    parent = list(range(len(starts_l)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for r in range(height - 1):
        i, i_end = first[r], first[r + 1]
        j, j_end = first[r + 1], first[r + 2]
        while i < i_end and j < j_end:
            # ends are exclusive: runs touching diagonally (a start equal to the other's end) connect
            if starts_l[i] <= ends_l[j] and starts_l[j] <= ends_l[i]:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            if ends_l[i] < ends_l[j]:
                i += 1
            else:
                j += 1
    roots = np.array([find(i) for i in range(len(parent))])
    labels, inverse = np.unique(roots, return_inverse=True)
    n = len(labels)
    x0 = np.full(n, width)
    y0 = np.full(n, height)
    x1 = np.zeros(n, np.int64)
    y1 = np.zeros(n, np.int64)
    np.minimum.at(x0, inverse, starts)
    np.maximum.at(x1, inverse, ends)
    np.minimum.at(y0, inverse, rows)
    np.maximum.at(y1, inverse, rows + 1)
    return list(zip(x0.tolist(), y0.tolist(), x1.tolist(), y1.tolist(), strict=True))


def boxes(prob: np.ndarray, thresh: float, box_thresh: float, unclip: float) -> list[Box]:
    """Text boxes from the probability map, in the map's coordinates.

    A region needs a mean probability of ``box_thresh``; it is then expanded on every side by
    area * ``unclip`` / perimeter, PaddleOCR's own expansion of the shrunk text kernel.
    """
    out: list[Box] = []
    for x0, y0, x1, y1 in components(prob > thresh):
        w, h = x1 - x0, y1 - y0
        if min(w, h) < 3 or float(prob[y0:y1, x0:x1].mean()) < box_thresh:
            continue
        d = w * h * unclip / (2 * (w + h))
        if min(w, h) + 2 * d < 5:
            continue
        out.append((x0 - d, y0 - d, x1 + d, y1 + d))
    return out


ROWS = 32  # the boxes kept are looked up by the bands of this many rows of pixels they span


def merge_rows(found: list[tuple[int, int, int, int]], gap: float = 0.6) -> list[tuple[int, int, int, int]]:
    """Join boxes of one line that the detector split at word gaps (large text, wide letter spacing).

    Two boxes join when they share most of their height, their heights are alike, and the gap between
    them is at most ``gap`` times their height (expanded boxes often overlap). Table columns stay
    apart: their gaps are wider.

    Left to right, each box joins the first box kept before it that it can join. Only those that share a row of pixels
    with it can, and only while it is near enough along the row: a box kept is looked up by the bands of rows it spans,
    and forgotten once the boxes have gone further than it could join (they come in order), so that the boxes of a page
    read in tiles, tens of thousands, join in a time that grows with their number, not its square.
    """
    out: list[list[int]] = []
    filed: list[range] = []  # the bands each box kept is looked up by
    bands: dict[int, list[int]] = {}  # the boxes kept that may still join, by band
    for box in sorted(found, key=lambda b: b[0]):
        h = box[3] - box[1]
        near: set[int] = set()
        for band in _bands(box):
            kept = bands.get(band)
            if kept:  # one this far behind joins no box from here on, nor grows
                kept[:] = [i for i in kept if box[0] - out[i][2] <= gap * (out[i][3] - out[i][1])]
                near.update(kept)
        for i in sorted(near):  # in the order they were kept
            other = out[i]
            oh = other[3] - other[1]
            shared = min(box[3], other[3]) - max(box[1], other[1])
            if shared < 0.6 * min(h, oh) or max(h, oh) > 1.5 * min(h, oh):
                continue
            if box[0] - other[2] <= gap * min(h, oh):
                other[0] = min(other[0], box[0])
                other[1] = min(other[1], box[1])
                other[2] = max(other[2], box[2])
                other[3] = max(other[3], box[3])
                grown = _bands(other)
                for band in grown:
                    if band not in filed[i]:
                        bands.setdefault(band, []).append(i)
                filed[i] = grown
                break
        else:
            out.append(list(box))
            filed.append(_bands(box))
            for band in filed[-1]:
                bands.setdefault(band, []).append(len(out) - 1)
    return [(b[0], b[1], b[2], b[3]) for b in out]


def _bands(box: tuple[int, int, int, int] | list[int]) -> range:
    """The bands of rows a box spans, its last row included: two boxes that share a row share a band."""
    return range(int(box[1] // ROWS), int(box[3] // ROWS) + 1)
