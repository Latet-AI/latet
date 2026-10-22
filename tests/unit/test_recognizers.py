"""Your own recognizers: each entry checked on its own, broken ones kept in the file and turned off."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from latet.config import Config, load_config, save_config
from latet.engine.build import build
from latet.engine.detectors import checksums
from latet.engine.detectors.recognizers import MAX_PATTERN, compile_all, compile_one
from latet.engine.types import EntityClass, Policy, SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

BADGE = {
    "name": "employee-id",
    "description": "Badge numbers: two letters, a hyphen, six digits",
    "pattern": r"\b[A-Z]{2}-\d{6}\b",
    "context": {"words": ["badge", "employee"]},
    "examples": {"match": ["badge AB-123456"], "no_match": ["AB-123456 alone", "build AB-12345"]},
}
CARD_REF = {
    "name": "card-ref",
    "type": "CARD_REF",
    "pattern": r"\bCR(?P<value>\d{8})\b",
    "checksum": "luhn",
    "class": "secret",
    "examples": {"match": ["ref CR12345674"], "no_match": ["ref CR12345678"]},
}


def _masker(*recognizers, **config):
    return build(Config(recognizers=list(recognizers), **config), Vault(ROOT_KEY)).masker


def test_a_recognizer_masks_with_its_own_type_rule_and_fakes():
    masker = _masker(BADGE, CARD_REF)
    masked = masker.mask("employee badge AB-123456, card ref CR12345674", SlotKind.USER_TEXT)
    by_type = {r.etype: r for r in masked.replacements}
    assert set(by_type) == {"EMPLOYEE_ID", "CARD_REF"}
    badge, card = by_type["EMPLOYEE_ID"], by_type["CARD_REF"]
    assert badge.rule == "recognizer:employee-id" and badge.eclass == EntityClass.CUSTOM
    assert card.rule == "recognizer:card-ref" and card.eclass == EntityClass.SECRET
    assert card.original == "12345674" and "CR" + card.replacement in masked.text  # only the value group
    assert len(badge.replacement) == 9 and badge.replacement[2] == "-" and badge.replacement != "AB-123456"
    assert checksums.luhn(card.replacement)
    again = _masker(BADGE, CARD_REF).mask("employee badge AB-123456", SlotKind.USER_TEXT)
    assert again.replacements[0].replacement == badge.replacement  # the same fake on the same machine


def test_keywords_checksums_and_lengths():
    _, [badge] = compile_all([BADGE])
    [recognizer], _ = compile_all([BADGE])
    assert badge.state == "active" and not badge.warnings
    assert [s for s in recognizer.find("AB-123456")] == []  # no keyword
    table = "name,badge\nAnna,QX-904417\n"
    assert [table[s.start : s.end] for s in recognizer.find(table)] == ["QX-904417"]  # column header
    weighted = {  # the digits only: the 11 test of a Dutch BSN
        "name": "dutch-like",
        "pattern": r"\bNL\d{9}\b",
        "checksum": {"algorithm": "weighted", "weights": [9, 8, 7, 6, 5, 4, 3, 2, -1], "modulus": 11},
    }
    [recognizer], [status] = compile_all([weighted])
    assert status.state == "active"
    assert [s.etype for s in recognizer.find("id NL111222333")] == ["DUTCH_LIKE"]
    assert not list(recognizer.find("id NL111222334"))
    short = {"name": "short", "pattern": r"#(?P<value>\w+)", "min_length": 5, "context": {"words": ["tag"]}}
    [recognizer], _ = compile_all([short])
    assert [s.end - s.start for s in recognizer.find("tag #abc and #abcdef")] == [6]


def test_a_match_of_signs_alone_is_not_a_value():
    # A fake keeps the signs of a value, so `---` was its own fake: a rule that matched one (`-{3,}` passes the check
    # against ordinary code) stopped every request with a Markdown rule line in it.
    lines = {"name": "rule-line", "pattern": "-{3,}"}
    parts = {"name": "part-code", "pattern": r"(?i)\bpart[ :#]+(?P<value>[A-Z0-9-]{4,})"}
    text = "---\ntitle: parts\n---\npart: ---- (none yet), part: AB-1234"
    masked = _masker(lines, parts).mask(text, SlotKind.TOOL_RESULT)
    assert [r.original for r in masked.replacements] == ["AB-1234"]
    assert masked.text.startswith("---\ntitle: parts\n---\npart: ---- (none yet), part: ")
    [recognizer], _ = compile_all([lines])
    found, reason = recognizer.explain("a\n---\nb")  # what `latet rules test` and Try it say
    assert found == [] and reason == "matched '---', but it has no letter or digit: signs alone are never masked"


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"name": "lookbehind", "pattern": "(?<=id )\\d+"}, "not a valid RE2 expression"),
        ({"name": "empty", "pattern": "x*"}, "matches an empty text"),
        ({"name": "long", "pattern": "a" * (MAX_PATTERN + 1)}, "pattern"),
        ({"name": "broad", "pattern": r"\bdef\b"}, "too broad"),
        ({"name": "Bad Name", "pattern": "x"}, "lower case letters"),
        ({"name": "reserved", "type": "EMAIL", "pattern": "x+y"}, "a type Latet already has"),
        ({"name": "sum", "pattern": "x+y", "checksum": "crc32"}, "unknown checksum"),
        ({"name": "typo", "pattern": "x+y", "pattren": "x"}, "pattren: not a key"),
        ({"pattern": "x+y"}, "name: required"),
        ("just text", "a mapping"),
    ],
)
def test_broken_entries_are_turned_off_with_a_reason(entry, error):
    from latet.engine.corpus import benign_sample

    recognizer, status = compile_one(entry, {"EMAIL"}, benign_sample())
    assert recognizer is None and status.state == "error"
    assert any(error in e for e in status.errors), status.errors


def test_a_broken_entry_stays_in_the_file_and_the_others_keep_working(tmp_path):
    path = tmp_path / "config.yaml"
    broken = {"name": "broken", "pattern": "(unclosed", "description": "kept as written"}
    path.write_text(yaml.safe_dump({"recognizers": [broken, BADGE]}), "utf-8")
    config = load_config(path)
    save_config(config, path)
    text = path.read_text("utf-8")
    assert text.startswith("# yaml-language-server: $schema=")
    assert yaml.safe_load(text)["recognizers"][0] == broken
    built = build(config, Vault(ROOT_KEY))
    assert [s.state for s in built.recognizers] == ["error", "active"]
    assert built.masker.mask("badge AB-123456", SlotKind.USER_TEXT).replacements


def test_names_and_types_are_unique_and_examples_are_checked():
    duplicate = {**BADGE, "description": "again"}
    same_type = {**CARD_REF, "name": "card-ref-2"}
    failing = {**CARD_REF, "name": "failing", "type": "FAILING", "examples": {"match": ["ref CR12345678"]}}
    recognizers, statuses = compile_all([BADGE, duplicate, CARD_REF, same_type, failing])
    assert [s.state for s in statuses] == ["active", "error", "active", "error", "active"]
    assert "already called employee-id" in statuses[1].errors[0]
    assert "already uses CARD_REF" in statuses[3].errors[0]
    assert "luhn check fails" in statuses[4].warnings[0]
    assert len(recognizers) == 3
    off = {**BADGE, "name": "off", "type": "OFF_ONE", "enabled": False}
    recognizers, [status] = compile_all([off])
    assert recognizers == [] and status.state == "off"


def test_secret_class_blocks_and_policies_choose_what_happens():
    secret = {**CARD_REF, "policy": "redact"}
    built = build(Config(recognizers=[secret]), Vault(ROOT_KEY))
    result = built.verifier.verify([("pay with ref CR12345674", SlotKind.USER_TEXT, False)], "s")
    assert [leak.etype for leak in result.blocking] == ["CARD_REF"]
    masked = built.masker.mask("ref CR12345674", SlotKind.USER_TEXT)
    assert masked.text == "ref CR[REDACTED:CARD_REF]"  # the recognizer's own policy
    config = Config(recognizers=[secret], policies={"CARD_REF": Policy.MASK})  # `policies` wins
    masked = build(config, Vault(ROOT_KEY)).masker.mask("ref CR12345674", SlotKind.USER_TEXT)
    assert "REDACTED" not in masked.text and "12345674" not in masked.text


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    from latet.cli import admin

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)  # no daemon
    return tmp_path


def _cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def test_rules_test_explains_every_example(isolated):
    rules = isolated / "rules.yaml"
    rules.write_text(yaml.safe_dump({"recognizers": [BADGE, CARD_REF]}), "utf-8")
    ok = _cli("rules", "test", str(rules))
    assert ok.exit_code == 0, ok.output
    assert "no keyword (badge, employee) within 48 characters" in ok.output
    assert "the luhn check fails" in ok.output and "standard library: 0 matches" in ok.output
    failing = {**BADGE, "examples": {"match": ["AB-123456"]}}
    result = _cli("rules", "test", "-", "--json", input=yaml.safe_dump(failing))
    assert result.exit_code == 1
    report = json.loads(result.output)
    [example] = report["recognizers"][0]["examples"]
    assert not example["ok"] and "no keyword" in example["reason"]
    broken = _cli("rules", "test", "-", input=yaml.safe_dump({"name": "x", "pattern": "(a"}))
    assert broken.exit_code == 2 and "not a valid RE2 expression" in broken.output
    lines = {"name": "rule-line", "pattern": "-{3,}", "examples": {"match": ["a --- b"]}}
    signs = _cli("rules", "test", "-", input=yaml.safe_dump(lines))
    assert signs.exit_code == 1 and "matched '---', but it has no letter or digit" in signs.output


def test_rules_test_on_your_files_shows_shapes_never_values(isolated):
    src = isolated / "src"
    src.mkdir()
    (src / "staff.csv").write_text("name,badge\nAnna,QX-904417\nLuca,ZT-118823\n", "utf-8")
    result = _cli("rules", "test", "-", "--corpus", str(src), input=yaml.safe_dump(BADGE))
    assert result.exit_code == 0, result.output
    assert "2 matches in 1 of 1 files: AA-999999 x2" in result.output
    assert "QX-904417" not in result.output and "ZT-118823" not in result.output


def test_rules_add_list_and_remove(isolated, monkeypatch):
    from latet.cli import rules

    monkeypatch.setattr(rules, "_known_in_vault", lambda values: [False] * len(values))
    added = _cli("rules", "add", "-", input=yaml.safe_dump([BADGE, CARD_REF]))
    assert added.exit_code == 0, added.output
    assert [e["name"] for e in load_config().recognizers] == ["employee-id", "card-ref"]
    listed = _cli("rules", "list")
    assert "employee-id" in listed.output and "active" in listed.output
    assert _cli("rules", "remove", "card-ref").exit_code == 0
    assert [e["name"] for e in load_config().recognizers] == ["employee-id"]
    assert _cli("rules", "remove", "card-ref").exit_code == 1
    monkeypatch.setattr(rules, "_known_in_vault", lambda values: [True] * len(values))  # a real value
    refused = _cli("rules", "add", "-", input=yaml.safe_dump({**CARD_REF, "name": "real-one", "type": "REAL_ONE"}))
    assert refused.exit_code == 2 and "real value" in refused.output
    assert [e["name"] for e in load_config().recognizers] == ["employee-id"]
    broken = _cli("rules", "add", "-", input=yaml.safe_dump({"name": "x", "pattern": "(a"}))
    assert broken.exit_code == 2 and "Nothing was added" in broken.output


def test_config_schema_and_validate(isolated):
    schema = json.loads(_cli("config", "schema").output)
    assert schema["$id"].endswith("/config.schema.json")
    assert schema["$defs"]["Recognizer"]["properties"]["checksum"]["anyOf"][0]["enum"][0] == "luhn"
    path = isolated / "check.yaml"
    path.write_text(yaml.safe_dump({"profile": "max", "recognizers": [BADGE]}), "utf-8")
    assert _cli("config", "validate", str(path)).exit_code == 0
    path.write_text(yaml.safe_dump({"profile": "loud"}), "utf-8")
    bad = _cli("config", "validate", str(path))
    assert bad.exit_code == 2 and "profile" in bad.output


def test_config_validate_names_a_term_of_signs_alone(isolated):
    path = isolated / "check.yaml"
    data = {"terms": [{"value": "Acme"}, {"value": "--"}], "projects": [{"path": "/p", "terms": [{"value": "+"}]}]}
    path.write_text(yaml.safe_dump(data), "utf-8")
    bad = _cli("config", "validate", str(path))
    assert bad.exit_code == 2 and "terms.1.value: a term needs a letter or a digit" in bad.output
    assert "projects.0.terms.0.value: a term needs a letter or a digit" in bad.output


def test_a_term_of_signs_alone_written_by_hand_stops_no_other_change(isolated):
    # Two of them in the file: taking one out, or changing something else, must not fail on the other.
    (isolated / "config.yaml").write_text("terms:\n  - value: '--'\n  - value: '###'\n", "utf-8")
    assert _cli("config", "remove", "terms", "###").exit_code == 0
    assert _cli("config", "set", "profile", "max").exit_code == 0
    assert [t.value for t in load_config().terms] == ["--"] and load_config().profile == "max"
    refused = _cli("config", "add", "terms", "{value: '+'}")
    assert refused.exit_code == 2 and "terms.1.value: a term needs a letter or a digit" in refused.output


def test_a_bare_off_on_the_command_line_is_the_word_and_a_switch_still_reads_it(isolated):
    """YAML reads a bare off as false: a setting that takes the word (`ocr: off`) would refuse it."""
    assert _cli("config", "set", "ocr", "off").exit_code == 0 and load_config().ocr == "off"
    assert _cli("config", "set", "include_private_ips", "yes").exit_code == 0
    assert load_config().include_private_ips is True  # a switch reads yes, on, no and off as pydantic does


def test_a_bare_no_added_to_a_list_on_the_command_line_is_the_word(isolated):
    """YAML reads a bare NO as false: Norway's region code, added to a list, is the word, and taken out again by it."""
    added = _cli("config", "add", "phone_regions", "NO")
    assert added.exit_code == 0, added.output
    assert "NO" in load_config().phone_regions
    assert _cli("config", "remove", "phone_regions", "NO").exit_code == 0
    assert "NO" not in load_config().phone_regions


def test_the_published_schema_is_current():
    from pathlib import Path

    from latet.schema import config_schema

    published = Path(__file__).resolve().parents[2] / "docs" / "public" / "config.schema.json"
    assert json.loads(published.read_text("utf-8")) == config_schema()


@pytest.mark.parametrize("guide", ["llms-rules.md", "docs/src/content/docs/concepts/custom-recognizers.md"])
def test_every_recognizer_in_the_guides_passes_its_test(guide):
    import re
    import textwrap
    from pathlib import Path

    from latet.rules_report import OK
    from latet.rules_report import outcome as _outcome
    from latet.rules_report import report as _report

    text = (Path(__file__).resolve().parents[2] / guide).read_text("utf-8")
    blocks = [textwrap.dedent(b) for b in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL) if "recognizers:" in b]
    assert len(blocks) >= 2
    for block in blocks:
        report = _report(yaml.safe_load(block)["recognizers"])
        assert _outcome(report) == OK, report
