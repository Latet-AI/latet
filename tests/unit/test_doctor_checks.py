"""What `latet doctor` says of the daemon and of local name detection, right after an install and later."""

import json
from types import SimpleNamespace

import pytest

from latet.config import Config, data_dir, save_config
from latet.doctor import checks
from latet.service import autostart, daemon


@pytest.fixture
def stopped(computer, monkeypatch):
    """No daemon running here, Latet not started at login, outside the container."""
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    monkeypatch.setattr(autostart, "installed", lambda: False)
    monkeypatch.setattr("latet.config.in_container", lambda: False)
    return computer


def _daemon(found):
    return next(check for check in found if check.title == "Daemon")


def _routed(*agents):
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = {agent: {"integration": agent, "applied_at": 1.0} for agent in agents}
    (data_dir() / "integrations.json").write_text(json.dumps(rows), encoding="utf-8")


def test_a_stopped_daemon_is_a_warning_while_no_agent_goes_through_latet_for_good(stopped):
    """Right after the installer, nothing is routed through Latet yet and the wrappers start the daemon themselves:
    not running is no failure there."""
    check = _daemon(checks.check_daemon())
    assert check.status == checks.WARN
    assert "latet start" in check.detail and "latet claude" in check.detail


def test_a_stopped_daemon_fails_once_an_agent_goes_through_latet_for_good(stopped):
    _routed("codex")
    check = _daemon(checks.check_daemon())
    assert check.status == checks.FAIL
    assert "Codex" in check.detail and "latet start" in check.detail


def test_a_record_of_another_settings_folder_routes_nothing_through_the_daemon(stopped):
    """Latet applied Claude Code with another CLAUDE_CONFIG_DIR: the settings it uses now go elsewhere."""
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    other = {
        "path": str(stopped / "elsewhere" / "settings.json"),
        "backup": None,
        "sha_before": None,
        "sha_after": None,
    }
    rows = {"claude": {"integration": "claude", "applied_at": 1.0, "files": [other]}}
    (data_dir() / "integrations.json").write_text(json.dumps(rows), encoding="utf-8")
    assert _daemon(checks.check_daemon()).status == checks.WARN


def test_a_stopped_daemon_fails_when_latet_starts_at_login(stopped, monkeypatch):
    monkeypatch.setattr(autostart, "installed", lambda: True)
    assert _daemon(checks.check_daemon()).status == checks.FAIL


def _names():
    found = checks.check_ner()
    assert [check.title for check in found] == ["Names"], found
    assert found[0].status == checks.OK
    return found[0].detail


def test_the_names_line_says_the_model_is_not_downloaded(computer, monkeypatch):
    from latet.engine.detectors import ner

    monkeypatch.setattr(ner, "runtime_available", lambda: True)
    monkeypatch.setattr(ner, "is_installed", lambda data, spec=ner.MODEL: False)
    detail = _names()
    assert "not looked for" in detail and "latet ner install" in detail


def test_the_names_line_says_the_extra_is_missing(computer, monkeypatch):
    from latet.engine.detectors import ner

    monkeypatch.setattr(ner, "runtime_available", lambda: False)
    monkeypatch.setattr(ner, "is_installed", lambda data, spec=ner.MODEL: False)
    detail = _names()
    assert "not looked for" in detail and "ner extra" in detail


@pytest.mark.parametrize(
    ("config", "said"),
    [
        (Config(), "in your prompts and attachments"),
        (Config(profile="max"), "everywhere"),
        (Config(ner="off"), "`ner: off`"),
        (Config(profile="secrets"), "secrets"),
    ],
    ids=["balanced", "max", "off", "secrets"],
)
def test_the_names_line_says_where_names_are_looked_for(computer, monkeypatch, config, said):
    from latet.config import config_path
    from latet.engine.detectors import ner

    save_config(config, config_path())
    monkeypatch.setattr(ner, "runtime_available", lambda: True)
    monkeypatch.setattr(ner, "is_installed", lambda data, spec=ner.MODEL: True)
    assert said in _names()
