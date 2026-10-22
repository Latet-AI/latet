"""Italy: codice fiscale and partita IVA (patterns and checks as in Microsoft Presidio, MIT)."""

from __future__ import annotations

import string
from collections.abc import Iterator

import re2

from latet.engine.detectors.context import ContextWords
from latet.engine.detectors.ids.spec import IdSpec, Rng
from latet.engine.types import EntityClass, Span

# --- codice fiscale -------------------------------------------------------------------------------

_CF = re2.compile(r"(?i)\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b")
# The value of each character in an odd position (1st, 3rd...), as the check character counts it:
# digits 0-9 and letters A-J share the first ten values.
_CF_ODD_DIGITS = (1, 0, 5, 7, 9, 13, 15, 17, 19, 21)
_CF_ODD = dict(
    zip(
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        (*_CF_ODD_DIGITS, *_CF_ODD_DIGITS, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23),
        strict=True,
    )
)


def _cf_even(c: str) -> int:
    return int(c) if c.isdigit() else ord(c) - ord("A")


def fiscal_code_check_char(first15: str) -> str:
    total = sum(_CF_ODD[c] if i % 2 == 0 else _cf_even(c) for i, c in enumerate(first15.upper()))
    return chr(ord("A") + total % 26)


def fiscal_code_valid(code: str) -> bool:
    code = code.upper()
    return len(code) == 16 and fiscal_code_check_char(code[:15]) == code[15]


def _fiscal_codes(text: str, natural: bool) -> Iterator[Span]:
    for m in _CF.finditer(text):
        if fiscal_code_valid(text[m.start() : m.end()]):
            yield Span(m.start(), m.end(), "IT_FISCAL_CODE", EntityClass.PII, "pii:it-fiscal-code")


_CF_CONSONANTS = "BCDFGHJKLMNPQRSTVWXZ"
_CF_MONTHS = "ABCDEHLMPRST"


def gen_fiscal_code(original: str, rng: Rng) -> str:
    letters = "".join(rng.choice(_CF_CONSONANTS) for _ in range(6))
    year = f"{rng.below(100):02d}"
    month = rng.choice(_CF_MONTHS)
    day = rng.below(28) + 1 + (40 if rng.below(2) else 0)
    place = rng.choice(string.ascii_uppercase) + f"{rng.below(1000):03d}"
    first15 = f"{letters}{year}{month}{day:02d}{place}"
    code = first15 + fiscal_code_check_char(first15)
    return code.lower() if original.islower() else code


# --- partita IVA ----------------------------------------------------------------------------------

_VAT = re2.compile(r"\b(?:IT[ ]?)?(\d{11})\b")
_VAT_CONTEXT = ContextWords(
    ("iva", "vat", "p.i.", "piva", "partita", "vat id", "vat number", "ust-id"), mode="substring"
)


def vat_code_valid(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or digits == "0" * 11:
        return False
    odd = sum(int(digits[i]) for i in range(0, 10, 2))
    even = 0
    for i in range(1, 10, 2):
        d = int(digits[i]) * 2
        even += d - 9 if d > 9 else d
    return (10 - (odd + even) % 10) % 10 == int(digits[10])


def _vat_codes(text: str, natural: bool) -> Iterator[Span]:
    for m in _VAT.finditer(text):
        start, end = m.span(1)
        prefixed = m.start() < start
        if not vat_code_valid(text[start:end]):
            continue
        if prefixed or _VAT_CONTEXT.near(text, m.start(), end, before=40, after=0, header=False):
            yield Span(m.start(), end, "IT_VAT_CODE", EntityClass.PII, "pii:it-vat-code")


def gen_vat_code(original: str, rng: Rng) -> str:
    digits = [c for c in original if c.isdigit()]
    # 7-digit company number + a valid provincial office code (001-100), then the check digit.
    company = str(rng.below(9_000_000) + 1_000_000)
    body = company + f"{rng.below(100) + 1:03d}"
    odd = sum(int(body[i]) for i in range(0, 10, 2))
    even = sum((2 * int(body[i])) - 9 if 2 * int(body[i]) > 9 else 2 * int(body[i]) for i in range(1, 10, 2))
    new = body + str((10 - (odd + even) % 10) % 10)
    assert len(digits) == 11
    out, j = [], 0
    for c in original:
        if c.isdigit():
            out.append(new[j])
            j += 1
        else:
            out.append(c)
    return "".join(out)


SPECS = (
    IdSpec(
        "IT_FISCAL_CODE",
        "IT",
        "Codice fiscale",
        "Structure (letters, birth date, place) and check character.",
        "AAAAAANNANNANNNA",
        "",
        fiscal_code_valid,
        gen_fiscal_code,
        words=("codice fiscale", "cf"),
        numeric=False,
        rule="pii:it-fiscal-code",
        finder=_fiscal_codes,
    ),
    IdSpec(
        "IT_VAT_CODE",
        "IT",
        "Partita IVA",
        "Check digit, with an `IT` prefix or a VAT word on the same line.",
        "NNNNNNNNNNN",
        "",
        vat_code_valid,
        gen_vat_code,
        context="always",
        words=("iva", "vat", "partita iva", "p.iva"),
        rule="pii:it-vat-code",
        finder=_vat_codes,
    ),
)
