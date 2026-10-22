"""Covering text in an image: a box of the background's colour with the fake written over it, or a bar.

The fake is drawn as large as the text it covers, in the text's colour, with a font whose letters
are hard to confuse (0 and O, 1, l and I), so the model reads it exactly and the answer restores it.
It is never cut short: when it is longer than the original it takes the free space to the right,
then it is squeezed, then the font gets smaller.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from latet.vision.layout import Cover

if TYPE_CHECKING:
    from PIL import Image, ImageFont

Rect = tuple[float, float, float, float]
RGB = tuple[int, int, int]

# IBM Plex Sans (SIL Open Font License 1.1, fonts/OFL.txt): it has the accented letters Pillow's own
# font lacks, and its O is wider than its 0 (notes/spike-ocr.md, Fonts). Absent, Pillow's own font.
FONT = Path(__file__).parent / "fonts" / "IBMPlexSans-Regular.ttf"
MIN_SQUEEZE = 0.6
MIN_SIZE = 8


@lru_cache(maxsize=64)
def font_at(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from PIL import ImageFont

    if FONT.is_file():
        return ImageFont.truetype(str(FONT), size)
    return ImageFont.load_default(size)


def _box(rect: Rect, image: Image.Image) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    return (
        max(0, int(np.floor(x0))),
        max(0, int(np.floor(y0))),
        min(image.width, int(np.ceil(x1))),
        min(image.height, int(np.ceil(y1))),
    )


def colours(image: Image.Image, line: Rect) -> tuple[RGB, RGB]:
    """The background (the commonest colour) and the text (the far colours) of a line."""
    region = np.asarray(image.crop(_box(line, image)).convert("RGB")).reshape(-1, 3).astype(np.int32)
    if not len(region):
        return (255, 255, 255), (0, 0, 0)
    quantised = region // 16
    keys = quantised[:, 0] * 256 + quantised[:, 1] * 16 + quantised[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    background = region[keys == values[counts.argmax()]].mean(axis=0)
    bg: RGB = (int(background[0]), int(background[1]), int(background[2]))
    distance = np.abs(region - np.array(bg)).sum(axis=1)
    far = region[distance > 0.6 * distance.max()] if distance.max() > 60 else region[:0]
    if len(far) < 3:
        return bg, ((0, 0, 0) if sum(bg) > 382 else (255, 255, 255))
    text = far.mean(axis=0)
    return bg, (int(text[0]), int(text[1]), int(text[2]))


def ink(image: Image.Image, line: Rect, bg: RGB) -> tuple[int, int] | None:
    """Top and bottom of the letters in a line's box (the box may reach into the lines around it)."""
    x0, y0, x1, y1 = _box(line, image)
    region = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB")).astype(np.int32)
    if not region.size:
        return None
    distance = np.abs(region - np.array(bg)).sum(axis=2)
    if distance.max() < 60:
        return None
    inked = (distance > 0.4 * distance.max()).any(axis=1).tolist()
    runs: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for y, on in enumerate([*inked, False, False]):  # gaps of one pixel stay inside a run
        if on:
            start = y if start is None else start
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 1:
                runs.append((start, y - gap + 1))
                start, gap = None, 0
    if not runs:
        return None
    middle = len(inked) / 2
    top, bottom = min(runs, key=lambda r: 0 if r[0] <= middle < r[1] else min(abs(r[0] - middle), abs(r[1] - middle)))
    return y0 + top, y0 + bottom


def free_right(image: Image.Image, rect: Rect, line: Rect, bg: RGB) -> float:
    """How far right of ``rect`` the line holds only background (where a longer fake may go)."""
    x0, y0, x1, y1 = _box((rect[2], line[1], line[2] + (line[3] - line[1]) * 4, line[3]), image)
    region = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB")).astype(np.int32)
    if not region.size:
        return 0.0
    busy = (np.abs(region - np.array(bg)).sum(axis=2) > 60).any(axis=0)
    hits = np.nonzero(busy)[0]
    reach = int(hits[0]) if len(hits) else region.shape[1]
    return max(0.0, reach - (line[3] - line[1]) * 0.5)  # keep half a line from the next text


def fitted(text: str, width: float, ink_height: float, reference: str = "Hg"):
    """A font sized to the line's letters (``reference`` spans what its ink spans), and the squeeze
    (at most 1) that fits ``width``."""
    _, top, _, bottom = font_at(100).getbbox(reference)
    size = max(MIN_SIZE, round(100 * ink_height / max(1, bottom - top)))
    while True:
        font = font_at(size)
        length = font.getlength(text)
        squeeze = min(1.0, width / length) if length else 1.0
        if squeeze >= MIN_SQUEEZE or size <= MIN_SIZE:
            return font, squeeze
        size -= 1


def paint(image: Image.Image, covers: list[Cover]) -> Image.Image:
    """A copy of ``image`` with every cover drawn; the image itself is left as it is."""
    from PIL import Image, ImageDraw

    out = image.copy()
    draw = ImageDraw.Draw(out)
    for cover in covers:
        bg, fg = colours(image, cover.line)
        x0, y0, x1, y1 = _box(cover.rect, image)
        if x1 <= x0 or y1 <= y0:
            continue
        if cover.text is None:
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fg)
            continue
        span = ink(image, cover.line, bg) or (y0, y1)
        font, squeeze = fitted(cover.text, x1 - x0, span[1] - span[0], cover.reference)
        if squeeze < 1:  # a longer fake: first the free space to the right
            room = x1 - x0 + free_right(image, cover.rect, cover.line, bg)
            font, squeeze = fitted(cover.text, room, span[1] - span[0], cover.reference)
        ref_top = font.getbbox(cover.reference)[1]
        right, bottom = font.getbbox(cover.text)[2:]
        stamp = Image.new(out.mode, (max(1, int(right)), max(1, int(bottom) + 2)), bg)
        ImageDraw.Draw(stamp).text((0, 0), cover.text, font=font, fill=fg)
        stamp = stamp.crop((0, int(ref_top), stamp.width, stamp.height))
        if squeeze < 1:
            stamp = stamp.resize((max(1, int(stamp.width * squeeze)), stamp.height), Image.Resampling.LANCZOS)
        left = max(x0, round(cover.start)) if cover.start is not None else x0
        draw.rectangle((x0, y0, max(x1, left + stamp.width) - 1, y1 - 1), fill=bg)
        out.paste(stamp, (left, span[0]))
    return out
