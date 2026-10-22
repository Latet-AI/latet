"""Values the text recognition read with a letter wrong, in the text of images.

Text recognition confuses letters that look alike: O and 0, I and 1, S and 5, a capital and its small
letter. An IBAN or an Italian fiscal code read that way fails its check digits, and the detectors that
check them would let it through, though the image still shows the value. Here the look-alike letters
are tried the other way until the check digits agree: the span found is the text as read, so it is
covered in the image like any other value.

Only the text read in images is searched (``SlotKind.IMAGE_TEXT``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import combinations

import re2

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.ids.regions.it import fiscal_code_valid
from latet.engine.detectors.pii import IBAN_LENGTHS, iban_valid
from latet.engine.types import EntityClass, SlotKind, Span

LOOK_ALIKE = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5", "8": "B", "B": "8", "2": "Z", "Z": "2"}
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
MAX_FLIPS = 2

# A country (a capital first), check digits as they may be misread, then the rest, small letters and all.
_IBANISH = re2.compile(r"\b[A-Z][A-Z0-9][0-9OoIlSsZzBb]{2}(?:[ \-]?[A-Za-z0-9]){11,30}\b")
_CFISH = re2.compile(r"(?i)\b[A-Z0-9]{16}\b")
# Where an Italian fiscal code has letters (L) and digits (D); a digit may be an omocode letter.
_CF_SHAPE = "LLLLLLDDLDDLDDDL"
_OMOCODE = set("LMNPQRSTUV")


def _variants(text: str, fixed: set[int]) -> Iterator[str]:
    """``text`` with up to MAX_FLIPS look-alike letters swapped, fewest swaps first."""
    yield text
    places = [i for i, c in enumerate(text) if c in LOOK_ALIKE and i not in fixed]
    for n in range(1, MAX_FLIPS + 1):
        for chosen in combinations(places, n):
            chars = list(text)
            for i in chosen:
                chars[i] = LOOK_ALIKE[chars[i]]
            yield "".join(chars)


def repaired_iban(raw: str) -> str | None:
    """The IBAN that ``raw`` was read from, when it fails its check digits as read; None otherwise."""
    as_read = raw.replace(" ", "").replace("-", "")
    letters = [c for c in as_read if c.isalpha()]
    if sum(c.islower() for c in letters) > len(letters) // 4 + 1:
        return None  # words, not a code: codes are capitals and digits, a letter or two read small at most
    compact = as_read.upper()
    if iban_valid(compact):
        return None if compact == as_read else compact  # only its case was off
    chars = list(compact)
    for i in (0, 1):  # the country: letters
        chars[i] = TO_LETTER.get(chars[i], chars[i])
    for i in (2, 3):  # its check digits
        chars[i] = TO_DIGIT.get(chars[i], chars[i])
    if IBAN_LENGTHS.get("".join(chars[:2])) != len(chars) or not "".join(chars[2:4]).isdigit():
        return None
    if sum(c.isdigit() for c in chars) < len(chars) // 2:
        return None  # an IBAN is mostly digits
    fixed = {0, 1, 2, 3}
    if "".join(chars[:2]) in ("IT", "SM") and len(chars) == 27:  # a letter, then the bank's ten digits
        chars[4] = TO_LETTER.get(chars[4], chars[4])
        for i in range(5, 15):
            chars[i] = TO_DIGIT.get(chars[i], chars[i])
        fixed |= set(range(15))
    for candidate in _variants("".join(chars), fixed):
        if iban_valid(candidate):
            return candidate
    return None


def repaired_fiscal_code(raw: str) -> str | None:
    """The fiscal code that ``raw`` was read from, when it fails its check character as read."""
    upper = raw.upper()
    if fiscal_code_valid(upper):
        return None if upper == raw else upper
    chars = list(upper)
    fixed: set[int] = set()
    for i, kind in enumerate(_CF_SHAPE):
        if kind == "L":
            chars[i] = TO_LETTER.get(chars[i], chars[i])
            fixed.add(i)
        elif chars[i] not in _OMOCODE:
            chars[i] = TO_DIGIT.get(chars[i], chars[i])
    shaped = "".join(chars)
    if not all(c.isalpha() for c, kind in zip(shaped, _CF_SHAPE, strict=True) if kind == "L"):
        return None
    for candidate in _variants(shaped, fixed):
        if fiscal_code_valid(candidate):
            return candidate
    return None


def _iban_end(text: str, start: int, end: int) -> int:
    """Where an IBAN read from ``start`` ends: the pattern may run on into the next word, so it stops
    once the country's length is reached."""
    country = "".join(TO_LETTER.get(c, c) for c in text[start : start + 2].upper())
    expected = IBAN_LENGTHS.get(country)
    if expected is None:
        return end
    seen, at = 0, start
    while at < end and seen < expected:
        if text[at] not in " -":
            seen += 1
        at += 1
    return at


class MisreadDetector:
    """IBANs and Italian fiscal codes read with a look-alike letter wrong, in the text of images."""

    id = "misread"
    mandatory = False

    def __init__(self, types: Iterable[str] = ("IBAN", "IT_FISCAL_CODE")) -> None:
        self.types = frozenset(types)

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if ctx.kind != SlotKind.IMAGE_TEXT:
            return
        if "IBAN" in self.types:
            for m in _IBANISH.finditer(text):
                end = _iban_end(text, m.start(), m.end())
                if repaired_iban(text[m.start() : end]) is not None:
                    yield Span(m.start(), end, "IBAN", EntityClass.PII, "pii:iban-misread")
        if "IT_FISCAL_CODE" in self.types:
            for m in _CFISH.finditer(text):
                if repaired_fiscal_code(text[m.start() : m.end()]) is not None:
                    yield Span(m.start(), m.end(), "IT_FISCAL_CODE", EntityClass.PII, "pii:it-fiscal-code-misread")
