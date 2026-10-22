"""IBANs and fiscal codes read from an image with a look-alike letter wrong are still found."""

from __future__ import annotations

import pytest

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.misread import MisreadDetector, repaired_fiscal_code, repaired_iban
from latet.engine.types import SlotKind

IMAGE = DetectContext(SlotKind.IMAGE_TEXT)


@pytest.mark.parametrize(
    ("read", "real"),
    [
        ("IT05 1390 4600 7843 6006 2969 658", "IT05I3904600784360062969658"),  # I read as 1
        ("ITo7 K136 3783 3418 1998 3097 507", "IT07K1363783341819983097507"),  # 0 read as o
        ("IT75 0969 9562 2397 7756 3509 814", "IT75O9699562239777563509814"),  # O read as 0
        ("IT18 z997 2141 2174 6720 5165 582", "IT18Z9972141217467205165582"),  # a small z
    ],
)
def test_misread_ibans_are_repaired(read, real):
    assert repaired_iban(read) == real


@pytest.mark.parametrize(
    ("read", "real"),
    [
        ("MMCLZV82s09R761c", "MMCLZV82S09R761C"),
        ("XKRMWC17L520291Y", "XKRMWC17L52O291Y"),
    ],
)
def test_misread_fiscal_codes_are_repaired(read, real):
    assert repaired_fiscal_code(read) == real


def test_values_read_right_are_left_to_the_other_detectors():
    assert repaired_iban("IT60 X054 2811 1010 0000 0123 456") is None
    assert repaired_fiscal_code("RSSMRA85T10A562S") is None


def test_the_span_is_the_text_as_read_and_stops_where_the_iban_does():
    text = "Bonifico sul conto ITo7 K136 3783 3418 1998 3097 507 intestato a Fattorie Riunite"
    [span] = MisreadDetector().detect(text, IMAGE)
    assert text[span.start : span.end] == "ITo7 K136 3783 3418 1998 3097 507" and span.etype == "IBAN"


def test_only_the_text_of_images_and_never_plain_words():
    text = '"tax_code": "MMCLZV82s09R761c", "iban": "IT75 0969 9562 2397 7756 3509 814"'
    assert len(list(MisreadDetector().detect(text, IMAGE))) == 2
    assert list(MisreadDetector().detect(text, DetectContext(SlotKind.USER_TEXT))) == []
    prose = "La preghiamo di effettuare il bonifico sul conto entro trenta giorni dalla data della fattura"
    assert list(MisreadDetector().detect(prose, IMAGE)) == []
    assert list(MisreadDetector(types=()).detect(text, IMAGE)) == []
