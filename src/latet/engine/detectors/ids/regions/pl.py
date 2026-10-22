"""Poland: PESEL, the birth date (century in the month) and a weighted check digit."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, is_date, laid_out, placeholder, random_digits

_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
_CENTURY = {0: 1900, 20: 2000, 40: 2100, 60: 2200, 80: 1800}  # added to the month


def _check(first10: str) -> int:
    return (10 - sum(int(c) * w for c, w in zip(first10, _WEIGHTS, strict=True)) % 10) % 10


def pesel_valid(value: str) -> bool:
    d = digits(value)
    if len(d) != 11 or placeholder(d):
        return False
    month = int(d[2:4])
    offset = month // 20 * 20
    century = _CENTURY.get(offset)
    if century is None or not is_date(century + int(d[:2]), month - offset, int(d[4:6])):
        return False
    return _check(d[:10]) == int(d[10])


def gen_pesel(original: str, rng: Rng) -> str:
    first10 = f"{rng.below(50) + 50:02d}{rng.below(12) + 1:02d}{rng.below(28) + 1:02d}{random_digits(rng, 4)}"
    return laid_out(original, first10 + str(_check(first10)))


SPECS = (
    IdSpec(
        "PL_PESEL",
        "PL",
        "PESEL",
        "Birth date and weighted check digit. In code, only next to a keyword.",
        "NNNNNNNNNNN",
        r"\b\d{11}\b",
        pesel_valid,
        gen_pesel,
        context="code",
        words=("pesel", "numer pesel"),
    ),
)
