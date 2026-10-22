"""Belgium: the national register number (rijksregisternummer / numéro de registre national)."""

from __future__ import annotations

import datetime

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, is_date, laid_out, placeholder


def _born(year: int, month: int, day: int) -> bool:
    # BIS numbers add 20 or 40 to the month; an unknown month or day is 00.
    month = next((month - offset for offset in (40, 20, 0) if 0 <= month - offset <= 12), -1)
    if month < 0 or day > 31:
        return False
    return month == 0 or day == 0 or is_date(year, month, day)


def nn_valid(value: str) -> bool:
    d = digits(value)
    if len(d) != 11 or placeholder(d):
        return False
    year, month, day, check = int(d[:2]), int(d[2:4]), int(d[4:6]), int(d[9:])
    if 97 - int(d[:9]) % 97 == check and _born(1900 + year, month, day):
        return True
    # People born from 2000 on: a 2 in front of the first nine digits.
    recent = 2000 + year <= datetime.date.today().year
    return recent and 97 - int("2" + d[:9]) % 97 == check and _born(2000 + year, month, day)


def gen_nn(original: str, rng: Rng) -> str:
    body = f"{rng.below(100):02d}{rng.below(12) + 1:02d}{rng.below(28) + 1:02d}{rng.below(997) + 1:03d}"
    return laid_out(original, f"{body}{97 - int(body) % 97:02d}")


SPECS = (
    IdSpec(
        "BE_NATIONAL_NUMBER",
        "BE",
        "National register number (rijksregisternummer)",
        "Birth date and mod-97 check digits. Written without separators, only next to a keyword.",
        "NN.NN.NN-NNN.NN",
        r"\b\d{2}\.?\d{2}\.?\d{2}[-. ]?\d{3}[. ]?\d{2}\b",
        nn_valid,
        gen_nn,
        context="bare",
        words=(
            "rijksregisternummer",
            "rijksregister",
            "nationaal nummer",
            "numéro national",
            "numero national",
            "registre national",
            "national number",
            "insz",
            "niss",
        ),
        acronyms=("RRN",),
    ),
)
