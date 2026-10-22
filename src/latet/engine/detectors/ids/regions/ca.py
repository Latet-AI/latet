"""Canada: the Social Insurance Number (SIN), a Luhn check digit."""

from __future__ import annotations

from latet.engine.detectors import checksums
from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out, placeholder, random_digits


def sin_valid(value: str) -> bool:
    d = digits(value)
    return len(d) == 9 and d[0] not in "08" and not placeholder(d) and checksums.luhn(d)


def gen_sin(original: str, rng: Rng) -> str:
    first8 = "1234567"[rng.below(7)] + random_digits(rng, 7)
    return laid_out(original, first8 + checksums.luhn_digit(first8))


SPECS = (
    IdSpec(
        "CA_SIN",
        "CA",
        "Social Insurance Number (SIN)",
        "Luhn check digit; always next to a keyword (`SIN` in capitals only, never `sin(`).",
        "NNN NNN NNN",
        r"\b\d{3}[- ]?\d{3}[- ]?\d{3}\b",
        sin_valid,
        gen_sin,
        context="always",
        words=(
            "social insurance number",
            "social insurance",
            "numéro d'assurance sociale",
            "numero d'assurance sociale",
            "assurance sociale",
        ),
        acronyms=("SIN", "NAS"),
    ),
)
