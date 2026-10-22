"""`global-install` and `agents apply` keep a company's endpoint and go in front of it, for good; undoing
gives every address back, even after you edited the file since. The address is saved in config.yaml behind
the daemon only when no daemon answers: one that answers and refuses is left alone."""

import io
import json
import os
import stat
import urllib.error
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the agents commands
from latet import wrappers
from latet.cli.main import app
from latet.config import load_config
from latet.doctor import checks
from latet.integrations import agents as agents_module
from latet.integrations.agents import ClaudeCode, ClaudeOptions, Codex
from latet.integrations.base import FileEdit, Record, load_records, save_records
from latet.integrations.routes import ROUTE_VARS
from latet.storage.folders import private_folder_problem

LATET = "http://127.0.0.1:5283"
COMPANY = {"env": {"ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "company-key"}}
AZURE = (
    '# my providers\nmodel_provider = "azure"\n\n[model_providers.azure]\nname = "Azure"\n'
    'base_url = "https://acme.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\n'
)
REAL_DESTINATION_FOR = wrappers.destination_for  # `home` puts a daemon that is down in its place


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ROUTE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    # Private, as Claude Code's settings folder must be for Latet to read it (on macOS also unreadable by others).
    (tmp_path / "claude").mkdir(mode=0o700)
    (tmp_path / "codex").mkdir(mode=0o700)

    def claude_cli(self, *args):  # what `claude plugin install` does to settings.json
        if args[:2] == ("plugin", "install"):
            path = self.settings_path()
            data = json.loads(path.read_text("utf-8")) if path.exists() else {}
            data.setdefault("enabledPlugins", {})["latet@latet"] = True
            path.write_text(json.dumps(data, indent=2), "utf-8")
        return {"outcome": "ok"}

    monkeypatch.setattr(ClaudeCode, "_claude", claude_cli)
    monkeypatch.setattr("latet.integrations.routes.MANAGED_SETTINGS", (tmp_path / "managed.json",))
    # A Codex that reads config.toml: the one installed on this computer is never asked.
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 161, 0))

    def daemon_down(url, agent, config=None):
        raise wrappers.DaemonError("the daemon did not answer")

    monkeypatch.setattr(wrappers, "destination_for", daemon_down)  # destinations go straight to config.yaml
    return tmp_path


def _settings(home: Path) -> dict:
    return json.loads((home / "claude" / "settings.json").read_text("utf-8"))


def _private_settings(home: Path) -> None:
    """Claude Code's settings.json its user's alone, as the doctor reads no other on Linux and macOS: Claude Code
    writes it 0644, and the person makes it private (no change on Windows, where its access list is checked)."""
    (home / "claude" / "settings.json").chmod(0o600)


def test_the_settings_that_hold_the_bridge_token_are_yours_alone_once_applied(home):
    """The local bridge token opens the daemon's engine (a value restored from the vault): Claude Code writes its
    settings.json 0644, so where Latet puts the token there the file becomes the person's alone, on this computer as
    on a connected one."""
    settings = home / "claude" / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}), "utf-8")
    if os.name != "nt":
        settings.chmod(0o644)
    else:  # what 0644 is on Windows: the computer's users may read it
        from .test_eop_computer_namespace import _icacls

        _icacls(settings, "/grant", "*S-1-5-32-545:R")
        assert private_folder_problem(settings) is not None
    ClaudeCode().apply(ClaudeOptions(), load_config())
    assert _settings(home)["env"].get("LATET_BRIDGE_TOKEN")
    if os.name != "nt":
        assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    else:
        assert private_folder_problem(settings) is None


def test_a_company_gateway_stays_behind_latet_for_good(home):
    original = json.dumps(COMPANY, indent=4) + "\n"
    (home / "claude" / "settings.json").write_text(original, "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    env = _settings(home)["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"{LATET}/llm-acme-com" and env["ANTHROPIC_AUTH_TOKEN"] == "company-key"
    assert [(d.name, d.url, d.agent) for d in load_config().upstreams] == [
        ("llm-acme-com", "https://llm.acme.com", "claude")
    ]
    assert ClaudeCode().status() == "applied"
    assert ClaudeCode().revert() == "restored"
    assert (home / "claude" / "settings.json").read_text("utf-8") == original


def test_undoing_after_your_edits_gives_the_company_address_back(home):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    edited = _settings(home)
    edited["model"] = "opus"
    (home / "claude" / "settings.json").write_text(json.dumps(edited), "utf-8")
    ClaudeCode().revert()
    after = _settings(home)
    assert after["model"] == "opus" and after["env"] == COMPANY["env"]


def test_an_older_install_is_undone_from_its_backup(home):
    settings = home / "claude" / "settings.json"
    saved = home / "backup-settings.json"
    saved.write_text(json.dumps(COMPANY), "utf-8")
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"{LATET}/anthropic"}, "model": "opus"}), "utf-8")
    edit = FileEdit(str(settings), str(saved), "before", "not-the-current-file")
    save_records({"claude": Record("claude", 0.0, [edit], {"env": ["ANTHROPIC_BASE_URL"]})})
    ClaudeCode().revert()
    assert _settings(home)["env"]["ANTHROPIC_BASE_URL"] == "https://llm.acme.com"


def test_the_summary_reads_past_latet_own_values(home):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    route = ClaudeCode().route(load_config())
    assert route.kind == "gateway" and "llm.acme.com" in route.summary


def test_codex_on_azure_for_good_and_back(home):
    config_toml = home / "codex" / "config.toml"
    config_toml.write_text(AZURE, "utf-8")
    assert Codex().status() == "not applied"
    Codex().apply(load_config(), chatgpt=False)
    text = config_toml.read_text("utf-8")
    assert f'base_url = "{LATET}/azure-acme"' in text and "# my providers" in text
    assert f'openai_base_url = "{LATET}/openai/v1"' in text and Codex().status() == "applied"
    Codex().revert()
    assert config_toml.read_text("utf-8") == AZURE


def test_codex_undone_after_your_edits_gives_azure_back(home):
    config_toml = home / "codex" / "config.toml"
    config_toml.write_text(AZURE, "utf-8")
    Codex().apply(load_config(), chatgpt=False)
    config_toml.write_text(config_toml.read_text("utf-8") + '\nmodel = "gpt-5.1"\n', "utf-8")
    Codex().revert()
    text = config_toml.read_text("utf-8")
    assert 'base_url = "https://acme.openai.azure.com/openai/v1"' in text and 'model = "gpt-5.1"' in text
    assert "openai_base_url" not in text


def test_a_profile_s_provider_is_changed_where_it_is_written(home):
    (home / "codex" / "config.toml").write_text("", "utf-8")
    work = '[model_providers.acme]\nname = "Acme"\nbase_url = "https://gw.acme.com/v1"\n'
    (home / "codex" / "work.config.toml").write_text(work, "utf-8")
    Codex().apply(load_config(), chatgpt=False)
    assert f'base_url = "{LATET}/gw-acme-com"' in (home / "codex" / "work.config.toml").read_text("utf-8")
    Codex().revert()
    assert (home / "codex" / "work.config.toml").read_text("utf-8") == work


def test_global_install_says_what_stays_and_what_is_left_alone(home, monkeypatch):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in ("claude", "codex") else None)
    monkeypatch.setattr(agents_module, "_version", lambda exe: "1.0")
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 161, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    from latet.service import autostart, daemon

    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=port or 5283))
    monkeypatch.setattr(daemon, "start", lambda: SimpleNamespace(running=False, port=5283))
    monkeypatch.setattr(autostart, "install", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_USE_MANTLE", "1")  # Claude Code: one Latet cannot stand in front of
    result = CliRunner().invoke(app, ["global-install", "--yes", "--no-autostart", "--no-ner", "--port", "6799"])
    assert result.exit_code == 0, result.output
    assert "left as it is" in result.output and "Mantle" in result.output
    assert "claude" not in load_records() and "codex" in load_records()


def test_a_codex_that_ignores_config_toml_is_left_as_it_is(home, monkeypatch):
    config_toml = home / "codex" / "config.toml"
    config_toml.write_text(AZURE, "utf-8")
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    with pytest.raises(agents_module.RouteProblem) as refused:
        Codex().apply(load_config(), chatgpt=False)
    said = str(refused.value)
    assert said.startswith("Codex 0.110.0 ignores openai_base_url in config.toml: update it")
    assert "npm i -g @openai/codex" in said and "`latet codex`" in said
    assert config_toml.read_text("utf-8") == AZURE and "codex" not in load_records()
    assert load_config().upstreams == []  # no destination for a route it would not take


def test_agents_apply_and_global_install_say_a_codex_that_ignores_config_toml_is_left_as_it_is(home, monkeypatch):
    config_toml = home / "codex" / "config.toml"
    config_toml.write_text(AZURE, "utf-8")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(agents_module, "_version", lambda exe: "codex-cli 0.110.0")
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 110, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    from latet.service import autostart, daemon

    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=port or 5283))
    monkeypatch.setattr(daemon, "start", lambda: SimpleNamespace(running=False, port=5283))
    monkeypatch.setattr(autostart, "install", lambda: None)
    applied = CliRunner().invoke(app, ["agents", "apply", "codex"])
    assert applied.exit_code == 0, applied.output
    assert "Codex: left as it is (Codex 0.110.0 ignores openai_base_url in config.toml" in applied.output
    installed = CliRunner().invoke(app, ["global-install", "--yes", "--no-autostart", "--no-ner", "--port", "6799"])
    assert installed.exit_code == 0, installed.output
    assert "left as it is: Codex 0.110.0 ignores openai_base_url in config.toml" in installed.output
    assert "Codex: done" not in installed.output  # what it plans is what it does
    assert config_toml.read_text("utf-8") == AZURE and "codex" not in load_records()


def test_the_doctor_reads_each_agent_s_route(home, monkeypatch):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    _private_settings(home)
    monkeypatch.setattr(ClaudeCode, "detect", lambda self: agents_module.AgentInfo("claude", "Claude Code", True, "1"))
    monkeypatch.setattr(Codex, "detect", lambda self: agents_module.AgentInfo("codex", "Codex", False, None))
    found = {c.title: c for c in checks.check_agents()}
    assert "llm.acme.com" in found["Claude Code route"].detail
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://other.acme.com")
    differs = [c for c in checks.check_agents() if "your shell" in c.detail]
    assert differs and differs[0].status == checks.WARN


@pytest.mark.parametrize(
    ("shell", "var"),
    [
        ({"ANTHROPIC_BASE_URL": "https://llm.example.com"}, "ANTHROPIC_BASE_URL"),
        (
            {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": "https://llm.example.com"},
            "ANTHROPIC_BEDROCK_BASE_URL",
        ),
        (
            {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_BASE_URL": "https://llm.example.com"},
            "ANTHROPIC_VERTEX_BASE_URL",
        ),
        (
            {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_BASE_URL": "https://llm.example.com"},
            "ANTHROPIC_FOUNDRY_BASE_URL",
        ),
    ],
    ids=["gateway", "bedrock", "vertex", "foundry"],
)
def test_the_doctor_does_not_warn_about_a_shell_address_latet_already_stands_in_front_of(home, monkeypatch, shell, var):
    for name, value in shell.items():
        monkeypatch.setenv(name, value)
    ClaudeCode().apply(ClaudeOptions(), load_config())  # no settings.json: the company's address is only in the shell
    assert _settings(home)["env"][var] == f"{LATET}/llm-example-com"
    _private_settings(home)
    monkeypatch.setattr(ClaudeCode, "detect", lambda self: agents_module.AgentInfo("claude", "Claude Code", True, "1"))
    monkeypatch.setattr(Codex, "detect", lambda self: agents_module.AgentInfo("codex", "Codex", False, None))
    assert not [c for c in checks.check_agents() if "your shell" in c.detail]  # it put its own there, on purpose
    monkeypatch.setenv(var, "https://other.example.com")  # a shell that changed since is still said
    changed = [c for c in checks.check_agents() if "now say" in c.detail]
    assert changed and changed[0].status == checks.WARN


def test_the_doctor_still_warns_when_the_settings_were_changed_since_the_install(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://llm.example.com")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    edited = _settings(home)
    edited["env"]["ANTHROPIC_BASE_URL"] = "https://mine.example.com"  # no longer what Latet wrote
    (home / "claude" / "settings.json").write_text(json.dumps(edited), "utf-8")
    _private_settings(home)
    monkeypatch.setattr(ClaudeCode, "detect", lambda self: agents_module.AgentInfo("claude", "Claude Code", True, "1"))
    monkeypatch.setattr(Codex, "detect", lambda self: agents_module.AgentInfo("codex", "Codex", False, None))
    differs = [c for c in checks.check_agents() if "your shell" in c.detail]
    assert differs and differs[0].status == checks.WARN


def test_the_doctor_says_when_the_aws_cli_is_missing(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_PROFILE", "work")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    _private_settings(home)
    monkeypatch.setattr(ClaudeCode, "detect", lambda self: agents_module.AgentInfo("claude", "Claude Code", True, "1"))
    monkeypatch.setattr(Codex, "detect", lambda self: agents_module.AgentInfo("codex", "Codex", False, None))
    monkeypatch.setattr(checks, "find_aws_cli", lambda name="aws": None)
    missing = [c for c in checks.check_agents() if "AWS CLI" in c.detail]
    assert missing and missing[0].status == checks.WARN


@pytest.fixture
def on_bedrock(home, monkeypatch):
    """Real host trust checks and the AWS CLI's POSIX search places, with no `aws` on PATH. Only files under the
    test's folder exist: an AWS CLI in /usr/local/bin on the machine running the tests is not seen."""
    from latet.gateway import aws

    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_PROFILE", "work")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    _private_settings(home)
    monkeypatch.setattr(ClaudeCode, "detect", lambda self: agents_module.AgentInfo("claude", "Claude Code", True, "1"))
    monkeypatch.setattr(Codex, "detect", lambda self: agents_module.AgentInfo("codex", "Codex", False, None))
    real_is_file = Path.is_file
    monkeypatch.setattr(aws, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(Path, "is_file", lambda self, **kw: real_is_file(self, **kw) and self.is_relative_to(home))
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: None)
    return home


def _observed_aws_doctor(monkeypatch):
    """Forward the real route/finder and require the intended profile branch, even for the no-warning control."""
    route, find = ClaudeCode.route, checks.find_aws_cli
    routes, finds = [], []

    def observed_route(self, *args, **kwargs):
        result = route(self, *args, **kwargs)
        routes.append((result.problem, "/profile/" in result.env.get("ANTHROPIC_BEDROCK_BASE_URL", "")))
        return result

    def observed_find(*args, **kwargs):
        result = find(*args, **kwargs)
        finds.append(result)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(ClaudeCode, "route", observed_route)
        scoped.setattr(checks, "find_aws_cli", observed_find)
        result = checks.check_agents()
    assert routes == [(None, True)], "intended AWS profile branch was not reached"
    return result, finds


def test_the_doctor_finds_the_aws_cli_where_the_daemon_looks_for_it(on_bedrock, monkeypatch):
    cli = on_bedrock / ".local" / "bin" / "aws"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n", "utf-8")
    cli.chmod(0o755)
    result, finds = _observed_aws_doctor(monkeypatch)
    assert finds == [str(cli)], "real AWS finder did not return the owned candidate exactly once"
    assert not [c for c in result if "AWS CLI" in c.detail]


def test_the_doctor_says_where_it_looked_for_the_aws_cli(on_bedrock, monkeypatch):
    result, finds = _observed_aws_doctor(monkeypatch)
    assert finds == [None], "real AWS finder did not report absence exactly once"
    [missing] = [c for c in result if "AWS CLI" in c.detail]
    assert missing.status == checks.WARN
    assert "PATH" in missing.detail and "~/.local/bin" in missing.detail and "/snap/bin" in missing.detail


def _bedrock_at(address: str) -> dict:
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1", "AWS_PROFILE": "work"}
    return {"env": {**env, "ANTHROPIC_BEDROCK_BASE_URL": address}}


def test_bedrock_s_public_address_written_out_is_signed_by_latet_for_good_and_comes_back(home):
    public = "https://bedrock-runtime.eu-west-1.amazonaws.com"
    original = json.dumps(_bedrock_at(public), indent=4) + "\n"
    (home / "claude" / "settings.json").write_text(original, "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    env = _settings(home)["env"]
    assert env["ANTHROPIC_BEDROCK_BASE_URL"].startswith(f"{LATET}/bedrock/eu-west-1/profile/work/")
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1" and load_config().upstreams == []  # Amazon's, not a destination
    assert ClaudeCode().status() == "applied" and ClaudeCode().route(load_config()).kind == "bedrock"
    assert ClaudeCode().revert() == "restored"
    assert (home / "claude" / "settings.json").read_text("utf-8") == original


def test_bedrock_through_a_vpc_endpoint_is_refused_for_good_and_the_settings_stay(home):
    original = json.dumps(_bedrock_at("https://vpce-0abc1234.bedrock-runtime.eu-west-1.vpce.amazonaws.com")) + "\n"
    (home / "claude" / "settings.json").write_text(original, "utf-8")
    with pytest.raises(agents_module.RouteProblem, match="VPC endpoint"):
        ClaudeCode().apply(ClaudeOptions(), load_config())
    assert (home / "claude" / "settings.json").read_text("utf-8") == original and "claude" not in load_records()
    assert load_config().upstreams == []  # no destination was made for it


def test_an_address_on_the_internet_over_http_is_said_never_a_crash(home):
    plain = {"env": {"ANTHROPIC_BASE_URL": "http://llm.corp.acme.com"}}
    (home / "claude" / "settings.json").write_text(json.dumps(plain), "utf-8")
    (home / "codex" / "config.toml").write_text(AZURE.replace("https://acme.openai.azure", "http://acme.corp"), "utf-8")
    assert "https" in ClaudeCode().route(load_config()).problem
    assert "https" in Codex().route(load_config(), chatgpt=False).problem
    assert ClaudeCode().status() and Codex().status() == "not applied"
    with pytest.raises(agents_module.RouteProblem):
        Codex().apply(load_config(), chatgpt=False)
    assert load_config().upstreams == []


def test_a_profile_s_address_for_openai_is_changed_in_its_file(home):
    (home / "codex" / "config.toml").write_text("", "utf-8")
    work = 'openai_base_url = "https://gw.acme.com/v1"\n'
    (home / "codex" / "work.config.toml").write_text(work, "utf-8")
    assert Codex().status() == "not applied"
    Codex().apply(load_config(), chatgpt=False)
    assert f'openai_base_url = "{LATET}/gw-acme-com"' in (home / "codex" / "work.config.toml").read_text("utf-8")
    assert Codex().status() == "applied"
    Codex().revert()
    assert (home / "codex" / "work.config.toml").read_text("utf-8") == work


# The real call that asks the daemon to add a company's address: what answers on Latet's port decides
# whether config.yaml is written behind it.
UNAUTHORIZED = (401, b'{"error":"unauthorized"}')  # how a Latet made with another token answers


def _daemon_replies(monkeypatch, answer: tuple[int, bytes] | Exception) -> list[str]:
    """The real call to the daemon, with ``answer`` from whatever listens on Latet's port: a refusal (its
    status and body), or the error of no answer at all. The addresses it was made to."""
    asked: list[str] = []

    def urlopen(request, timeout=None):
        asked.append(request.full_url)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        raise urllib.error.HTTPError(request.full_url, status, HTTPStatus(status).phrase, Message(), io.BytesIO(body))

    monkeypatch.setattr(wrappers, "destination_for", REAL_DESTINATION_FOR)
    monkeypatch.setattr("latet.service.daemon.local_urlopen", urlopen)  # every call of the CLI's to the daemon
    return asked


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (401, b'{"error":"unauthorized"}', "unauthorized"),  # a Latet made with another token
        (500, b'{"error":"disk full"}', "disk full"),  # a Latet that failed
        (403, b"<h1>Forbidden</h1>", "Forbidden"),  # a program that is not Latet, in its own words
    ],
    ids=["unauthorized", "failed", "another-program"],
)
def test_a_daemon_that_answers_and_refuses_is_not_gone_round(home, monkeypatch, status, body, said):
    original = json.dumps(COMPANY, indent=4) + "\n"
    (home / "claude" / "settings.json").write_text(original, "utf-8")
    asked = _daemon_replies(monkeypatch, (status, body))
    with pytest.raises(agents_module.RouteProblem) as refused:
        ClaudeCode().apply(ClaudeOptions(), load_config())
    reason = str(refused.value)
    assert "port 5283" in reason and "https://llm.acme.com" in reason and f"{status}: {said}" in reason
    assert asked == [f"{LATET}/_latet/admin/destinations"]  # the daemon was asked, once
    assert not (home / "config.yaml").exists()  # nothing was written behind the one that answered
    assert (home / "claude" / "settings.json").read_text("utf-8") == original and "claude" not in load_records()


def test_codex_is_left_as_it_is_when_a_daemon_refuses(home, monkeypatch):
    (home / "codex" / "config.toml").write_text(AZURE, "utf-8")
    _daemon_replies(monkeypatch, UNAUTHORIZED)
    with pytest.raises(agents_module.RouteProblem, match=r"refused to add https://acme\.openai\.azure\.com"):
        Codex().apply(load_config(), chatgpt=False)
    assert (home / "codex" / "config.toml").read_text("utf-8") == AZURE and "codex" not in load_records()
    assert not (home / "config.yaml").exists()


def test_agents_apply_says_a_refusal_in_plain_words_and_leaves_the_agent_as_it_is(home, monkeypatch):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    _daemon_replies(monkeypatch, UNAUTHORIZED)
    result = CliRunner().invoke(app, ["agents", "apply", "claude"])
    assert result.exit_code == 0, result.output
    assert "Claude Code: left as it is (what answers on port 5283 refused to add https://llm.acme.com" in result.output
    assert "(401: unauthorized)" in result.output and "`latet doctor`" in result.output
    assert _settings(home) == COMPANY and not (home / "config.yaml").exists()


def test_a_refused_re_apply_leaves_claude_code_as_it_was_through_latet(home, monkeypatch):
    settings, saved = home / "claude" / "settings.json", home / "config.yaml"
    settings.write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())  # no daemon: the address goes to config.yaml
    applied, record, upstreams = settings.read_bytes(), load_records()["claude"], saved.read_bytes()
    _daemon_replies(monkeypatch, UNAUTHORIZED)
    with pytest.raises(agents_module.RouteProblem, match=r"refused to add https://llm\.acme\.com"):
        ClaudeCode().apply(ClaudeOptions(), load_config())
    assert settings.read_bytes() == applied and load_records()["claude"] == record  # not undone
    assert saved.read_bytes() == upstreams and ClaudeCode().status() == "applied"


def test_a_refused_re_apply_leaves_codex_as_it_was_through_latet(home, monkeypatch):
    config_toml, saved = home / "codex" / "config.toml", home / "config.yaml"
    config_toml.write_text(AZURE, "utf-8")
    Codex().apply(load_config(), chatgpt=False)  # no daemon: the address goes to config.yaml
    applied, record, upstreams = config_toml.read_bytes(), load_records()["codex"], saved.read_bytes()
    _daemon_replies(monkeypatch, UNAUTHORIZED)
    with pytest.raises(agents_module.RouteProblem, match=r"refused to add https://acme\.openai\.azure\.com"):
        Codex().apply(load_config(), chatgpt=False)
    assert config_toml.read_bytes() == applied and load_records()["codex"] == record  # not undone
    assert saved.read_bytes() == upstreams and Codex().status() == "applied"


@pytest.mark.parametrize(
    ("managed", "shell", "said"),
    [
        ({"ANTHROPIC_BASE_URL": "https://llm.it.acme.com"}, {}, "managed settings"),  # rolled out since the install
        ({}, {"CLAUDE_CODE_USE_MANTLE": "1"}, "Mantle"),
        ({}, {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1"}, "keep one"),
    ],
    ids=["managed-settings", "mantle", "two-clouds"],
)
def test_a_re_apply_latet_can_no_longer_stand_in_front_of_leaves_claude_code_as_it_was(
    home, monkeypatch, managed, shell, said
):
    settings = home / "claude" / "settings.json"
    settings.write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    applied, record = settings.read_bytes(), load_records()["claude"]
    (home / "managed.json").write_text(json.dumps({"env": managed}), "utf-8")
    for name, value in shell.items():
        monkeypatch.setenv(name, value)
    ran: list[tuple[str, ...]] = []  # what the claude CLI was asked: an undo uninstalls the plugin
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: f"/usr/bin/{name}")
    monkeypatch.setattr(ClaudeCode, "_claude", lambda self, *args: ran.append(args) or {"outcome": "ok"})
    with pytest.raises(agents_module.RouteProblem, match=said):
        ClaudeCode().apply(ClaudeOptions(), load_config())
    assert settings.read_bytes() == applied and load_records()["claude"] == record  # not undone
    assert ran == []  # the plugin stays: it stops the prompts that would go around Latet


@pytest.mark.parametrize(
    ("profile", "said"),
    [
        ("model_provider = \n", "work.config.toml cannot be read"),
        ('[model_providers.lan]\nname = "LAN"\nbase_url = "http://llm.corp.acme.com/v1"\n', "https"),
    ],
    ids=["unreadable", "http"],
)
def test_a_re_apply_latet_can_no_longer_stand_in_front_of_leaves_codex_as_it_was(home, profile, said):
    config_toml = home / "codex" / "config.toml"
    config_toml.write_text(AZURE, "utf-8")
    Codex().apply(load_config(), chatgpt=False)
    applied, record = config_toml.read_bytes(), load_records()["codex"]
    (home / "codex" / "work.config.toml").write_text(profile, "utf-8")  # a profile's file, written since
    with pytest.raises(agents_module.RouteProblem, match=said):
        Codex().apply(load_config(), chatgpt=False)
    assert config_toml.read_bytes() == applied and load_records()["codex"] == record  # not undone


def test_a_refresh_goes_on_past_an_agent_latet_can_no_longer_stand_in_front_of(home, monkeypatch):
    claude, config_toml = home / "claude" / "settings.json", home / "codex" / "config.toml"
    claude.write_text(json.dumps(COMPANY), "utf-8")
    config_toml.write_text(AZURE, "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    Codex().apply(load_config(), chatgpt=False)
    applied = claude.read_bytes()
    monkeypatch.setenv("CLAUDE_CODE_USE_MANTLE", "1")  # Claude Code, refreshed first: refused since
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    config_toml.write_text(AZURE, "utf-8")  # Codex, undone by hand: the refresh routes it again
    result = CliRunner().invoke(app, ["agents", "refresh"])  # what `latet upgrade` runs
    assert result.exit_code == 0, result.output
    assert "Claude Code: left as it is (" in result.output and "Mantle" in result.output
    assert "Codex: refreshed" in result.output
    assert f'base_url = "{LATET}/azure-acme"' in config_toml.read_text("utf-8")
    assert claude.read_bytes() == applied and "claude" in load_records()


def test_a_re_apply_follows_the_address_you_set_since_not_the_one_it_installed(home):
    settings = home / "claude" / "settings.json"
    settings.write_text(json.dumps(COMPANY), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    moved = _settings(home)
    moved["env"]["ANTHROPIC_BASE_URL"] = "https://llm.other.com"  # you moved to another gateway
    settings.write_text(json.dumps(moved), "utf-8")
    ClaudeCode().apply(ClaudeOptions(), load_config())
    assert _settings(home)["env"]["ANTHROPIC_BASE_URL"] == f"{LATET}/llm-other-com"
    assert sorted(d.name for d in load_config().upstreams) == ["llm-acme-com", "llm-other-com"]


def test_an_older_daemon_without_the_route_still_gets_the_address_in_config_yaml(home, monkeypatch):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    asked = _daemon_replies(monkeypatch, (404, b"Not Found"))
    ClaudeCode().apply(ClaudeOptions(), load_config())
    assert asked == [f"{LATET}/_latet/admin/destinations"]
    assert [(d.name, d.url, d.agent) for d in load_config().upstreams] == [
        ("llm-acme-com", "https://llm.acme.com", "claude")
    ]
    assert _settings(home)["env"]["ANTHROPIC_BASE_URL"] == f"{LATET}/llm-acme-com"


@pytest.mark.parametrize(
    "silence", [urllib.error.URLError(ConnectionRefusedError()), TimeoutError()], ids=["refused", "timeout"]
)
def test_no_daemon_that_answers_leaves_the_address_in_config_yaml(home, monkeypatch, silence):
    (home / "claude" / "settings.json").write_text(json.dumps(COMPANY), "utf-8")
    asked = _daemon_replies(monkeypatch, silence)
    ClaudeCode().apply(ClaudeOptions(), load_config())
    assert asked == [f"{LATET}/_latet/admin/destinations"]
    assert [d.name for d in load_config().upstreams] == ["llm-acme-com"]
    assert _settings(home)["env"]["ANTHROPIC_BASE_URL"] == f"{LATET}/llm-acme-com"


@pytest.mark.parametrize(
    ("answer", "status", "reason"),
    [
        (UNAUTHORIZED, 401, "unauthorized"),
        ((502, b"<h1>Bad Gateway</h1>"), 502, "Bad Gateway"),
        (urllib.error.URLError(ConnectionRefusedError()), None, "the daemon did not answer (URLError)"),
        (TimeoutError(), None, "the daemon did not answer (TimeoutError)"),
    ],
    ids=["unauthorized", "bad-gateway", "refused", "timeout"],
)
def test_the_error_says_the_status_the_daemon_refused_with_and_none_when_nothing_answered(
    home, monkeypatch, answer, status, reason
):
    _daemon_replies(monkeypatch, answer)
    with pytest.raises(wrappers.DaemonError) as raised:
        wrappers.destination_for("https://llm.acme.com", "claude")
    assert raised.value.status == status and str(raised.value) == reason
    assert wrappers.DaemonError("the daemon did not answer").status is None
