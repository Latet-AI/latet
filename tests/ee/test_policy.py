"""Locked rules: a policy file read into a floor, the settings raised to it, never lowered, and the reasons a change
goes below it, in protection.loosened's words: a setting each, never a value."""

import hashlib
import itertools
import json
import re
import time

import pytest
import re2
import yaml

from latet.config import Config, FieldRule, ProjectEntry, TermConfig
from latet.ee import policy as policy_module
from latet.ee.policy import (
    PolicyError,
    below_floor,
    floor_view,
    parse_policy,
    policy_json,
    raise_to_floor,
    read_policy,
)
from latet.engine.build import build_masker
from latet.engine.detectors.recognizers import compile_all
from latet.engine.types import SlotKind
from latet.engine.vault import MaskingError, Vault
from latet.floor import raised_to
from latet.projects import effective_config, repo_settings
from latet.protection import loosened
from latet.transfer import KEYS, MAX_BYTES

from ..conftest import ROOT_KEY


def _floor(text, tmp_path, **choice):
    path = tmp_path / "policy.yaml"
    path.write_text(text, "utf-8")
    _, floor = read_policy(path, **choice)
    return floor


def _masked(config, text, entry=None):
    """``text`` as the engine built on ``config`` (or on its rules in ``entry``'s project) sends it."""
    if entry is not None:
        config, _ = effective_config(config, None, entry)
    masker, _ = build_masker(config, Vault(ROOT_KEY))
    return masker.mask(text).text


FALCON = "terms: [{value: Project Falcon, type: PROJECT}]\n"
FORMS = ("Project Falcon", "PROJECT FALCON", "project falcon")


def _in_clear(config, *, entry=None, around="see {} today", plus=False):
    """The forms of the company's term the engine built on ``config`` leaves in clear; ``plus``: each written as a
    form's query writes it, its space a `+`."""
    written = [form.replace(" ", "+") if plus else form for form in FORMS]
    return [form for form in written if form.casefold() in _masked(config, around.format(form), entry).casefold()]


def test_a_policy_file_is_read_into_the_floor_it_sets(tmp_path):
    floor = _floor(
        "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\nprofle: max\n", tmp_path
    )
    assert floor.keys == {"profile", "terms"}
    assert floor.rules.profile == "max" and [t.value for t in floor.rules.terms] == ["Project Falcon"]
    assert floor.source == "managed" and floor.origin == str(tmp_path / "policy.yaml") and len(floor.etag) == 64
    assert floor.exceptions == "personal"
    assert any("profle" in notice for notice in floor.notices)  # a key with a mistake: left out, and said
    assert floor.unread == {"profle"}  # left out whole


def test_a_policy_saved_by_notepad_is_read_as_any_other(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_bytes(b"\xef\xbb\xbflatet_rules: 1\r\nprofile: max\r\n")  # a byte order mark, Windows line ends
    text, floor = read_policy(path)
    assert floor.keys == {"profile"} and floor.notices == () and text == "latet_rules: 1\r\nprofile: max\r\n"
    path.write_bytes("\N{ZERO WIDTH NO-BREAK SPACE}profile: max\n".encode("utf-16-le"))  # PowerShell 5.1's
    assert read_policy(path)[1].keys == {"profile"}


@pytest.mark.parametrize("text", ["profile: [max", "latet_rules: 99\n", "- a list\n"])
def test_a_policy_that_cannot_be_read_says_why(tmp_path, text):
    with pytest.raises(PolicyError, match=r"policy\.yaml"):
        _floor(text, tmp_path)
    with pytest.raises(PolicyError, match="cannot read it"):
        read_policy(tmp_path / "missing.yaml")


def test_a_policy_that_cannot_be_read_never_quotes_it_and_is_never_read_whole(tmp_path):
    """Whatever stops a policy (nesting PyYAML cannot follow, half of a character, bytes that are not text, a file too
    large) is a PolicyError in Latet's own words: none quotes what the file holds, and a large file is not read."""
    deep = "terms: " + "[" * 5000 + "Falcon Holdings" + "]" * 5000 + "\n"
    tagged = "terms: [!Falcon-Holdings {value: x}]\n"  # PyYAML's own words quote a tag it has no constructor for
    halved = "terms: [{value: Falcon Holdings\N{LOW LINE}" + chr(0xD800) + "}]\n"
    for text, said in ((deep, "nested too deeply"), (halved, "half of a character"), (tagged, "not valid YAML")):
        with pytest.raises(PolicyError, match=said) as refused:
            parse_policy(text)
        assert "Falcon" not in str(refused.value)
    path = tmp_path / "policy.yaml"
    path.write_bytes(b"terms: [{value: Falcon Holdings \xff\xfe}]\n")
    with pytest.raises(PolicyError, match="not text") as refused:
        read_policy(path)
    assert "Falcon" not in str(refused.value)
    path.write_bytes(b"# Falcon Holdings\n" * (MAX_BYTES // 16))
    with pytest.raises(PolicyError, match="larger than 2 MB") as refused:
        read_policy(path)
    assert "Falcon" not in str(refused.value)


def test_a_policy_file_is_read_up_to_one_byte_more_than_a_rules_file_holds(tmp_path, monkeypatch):
    asked = []
    real = policy_module.read_regular_snapshot

    def bounded(path, *, limit, follow):
        asked.append((limit, follow))
        return real(path, limit=limit, follow=follow)

    monkeypatch.setattr(policy_module, "read_regular_snapshot", bounded)
    path = tmp_path / "policy.yaml"
    path.write_text("profile: max\n", "utf-8")
    assert read_policy(path)[1].keys == {"profile"} and asked == [(MAX_BYTES + 1, True)]


def test_a_date_out_of_range_in_a_policy_is_a_yaml_mistake_said_by_its_place():
    with pytest.raises(PolicyError, match=r"out of range \(line 2, column \d+\)") as refused:
        parse_policy("terms:\n  - value: 2024-13-45\n")
    assert "2024-13-45" not in str(refused.value)


def test_whose_exceptions_count_is_said_in_the_policy_and_a_mistake_reads_as_the_stricter():
    assert parse_policy("profile: max\n").exceptions == "personal"
    assert parse_policy("profile: max\n", exceptions="company").exceptions == "company"  # the file says none
    assert parse_policy("exceptions: company\nprofile: max\n").exceptions == "company"
    mistaken = parse_policy("exceptions: compagny\nprofile: max\n")
    assert mistaken.exceptions == "company" and mistaken.keys == {"profile"}
    assert mistaken.notices == ("exceptions: personal or company; read as company, the stricter",)


@pytest.mark.parametrize("written", ["exceptions:\n", "exceptions: null\n", "exceptions: ~\n"])
def test_exceptions_written_with_no_value_read_as_company_and_are_said(written):
    """Final review L1: `exceptions:` with no value is a value forgotten, never the default: it reads as a value Latet
    cannot read does, the stricter, and says so (`latet policy check` exits 1)."""
    floor = parse_policy(f"{written}profile: max\n")
    assert floor.exceptions == "company" and floor.carves_terms and floor.decides_exceptions
    assert floor.notices == ("exceptions: personal or company; read as company, the stricter",)
    assert floor.keys == {"profile"} and floor.unread == frozenset()
    assert parse_policy(f"{written}profile: max\n", exceptions="personal").exceptions == "company"


@pytest.mark.parametrize("key", KEYS)
def test_a_key_written_with_no_value_locks_nothing_and_is_said(key):
    """Final review L1: `ocr:` with no value is a value forgotten: it locks nothing, and it is left out whole as a key
    with a mistake in it is, so that `latet policy check` says it (exit 1) and the administration's PUT asks for
    ``accept_notices``."""
    other = "ocr" if key == "ner" else "ner"
    floor = parse_policy(f"{key}:\n{other}: auto\n")
    assert floor.keys == {other} and floor.unread == {key}
    assert floor.notices == (f"{key}: written with no value, so it is not locked",)


def test_guidance_written_with_no_value_takes_none_from_a_config_files_reviewer():
    """A config.yaml's `llm_review.guidance` is read where the file has no `guidance` of its own: one written with no
    value is there, locks nothing, and is said."""
    floor = parse_policy("guidance:\nllm_review: {guidance: Ward names are confidential.}\nner: auto\n")
    assert floor.keys == {"ner"} and floor.unread == {"guidance"} and floor.rules.llm_review.guidance is None
    assert parse_policy("llm_review: {guidance: Ward names are confidential.}\n").keys == {"guidance"}


def test_the_etag_is_the_whole_digest_of_the_text():
    text = "profile: max\n"
    assert parse_policy(text).etag == hashlib.sha256(text.encode()).hexdigest()


def test_what_the_policy_sets_is_raised_and_what_it_leaves_out_stays_yours(tmp_path):
    floor = _floor("profile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n", tmp_path)
    mine = Config(profile="balanced", ner="off", terms=[TermConfig(value="Northwind Traders", type="CLIENT")])
    raised = raise_to_floor(floor, mine)
    assert raised.profile == "max"
    assert sorted(t.value for t in raised.terms) == ["Northwind Traders", "Project Falcon"]
    assert raised.ner == "off"  # not in the policy: yours
    assert mine.profile == "balanced"  # yours are not changed


def test_a_setting_below_the_floor_is_named_never_with_a_value(tmp_path):
    floor = _floor(
        "profile: max\nterms: [{value: Project Falcon, type: PROJECT}]\npolicies: {IBAN: redact}\n", tmp_path
    )
    reasons = below_floor(floor, Config(profile="balanced", policies={"IBAN": "mask"}))
    assert "profile: from max to balanced" in reasons
    assert "terms: 1 value no longer masked" in reasons
    assert "policy for IBAN: from redact to mask" in reasons
    assert not any("Falcon" in reason for reason in reasons)
    assert below_floor(floor, raise_to_floor(floor, Config())) == []


def test_the_profile_locks_what_it_brings_with_it(tmp_path):
    floor = _floor("profile: max\n", tmp_path)
    mine = Config(profile="max", include_private_ips=False, data="normal", images="mask")
    raised = raise_to_floor(floor, mine)
    assert raised.include_private_ips is True and raised.data_mode() == "strict" and raised.images == "block"
    reasons = below_floor(floor, mine)
    assert "private IP addresses: no longer masked" in reasons
    assert "strict data mode: turned off" in reasons
    assert "images: from block to mask" in reasons


def test_exceptions_are_each_persons_unless_the_policy_says_company(tmp_path):
    mine = Config(allow_values=["acme.com", "emily carter"], disabled_secret_rules=["generic-api-key"])
    assert below_floor(_floor("allow_values: [acme.com]\n", tmp_path), mine) == []
    company = _floor("exceptions: company\nallow_values: [acme.com]\n", tmp_path)
    raised = raise_to_floor(company, mine)
    assert raised.allow_values == ["acme.com"] and raised.disabled_secret_rules == []
    assert set(below_floor(company, mine)) == {"values never to mask: 1 added", "secret rules: 1 turned off"}
    assert raise_to_floor(company, Config()).allow_values == []  # the company's exceptions are allowed, never added


def test_with_company_exceptions_what_leaves_values_in_clear_is_an_exception_too(tmp_path):
    """Under `exceptions: company` a recognizer of yours that leaves what it finds in clear, `allow` for a type the
    company does not allow, and a field kept in clear the policy does not keep so, are exceptions: out of the settings
    in use, never said as protection lowered by the raise."""
    company = _floor("exceptions: company\npolicies: {URL: allow}\nfields: [{name: notes, mask: false}]\n", tmp_path)
    mine = Config(
        recognizers=[
            {"name": "public", "type": "PUBLIC", "pattern": "PUB-[0-9]{6}", "policy": "allow"},
            {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}"},
        ],
        policies={"EMAIL": "allow", "URL": "allow", "TICKET": "allow"},
        fields=[FieldRule(name="notes", mask=False), FieldRule(name="memo", mask=False)],
        projects=[
            ProjectEntry(path="C:/work/shop", policies={"PHONE": "allow"}, fields=[{"name": "x", "mask": False}])
        ],
    )
    raised = raise_to_floor(company, mine)
    assert [r["name"] for r in raised.recognizers] == ["ticket"]
    assert raised.policies == {"EMAIL": "mask", "URL": "allow", "TICKET": "mask"}
    assert raised.fields == [FieldRule(name="notes", mask=False)]  # the company's own exception stays
    assert raised.projects[0].policies == {"PHONE": "mask"} and raised.projects[0].fields == []
    assert loosened(mine, raised) == []
    reasons = below_floor(company, mine)
    assert "policy for EMAIL: from mask to allow" in reasons
    assert "recognizers: 1 that leave what they find in clear (policy allow)" in reasons
    assert "field rules: values kept in clear in strict data mode" in reasons


# M-A: a type the company lets through (`policies: {URL: allow}`) that a person gives their own term or field rule, under
# `exceptions: company`. A term (CUSTOM) outranks personal data and a company recognizer's match where they meet, and a
# field rule names the field the company's is merged into: what it covers would leave in clear, the person's exception.
# A term stays in use, cut around what is masked without it (``Config.carves_terms``); a field rule goes out of use (a
# field's first rule outranks strict data mode's, which nothing can cut it around).
COMPANY = (
    "exceptions: company\nprofile: max\npolicies: {URL: allow, PUBLIC: allow}\n"
    "recognizers: [{name: ticket, type: TICKET, pattern: 'TCK-[0-9]{6}'}]\nfields: [{name: iban, type: IBAN}]\n"
)
IBAN = "IT60X0542811101000000123456"
LEDGER = "Northwind ledger 7"
OWN_TERMS = [
    TermConfig(value=IBAN, type="URL"),
    TermConfig(value="see TCK-123456", type="PUBLIC"),
    TermConfig(value=LEDGER, type="PUBLIC"),
    TermConfig(value="Northwind", type="PUBLIC"),  # inside the company field's value
]


def _sent(config, text, *, entry=None, kind=SlotKind.USER_TEXT):
    """``text`` as the engine built on ``config`` (or on its rules in ``entry``'s project) sends it, as ``kind``."""
    if entry is not None:
        config, _ = effective_config(config, None, entry)
    masker, _ = build_masker(config.model_copy(update={"ner": "off"}), Vault(ROOT_KEY))
    return masker.mask(text, kind).text


def _left_in_clear(raised, entry=None):
    """What of personal data, a company recognizer's match and a company field's value the engine leaves in clear."""
    found = []
    sent = _sent(raised, f"pay {IBAN} after you see TCK-123456 today", entry=entry)
    found += [value for value in (IBAN, "TCK-123456") if value in sent]
    field = _sent(raised, json.dumps({"iban": LEDGER}), entry=entry, kind=SlotKind.TOOL_RESULT)
    return found + ([LEDGER] if LEDGER in field else [])


def test_with_company_exceptions_a_term_of_yours_of_a_type_the_company_allows_leaves_nothing_in_clear():
    """It stays in use, and what it leaves in clear is cut around what is masked without it: nothing of yours is taken
    out, so nothing is lowered, and the raise names no exception of yours."""
    floor = parse_policy(COMPANY)
    mine = Config(ner="off", terms=OWN_TERMS, policies={"URL": "allow", "PUBLIC": "allow"})
    personal = raise_to_floor(parse_policy(COMPANY.replace("company", "personal", 1)), mine)
    # Each person's exceptions count, theirs to set, and nothing is carved: since 0.9.10 a value let through leaves in
    # clear itself and nothing more, so the IBAN, the ticket and the field's value are masked inside the terms anyway.
    assert _left_in_clear(personal) == []
    assert not personal.carves_terms
    raised = raise_to_floor(floor, mine)
    assert _left_in_clear(raised) == [] and raised.carves_terms
    assert raised.terms == OWN_TERMS and raised.policies == {"URL": "allow", "PUBLIC": "allow"}
    assert _sent(raised, "see the Northwind roadmap") == "see the Northwind roadmap"  # yours, where nothing masks it
    assert loosened(mine, raised) == [] and below_floor(floor, raised) == []
    assert [reason for reason in below_floor(floor, mine) if "terms" in reason] == []  # the profile and the recognizer


def test_with_company_exceptions_a_term_of_yours_never_names_a_group_that_holds_a_company_match():
    """What `exceptions: company` carves that 0.9.10 does not mask by itself: a term of yours let through, inside an
    address let through that holds the company's ticket. Each person's exceptions counting, yours names the group,
    which gives way to the ticket alone, the address in clear; with the company's alone, your term is carved and the
    address is masked whole, as without your term."""
    policy = COMPANY.replace("{URL: allow, PUBLIC: allow}", "{URL: allow, PUBLIC: allow, EMAIL: allow}")
    mine = Config(
        ner="off",
        pii=["EMAIL"],
        terms=[TermConfig(value="operations-team", type="PUBLIC")],
        policies={"PUBLIC": "allow", "EMAIL": "allow"},
    )
    text = "write to TCK-123456.operations-team@acme-intra.it today"
    personal = _sent(raise_to_floor(parse_policy(policy.replace("company", "personal", 1)), mine), text)
    assert "TCK-123456" not in personal and ".operations-team@acme-intra.it today" in personal
    raised = raise_to_floor(parse_policy(policy), mine)
    company = _sent(raised, text)
    assert raised.carves_terms and "TCK-123456" not in company and "operations-team" not in company


def test_with_company_exceptions_a_term_of_a_project_of_a_type_the_company_allows_leaves_nothing_in_clear():
    floor = parse_policy(COMPANY)
    entry = ProjectEntry(path="C:/work/shop", terms=OWN_TERMS, policies={"PUBLIC": "allow", "URL": "allow"})
    mine = Config(ner="off", projects=[entry])
    raised = raise_to_floor(floor, mine)
    assert _left_in_clear(raised, raised.projects[0]) == [] and raised.projects[0].terms == OWN_TERMS
    assert effective_config(raised, None, raised.projects[0])[0].carves_terms
    assert loosened(mine, raised) == [] and [reason for reason in below_floor(floor, mine) if "terms" in reason] == []


def test_with_company_exceptions_a_term_of_yours_let_through_in_some_places_only_stays_as_you_set_it():
    """A term of yours that masks in a project of yours (its type masked there) and is let through elsewhere: in use
    everywhere, its type's policies as you set them, cut around what is masked where it is let through."""
    floor = parse_policy(COMPANY)
    shop = ProjectEntry(path="C:/work/shop", policies={"PUBLIC": "mask"})
    site = ProjectEntry(path="C:/work/site")
    mine = Config(ner="off", terms=OWN_TERMS, policies={"PUBLIC": "allow", "URL": "allow"}, projects=[shop, site])
    raised = raise_to_floor(floor, mine)
    assert raised.terms == OWN_TERMS
    assert raised.policies == {"PUBLIC": "allow", "URL": "allow"} and raised.projects[0].policies == {"PUBLIC": "mask"}
    for entry in (None, *raised.projects):
        assert _left_in_clear(raised, entry) == []
    assert "Northwind" not in _sent(raised, "see the Northwind roadmap", entry=raised.projects[0])  # masked there
    assert loosened(mine, raised) == []
    assert raise_to_floor(floor, raised).model_dump() == raised.model_dump()


@pytest.mark.parametrize("where", ["top", "project"])
def test_with_company_exceptions_a_term_of_yours_never_frees_a_company_match_through_an_address_it_allows(where):
    """Round 2: your term sits in an address the company lets through, which holds a company recognizer's match: the
    address's group is masked as the match without your term, and stays so with it."""
    floor = parse_policy(
        "exceptions: company\nprofile: balanced\npii: [IBAN, EMAIL]\npolicies: {PUBLIC: allow, EMAIL: allow}\n"
        "recognizers: [{name: ticket, type: TICKET, pattern: 'TCK-[0-9]{6}'}]\n"
    )
    own = {
        "terms": [TermConfig(value="operations-team", type="PUBLIC")],
        "policies": {"PUBLIC": "allow", "EMAIL": "allow"},
    }
    if where == "top":
        mine = Config(ner="off", pii=["IBAN", "EMAIL"], **own)
    else:
        mine = Config(ner="off", pii=["IBAN", "EMAIL"], projects=[ProjectEntry(path="C:/work/shop", **own)])
    raised = raise_to_floor(floor, mine)
    entry = raised.projects[0] if where == "project" else None
    text = "write to TCK-123456.operations-team@acme-intra.it today"
    sent = _sent(raised, text, entry=entry)
    assert "TCK-123456" not in sent and "operations-team" not in sent
    assert loosened(mine, raised) == []


def test_with_company_exceptions_an_email_term_of_yours_still_brings_its_domain_along():
    """L-1: e-mail addresses let through by the company and by you, and not detected. Your term typed EMAIL leaves its
    address in clear, as the company allows, and still masks its domain wherever else it appears (``Masker._repeats``),
    which taking it out of use would leave in clear."""
    floor = parse_policy("exceptions: company\npolicies: {EMAIL: allow}\n")
    email = TermConfig(value="ops@acme-intra.it", type="EMAIL")
    mine = Config(ner="off", pii=["PHONE"], terms=[email], policies={"EMAIL": "allow"})
    text = "write to ops@acme-intra.it; the API is api.acme-intra.it"
    raised = raise_to_floor(floor, mine)
    for config in (mine, raised):
        sent = _sent(config, text)
        assert "ops@acme-intra.it" in sent and "api.acme-intra.it" not in sent
    assert loosened(mine, raised) == [] and below_floor(floor, mine) == []


def test_settings_raised_under_company_exceptions_are_built_only_with_their_terms_carved():
    """The cut is not a setting: a copy of the settings in use dumped and read again on the way has none, and nothing
    is built from it (``latet.floor.raised_to``), as for the terms a company locks."""
    floor = parse_policy("exceptions: company\npolicies: {PUBLIC: allow}\n")
    mine = Config(ner="off", terms=[TermConfig(value="see TCK-123456", type="PUBLIC")])
    assert floor.carves_terms and raised_to(floor, mine).carves_terms
    assert not parse_policy("policies: {PUBLIC: allow}\n").carves_terms

    class Dropping:  # a floor whose raise loses what no dump holds
        locks_terms, carves_terms, decides_exceptions = (
            False,
            True,
            True,
        )  # a company floor's pair (exceptions: company)

        def raise_to(self, config):
            return Config.model_validate(floor.raise_to(config).model_dump())

    with pytest.raises(MaskingError, match="cut"):
        raised_to(Dropping(), mine)


def test_with_company_exceptions_a_field_rule_of_yours_of_a_type_the_company_allows_leaves_nothing_in_clear():
    """A field rule of yours of a type let through is out of use as a term is (a secret one outranks the rest of what
    is found in its field); merged with the company's of its name, it takes the company's type."""
    floor = parse_policy(COMPANY)
    fields = [FieldRule(name="notes", type="PUBLIC", secret=True), FieldRule(name="iban", type="PUBLIC")]
    entry = ProjectEntry(path="C:/work/shop", fields=fields, policies={"PUBLIC": "allow"})
    for mine in (
        Config(ner="off", fields=fields, policies={"PUBLIC": "allow"}),
        Config(ner="off", projects=[entry]),
    ):
        raised = raise_to_floor(floor, mine)
        where = raised.projects[0] if raised.projects else None
        notes = _sent(raised, json.dumps({"notes": f"TCK-123456 {IBAN}"}), entry=where, kind=SlotKind.TOOL_RESULT)
        assert "TCK-123456" not in notes and IBAN not in notes
        iban = _sent(raised, json.dumps({"iban": LEDGER}), entry=where, kind=SlotKind.TOOL_RESULT)
        assert LEDGER not in iban
        # Out of use as exceptions (one merged with the company's of its name takes the company's type): said as
        # changed, as a term of a type let through taken out is (0.9.8), and named once by ``below_floor``.
        prefix = "project shop: " if raised.projects else ""
        assert loosened(mine, raised) == [f"{prefix}field rules: changed or removed"]
        below = below_floor(floor, mine)
        assert below.count(f"{prefix}field rules: 2 that leave what they find in clear (policy allow)") == 1


def test_with_company_exceptions_a_recognizer_of_yours_let_through_in_a_project_is_masked_there():
    floor = parse_policy(COMPANY)
    mine = Config(
        ner="off",
        recognizers=[{"name": "seen", "type": "PUBLIC", "pattern": "see TCK-[0-9]{6}"}],
        projects=[ProjectEntry(path="C:/work/shop", policies={"PUBLIC": "allow"})],
    )
    raised = raise_to_floor(floor, mine)
    assert "TCK-123456" not in _sent(raised, "see TCK-123456 today", entry=raised.projects[0])
    assert raised.projects[0].policies == {"PUBLIC": "mask"}
    assert loosened(mine, raised) == []


def test_a_company_term_stays_masked_whatever_a_person_marks_not_sensitive(tmp_path):
    floor = _floor(FALCON, tmp_path)  # exceptions: personal
    mine = Config(
        ner="off",
        allow_values=["project falcon", "acme.com"],
        allow_patterns=[r"Project \w+"],
        policies={"PROJECT": "allow"},
    )
    raised = raise_to_floor(floor, mine)
    assert raised.allow_values == ["acme.com"] and raised.policies["PROJECT"] == "mask"  # ruling 4, said by below
    assert raised.allow_patterns == [r"Project \w+"]  # yours: it never lets the company's term through
    assert [t.value for t in raised.locked_terms] == ["Project Falcon"]
    reasons = below_floor(floor, mine)
    assert "values never to mask: 1 added" in reasons and "policy for PROJECT: from mask to allow" in reasons
    assert _in_clear(raised) == []
    assert "acme.com" in _masked(raised, "see acme.com today")  # yours, as you set it


def _naive(mine):
    """``mine`` raised to a floor of the company's term as the plan first raised it, what each route below defeats: the
    term added where no entry of yours has its value, the allow values equal to it and the patterns that match it as
    written taken out, and `allow` for its type made `mask`."""

    def cleared(holder):
        return {
            "allow_values": [v for v in holder.allow_values if v.lower() != "project falcon"],
            "allow_patterns": [p for p in holder.allow_patterns if not re2.fullmatch(p, "Project Falcon")],
            "policies": {
                **holder.policies,
                **({"PROJECT": "mask"} if holder.policies.get("PROJECT") == "allow" else {}),
            },
        }

    terms = list(mine.terms)
    if "Project Falcon" not in {t.value for t in terms}:
        terms.append(TermConfig(value="Project Falcon", type="PROJECT"))
    projects = [entry.model_copy(update=cleared(entry)) for entry in mine.projects]
    return mine.model_copy(update={**cleared(mine), "terms": terms, "projects": projects})


# H1: each way a person's own settings could leave a company term in clear, checked on the engine built on them.
ROUTES = {
    "the same value as another type, which is allowed": Config(
        terms=[TermConfig(value="Project Falcon", type="PUBLIC", case_sensitive=True)], policies={"PUBLIC": "allow"}
    ),
    "a pattern in another case": Config(allow_patterns=["PROJECT FALCON", "project falcon"]),
    "a pattern of capitals": Config(allow_patterns=["[A-Z ]{5,}", "[a-z ]{5,}"]),
    "a pattern with a flag of its own": Config(allow_patterns=["(?-i:PROJECT FALCON)", "(?-i:project falcon)"]),
    "a pattern anchored to the whole text": Config(allow_patterns=[r"\APROJECT FALCON\z", r"\Aproject falcon\z"]),
    "a recognizer around it that leaves what it finds in clear": Config(
        recognizers=[{"name": "roadmap", "type": "ROADMAP", "pattern": r"(?i)Project Falcon \w+", "policy": "allow"}]
    ),
    "a recognizer around it whose type is allowed": Config(
        recognizers=[{"name": "roadmap", "type": "ROADMAP", "pattern": r"(?i)Project Falcon \w+"}],
        policies={"ROADMAP": "allow"},
    ),
    "a longer term of an allowed type": Config(
        terms=[TermConfig(value="Project Falcon Phase", type="PUBLIC")], policies={"PUBLIC": "allow"}
    ),
    # Matched as it reads in a form (`Project+Falcon` read as `Project Falcon`, which only the detectors of whole values
    # read there): the plan's raise keeps it, as it matches no form as written.
    "a pattern that matches it as it reads in a form": Config(allow_patterns=[r"(?i)project\+falcon"]),
}
# The text each route is tried in, and whether its forms are written as a form's query writes them.
AROUND = {"a pattern that matches it as it reads in a form": ("https://x.example/?q={} Phase 2 today", True)}


# The routes 0.9.10 closes by itself: a value let through leaves in clear itself and nothing more, so a recognizer's
# find around the company's term (a type that is masked) gives way to it, the term added alone.
CLOSED_SINCE_0_9_10 = frozenset(
    {"a recognizer around it that leaves what it finds in clear", "a recognizer around it whose type is allowed"}
)


@pytest.mark.parametrize("route", list(ROUTES))
def test_no_setting_of_yours_leaves_a_company_term_in_clear(tmp_path, route):
    floor = _floor(FALCON, tmp_path)
    mine = ROUTES[route].model_copy(update={"ner": "off"})
    around, plus = AROUND.get(route, ("see {} Phase 2 today", False))
    alone = _in_clear(_naive(mine), around=around, plus=plus)
    if route in CLOSED_SINCE_0_9_10:
        assert alone == [], "0.9.10 masks the term inside the find all the same"
    else:
        assert alone, "the term added alone is left in clear by the route"
    raised = raise_to_floor(floor, mine)
    assert _in_clear(raised, around=around, plus=plus) == []
    assert _in_clear(raised) == []
    assert _in_clear(raised, around="{}") == []  # the whole text: what an anchored pattern lets through
    assert [r for r in loosened(mine, raised) if "guidance" not in r] == []  # raised, never lowered


@pytest.mark.parametrize("route", list(ROUTES))
def test_no_setting_of_a_project_leaves_a_company_term_in_clear_there(tmp_path, route):
    floor = _floor(FALCON, tmp_path)
    own = ROUTES[route]
    entry = ProjectEntry(
        path="C:/work/shop",
        **{key: getattr(own, key) for key in ("terms", "allow_patterns", "recognizers", "policies")},
    )
    mine = Config(ner="off", projects=[entry])
    around, plus = AROUND.get(route, ("see {} Phase 2 today", False))
    raised = raise_to_floor(floor, mine)
    assert _in_clear(raised, entry=raised.projects[0], around=around, plus=plus) == []
    assert _in_clear(raised, entry=raised.projects[0], around="{}") == []
    assert loosened(mine, raised) == []


def test_a_company_term_is_typed_as_the_company_wrote_it(tmp_path):
    """An entry of yours of the company's value that the company's covers is left out, so the term is masked as the
    company's type, with the company's policy for it (here: redacted, never kept)."""
    floor = _floor(FALCON + "policies: {PROJECT: redact}\n", tmp_path)
    yours = [TermConfig(value="project falcon", type="PUBLIC")]
    mine = Config(ner="off", terms=yours, projects=[ProjectEntry(path="C:/work/shop", terms=yours)])
    raised = raise_to_floor(floor, mine)
    assert [(t.value, t.type) for t in raised.terms] == [("Project Falcon", "PROJECT")]
    assert raised.projects[0].terms == []
    assert _masked(raised, "see PROJECT FALCON today") == "see [REDACTED:PROJECT] today"
    assert _masked(raised, "see PROJECT FALCON today", raised.projects[0]) == "see [REDACTED:PROJECT] today"
    assert loosened(mine, raised) == []


def test_a_term_of_yours_around_a_company_term_keeps_its_type_and_its_policy(tmp_path):
    """Your term of a type the company allows (its own exception) leaves the rest of it in clear, the company's term in
    it masked as the company's; one of a type you redact is redacted whole: nothing of yours is re-typed."""
    floor = _floor(FALCON + "policies: {PUBLIC: allow}\n", tmp_path)
    phase = [TermConfig(value="Project Falcon Phase", type="PUBLIC")]
    allowed = Config(ner="off", terms=phase, policies={"PUBLIC": "allow"})
    assert _in_clear(_naive(allowed), around="see {} Phase 2")  # the company allows PUBLIC: the plan's raise kept it
    raised = raise_to_floor(floor, allowed)
    assert raised.policies == {"PUBLIC": "allow"} and raised.terms[1:] == phase  # yours, as you set them
    assert _in_clear(raised, around="see {} Phase 2") == []
    assert _masked(raised, "see Project Falcon Phase 2").endswith(" Phase 2")
    assert loosened(allowed, raised) == []
    redacted = Config(ner="off", terms=phase, policies={"PUBLIC": "redact"})
    raised = raise_to_floor(floor, redacted)
    assert raised.terms[1:] == phase and raised.policies == {"PUBLIC": "redact"}
    assert _masked(raised, "see Project Falcon Phase 2") == "see [REDACTED:PUBLIC] 2"
    assert loosened(redacted, raised) == []


NORTHWIND = "the order from Northwind Traders is late"
# A person who lets CLIENT through as the company does, and masks Northwind Traders by means of their own.
MASKED_BY_YOU = {
    "a term of yours of another type": {"terms": [TermConfig(value="Northwind Traders", type="PARTNER")]},
    "a recognizer of yours of another type": {
        "recognizers": [{"name": "partner", "type": "PARTNER", "pattern": "Northwind Traders"}]
    },
}


@pytest.mark.parametrize("exceptions", ["personal", "company"])
@pytest.mark.parametrize("written", ["Northwind Traders", "NORTHWIND TRADERS"])
@pytest.mark.parametrize("means", list(MASKED_BY_YOU))
def test_a_company_term_of_a_type_the_company_allows_is_its_exception_and_never_unmasks_yours(
    exceptions, written, means
):
    """M-2: a company term of a type the company allows is the company's exception, as its recognizers and field rules
    of such a type are: never added to your settings, never locked, and never taking out an entry of yours of its value.
    Your own masking of that value stays, under either `exceptions`; a term of the company's of a type it masks is
    locked as ever beside it."""
    floor = parse_policy(
        f"exceptions: {exceptions}\npolicies: {{CLIENT: allow}}\n"
        f"terms: [{{value: {written}, type: CLIENT}}, {{value: Project Falcon, type: PROJECT}}]\n"
    )
    mine = Config(ner="off", policies={"CLIENT": "allow"}, **MASKED_BY_YOU[means])
    assert "Northwind Traders" not in _masked(mine, NORTHWIND)
    raised = raise_to_floor(floor, mine)
    assert "Northwind Traders" not in _masked(raised, NORTHWIND)
    assert [t.value for t in raised.locked_terms] == ["Project Falcon"] and floor.locks_terms
    assert "CLIENT" not in [t.type for t in raised.terms] and raised.terms[1:] == mine.terms
    assert _in_clear(raised) == []
    assert loosened(mine, raised) == [] and below_floor(floor, raised) == []
    assert below_floor(floor, mine) == ["terms: 1 value no longer masked"]  # Project Falcon, and nothing of CLIENT
    assert floor_view(floor)["rules"]["terms"] == 1  # the terms it keeps masked: its exception is not one
    alone = parse_policy(
        f"exceptions: {exceptions}\npolicies: {{CLIENT: allow}}\nterms: [{{value: {written}, type: CLIENT}}]\n"
    )
    assert not alone.locks_terms and raised_to(alone, mine).locked_terms == []  # nothing to lock: built all the same
    assert raise_to_floor(alone, mine).model_dump() == mine.model_dump()


def test_an_entry_of_yours_in_another_case_is_weighed_as_re2_folds_case(tmp_path):
    """RE2 folds case one character for another: a sharp s is not ss there. A company term of capitals does not cover
    yours with a sharp s, nor match its text: yours stays, and each is masked where it is found."""
    floor = _floor("terms: [{value: STRASSE AG, type: CLIENT}]\n", tmp_path)
    sharp = "Stra\N{LATIN SMALL LETTER SHARP S}e AG"
    mine = Config(ner="off", terms=[TermConfig(value=sharp, type="CLIENT")], allow_values=[sharp.lower()])
    raised = raise_to_floor(floor, mine)
    assert [t.value for t in raised.terms] == ["STRASSE AG", sharp] and raised.allow_values == [sharp.lower()]
    assert loosened(mine, raised) == []
    for text in ("STRASSE AG", "strasse ag"):
        assert "strasse" not in _masked(raised, f"see {text} now").lower()


def test_the_companys_recognizer_you_hold_already_keeps_the_companys_policy(tmp_path):
    """A company recognizer in use among yours as it is written still has its type masked as the company has it:
    `allow` of yours for its type is no more, and said, as where the company's is placed."""
    floor = _floor(f"recognizers: [{yaml.safe_dump(TICKET, default_flow_style=True).strip()}]\n", tmp_path)
    for held in ([TICKET], []):
        mine = Config(ner="off", recognizers=held, policies={"TICKET": "allow"})
        raised = raise_to_floor(floor, mine)
        assert raised.policies == {"TICKET": "mask"}
        assert "TCK-381904" not in _masked(raised, "see TCK-381904")
        assert "policy for TICKET: from mask to allow" in below_floor(floor, mine)


def test_the_images_a_project_sends_are_weighed_as_they_are_sent_there(tmp_path):
    """The max profile sends no image unless `images` says mask: a project of max with `images: allow` sends none,
    and keeps it; one of max that sets none keeps sending none when the global images are raised to mask."""
    floor = _floor("images: mask\n", tmp_path)
    mine = Config(
        projects=[
            ProjectEntry(path="C:/work/shop", profile="max", images="allow"),
            ProjectEntry(path="C:/work/billing", profile="max"),
            ProjectEntry(path="C:/work/site", images="allow"),
        ]
    )
    raised = raise_to_floor(floor, mine)
    assert raised.images == "mask" and [e.images for e in raised.projects] == ["allow", "block", "mask"]
    assert loosened(mine, raised) == []


def test_personal_data_types_a_policy_wants_are_added_by_their_countries_where_none_are_listed(tmp_path):
    """Without a list of yours, the types are added by the countries that give them: a list written out would be used
    as it is, also in a project of max, which detects every country's ID numbers."""
    floor = _floor("pii: [DE_TAX_ID, EMAIL]\n", tmp_path)
    mine = Config(projects=[ProjectEntry(path="C:/work/shop", profile="max")])
    raised = raise_to_floor(floor, mine)
    assert raised.pii is None and "DE" in raised.id_regions
    assert loosened(mine, raised) == [] and below_floor(floor, raised) == []
    listed = raise_to_floor(floor, Config(pii=["EMAIL"]))
    assert listed.pii == ["EMAIL", "DE_TAX_ID"]


def test_a_narrowed_entry_of_yours_still_leaves_the_company_term_matched_as_the_company_wrote_it(tmp_path):
    """F10: your entry of the company's value that matches it in fewer forms (its case only, whole words only) does
    not narrow the company's: theirs is added all the same."""
    floor = _floor("terms: [{value: Project Falcon, type: PROJECT, whole_word: false}]\n", tmp_path)
    mine = Config(ner="off", terms=[TermConfig(value="Project Falcon", type="PROJECT", case_sensitive=True)])
    raised = raise_to_floor(floor, mine)
    for text in ("xPROJECT FALCONx", "the project falconry"):
        assert "falcon" not in _masked(raised, text).casefold()
    assert below_floor(floor, mine) == ["terms: 1 matches fewer forms"]


def test_an_entry_of_yours_that_matches_more_forms_than_the_companys_stays(tmp_path):
    floor = _floor(FALCON, tmp_path)  # whole words only
    mine = Config(ner="off", terms=[TermConfig(value="Project Falcon", type="CLIENT", whole_word=False)])
    raised = raise_to_floor(floor, mine)
    assert {(t.type, t.whole_word) for t in raised.terms} == {("PROJECT", True), ("CLIENT", False)}
    assert "falcon" not in _masked(raised, "xProject Falconx").casefold()
    assert loosened(mine, raised) == []


def test_latet_stays_on_in_every_project_and_a_repository_file_cannot_take_one_below(tmp_path):
    floor = _floor("profile: max\nocr: auto\n", tmp_path)
    shop = str(tmp_path / "shop")
    mine = Config(projects=[ProjectEntry(path=shop, enabled=False, profile="secrets", ocr="off")])
    raised = raise_to_floor(floor, mine)
    entry = raised.projects[0]
    assert entry.enabled and entry.profile == "max" and entry.ocr == "auto"
    repo, _ = repo_settings({"profile": "balanced", "images": "allow"})
    here, notices = effective_config(raised, repo, entry)
    assert here.profile == "max" and below_floor(floor, here) == []
    assert any(notice.key == "profile" for notice in notices)  # the repository's weaker profile: not applied, said
    assert "project shop: Latet turned off" in below_floor(floor, mine)


TICKET = {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}"}


def test_your_recognizers_stay_in_use_beside_the_companys(tmp_path):
    """M1: a recognizer of yours of the company's name, or of its type, is not put out of use: the company's joins
    yours under a name and a type of its own, and both find what they find."""
    floor = _floor(f"recognizers: [{yaml.safe_dump(TICKET, default_flow_style=True).strip()}]\n", tmp_path)
    mine = Config(
        ner="off",
        recognizers=[
            {"name": "ticket", "type": "TICKET", "pattern": "T-[0-9]{4}"},
            {"name": "jira", "type": "JIRA", "pattern": "JIRA-[0-9]+"},
        ],
        projects=[ProjectEntry(path="C:/work/shop", recognizers=[{"name": "badge", "pattern": "BD-[0-9]{6}"}])],
    )
    raised = raise_to_floor(floor, mine)
    _, statuses = compile_all(raised.recognizers)
    assert [s.state for s in statuses] == ["active", "active", "active"]
    assert raised.recognizers[1:] == mine.recognizers  # yours, as you wrote them
    company = raised.recognizers[0]
    assert company["pattern"] == TICKET["pattern"] and company["name"] != "ticket" and company["type"] != "TICKET"
    out = _masked(raised, "TCK-381904 T-4711 JIRA-12")
    assert not any(value in out for value in ("TCK-381904", "T-4711", "JIRA-12"))
    assert loosened(mine, raised) == []
    here, _ = effective_config(raised, None, raised.projects[0])
    assert [s.state for s in compile_all(here.recognizers)[1]] == ["active"] * 4
    # Of another type: one that puts out of use none of yours keeps its own name and type.
    other = _floor("recognizers: [{name: badge-2, type: BADGE2, pattern: 'BB-[0-9]{6}'}]\n", tmp_path)
    assert raise_to_floor(other, mine).recognizers[0] == {"name": "badge-2", "type": "BADGE2", "pattern": "BB-[0-9]{6}"}
    # One the company keeps in clear is its own exception: allowed, never added.
    allowing = _floor("recognizers: [{name: badge-3, pattern: 'BC-[0-9]{6}', policy: allow}]\n", tmp_path)
    assert raise_to_floor(allowing, mine).recognizers == mine.recognizers


def test_without_a_floor_two_recognizers_of_one_type_are_as_they_were():
    """The engine is unchanged: of two recognizers of one name or one type, the second is still out of use."""
    _, statuses = compile_all([TICKET, {"name": "jira", "type": "TICKET", "pattern": "JIRA-[0-9]+"}])
    assert [s.state for s in statuses] == ["active", "error"]


def test_a_company_recognizer_keeps_its_policy_under_a_type_of_its_own(tmp_path):
    floor = _floor(
        "recognizers: [{name: ticket, type: TICKET, pattern: 'TCK-[0-9]{6}'}]\npolicies: {TICKET: redact}\n",
        tmp_path,
    )
    mine = Config(ner="off", recognizers=[{"name": "jira", "type": "TICKET", "pattern": "JIRA-[0-9]+"}])
    raised = raise_to_floor(floor, mine)
    assert "[REDACTED:" in _masked(raised, "TCK-381904") and "JIRA-12" not in _masked(raised, "JIRA-12")
    assert raised.policies["TICKET"] == "redact"  # the policy's own, for the type yours keeps


def test_a_company_field_rule_is_merged_into_yours_of_its_name(tmp_path):
    """M1: the company's rule for a field joins yours for it (a secret if either says so, masked, typed as yours):
    nothing of yours goes out of use, and what kept the field in clear no longer does."""
    floor = _floor("fields: [{name: notes}, {name: api_token, secret: true}, {name: iban}]\n", tmp_path)
    mine = Config(
        fields=[
            FieldRule(name="Notes", mask=False),
            FieldRule(name="api_token", type="TOKEN"),
            FieldRule(name="iban", type="IBAN", secret=True),
            FieldRule(name="iban", mask=False),  # never in use: the first rule of a field wins
        ],
        policies={"IBAN": "redact"},
    )
    raised = raise_to_floor(floor, mine)
    assert raised.fields[:3] == [
        FieldRule(name="Notes"),
        FieldRule(name="api_token", type="TOKEN", secret=True),  # the company's secret
        FieldRule(name="iban", type="IBAN", secret=True),  # yours
    ]
    assert raised.policies == {"IBAN": "redact"}
    assert loosened(mine, raised) == []
    assert "field rules: values kept in clear in strict data mode" in below_floor(floor, mine)


def test_a_field_rule_of_yours_that_would_keep_a_company_field_in_clear_is_left_out(tmp_path):
    floor = _floor("fields: [{name: iban, type: IBAN}]\n", tmp_path)
    mine = Config(
        fields=[FieldRule(name="*", mask=False), FieldRule(name="customer.*", type="CUSTOMER"), FieldRule(name="sku")],
        policies={"CUSTOMER": "allow"},
    )
    raised = raise_to_floor(floor, mine)
    assert [f.name for f in raised.fields] == ["customer.*", "sku", "iban"]
    assert raised.policies == {"CUSTOMER": "mask"}  # it may name the company's field first: never in clear
    assert loosened(mine, raised) == []


def test_a_rule_placed_before_a_company_field_masks_it_as_strictly_as_the_company_does(tmp_path):
    """Your copy of a company rule comes first, before the company's other rules: for a field both may name
    (`customer.notes`) it is the one in use, so its type is masked as strictly as the company's field."""
    floor = _floor(
        "fields: [{name: notes}, {name: 'customer.*', type: CUSTOMER}]\npolicies: {FIELD: redact}\n", tmp_path
    )
    mine = Config(fields=[FieldRule(name="customer.*", type="CUSTOMER")])
    raised = raise_to_floor(floor, mine)
    assert [f.name for f in raised.fields] == ["customer.*", "notes"]
    assert raised.policies == {"FIELD": "redact", "CUSTOMER": "redact"}
    assert below_floor(floor, raised) == [] and loosened(mine, raised) == []


def test_two_field_names_meet_exactly_when_a_name_matches_both():
    """``*`` patterns, every pair of three characters at most, against every text that could match both."""
    patterns = [""] + ["".join(p) for n in (1, 2, 3) for p in itertools.product("ab*", repeat=n)]
    texts = ["".join(t) for n in range(7) for t in itertools.product("ab", repeat=n)]

    def matches(pattern, text):
        return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), text, re.DOTALL) is not None

    for a, b in itertools.product(patterns, repeat=2):
        both = any(matches(a, text) and matches(b, text) for text in texts)
        assert policy_module._globs_meet(a, b) == both, (a, b)


def test_a_policy_sets_the_files_and_the_addresses_strict_data_mode_covers(tmp_path):
    floor = _floor("data_files: ['*.csv']\ndata_hosts: ['*.salesforce.com']\n", tmp_path)
    raised = raise_to_floor(floor, Config(data_files=["*.json"]))
    assert raised.data_files == ["*.json", "*.csv"] and raised.data_hosts == ["*.salesforce.com"]
    assert set(below_floor(floor, Config())) == {
        "strict data mode: covers fewer files",
        "strict data mode: covers fewer web addresses",
    }


# What a policy of each key alone sets: each changes the settings raised from Latet's defaults, but those that need none
# (an exception of the company's is allowed, never added; the tools strict data mode covers narrow it).
NEEDS_NONE = {"allow_values", "allow_patterns", "disabled_secret_rules", "data_tools"}
ALONE = {
    "profile": "max",
    "pii": ["DE_TAX_ID"],
    "id_regions": ["DE"],
    "phone_regions": ["DE"],
    "include_private_ips": True,
    "ner": "auto",
    "terms": [{"value": "Project Falcon"}],
    "recognizers": [TICKET],
    "policies": {"EMAIL": "redact"},
    "fields": [{"name": "notes"}],
    "data": "strict",
    "data_files": ["*.csv"],
    "data_hosts": ["*.salesforce.com"],
    "dump_guard": 20,
    "images": "block",
    "ocr": "auto",
    "unknown_members": "refuse",
    "allow_values": ["acme.com"],
    "allow_patterns": ["ok-[0-9]+"],
    "disabled_secret_rules": ["generic-api-key"],
    "data_tools": ["psql"],
    "guidance": "Ward names are confidential.",
}


@pytest.mark.parametrize("key", KEYS)
def test_every_key_a_policy_sets_alone_is_raised_or_needs_none(key):
    floor = parse_policy(yaml.safe_dump({key: ALONE[key]}))
    assert floor.keys == {key}
    start = Config(ner="off", ocr="off", phone_regions=["IT"], id_regions=["IT"])
    raised = raise_to_floor(floor, start)
    if key in NEEDS_NONE:
        assert raised == start
    else:  # the company's guidance joins yours: none of yours is below it
        assert raised != start and (below_floor(floor, start) != []) == (key != "guidance")


def test_a_person_is_shown_what_is_locked_never_the_values(tmp_path):
    """M6: a person's view names what the policy locks, with no term, value, pattern, recognizer or guidance in it,
    and its notices as a count; the rules as written are the administration API's alone."""
    floor = _floor(
        "terms: [{value: Project Falcon, type: PROJECT}, {value: Northwind Traders}]\nallow_values: [acme.com]\n"
        "allow_patterns: ['acme-[0-9]+']\nrecognizers: [{name: ticket, type: TICKET, pattern: 'TCK-[0-9]{6}'}]\n"
        "fields: [{name: notes}, {name: raw_notes, mask: false}]\nguidance: Ward names are confidential.\nprofile: max\nprofle: max\n"
        "data_files: ['northwind/*.csv']\ndata_hosts: [crm.northwind.example]\n",
        tmp_path,
    )
    person = floor_view(floor)
    shown = str(person)
    for value in ("Falcon", "Northwind", "northwind", "acme", "TCK", "Ward"):
        assert value not in shown
    rules = person["rules"]
    assert rules["terms"] == 2 and rules["allow_values"] == 1 and rules["allow_patterns"] == 1
    assert rules["data_files"] == 1 and rules["data_hosts"] == 1
    assert rules["recognizers"] == ["ticket"] and rules["fields"] == ["notes"] and rules["guidance"] is True
    assert rules["fields_clear"] == 1 and "raw_notes" not in shown  # a field kept in clear is counted, not locked
    assert "etag" not in person and rules["profile"] == "max" and person["notices"] == 1
    assert person["brought"] == ["pii", "id_regions", "include_private_ips", "data", "images"]
    assert policy_json(floor)["terms"][0]["value"] == "Project Falcon"  # the rules as written: the administrators'


def test_a_person_is_shown_how_many_tools_strict_data_mode_covers_never_their_names():
    """Final review L2: an MCP server's name in a tool pattern can name a client, as a web address can: counted."""
    floor = parse_policy("data: strict\ndata_tools: ['mcp__northwind_crm__*', 'mcp__acme_billing__*']\n")
    person = floor_view(floor)
    assert person["rules"]["data_tools"] == 2 and person["rules"]["data"] == "strict"
    assert "northwind" not in str(person) and "acme" not in str(person)
    assert policy_json(floor)["data_tools"] == ["mcp__northwind_crm__*", "mcp__acme_billing__*"]  # the administrators'


def test_the_policy_as_json_holds_only_what_it_sets(tmp_path):
    floor = _floor("profile: max\nguidance: Ward names are confidential.\n", tmp_path)
    assert policy_json(floor) == {"latet_rules": 1, "profile": "max", "guidance": "Ward names are confidential."}


def test_the_companys_guidance_comes_before_yours(tmp_path):
    floor = _floor("guidance: Ward names are confidential.\n", tmp_path)
    mine = Config(llm_review={"guidance": "Ticket numbers are fine."})
    assert raise_to_floor(floor, mine).llm_review.guidance == "Ward names are confidential.\n\nTicket numbers are fine."


def test_guidance_of_yours_or_none_is_not_below_the_companys(tmp_path):
    """Whole-branch review, Minor 2: the company's guidance joins yours, which stays whole in it: no person is told,
    for good, that the reviewer's guidance changed (a change of it is weighed as any other: ``fresh_reasons``)."""
    floor = _floor("guidance: Ward names are confidential.\n", tmp_path)
    for mine in (Config(), Config(llm_review={"guidance": "Report nothing."})):
        assert below_floor(floor, mine) == []
        assert below_floor(floor, raise_to_floor(floor, mine)) == []


@pytest.mark.timing
def test_settings_at_their_caps_are_raised_in_under_a_second():
    """M7: 5,000 company terms over 5,000 of yours and 500 patterns of yours: no pattern is weighed against a term (the
    build finds the company's as locked)."""
    terms = [{"value": f"Project {n:05d} Falcon", "type": "PROJECT"} for n in range(5000)]
    floor = parse_policy(yaml.safe_dump({"terms": terms}))
    patterns = [f"build-{n}-[0-9]+" for n in range(499)] + ["Project 0421\\d Falcon"]
    mine = Config(allow_patterns=patterns, terms=[{"value": f"Mine {n}"} for n in range(5000)])
    started = time.perf_counter()
    raised = raise_to_floor(floor, mine)
    took = time.perf_counter() - started
    assert raised.allow_patterns == patterns and len(raised.locked_terms) == 5000 and took < 1.0, took


def _timed_raises(text, mine):
    """How long ``mine`` takes to be raised to the policy ``text``: on a floor read anew (what the policy alone gives
    worked out then; recognizers compiled already, as a build keeps them), and again on the same floor (a space's
    re-application, a request's)."""
    raise_to_floor(parse_policy(text), mine)  # compiles the recognizers: a build of the settings does it too
    floor = parse_policy(text)
    took = []
    for _ in range(2):
        started = time.perf_counter()
        raise_to_floor(floor, mine)
        took.append(time.perf_counter() - started)
    return took


@pytest.mark.timing
def test_500_company_field_rules_over_500_of_yours_are_raised_in_under_a_second():
    """M-B: every field rule of yours was weighed against every company rule, pair by pair (5 s a raise)."""
    fields = [{"name": f"*field_{n}*", "type": f"F_{n}", "secret": n % 2 == 0} for n in range(250)]
    fields += [{"name": f"customer.field_{n}[]", "type": f"C_{n}"} for n in range(250)]
    text = yaml.safe_dump({"fields": fields})
    mine = Config(
        fields=[FieldRule(name=f"x.field_{n}", type=f"G_{n}") for n in range(500)],
        projects=[
            ProjectEntry(path=f"C:/work/p{j}", fields=[FieldRule(name=f"field_{n}") for n in range(10)])
            for j in range(20)
        ],
    )
    assert max(took := _timed_raises(text, mine)) < 1.0, took


@pytest.mark.timing
def test_500_company_recognizers_over_100_of_yours_are_raised_in_under_a_second():
    recognizers = [{"name": f"rec-{n}", "type": f"T_{n}", "pattern": f"R{n}-[0-9]{{6}}"} for n in range(500)]
    mine = Config(
        recognizers=[{"name": f"rec-{n}", "type": f"T_{n}", "pattern": f"Q{n}-[0-9]{{5}}"} for n in range(100)]
    )
    assert max(took := _timed_raises(yaml.safe_dump({"recognizers": recognizers}), mine)) < 1.0, took


def test_what_the_policy_alone_gives_is_worked_out_once_for_a_floor():
    floor = parse_policy("fields: [{name: notes}]\nrecognizers: [{name: ticket, pattern: 'TCK-[0-9]{6}'}]\n")
    assert floor.company is floor.company
    raise_to_floor(floor, Config())
    known = floor.company
    raise_to_floor(floor, Config(fields=[FieldRule(name="notes", type="NOTE")]))
    assert floor.company is known


def _meet_as_written(a, b):
    """``_may_share_a_field`` as it was first written: each name against the other's names of a last part."""
    a, b = a.lower(), b.lower()
    wider = ("{}", "*.{}", "{}[]", "*.{}[]", "*.{}[]*", "{}[]*")
    return a == b or any(policy_module._globs_meet(x, w.format(y)) for x, y in ((a, b), (b, a)) for w in wider)


@pytest.mark.parametrize("many", [None, 1])
def test_the_companys_field_rules_a_name_may_share_a_field_with_are_found_as_pair_by_pair(monkeypatch, many):
    """Every name of up to four parts among a letter, a dot, ``[]`` and a star, against every other: the index finds
    the rules weighing them pair by pair finds, in their order (with ``many`` 1, through what each holds weighed name
    by name)."""
    if many is not None:
        monkeypatch.setattr(policy_module, "_MANY_PARTS", many)
    names = ["".join(p) for n in (1, 2, 3, 4) for p in itertools.product(["a", ".", "[]", "*"], repeat=n)]
    rules = [FieldRule(name=name) for name in names]
    index = policy_module._FieldIndex(rules)
    for name in [*names, "A.a[]", "a.A", "B"]:
        assert index.met(name) == [rule for rule in rules if _meet_as_written(name, rule.name)], name
        assert all(
            policy_module._may_share_a_field(name, rule.name) == _meet_as_written(name, rule.name)
            for rule in rules[:40]
        )


def test_a_policy_of_profile_max_alone_locks_what_max_brings_at_the_engine_and_says_so(tmp_path):
    floor = _floor("latet_rules: 1\nprofile: max\n", tmp_path)
    loosened_config = Config(
        profile="balanced", id_regions=[], include_private_ips=False, data="normal", images="allow"
    )
    raised = floor.raise_to(loosened_config)
    text = "Server 172.16.0.5 and 192.168.1.21 and DE89370400440532013000 and ID 12345678Z"
    assert "192.168.1.21" in _masked(loosened_config, text)  # the person's own settings leave it in clear
    assert _masked(raised, text) == _masked(Config(profile="max"), text) and "192.168.1.21" not in _masked(raised, text)
    assert raised.profile == "max" and raised.data_mode() == "strict" and raised.include_private_ips is True
    assert {"profile: from max to balanced", "private IP addresses: no longer masked"} <= set(
        floor.below(loosened_config)
    )
    # What it brings is what `latet policy check` and the floor call locked, not left to each person.
    assert floor.brought == {"pii", "id_regions", "include_private_ips", "data", "images"}
    assert floor.locked == floor.keys | floor.brought
    assert _floor("latet_rules: 1\nner: auto\n", tmp_path).brought == frozenset()
    assert _floor("latet_rules: 1\nprofile: secrets\n", tmp_path).brought == frozenset()
    assert _floor("latet_rules: 1\nprofile: balanced\n", tmp_path).brought == {"pii"}
