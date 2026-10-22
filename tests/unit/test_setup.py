from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers global-install
from latet.cli.main import app
from latet.config import load_config
from latet.integrations.routes import ROUTE_VARS


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ROUTE_VARS:  # where the agents go is read from the shell too: none of this one's
        monkeypatch.delenv(var, raising=False)
    from latet.service import autostart, daemon

    calls = {"autostart": 0, "start": 0}
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=port or 5283))
    monkeypatch.setattr(autostart, "install", lambda: calls.__setitem__("autostart", calls["autostart"] + 1))
    monkeypatch.setattr(
        daemon,
        "start",
        lambda: calls.__setitem__("start", calls["start"] + 1) or SimpleNamespace(running=False, port=5283),
    )
    return tmp_path, calls


def test_global_install_runs_without_prompts_from_options(isolated):
    tmp_path, calls = isolated
    result = CliRunner().invoke(
        app,
        [
            "global-install",
            "--yes",
            "--profile",
            "max",
            "--no-identity",
            "--agents",
            "none",
            "--no-ner",
            "--no-autostart",
        ],
        input="",  # any prompt would read EOF and fail
    )
    assert result.exit_code == 0, result.output
    config = load_config(tmp_path / "config.yaml")
    assert config.profile.value == "max" and config.identity.enabled is False
    assert calls == {"autostart": 0, "start": 1}


def test_global_install_rejects_unknown_agents_profiles_and_review_modes(isolated):
    runner = CliRunner()
    bad_agent = runner.invoke(app, ["global-install", "--yes", "--agents", "claude,cursor"])
    assert bad_agent.exit_code != 0 and "cursor" in bad_agent.output
    bad_review = runner.invoke(app, ["global-install", "--yes", "--auto-mode-review", "maybe"])
    assert bad_review.exit_code != 0
    bad_profile = runner.invoke(app, ["global-install", "--yes", "--profile", "loud"])
    assert bad_profile.exit_code == 2 and "balanced, max or secrets" in bad_profile.output


def test_global_install_takes_the_id_regions(isolated, monkeypatch):
    import latet.config

    tmp_path, _ = isolated
    monkeypatch.setattr(latet.config, "system_country", lambda: "DE")
    args = ["global-install", "--yes", "--no-identity", "--agents", "none", "--no-ner", "--no-autostart"]
    runner = CliRunner()
    assert runner.invoke(app, args).exit_code == 0
    assert load_config(tmp_path / "config.yaml").id_regions == ["DE", "US", "GB"]  # this machine's country first
    assert runner.invoke(app, [*args, "--id-regions", "es, it"]).exit_code == 0
    assert load_config(tmp_path / "config.yaml").id_regions == ["ES", "IT"]
    assert runner.invoke(app, args).exit_code == 0  # a choice in config.yaml is kept
    assert load_config(tmp_path / "config.yaml").id_regions == ["ES", "IT"]
    bad = runner.invoke(app, [*args, "--id-regions", "IT,XX"])
    assert bad.exit_code != 0 and "country codes" in bad.output


def test_run_with_an_agent_is_its_wrapper(isolated, monkeypatch):
    from latet import wrappers

    launched = []
    monkeypatch.setattr(wrappers, "run", lambda argv, env: launched.append((argv, env)) or 0)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda **_kwargs: None)
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 120, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": True)
    runner = CliRunner()
    result = runner.invoke(app, ["run", "claude", "--resume"])
    assert result.exit_code == 0 and "latet run claude is latet claude" in result.output
    argv, env = launched.pop()
    assert argv[:2] == ["claude", "--settings"] and "--plugin-dir" in argv and argv[-1] == "--resume"  # with its plugin
    assert env["LATET_BRIDGE_TOKEN"] and env["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert runner.invoke(app, ["run", "codex.exe", "exec", "hi"]).exit_code == 0
    argv, _ = launched.pop()
    assert argv[0] == "codex.exe" and argv[2] == 'openai_base_url="http://127.0.0.1:5283/backend-api/codex"'
    # Anything else gets the two addresses, and runs as usual.
    assert runner.invoke(app, ["run", "python", "script.py"]).exit_code == 0
    argv, env = launched.pop()
    assert argv == ["python", "script.py"] and env["OPENAI_BASE_URL"] == "http://127.0.0.1:5283/openai/v1"


def test_a_missing_agent_is_named(isolated, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = CliRunner().invoke(app, ["claude"])
    assert result.exit_code == 127 and "Cannot find `claude`" in result.output
    assert isolated[1]["start"] == 0  # the daemon is not started for nothing


def test_without_the_extras_it_points_at_upgrade_never_at_pypi(isolated, monkeypatch):
    from latet.engine.detectors import ner
    from latet.vision import models

    monkeypatch.setattr(ner, "runtime_available", lambda: False)
    monkeypatch.setattr(models, "runtime_available", lambda: False)
    result = CliRunner().invoke(app, ["global-install", "--agents", "none"])  # no terminal: shown, nothing changed
    assert result.exit_code == 2, result.output
    names = next(line for line in result.output.splitlines() if "Names and addresses" in line)
    images = next(line for line in result.output.splitlines() if "Text in images" in line)
    assert "latet upgrade" in names and "latet upgrade" in images
    assert "uv tool install" not in result.output


def test_with_max_it_says_strict_data_mode_comes_with_it(isolated):
    def profile_row(*args: str) -> str:
        shown = CliRunner().invoke(app, ["global-install", "--agents", "none", *args])  # shown, nothing changed
        return next(line for line in shown.output.splitlines() if line.strip().startswith("Profile"))

    assert "strict data mode" in profile_row("--profile", "max")
    assert "strict data mode" not in profile_row()


def test_it_says_what_it_changes_and_how_to_undo_it_and_asks_once(isolated, monkeypatch):
    from latet.cli import agent

    tmp_path, calls = isolated
    args = ["global-install", "--agents", "none", "--no-ner"]
    blind = CliRunner().invoke(app, args)  # no terminal of yours: nothing changes without --yes
    assert blind.exit_code == 2 and "add --yes once the user agreed" in blind.output
    monkeypatch.setattr(agent, "at_your_terminal", lambda strict=False: True)
    declined = CliRunner().invoke(app, args, input="n\n")
    assert declined.exit_code == 1 and "Nothing changed." in declined.output
    for line in ("Start at login", "Your identity", "Profile", "Port", "global-uninstall` takes all of it away"):
        assert line in declined.output, line
    assert not (tmp_path / "config.yaml").exists() and calls == {"autostart": 0, "start": 0}
    accepted = CliRunner().invoke(app, args, input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert calls == {"autostart": 1, "start": 1}  # start at login by default
    identity = load_config(tmp_path / "config.yaml").identity
    assert identity.enabled and identity.usernames == [] and identity.emails == []  # detected at every start
