"""VAT numbers of the EU countries, Northern Ireland, the United Kingdom, Switzerland and Norway.

Always written with the country prefix (EL for Greece, CHE for Switzerland), and confirmed by each
country's check digits. The algorithms follow the public specifications of each tax authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import re2

from latet.engine.detectors import checksums
from latet.engine.detectors.base import quiet_options
from latet.engine.detectors.ids.regions.it import vat_code_valid
from latet.engine.detectors.ids.regions.lv import personal_code_valid
from latet.engine.detectors.ids.regions.pt import nif_check
from latet.engine.detectors.ids.spec import Rng, by_rejection, compact, is_date
from latet.engine.types import EntityClass, Span


def _weighted(digits: str, weights: tuple[int, ...]) -> int:
    return sum(int(c) * w for c, w in zip(digits, weights, strict=False))


def _mod11_check(digits: str, weights: tuple[int, ...]) -> int | None:
    """11 minus the weighted sum modulo 11: 11 gives 0, 10 is no valid number (None)."""
    check = 11 - _weighted(digits, weights) % 11
    return None if check == 10 else check % 11


def _at(b: str) -> bool:
    if len(b) != 9 or b[0] != "U" or not b[1:].isdigit():
        return False
    total = 0
    for i, c in enumerate(b[1:8]):
        p = int(c) * (2 if i % 2 else 1)
        total += p // 10 + p % 10
    return (10 - (total + 4) % 10) % 10 == int(b[8])


def _be(b: str) -> bool:
    b = b.zfill(10) if len(b) == 9 else b
    return len(b) == 10 and b.isdigit() and b[0] in "01" and 97 - int(b[:8]) % 97 == int(b[8:])


def _bg(b: str) -> bool:
    if not b.isdigit():
        return False
    if len(b) == 9:
        check = _weighted(b, (1, 2, 3, 4, 5, 6, 7, 8)) % 11
        if check == 10:
            check = _weighted(b, (3, 4, 5, 6, 7, 8, 9, 10)) % 11
        return check % 10 == int(b[8])
    if len(b) != 10:
        return False
    # A person's EGN (birth date and check digit), a foreigner's number, or another entity.
    month = int(b[2:4])
    century, month = (1900, month) if month <= 12 else (1800, month - 20) if month <= 32 else (2000, month - 40)
    birth = is_date(century + int(b[:2]), month, int(b[4:6]))
    if birth and _weighted(b, (2, 4, 8, 5, 10, 9, 7, 3, 6)) % 11 % 10 == int(b[9]):
        return True
    if _weighted(b, (21, 19, 17, 13, 11, 9, 7, 3, 1)) % 10 == int(b[9]):
        return True
    return _mod11_check(b, (4, 3, 2, 7, 6, 5, 4, 3, 2)) == int(b[9])


_CY_ODD = (1, 0, 5, 7, 9, 13, 15, 17, 19, 21)


def _cy(b: str) -> bool:
    if len(b) != 9 or not b[:8].isdigit() or not b[8].isalpha() or b[0] not in "013459" or b[:2] == "12":
        return False
    total = sum(_CY_ODD[int(c)] if i % 2 == 0 else int(c) for i, c in enumerate(b[:8]))
    return chr(ord("A") + total % 26) == b[8]


def _cz_birth_date(b: str) -> bool:
    month = int(b[2:4])
    for offset in (0, 20, 50, 70):  # women add 50; since 2004 also 20 when numbers run out
        if 1 <= month - offset <= 12:
            year = int(b[:2])
            year += 1900 if len(b) == 9 or year >= 54 else 2000
            return is_date(year, month - offset, int(b[4:6]))
    return False


def _birth_number(b: str) -> bool:
    """A Czech or Slovak birth number (rodné číslo) of ten digits: a birth date, divisible by 11."""
    return _cz_birth_date(b) and (int(b) % 11 == 0 or (int(b[:9]) % 11 == 10 and b[9] == "0"))


def _cz(b: str) -> bool:
    if not b.isdigit():
        return False
    if len(b) == 8:  # a company
        return b[0] != "9" and (11 - _weighted(b, (8, 7, 6, 5, 4, 3, 2)) % 11) % 10 == int(b[7])
    if len(b) == 9:  # a person born before 1954
        return b[0] != "6" and _cz_birth_date(b)
    return len(b) == 10 and _birth_number(b)


def _de(b: str) -> bool:
    return len(b) == 9 and b.isdigit() and b[0] != "0" and checksums.mod11_10(b)


def _dk(b: str) -> bool:
    return len(b) == 8 and b.isdigit() and b[0] != "0" and _weighted(b, (2, 7, 6, 5, 4, 3, 2, 1)) % 11 == 0


def _ee(b: str) -> bool:
    return (
        len(b) == 9
        and b.isdigit()
        and b[:2] == "10"
        and (10 - _weighted(b, (3, 7, 1, 3, 7, 1, 3, 7)) % 10) % 10 == int(b[8])
    )


def _el(b: str) -> bool:
    return len(b) == 9 and b.isdigit() and _weighted(b, (256, 128, 64, 32, 16, 8, 4, 2)) % 11 % 10 == int(b[8])


_ES_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _es(b: str) -> bool:
    if len(b) != 9 or not b[1:8].isdigit():
        return False
    first, middle, last = b[0], b[1:8], b[8]
    if first.isdigit():  # a person: DNI number and check letter
        return _ES_LETTERS[int(b[:8]) % 23] == last
    if first in "KLM":
        return _ES_LETTERS[int(middle) % 23] == last
    if first in "XYZ":  # a foreigner: NIE
        return _ES_LETTERS[int(str("XYZ".index(first)) + middle) % 23] == last
    if first in "ABCDEFGHJNPQRSUVW":  # a company: CIF
        total = 0
        for i, c in enumerate(middle):
            n = int(c) * (2 if i % 2 == 0 else 1)
            total += n // 10 + n % 10
        check = (10 - total % 10) % 10
        return last in (str(check), "JABCDEFGHI"[check])
    return False


def _fi(b: str) -> bool:
    return len(b) == 8 and b.isdigit() and _mod11_check(b, (7, 9, 10, 5, 8, 4, 2)) == int(b[7])


_FR_CHECK_LETTERS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _fr(b: str) -> bool:
    if len(b) != 11 or not b[2:].isdigit() or any(c not in _FR_CHECK_LETTERS for c in b[:2]):
        return False
    key, siren = b[:2], b[2:]
    if not checksums.luhn(siren):
        return False
    return not key.isdigit() or int(key) == (12 + 3 * (int(siren) % 97)) % 97


def _hr(b: str) -> bool:
    return len(b) == 11 and b.isdigit() and checksums.mod11_10(b)


def _hu(b: str) -> bool:
    return len(b) == 8 and b.isdigit() and (10 - _weighted(b, (9, 7, 3, 1, 9, 7, 3)) % 10) % 10 == int(b[7])


_IE_CHECK = "WABCDEFGHIJKLMNOPQRSTUV"


def _ie(b: str) -> bool:
    if re.fullmatch(r"\d[A-Z+*]\d{5}[A-W]", b):  # the old layout: 1A23456B
        b = "0" + b[2:7] + b[0] + b[7]
    if not re.fullmatch(r"\d{7}[A-W][A-IW]?", b):
        return False
    total = _weighted(b, (8, 7, 6, 5, 4, 3, 2))
    if len(b) == 9:
        total += 9 * "WABCDEFGHI".index(b[8])
    return _IE_CHECK[total % 23] == b[7]


_IT_OFFICES = frozenset((*range(1, 101), 120, 121, 888, 999))


def _it(b: str) -> bool:
    # The digits 8-10 name the provincial office that issued it.
    return vat_code_valid(b) and int(b[7:10]) in _IT_OFFICES


def _lt(b: str) -> bool:
    if not b.isdigit() or len(b) not in (9, 12) or b[-2] != "1":
        return False
    n = len(b) - 1
    check = sum(int(c) * (1 + i % 9) for i, c in enumerate(b[:n])) % 11
    if check == 10:
        check = sum(int(c) * (1 + (i + 2) % 9) for i, c in enumerate(b[:n])) % 11
    return check % 10 == int(b[n])


def _lu(b: str) -> bool:
    return len(b) == 8 and b.isdigit() and int(b[:6]) % 89 == int(b[6:])


def _lv(b: str) -> bool:
    if len(b) != 11 or not b.isdigit():
        return False
    if b[0] > "3":  # a company
        return _weighted(b, (9, 1, 4, 8, 3, 10, 2, 5, 7, 6, 1)) % 11 == 3
    return personal_code_valid(b)  # a person: the personal code


def _mt(b: str) -> bool:
    return len(b) == 8 and b.isdigit() and b[0] != "0" and _weighted(b, (3, 4, 6, 7, 8, 9, 10, 1)) % 37 == 0


def _nl(b: str) -> bool:
    if len(b) != 12 or b[9] != "B" or not b[:9].isdigit() or not b[10:].isdigit() or int(b[:9]) == 0:
        return False
    if b[10:] == "00":
        return False
    if _weighted(b, (9, 8, 7, 6, 5, 4, 3, 2, -1)) % 11 == 0:
        return True
    return checksums.mod97_10("NL" + b)  # sole traders since 2020


def _pl(b: str) -> bool:
    if len(b) != 10 or not b.isdigit():
        return False
    check = _weighted(b, (6, 5, 7, 2, 3, 4, 5, 6, 7)) % 11
    return check != 10 and check == int(b[9])


def _pt(b: str) -> bool:
    return len(b) == 9 and b.isdigit() and b[0] != "0" and nif_check(b[:8]) == int(b[8])


def _ro(b: str) -> bool:
    if not b.isdigit():
        return False
    if len(b) == 13:  # a person: CNP
        check = _weighted(b, (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)) % 11
        dated = 1 <= int(b[3:5]) <= 12 and 1 <= int(b[5:7]) <= 31
        return b[0] != "0" and dated and (check if check < 10 else 1) == int(b[12])
    if not 6 <= len(b) <= 10 or b[0] == "0":
        return False
    padded = b.zfill(10)
    return _weighted(padded, (7, 5, 3, 2, 1, 7, 5, 3, 2)) * 10 % 11 % 10 == int(padded[9])


def _se(b: str) -> bool:
    return len(b) == 12 and b.isdigit() and b[10:] == "01" and checksums.luhn(b[:10])


def _si(b: str) -> bool:
    if len(b) != 8 or not b.isdigit() or b[0] == "0":
        return False
    check = 11 - _weighted(b, (8, 7, 6, 5, 4, 3, 2)) % 11
    return check != 11 and check % 10 == int(b[7])


def _sk(b: str) -> bool:
    if len(b) != 10 or not b.isdigit():
        return False
    company = b[0] != "0" and b[2] in "234789" and int(b) % 11 == 0
    return company or _birth_number(b)  # a person: the birth number


def _gb(b: str) -> bool:
    if b[:2] in ("GD", "HA"):  # government departments 000-499, health authorities 500-999
        return len(b) == 5 and b[2:].isdigit() and (int(b[2:]) < 500) == (b[:2] == "GD")
    if len(b) == 12:
        b = b[:9]  # a branch
    if len(b) != 9 or not b.isdigit():
        return False
    total = _weighted(b, (8, 7, 6, 5, 4, 3, 2)) + int(b[7:])
    # Numbers from 100 000 000 on may also use the newer rule, 55 added to (or taken from) the sum.
    return total % 97 == 0 or (b[0] != "0" and total % 97 in (42, 55))


def _che(b: str) -> bool:
    return len(b) == 9 and b.isdigit() and _mod11_check(b, (5, 4, 3, 2, 7, 6, 5, 4)) == int(b[8])


def _no(b: str) -> bool:
    return len(b) == 9 and b.isdigit() and _mod11_check(b, (3, 2, 7, 6, 5, 4, 3, 2)) == int(b[8])


@dataclass(frozen=True)
class Vat:
    country: str  # ISO 3166-1 alpha-2 (XI: Northern Ireland)
    name: str
    prefix: str  # as written in front of the number
    body: str  # RE2: the number after the prefix, separators included
    valid: Callable[[str], bool]  # the body, compact
    shape: str
    suffix: str = ""  # RE2: a word that may follow (MVA, MWST)

    @property
    def rule(self) -> str:
        return f"pii:vat-{self.country.lower()}"

    @property
    def pattern(self) -> str:
        return rf"\b{self.prefix}[ -]?({self.body}){self.suffix}\b"


VATS = (
    Vat("AT", "Austria", "AT", r"U\d{8}", _at, "ATUNNNNNNNN"),
    Vat("BE", "Belgium", "BE", r"[01]\d{3}[. ]?\d{3}[. ]?\d{3}|\d{9}", _be, "BENNNN.NNN.NNN"),
    Vat("BG", "Bulgaria", "BG", r"\d{9,10}", _bg, "BGNNNNNNNNN"),
    Vat("CY", "Cyprus", "CY", r"\d{8}[A-Z]", _cy, "CYNNNNNNNNA"),
    Vat("CZ", "Czechia", "CZ", r"\d{8,10}", _cz, "CZNNNNNNNN"),
    Vat("DE", "Germany", "DE", r"\d{3} ?\d{3} ?\d{3}", _de, "DENNNNNNNNN"),
    Vat("DK", "Denmark", "DK", r"\d{2} ?\d{2} ?\d{2} ?\d{2}", _dk, "DKNN NN NN NN"),
    Vat("EE", "Estonia", "EE", r"\d{9}", _ee, "EENNNNNNNNN"),
    Vat("GR", "Greece", "EL", r"\d{9}", _el, "ELNNNNNNNNN"),
    Vat("ES", "Spain", "ES", r"[0-9A-Z]\d{7}[0-9A-Z]", _es, "ESANNNNNNNA"),
    Vat("FI", "Finland", "FI", r"\d{8}", _fi, "FINNNNNNNN"),
    Vat("FR", "France", "FR", r"[0-9A-HJ-NP-Z]{2} ?\d{3} ?\d{3} ?\d{3}", _fr, "FRNN NNN NNN NNN"),
    Vat("HR", "Croatia", "HR", r"\d{11}", _hr, "HRNNNNNNNNNNN"),
    Vat("HU", "Hungary", "HU", r"\d{8}", _hu, "HUNNNNNNNN"),
    Vat("IE", "Ireland", "IE", r"\d[0-9A-Z+*]\d{5}[A-Z]{1,2}", _ie, "IENNNNNNNA"),
    Vat("IT", "Italy", "IT", r"\d{11}", _it, "ITNNNNNNNNNNN"),
    Vat("LT", "Lithuania", "LT", r"\d{12}|\d{9}", _lt, "LTNNNNNNNNN"),
    Vat("LU", "Luxembourg", "LU", r"\d{8}", _lu, "LUNNNNNNNN"),
    Vat("LV", "Latvia", "LV", r"\d{11}", _lv, "LVNNNNNNNNNNN"),
    Vat("MT", "Malta", "MT", r"\d{8}", _mt, "MTNNNNNNNN"),
    Vat("NL", "Netherlands", "NL", r"\d{9}B\d{2}", _nl, "NLNNNNNNNNNBNN"),
    Vat("PL", "Poland", "PL", r"\d{3}-?\d{3}-?\d{2}-?\d{2}", _pl, "PLNNNNNNNNNN"),
    Vat("PT", "Portugal", "PT", r"\d{9}", _pt, "PTNNNNNNNNN"),
    Vat("RO", "Romania", "RO", r"\d{13}|\d{6,10}", _ro, "RONNNNNNNN"),
    Vat("SE", "Sweden", "SE", r"\d{10}01", _se, "SENNNNNNNNNN01"),
    Vat("SI", "Slovenia", "SI", r"\d{8}", _si, "SINNNNNNNN"),
    Vat("SK", "Slovakia", "SK", r"\d{10}", _sk, "SKNNNNNNNNNN"),
    Vat("XI", "Northern Ireland", "XI", r"\d{3} ?\d{4} ?\d{2}(?: ?\d{3})?", _gb, "XINNN NNNN NN"),
    Vat("GB", "United Kingdom", "GB", r"\d{3} ?\d{4} ?\d{2}(?: ?\d{3})?|GD\d{3}|HA\d{3}", _gb, "GBNNN NNNN NN"),
    Vat("CH", "Switzerland", "CHE", r"\d{3}\.?\d{3}\.?\d{3}", _che, "CHE-NNN.NNN.NNN", r"(?: ?(?:MWST|TVA|IVA))?"),
    Vat("NO", "Norway", "NO", r"\d{3} ?\d{3} ?\d{3}", _no, "NONNNNNNNNN", r"(?: ?MVA)?"),
)

_COMPILED = [(vat, re2.compile(vat.pattern, quiet_options())) for vat in VATS]

VALIDATION = (
    "With the country prefix (EL for Greece, CHE for Switzerland), checked by each country's check "
    "digits; an Italian partita IVA is `IT_VAT_CODE` while that type is on."
)


def find(text: str, skip: frozenset[str] = frozenset()) -> Iterator[Span]:
    """VAT numbers in ``text``; ``skip`` names countries another type covers (IT: IT_VAT_CODE)."""
    for vat, regex in _COMPILED:
        if vat.country in skip or vat.prefix not in text:
            continue
        for m in regex.finditer(text):
            start, end = m.span()
            after = text[end : end + 2]
            if len(after) == 2 and after[0] in ".-" and after[1].isdigit():
                continue  # part of a longer number
            if vat.valid(compact(text[m.start(1) : m.end(1)])):
                yield Span(start, end, "VAT_NUMBER", EntityClass.PII, vat.rule, 2.0)  # matcher.SCORE


def generate(original: str, rng: Rng) -> str:
    """A valid VAT number of the same country and layout (new digits; letters and separators kept)."""
    found = next(((vat, m) for vat, regex in _COMPILED if (m := regex.fullmatch(original)) is not None), None)
    if found is None:
        raise ValueError("not a VAT number")
    vat, m = found
    start, end = m.span(1)
    body = by_rejection(original[start:end], rng, lambda candidate: vat.valid(compact(candidate)))
    return original[:start] + body + original[end:]
