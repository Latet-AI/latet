"""Sweden: personnummer (and samordningsnummer), the birth date and a Luhn check digit."""

from __future__ import annotations

from latet.engine.detectors import checksums
from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, is_date, laid_out, placeholder


def personnummer_valid(value: str) -> bool:
    d = digits(value)
    if len(d) == 12:
        years = [int(d[:4])]
        d = d[2:]
    elif len(d) == 10:
        years = [1900 + int(d[:2]), 2000 + int(d[:2])]
    else:
        return False
    if placeholder(d):
        return False
    month, day = int(d[2:4]), int(d[4:6])
    if day > 60:
        day -= 60  # samordningsnummer: the day plus 60
    return any(is_date(year, month, day) for year in years) and checksums.luhn(d)


def gen_personnummer(original: str, rng: Rng) -> str:
    body = f"{rng.below(100):02d}{rng.below(12) + 1:02d}{rng.below(28) + 1:02d}{rng.below(1000):03d}"
    ten = body + checksums.luhn_digit(body)
    return laid_out(original, ("19" + ten) if len(digits(original)) == 12 else ten)


SPECS = (
    IdSpec(
        "SE_PERSONNUMMER",
        "SE",
        "Personnummer",
        "Birth date and Luhn check digit. In code, only next to a keyword.",
        "NNNNNN-NNNN",
        r"\b(?:(?:18|19|20)\d{2}|\d{2})(?:0[1-9]|1[0-2])\d{2}[-+]?\d{4}\b",
        personnummer_valid,
        gen_personnummer,
        context="code",
        words=("personnummer", "samordningsnummer", "personnr", "person number", "personal identity number"),
    ),
)
