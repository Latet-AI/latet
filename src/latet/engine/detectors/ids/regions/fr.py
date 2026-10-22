"""France: the social security number (NIR, numéro de sécurité sociale), closed by a mod-97 key."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, compact, laid_out


def nir_valid(value: str) -> bool:
    c = compact(value)
    if len(c) != 15 or c[0] not in "123478" or not c[13:].isdigit():
        return False
    month = c[3:5]
    if not month.isdigit() or not (1 <= int(month) <= 12 or 20 <= int(month) <= 42 or int(month) >= 50):
        return False
    # Corsica: departments 2A and 2B count as 19 and 18 for the key.
    body = c[:5] + {"2A": "19", "2B": "18"}.get(c[5:7], c[5:7]) + c[7:13]
    return body.isdigit() and 97 - int(body) % 97 == int(c[13:])


def gen_nir(original: str, rng: Rng) -> str:
    sex = compact(original)[:1] if compact(original)[:1] in ("1", "2") else "1"
    body = (
        f"{sex}{rng.below(100):02d}{rng.below(12) + 1:02d}{rng.below(95) + 1:02d}"
        f"{rng.below(989) + 1:03d}{rng.below(999) + 1:03d}"
    )
    return laid_out(original, f"{body}{97 - int(body) % 97:02d}")


SPECS = (
    IdSpec(
        "FR_NIR",
        "FR",
        "Social security number (NIR)",
        "Sex, birth year and month, place; mod-97 key. Written without spaces, only next to a keyword.",
        "N NN NN NN NNN NNN NN",
        r"\b[1-478][ .]?\d{2}[ .]?\d{2}[ .]?(?:\d{2}|2[AB])[ .]?\d{3}[ .]?\d{3}[ .]?\d{2}\b",
        nir_valid,
        gen_nir,
        context="bare",
        words=(
            "nir",
            "nss",
            "insee",
            "sécurité sociale",
            "securite sociale",
            "numéro de sécurité sociale",
            "carte vitale",
            "social security",
        ),
    ),
)
