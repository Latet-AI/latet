"""A tall or a wide image is read in tiles along its long side, each at the scale the detector reads an ordinary
screenshot at, and the boxes each tile finds are joined back into the image's: Task 16 found that a full page's
screenshot (1280 by 12,288, 640 by 16,000), read scaled down to a long side of 2048, had its text shrink below what the
detector finds, and left as it came. The models are stood in for here; ``test_ocr_live.py`` reads with the real ones."""

from __future__ import annotations

import base64
import io
import threading
import time
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from latet.engine.types import SlotKind
from latet.protocols.base import ImageRef
from latet.vision import db, tiles
from latet.vision.masking import WITHHELD, ImageMasking
from latet.vision.ocr import (
    MAX_TILES,
    OcrBusy,
    OcrTooLong,
    OcrTooManyTiles,
    OcrTooWide,
    OnnxOcr,
)
from latet.vision.pool import (
    SPACE_MAX_PIXELS,
    SpaceReads,
)

SIDE = OnnxOcr.MAX_SIDE


class _Session:
    """A model session that stands in for one of ONNX Runtime's: it keeps the shape of each input it is given, takes
    ``seconds`` a run, and answers ``out(inputs)``."""

    def __init__(self, out, seconds=0.0):
        self.out, self.seconds = out, seconds
        self.shapes: list[tuple[int, ...]] = []

    @property
    def runs(self) -> int:
        return len(self.shapes)

    def run(self, names, inputs, options=None):
        self.shapes.append(inputs["x"].shape)
        time.sleep(self.seconds)
        return self.out(inputs)


def _ink(inputs):
    """The detector's map of its input: text wherever the input is dark (it reads the image normalised: dark is
    below zero)."""
    x = inputs["x"][0]
    return [((x.mean(0) < 0) * np.float32(0.9))[None, None]]


def _one_letter(inputs):
    """The recognizer's answer for each line of a batch: one letter, "a"."""
    batch, _, _, width = inputs["x"].shape
    probs = np.zeros((batch, max(1, width // 8), 3), np.float32)
    probs[:, :, 0] = 1.0
    probs[:, 0, :] = (0.0, 1.0, 0.0)
    return [probs]


def _ocr(det=None, rec=None, unclip=1.5):
    """A text recognition with stand-ins for its two models (the detector's map read as the real one's is)."""
    ocr = OnnxOcr(Path("det"), Path("rec"), threads=1)
    ocr._det, ocr._rec = det or _Session(_ink), rec or _Session(_one_letter)
    ocr._chars = ["", "a", " "]
    ocr._post = (0.3, 0.6, unclip)
    return ocr


def _bars(width, height, bars):
    """A white image with a black bar for each of ``bars`` (x0, y0, x1, y1): lines of text, as the stand-in reads."""
    rgb = np.full((height, width, 3), 255, np.uint8)
    for x0, y0, x1, y1 in bars:
        rgb[y0:y1, x0:x1] = 0
    return rgb


def _covers(box, bar, slack=2):
    return (
        box[0] <= bar[0] + slack and box[1] <= bar[1] + slack and box[2] >= bar[2] - slack and box[3] >= bar[3] - slack
    )


def _one_box_for_each(boxes, bars):
    """Each bar is covered by exactly one box, and each box covers exactly one bar: no line lost, read twice, or read
    in part."""
    for bar in bars:
        assert sum(_covers(box, bar) for box in boxes) == 1, (bar, boxes)
    for box in boxes:
        assert sum(_covers(box, bar) for bar in bars) == 1, (box, bars)
    assert len(boxes) == len(bars)


def _before(width, height):
    """The size the detector read an image at before tiles: its long side scaled down to 2048, in steps of 32."""
    scale = min(1.0, SIDE / max(height, width))
    return max(32, round(height * scale / 32) * 32), max(32, round(width * scale / 32) * 32)


def _scale(width, height):
    """The scale the detector reads an image (or a tile) of this size at."""
    return min(1.0, SIDE / max(width, height))


ORDINARY = [
    (300, 200),
    (1366, 768),
    (1920, 1080),
    (1920, 1200),
    (2560, 1440),
    (2560, 1600),
    (2880, 1800),
    (3024, 1964),
    (1170, 2532),
    (1290, 2796),
    (2048, 2732),
]


# --- screenshots the detector reads whole: as before -----------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), ORDINARY)
def test_a_screenshot_read_at_a_readable_size_is_read_whole_as_before(width, height):
    """A screenshot the detector reads at two thirds of its size at least (full HD, QHD, a MacBook's, a phone's, an
    iPad's): one detection of the whole image, at the size it was read before. Its boxes, its masks and what it costs
    are what they were."""
    assert tiles.tiling(width, height, SIDE).regions == ((0, 0, width, height),)
    assert _scale(width, height) >= tiles.MIN_SCALE
    ocr = _ocr()
    ocr.read(Image.new("RGB", (width, height), "white"))
    assert ocr._det.shapes == [(1, 3, *_before(width, height))]


# --- tall, wide and large images: in tiles ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (1280, 12288),
        (640, 16000),
        (1280, 4096),
        (16000, 640),
        (3840, 1080),
        (5120, 1440),
        (100, 28000),
        (2100, 800),
        (3440, 1440),  # a 21:9 screen: read whole at 0.6
        (3456, 2234),  # a 16-inch MacBook's: 0.59
        (3840, 1600),  # 0.53
        (3840, 2160),  # a 4K screen's: 0.53, small text at 100% misread
        (5120, 2160),  # a 5K2K one's: 0.4, 13 px text read 12 times in 28
        (5120, 2880),  # a 5K one's: 0.4
        (1440, 3200),  # a phone's of 20:9 at 3x: 0.64
        (2160, 3840),  # 4K, upright
    ],
)
def test_a_tall_wide_or_large_screenshot_is_read_in_tiles_along_it_at_a_readable_size(width, height):
    """Task 16's finding: 1280 by 12,288 and 640 by 16,000 were read at a sixth and an eighth of their size, their text
    too small to be found; and a 4K or 5K screen's, at 0.53 or 0.4, misread its small text. Each tile is read at its
    own size, as a screenshot of up to 2048 pixels is (one wider than that, as a square as wide is), at two thirds of it
    at least: along a tall image's height, a wide one's width."""
    plan = tiles.tiling(width, height, SIDE)
    assert len(plan.regions) > 1 and 1 in (plan.columns, plan.rows)
    ocr = _ocr()
    ocr.read(Image.new("RGB", (width, height), "white"))
    assert ocr._det.runs == len(plan.regions)
    short = min(width, height)
    for (x0, y0, x1, y1), shape in zip(plan.regions, ocr._det.shapes, strict=True):
        assert max(x1 - x0, y1 - y0) <= max(SIDE, short)
        assert _scale(x1 - x0, y1 - y0) >= tiles.MIN_SCALE
        assert shape == (1, 3, *_before(x1 - x0, y1 - y0))  # read as an image of the tile's size is
    tall = height > width
    for x0, y0, x1, y1 in plan.regions:  # each tile holds the image across
        assert ((x0, x1) == (0, width)) if tall else ((y0, y1) == (0, height))


@pytest.mark.parametrize(
    ("width", "height"),
    [(6016, 3384), (3384, 6016), (7680, 4320), (3200, 3200), (4000, 4000), (5000, 5000), (6000, 6000), (10_000, 4000)],
)
def test_an_image_too_large_both_ways_is_read_in_a_grid_of_tiles_at_a_readable_size(width, height):
    """An image whose short side is too long for tiles along it to be read at two thirds of their size (a 6K or 8K
    screen's, a photograph's, a near-square image of tens of megapixels, read at 0.34 down to 0.2) is read in a grid of
    tiles, none longer either way than the detector reads at two thirds of its size."""
    plan = tiles.tiling(width, height, SIDE)
    assert plan.columns > 1 and plan.rows > 1 and len(plan.regions) <= MAX_TILES
    ocr = _ocr()
    ocr.read(Image.new("RGB", (width, height), "white"))
    assert ocr._det.runs == len(plan.regions)
    for (x0, y0, x1, y1), shape in zip(plan.regions, ocr._det.shapes, strict=True):
        assert _scale(x1 - x0, y1 - y0) >= tiles.MIN_SCALE and shape == (1, 3, *_before(x1 - x0, y1 - y0))


@given(st.data())
def test_no_image_is_read_too_small_and_every_square_as_wide_as_the_band_is_whole_in_one_tile(data):
    """However an image is shaped (within a computer's 40 megapixels), the detector reads it, or each of its tiles, at
    two thirds of its size at least. Its tiles form a grid (one row or one column but for an image too large both ways),
    each read at no smaller a scale than the image whole; adjacent tiles share a band of at least ``tiles.OVERLAP``
    detector pixels either way, so that any square as wide as the band (a line of text up to that height, or that
    length across a tile's side) is whole in one tile."""
    short = data.draw(st.integers(1, 6324))
    long = data.draw(st.integers(short, min(80_000, 40_000_000 // short)))
    tall = data.draw(st.booleans())
    width, height = (short, long) if tall else (long, short)
    plan = tiles.tiling(width, height, SIDE)
    assert all(_scale(x1 - x0, y1 - y0) >= tiles.MIN_SCALE for x0, y0, x1, y1 in plan.regions)
    if len(plan.regions) == 1:
        assert plan.regions == ((0, 0, width, height),)
        return
    assert plan.columns * plan.rows == len(plan.regions)
    rows = [plan.regions[r * plan.columns : (r + 1) * plan.columns] for r in range(plan.rows)]
    xs = [(x0, x1) for x0, _, x1, _ in rows[0]]
    ys = [(y0, y1) for _, y0, _, y1 in plan.regions[:: plan.columns]]
    assert all([(x0, x1) for x0, _, x1, _ in row] == xs for row in rows)  # a grid
    assert all([(y0, y1) for _, y0, _, y1 in row] == [ys[r]] * plan.columns for r, row in enumerate(rows))
    for spans, extent in ((xs, width), (ys, height)):
        assert spans[0][0] == 0 and spans[-1][1] == extent
        assert all(s0 < s1 and e0 < e1 and e0 - s1 >= plan.overlap for (s0, e0), (s1, e1) in pairwise(spans))
    scales = [_scale(x1 - x0, y1 - y0) for x0, y0, x1, y1 in plan.regions]
    assert plan.overlap * min(scales) >= tiles.OVERLAP - 1 and min(scales) > SIDE / long
    x = data.draw(st.integers(0, max(0, width - plan.overlap)))
    y = data.draw(st.integers(0, max(0, height - plan.overlap)))
    side = min(plan.overlap, width, height)
    assert any(x0 <= x and x + side <= x1 and y0 <= y and y + side <= y1 for x0, y0, x1, y1 in plan.regions)


def test_the_tiles_of_a_tall_image_are_the_fewest_that_share_the_band():
    plan = tiles.tiling(1280, 12288, SIDE)
    assert len(plan.regions) == 7 and plan.overlap == tiles.OVERLAP  # 6 tiles of 2048 sharing 256 would end at 11,008


# --- the tiles' boxes, joined back -------------------------------------------------------------------------------------


def _edges(plan):
    """Where each tile after the first starts, and where each tile before the last ends, along the plan's axis."""
    a = plan.axis
    return [r[a] for r in plan.regions[1:]], [r[a + 2] for r in plan.regions[:-1]]


@pytest.mark.parametrize("unclip", [0.0, 1.5])
def test_the_lines_of_a_tall_image_are_found_once_each_wherever_the_tiles_cut_it(unclip):
    """A line across a tile's edge, a line in the band two tiles share, a line taller than the band: each is one box,
    in the image's coordinates, holding the whole line; none is found twice, and no tile's cut part of it is kept."""
    width, height = 1280, 12288
    plan = tiles.tiling(width, height, SIDE)
    starts, ends = _edges(plan)
    bars = [(100, 500, 1100, 520), (100, 9000, 1100, 9020)]  # far from any edge
    bars += [(100, starts[0] - 10, 1100, starts[0] + 10)]  # across where the second tile starts
    bars += [(100, ends[0] - 10, 1100, ends[0] + 10)]  # across where the first tile ends
    bars += [(100, (starts[0] + ends[0]) // 2 - 10, 1100, (starts[0] + ends[0]) // 2 + 10)]  # in the band they share
    bars += [(100, starts[1] - 300, 1100, ends[1] + 300)]  # taller than the band: no tile holds it whole
    bars += [(200, ends[3] - 3, 900, ends[3] + 17), (200, starts[3] - 17, 900, starts[3] + 3)]  # just across, each way
    ocr = _ocr(unclip=unclip)
    _one_box_for_each(ocr._detect(_bars(width, height, bars)), bars)


@pytest.mark.parametrize("unclip", [0.0, 1.5])
def test_the_lines_of_a_wide_image_are_found_once_each_wherever_the_tiles_cut_them(unclip):
    """On a very long, low image the tiles cut the lines across their length: a line cut by a tile's edge is whole in
    the next one, and a line longer than a tile is joined from its parts, from one tile to the next."""
    width, height = 16000, 300
    plan = tiles.tiling(width, height, SIDE)
    assert plan.axis == 0 and len(plan.regions) > 3
    starts, ends = _edges(plan)
    bars = [(starts[0] - 50, 40, starts[0] + 50, 60)]  # across where the second tile starts
    bars += [(ends[0] - 60, 100, ends[0] + 40, 120)]  # across where the first tile ends
    bars += [(starts[1] + 20, 160, ends[1] - 20, 180)]  # in the band two tiles share
    bars += [(starts[2] - 500, 220, ends[4] + 500, 240)]  # longer than a tile: across four of them
    bars += [(300, 160, 900, 180), (15000, 40, 15800, 60)]  # far from any edge
    ocr = _ocr(unclip=unclip)
    _one_box_for_each(ocr._detect(_bars(width, height, bars)), bars)


@pytest.mark.parametrize("unclip", [0.0, 1.5])
def test_the_lines_of_an_image_read_in_a_grid_are_found_once_each_wherever_the_tiles_cut_them(unclip):
    """In a grid the tiles' edges cut lines both ways: across their letters where a row of tiles meets the next, along
    them where two tiles of a row meet, and both at a corner. Each line is one box holding it whole."""
    width = height = 3600
    plan = tiles.tiling(width, height, SIDE)
    assert (plan.columns, plan.rows) == (2, 2)
    (left, right), (top, bottom) = (plan.regions[0], plan.regions[1]), (plan.regions[0], plan.regions[2])
    x_start, x_end = right[0], left[2]  # the band two tiles of a row share
    y_start, y_end = bottom[1], top[3]  # the band two rows share
    bars = [(x_start - 300, 300, x_end + 300, 320)]  # along a row, across both edges of the band
    bars += [(200, y_start - 10, 1200, y_start + 10), (200, y_end - 10, 1200, y_end + 10)]  # across a row's edges
    bars += [(x_start - 200, y_end - 150, x_end + 200, y_end - 130)]  # across the band of a row, in that of the rows
    bars += [(x_start - 200, y_end - 10, x_end + 200, y_end + 10)]  # at the corner: across both
    bars += [(x_start + 40, y_start + 100, x_end - 40, y_start + 120)]  # in the square all four tiles share
    bars += [(500, 3300, 3100, 3320)]  # along the bottom row, across its band
    bars += [(x_end - 3, 2900, x_end + 300, 2920)]  # in the bottom row, three pixels of it in the left tile
    bars += [(x_end + 300, y_start - 400, 3300, y_end + 400)]  # taller than the band, in the right column
    ocr = _ocr(unclip=unclip)
    _one_box_for_each(ocr._detect(_bars(width, height, bars)), bars)


def test_two_lines_on_either_side_of_a_tiles_edge_stay_two():
    """A line that ends just before a tile's edge and the next one, just after it, are two lines, not one."""
    plan = tiles.tiling(1280, 12288, SIDE)
    end = _edges(plan)[1][0]
    bars = [(100, end - 25, 1100, end - 5), (100, end + 15, 1100, end + 35)]
    _one_box_for_each(_ocr()._detect(_bars(1280, 12288, bars)), bars)


def test_a_tall_image_s_lines_are_read_once_each():
    """Through the whole read: each line of a tall image is read once, wherever the tiles cut it."""
    plan = tiles.tiling(1280, 12288, SIDE)
    starts, ends = _edges(plan)
    rows = [500, starts[0] - 10, ends[0] - 10, (starts[1] + ends[1]) // 2, starts[5] - 10, 12200]
    bars = [(100, top, 1100, top + 20) for top in rows]
    result = _ocr().read(Image.fromarray(_bars(1280, 12288, bars)))
    assert result.text == "\n".join("a" * len(rows))
    assert all(line.box[1] <= top + 2 and line.box[3] >= top + 18 for line, top in zip(result.lines, rows, strict=True))


def test_joining_the_tiles_boxes_takes_a_time_that_grows_with_them_not_their_square():
    """Bands full of small words far apart, each found by both tiles of its band: a box is compared only with those it
    could be one line with, so that 96,000 boxes are joined in about a second, where comparing every pair of a band
    took five minutes, holding the text recognition a server's spaces share."""
    plan = tiles.tiling(1280, 12288, SIDE)
    found = []
    for region in plan.regions:
        boxes = []
        for other in plan.regions:
            low, high = max(region[1], other[1]), min(region[3], other[3])
            if other is not region and low < high:  # a band this tile shares: 40 rows of 200 boxes
                top = low + 8
                boxes += [
                    (6 * (n % 200), top + 6 * (n // 200), 6 * (n % 200) + 3, top + 6 * (n // 200) + 3)
                    for n in range(8000)
                ]
        found.append(boxes)
    started = time.monotonic()
    joined = tiles.stitch(plan, found)
    assert time.monotonic() - started < 20
    assert len(joined) * 2 == sum(map(len, found)) == 96_000  # each found by both tiles of its band, kept once


def test_a_wide_image_s_line_one_tile_reads_whole_and_the_next_in_words_is_kept_whole():
    """On a wide image the tiles cut a line across its letters, never through them: a tile's cut part is whole letters,
    and is left out only when the other tile's lines hold all of it. Here the first tile reads the line as one box up
    to its edge, and the next reads it in two words, the gap between them in the band they share: the first tile's part
    holds the line's start, which the next tile never sees, and is kept, joined with the words."""
    plan = tiles.tiling(16_000, 300, SIDE)
    (start, end) = plan.regions[1][0], plan.regions[0][2]
    line = (start - 100, 100, end, 130)  # the first tile's: from before the band to its edge
    words = [(start, 101, start + 20, 129), (start + 40, 100, start + 500, 130)]  # the next tile's, a gap of 20
    assert 0.5 < tiles._shared(line, words[1]) / tiles._area(line) < 0.9
    joined = db.merge_rows(tiles.stitch(plan, [[line], words, *([] for _ in plan.regions[2:])]))
    assert joined == [(start - 100, 100, start + 500, 130)]


def test_a_line_two_tiles_of_a_row_box_at_unlike_heights_is_joined_into_one():
    """The two parts of a line that the edges of a row's tiles cut, each longer than the band, are one line even when
    the two tiles box it at heights too unlike for a row's words to be joined: joined, not read as two lines."""
    plan = tiles.tiling(16_000, 300, SIDE)
    start, end = plan.regions[1][0], plan.regions[0][2]
    mine, theirs = (start - 1000, 100, end, 130), (start, 95, start + 1500, 145)  # 30 and 50 high
    assert db.merge_rows([mine, theirs]) == [mine, theirs]
    joined = db.merge_rows(tiles.stitch(plan, [[mine], [theirs], *([] for _ in plan.regions[2:])]))
    assert joined == [(start - 1000, 95, start + 1500, 145)]


def test_a_tall_image_s_line_cut_across_its_letters_is_left_out_where_the_next_tile_holds_it():
    """On a tall image the tiles cut a line through its letters: the cut part is half letters, left out once the other
    tile's lines hold half of it, whatever the detector's few pixels of difference between the two."""
    plan = tiles.tiling(1280, 12288, SIDE)
    end = plan.regions[0][3]
    cut = (100, end - 9, 1100, end)  # the first tile's: the top of a line 20 high, cut by its edge
    whole = [(104, end - 7, 600, end + 13), (620, end - 8, 1080, end + 12)]  # the next tile's, in two words
    joined = db.merge_rows(tiles.stitch(plan, [[cut], whole, *([] for _ in plan.regions[2:])]))
    assert sorted(joined) == whole


def test_a_box_that_stops_just_short_of_a_tile_s_edge_was_cut_by_it():
    """The detector's box of what a tile's edge cuts may stop a pixel or two short of the edge: it is a cut part all
    the same, left out where the other tile holds whole the lines it lies across, never read as a line of its own."""
    plan = tiles.tiling(1280, 12288, SIDE)
    end = plan.regions[0][3]
    short = (100, end - 12, 1100, end - 1)  # the first tile's, a pixel short of its edge
    lines = [(100, end - 20, 1100, end - 7), (100, end - 6, 1100, end + 10)]  # the second tile's, whole
    joined = db.merge_rows(tiles.stitch(plan, [[short], lines, *([] for _ in plan.regions[2:])]))
    assert sorted(joined) == lines


def test_a_line_both_tiles_hold_whole_is_kept_once_however_each_boxed_it():
    """The two tiles' boxes of one line in the band they share differ (one boxed it taller): kept once, as one box
    holding both, where joining a row's boxes alone would keep both and read the line twice."""
    plan = tiles.tiling(1280, 12288, SIDE)
    start, end = plan.regions[1][1], plan.regions[0][3]
    middle = (start + end) // 2
    mine, theirs = (100, middle - 15, 1100, middle + 15), (110, middle - 8, 1090, middle + 8)
    assert db.merge_rows([mine, theirs]) == [mine, theirs]  # too unlike in height to be one row's words
    joined = db.merge_rows(tiles.stitch(plan, [[mine], [theirs], *([] for _ in plan.regions[2:])]))
    assert joined == [mine]


def test_parts_of_two_lines_cut_by_the_band_s_edges_stay_two():
    """Parts cut by the two edges of a band are of one line only when they hold the same stretch of the band (half of
    it at least) and lie across from each other: two tall lines that meet in the band, one above the other, or two
    columns side by side, stay apart."""
    plan = tiles.tiling(3000, 12288, SIDE)  # tiles as wide as the image, read at about two thirds of their size
    assert plan.columns == 1
    start, end = plan.regions[1][1], plan.regions[0][3]
    band = end - start
    above = (100, start, 1900, start + band // 2 + 10)  # the second tile's part of a line from far above
    below = (100, start + band // 2 - 10, 1900, end)  # the first tile's part of a line going far below
    rest = [*([] for _ in plan.regions[2:])]
    assert sorted(tiles.stitch(plan, [[below], [above], *rest])) == sorted([below, above])
    left = (100, start + 10, 1900, end)  # cut by the first tile's end
    right = (1800, start, 2900, end - 10)  # cut by the second tile's start, a column to the right
    assert sorted(tiles.stitch(plan, [[left], [right], *rest])) == sorted([left, right])


def test_stitch_keeps_each_tile_s_boxes_of_an_image_read_whole():
    plan = tiles.tiling(1920, 1080, SIDE)
    assert tiles.stitch(plan, [[(10, 10, 300, 30), (10, 40, 300, 60)]]) == [(10, 10, 300, 30), (10, 40, 300, 60)]


# --- bounded --------------------------------------------------------------------------------------------------------


def test_a_read_in_tiles_stops_once_it_has_run_its_budget():
    """A server's space reads for a bounded time: the time is checked between tiles too, so that a tall image's tiles
    stop with the budget, and the next read has its turn."""
    ocr = _ocr(det=_Session(_ink, seconds=0.2))
    started = time.monotonic()
    with pytest.raises(OcrTooLong):
        ocr.read(Image.new("RGB", (640, 16_000), "white"), budget=0.3)  # 9 tiles
    assert time.monotonic() - started < 2 and ocr._det.runs <= 3
    assert ocr._lock.acquire(0)
    ocr._lock.release()


def _png(image) -> str:
    out = io.BytesIO()
    image.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def _ref(image) -> ImageRef:
    return ImageRef({"data": _png(image)}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)


def test_a_line_joined_from_tiles_too_long_for_its_height_withholds_its_image():
    """A line no tile holds whole is read as the line it is: longer for its height than the recognizer reads, it is not
    read, and its image is withheld (as Task 11 has it), before the recognizer runs."""
    image = Image.fromarray(_bars(20_000, 40, [(500, 17, 19_500, 23)]))
    assert 2 < len(tiles.tiling(20_000, 40, SIDE).regions) <= MAX_TILES
    ocr = _ocr()
    with pytest.raises(OcrTooWide):
        ocr.read(image)
    work = ImageMasking(ocr).work(_ref(image))
    assert (work.state, work.reason) == (WITHHELD, "its text could not be read (OcrTooWide)")
    assert ocr._rec.runs == 0


def _tiles_for(long):
    return len(tiles.tiling(64, long, SIDE).regions)


def test_an_image_past_the_tile_cap_is_not_read_and_takes_no_turn():
    """One image takes at most ``MAX_TILES`` tiles, and so at most that many times what one detection costs: an image
    longer than that for its width is refused before anything is read, without holding the turn every space shares."""
    longest = next((n for n in range(28_000, 30_000) if _tiles_for(n + 1) > MAX_TILES), None)
    assert longest is not None and _tiles_for(longest) == MAX_TILES
    holder = _ocr()
    assert holder._lock.acquire(0)  # another read holds the turn: refused at once all the same
    try:
        started = time.monotonic()
        with pytest.raises(OcrTooManyTiles):
            holder.read(Image.new("RGB", (64, longest + 1), "white"), wait=1)
        assert holder._det.runs == 0 and time.monotonic() - started < 0.5
    finally:
        holder._lock.release()
    ocr = _ocr()
    ocr.read(Image.new("RGB", (64, longest), "white"))
    assert ocr._det.runs == MAX_TILES


@pytest.mark.parametrize("space", [False, True])
def test_an_image_past_the_tile_cap_is_withheld_never_sent_unread(space):
    """On a person's computer and in a server's space alike: withheld, saying why as an image whose text could not be
    read says it."""
    ocr = _ocr()
    service = ImageMasking(SpaceReads(ocr, wait=0.2, budget=5), max_pixels=SPACE_MAX_PIXELS) if space else None
    work = (service or ImageMasking(ocr)).work(_ref(Image.new("RGB", (64, 40_000), "white")))
    assert (work.state, work.reason) == (WITHHELD, "its text could not be read (OcrTooManyTiles)")
    assert ocr._det.runs == 0


def test_a_space_s_tall_image_waits_its_turn_like_any_other():
    """A tall image within the cap takes the turn as any image does: one identity's tiles never run alongside another's
    read."""
    go = threading.Event()
    slow = _Session(_ink)
    original = slow.run

    def held(names, inputs, options=None):
        assert go.wait(10)
        return original(names, inputs, options)

    slow.run = held
    ocr = _ocr(det=slow)
    first = threading.Thread(target=ocr.read, args=(Image.new("RGB", (640, 16_000), "white"),))
    first.start()
    time.sleep(0.2)
    with pytest.raises(OcrBusy):
        ocr.read(Image.new("RGB", (300, 200), "white"), wait=0.2)
    go.set()
    first.join(10)
    assert slow.runs == len(tiles.tiling(640, 16_000, SIDE).regions)
