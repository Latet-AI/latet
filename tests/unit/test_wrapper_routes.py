"""`latet claude` and `latet codex` put Latet in front of where the agent goes, for one session:
Claude Code through a private settings file that beats its own settings, Codex through `-c`."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app
from latet.config import Config
from latet.integrations.routes import ROUTE_VARS

LATET = "http://127.0.0.1:5283"
REAL_RUN = wrappers.run  # `launched` records what would start in its place


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """What the agent was started with: its arguments, its variables and its settings file (read while
    it runs: it is gone after)."""
    for var in ROUTE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "claude").mkdir()
    (tmp_path / "codex").mkdir()
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda **_kwargs: None)
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 161, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    runs: list[dict] = []

    def run(argv, env):
        settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        content = json.loads(Path(settings).read_text("utf-8")) if settings else None
        runs.append({"argv": argv, "env": env, "settings": settings, "content": content})
        return 0

    monkeypatch.setattr(wrappers, "run", run)
    # No test dials a daemon: the one that hands AWS credentials over replaces this below.
    monkeypatch.setattr(wrappers, "hold_aws_session", lambda session, config=None: pytest.fail("nothing to hold"))
    added: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wrappers, "destination_for", lambda url, agent, config=None, **_kwargs: added.append((url, agent)) or "acme"
    )
    return runs, added, tmp_path


def test_claude_runs_with_settings_that_beat_its_own(launched):
    runs, added, tmp_path = launched
    (tmp_path / "claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://llm.acme.com"}})
    )
    result = CliRunner().invoke(app, ["claude", "-p", "hi"])
    assert result.exit_code == 0, result.output
    [run] = runs
    assert run["argv"][1] == "--settings" and run["argv"][-2:] == ["-p", "hi"] and "--plugin-dir" in run["argv"]
    env = run["content"]["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"{LATET}/acme" and env["LATET_BRIDGE_URL"] == LATET
    assert env["LATET_BRIDGE_TOKEN"] and run["env"]["ANTHROPIC_BASE_URL"] == f"{LATET}/acme"
    assert added == [("https://llm.acme.com", "claude")]
    assert not Path(run["settings"]).exists()  # private, and gone once Claude Code exits
    assert "llm.acme.com" in result.output


def test_settings_of_your_own_are_merged_in(launched):
    runs, _, _ = launched
    mine = json.dumps({"model": "opus", "env": {"MY_VAR": "1"}})
    assert CliRunner().invoke(app, ["claude", "--settings", mine, "--resume"]).exit_code == 0
    [run] = runs
    assert run["argv"].count("--settings") == 1 and run["argv"][-1] == "--resume"
    assert run["content"]["model"] == "opus" and run["content"]["env"]["MY_VAR"] == "1"
    assert run["content"]["env"]["ANTHROPIC_BASE_URL"] == f"{LATET}/anthropic"


def test_aws_keys_only_in_the_shell_live_as_long_as_the_session(launched, monkeypatch):
    runs, _, _ = launched
    for name, value in {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "the-secret",
        "AWS_CREDENTIAL_EXPIRATION": "2026-10-09T18:00:00Z",
    }.items():
        monkeypatch.setenv(name, value)
    handed, dropped, held = [], [], []
    monkeypatch.setattr(
        wrappers, "aws_session", lambda credentials, config=None: handed.append(credentials) or "f" * 32
    )
    monkeypatch.setattr(wrappers, "drop_aws_session", lambda session, config=None: dropped.append(session))
    monkeypatch.setattr(wrappers, "hold_aws_session", lambda session, config=None: held.append(session))
    assert CliRunner().invoke(app, ["claude"]).exit_code == 0
    [run] = runs
    assert run["content"]["env"]["ANTHROPIC_BEDROCK_BASE_URL"] == f"{LATET}/bedrock/eu-west-1/session/{'f' * 32}"
    assert run["content"]["env"]["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    assert "ANTHROPIC_BASE_URL" not in run["content"]["env"]
    assert [c.access_key for c in handed] == ["AKIDEXAMPLE"] and dropped == ["f" * 32]
    assert [c.expires for c in handed] == [datetime(2026, 10, 9, 18, 0, tzinfo=UTC).timestamp()]
    assert held == ["f" * 32]  # held from the moment it exists, for as long as this process lives
    assert "the-secret" not in json.dumps(run["content"])


def test_a_daemon_that_does_not_answer_leaves_the_session_without_a_hold(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr("http.client.HTTPConnection.request", refuse)
    assert wrappers.hold_aws_session("f" * 32, Config(port=1)) is None  # the session lasts until it is dropped


def test_what_latet_cannot_stand_in_front_of_does_not_start(launched, monkeypatch):
    runs, _, _ = launched
    monkeypatch.setenv("CLAUDE_CODE_USE_MANTLE", "1")
    result = CliRunner().invoke(app, ["claude"])
    assert result.exit_code == 2 and "Mantle" in result.output and runs == []


def test_an_address_on_this_computer_is_left_alone_and_said(launched, monkeypatch):
    runs, added, _ = launched
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")
    result = CliRunner().invoke(app, ["claude"])
    assert result.exit_code == 0 and "latet config add upstreams" in result.output
    [run] = runs
    assert "ANTHROPIC_BASE_URL" not in run["content"]["env"] and added == []


def test_codex_providers_move_to_latet_for_the_session(launched):
    runs, added, tmp_path = launched
    (tmp_path / "codex" / "config.toml").write_text(
        'model_provider = "azure"\n[model_providers.azure]\nname = "Azure"\n'
        'base_url = "https://acme.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\n',
        "utf-8",
    )
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 0, result.output
    [run] = runs
    assert run["argv"][:3] == ["codex", "-c", f'openai_base_url="{LATET}/openai/v1"']
    assert ["-c", f'model_providers.azure.base_url="{LATET}/acme"'] == run["argv"][3:5]
    assert run["argv"][-2:] == ["exec", "hi"] and added == [("https://acme.openai.azure.com/openai/v1", "codex")]
    assert "acme.openai.azure.com" in result.output


def test_codex_on_an_address_on_this_computer_is_not_said_to_be_behind_latet(launched):
    runs, added, tmp_path = launched
    (tmp_path / "codex" / "config.toml").write_text('openai_base_url = "http://localhost:4000"\n', "utf-8")
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 0, result.output
    assert runs[-1]["argv"] == ["codex", "exec", "hi"] and added == []  # nothing to override: left alone
    assert "localhost:4000, on this computer: left as it is" in result.output
    assert "goes in front of it" not in result.output


def test_codex_overrides_only_the_providers_of_its_profile(launched):
    runs, _, tmp_path = launched
    work = '[model_providers.acme]\nname = "Acme"\nbase_url = "https://gw.acme.com/v1"\n'
    (tmp_path / "codex" / "work.config.toml").write_text(work, "utf-8")
    assert CliRunner().invoke(app, ["codex", "exec", "hi"]).exit_code == 0
    assert not any("model_providers.acme" in arg for arg in runs[-1]["argv"])  # Codex would refuse to start
    assert CliRunner().invoke(app, ["codex", "--profile", "work", "exec", "hi"]).exit_code == 0
    assert f'model_providers.acme.base_url="{LATET}/acme"' in runs[-1]["argv"]


def test_a_codex_too_old_for_its_providers_does_not_start(launched, monkeypatch):
    runs, _, tmp_path = launched
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    (tmp_path / "codex" / "config.toml").write_text(
        'model_provider = "azure"\n[model_providers.azure]\nname = "Azure"\n'
        'base_url = "https://acme.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\n',
        "utf-8",
    )
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 2 and runs == []  # started, it would go to Azure around Latet
    assert "0.115" in result.output and "azure" in result.output and "update" in result.output
    assert "goes in front of it" not in result.output  # no summary that says the opposite


def test_a_codex_too_old_for_providers_still_takes_openai_base_url(launched, monkeypatch):
    runs, _, _ = launched
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    assert CliRunner().invoke(app, ["codex", "exec", "hi"]).exit_code == 0
    [run] = runs
    assert run["argv"] == ["codex", "exec", "hi"] and run["env"] == {"OPENAI_BASE_URL": f"{LATET}/openai/v1"}


@pytest.mark.parametrize(
    ("chatgpt", "address"),
    [(False, f"{LATET}/openai/v1"), (True, f"{LATET}/backend-api/codex")],
    ids=["api-key", "chatgpt"],
)
def test_a_codex_too_old_for_config_toml_gets_latet_address_where_it_reads_it(launched, monkeypatch, chatgpt, address):
    runs, _, tmp_path = launched
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": chatgpt)
    # As `agents apply codex` writes it: an old Codex ignores it, and reads only OPENAI_BASE_URL.
    (tmp_path / "codex" / "config.toml").write_text(f'openai_base_url = "{address}"\n', "utf-8")
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 0, result.output
    [run] = runs
    assert run["argv"] == ["codex", "exec", "hi"] and run["env"] == {"OPENAI_BASE_URL": address}


def test_latet_address_wins_over_the_one_your_shell_gives_an_old_codex(launched, monkeypatch):
    _, _, tmp_path = launched
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example")
    (tmp_path / "codex" / "config.toml").write_text(f'openai_base_url = "{LATET}/openai/v1"\n', "utf-8")
    started: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(wrappers, "run", REAL_RUN)  # Codex started as it is, with your shell's variables
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: started.append((argv, env)) or 0)
    assert CliRunner().invoke(app, ["codex", "exec", "hi"]).exit_code == 0
    [(argv, env)] = started
    assert argv == ["/usr/bin/codex", "exec", "hi"] and env["OPENAI_BASE_URL"] == f"{LATET}/openai/v1"


def test_a_codex_too_old_for_config_toml_gets_an_address_on_this_computer_where_it_reads_it(launched, monkeypatch):
    runs, added, tmp_path = launched
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    (tmp_path / "codex" / "config.toml").write_text('openai_base_url = "http://localhost:4000"\n', "utf-8")
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])  # an address on this computer: left alone, and said
    assert result.exit_code == 0, result.output
    assert runs[-1]["argv"] == ["codex", "exec", "hi"]
    assert runs[-1]["env"] == {"OPENAI_BASE_URL": "http://localhost:4000"} and added == []
    assert "latet config add upstreams" in result.output
    with pytest.raises(wrappers.CodexTooOld, match="OPENAI_BASE_URL"):
        wrappers.codex_argv(["exec"], {})  # no address to give it: it would go to OpenAI
