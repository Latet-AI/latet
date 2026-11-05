"""`latet claude` and `latet codex` put Latet in front of where the agent goes, for one session:
Claude Code through a private settings file that beats its own settings, Codex through `-c`."""

import json
import tomllib
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
    # OpenCode at its verified carrier (P2-8a, `latet.launch_carriers`): never this computer's `opencode --version`.
    monkeypatch.setattr(wrappers, "opencode_version", lambda exe="opencode": (1, 19, 5), raising=False)
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (tmp_path / "absent-policy.toml",))
    runs: list[dict] = []

    def run(argv, env, **_kwargs):
        settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        content = json.loads(Path(settings).read_text("utf-8")) if settings else None
        profile_path = profile_text = None
        if argv[0] == "codex" and "--profile" in argv:
            name = argv[argv.index("--profile") + 1]
            if name.startswith("latet-"):
                profile_path = tmp_path / "codex" / f"{name}.config.toml"
                profile_text = profile_path.read_text("utf-8")
        runs.append(
            {
                "argv": argv,
                "env": env,
                "settings": settings,
                "content": content,
                "profile_path": profile_path,
                "profile_text": profile_text,
            }
        )
        return 0

    monkeypatch.setattr(wrappers, "run", run)

    def no_launch(*_args, **_kwargs):
        from latet.launch_session import LaunchControlUnavailable

        raise LaunchControlUnavailable()

    # Existing wrapper controls never dial a daemon after lifecycle wiring.
    # The two causal/green carrier rows override these exact thin adapters.
    monkeypatch.setattr(wrappers, "_launch_call", no_launch, raising=False)
    monkeypatch.setattr(
        wrappers, "_launch_hold", lambda *_args, **_kwargs: pytest.fail("nothing to hold"), raising=False
    )
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
    config = tmp_path / "codex" / "config.toml"
    original = (
        b'model_provider = "azure"\n[model_providers.azure]\nname = "Azure"\n'
        b'base_url = "https://acme.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\n'
    )
    config.write_bytes(original)
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 0, result.output
    [run] = runs
    private_path, private_text = run["profile_path"], run["profile_text"]
    assert private_path is not None and private_text is not None
    argv = run["argv"]
    profile, remaining, position = wrappers.codex_profile(argv[1:])
    assert profile == private_path.name.removesuffix(".config.toml")
    assert position is not None and argv[position + 1 : position + 3] == ["--profile", profile]
    assert [argv[0], *remaining] == ["codex", "exec", "hi"]
    private = tomllib.loads(private_text)
    assert private["openai_base_url"] == f"{LATET}/openai/v1"
    assert private["model_providers"]["azure"]["base_url"] == f"{LATET}/acme"
    assert config.read_bytes() == original and not private_path.exists()
    assert added == [("https://acme.openai.azure.com/openai/v1", "codex")]
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


@pytest.mark.parametrize("capacity", [False, True])
def test_claude_launch_controls_precede_private_carrier_publication(launched, monkeypatch, capacity):
    runs, _, _ = launched
    original_run = wrappers.run
    calls = []
    held = []
    key = "latet_lk_" + "A" * 43
    launch_id = "1" * 32
    base = "/_latet/engine/v1/launches"

    class Hold:
        generation = 1

        def wait(self):
            pass

        def close(self):
            pass

    def call(method, path, body, **_kwargs):
        assert path.startswith(base)
        calls.append((method, path))
        if path == base:
            if capacity:
                return {"key": None, "launch_id": None, "reason": "launch-capacity"}
            return {"key": key, "launch_id": launch_id}
        if path.endswith("/prepare"):
            return {"epoch": "2" * 32, "retirement": 0}
        if path.endswith("/end"):
            return {"ended": True}
        if path.endswith("/exit"):
            return {"retired": True, "epoch": "2" * 32, "retirement": 1}
        pytest.fail("no recovery is needed for this bounded wrapper case")

    def hold(identifier, generation, **_kwargs):
        assert identifier == launch_id and generation == 0
        held.append(identifier)
        return Hold()

    # These are the selected future thin adapters, not authority mocks inside
    # the registry. raising=False allows the current wrapper to reach its real
    # child-launch seam and demonstrate the missing integration causally.
    monkeypatch.setattr(wrappers, "_launch_call", call, raising=False)
    monkeypatch.setattr(wrappers, "_launch_hold", hold, raising=False)

    def record(argv, env, **_kwargs):
        result = original_run(argv, env)
        run = runs[-1]
        run["control_before_spawn"] = tuple(calls)
        run["held_before_spawn"] = tuple(held)
        return result

    monkeypatch.setattr(wrappers, "run", record)
    mine = json.dumps(
        {"model": "opus", "env": {"MY_VAR": "1", "ANTHROPIC_CUSTOM_HEADERS": "X-Company: keep\nX-Trace: user"}}
    )
    result = CliRunner().invoke(app, ["claude", "--settings", mine, "--resume"])
    assert result.exit_code == 0
    [run] = runs
    assert run["control_before_spawn"] == (("POST", base),)
    assert run["content"]["model"] == "opus" and run["content"]["env"]["MY_VAR"] == "1"
    value = run["content"]["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Company: keep" in value and "X-Trace: user" in value
    assert key not in json.dumps(run["argv"]) and key not in result.output
    assert not Path(run["settings"]).exists()
    if capacity:
        assert run["held_before_spawn"] == ()
        assert "x-latet-launch-key:" not in value
        assert "LATET_LAUNCH_ID" not in run["content"]["env"]
    else:
        assert run["held_before_spawn"] == (launch_id,)
        assert value.count("x-latet-launch-key:") == 1 and key in value
        assert run["content"]["env"]["LATET_LAUNCH_ID"] == launch_id


def test_keyless_unverified_settings_scrub_only_reserved_headers(launched, monkeypatch):
    runs, _, _ = launched
    old_key = "latet_lk_" + "A" * 43
    headers = f"X-Company: kept\nX-Latet-Launch-Key: {old_key}\nX-Company: second"
    mine = json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": headers, "MY_VAR": "1"}})
    monkeypatch.setattr(
        wrappers, "_launch_call", lambda *_args, **_kwargs: pytest.fail("unverified carrier must stay keyless")
    )
    result = CliRunner().invoke(app, ["claude", "--settings", mine, "--worktree", "-p", "hi"])
    assert result.exit_code == 0
    [run] = runs
    assert run["content"]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-Company: kept\nX-Company: second"
    assert run["content"]["env"]["MY_VAR"] == "1"
    assert "LATET_LAUNCH_ID" not in run["content"]["env"]
    assert old_key not in json.dumps(run["content"]) and old_key not in result.output
    assert "--worktree" in run["argv"]
    assert not Path(run["settings"]).exists()
