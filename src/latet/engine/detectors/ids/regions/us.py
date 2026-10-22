"""United States: Social Security numbers and ITINs, both always next to a keyword."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out

# Never issued, or printed on sample cards and in advertising.
_NOT_ISSUED = {"078051120", "219099999", "123456789", "457555462"}


def ssn_valid(value: str) -> bool:
    d = digits(value)
    if len(d) != 9 or d in _NOT_ISSUED:
        return False
    area, group, serial = d[:3], d[3:5], d[5:]
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def gen_ssn(original: str, rng: Rng) -> str:
    area = 666
    while area == 666:
        area = rng.below(899) + 1
    new = f"{area:03d}{rng.below(99) + 1:02d}{rng.below(9999) + 1:04d}"
    return laid_out(original, new)


# The IRS issues the groups 70-88, 90-92 and 94-99, and since 2011 also 50-65.
_FIRST_GROUPS = (*range(70, 89), 90, 91, 92, *range(94, 100))
_ITIN_GROUPS = frozenset((*range(50, 66), *_FIRST_GROUPS))


def itin_valid(value: str) -> bool:
    d = digits(value)
    return len(d) == 9 and d[0] == "9" and int(d[3:5]) in _ITIN_GROUPS


def gen_itin(original: str, rng: Rng) -> str:
    group = _FIRST_GROUPS[rng.below(len(_FIRST_GROUPS))]  # valid for every reader, old ranges included
    return laid_out(original, f"9{rng.below(100):02d}{group:02d}{rng.below(10_000):04d}")


SPECS = (
    IdSpec(
        "US_SSN",
        "US",
        "Social Security number",
        "Issued area, group and serial ranges; always next to a keyword.",
        "NNN-NN-NNNN",
        r"\b\d{3}-\d{2}-\d{4}\b|\b\d{3} \d{2} \d{4}\b|\b\d{9}\b",
        ssn_valid,
        gen_ssn,
        context="always",
        words=("ssn", "ssns", "social security", "social security number", "soc sec"),
    ),
    IdSpec(
        "US_ITIN",
        "US",
        "Individual Taxpayer Identification Number (ITIN)",
        "Starts with 9, issued group ranges; always next to a keyword.",
        "9NN-NN-NNNN",
        r"\b9\d{2}-\d{2}-\d{4}\b|\b9\d{2} \d{2} \d{4}\b|\b9\d{8}\b",
        itin_valid,
        gen_itin,
        context="always",
        words=("itin", "individual taxpayer identification number", "taxpayer identification number", "tax id"),
    ),
)
