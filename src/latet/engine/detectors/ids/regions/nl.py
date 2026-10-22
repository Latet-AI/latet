"""Netherlands: the citizen service number (BSN), checked by the "11 test"."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out, placeholder, random_digits

_WEIGHTS = (9, 8, 7, 6, 5, 4, 3, 2, -1)


def bsn_valid(value: str) -> bool:
    d = digits(value)
    return len(d) == 9 and not placeholder(d) and sum(int(c) * w for c, w in zip(d, _WEIGHTS, strict=True)) % 11 == 0


def gen_bsn(original: str, rng: Rng) -> str:
    while True:
        first8 = str(rng.below(9) + 1) + random_digits(rng, 7)
        last = sum(int(c) * w for c, w in zip(first8, _WEIGHTS, strict=False)) % 11
        if last < 10:
            return laid_out(original, first8 + str(last))


SPECS = (
    IdSpec(
        "NL_BSN",
        "NL",
        "Citizen service number (BSN)",
        "The 11 test; always next to a keyword.",
        "NNNN.NN.NNN",
        r"\b\d{4}[. ]?\d{2}[. ]?\d{3}\b",
        bsn_valid,
        gen_bsn,
        context="always",
        words=("bsn", "burgerservicenummer", "sofinummer", "sofi-nummer", "sofi", "citizen service number"),
    ),
)
