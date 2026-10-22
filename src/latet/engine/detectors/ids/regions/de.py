"""Germany: the tax ID (steuerliche Identifikationsnummer), digit rules and ISO 7064 MOD 11,10."""

from __future__ import annotations

from collections import Counter

from latet.engine.detectors import checksums
from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out, random_digits


def _digit_rule(first10: str) -> bool:
    """In the first ten digits exactly one digit repeats, twice or three times (not three in a row)."""
    counts = Counter(first10)
    repeated = [(digit, n) for digit, n in counts.items() if n > 1]
    if len(repeated) != 1:
        return False
    digit, n = repeated[0]
    return n == 2 or (n == 3 and digit * 3 not in first10)


def idnr_valid(value: str) -> bool:
    d = digits(value)
    return len(d) == 11 and d[0] != "0" and _digit_rule(d[:10]) and checksums.mod11_10(d)


def gen_idnr(original: str, rng: Rng) -> str:
    while True:
        first10 = str(rng.below(9) + 1) + random_digits(rng, 9)
        if _digit_rule(first10):
            return laid_out(original, first10 + checksums.mod11_10_digit(first10))


SPECS = (
    IdSpec(
        "DE_TAX_ID",
        "DE",
        "Tax ID (Steuer-IdNr)",
        "Digit repetition rule and ISO 7064 MOD 11,10; always next to a keyword.",
        "NN NNN NNN NNN",
        r"\b\d{2} ?\d{3} ?\d{3} ?\d{3}\b",
        idnr_valid,
        gen_idnr,
        context="always",
        words=(
            "steuer-id",
            "steuerid",
            "steuer-idnr",
            "steueridnr",
            "steuer-identifikationsnummer",
            "steueridentifikationsnummer",
            "steuerliche identifikationsnummer",
            "identifikationsnummer",
            "idnr",
            "tax id",
            "tax identification number",
            "tin",
        ),
    ),
)
