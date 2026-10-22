"""Spain: DNI and NIE, each closed by a check letter (the number modulo 23)."""

from __future__ import annotations

from latet.engine.detectors.ids.spec import IdSpec, Rng, compact, laid_out, placeholder, random_digits

LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def dni_valid(value: str) -> bool:
    c = compact(value)
    return len(c) == 9 and c[:8].isdigit() and not placeholder(c[:8]) and LETTERS[int(c[:8]) % 23] == c[8]


def gen_dni(original: str, rng: Rng) -> str:
    number = random_digits(rng, 8)
    return laid_out(original, number + LETTERS[int(number) % 23])


def nie_valid(value: str) -> bool:
    c = compact(value)
    if len(c) != 9 or c[0] not in "XYZ" or not c[1:8].isdigit():
        return False
    return LETTERS[int(str("XYZ".index(c[0])) + c[1:8]) % 23] == c[8]


def gen_nie(original: str, rng: Rng) -> str:
    first = compact(original)[:1] if compact(original)[:1] in ("X", "Y", "Z") else "X"
    number = random_digits(rng, 7)
    return laid_out(original, first + number + LETTERS[int(str("XYZ".index(first)) + number) % 23])


SPECS = (
    IdSpec(
        "ES_DNI",
        "ES",
        "DNI (Documento Nacional de Identidad)",
        "Check letter: the number modulo 23.",
        "NNNNNNNNA",
        r"\b\d{8}-?[A-Z]\b",
        dni_valid,
        gen_dni,
        words=("dni", "nif", "documento nacional de identidad"),
        numeric=False,
    ),
    IdSpec(
        "ES_NIE",
        "ES",
        "NIE (Número de Identidad de Extranjero)",
        "X, Y or Z, seven digits and a check letter (modulo 23).",
        "ANNNNNNNA",
        r"\b[XYZ]-?\d{7}-?[A-Z]\b",
        nie_valid,
        gen_nie,
        words=("nie", "número de identidad de extranjero", "numero de identidad de extranjero"),
        numeric=False,
    ),
)
