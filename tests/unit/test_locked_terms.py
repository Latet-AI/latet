"""The terms a company locks (Latet Enterprise), as the free engine masks them: the settings in use carry them
(``Config.locked_terms``, never a setting), a detector of their own finds them, no exception lets one through, what is
left in clear around one is cut around it, and a value holding one is masked at least as strictly as its type. Without
any, the masking is as it was."""

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from latet.config import Config, ProjectEntry, TermConfig
from latet.engine.build import build_masker
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.engine.verifier import Verifier
from latet.instructions import findings
from latet.projects import effective_config

from ..conftest import ROOT_KEY

FALCON = TermConfig(value="Project Falcon", type="PROJECT")
AROUND = {"name": "roadmap", "type": "ROADMAP", "pattern": r"(?i)Project Falcon \w+", "policy": "allow"}
FORMS = ("Project Falcon", "PROJECT FALCON", "project falcon")


def _masker(config):
    return build_masker(config, Vault(ROOT_KEY))[0]


def _locked(config):
    config = config.model_copy(deep=True)
    config.lock_terms([FALCON])
    return config


def test_without_a_locked_term_the_masking_is_as_it_was():
    """Your own term: your exception lets it through, and what you leave in clear around it leaves it so."""
    mine = Config(ner="off", terms=[FALCON], allow_values=["project falcon"], recognizers=[AROUND])
    masker = _masker(mine)
    assert mine.locked_terms == [] and not any(d.id == "locked-terms" for d in masker.detectors)
    assert masker.mask("see PROJECT FALCON today").text == "see PROJECT FALCON today"
    assert masker.mask("see Project Falcon Phase 2").text == "see Project Falcon Phase 2"
    assert Verifier(masker).verify([("see project falcon today", SlotKind.USER_TEXT, False)], "s").leaks == []


def test_a_locked_term_is_never_let_through_by_an_exception():
    patterns = [r"\AProject Falcon\z", "[A-Z ]{5,}", "(?-i:project falcon)"]
    masker = _masker(_locked(Config(ner="off", allow_values=FORMS, allow_patterns=patterns)))
    for form in FORMS:
        assert "falcon" not in masker.mask(f"see {form} today").text.casefold()
        assert "falcon" not in masker.mask(json.dumps({"notes": form}), SlotKind.TOOL_RESULT).text.casefold()


def test_what_is_left_in_clear_around_a_locked_term_is_cut_around_it():
    masked = _masker(_locked(Config(ner="off", recognizers=[AROUND]))).mask("see Project Falcon Phase 2")
    assert "Falcon" not in masked.text and masked.text.endswith(" Phase 2")
    [replacement] = masked.replacements
    assert (replacement.original, replacement.etype, replacement.rule) == (
        "Project Falcon",
        "PROJECT",
        "company:project",
    )


def test_an_address_left_in_clear_keeps_a_locked_term_masked_inside_it():
    link = {"name": "link", "type": "LINK", "pattern": r"https://[a-z./]+[A-Za-z/]*"}
    config = Config(ner="off", recognizers=[link], policies={"LINK": "allow"})
    config.lock_terms([TermConfig(value="Falcon", type="PROJECT")])
    masked = _masker(config).mask("read https://wiki.acme.com/Falcon/roadmap now")
    assert "Falcon" not in masked.text and "https://wiki.acme.com/" in masked.text and "/roadmap now" in masked.text


def test_a_value_holding_a_locked_term_is_masked_at_least_as_strictly_as_its_type():
    longer = [TermConfig(value="Project Falcon Phase", type="PUBLIC")]
    mine = _locked(Config(ner="off", terms=longer, policies={"PROJECT": "redact"}))
    assert _masker(mine).mask("see Project Falcon Phase 2").text == "see [REDACTED:PUBLIC] 2"


def test_a_projects_rules_carry_the_locked_terms_and_the_dump_never_does():
    mine = _locked(Config(ner="off", projects=[ProjectEntry(path="C:/work/shop", allow_values=["Project Falcon"])]))
    here, _ = effective_config(mine, None, mine.projects[0])
    assert here.locked_terms == [FALCON]
    assert "Falcon" not in _masker(here).mask("see Project Falcon").text
    assert mine.model_dump() == Config.model_validate(mine.model_dump()).model_dump()  # nothing locked in it
    assert "locked" not in json.dumps(mine.model_dump(mode="json")) and "PROJECT" not in json.dumps(mine.model_dump())
    assert Config.model_validate(mine.model_dump()).locked_terms == []  # read from a file: never one


def test_the_checks_never_take_a_locked_term_for_an_exception(tmp_path):
    """The final check of what leaves, and the files auto mode's reviewer is not sent."""
    masker = _masker(_locked(Config(ner="off", allow_values=["project falcon"])))
    assert Verifier(masker).verify([("see project falcon today", SlotKind.USER_TEXT, False)], "s").leaks != []
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("Project Falcon ships in May.\n", "utf-8")
    assert [f.etype for f in findings([instructions], masker)] == ["PROJECT"]


def test_a_locked_term_your_own_entry_also_finds_is_still_no_exception_of_yours(tmp_path):
    """The settings in use hold the company's entry among your terms too (F10): the same place found twice, which the
    files kept from auto mode's reviewer merge before they weigh your exceptions."""
    masker = _masker(_locked(Config(ner="off", terms=[FALCON], allow_patterns=["Project Falcon"])))
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("Project Falcon ships in May.\n", "utf-8")
    assert [f.etype for f in findings([instructions], masker)] == ["PROJECT"]
    mine = _masker(Config(ner="off", terms=[FALCON], allow_patterns=["Project Falcon"]))  # yours alone: let through
    assert findings([instructions], mine) == []


# A company's term written with escapes that write no character (a form's `+`, `%20`, JSON's `\u0020`, `&#32;`), which
# the masking reads again as they read: the company's detector reads that reading too, so no exception of yours lets it
# through there as your own copy of it (the settings in use hold the company's entry among your terms too, F10).
WRITTEN = {
    "a query's +": ("see https://x.example/?q=Project+Falcon&x=1 today", SlotKind.USER_TEXT),
    "%20": ("see https://x.example/Project%20Falcon today", SlotKind.USER_TEXT),
    "JSON's u0020": ('{"notes": "Project\\u0020Falcon"}', SlotKind.TOOL_RESULT),
    "&#32;": ("<p>Project&#32;Falcon</p>", SlotKind.TOOL_RESULT),
}
EXCEPTIONS = {
    "a pattern of any sign between": {"allow_patterns": ["(?i)project.falcon"]},
    "a pattern of letters, + and spaces": {"allow_patterns": ["[A-Za-z+ ]{5,}"]},
    "a value as a query writes it": {"allow_values": ["project+falcon"]},
}


@pytest.mark.parametrize("exception", list(EXCEPTIONS))
@pytest.mark.parametrize("form", list(WRITTEN))
def test_a_locked_term_written_in_escapes_of_no_character_is_never_let_through_by_an_exception(
    tmp_path, form, exception
):
    text, kind = WRITTEN[form]
    masker = _masker(_locked(Config(ner="off", terms=[FALCON], **EXCEPTIONS[exception])))
    masked = masker.mask(text, kind)
    assert "falcon" not in masked.text.casefold()
    assert any(r.etype == "PROJECT" for r in masked.replacements)
    leaks = Verifier(masker).verify([(text, kind, False)], "s").leaks
    assert any(leak.etype == "PROJECT" for leak in leaks)  # the final check stops it, as written
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text(text + "\n", "utf-8")
    assert "PROJECT" in [f.etype for f in findings([instructions], masker)]


# A company's domain inside an address you let through: 0.9.10 keeps such an address whole with its own domain
# (``Masker._merged``'s own domains, ``_whole_addresses`` in a field typed EMAIL), so the locked carve is what cuts the
# address around the company's domain, masked.
DOMAIN = TermConfig(value="acme-intra.it", type="DOMAIN")
ADDRESSES = {
    "a term of yours typed EMAIL": (
        Config(ner="off", terms=[TermConfig(value="ops@acme-intra.it", type="EMAIL")], policies={"EMAIL": "allow"}),
        "write to ops@acme-intra.it today",
        SlotKind.USER_TEXT,
    ),
    "an address the e-mail detector finds": (
        Config(ner="off", pii=["EMAIL"], policies={"EMAIL": "allow"}),
        "write to ops@acme-intra.it today",
        SlotKind.USER_TEXT,
    ),
    "a field typed EMAIL": (
        Config(ner="off", fields=[{"name": "contact", "type": "EMAIL"}], policies={"EMAIL": "allow"}),
        json.dumps({"contact": "ops@acme-intra.it"}),
        SlotKind.TOOL_RESULT,
    ),
}


@pytest.mark.parametrize("address", list(ADDRESSES))
def test_a_locked_domain_inside_an_address_you_let_through_is_masked(address):
    mine, text, kind = ADDRESSES[address]
    assert _masker(mine).mask(text, kind).text == text  # yours: let through whole, its domain with it
    locked = mine.model_copy(deep=True)
    locked.lock_terms([DOMAIN])
    masked = _masker(locked).mask(text, kind)
    assert "acme-intra.it" not in masked.text and "ops@" in masked.text
    assert [(r.etype, r.rule) for r in masked.replacements] == [("DOMAIN", "company:domain")]


PHASE = TermConfig(value="Project Falcon Phase", type="PUBLIC")
CROSS = TermConfig(value="Falcon Phase", type="PROJECT")


def _both(*terms, filler=0):
    """The company's terms, with ``filler`` more of its own (past 2,000: looked up word by word), and PUBLIC let
    through by the company itself."""
    more = [TermConfig(value=f"Client {n:04d}", type="CLIENT") for n in range(filler)]
    config = Config(ner="off", terms=[*terms, *more], policies={"PUBLIC": "allow"})
    config.lock_terms([*terms, *more])
    return config


@pytest.mark.parametrize("filler", [0, 2_001], ids=["an-alternation", "word-by-word"])
def test_a_locked_term_inside_a_longer_one_the_company_lets_through_is_masked(filler):
    """Task 1's leftover: a company term inside a longer company term of a type the company allows: both are found
    (one detector finds each wherever it stands), and what holds the masked one is masked."""
    masker = _masker(_both(FALCON, PHASE, filler=filler))
    assert "Falcon" not in masker.mask("see Project Falcon Phase 2").text
    assert masker.mask("see the Phase 2 plan").text == "see the Phase 2 plan"


@pytest.mark.parametrize("filler", [0, 2_001], ids=["an-alternation", "word-by-word"])
def test_locked_terms_across_one_another_are_each_found(filler):
    masker = _masker(_both(TermConfig(value="Project Falcon", type="PUBLIC"), CROSS, filler=filler))
    masked = masker.mask("see Project Falcon Phase 2").text
    assert "Falcon" not in masked and "Phase" not in masked


def test_a_value_the_vault_knows_is_in_clear_where_it_holds_a_locked_term_whatever_you_let_through():
    """A text sent before, checked for values the vault learnt since (``known_in``): your exception never covers a term
    your company locks."""
    mine = Config(ner="off", allow_patterns=["Project Falcon"])
    masker = _masker(_locked(mine))
    masker.vault.intern("PROJECT", EntityClass.CUSTOM, "Project Falcon")
    assert [(s.start, s.end) for s in masker.known_in("the Project Falcon launch")] == [(4, 18)]
    yours = _masker(mine)
    yours.vault.intern("PROJECT", EntityClass.CUSTOM, "Project Falcon")
    assert yours.known_in("the Project Falcon launch") == []  # your own exception, as before


def test_a_value_the_vault_knows_outside_what_was_masked_holds_a_locked_term_whatever_you_let_through():
    """The same for a text masked before, checked once the vault learnt more (``known_outside``, Latet 0.11's check of
    a cached masking): a value let through by your exception that holds a term your company locks is in clear there."""
    mine = Config(ner="off", allow_patterns=["Project Falcon"])
    masker = _masker(_locked(mine))
    masker.vault.intern("PROJECT", EntityClass.CUSTOM, "Project Falcon")
    assert masker.known_outside("the Project Falcon launch", []) is True
    yours = _masker(mine)
    yours.vault.intern("PROJECT", EntityClass.CUSTOM, "Project Falcon")
    assert yours.known_outside("the Project Falcon launch", []) is False  # your own exception, as before


def test_a_masker_with_locked_terms_or_carved_terms_reads_no_call_joined():
    """Latet 0.11 reads an applications call's texts joined (``Masker.joined``); a masker with the company's locked
    terms reads each text alone, as each cuts what is left in clear around the terms it holds and replaces a value
    holding one at least as strictly as the term's type (``LockedTermsDetector.joinable``)."""
    texts = ["write to Maria about Project Falcon", "and to Luca about the launch"]
    assert _masker(Config(ner="off"))._parts(texts, SlotKind.TOOL_RESULT) == [texts]
    assert _masker(_locked(Config(ner="off")))._parts(texts, SlotKind.TOOL_RESULT) == []
    assert _masker(_carved(Config(ner="off")))._parts(texts, SlotKind.TOOL_RESULT) == []  # terms carved: alike


# With only the company's exceptions counting (Latet Enterprise, `exceptions: company`), a term of yours of a type let
# through stays in use, and every CUSTOM span left in clear is cut around what is masked without them
# (``Config.carves_terms``): a recognizer's match, personal data, a field's value, a company term.
IBAN = "IT60X0542811101000000123456"
TICKET = {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}"}
WIDE = TermConfig(value=f"pay {IBAN} after you see TCK-123456 for Project Falcon", type="PUBLIC")
SENTENCE = f"please pay {IBAN} after you see TCK-123456 for Project Falcon today"


def _carved(config):
    config = config.model_copy(deep=True)
    config.carve_terms()
    return config


def test_a_term_of_yours_left_in_clear_is_cut_around_everything_masked_where_terms_are_carved():
    mine = Config(ner="off", pii=["IBAN"], terms=[WIDE], recognizers=[TICKET], policies={"PUBLIC": "allow"})
    # Yours, without carving: its span outranks what it holds and names the group, which gives way to the values masked
    # inside it (0.9.10: a value let through leaves in clear itself and nothing more).
    alone = _masker(mine).mask(SENTENCE).text
    assert IBAN not in alone and "TCK-123456" not in alone and alone.endswith(" for Project Falcon today")
    for config in (_carved(mine), _locked(_carved(mine))):
        masked = _masker(config).mask(SENTENCE)
        assert IBAN not in masked.text and "TCK-123456" not in masked.text
        assert masked.text.startswith("please pay ") and " after you see " in masked.text
        assert masked.text.endswith(" today")
        assert ("Falcon" in masked.text) == (config.locked_terms == [])  # no company term here unless one is locked
        assert {r.etype for r in masked.replacements} >= {"IBAN", "TICKET"}
    raised = _masker(_carved(mine.model_copy(update={"policies": {}})))  # masked: nothing of it is cut
    assert raised.mask(SENTENCE).text == _masker(mine.model_copy(update={"policies": {}})).mask(SENTENCE).text


CHAINED = "write to TCK-123456.operations-team@acme-intra.it today"


def test_a_term_of_yours_left_in_clear_never_names_a_group_that_holds_a_masked_value_where_terms_are_carved():
    """Round 2: the term meets no masked span itself, only an address let through that holds one (a ticket number the
    address's own group is masked as). Cut around what is masked once the spans but yours are merged, it leaves the
    group as it is without it: masked."""
    base = {"ner": "off", "pii": ["IBAN", "EMAIL"], "recognizers": [TICKET]}
    without = Config(**base, policies={"PUBLIC": "allow", "EMAIL": "allow"})
    mine = Config(
        **base,
        policies={"PUBLIC": "allow", "EMAIL": "allow"},
        terms=[TermConfig(value="operations-team", type="PUBLIC")],
    )
    plain = _masker(_carved(without)).mask(CHAINED).text
    assert "TCK-123456" not in plain and "operations-team" not in plain  # the address masked whole, as the ticket
    # Yours alone, without carving: its span names the group, which gives way to the ticket inside it alone (0.9.10),
    # the rest of the address in clear.
    alone = _masker(mine).mask(CHAINED).text
    assert "TCK-123456" not in alone and ".operations-team@acme-intra.it today" in alone
    masked = _masker(_carved(mine)).mask(CHAINED)
    assert "TCK-123456" not in masked.text and "operations-team" not in masked.text
    assert masked.text.startswith("write to ") and masked.text.endswith(" today")
    assert [r.etype for r in masked.replacements] == ["TICKET"]


def test_a_term_of_yours_left_in_clear_never_takes_the_place_of_a_masked_value_it_equals_in_its_repeats():
    """Round 2: a secret found once by where it stands (an assignment) is masked again wherever else it appears in the
    text (``Masker._repeats``). A term of yours of its value, let through, would have its later occurrences found as
    yours, let through; where terms are carved, the masked value is what is repeated."""
    text = "export API_TOKEN=Zx9qkQ2vTr7wq then Zx9qkQ2vTr7wq again"
    mine = Config(ner="off", terms=[TermConfig(value="Zx9qkQ2vTr7wq", type="PUBLIC")], policies={"PUBLIC": "allow"})
    assert "Zx9qkQ2vTr7wq" not in _masker(_carved(Config(ner="off"))).mask(text).text
    assert "Zx9qkQ2vTr7wq" not in _masker(_carved(mine)).mask(text).text


def test_a_term_of_yours_left_in_clear_that_is_an_addresss_domain_never_keeps_the_domain_from_being_repeated():
    """Round 3: an address brings its domain along, masked wherever else it appears (``Masker._repeats``), unless a
    value found already is the domain. A term of yours of that value, let through, is no such value where terms are
    carved: the domain is repeated all the same."""
    text = "mail ops@acme-intra.it; the site is acme-intra.it and api.acme-intra.it"
    base = {"ner": "off", "pii": ["EMAIL"], "policies": {"PUBLIC": "allow"}}
    assert "acme-intra.it" not in _masker(_carved(Config(**base))).mask(text).text
    mine = Config(**base, terms=[TermConfig(value="acme-intra.it", type="PUBLIC")])
    assert "acme-intra.it" not in _masker(_carved(mine)).mask(text).text


DOMAIN_TEXTS = {
    "after the address": "mail ops@acme-intra.it; the site is acme-intra.it and api.acme-intra.it",
    "before the address": "the site is acme-intra.it and api.acme-intra.it; mail ops@acme-intra.it",
}
HOLDING = {
    "a longer host": [TermConfig(value="api.acme-intra.it", type="PUBLIC")],
    "words around it": [TermConfig(value="site is acme-intra.it", type="PUBLIC")],
    "two terms": [
        TermConfig(value="acme-intra.it", type="PUBLIC"),
        TermConfig(value="api.acme-intra.it", type="PUBLIC"),
    ],
    "another case": [
        TermConfig(value="API.ACME-INTRA.IT", type="PUBLIC"),
        TermConfig(value="Is Acme-Intra.It", type="PUBLIC"),
    ],
}


def _masked_at(masker, text):
    """The characters of ``text`` masked (a replacement that is not let through), by their places."""
    return {i for r in masker.mask(text).replacements for i in range(r.start, r.end)}


@pytest.mark.parametrize("where", list(DOMAIN_TEXTS))
@pytest.mark.parametrize("holding", list(HOLDING))
def test_a_term_of_yours_left_in_clear_that_holds_an_addresss_domain_never_keeps_the_domain_from_being_repeated(
    holding, where
):
    """Round 4: the repeats are found as they are without the spans left in clear (a term of yours holding the domain
    would win the place of the domain's repeat, a longer match), and those spans cut around them: every character
    masked without your terms is masked with them, and the domain is kept with the address's rule."""
    text = DOMAIN_TEXTS[where]
    base = {"ner": "off", "pii": ["EMAIL"], "policies": {"PUBLIC": "allow"}}
    without = _masker(_carved(Config(**base)))
    masker = _masker(_carved(Config(**base, terms=HOLDING[holding])))
    assert "acme-intra.it" not in masker.mask(text).text
    assert _masked_at(without, text) <= _masked_at(masker, text)
    assert masker.vault.by_original("acme-intra.it").rule == without.vault.by_original("acme-intra.it").rule


TOKENS = [
    "ops@acme-intra.it",
    "acme-intra.it",
    "api.acme-intra.it",
    "TCK-123456",
    "TCK-123456.ops@acme-intra.it",
    IBAN,
    "see",
    "the site",
    "Northwind",
]


@st.composite
def texts_and_terms(draw):
    words = draw(st.lists(st.sampled_from(TOKENS), min_size=1, max_size=8))
    seps = draw(st.lists(st.sampled_from([" ", "; ", " and ", "."]), min_size=len(words), max_size=len(words)))
    text = "".join(w + s for w, s in zip(words, seps, strict=True)).strip()
    terms = []
    for _ in range(draw(st.integers(1, 3))):
        start = draw(st.integers(0, len(words) - 1))
        end = draw(st.integers(start + 1, min(len(words), start + 3)))
        value = "".join(w + s for w, s in zip(words[start:end], seps[start:end], strict=True)).strip(" ;.")
        if value:
            terms.append(TermConfig(value=value, type="PUBLIC", case_sensitive=draw(st.booleans())))
    return text, terms


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(texts_and_terms())
def test_where_terms_are_carved_terms_of_yours_left_in_clear_never_unmask_a_character(drawn):
    """Every character the masking masks without your terms let through, it masks with them."""
    text, terms = drawn
    base = {
        "ner": "off",
        "pii": ["EMAIL", "IBAN"],
        "recognizers": [TICKET],
        "policies": {"PUBLIC": "allow", "EMAIL": "allow"},
    }
    without = _masked_at(_masker(_carved(Config(**base))), text)
    assert without <= _masked_at(_masker(_carved(Config(**base, terms=terms))), text), (text, terms)


def test_a_recognizers_find_left_in_clear_is_cut_as_a_term_is_where_terms_are_carved():
    """The cut is for every CUSTOM span left in clear, a recognizer's too (under `exceptions: company` the raise takes
    yours that leave what they find in clear out of use; the engine cuts one all the same)."""
    wide = {"name": "wide", "type": "WIDE", "pattern": r"pay \S+ now"}
    mine = Config(ner="off", pii=["IBAN"], recognizers=[wide], policies={"WIDE": "allow"})
    text = f"please pay {IBAN} now"
    assert IBAN not in _masker(mine).mask(text).text  # without carving: the find gives way to the IBAN (0.9.10)
    masked = _masker(_carved(mine)).mask(text).text
    assert IBAN not in masked and masked.startswith("please pay ") and masked.endswith(" now")


def test_a_term_of_yours_left_in_clear_inside_a_masked_field_leaves_none_of_it_in_clear_where_terms_are_carved():
    mine = Config(
        ner="off",
        terms=[TermConfig(value="Northwind", type="PUBLIC")],
        fields=[{"name": "ledger", "type": "LEDGER"}],
        policies={"PUBLIC": "allow"},
    )
    text = json.dumps({"ledger": "Northwind ledger 7", "note": "Northwind called"})
    sent = _masker(mine).mask(text, SlotKind.TOOL_RESULT).text
    assert '"ledger": "Northwind ' in sent and "ledger 7" not in sent  # the field's value cut around yours
    masked = _masker(_carved(mine)).mask(text, SlotKind.TOOL_RESULT).text
    assert '"ledger": "Northwind' not in masked and "ledger 7" not in masked  # the field's value masked whole
    assert "Northwind called" in masked  # outside it, yours as you set it


def test_an_email_term_of_yours_left_in_clear_still_brings_its_domain_along_where_terms_are_carved():
    email = TermConfig(value="ops@acme-intra.it", type="EMAIL")
    mine = Config(ner="off", pii=["PHONE"], terms=[email], policies={"EMAIL": "allow"})
    masked = _masker(_carved(mine)).mask("write to ops@acme-intra.it; the API is api.acme-intra.it").text
    assert "ops@acme-intra.it" in masked and "api.acme-intra.it" not in masked


def test_terms_carved_are_carried_by_a_copy_name_the_rules_and_never_reach_the_dump():
    from latet.runtime import rules_digest

    mine = _carved(Config(ner="off", projects=[ProjectEntry(path="C:/work/shop")]))
    here, _ = effective_config(mine, None, mine.projects[0])
    assert mine.carves_terms and here.carves_terms and not Config().carves_terms
    assert Config.model_validate(mine.model_dump()).carves_terms is False  # read from a file: never
    assert mine.model_dump() == Config(ner="off", projects=[ProjectEntry(path="C:/work/shop")]).model_dump()
    plain = Config(ner="off", projects=[ProjectEntry(path="C:/work/shop")])
    assert rules_digest(mine) != rules_digest(plain)
    assert rules_digest(_locked(mine)) not in (rules_digest(_locked(plain)), rules_digest(mine))
