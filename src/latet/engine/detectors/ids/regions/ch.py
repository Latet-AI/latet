"""Switzerland: the AHV/AVS number (social security), 756 and an EAN-13 check digit."""

from __future__ import annotations

from latet.engine.detectors import checksums
from latet.engine.detectors.ids.spec import IdSpec, Rng, digits, laid_out, random_digits


def ahv_valid(value: str) -> bool:
    d = digits(value)
    return len(d) == 13 and d.startswith("756") and checksums.ean(d)


def gen_ahv(original: str, rng: Rng) -> str:
    payload = "756" + random_digits(rng, 9)
    return laid_out(original, payload + checksums.ean_digit(payload))


SPECS = (
    IdSpec(
        "CH_AHV",
        "CH",
        "AHV/AVS number (social security)",
        "Starts with 756; EAN-13 check digit.",
        "756.NNNN.NNNN.NN",
        r"\b756[. ]?\d{4}[. ]?\d{4}[. ]?\d{2}\b",
        ahv_valid,
        gen_ahv,
        words=(
            "ahv",
            "avs",
            "ahv-nr",
            "avs-nr",
            "ahv-nummer",
            "sozialversicherungsnummer",
            "numéro avs",
            "numero avs",
            "social security",
        ),
    ),
)
