"""National ID and VAT validators against python-stdnum (a test-only reference, never shipped).

Every surrogate must be valid for stdnum, and our validator must agree with stdnum on valid numbers
and on numbers one digit away from valid. Where the two disagree on purpose, ``KNOWN`` says why.
"""

import importlib
import re

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from latet.engine.detectors import ids
from latet.engine.detectors.ids import vat
from latet.engine.surrogates.generators import Drbg

KEY = b"k" * 32

# The layout each fake is drawn in (any value of the right shape: it is never checked itself).
LAYOUTS = {
    "IT_FISCAL_CODE": "RSSMRA85T10A562S",
    "IT_VAT_CODE": "12345678903",
    "ES_DNI": "12345678Z",
    "ES_NIE": "X1234567L",
    "CH_AHV": "756.1234.5678.97",
    "FI_HETU": "131052-308T",
    "FR_NIR": "1 85 05 78 006 084 36",
    "BE_NATIONAL_NUMBER": "85.07.30-033.28",
    "PL_PESEL": "44051401359",
    "SE_PERSONNUMMER": "900101-1234",
    "LV_PERSONAL_CODE": "010100-12342",
    "GB_NINO": "AB 12 34 56 C",
    "DE_TAX_ID": "12 345 678 901",
    "NL_BSN": "1234.56.782",
    "PT_NIF": "123 456 789",
    "US_SSN": "123-45-6789",
    "US_ITIN": "912-70-1234",
    "CA_SIN": "123 456 782",
}
ORACLES = {
    "IT_FISCAL_CODE": "stdnum.it.codicefiscale",
    "IT_VAT_CODE": "stdnum.it.iva",
    "ES_DNI": "stdnum.es.dni",
    "ES_NIE": "stdnum.es.nie",
    "CH_AHV": "stdnum.ch.ssn",
    "FI_HETU": "stdnum.fi.hetu",
    "FR_NIR": "stdnum.fr.nir",
    "BE_NATIONAL_NUMBER": "stdnum.be.nn",
    "PL_PESEL": "stdnum.pl.pesel",
    "SE_PERSONNUMMER": "stdnum.se.personnummer",
    "LV_PERSONAL_CODE": "stdnum.lv.pvn",
    "DE_TAX_ID": "stdnum.de.idnr",
    "NL_BSN": "stdnum.nl.bsn",
    "PT_NIF": "stdnum.pt.nif",
    "US_SSN": "stdnum.us.ssn",
    "US_ITIN": "stdnum.us.itin",
    "CA_SIN": "stdnum.ca.sin",
}
VAT_LAYOUTS = {
    "AT": "ATU12345678",
    "BE": "BE0123456789",
    "BG": "BG123456789",
    "CY": "CY12345678A",
    "CZ": "CZ12345678",
    "DE": "DE123456789",
    "DK": "DK12345678",
    "EE": "EE101234567",
    "GR": "EL123456789",
    "ES": "ESA12345674",
    "FI": "FI12345678",
    "FR": "FR12345678901",
    "HR": "HR12345678901",
    "HU": "HU12345678",
    "IE": "IE1234567T",
    "IT": "IT12345678903",
    "LT": "LT123456711",
    "LU": "LU12345678",
    "LV": "LV41234567891",
    "MT": "MT12345678",
    "NL": "NL123456789B01",
    "PL": "PL1234567890",
    "PT": "PT123456789",
    "RO": "RO12345678",
    "SE": "SE123456789001",
    "SI": "SI12345678",
    "SK": "SK2023456789",
    "XI": "XI123456789",
    "GB": "GB123456789",
    "CH": "CHE-123.456.789 MWST",
    "NO": "NO 123456789 MVA",
}
VAT_ORACLES = {"XI": "stdnum.gb.vat", "GB": "stdnum.gb.vat", "CH": "stdnum.ch.vat", "NO": "stdnum.no.mva"}

# Where Latet and stdnum part ways on purpose.
KNOWN = {
    # stdnum also checks the birth date and place; Latet only the check character, as before.
    "IT_FISCAL_CODE": "check character only",
    # stdnum also checks the provincial office; the partita IVA kept its original check.
    "IT_VAT_CODE": "check digit only",
}


def _expected_difference(kind: str, value: str) -> bool:
    d = "".join(c for c in value if c.isdigit())
    if kind == "US_ITIN":
        return 50 <= int(d[3:5]) <= 65  # issued since 2011; stdnum knows the older groups only
    if kind == "LV":
        # A dated code's century digit is 0, 1 or 2 and stdnum takes any; a code starting with 32 holds no date.
        return d[0] <= "3" and d[6] > "2" and d[:2] != "32"
    if kind == "LV_PERSONAL_CODE":
        # stdnum's PVN also takes a company's number (it starts with 4 to 9), and the same century digits as above.
        return d[0] > "3" or (d[6] > "2" and d[:2] != "32")
    if kind in ("CZ", "SK") and len(d) == 10:
        # Birth numbers add 0, 20, 50 or 70 to the month; stdnum also takes other months (96: June).
        month = int(d[2:4])
        return not any(1 <= month - offset <= 12 for offset in (0, 20, 50, 70))
    return False


def _oracle(name: str):
    return importlib.import_module(name)


def _fakes(layout: str, generate, n: int) -> list[str]:
    return [generate(layout, Drbg(KEY, f"seed-{i}".encode())) for i in range(n)]


@pytest.mark.parametrize("etype", sorted(LAYOUTS))
def test_every_fake_id_is_valid_and_keeps_its_layout(etype):
    spec = ids.SPECS[etype]
    oracle = _oracle(ORACLES[etype]) if etype in ORACLES else None
    layout = LAYOUTS[etype]
    for fake in _fakes(layout, spec.generate, 200):
        assert spec.validate(fake), (etype, fake)
        assert len(fake) == len(layout) and [c.isalnum() for c in fake] == [c.isalnum() for c in layout], fake
        if oracle is not None:
            assert oracle.is_valid(fake), (etype, fake)


@pytest.mark.parametrize("country", sorted(VAT_LAYOUTS))
def test_every_fake_vat_number_is_valid(country):
    from stdnum.eu import vat as eu_vat

    layout = VAT_LAYOUTS[country]
    oracle = _oracle(VAT_ORACLES[country]).is_valid if country in VAT_ORACLES else eu_vat.is_valid
    for fake in _fakes(layout, vat.generate, 200):
        spans = list(vat.find(f"VAT {fake}."))
        assert [s.rule for s in spans] == [f"pii:vat-{country.lower()}"], fake
        assert oracle(fake), (country, fake)


def _mutated(value: str, position: int, digit: int) -> str:
    places = [i for i, c in enumerate(value) if c.isdigit()]
    i = places[position % len(places)]
    return value[:i] + str(digit) + value[i + 1 :]


@pytest.mark.parametrize("etype", sorted(set(ORACLES) - set(KNOWN)))
@settings(max_examples=300, deadline=None)
@given(seed=st.integers(0, 10**9), position=st.integers(0, 40), digit=st.integers(0, 9))
def test_id_validator_agrees_with_stdnum_near_valid_numbers(etype, seed, position, digit):
    spec = ids.SPECS[etype]
    fake = spec.generate(LAYOUTS[etype], Drbg(KEY, str(seed).encode()))
    candidate = _mutated(fake, position, digit)
    assume(not _expected_difference(etype, candidate))
    assert spec.validate(candidate) == _oracle(ORACLES[etype]).is_valid(candidate), (etype, candidate)


@pytest.mark.parametrize("country", sorted(VAT_LAYOUTS))
@settings(max_examples=300, deadline=None)
@given(seed=st.integers(0, 10**9), position=st.integers(0, 40), digit=st.integers(0, 9))
def test_vat_validator_agrees_with_stdnum_near_valid_numbers(country, seed, position, digit):
    from stdnum.eu import vat as eu_vat

    oracle = _oracle(VAT_ORACLES[country]).is_valid if country in VAT_ORACLES else eu_vat.is_valid
    fake = vat.generate(VAT_LAYOUTS[country], Drbg(KEY, str(seed).encode()))
    candidate = _mutated(fake, position, digit)
    assume(not _expected_difference(country, candidate))
    ours = any(True for _ in vat.find(candidate))
    assert ours == oracle(candidate), (country, candidate)


# Latvian numbers and whether stdnum takes them. A company's number starts with 4 to 9. A person's code holds a birth
# date (DDMMYY, a century digit, three digits, the check digit) or, when issued since July 2017, starts with 32 and
# holds none.
LV_NUMBERS = [
    pytest.param("41234567891", True, id="company"),
    pytest.param("41234567892", False, id="company, wrong check digit"),
    pytest.param("01010012342", True, id="person with a birth date"),
    pytest.param("01010012343", False, id="person with a birth date, wrong check digit"),
    pytest.param("31020012343", False, id="person born on 31 February"),
    pytest.param("32620224387", True, id="person since July 2017"),
    pytest.param("32123456785", True, id="person since July 2017, digits in a row"),
    pytest.param("32620224388", False, id="person since July 2017, wrong check digit"),
    pytest.param("33620224381", False, id="day 33 is neither a birth date nor 32"),
    pytest.param("31620224382", False, id="day 31 of month 62"),
]


@pytest.mark.parametrize(("number", "valid"), LV_NUMBERS)
def test_a_latvian_number_is_found_when_stdnum_takes_it(number, valid):
    from stdnum.eu import vat as eu_vat

    assert eu_vat.is_valid(f"LV{number}") is valid  # the table is right
    text = f"VAT LV{number}."
    assert [text[s.start : s.end] for s in vat.find(text)] == ([f"LV{number}"] if valid else [])


def test_a_latvian_code_needs_a_birth_date_unless_it_starts_with_32():
    from stdnum.eu import vat as eu_vat
    from stdnum.lv import pvn

    # Every start a code can have (DDMM, 0000 to 3999), one tail, the correct check digit.
    bodies = (f"{ddmm:04d}991234" for ddmm in range(4000))
    numbers = [body + pvn.calc_check_digit_pers(body) for body in bodies]
    ours = {n for n in numbers if list(vat.find(f"LV{n}"))}
    assert ours == {n for n in numbers if eu_vat.is_valid(f"LV{n}")}
    assert sum(n.startswith("32") for n in ours) == 100  # all of them: a date is not read


def test_a_latvian_code_starting_with_32_is_taken_whatever_its_seventh_digit():
    from stdnum.eu import vat as eu_vat

    # 32, any two digits, any seventh digit (a dated code's century digit), then each of the ten last digits.
    bodies = [f"32{mm:02d}99{seventh}234" for mm in range(100) for seventh in "0123456789"]
    numbers = [body + last for body in bodies for last in "0123456789"]
    ours = {n for n in numbers if list(vat.find(f"LV{n}"))}
    assert ours == {n for n in numbers if eu_vat.is_valid(f"LV{n}")}
    assert len(ours) == len(bodies)  # the check digit of each body, no other last digit


# A Latvian personal code as it is usually written: the birth date (DDMMYY), a hyphen, a century digit (0, 1 or 2 for
# the 1800s, 1900s and 2000s), three digits and the check digit; since July 2017 a code starts with 32 and holds no
# date. A layout need not be a valid code (it is never checked itself), only say which kind to make.
LV_FORMS = [
    pytest.param("010100-12342", id="dated, 1900s, with its hyphen"),
    pytest.param("01010012342", id="dated, 1900s, as eleven digits"),
    pytest.param("150580-01234", id="dated, 1800s"),
    pytest.param("150510-21234", id="dated, 2000s"),
    pytest.param("326202-24387", id="since July 2017, with its hyphen"),
    pytest.param("32620224387", id="since July 2017, as eleven digits"),
]


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


@pytest.mark.parametrize("layout", LV_FORMS)
def test_every_fake_latvian_code_is_valid_and_of_the_same_written_form(layout):
    from stdnum.lv import pvn

    spec = ids.SPECS["LV_PERSONAL_CODE"]
    original = _digits(layout)
    for fake in _fakes(layout, spec.generate, 200):
        assert spec.validate(fake) and pvn.is_valid(fake), fake
        assert re.sub(r"\d", "N", fake) == re.sub(r"\d", "N", layout), fake  # the hyphen where it was, or none
        d = _digits(fake)
        assert (d[:2] == "32") == (original[:2] == "32"), fake  # a code of July 2017 stays one, a dated code too
        if d[:2] != "32":
            assert d[6] == original[6], fake  # born in the same century
            assert d[6] != "2" or int(d[4:6]) < 20, fake  # and in the 2000s no later than 2019


class _TwoValues:
    """A generator that draws from a real one but never beyond 0 or 1: the fakes it can make are few, and one of
    them is the original, so a fake that must differ from the original shows it."""

    def __init__(self, inner: Drbg) -> None:
        self.inner = inner

    def below(self, n: int) -> int:
        return self.inner.below(min(n, 2))

    def choice(self, seq):
        return seq[self.below(len(seq))]


@pytest.mark.parametrize(
    "body", [pytest.param("0101001000", id="dated"), pytest.param("3200000000", id="since July 2017")]
)
def test_a_fake_latvian_code_is_never_the_original(body):
    from stdnum.lv import pvn

    spec = ids.SPECS["LV_PERSONAL_CODE"]
    original = body[:6] + "-" + body[6:] + pvn.calc_check_digit_pers(body)
    # Every draw is 0 or 1, so the original is one of the 64 (dated) or 256 fakes the draws can make: 3000 fakes meet
    # it many times over unless it is kept out.
    fakes = {spec.generate(original, _TwoValues(Drbg(KEY, f"seed-{i}".encode()))) for i in range(3000)}
    assert original not in fakes
    assert len(fakes) >= 10 and all(spec.validate(f) and pvn.is_valid(f) for f in fakes)


def test_a_latvian_personal_code_is_taken_when_stdnum_takes_it():
    from stdnum.lv import pvn

    spec = ids.SPECS["LV_PERSONAL_CODE"]
    # Every start a code can have (DDMM, 0000 to 3999), three years (29 February 2000 is a date, 29 February 1900 and
    # 1800 are not), each century digit, one tail, the correct check digit.
    bodies = [
        f"{ddmm:04d}{year}{century}234"
        for ddmm in range(4000)
        for year in ("00", "04", "99")
        for century in "0123456789"
    ]
    codes = [body + pvn.calc_check_digit_pers(body) for body in bodies]
    ours = {c for c in codes if spec.validate(c)}
    # stdnum takes any century digit of a dated code and the personal code only 0, 1 and 2; a code starting with 32
    # holds no date, so its seventh digit is free for both.
    assert ours == {c for c in codes if pvn.is_valid(c) and (c[6] in "012" or c[:2] == "32")}
    assert any(c[:2] == "32" for c in ours) and any(c[:2] != "32" for c in ours)
    assert any(c.startswith("2902002") for c in ours) and not any(c.startswith("2902001") for c in ours)
    # The VAT number of a person is the same code, read by the same check (every thirteenth code, for speed).
    sample = codes[::13]
    assert {c for c in sample if list(vat.find(f"LV{c}"))} == {c for c in sample if spec.validate(c)}
