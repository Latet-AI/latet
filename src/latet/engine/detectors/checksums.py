"""Check-digit algorithms, written from their public specifications (ISO 7064, ISO 13616, GS1,
Luhn, Verhoeff, Damm).

National IDs and VAT numbers use them, and so can user-defined recognizers (`checksum:` in
config.yaml). Each function takes the compact value: digits only, or letters and digits for the
alphanumeric ones, without spaces or separators.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal


def luhn(digits: str) -> bool:
    """Luhn (ISO/IEC 7812-1): payment cards, Canadian SIN, Swedish personnummer."""
    if len(digits) < 2 or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def luhn_digit(payload: str) -> str:
    """The digit that makes ``payload + digit`` pass Luhn."""
    return next(d for d in "0123456789" if luhn(payload + d))


def _mod11_10_state(digits: str) -> int:
    product = 10
    for ch in digits:
        total = (ord(ch) - 48 + product) % 10 or 10
        product = (total * 2) % 11
    return product


def mod11_10(digits: str) -> bool:
    """ISO 7064 MOD 11,10 (hybrid system): German tax ID, Croatian OIB."""
    if len(digits) < 2 or not digits.isdigit():
        return False
    return (_mod11_10_state(digits[:-1]) + ord(digits[-1]) - 48) % 10 == 1


def mod11_10_digit(payload: str) -> str:
    return str((11 - _mod11_10_state(payload)) % 10)


def _alnum_value(chars: str) -> str:
    """Letters as numbers (A=10 ... Z=35), as ISO 7064 and ISO 13616 read them."""
    return "".join(str(int(c, 36)) for c in chars)


def mod97_10(chars: str) -> bool:
    """ISO 7064 MOD 97-10 over the whole value (letters A=10 ... Z=35)."""
    if len(chars) < 2 or not chars.isalnum() or not chars.isascii():
        return False
    return int(_alnum_value(chars.upper())) % 97 == 1


def iban(compact: str) -> bool:
    """ISO 13616: the first four characters move to the end, then MOD 97-10."""
    if len(compact) < 5 or not compact.isalnum() or not compact.isascii():
        return False
    return mod97_10(compact[4:] + compact[:4])


def ean(digits: str) -> bool:
    """GS1 (EAN/GTIN) check digit: weights 1 and 3 from the right. Swiss AHV numbers use it."""
    if len(digits) < 2 or not digits.isdigit():
        return False
    return sum((3 if i % 2 else 1) * (ord(c) - 48) for i, c in enumerate(reversed(digits))) % 10 == 0


def ean_digit(payload: str) -> str:
    return next(d for d in "0123456789" if ean(payload + d))


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff(digits: str) -> bool:
    if len(digits) < 2 or not digits.isdigit():
        return False
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][ord(ch) - 48]]
    return check == 0


_DAMM = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2),
    (7, 0, 9, 2, 1, 5, 4, 8, 6, 3),
    (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6),
    (6, 1, 2, 3, 0, 4, 5, 9, 7, 8),
    (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4),
    (8, 9, 4, 5, 3, 6, 2, 0, 1, 7),
    (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)


def damm(digits: str) -> bool:
    if len(digits) < 2 or not digits.isdigit():
        return False
    interim = 0
    for ch in digits:
        interim = _DAMM[interim][ord(ch) - 48]
    return interim == 0


def weighted(digits: str, weights: Sequence[int], modulus: int, remainder: int = 0) -> bool:
    """Sum of digit times weight (weights repeat from the left) modulo ``modulus`` equals ``remainder``.

    Many national schemes are of this kind: Polish PESEL, Dutch BSN, Portuguese NIF...
    """
    if not digits or not digits.isdigit() or not weights or modulus < 2:
        return False
    total = sum((ord(c) - 48) * weights[i % len(weights)] for i, c in enumerate(digits))
    return total % modulus == remainder


@dataclass(frozen=True, slots=True)
class Checksum:
    name: str
    description: str
    validate: Callable[[str], bool]
    reads: Literal["digits", "alnum"]  # what the value is reduced to before the check


CHECKSUMS: dict[str, Checksum] = {
    c.name: c
    for c in (
        Checksum("luhn", "Luhn (ISO/IEC 7812): payment cards and many national numbers", luhn, "digits"),
        Checksum("iban", "IBAN (ISO 13616): first four characters moved to the end, then MOD 97-10", iban, "alnum"),
        Checksum("mod97-10", "ISO 7064 MOD 97-10 over the whole value; letters count A=10 to Z=35", mod97_10, "alnum"),
        Checksum("mod11-10", "ISO 7064 MOD 11,10", mod11_10, "digits"),
        Checksum("ean", "GS1 / EAN check digit: weights 1 and 3 from the right", ean, "digits"),
        Checksum("verhoeff", "Verhoeff dihedral check digit", verhoeff, "digits"),
        Checksum("damm", "Damm quasigroup check digit", damm, "digits"),
    )
}


def compact(value: str, reads: Literal["digits", "alnum"]) -> str:
    """The characters a checksum reads: digits, or letters and digits (upper-cased)."""
    if reads == "digits":
        return "".join(c for c in value if c.isdigit())
    return "".join(c for c in value if c.isalnum()).upper()
