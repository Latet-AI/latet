"""S-03 on a computer: a term longer than a part of its list holds (``TERM_LONGEST``) is compiled alone, and one of a
million letters with more than one case (``k``) compiles within no budget, so that the daemon's rules could not be built
at all: it did not start, and a reload failed. Whatever saves a computer's settings refuses such a term, in words that
name its place and never the term; a file that holds one anyway (config.yaml written by hand, the settings approved
before, a repository's latet.yaml, a file of rules imported) is read with it left out and a notice by its place, so that
the rest applies and nothing is left out unsaid."""

import json

import pytest
import yaml
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, ProjectEntry, TermConfig, load_config, term_errors
from latet.engine.detectors.terms import TERM_LONGEST
from latet.engine.types import SlotKind
from latet.gateway.app import create_app
from latet.projects import Projects
from latet.runtime import REVIEWED_FILE, make_runtime
from latet.server.shared import SharedItemsError
from latet.state import LocalSettings
from latet.transfer import read_rules

from ...conftest import ROOT_KEY
from .test_settings import _reload

LONG = "Orion" + "x" * TERM_LONGEST  # one character more than a term holds
UNCOMPILED = "Orion" + "k" * 1_000_000  # compiles within no budget: k, K and the Kelvin sign at each place
HELD = "xxxxx"  # what of the long term nothing may say


def _said(text: str) -> bool:
    return str(TERM_LONGEST) in text and HELD not in text and "kkkkk" not in text


def test_terms_too_long_are_refused_by_their_places_and_never_said():
    errors = term_errors(["Mercury Labs", LONG])
    assert [(e["loc"], e["type"]) for e in errors] == [(["terms", 1, "value"], "value_error")]
    assert _said(errors[0]["msg"])
    assert term_errors([LONG], ("projects", 0, "terms"))[0]["loc"] == ["projects", 0, "terms", 0, "value"]
    assert term_errors(["Mercury Labs", LONG], kept=[LONG]) == []  # one there already blocks no other change


def _signed_in(rt) -> TestClient:
    client = TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283")
    code = rt.gateway.dashboard.issue_code()
    assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
    return client


def test_a_computers_saves_refuse_a_term_too_long_by_its_place(tmp_path):
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **start)
    ui = {"x-latet-ui": "1"}
    try:
        with _signed_in(rt) as client:
            whole = client.put("/_latet/api/v1/config", json={"terms": [{"value": "Mercury Labs"}, {"value": LONG}]},
                               headers=ui)  # fmt: skip
            one = client.post("/_latet/api/v1/terms", json={"value": UNCOMPILED}, headers=ui)
        assert whole.status_code == 400 and _said(whole.text)
        assert [d["loc"] for d in whole.json()["details"]] == [["terms", 1, "value"]]
        assert one.status_code == 409 and _said(one.text) and "terms.0" in one.text
        assert rt.config.terms == [] and not (tmp_path / "config.yaml").exists()  # nothing applied, nothing written
        entry = ProjectEntry(path=str(tmp_path / "shop"), terms=[TermConfig(value=LONG)])
        with pytest.raises(SharedItemsError) as refused:  # as a change that cannot be kept is, whatever saves it
            rt.settings.prepare(Config(projects=[entry]), strict=True)
        assert "projects.0.terms.0" in str(refused.value) and _said(str(refused.value))
    finally:
        rt.close()


def _hand_edited(tmp_path):
    """A config.yaml edited by hand: a term that compiles nowhere globally, one too long in a project's entry."""
    path = tmp_path / "config.yaml"
    data = {
        "identity": {"enabled": False},
        "terms": [{"value": "Mercury Labs", "type": "CLIENT"}, {"value": UNCOMPILED}],
        "projects": [{"path": str(tmp_path / "shop"), "terms": [{"value": LONG}, {"value": "Heron Labs"}]}],
    }
    path.write_text(yaml.safe_dump(data), "utf-8")
    return path


LEFT_OUT = [
    f"terms entry 2 holds more than {TERM_LONGEST} characters and is left out",
    f"projects entry 1, terms entry 1 holds more than {TERM_LONGEST} characters and is left out",
]


def test_a_config_yaml_holding_terms_too_long_loads_with_them_left_out_and_said(tmp_path, caplog):
    notices: list[str] = []
    config = load_config(_hand_edited(tmp_path), notices)
    assert [t.value for t in config.terms] == ["Mercury Labs"]
    assert [t.value for t in config.projects[0].terms] == ["Heron Labs"]
    assert notices == LEFT_OUT
    assert [r.getMessage() for r in caplog.records if r.name == "latet.config"] == [
        f"config.yaml: {n}" for n in LEFT_OUT
    ]
    assert HELD not in caplog.text and "kkkkk" not in caplog.text


def test_the_daemon_starts_and_reloads_with_a_config_yaml_holding_a_term_that_compiles_nowhere(tmp_path):
    """It did not start: the term compiled within no budget, and the rules could not be built. Now the rest applies,
    and the reload's answer says what was left out, as the log does."""
    path = _hand_edited(tmp_path)
    rt = make_runtime(data=tmp_path / "data", config_file=path, root_key=ROOT_KEY)
    try:
        masked = rt.built.masker.mask("Mercury Labs signed", SlotKind.USER_TEXT).text
        assert "Mercury Labs" not in masked and [t.value for t in rt.config.terms] == ["Mercury Labs"]
        path.write_text(path.read_text("utf-8").replace("Mercury Labs", "Mercury Group"), "utf-8")
        answer = _reload(rt)  # a term replaced: what it lowers waits, and what the load left out is said all the same
        assert answer["notices"] == LEFT_OUT and not answer["applied"]
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == ["terms: 1 value no longer masked"]
    finally:
        rt.close()


def test_settings_approved_holding_a_term_too_long_still_count_without_it(tmp_path, caplog):
    path = tmp_path / "data" / REVIEWED_FILE
    path.parent.mkdir()
    approved = Config(terms=[TermConfig(value="Mercury Labs")]).model_dump(mode="json")
    path.write_text(json.dumps({**approved, "terms": [*approved["terms"], {"value": LONG}]}), "utf-8")
    reviewed = LocalSettings(tmp_path / "config.yaml", tmp_path / "data").reviewed()
    assert reviewed is not None and [t.value for t in reviewed.terms] == ["Mercury Labs"]
    said = [r.getMessage() for r in caplog.records if r.name == "latet.state.local"]
    assert said == [f"{REVIEWED_FILE}: terms entry 2 holds more than {TERM_LONGEST} characters and is left out"]
    assert HELD not in caplog.text


def test_a_repositorys_term_too_long_is_left_out_with_a_notice_by_its_place(tmp_path):
    # A latet.yaml is read up to 256 KB: a term that compiles nowhere cannot be in one, a term too long can.
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_text(yaml.safe_dump({"terms": [{"value": LONG}, {"value": "Heron Labs"}]}))
    project = Projects(Config(identity=IdentityConfig(enabled=False)), _vault()).resolve(str(root))
    [notice] = [n for n in project.notices if n.key == "terms"]
    assert notice.message == f"entry 1 holds more than {TERM_LONGEST} characters: left out"
    assert [t.value for t in project.config.terms] == ["Heron Labs"] and project.scope is not None


def test_an_imported_term_too_long_is_left_out_with_a_notice_by_its_place():
    settings, notices = read_rules(yaml.safe_dump({"terms": [{"value": "Heron Labs"}, {"value": LONG}]}))
    assert [t.value for t in settings.terms] == ["Heron Labs"]
    assert notices == [f"terms: entry 2 holds more than {TERM_LONGEST} characters, left out"]


def _vault():
    from latet.engine.vault import Vault

    return Vault(ROOT_KEY)
