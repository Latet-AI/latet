"""The OCR runtime's parts that need no model: boxes from the detector's map, characters from the
recognizer's frames, lines in reading order, and the rectangles over a stretch of text."""

from __future__ import annotations

import time

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from latet.vision import db
from latet.vision.layout import OcrLine, join, reading_order
from latet.vision.ocr import decode


def test_components_join_runs_that_touch_even_diagonally():
    mask = np.zeros((6, 10), bool)
    mask[1, 1:3] = True
    mask[2, 3:5] = True  # touches the run above at a corner
    mask[4, 7:9] = True  # apart
    assert sorted(db.components(mask)) == [(1, 1, 5, 3), (7, 4, 9, 5)]
    assert db.components(np.zeros((3, 3), bool)) == []


def test_boxes_keep_confident_regions_and_expand_them():
    prob = np.zeros((40, 100), np.float32)
    prob[10:20, 10:60] = 0.9  # a line of text
    prob[30:33, 70:72] = 0.9  # too small
    prob[25:35, 80:95] = 0.4  # below the threshold that starts a region? no: above 0.3, below box_thresh
    [box] = db.boxes(prob, thresh=0.3, box_thresh=0.6, unclip=1.5)
    d = 50 * 10 * 1.5 / (2 * 60)
    assert box == pytest.approx((10 - d, 10 - d, 60 + d, 20 + d))


def test_merge_rows_joins_word_boxes_and_keeps_columns_apart():
    words = [(10, 10, 50, 30), (58, 11, 120, 31), (128, 10, 160, 30)]  # gaps of 8: words of one line
    column = (400, 10, 500, 30)  # far right: another column
    below = (10, 40, 50, 60)  # the next line
    merged = db.merge_rows([*words, column, below])
    assert sorted(merged) == [(10, 10, 160, 31), (10, 40, 50, 60), (400, 10, 500, 30)]


def _merge_rows_as_it_was(found, gap=0.6):
    """``db.merge_rows`` as it was written first: each box compared with every box kept before it."""
    out: list[list[int]] = []
    for box in sorted(found, key=lambda b: b[0]):
        h = box[3] - box[1]
        for other in out:
            oh = other[3] - other[1]
            shared = min(box[3], other[3]) - max(box[1], other[1])
            if shared < 0.6 * min(h, oh) or max(h, oh) > 1.5 * min(h, oh):
                continue
            if box[0] - other[2] <= gap * min(h, oh):
                other[0] = min(other[0], box[0])
                other[1] = min(other[1], box[1])
                other[2] = max(other[2], box[2])
                other[3] = max(other[3], box[3])
                break
        else:
            out.append(list(box))
    return [(b[0], b[1], b[2], b[3]) for b in out]


_BOX = st.tuples(st.integers(0, 600), st.integers(0, 400), st.integers(0, 120), st.integers(0, 40)).map(
    lambda b: (b[0], b[1], b[0] + b[2], b[1] + b[3])
)


@given(st.lists(_BOX, max_size=60))
def test_merge_rows_joins_exactly_as_it_did_comparing_every_pair(found):
    """It compares a box only with those kept that it could join (sharing rows of pixels, near enough along them), so
    that the boxes of a page join in a time that grows with their number, not its square: what it joins is what it
    joined comparing every pair."""
    assert db.merge_rows(found) == _merge_rows_as_it_was(found)


def test_merge_rows_joins_a_box_that_grew_into_rows_it_did_not_span_at_first():
    """A box grows as it joins (each box it joins may be half again as tall): it is looked up by the rows it spans now,
    so that one that shares with it only rows it grew into is joined, as comparing every pair joined it."""
    found = [(0, 0, 10, 10), (10, 4, 20, 19), (20, 7, 30, 35), (30, 14, 40, 66), (40, 26, 50, 125), (50, 50, 60, 237)]
    assert db.merge_rows(found) == _merge_rows_as_it_was(found) == [(0, 0, 60, 237)]


def test_merge_rows_takes_a_time_that_grows_with_the_boxes_not_their_square():
    """A tall page read in tiles can hold tens of thousands of boxes (small words far apart): 4,000 took 1.9 s compared
    pair by pair, 40,000 about 3.5 minutes, holding the text recognition a server's spaces share."""
    found = [(30 * (n % 42), 20 * (n // 42), 30 * (n % 42) + 12, 20 * (n // 42) + 14) for n in range(40_000)]
    started = time.monotonic()
    assert len(db.merge_rows(found)) == 40_000
    assert time.monotonic() - started < 10


def _probs(frames: list[int], classes: int = 6) -> np.ndarray:
    out = np.full((len(frames), classes), 0.01, np.float32)
    for t, c in enumerate(frames):
        out[t, c] = 0.9
    return out


def test_decode_reads_ctc_frames_and_places_each_character():
    chars = ["", "a", "b", "c", "d", " "]
    # 10 frames over a padded input of 80 px; the line is 40 px wide in it and 100 px wide in the image
    probs = _probs([1, 1, 0, 1, 2, 0, 0, 0, 0, 0])  # "a" twice (blank between), then "b"
    line = decode(probs, chars, (100, 5, 200, 25), width=80, resized=40)
    assert line.text == "aab"
    per_frame = 80 / 10 * 100 / 40  # 20 image pixels a frame
    middles = [100 + 1 * per_frame, 100 + 3.5 * per_frame, 100 + 4.5 * per_frame]
    assert line.edges[1] == pytest.approx((middles[0] + middles[1]) / 2)
    assert len(line.edges) == 4 and line.edges[0] >= 100 and line.edges[-1] <= 200


def test_decode_trims_spaces_and_handles_nothing_read():
    chars = ["", "a", "b", "c", "d", " "]
    line = decode(_probs([5, 1, 2, 5, 0]), chars, (0, 0, 50, 10), width=50, resized=50)
    assert line.text == "ab" and len(line.edges) == 3
    assert decode(_probs([0, 0, 0]), chars, (0, 0, 50, 10), 50, 50).text == ""


def _line(x0: int, y0: int, text: str, char: float = 10.0, height: int = 20) -> OcrLine:
    edges = tuple(x0 + i * char for i in range(len(text) + 1))
    return OcrLine((x0, y0, int(x0 + char * len(text)), y0 + height), text, edges)


def test_lines_join_in_reading_order_with_spaces_tabs_and_newlines():
    lines = [
        _line(300, 52, "x@acme.it"),  # same row as "Email:", a column away
        _line(10, 50, "Email:"),
        _line(80, 51, "Mario"),  # a word gap away
        _line(10, 90, "Phone: +39 333 1234567"),
    ]
    result = join(lines, 600, 200)
    assert result.text == "Email: Mario\tx@acme.it\nPhone: +39 333 1234567"
    assert result.starts == (0, 7, 13, 23)


def test_a_sidebar_does_not_swallow_two_lines_of_the_text_beside_it():
    sidebar = OcrLine((0, 57, 60, 84), "client.py", tuple(float(i) for i in range(10)))
    two = _line(236, 56, "2", height=18)
    three = _line(236, 76, "3", height=15)
    rows = reading_order([sidebar, two, three])
    assert [[x.text for x in row] for row in rows] == [["client.py", "2"], ["3"]]


def test_cover_spans_lines_and_pads_a_little():
    result = join([_line(10, 10, "Mario Rossi"), _line(10, 40, "via Roma 1")], 400, 100)
    start = result.text.index("Rossi")
    [(i, rect)] = result.cover(start, start + 5)
    assert i == 0 and rect == pytest.approx((10 + 60 - 3, 10, 10 + 110 + 3, 30))
    both = result.cover(start, len(result.text))  # "Rossi\nvia Roma 1"
    assert [i for i, _ in both] == [0, 1] and both[1][1][0] == pytest.approx(10 - 3)


def _reading_order_as_it_was(lines):
    """``reading_order`` as it was written first: each line compared with every row made before it."""
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda x: ((x.box[1] + x.box[3]) / 2, x.box[0])):
        middle = (line.box[1] + line.box[3]) / 2
        for row in rows:
            first = row[0]
            if abs(middle - (first.box[1] + first.box[3]) / 2) <= 0.5 * min(first.height, line.height):
                row.append(line)
                break
        else:
            rows.append([line])
    return [sorted(row, key=lambda x: x.box[0]) for row in rows]


def _cover_as_it_was(result, start, end):
    """``OcrResult.cover`` as it was written first: every line of the text looked at."""
    out = []
    for i, (line, at) in enumerate(zip(result.lines, result.starts, strict=True)):
        a, b = max(start, at), min(end, at + len(line.text))
        if a >= b:
            continue
        pad = 0.3 * line.char_width()
        out.append(
            (
                i,
                (
                    max(0.0, line.edges[a - at] - pad),
                    float(line.box[1]),
                    min(float(result.width), line.edges[b - at] + pad),
                    float(line.box[3]),
                ),
            )
        )
    return out


_LINE = st.tuples(st.integers(0, 500), st.integers(0, 300), st.integers(1, 12), st.integers(1, 40)).map(
    lambda v: _line(v[0], v[1], "x" * v[2], height=v[3])
)


@given(st.lists(_LINE, max_size=40), st.integers(-5, 400), st.integers(0, 60))
def test_reading_order_and_cover_are_what_they_were_comparing_everything(lines, start, length):
    """A line is compared only with the rows it could join (their first line less than half a line above it), and a
    stretch of the text only with the lines it can touch: the rows and the rectangles are what they were."""
    assert reading_order(lines) == _reading_order_as_it_was(lines)
    result = join(lines, 600, 400)
    assert result.cover(start, start + length) == _cover_as_it_was(result, start, start + length)


def test_the_lines_of_a_tall_page_are_ordered_and_covered_in_a_time_that_grows_with_them():
    """A tall page read in tiles can hold thousands of lines: compared every row with every line, and every value with
    every line, 20,000 lines with a value on one line in ten took 70 s; with one on every line, minutes."""
    lines = [_line(10, 20 * n, "emily.carter@acme.com") for n in range(20_000)]
    started = time.monotonic()
    result = join(lines, 400, 20 * 20_000)
    assert len(result.lines) == 20_000 and result.text.count("\n") == 19_999
    for n in range(20_000):  # a value on every line
        at = result.starts[n]
        assert [i for i, _ in result.cover(at, at + 5)] == [n]
    assert time.monotonic() - started < 15
