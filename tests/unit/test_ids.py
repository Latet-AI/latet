"""National IDs and VAT numbers in text: keywords where they are needed, never inside other numbers."""

import re

import pytest

from latet.config import Config, Profile
from latet.engine.build import detectors_for
from latet.engine.detectors import ids
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.ids import vat
from latet.engine.detectors.pii import PiiDetector, effective_pii
from latet.engine.surrogates.generators import Drbg
from latet.engine.types import SlotKind

from .test_ids_oracle import LAYOUTS, VAT_LAYOUTS

PROSE, CODE = DetectContext(SlotKind.USER_TEXT), DetectContext(SlotKind.TOOL_RESULT)
DETECTOR = PiiDetector()  # every type on
GENERIC = [etype for etype, spec in ids.SPECS.items() if spec.finder is None]


def _example(etype: str, seed: int = 1) -> str:
    return ids.SPECS[etype].generate(LAYOUTS[etype], Drbg(b"t" * 32, f"example-{seed}".encode()))


def _found(text: str, ctx: DetectContext, etype: str) -> list[str]:
    return [text[s.start : s.end] for s in DETECTOR.detect(text, ctx) if s.etype == etype]


def _keyword(etype: str) -> str:
    spec = ids.SPECS[etype]
    return spec.acronyms[0] if spec.acronyms else spec.words[0]


@pytest.mark.parametrize("etype", GENERIC)
def test_found_next_to_its_keyword(etype):
    value = _example(etype)
    for ctx in (PROSE, CODE):
        assert _found(f"{_keyword(etype)}: {value}\n", ctx, etype) == [value]
        assert _found(f"{_keyword(etype)} {value}", ctx, etype) == [value]  # at the very end
        assert _found(f'record = {{"id": 7, "{_keyword(etype)}": "{value}"}}', ctx, etype) == [value]


@pytest.mark.parametrize("etype", GENERIC)
def test_keyword_rules(etype):
    spec, value = ids.SPECS[etype], _example(etype)
    alone = f"the value is {value} here"
    expected = {
        "never": (True, True),
        "bare": (True, True),  # the layouts above are grouped
        "code": (True, False),
        "always": (False, False),
    }[spec.context]
    assert (bool(_found(alone, PROSE, etype)), bool(_found(alone, CODE, etype))) == expected
    if spec.context == "bare":
        bare = "".join(c for c in value if c.isalnum())
        assert not _found(f"the value is {bare} here", CODE, etype)
        assert _found(f"{_keyword(etype)} {bare}", CODE, etype) == [bare]


@pytest.mark.parametrize("etype", [e for e in GENERIC if e not in ("GB_NINO", "US_SSN", "US_ITIN")])
def test_a_wrong_check_digit_is_not_found(etype):  # the others have no check digit
    spec, value = ids.SPECS[etype], _example(etype)
    places = [i for i, c in enumerate(value) if c.isdigit()]
    for i in reversed(places):  # change one digit, ideally the check digit at the end
        for d in "0123456789":
            wrong = value[:i] + d + value[i + 1 :]
            if not spec.validate(wrong):
                assert not _found(f"{_keyword(etype)}: {wrong}", PROSE, etype)
                return
    pytest.fail("no invalid neighbour")


@pytest.mark.parametrize("etype", [e for e in GENERIC if ids.SPECS[e].numeric])
def test_never_inside_longer_numbers_versions_decimals_or_paths(etype):
    value, kw = _example(etype), _keyword(etype)
    for text in (
        f"{kw} 9{value}",
        f"{kw} {value}7",
        f"{kw} v1.2.{value}",
        f"{kw} 0.{value}",
        f"{kw} {value}.5",
        f"{kw} /api/{value}/",
        f"{kw} 12-{value}",
    ):
        assert not _found(text, PROSE, etype), text


def test_scientific_notation_is_no_id():
    assert not _found("Decimal('1.38435736E+12')", CODE, "ES_DNI")
    assert _found("dni 38435736E", CODE, "ES_DNI") in ([], ["38435736E"])  # whatever its letter: no crash


def test_keyword_in_the_csv_column_header():
    value = _example("US_SSN")
    table = f"name,ssn,city\nJane,{value},Boston\n"
    assert _found(table, CODE, "US_SSN") == [value]
    assert not _found(f"name,code,city\nJane,{value},Boston\n", CODE, "US_SSN")


def test_sin_only_in_capitals():
    value = _example("CA_SIN")
    assert _found(f"SIN {value}", CODE, "CA_SIN") == [value]
    assert not _found(f"y = math.sin({value})", CODE, "CA_SIN")


@pytest.mark.parametrize("country", sorted(VAT_LAYOUTS))
def test_vat_numbers_of_every_country(country):
    fake = vat.generate(VAT_LAYOUTS[country], Drbg(b"t" * 32, b"vat"))
    for ctx in (PROSE, CODE):
        spans = [s for s in PiiDetector(["VAT_NUMBER"]).detect(f"Billing to {fake}, thanks", ctx)]
        assert [s.rule for s in spans] == [f"pii:vat-{country.lower()}"]
    assert not list(PiiDetector(["VAT_NUMBER"]).detect(f"Billing to X{fake}", PROSE))


def test_the_italian_vat_number_keeps_its_type():
    text = "Fattura a IT12345678903"
    on = {s.etype for s in PiiDetector(["IT_VAT_CODE", "VAT_NUMBER"]).detect(text, PROSE)}
    assert on == {"IT_VAT_CODE"}
    office_ok = "IT01234560019"  # check digit and a valid office: found as a VAT number without IT_VAT_CODE
    assert {s.etype for s in PiiDetector(["VAT_NUMBER"]).detect(f"VAT {office_ok}", PROSE)} <= {"VAT_NUMBER"}


def test_which_types_are_on():
    base = ["EMAIL", "PHONE", "IBAN", "CREDIT_CARD", "IP_ADDRESS", "CRYPTO_WALLET", "VAT_NUMBER"]
    assert effective_pii(None, ["IT"]) == [*base, "IT_FISCAL_CODE", "IT_VAT_CODE"]
    assert effective_pii(None, ["de", "US"]) == [*base, "DE_TAX_ID", "US_SSN", "US_ITIN"]
    assert set(effective_pii(None, [], every_region=True)) == set(base) | set(ids.ID_TYPES)
    assert effective_pii(["EMAIL"], ["IT"], every_region=True) == ["EMAIL"]  # a list is used as it is
    # Files from before id_regions keep the Italian numbers.
    [pii] = [d for d in detectors_for(Config(), identity=None) if d.id == "pii"]
    assert {"IT_FISCAL_CODE", "IT_VAT_CODE", "US_SSN", "GB_NINO", "VAT_NUMBER"} <= pii.enabled
    assert "DE_TAX_ID" not in pii.enabled
    [every] = [d for d in detectors_for(Config(profile=Profile.MAX), identity=None) if d.id == "pii"]
    assert set(ids.ID_TYPES) <= every.enabled


def test_masked_with_a_valid_fake_and_restored():
    from latet.engine.build import build
    from latet.engine.rehydrate import restore_text, restores_for
    from latet.engine.vault import Vault

    built = build(Config(id_regions=["DE", "ES", "US"]), Vault(b"r" * 32))
    ssn, dni, idnr = _example("US_SSN"), _example("ES_DNI"), _example("DE_TAX_ID")
    vat_number = vat.generate("DE123456789", Drbg(b"t" * 32, b"de"))
    text = f"SSN {ssn}; DNI {dni}; Steuer-ID {idnr}; USt-IdNr. {vat_number}"
    masked = built.masker.mask(text, SlotKind.USER_TEXT)
    fakes = {r.etype: r.replacement for r in masked.replacements}
    assert set(fakes) == {"US_SSN", "ES_DNI", "DE_TAX_ID", "VAT_NUMBER"}
    for etype, fake in fakes.items():
        valid = bool(list(vat.find(fake))) if etype == "VAT_NUMBER" else ids.SPECS[etype].validate(fake)
        assert valid and fake in masked.text, (etype, fake)
    for original in (ssn, dni, idnr, vat_number):
        assert original not in masked.text
    restored, _ = restore_text(masked.text, restores_for(built.vault.entities()))
    assert restored == text


def test_an_id_names_the_span_it_shares_with_a_phone_number():
    from latet.engine.build import build
    from latet.engine.vault import Vault

    masker = build(Config(), Vault(b"r" * 32)).masker
    ssn = "380-23-9090"  # also a valid Italian phone number in prose
    [replacement] = masker.mask(f"SSN {ssn}.", SlotKind.USER_TEXT).replacements
    assert (replacement.etype, replacement.rule) == ("US_SSN", "pii:us-ssn")
    assert ids.SPECS["US_SSN"].validate(replacement.replacement)


# --- Latvian personal codes -------------------------------------------------------------------------------------

LV = "LV_PERSONAL_CODE"
# Invented codes: a person born on 1 January 1900, and one of those issued since July 2017 (no birth date).
LV_DATED, LV_NEW = "010100-12342", "326202-24387"


def _lv_code(first10: str) -> str:
    """The code that starts with these ten digits, closed by its check digit as the reference works it out."""
    from stdnum.lv import pvn

    code = first10 + pvn.calc_check_digit_pers(first10)
    return f"{code[:6]}-{code[6:]}"


@pytest.mark.parametrize("code", [LV_DATED, LV_NEW])
def test_a_latvian_personal_code_is_found_as_it_is_usually_written(code):
    for ctx in (PROSE, CODE):
        assert _found(f"Klienta personas kods: {code}.", ctx, LV) == [code]
        assert _found(f"the client {code} paid", ctx, LV) == [code]  # the layout is enough: no keyword
        assert _found(code, ctx, LV) == [code]  # the whole text
        assert _found(f"name,code,city\nJane,{code},Riga\n", ctx, LV) == [code]


def test_a_latvian_code_written_as_eleven_digits_needs_a_keyword():
    for code in (LV_DATED, LV_NEW):
        bare = code.replace("-", "")
        for ctx in (PROSE, CODE):
            assert not _found(f"order {bare} shipped", ctx, LV)
            assert _found(f"Personas kods: {bare}", ctx, LV) == [bare]
            assert _found(f"personal code {bare}", ctx, LV) == [bare]
            assert _found(f"personas_kods,name\n{bare},Jane\n", ctx, LV) == [bare]  # the column header
            assert not _found(f"name,city\n{bare},Riga\n", ctx, LV)


@pytest.mark.parametrize("code", [LV_DATED, LV_NEW])
def test_a_latvian_code_is_not_taken_from_inside_a_longer_number(code):
    bare = code.replace("-", "")
    assert _found(f"personas kods {bare}", PROSE, LV) == [bare] and _found(f"x {code}", PROSE, LV) == [code]
    for text in (
        f"personas kods 9{bare}",
        f"personas kods {bare}7",  # twelve digits
        f"personas kods {bare}.5",
        f"personas kods 12-{bare}",
        f"9{code}",
        f"{code}7",
        f"{code}-1",
        f"12-{code}",
    ):
        for ctx in (PROSE, CODE):
            assert not _found(text, ctx, LV), text


def test_what_is_no_latvian_personal_code_is_not_found():
    assert _found(f"personas kods {LV_DATED}", PROSE, LV) == [LV_DATED]  # the same text with a code is found
    cases = {
        "a wrong check digit": LV_DATED[:-1] + "3",
        "a wrong check digit, since July 2017": LV_NEW[:-1] + "8",
        "31 February": _lv_code("3102001234"),
        "29 February 1900": _lv_code("2902001234"),
        "a month 13": _lv_code("0113001234"),
        "a day that is neither a date nor 32": _lv_code("3301001234"),
        "a century digit that is not 0, 1 or 2": _lv_code("0101003234"),
        "a company's number": "412345-67891",
    }
    for why, code in cases.items():
        for ctx in (PROSE, CODE):
            assert not _found(f"personas kods {code}", ctx, LV), why
            assert not _found(f"personas kods {code.replace('-', '')}", ctx, LV), why


def test_a_latvian_vat_number_stays_a_vat_number():
    text = "VAT LV01010012342."
    assert [(s.etype, text[s.start : s.end]) for s in DETECTOR.detect(text, PROSE)] == [("VAT_NUMBER", "LV01010012342")]


def test_latvia_is_a_country_whose_numbers_are_on_when_it_is_chosen_and_in_max():
    assert Config(id_regions=["lv"]).id_regions == ["LV"]
    assert ids.types_for(["lv"]) == [LV] and ids.SPECS[LV].country == "LV"
    assert LV in effective_pii(None, ["LV"]) and LV not in effective_pii(None, ["IT", "US", "GB"])
    [on] = [d for d in detectors_for(Config(id_regions=["LV"]), identity=None) if d.id == "pii"]
    [off] = [d for d in detectors_for(Config(), identity=None) if d.id == "pii"]
    assert LV in on.enabled and LV not in off.enabled
    [every] = [d for d in detectors_for(Config(profile=Profile.MAX), identity=None) if d.id == "pii"]
    assert LV in every.enabled


def test_a_latvian_code_is_masked_with_a_valid_fake_of_its_written_form_and_restored():
    from stdnum.lv import pvn

    from latet.engine.build import build
    from latet.engine.rehydrate import restore_text, restores_for
    from latet.engine.vault import Vault

    built = build(Config(id_regions=["LV"]), Vault(b"r" * 32))
    bare = LV_DATED.replace("-", "")
    text = f"Personas kods {LV_DATED}; jaunais kods {LV_NEW}; personas kods {bare}."
    masked = built.masker.mask(text, SlotKind.USER_TEXT)
    assert {r.rule for r in masked.replacements} == {"pii:lv-personal-code"}
    fakes = {r.original: r.replacement for r in masked.replacements if r.etype == LV}
    assert set(fakes) == {LV_DATED, LV_NEW, bare}
    for original, fake in fakes.items():
        assert fake != original and fake in masked.text and original not in masked.text
        assert ids.SPECS[LV].validate(fake) and pvn.is_valid(fake)
        assert re.sub(r"\d", "N", fake) == re.sub(r"\d", "N", original)  # the hyphen where it was, or none
        assert (fake[:2] == "32") == (original[:2] == "32")  # a code of July 2017 stays one, a dated code too
    restored, _ = restore_text(masked.text, restores_for(built.vault.entities()))
    assert restored == text
