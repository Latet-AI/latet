"""The check-digit algorithms agree with python-stdnum (test-only reference), and the keyword
context finds words where a reader would."""

from hypothesis import given
from hypothesis import strategies as st
from stdnum import damm as std_damm
from stdnum import ean as std_ean
from stdnum import iban as std_iban
from stdnum import luhn as std_luhn
from stdnum import verhoeff as std_verhoeff
from stdnum.iso7064 import mod_11_10 as std_mod_11_10
from stdnum.iso7064 import mod_97_10 as std_mod_97_10

from latet.engine.corpus import benign_sample, stdlib_files
from latet.engine.detectors import checksums
from latet.engine.detectors.context import ContextWords
from latet.engine.surrogates.layout import fill_like

digits = st.text(alphabet="0123456789", min_size=2, max_size=24)
alnum = st.text(alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=30)


@given(digits)
def test_luhn_matches_stdnum(number):
    assert checksums.luhn(number) == std_luhn.is_valid(number)
    assert checksums.luhn(number[:-1] + checksums.luhn_digit(number[:-1]))


@given(digits)
def test_mod11_10_matches_stdnum(number):
    assert checksums.mod11_10(number) == std_mod_11_10.is_valid(number)
    payload = number[:-1]
    assert std_mod_11_10.is_valid(payload + checksums.mod11_10_digit(payload))


@given(alnum)
def test_mod97_10_matches_stdnum(value):
    assert checksums.mod97_10(value) == std_mod_97_10.is_valid(value)


@given(digits)
def test_ean_verhoeff_damm_match_stdnum(number):
    assert checksums.ean(number) == (std_ean.calc_check_digit(number[:-1]) == number[-1])
    assert checksums.verhoeff(number) == std_verhoeff.is_valid(number)
    assert checksums.damm(number) == std_damm.is_valid(number)
    assert checksums.ean(number[:-1] + checksums.ean_digit(number[:-1]))


def test_iban_and_weighted():
    assert checksums.iban("IT60X0542811101000000123456") and std_iban.is_valid("IT60X0542811101000000123456")
    assert not checksums.iban("IT61X0542811101000000123456")
    # Dutch BSN-style "11 test": weights 9..2 and -1 for the last digit.
    assert checksums.weighted("111222333", (9, 8, 7, 6, 5, 4, 3, 2, -1), 11)
    assert not checksums.weighted("111222334", (9, 8, 7, 6, 5, 4, 3, 2, -1), 11)
    assert not checksums.weighted("", (1,), 11) and not checksums.luhn("7") and not checksums.luhn("12a4")


def test_registry_and_compact():
    assert {"luhn", "iban", "mod97-10", "mod11-10", "ean", "verhoeff", "damm"} <= set(checksums.CHECKSUMS)
    assert checksums.compact("DE 123-456", "digits") == "123456"
    assert checksums.compact("de 12-ab", "alnum") == "DE12AB"


def test_context_words_on_the_same_line():
    words = ContextWords(["bsn", "burgerservicenummer"], acronyms=["SIN"])
    text = "id = 7\nBSN: 111222333 (verified)\nother 444555666"
    value = text.index("111222333")
    assert words.near(text, value, value + 9)
    other = text.index("444555666")
    assert not words.near(text, other, other + 9)  # the keyword is on another line
    far = "bsn" + " " * 60 + "111222333"
    assert not words.near(far, far.index("111"), len(far))  # beyond the window
    assert words.near("value 111222333 is my bsn", 6, 15)  # after the value, within 24 characters


def test_context_words_case_rules():
    sin = ContextWords(acronyms=["SIN"])
    assert sin.near("SIN 046 454 286", 4, 15)
    assert not sin.near("math.sin(046454286)", 9, 18)  # acronyms are case-sensitive
    word = ContextWords(["ssn"])
    assert word.near("Customer SSN: 123-45-6789", 14, 25)
    assert not word.near("lessn 123-45-6789", 6, 17)  # whole words only
    assert ContextWords(["iva"], mode="substring").near("P.IVA 12345678903", 6, 17)


def test_context_words_in_a_table_header():
    words = ContextWords(["bsn"])
    csv = "name,bsn,city\nAnna,111222333,Utrecht\nBram,123456782,Delft"
    value = csv.index("123456782")
    assert words.near(csv, value, value + 9)
    city = csv.index("Delft")
    assert not words.near(csv, city, city + 5)  # another column
    read = "     1\tname|bsn\n     2\tAnna|111222333"  # Claude Code's Read prefix is skipped
    assert words.near(read, read.index("111222333"), len(read))
    assert ContextWords().near("bsn 111222333", 4, 13) is False


def test_fill_like_keeps_layout_and_case():
    assert fill_like("de 123.456.789", "DE987654321") == "de 987.654.321"
    assert fill_like("AB-12", "xy99") == "XY-99"
    assert fill_like("12 34", "5678") == "56 78"


def test_corpus_sample():
    assert len(stdlib_files()) >= 25
    sample = benign_sample()
    assert "def " in sample and len(sample) > 10_000
