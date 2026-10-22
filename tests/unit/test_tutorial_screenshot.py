"""The tutorial's screenshot (`payment.png`) and what the text recognition models read in it, which the tests' fake
reader reads it as: the recording is of this very picture, the three values (an invented e-mail address and phone
number, and the documented example IBAN, which belongs to no bank) are there, whole, and Latet's detectors find each
one."""

import io
import json

from PIL import Image

from latet.config import Config
from latet.engine.build import build
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.pii import iban_valid
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.tutorial.sandbox import BASE

from ..fake_ocr import READING, SCREENSHOT, _pixels, payment_reader, reading

EMAIL, IBAN, PHONE = "amelia.hughes@acme.com", "GB82 WEST 1234 5698 7654 32", "+44 20 7946 0321"


def test_the_fake_reader_reads_the_screenshot_as_the_models_did():
    image = Image.open(io.BytesIO(SCREENSHOT.read_bytes())).convert("RGB")
    assert payment_reader().read(image) == reading()


def test_the_recording_is_of_this_picture():
    """A picture drawn again without `--read` has no reading of its own: its pixels are not the recording's."""
    recorded = json.loads(READING.read_bytes()).get("pixels")
    pixels = _pixels(Image.open(io.BytesIO(SCREENSHOT.read_bytes())))
    assert pixels == recorded, (
        "payment.png is not the picture payment-ocr.json was recorded from: run "
        "`uv run python scripts/tutorial_screenshot.py --read DATA` (DATA: a data folder that has the text recognition "
        "models) and commit both"
    )


def test_what_is_read_holds_the_three_values_and_latet_finds_each():
    text = reading().text
    masker = build(Config.model_validate({**BASE, "ocr": "auto"}), Vault(b"\x01" * 32)).masker
    found = {(s.rule, text[s.start : s.end]) for s in masker.detect(text, DetectContext(SlotKind.IMAGE_TEXT))}
    assert {("pii:email", EMAIL), ("pii:iban", IBAN), ("pii:phone", PHONE)} <= found
    assert iban_valid(IBAN.replace(" ", ""))
