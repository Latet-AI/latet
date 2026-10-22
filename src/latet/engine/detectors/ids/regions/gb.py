"""United Kingdom: the National Insurance number (NINO), two letters, six digits and a letter."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, compact, laid_out, placeholder, random_digits

_FIRST = "ABCEGHJKLMNOPRSTWXYZ"  # never D, F, I, Q, U or V
_SECOND = "ABCEGHJKLMNPRSTWXYZ"  # nor O
_NOT_ISSUED = {"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"}


def nino_valid(value: str) -> bool:
    c = compact(value)
    return (
        len(c) == 9
        and c[0] in _FIRST
        and c[1] in _SECOND
        and c[:2] not in _NOT_ISSUED
        and c[2:8].isdigit()
        and not placeholder(c[2:8])
        and c[8] in "ABCD"
    )


def gen_nino(original: str, rng: Rng) -> str:
    prefix = "GB"
    while prefix in _NOT_ISSUED:
        prefix = rng.choice(_FIRST) + rng.choice(_SECOND)
    suffix = compact(original)[-1:] if compact(original)[-1:] in ("A", "B", "C", "D") else "A"
    return laid_out(original, prefix + random_digits(rng, 6) + suffix)


SPECS = (
    IdSpec(
        "GB_NINO",
        "GB",
        "National Insurance number",
        "Issued prefixes and suffix. In code, only next to a keyword.",
        "AA NN NN NN A",
        r"\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b",
        nino_valid,
        gen_nino,
        context="code",
        words=("national insurance", "ni number", "ni no", "nino", "nat ins"),
        acronyms=("NI", "NINO"),
        numeric=False,
    ),
)
