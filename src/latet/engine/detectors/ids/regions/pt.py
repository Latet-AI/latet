"""Portugal: the tax number (NIF, número de contribuinte), a weighted mod-11 check digit."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out, random_digits


def nif_check(first8: str) -> int:
    check = 11 - sum(int(c) * (9 - i) for i, c in enumerate(first8)) % 11
    return 0 if check >= 10 else check


def nif_valid(value: str) -> bool:
    d = digits(value)
    # 1-3 people, 5 companies, 6 public bodies, 8 sole traders, 9 others; 45 non-resident people.
    if len(d) != 9 or not (d[0] in "1235689" or d[:2] == "45" or d[0] == "7"):
        return False
    return nif_check(d[:8]) == int(d[8])


def gen_nif(original: str, rng: Rng) -> str:
    first = digits(original)[:1] if digits(original)[:1] in ("1", "2", "3", "5") else "2"
    first8 = first + random_digits(rng, 7)
    return laid_out(original, first8 + str(nif_check(first8)))


SPECS = (
    IdSpec(
        "PT_NIF",
        "PT",
        "Tax number (NIF, número de contribuinte)",
        "Weighted mod-11 check digit; always next to a keyword.",
        "NNN NNN NNN",
        r"\b\d{3} ?\d{3} ?\d{3}\b",
        nif_valid,
        gen_nif,
        context="always",
        words=(
            "nif",
            "nipc",
            "contribuinte",
            "número de contribuinte",
            "numero de contribuinte",
            "número de identificação fiscal",
            "numero de identificacao fiscal",
        ),
    ),
)
