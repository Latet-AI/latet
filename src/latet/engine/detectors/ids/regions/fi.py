"""Finland: the personal identity code (henkilötunnus), birth date, century sign and check character."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, is_date

_CHECK = "0123456789ABCDEFHJKLMNPRSTUVWXY"
# The sign between the birth date and the individual number gives the century (more signs since 2023).
_CENTURY = dict.fromkeys("+", 1800) | dict.fromkeys("-UVWXY", 1900) | dict.fromkeys("ABCDEF", 2000)


def hetu_valid(value: str) -> bool:
    v = value.upper()
    if len(v) != 11 or v[6] not in _CENTURY:
        return False
    day, month, year, number, check = v[0:2], v[2:4], v[4:6], v[7:10], v[10]
    if not (day + month + year + number).isdigit():
        return False
    if not is_date(_CENTURY[v[6]] + int(year), int(month), int(day)):
        return False
    if not 2 <= int(number) <= 899:  # 900-999: temporary codes
        return False
    return _CHECK[int(day + month + year + number) % 31] == check


def gen_hetu(original: str, rng: Rng) -> str:
    sign = original[6].upper() if len(original) == 11 and original[6].upper() in _CENTURY else "-"
    century = _CENTURY[sign]
    year = rng.below(24 if century == 2000 else 100)
    birth = f"{rng.below(28) + 1:02d}{rng.below(12) + 1:02d}{year:02d}"
    number = f"{rng.below(898) + 2:03d}"
    code = f"{birth}{sign}{number}{_CHECK[int(birth + number) % 31]}"
    return code.lower() if any(c.islower() for c in original) else code


SPECS = (
    IdSpec(
        "FI_HETU",
        "FI",
        "Personal identity code (henkilötunnus)",
        "Birth date, century sign and check character (modulo 31).",
        "NNNNNN-NNNA",
        r"\b\d{6}[-+A-FU-Y]\d{3}[0-9A-FHJ-NPR-Y]\b",
        hetu_valid,
        gen_hetu,
        words=("hetu", "henkilötunnus", "henkilotunnus", "personbeteckning", "personal identity code"),
        numeric=False,
    ),
)
