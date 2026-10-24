"""Your rules in a file, for a colleague: what is exported, what is read, and what an import adds."""

import time

import pytest
import yaml
from typer.testing import CliRunner

from latet.config import Config, Profile, ProjectEntry, ProjectSettings, load_config, load_yaml, save_config
from latet.protection import loosened
from latet.transfer import FORMAT, RulesError, export_rules, merge_rules, read_rules

ALICE = Config(
    profile="max",
    terms=[{"value": "Fonderia Brambilla", "type": "CLIENT"}],
    recognizers=[{"name": "badge", "pattern": "BD-[0-9]{6}", "examples": {"match": ["badge BD-381904"]}}],
    policies={"EMAIL": "redact"},
    allow_values=["acme-dev"],
    fields=[{"name": "notes", "mask": False}, {"name": "ssn", "secret": True}],
    llm_review={"guidance": "Ward names are confidential."},
    id_regions=["IT", "DE"],
    identity={"usernames": ["alice"], "hostname": "alice-pc"},
    port=7000,
    projects=[
        ProjectEntry(
            path="C:/work/shop", terms=[{"value": "Banca Ligure", "type": "CLIENT"}], allow_values=["shop-dev"]
        )
    ],
)


def _dump(config):
    return config.model_dump(mode="json")


def test_the_file_holds_the_rules_and_nothing_of_the_machine():
    text, warnings = export_rules(ALICE)
    data = load_yaml(text)
    assert text.startswith("# yaml-language-server: $schema=") and data["latet_rules"] == FORMAT
    assert {"profile", "terms", "recognizers", "policies", "fields", "allow_values", "guidance", "id_regions"} <= set(
        data
    )
    assert not {"identity", "port", "projects", "llm_review", "history"} & set(data)
    assert "alice" not in text and "Banca Ligure" not in text  # a project's settings make a file of their own
    assert any("in clear" in w for w in warnings)
    assert export_rules(Config())[0].endswith(f"latet_rules: {FORMAT}\n")  # the defaults: nothing to say


def test_a_project_s_own_settings_make_a_file_of_their_own():
    data = load_yaml(export_rules(ALICE, "C:/work/shop")[0])
    assert data["terms"] == [{"value": "Banca Ligure", "type": "CLIENT"}] and data["allow_values"] == ["shop-dev"]
    assert "profile" not in data
    assert load_yaml(export_rules(ALICE, "C:/work/other")[0]) == {"latet_rules": FORMAT}


def test_a_recognizer_whose_examples_are_real_values_is_left_out():
    text, warnings = export_rules(ALICE, known=lambda values: [v == "badge BD-381904" for v in values])
    assert "recognizers" not in load_yaml(text) and any("badge" in w for w in warnings)
    text, warnings = export_rules(ALICE, known=lambda values: None)  # the daemon is off
    assert "recognizers" in load_yaml(text) and any("could not be checked" in w for w in warnings)


def test_an_import_adds_protection_and_holds_back_the_rest():
    settings, notices = read_rules(export_rules(ALICE)[0])
    assert notices == []
    bob = Config(terms=[{"value": "Fonderia Brambilla", "type": "client"}], fields=[{"name": "SSN"}])
    merged = merge_rules(bob, settings)
    protective, full, report = merged.protective, merged.full, merged.report
    assert protective.profile == "max" and protective.policies == {"EMAIL": "redact"}
    assert [t.value for t in protective.terms] == ["Fonderia Brambilla"]  # Bob's already
    assert [r["name"] for r in protective.recognizers] == ["badge"]
    assert protective.id_regions == ["IT", "US", "GB", "DE"]  # added, none of Bob's removed
    assert [f.name for f in protective.fields] == ["SSN"]  # Bob's rule for the field is kept
    assert protective.allow_values == [] and protective.llm_review.guidance is None
    assert loosened(bob, protective) == []
    assert full.allow_values == ["acme-dev"] and full.llm_review.guidance == "Ward names are confidential."
    assert [f.name for f in full.fields] == ["SSN", "notes"]  # kept in clear: held back
    assert any("field rule ssn" in s for s in report.skipped) and len(report.held) == 3
    assert loosened(protective, full)
    again = merge_rules(protective, settings)  # imported once: only what was held back is left
    assert again.report.added == [] and again.report.held == report.held
    assert merge_rules(full, settings).report.skipped[0].startswith("nothing new")


def test_a_term_of_signs_alone_is_not_read():
    settings, notices = read_rules("terms: [{value: '--'}, {value: Banca Ligure, type: CLIENT}]\n")
    assert [t.value for t in settings.terms] == ["Banca Ligure"]
    assert len(notices) == 1 and notices[0].startswith("term '--': a term needs a letter or a digit")


def test_a_weaker_value_is_held_back():
    settings, notices = read_rules(
        "profile: secrets\npolicies: {EMAIL: allow}\nner: 'off'\ndump_guard: 0\nimages: allow\n"
    )
    assert notices == []
    mine = Config(profile="max", policies={"EMAIL": "redact"}, dump_guard=10, images="block")
    merged = merge_rules(mine, settings)
    assert merged.report.added == [] and len(merged.report.held) == 5
    assert _dump(merged.protective) == _dump(mine)
    full = merged.full
    assert (full.profile, full.policies["EMAIL"], full.ner, full.dump_guard, full.images) == (
        "secrets",
        "allow",
        "off",
        0,
        "allow",
    )
    stricter, _ = read_rules("profile: max\ndump_guard: 5\nimages: block\ndata: strict\ninclude_private_ips: true\n")
    merged = merge_rules(Config(), stricter)
    assert len(merged.report.added) == 5 and merged.report.held == []
    assert merged.protective.dump_guard == 5 and merged.protective.data == "strict"


def test_strict_data_mode_is_measured_as_it_applies_here():
    normal, _ = read_rules("data: normal\n")
    held = merge_rules(Config(profile="max"), normal)  # max turns it on: normal would turn it off
    assert held.report.held == ["strict data mode: normal"] and held.report.added == []
    assert held.protective.data_mode() == "strict" and held.full.data_mode() == "normal"
    same = merge_rules(Config(), normal)  # balanced: normal already, nothing new
    assert same.report.held == [] and same.report.added == []


def test_images_mask_is_held_back_from_the_max_profile_which_sends_none():
    settings, _ = read_rules("images: mask\n")
    held = merge_rules(Config(profile="max"), settings)
    assert held.report.added == [] and len(held.report.held) == 1
    added = merge_rules(Config(), settings)
    assert len(added.report.added) == 1 and added.protective.images == "mask"


def test_what_is_stricter_at_the_top_and_lowers_a_stricter_folder_of_yours_is_held_back():
    # Your payroll folder is at max: images are blocked there, and every country's ID numbers detected. images: mask
    # sends some there, and a list of types written out at the top is used as it is there too.
    mine = Config(projects=[ProjectEntry(path="C:/work/payroll", profile="max")])
    settings, _ = read_rules("images: mask\npii: [ES_DNI]\nterms:\n  - {value: Fonderia Brambilla, type: CLIENT}\n")
    merged = merge_rules(mine, settings)
    assert [s.split(":")[0] for s in merged.report.added] == ["terms"]
    assert sorted(s.split(":")[0] for s in merged.report.held) == ["images", "personal-data types"]
    assert loosened(mine, merged.protective) == []
    assert merged.protective.images == "allow" and merged.protective.pii is None
    assert [t.value for t in merged.protective.terms] == ["Fonderia Brambilla"]
    assert merged.full.images == "mask" and "ES_DNI" in (merged.full.pii or [])  # applied when asked for, and waits


def test_what_is_stricter_at_the_top_and_lowers_a_stricter_repository_seen_is_held_back():
    """A repository seen with a latet.yaml of its own at the max profile detects every country's ID numbers there: a
    list of personal-data types written out at the top is used as it is there too, and detects fewer. Weighed in the
    repositories seen, as a change of config.yaml is, it is held back; a term is added, and so is a recognizer of the
    type of the repository's own, which applies there with the file's, laid apart (Ruling 098-R3), where before it put
    the file's out of use and was held back."""
    badge = {"name": "badge", "pattern": "BD-[0-9]{6}"}
    repository = ("/work/acme", ProjectSettings(profile=Profile.MAX, recognizers=[badge]))
    text = yaml.safe_dump({
        "latet_rules": FORMAT,
        "pii": ["BE_NATIONAL_NUMBER"],
        "recognizers": [{"name": "door-badge", "type": "BADGE", "pattern": "DB-[0-9]{4}"}],
        "terms": [{"value": "Fonderia Brambilla"}],
    })  # fmt: skip
    settings, _ = read_rules(text)
    alone = merge_rules(Config(), settings)  # with no repository seen: all of it adds
    assert len(alone.report.added) == 3 and alone.report.held == []
    merged = merge_rules(Config(), settings, repositories=[repository])
    assert [s.split(":")[0] for s in merged.report.added] == ["terms", "recognizer door-badge"]
    assert [s.split(":")[0] for s in merged.report.held] == ["personal-data types"]
    assert loosened(Config(), merged.protective, [repository]) == [] and merged.protective.pii is None
    assert "BE_NATIONAL_NUMBER" in (merged.full.pii or [])  # applied when asked for, and waits


def test_a_large_import_over_large_settings_is_weighed_in_a_fraction_of_a_second():
    """Weighing each addition alone over the whole settings, each a deep copy checked again, took 56 s for this file over
    these settings (R3-5): only what can lower a folder is weighed alone, on a copy that shares what it does not
    change, and what is held back stays held."""
    folders = [ProjectEntry(path=f"C:/work/client-{i:03d}", terms=[{"value": f"Client {i:03d}"}]) for i in range(199)]
    mine = Config(
        terms=[{"value": f"Customer Company {i:05d}", "type": "ORG"} for i in range(5_000)],
        projects=[*folders, ProjectEntry(path="C:/work/payroll", profile="max")],
    )
    recognizers = [
        {"name": f"badge-{i:03d}", "type": f"BADGE_{i:03d}", "pattern": f"B{i:03d}-[0-9]{{6}}"} for i in range(500)
    ]
    fields = [{"name": f"column_{i:03d}", "type": "NOTE"} for i in range(500)]
    text = yaml.safe_dump({"latet_rules": FORMAT, "images": "mask", "recognizers": recognizers, "fields": fields})
    assert len(text.encode()) > 40 * 1024
    settings, notices = read_rules(text)
    assert notices == [] and len(settings.recognizers) == 500
    took = []
    for _ in range(3):
        started = time.perf_counter()
        merged = merge_rules(mine, settings)
        took.append(time.perf_counter() - started)
    assert [s for s in merged.report.held] == ["images: mask (yours: allow)"]  # it lowers the payroll folder
    assert len(merged.report.added) == 1_000 and loosened(mine, merged.protective) == []
    assert len(merged.protective.recognizers) == 500 and merged.full.images == "mask"
    assert min(took) < 1.0, f"{min(took):.2f} s"


def test_a_recognizer_that_would_put_one_of_a_folders_own_out_of_use_is_held_back():
    """Added at the top, a recognizer comes before a folder's own there: one of its type puts that one out of use, which
    lowers the folder's protection. Weighed alone, it is held back; the others, which reach no folder, are added."""
    mine = Config(projects=[ProjectEntry(path="C:/work/hr", recognizers=[{"name": "badge", "pattern": "BD-[0-9]{6}"}])])
    text = yaml.safe_dump({
        "latet_rules": FORMAT,
        "recognizers": [
            {"name": "door-badge", "type": "BADGE", "pattern": "DB-[0-9]{4}"},  # the folder's type: badge
            {"name": "ticket", "pattern": "T-[0-9]{5}"},
        ],
        "terms": [{"value": "Fonderia Brambilla"}],
    })  # fmt: skip
    settings, _ = read_rules(text)
    merged = merge_rules(mine, settings)
    assert merged.report.held == ["recognizer door-badge"]
    assert sorted(s.split(":")[0] for s in merged.report.added) == ["recognizer ticket", "terms"]
    assert loosened(mine, merged.protective) == []


def test_a_recognizer_named_or_typed_as_yours_is_kept_as_yours():
    mine = Config(
        recognizers=[
            {"name": "badge", "pattern": "BD-[0-9]{6}"},
            {"name": "ticket", "type": "TICKET", "pattern": "T-[0-9]{5}"},
        ]
    )
    file = yaml.safe_dump(
        {
            "recognizers": [
                {"name": "badge", "pattern": "BD-[0-9]{4}"},  # the same name, another pattern
                {"name": "tickets", "type": "TICKET", "pattern": "TK-[0-9]{5}"},  # the same type
                {"name": "acme-code", "pattern": "ACME-[0-9]{4}", "policy": "allow"},  # leaves it in clear
            ]
        }
    )
    settings, notices = read_rules(file)
    assert notices == []
    merged = merge_rules(mine, settings)
    assert len(merged.report.skipped) == 2 and merged.report.added == []
    assert [r["name"] for r in merged.protective.recognizers] == ["badge", "ticket"]
    assert [r["name"] for r in merged.full.recognizers] == ["badge", "ticket", "acme-code"]


IBAN = "IT60X0542811101000000123456"
TYPED_URL = yaml.safe_dump({
    "latet_rules": FORMAT,
    "terms": [{"value": IBAN, "type": "URL"}, {"value": "Fonderia Brambilla", "type": "CLIENT"}],
    "fields": [{"name": "password", "type": "URL"}, {"name": "ssn"}],
})  # fmt: skip


def test_a_term_or_a_field_rule_of_a_type_you_let_through_is_held_back():
    """You let URLs through: a term or a field rule typed URL would leave what it finds in clear (an IBAN, a password),
    so it is held back, as a recognizer that lets what it finds through is; the others are added."""
    mine = Config(policies={"URL": "allow"})
    settings, _ = read_rules(TYPED_URL)
    merged = merge_rules(mine, settings)
    assert merged.report.added == ["terms: Fonderia Brambilla", "field rule ssn"]
    assert merged.report.held == [
        f"terms that leave what they find in clear (policy allow): {IBAN}",
        "field rule password: leaves what it finds in clear (policy allow)",
    ]
    assert loosened(mine, merged.protective) == []
    assert [t.value for t in merged.full.terms] == ["Fonderia Brambilla", IBAN]  # applied when asked for, and waits
    assert [f.name for f in merged.full.fields] == ["ssn", "password"]
    # Into a project whose own policy lets URLs through, the same; at the top, they are held back for that folder.
    shop = Config(projects=[ProjectEntry(path="C:/work/shop", policies={"URL": "allow"})])
    into = merge_rules(shop, settings, "C:/work/shop")
    assert into.report.added == ["terms: Fonderia Brambilla", "field rule ssn"] and len(into.report.held) == 2
    top = merge_rules(shop, settings)
    assert top.report.added == ["terms: Fonderia Brambilla", "field rule ssn"]
    assert top.report.held == [f"terms: {IBAN}", "field rule password"]
    assert loosened(shop, top.protective) == [] and loosened(shop, top.full)
    assert merge_rules(Config(), settings).report.held == []  # URLs masked: all of it adds


def test_what_cannot_be_read():
    with pytest.raises(RulesError, match="newer"):
        read_rules("latet_rules: 99\n")
    with pytest.raises(RulesError, match="aliases"):
        read_rules("a: &x [1]\nterms: *x\n")
    for value in ("2024-13-45", "7" * 5000):  # a date no calendar has, a number Python will not read
        with pytest.raises(RulesError, match="not valid YAML: a date, a time or a number out of range") as refused:
            read_rules(f"terms:\n  - value: {value}\n")
        assert value not in str(refused.value)
    with pytest.raises(RulesError, match="mapping"):
        read_rules("- a\n- b\n")
    with pytest.raises(RulesError, match="MB"):
        read_rules("#" * (3 * 1024 * 1024))
    settings, notices = read_rules(
        "port: 7000\nidentity: {hostname: x}\nfoo: 1\nprofile: loud\nallow_patterns: ['(']\npii: [EMAIL, MADE_UP]\n"
    )
    assert len(notices) == 6 and settings.pii == ["EMAIL"] and settings.allow_patterns == []
    broken, notices = read_rules(yaml.safe_dump({"recognizers": [{"name": "x", "pattern": "("}]}))
    assert broken.recognizers == [] and notices[0].startswith("recognizer x")
    # A config.yaml is read too: its rules, and the reviewer's guidance.
    settings, notices = read_rules("profile: max\nport: 7000\nllm_review: {enabled: true, guidance: Wards.}\n")
    assert settings.profile == "max" and settings.review_guidance == "Wards." and len(notices) == 1


def test_into_a_project_measured_against_what_applies_there(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    settings, _ = read_rules("profile: balanced\nterms: [{value: Banca Ligure, type: CLIENT}]\nallow_values: [x]\n")
    merged = merge_rules(Config(profile="max"), settings, str(root))
    [entry] = merged.protective.projects
    assert [t.value for t in entry.terms] == ["Banca Ligure"] and entry.profile is None  # weaker than max
    assert any(s.startswith("profile") for s in merged.report.held)
    [full] = merged.full.projects
    assert full.profile == "balanced" and full.allow_values == ["x"]
    assert merged.protective.terms == []  # the global rules are untouched


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    from latet.cli import admin, rules

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)  # no daemon
    monkeypatch.setattr(rules, "_known_in_vault", lambda values: None)
    return tmp_path


def _cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def test_the_commands(isolated):
    save_config(ALICE.model_copy(update={"projects": []}))
    out = isolated / "rules.yaml"
    exported = _cli("rules", "export", str(out))
    assert exported.exit_code == 0, exported.output
    assert load_yaml(out.read_text("utf-8"))["profile"] == "max"
    assert "Fonderia" in _cli("rules", "export").stdout
    save_config(Config())
    dry = _cli("rules", "import", str(out), "--dry-run")
    assert dry.exit_code == 0 and "Would add:" in dry.output and "--all applies them too" in dry.output
    assert "Added:" not in dry.output  # planned, never said done (EOP-27)
    assert load_config().profile == "balanced"
    done = _cli("rules", "import", str(out))
    # Saved; no daemon answered the reload, so whether it applies is not known: said so, and exit 1.
    assert done.exit_code == 1 and "unconfirmed" in done.output, done.output
    assert load_config().profile == "max" and load_config().allow_values == []
    _cli("rules", "import", "-", "--all", input=out.read_text("utf-8"))
    assert load_config().allow_values == ["acme-dev"]
    broken = _cli("rules", "import", "-", input="latet_rules: 99\n")
    assert broken.exit_code == 2 and "newer" in broken.output
