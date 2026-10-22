"""The tutorial's screenshot, `src/latet/tutorial/assets/payment.png`: a refused payment, with an invented e-mail
address and phone number and the documented example IBAN (which belongs to no bank), drawn with the font Latet paints
fakes with; and what the text recognition models read in it, `tests/data/tutorial/payment-ocr.json`, which the tests'
fake reader reads it as.

    uv run python scripts/tutorial_screenshot.py              # draws the screenshot
    uv run python scripts/tutorial_screenshot.py --read DATA  # and records what the models in DATA/models read in it

The recording says which picture it was made from (`pixels`: the sha256 of the picture's RGB pixels, which a test
checks, so a picture drawn again without `--read` fails it) and with which models (`models`: their pinned revisions).
A live test (tests/unit/test_ocr_live.py) checks that the models still read it so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from latet.vision import models
from latet.vision.ocr import OnnxOcr
from latet.vision.paint import FONT

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT = ROOT / "src" / "latet" / "tutorial" / "assets" / "payment.png"
READING = ROOT / "tests" / "data" / "tutorial" / "payment-ocr.json"
SIZE = (1100, 420)
# Top to bottom, each with its size: the title, then the payment's details.
LINES = [
    ("Payment refused", 40),
    ("Amount: £1,240.00", 28),
    ("To account: GB82 WEST 1234 5698 7654 32", 28),
    ("Reason: the receiving bank closed this account on 2 March.", 28),
    ("Customer: amelia.hughes@acme.com", 28),
    ("Phone: +44 20 7946 0321", 28),
]


def draw() -> Image.Image:
    image = Image.new("RGB", SIZE, "white")
    pen = ImageDraw.Draw(image)
    y = 30
    for text, size in LINES:
        # The basic layout, so the picture is the same with Raqm installed or not.
        font = ImageFont.truetype(str(FONT), size, layout_engine=ImageFont.Layout.BASIC)
        pen.text((40, y), text, font=font, fill=(20, 20, 20))
        y += 60 if size > 30 else 52
    return image


def pixels(image: Image.Image) -> str:
    """The sha256 of the picture's RGB pixels: how the tests' fake reader (tests/fake_ocr.py) knows it."""
    return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def load_models(data: Path) -> OnnxOcr:
    """The text recognition models in ``data``, loaded: or the script stops, before it draws anything."""
    if not models.runtime_available() or not models.installed(data):
        raise SystemExit(f"no text recognition models in {data}: `latet ocr install`, or name the folder that has them")
    reader = OnnxOcr(*models.folders(data))
    if not reader.wait_ready():
        raise SystemExit(f"the models did not load: {reader.error}")
    return reader


def read(reader: OnnxOcr) -> dict[str, Any]:
    image = Image.open(SCREENSHOT).convert("RGB")
    result = reader.read(image)
    lines = [
        {
            "box": list(line.box),
            "text": line.text,
            "edges": [round(e, 2) for e in line.edges],
            "score": round(line.score, 4),
        }
        for line in result.lines
    ]
    return {
        "pixels": pixels(image),
        "models": {spec.name: spec.revision for spec in models.MODELS},
        "width": result.width,
        "height": result.height,
        "lines": lines,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="The tutorial's screenshot, and what text recognition reads in it.")
    parser.add_argument("--read", type=Path, metavar="DATA", help="a data folder holding the text recognition models")
    args = parser.parse_args()
    # Before drawing: when the models cannot read it, the picture that is there stays as it is.
    reader = load_models(args.read) if args.read is not None else None
    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
    draw().save(SCREENSHOT, optimize=True)
    print(f"drew {SCREENSHOT.relative_to(ROOT)}")
    if reader is not None:
        READING.parent.mkdir(parents=True, exist_ok=True)
        READING.write_bytes((json.dumps(read(reader), indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        print(f"recorded {READING.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
