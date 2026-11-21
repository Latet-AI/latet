"""Raising settings to the company's floor never lowers them, leaves nothing below it, and is done once and for all;
and the engine built on raised settings masks every company term, in every case it is matched in: for any policy and
any settings, projects included."""

import json
import re
import unicodedata

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from latet.config import Config, FieldRule, ProjectEntry, ProjectSettings, TermConfig
from latet.ee.policy import _folder, _raised, below_floor, floor_from, raise_to_floor
from latet.engine.build import build_masker
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.projects import effective_config
from latet.protection import loosened

from ..conftest import ROOT_KEY

GUIDANCE = "the local reviewer: its guidance changed"  # the company's guidance joins yours: a change of free text
TERMS = [
    TermConfig(value="Project Falcon", type="PROJECT"),
    TermConfig(value="Northwind Traders", type="CLIENT"),
    # The company's value as another type, in its own case only (H1), or whole words only and in its own case (F10).
    TermConfig(value="Project Falcon", type="PUBLIC", case_sensitive=True),
    TermConfig(value="project falcon", type="PUBLIC", case_sensitive=True, whole_word=False),
    TermConfig(value="Project Falcon Phase", type="PUBLIC"),
    # Letters a text may write composed or decomposed, and one RE2 folds unlike casefold (a sharp s is not ss there).
    TermConfig(value="Caf\N{LATIN SMALL LETTER E WITH ACUTE} Nord", type="CLIENT"),
    TermConfig(value="Stra\N{LATIN SMALL LETTER SHARP S}e AG", type="CLIENT"),
    TermConfig(value="STRASSE AG", type="CLIENT"),
    TermConfig(value="ops@acme-intra.it", type="EMAIL"),  # it brings its domain along, let through or not
]
RECOGNIZERS = [
    {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}"},
    {"name": "ticket", "type": "TICKET", "pattern": "T-[0-9]+"},
    {"name": "jira", "type": "TICKET", "pattern": "JIRA-[0-9]+"},  # the type of another, under a name of its own
    {"name": "badge", "type": "BADGE", "pattern": "BD-[0-9]{6}", "policy": "allow"},
    {"name": "roadmap", "type": "ROADMAP", "pattern": r"(?i)Project Falcon \w+", "policy": "allow"},  # around a term
]
FIELDS = [
    FieldRule(name="notes"),
    FieldRule(name="notes", mask=False),
    FieldRule(name="notes", secret=True),
    FieldRule(name="iban", type="IBAN"),
    FieldRule(name="*", mask=False),
    FieldRule(name="customer.*", type="CUSTOMER"),
    FieldRule(name="contact", type="EMAIL"),
]
REGIONS = st.lists(st.sampled_from(["IT", "DE", "FR", "US"]), unique=True, max_size=3)
VALUES = {
    "profile": st.sampled_from(["secrets", "balanced", "max"]),
    "pii": st.lists(
        st.sampled_from(["EMAIL", "PHONE", "IBAN", "IT_FISCAL_CODE", "DE_TAX_ID"]), unique=True, max_size=4
    ),
    "id_regions": REGIONS,
    "phone_regions": REGIONS,
    "include_private_ips": st.booleans(),
    "ner": st.sampled_from(["auto", "off"]),
    "ocr": st.sampled_from(["auto", "off"]),
    "terms": st.lists(
        st.sampled_from(TERMS), unique_by=lambda t: (t.value, t.type, t.case_sensitive, t.whole_word), max_size=3
    ),
    "recognizers": st.lists(st.sampled_from(RECOGNIZERS), max_size=3),
    "fields": st.lists(st.sampled_from(FIELDS), max_size=3),
    "policies": st.dictionaries(
        st.sampled_from(["EMAIL", "PROJECT", "IBAN", "PUBLIC", "TICKET", "ROADMAP", "CUSTOMER", "FIELD", "CLIENT"]),
        st.sampled_from(["allow", "mask", "redact"]),
        max_size=3,
    ),
    "data": st.sampled_from(["normal", "strict"]),
    "data_tools": st.lists(st.sampled_from(["psql", "mcp__postgres__*"]), unique=True, max_size=2),
    "data_files": st.lists(st.sampled_from(["*.csv", "exports/**"]), unique=True, max_size=2),
    "data_hosts": st.lists(st.sampled_from(["*.salesforce.com", "api.acme.com"]), unique=True, max_size=2),
    "dump_guard": st.integers(0, 40),
    "images": st.sampled_from(["allow", "mask", "block"]),
    "allow_values": st.lists(
        st.sampled_from(["project falcon", "acme.com", "PROJECT FALCON", "strasse ag"]), unique=True, max_size=2
    ),
    "allow_patterns": st.lists(
        st.sampled_from(
            [
                r"Project \w+",
                r"[a-z]+\.com",
                "PROJECT FALCON",
                "(?-i:PROJECT FALCON)",
                "[A-Z ]{5,}",
                r"\AProject Falcon\z",  # anchored to the whole text: what a value alone is
                r"\A.+\z",
            ]
        ),
        unique=True,
        max_size=2,
    ),
    "disabled_secret_rules": st.lists(st.sampled_from(["generic-api-key"]), max_size=1),
}


@st.composite
def some_rules(draw, required=()):
    keys = draw(st.lists(st.sampled_from(sorted(VALUES)), unique=True))
    rules = {key: draw(VALUES[key]) for key in keys}
    for key in required:
        rules[key] = draw(VALUES[key].filter(bool))
    return rules


@st.composite
def floors(draw, required=()):
    guidance = draw(st.sampled_from([None, "Ward names are confidential."]))
    settings = ProjectSettings.model_validate({**draw(some_rules(required)), "review_guidance": guidance})
    return floor_from(settings, exceptions=draw(st.sampled_from(["personal", "company"])))


@st.composite
def configs(draw):
    names = draw(st.lists(st.sampled_from(["shop", "billing"]), unique=True, max_size=2))
    projects = [
        ProjectEntry.model_validate({**draw(some_rules()), "path": f"C:/work/{name}", "enabled": draw(st.booleans())})
        for name in names
    ]
    guidance = draw(st.sampled_from([None, "Ticket numbers are fine."]))
    return Config.model_validate({**draw(some_rules()), "llm_review": {"guidance": guidance}, "projects": projects})


# Under `exceptions: company` the raise takes a field rule of yours of a type let through out of use (S1), an exception
# only the company's count, which ``loosened`` says as a field rule changed in its place (one still masks something: an
# EMAIL one brings its domain along, 0.9.8). Nothing else of yours goes: a term of yours of such a type stays in use, cut
# around what is masked without it (``Config.carves_terms``).
FIELDS_CHANGED = re.compile(r"(?:project (?P<one>[^:]+)|projects (?P<many>[^:]+)): field rules: changed or removed")


def _without(config, excepted):
    """``config`` without the field rules the raise took out of use (``_Raise.excepted_fields``: where they are among
    yours, by folder)."""

    def kept(rules, folder):
        return [rule for at, rule in enumerate(rules) if at not in excepted.get(folder, ())]

    projects = [e.model_copy(update={"fields": kept(e.fields, _folder(e))}) for e in config.projects]
    return config.model_copy(update={"fields": kept(config.fields, ""), "projects": projects})


def _taken_out(reason, excepted):
    """Whether ``reason`` is a field rule said changed only in places where the raise took one of yours out of use."""
    if reason == "field rules: changed or removed":
        return "" in excepted
    found = FIELDS_CHANGED.fullmatch(reason)
    if found is None:
        return False
    names = [found["one"]] if found["one"] else re.split(r", | and ", found["many"])
    return all(name in excepted for name in names)


S1_FLOOR = floor_from(ProjectSettings.model_validate({"policies": {"IBAN": "allow"}}), exceptions="company")
S1_FIELDS = [FieldRule(name="iban", type="IBAN"), FieldRule(name="notes")]


@given(floors(), configs())
@example(S1_FLOOR, Config(policies={"IBAN": "allow"}, fields=S1_FIELDS))
@example(S1_FLOOR, Config(projects=[ProjectEntry(path="C:/work/shop", policies={"IBAN": "allow"}, fields=S1_FIELDS)]))
def test_raising_to_the_floor_never_lowers_protection(floor, config):
    """Strictly, but for S1's field rules, weighed exactly: a field rule said changed only in a place where the raise
    took one of yours out of use, and without those rules the settings in use lower nothing at all."""
    raising = _raised(floor, config)
    reasons = [reason for reason in loosened(config, raising.c) if reason != GUIDANCE]
    if raising.excepted_fields:
        assert floor.exceptions == "company"
        without = _without(config, raising.excepted_fields)
        assert [reason for reason in loosened(without, raising.c) if reason != GUIDANCE] == []
        reasons = [reason for reason in reasons if not _taken_out(reason, raising.excepted_fields)]
    assert reasons == []


@given(floors(), configs())
def test_settings_raised_to_the_floor_are_not_below_it(floor, config):
    assert below_floor(floor, raise_to_floor(floor, config)) == []


@given(floors(), configs())
def test_raising_twice_changes_nothing_more(floor, config):
    once = raise_to_floor(floor, config)
    assert raise_to_floor(floor, once).model_dump() == once.model_dump()


def _forms(term):
    """How a text may write a term the engine matches (its case alone, or any as RE2 folds it, composed or decomposed;
    inside a word when it may be), and where: a sentence, a whole text, a value of a JSON tool output."""
    cases = [term.value]
    if not term.case_sensitive:
        cases += [form for form in (term.value.upper(), term.value.lower()) if form.lower() == term.value.lower()]
    cases = list(dict.fromkeys([*cases, *(unicodedata.normalize("NFD", form) for form in cases)]))
    around = ["{}", "see {} today", "see {} Phase 2", "{}: done"]
    if not term.whole_word:
        around.append("x{}x")
    texts = [(form, text.format(form), SlotKind.USER_TEXT) for form in cases for text in around]
    return texts + [(form, json.dumps({"notes": form}, ensure_ascii=False), SlotKind.TOOL_RESULT) for form in cases]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(floors(required=("terms",)), configs())
def test_every_company_term_is_masked_by_the_engine_built_on_the_raised_settings(floor, config):
    """H1: whatever a person sets (a term of the company's value as another type, an allow value, a pattern anchored
    or not, a recognizer that leaves what it finds in clear, `allow` for a type, a field kept in clear), in their
    settings or in a project's, the engine built on the raised settings leaves no company term in clear. (Without the
    local name model: it is not installed where the tests run.)"""
    raised = raise_to_floor(floor, config)
    allowed = {etype for etype, policy in floor.rules.policies.items() if policy == "allow"}
    # A company term of a type the company allows is its own exception: that term alone, never another of its value or
    # one inside it.
    for entry in [None, *raised.projects]:
        here = raised if entry is None else effective_config(raised, None, entry)[0]
        masker, _ = build_masker(here.model_copy(update={"ner": "off"}), Vault(ROOT_KEY))
        for term in (t for t in floor.rules.terms if t.type.upper() not in allowed):
            for form, text, kind in _forms(term):
                assert form not in masker.mask(text, kind).text, (form, text)
