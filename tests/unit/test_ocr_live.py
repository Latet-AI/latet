"""The real text recognition models, when they are on this machine (``latet ocr install``, or
LATET_OCR_DATA_DIR naming the data folder that holds them).

Never in CI: the models are a download.
"""

from __future__ import annotations

import base64
import io
import os
import time
from itertools import pairwise
from pathlib import Path

import numpy as np
import platformdirs
import pytest
from PIL import Image, ImageDraw, ImageFont

from latet.engine.types import SlotKind
from latet.protocols.base import ImageRef
from latet.vision import models, tiles
from latet.vision.codec import decode
from latet.vision.masking import MASKED, WITHHELD, ImageMasking, covers

from ..fake_ocr import SCREENSHOT, reading

pytestmark = pytest.mark.live


def _data() -> Path:
    """The data folder that holds the models, skipped when they are not there."""
    configured = os.environ.get("LATET_OCR_DATA_DIR")
    data = Path(configured) if configured else Path(platformdirs.user_data_dir("latet", appauthor=False))
    if not models.runtime_available() or not models.installed(data):
        pytest.skip("text recognition models not installed")
    return data


@pytest.fixture(scope="module")
def ocr():
    from latet.vision.ocr import OnnxOcr

    reader = OnnxOcr(*models.folders(_data()))
    assert reader.wait_ready(), reader.error
    return reader


@pytest.fixture(scope="module")
def space_reads():
    """The models read as a server's space reads them: through the instance's pool (``shared_pool``: its readers each
    with their share of this computer's processors, batches of 16,384 columns, no arena, no spinning), with the default
    wait and budget."""
    from latet.vision.pool import SPACE_READ_SECONDS, SPACE_READERS, SPACE_WAIT_SECONDS, SpaceReads, shared_pool

    pool = shared_pool(*models.folders(_data()), SPACE_READERS)
    assert pool.wait_ready() and all(reader.ready for reader in pool.readers), pool.error
    return SpaceReads(pool, SPACE_WAIT_SECONDS, SPACE_READ_SECONDS)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """A system font with accented letters (Pillow's own has none)."""
    for name in ("arial.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    pytest.skip("no font with accented letters on this machine")


def _screen() -> Image.Image:
    image = Image.new("RGB", (900, 140), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    font = _font(22)
    draw.text((20, 20), "Author: Niccolò Bianchi <niccolo.bianchi@studio-verdi.it>", font=font, fill=(220, 220, 220))
    draw.text((20, 70), "IBAN IT60 X054 2811 1010 0000 0123 456", font=font, fill=(220, 220, 220))
    return image


def test_reads_lines_in_order_with_accents(ocr):
    result = ocr.read(_screen())
    assert result.text.splitlines() == [
        "Author: Niccolò Bianchi <niccolo.bianchi@studio-verdi.it>",
        "IBAN IT60 X054 2811 1010 0000 0123 456",
    ]


def test_the_characters_are_where_the_image_has_them(ocr):
    image = _screen()
    result = ocr.read(image)
    start = result.text.index("niccolo.bianchi@")
    [(_, rect)] = result.cover(start, start + len("niccolo.bianchi@studio-verdi.it"))
    font = _font(22)
    left = 20 + font.getlength("Author: Niccolò Bianchi <")
    right = left + font.getlength("niccolo.bianchi@studio-verdi.it")
    char = font.getlength("n")
    assert abs(rect[0] - left) < char and abs(rect[2] - right) < char


def test_the_same_image_reads_the_same(ocr):
    assert ocr.read(_screen()) == ocr.read(_screen())


def test_the_tutorial_s_screenshot_reads_as_the_tests_fake_reader_says(ocr):
    """The models read `payment.png` as tests/data/tutorial/payment-ocr.json says, which the tests' fake reader reads it
    as: the same text, each box and character edge within two pixels."""
    expected = reading()
    got = ocr.read(Image.open(io.BytesIO(SCREENSHOT.read_bytes())).convert("RGB"))
    assert got.text == expected.text
    for line, want in zip(got.lines, expected.lines, strict=True):
        assert all(abs(a - b) <= 2 for a, b in zip(line.box, want.box, strict=True)), line.text
        assert all(abs(a - b) <= 2 for a, b in zip(line.edges, want.edges, strict=True)), line.text


# --- tall and wide screenshots: read in tiles ---------------------------------------------------------------------


def _page(
    width: int, height: int, places: list[tuple[int, int]], words: bool = True, font=None
) -> tuple[Image.Image, list[tuple[str, tuple]]]:
    """A screenshot as Task 16's measure drew them: on white, in Pillow's own font of 22 pixels (or ``font``), a line
    with an address at each of ``places`` (where it starts, its top), or the address alone; and each address with where
    it is drawn."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = font or ImageFont.load_default(size=22)
    drawn = []
    for n, (left, top) in enumerate(places):
        before, email = (f"Invoice {n}: write to " if words else ""), f"emily.carter{n}@acme.com"
        draw.text((left, top), f"{before}{email}" + (f" about account 99-{n:04d}" if words else ""), "black", font)
        drawn.append((email, draw.textbbox((left + font.getlength(before), top), email, font=font)))
    return image, drawn


def _png(image: Image.Image) -> str:
    out = io.BytesIO()
    image.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def _sent(ocr, masker, image: Image.Image, drawn: list[tuple[str, tuple]], space: bool = False) -> None:
    """``image`` as Latet sends it: every address in it read, masked and painted over, so that none leaves as it came;
    ``space``: as a server's space sends it (its ceiling of pixels, its bounded reads)."""
    from latet.vision.pool import SPACE_MAX_PIXELS

    original = _png(image)
    ref = ImageRef({"data": original}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)
    masking = ImageMasking(ocr, max_pixels=SPACE_MAX_PIXELS, space=True) if space else ImageMasking(ocr)
    work = masking.work(ref)
    assert work.state != WITHHELD, work.reason
    emails = [email for email, _ in drawn]
    assert [email for email in emails if email not in work.text] == []
    done = masker.mask(work.text, SlotKind.IMAGE_TEXT)
    assert set(emails) <= {r.original for r in done.replacements}
    work.masked("text", done.text, done.replacements)
    assert work.state == MASKED and ref.data() != original
    assert work.result is not None
    covered = np.zeros((image.height, image.width), bool)
    for cover in covers(work.result, done.replacements):
        x0, y0, x1, y1 = cover.rect
        covered[int(y0) : int(np.ceil(y1)), int(x0) : int(np.ceil(x1))] = True
    for email, (x0, y0, x1, y1) in drawn:  # each address's ink under what is painted
        assert covered[int(y0) : int(y1), int(x0) : int(x1)].mean() > 0.9, email
    assert decode(ref.data()).image.size == image.size


@pytest.mark.parametrize(
    ("width", "height", "places"),
    [
        (1280, 12288, [(20, 4 + 34 * n) for n in range(28)]),  # Task 16's: 28 lines of addresses at the top
        (640, 16000, [(20, 4 + 34 * n) for n in range(400)][::15]),  # its column of lines down a page, one in 15
        (640, 16000, [(20, 4 + 571 * n) for n in range(28)]),  # a line every 571 pixels, down to the bottom
    ],
)
def test_a_tall_screenshot_s_addresses_are_read_and_masked_however_tall(ocr, masker, width, height, places):
    """Task 16's finding: the detector read a 1280 by 12,288 screenshot at a sixth of its size and a 640 by 16,000 one at
    an eighth, found no text, and both left as they came. Read in tiles, every address is read and masked."""
    _sent(ocr, masker, *_page(width, height, places))


def test_an_address_across_a_tiles_edge_is_read_and_masked(ocr, masker):
    """A line across where a tile ends, or where the next one starts, is read whole in the tile that holds it."""
    plan = tiles.tiling(640, 12288, ocr.MAX_SIDE)
    assert len(plan.regions) > 2
    places = []
    for this, following in pairwise(plan.regions):
        places += [(20, following[1] - 11), (20, this[3] - 11), (20, (following[1] + this[3]) // 2 - 11)]
    _sent(ocr, masker, *_page(640, 12288, places))


def test_a_very_long_low_screenshot_s_addresses_are_read_and_masked(ocr, masker):
    """The same rule along a wide image's width: 16,000 by 160, four rows of addresses side by side, each row a fourth
    of the way along from the one above, so that wherever a tile's edge falls it cuts an address."""
    places = [(40 + 200 * row + 800 * k, 4 + 40 * row) for row in range(4) for k in range(19)]
    image, drawn = _page(16000, 160, places, words=False)
    _sent(ocr, masker, image, drawn)
    plan = tiles.tiling(16000, 160, ocr.MAX_SIDE)
    edges = [region[0] for region in plan.regions[1:]] + [region[2] for region in plan.regions[:-1]]
    assert plan.axis == 0 and len(edges) > 4
    assert all(any(x0 + 20 < edge < x1 - 20 for _, (x0, _, x1, _) in drawn) for edge in edges)


def test_a_large_screen_s_small_text_is_read_and_masked(ocr, masker):
    """A 5K2K screen's screenshot (5120 by 2160) with text of 13 pixels in lines 20 apart, as a screen at 100% shows
    it: read whole at 0.4, such text loses about half of its addresses. Read in tiles, all 28 are found and covered."""
    places = [(20 + 1270 * column, 150 + 450 * column + 20 * row) for column in range(4) for row in range(7)]
    _sent(ocr, masker, *_page(5120, 2160, places, font=_font(13)))


@pytest.mark.parametrize("screen", ["4K at 100%", "5120x2160 at 100%"])
def test_a_dense_large_screen_is_read_within_a_servers_budget(space_reads, masker, screen):
    """A dense screenshot of a large screen, read as a server's space reads it (the instance's pool of readers, a
    space's masking, the default budget and wait): every address read and covered, within the budget."""
    from latet.vision.pool import SPACE_READ_SECONDS

    width, height = (3840, 2160) if screen.startswith("4K") else (5120, 2160)
    places = [(20 + (width // 4) * column, 150 + 400 * column + 20 * row) for column in range(4) for row in range(7)]
    started = time.monotonic()
    _sent(space_reads, masker, *_page(width, height, places, font=_font(13)), space=True)
    assert time.monotonic() - started < SPACE_READ_SECONDS


def test_an_image_too_large_both_ways_is_read_in_a_grid_and_its_small_text_masked(ocr, masker):
    """5000 by 5000, text of 13 pixels: read whole at 0.41, now in a grid of tiles at three quarters of its size. Lines
    across the edges of a row's tiles and of the rows, at the corner where four tiles meet, in the square they all
    share, and a block of lines 20 apart across the rows' band, are each read whole and covered."""
    plan = tiles.tiling(5000, 5000, ocr.MAX_SIDE)
    assert plan.columns > 1 and plan.rows > 1
    x_start, x_end = plan.regions[1][0], plan.regions[0][2]
    y_start, y_end = plan.regions[plan.columns][1], plan.regions[0][3]
    places = [(x_start - 300, 600), (x_end - 200, 900), (x_start - 300, 4300), (x_end - 200, 4600)]  # a row's edges
    places += [(300, y_start - 6), (300, y_end - 6), (3600, y_start - 6), (3600, y_end - 6)]  # the rows' edges
    places += [(x_start - 300, y_end - 6), (x_start + 20, (y_start + y_end) // 2), (x_end - 200, y_start - 6)]
    places += [(300, 300), (3600, 4800)]  # far from every edge
    places += [(1000, y_start - 140 + 20 * row) for row in range(14)]  # a block of lines across the rows' band
    _sent(ocr, masker, *_page(5000, 5000, places, font=_font(13)))
