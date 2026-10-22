"""Latvia: the personal code (personas kods), a birth date (32 since July 2017) and a check digit."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, is_date, laid_out, random_digits

_WEIGHTS = (1, 6, 3, 7, 9, 10, 5, 8, 4, 2)


def check_digit(first10: str) -> int:
    """1101 minus the weighted sum of the first ten digits, modulo 11; 10 gives 0."""
    return (1101 - sum(int(c) * w for c, w in zip(first10, _WEIGHTS, strict=True))) % 11 % 10


def personal_code_valid(value: str) -> bool:
    """A person's code: DDMMYY, a century digit (0, 1 or 2 for the 1800s, 1900s or 2000s), three digits and the
    check digit. A code issued since July 2017 holds no birth date and starts with 32 instead. The VAT number of a
    person is this code, so the VAT check of Latvia reads it here."""
    d = digits(value)
    if len(d) != 11:
        return False
    dated = d[6] in "012" and is_date(1800 + 100 * int(d[6]) + int(d[4:6]), int(d[2:4]), int(d[:2]))
    return (dated or d[:2] == "32") and check_digit(d[:10]) == int(d[10])


def gen_personal_code(original: str, rng: Rng) -> str:
    """A code of the same kind (dated, in the same century, or since July 2017) and layout, never the original."""
    d = digits(original)
    since_2017 = d[:2] == "32"
    century = d[6] if len(d) > 6 and d[6] in "012" else "1"
    for _ in range(20):
        if since_2017:
            first10 = "32" + random_digits(rng, 8)
        else:
            year = rng.below(20 if century == "2" else 100)  # the 2000s: no date after 2019
            first10 = f"{rng.below(28) + 1:02d}{rng.below(12) + 1:02d}{year:02d}{century}{random_digits(rng, 3)}"
        code = first10 + str(check_digit(first10))
        if code != d:
            return laid_out(original, code)
    raise ValueError("no other personal code found")


SPECS = (
    IdSpec(
        "LV_PERSONAL_CODE",
        "LV",
        "Personal code (personas kods)",
        "Birth date (a code issued since July 2017 starts with 32 instead) and check digit. Written without the "
        "hyphen, only next to a keyword.",
        "NNNNNN-NNNNN",
        r"\b\d{6}-?\d{5}\b",
        personal_code_valid,
        gen_personal_code,
        context="bare",
        words=(
            "personas kods",
            "personas koda",
            "personas kodu",
            "personas kodi",
            "personas_kods",
            "pers. kods",
            "personal code",
            "personal identity code",
            "personal identity number",
        ),
    ),
)
