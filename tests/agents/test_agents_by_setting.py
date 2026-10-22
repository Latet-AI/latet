"""AI tools that reach Latet's gateway through a setting of their own, run for real through Latet to a fake provider on
this computer that answers as OpenAI and Anthropic do: the provider reads no secret, and the tool gets them back.
Opt-in, as the other real agents: LATET_AGENT_TESTS=1, with each tool installed (a tool that is not, is skipped). These
are third-party programs that pytest-socket does not fence: each runs with its updates and telemetry off, and a proxy at
a port nothing listens on for everything but 127.0.0.1 (``test_real_agents._env``)."""

import json
import os
import shutil
import subprocess
import threading
import time

import pytest
import uvicorn

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY
from ..e2e.fake_upstream import FakeUpstream
from .test_real_agents import GH_PAT, _env, _free_port

pytestmark = [
    pytest.mark.agents,
    pytest.mark.skipif(os.environ.get("LATET_AGENT_TESTS") != "1", reason="the real agents: LATET_AGENT_TESTS=1"),
]

IBAN = "GB82 WEST 1234 5698 7654 32"
# No email address: Qwen Code reads each @ of a prompt typed to it as a file to attach, and sends `name @domain`. No `;`
# either: Windows' command shell cuts an argument there, on its way through the `.cmd` launcher npm writes for a tool.
PROMPT = f"Pay the invoice to {IBAN} and use the deploy key {GH_PAT}"


@pytest.fixture(scope="module")
def upstream():
    # Answers as OpenAI and Anthropic do, with no tool to run; Junie takes an answer only as a call of its `submit`.
    fake = FakeUpstream(real=True, text_only=True, finish=("submit", "solution_summary")).start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def gateway(upstream):
    port = _free_port()
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=port,
        upstreams=[
            Upstream("openai", "/openai", upstream.url),
            Upstream("anthropic", "/anthropic", upstream.url),
            Upstream("openrouter", "/openrouter/api", upstream.url),  # OpenRouter's API is at /api/v1
        ],
        mediator=Mediator(built.engine, built.verifier),
    )
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def _tool(name: str, title: str) -> str:
    found = shutil.which(name)
    if found is None:
        pytest.skip(f"{title} is not installed")
    return found


def _run(argv, env, cwd) -> subprocess.CompletedProcess[str]:
    """``argv`` run in ``cwd``, its input an empty file: Junie reads what is piped to it, and fails on Windows' NUL. Its
    output is read as UTF-8, a character in another encoding replaced (Junie writes in the console's code page)."""
    empty = cwd / "empty.txt"
    empty.write_bytes(b"")
    with empty.open("rb") as nothing:
        return subprocess.run(argv, env=env, cwd=cwd, stdin=nothing, capture_output=True, text=True, timeout=300,
                              encoding="utf-8", errors="replace")  # fmt: skip


def _assert_masked_and_restored(done, upstream, before: int, ending: str, exited: bool | None = None) -> None:
    """The tool's answer carries the real values, and the provider read none of them. ``exited``: whether the tool
    exited as it should, when its exit code alone does not say so (by default, the code is 0)."""
    exited = done.returncode == 0 if exited is None else exited
    assert exited, done.stdout[-2000:] + done.stderr[-2000:]
    assert "You said:" in done.stdout and GH_PAT in done.stdout and IBAN in done.stdout  # put back on the way in
    sent = [r for r in upstream.requests[before:] if r["path"].endswith(ending)]
    received = "\n".join(b.decode("utf-8", "replace") for b in upstream.received[before:])
    assert sent and GH_PAT not in received and IBAN not in received


@pytest.mark.parametrize(
    ("auth", "route", "ending"),
    [("openai", "OPENAI", "/v1/chat/completions"), ("anthropic", "ANTHROPIC", "/v1/messages")],
)
def test_qwen_code(gateway, upstream, tmp_path, auth, route, ending):
    """Qwen Code's OpenAI provider and its Anthropic one, which sends Claude Code's user agent (`claude-cli/0.22.0`) and
    is masked all the same."""
    qwen = _tool("qwen", "Qwen Code")
    home = _home(tmp_path)
    (home / ".qwen").mkdir()
    settings = {
        "security": {"auth": {"selectedType": auth}},
        "privacy": {"usageStatisticsEnabled": False},
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True},
    }
    (home / ".qwen" / "settings.json").write_text(json.dumps(settings), "utf-8")
    base = f"{gateway}/openai/v1" if auth == "openai" else f"{gateway}/anthropic"
    env = _env(HOME=str(home), USERPROFILE=str(home), **{f"{route}_BASE_URL": base, f"{route}_API_KEY": "sk-test",
               f"{route}_MODEL": "your-model"})  # fmt: skip
    before = len(upstream.requests)
    done = _run([qwen, "-p", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("kind", "route", "wire", "ending"),
    [
        ("openai", "openai/v1", None, "/v1/chat/completions"),
        ("openai", "openai/v1", "responses", "/v1/responses"),
        ("anthropic", "anthropic", None, "/v1/messages"),
    ],
)
def test_copilot_cli(gateway, upstream, tmp_path, kind, route, wire, ending):
    """GitHub Copilot CLI with a provider of your own (BYOK), offline: no GitHub sign-in, no telemetry."""
    copilot = _tool("copilot", "GitHub Copilot CLI")
    home = _home(tmp_path)
    provider = {"COPILOT_PROVIDER_BASE_URL": f"{gateway}/{route}", "COPILOT_PROVIDER_TYPE": kind}
    if wire:
        provider["COPILOT_PROVIDER_WIRE_API"] = wire
    env = _env(HOME=str(home), USERPROFILE=str(home), COPILOT_HOME=str(home / ".copilot"), COPILOT_OFFLINE="true",
               COPILOT_PROVIDER_API_KEY="sk-test", COPILOT_MODEL="your-model", **provider)  # fmt: skip
    before = len(upstream.requests)
    done = _run([copilot, "-p", PROMPT, "--disable-client-telemetry"], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("kind", "route", "ending"),
    [("openai-compat", "openai/v1", "/v1/chat/completions"), ("anthropic", "anthropic", "/v1/messages")],
)
def test_crush(gateway, upstream, tmp_path, kind, route, ending):
    """Crush, with a provider of its `crush.json`: OpenAI-compatible, or Anthropic."""
    crush = _tool("crush", "Crush")
    home = _home(tmp_path)
    model = {"id": "your-model", "name": "Your model", "context_window": 128000, "default_max_tokens": 8000}
    provider = {"type": kind, "base_url": f"{gateway}/{route}", "api_key": "sk-test", "models": [model]}
    chosen = {"model": "your-model", "provider": "latet"}
    config = {
        "options": {"disable_metrics": True, "disable_provider_auto_update": True},
        "providers": {"latet": provider},
        "models": {"large": chosen, "small": chosen},
    }
    (tmp_path / "crush.json").write_text(json.dumps(config), "utf-8")
    env = _env(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"),
               XDG_CONFIG_HOME=str(home / ".config"), CRUSH_DISABLE_METRICS="1", DO_NOT_TRACK="1",
               CRUSH_DISABLE_PROVIDER_AUTO_UPDATE="1")  # fmt: skip
    before = len(upstream.requests)
    done = _run([crush, "run", "--quiet", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("provider", "route", "ending"),
    [("openai", "openai/v1", "/v1/chat/completions"), ("anthropic", "anthropic/v1", "/v1/messages")],
)
def test_continue_cli(gateway, upstream, tmp_path, provider, route, ending):
    """Continue's `cn`, with a model of its `config.yaml` whose `apiBase` is Latet. Its Anthropic provider adds
    `/messages` to the base: the route ends in `/v1`."""
    cn = _tool("cn", "the Continue CLI")
    home = _home(tmp_path)
    config = home / "config.yaml"
    model = {"name": "Your model", "provider": provider, "model": "your-model", "apiBase": f"{gateway}/{route}",
             "apiKey": "sk-test", "roles": ["chat", "edit", "apply"]}  # fmt: skip
    config.write_text(json.dumps({"name": "Latet", "version": "1.0.0", "schema": "v1", "models": [model]}), "utf-8")
    env = _env(HOME=str(home), USERPROFILE=str(home), CONTINUE_GLOBAL_DIR=str(home / ".continue"),
               CONTINUE_CLI_ENABLE_TELEMETRY="0")  # fmt: skip
    before = len(upstream.requests)
    done = _run([cn, "-p", "--config", str(config), PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("provider", "route", "ending"),
    [("openai-compatible", "openai/v1", "/v1/chat/completions"), ("anthropic", "anthropic/v1", "/v1/messages")],
)
def test_cline_cli(gateway, upstream, tmp_path, provider, route, ending):
    """The Cline CLI, its provider's base URL in its settings (`cline auth -b` takes one for OpenAI-compatible providers
    only). Its Anthropic provider adds `/messages` to the base: the route ends in `/v1`."""
    cline = _tool("cline", "the Cline CLI")
    home = _home(tmp_path)
    settings = home / ".cline" / "data" / "settings"
    settings.mkdir(parents=True)
    (settings / "global-settings.json").write_text(json.dumps({"telemetryOptOut": True}), "utf-8")
    chosen = {"provider": provider, "apiKey": "sk-test", "model": "your-model", "baseUrl": f"{gateway}/{route}"}
    entry = {"settings": chosen, "updatedAt": "2026-10-20T00:00:00.000Z", "tokenSource": "manual"}  # all three read
    providers = {"version": 1, "lastUsedProvider": provider, "modes": {}, "providers": {provider: entry}}
    (settings / "providers.json").write_text(json.dumps(providers), "utf-8")
    env = _env(HOME=str(home), USERPROFILE=str(home))
    before = len(upstream.requests)
    argv = [cline, "-P", provider, "-m", "your-model", "--auto-approve", "false", "-t", "120", PROMPT]
    done = _run(argv, env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("package", "route", "ending"),
    [
        ("@ai-sdk/openai-compatible", "openai/v1", "/v1/chat/completions"),
        ("@ai-sdk/anthropic", "anthropic/v1", "/v1/messages"),
    ],
)
def test_kilo_cli(gateway, upstream, tmp_path, package, route, ending):
    """The Kilo CLI, with a provider of its `kilo.json`. Its Anthropic package adds `/messages` to the base: the route
    ends in `/v1`."""
    kilo = _tool("kilo", "the Kilo CLI")
    home = _home(tmp_path)
    model = {"name": "Your model", "limit": {"context": 128000, "output": 16000}}
    provider = {"npm": package, "name": "Latet", "options": {"baseURL": f"{gateway}/{route}", "apiKey": "sk-test"},
                "models": {"your-model": model}}  # fmt: skip
    config = {"disabled_providers": ["kilo"], "provider": {"latet": provider}, "model": "latet/your-model"}
    (tmp_path / "kilo.json").write_text(json.dumps(config), "utf-8")
    xdg = {f"XDG_{name}_HOME": str(home / name.lower()) for name in ("CONFIG", "DATA", "CACHE", "STATE")}
    env = _env(HOME=str(home), USERPROFILE=str(home), KILO_TELEMETRY_LEVEL="off", **xdg)
    before = len(upstream.requests)
    done = _run([kilo, "run", "-m", "latet/your-model", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


@pytest.mark.parametrize(
    ("api", "path", "ending"),
    [
        ("OpenAIResponses", "openai/v1/responses", "/v1/responses"),
        ("OpenAICompletion", "openai/v1/chat/completions", "/v1/chat/completions"),
        ("Anthropic", "anthropic/v1/messages", "/v1/messages"),
    ],
)
def test_junie_cli(gateway, upstream, tmp_path, api, path, ending):
    """JetBrains' Junie CLI, with a custom model profile: its `baseUrl` is the whole address of the call, Junie adds
    nothing to it. No JetBrains account, no statistics."""
    junie = _tool("junie", "the Junie CLI")
    home = _home(tmp_path)
    models = home / ".junie" / "models"
    models.mkdir(parents=True)
    profile = {"id": "your-model", "baseUrl": f"{gateway}/{path}", "apiType": api, "apiKey": "sk-test"}
    (models / "latet.json").write_text(json.dumps(profile), "utf-8")
    env = _env(HOME=str(home), USERPROFILE=str(home), JUNIE_HOME=str(home / ".junie"),
               JUNIE_SHARE_ANONYMOUS_STATISTICS="false")  # fmt: skip
    before = len(upstream.requests)
    argv = [junie, "--skip-update-check", "--share-anonymous-statistics=false", "--model", "custom:latet",
            "--output-format", "json", "--task", PROMPT]  # fmt: skip
    done = _run(argv, env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, ending)


# The autonomous agents, set up as docs/src/content/docs/agents/autonomous-agents.md says: a failure here may be the
# documentation's.


def _windows_home(home) -> dict[str, str]:
    """A home whose Windows folders (AppData) are in it too: the agents below keep their settings there on Windows."""
    return {"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local")}  # fmt: skip


def test_goose(gateway, upstream, tmp_path):
    """Goose's OpenAI provider, from the environment: a host and a path, which it joins."""
    goose = _tool("goose", "Goose")
    home = _home(tmp_path)
    env = _env(**_windows_home(home), OPENAI_HOST=gateway, OPENAI_BASE_PATH="openai/v1/chat/completions",
               OPENAI_API_KEY="sk-test", GOOSE_PROVIDER="openai", GOOSE_MODEL="gpt-5.1",
               GOOSE_TELEMETRY_ENABLED="false", GOOSE_DISABLE_KEYRING="1")  # fmt: skip
    before = len(upstream.requests)
    done = _run([goose, "run", "--no-session", "-q", "-t", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, "/v1/chat/completions")


def test_openhands(gateway, upstream, tmp_path):
    """The OpenHands CLI, its model from the environment (`--override-with-envs`). A model named `gpt-5` speaks the
    Responses API."""
    openhands = _tool("openhands", "the OpenHands CLI")
    home = _home(tmp_path)
    env = _env(**_windows_home(home), LLM_MODEL="openai/gpt-5.1", LLM_BASE_URL=f"{gateway}/openai/v1",
               LLM_API_KEY="sk-test", LITELLM_LOCAL_MODEL_COST_MAP="True",
               PYTHONUTF8="1")  # its summary has an emoji, which Windows' console code page cannot write  # fmt: skip
    before = len(upstream.requests)
    done = _run([openhands, "--headless", "--override-with-envs", "-t", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, "/v1/responses")


def test_hermes_agent(gateway, upstream, tmp_path):
    """Hermes Agent, with a custom provider in its `config.yaml`: in `~/.hermes`, or on Windows in
    `%LOCALAPPDATA%\\hermes`."""
    hermes = _tool("hermes", "Hermes Agent")
    home = _home(tmp_path)
    folder = home / "AppData" / "Local" / "hermes" if os.name == "nt" else home / ".hermes"
    folder.mkdir(parents=True)
    (folder / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {gateway}/openrouter/api/v1\n  key_env: OPENROUTER_API_KEY\n"
        "  default: anthropic/claude-sonnet-4.5\n",
        "utf-8",
    )
    env = _env(**_windows_home(home), OPENROUTER_API_KEY="sk-or-test", PYTHONUTF8="1")
    before = len(upstream.requests)
    done = _run([hermes, "-z", PROMPT], env, tmp_path)
    _assert_masked_and_restored(done, upstream, before, "/v1/chat/completions")


def test_openclaw(gateway, upstream, tmp_path):
    """OpenClaw, with a provider in `~/.openclaw/openclaw.json` (JSON5) and one of its models the default, one task run
    with `agent exec`. On Windows, OpenClaw may fail to delete its own temporary database once the task is done
    (EBUSY): that error alone is not a failure here."""
    openclaw = _tool("openclaw", "OpenClaw")
    home = _home(tmp_path)
    (home / ".openclaw").mkdir()
    (home / ".openclaw" / "openclaw.json").write_text(
        '{\n  agents: { defaults: { model: { primary: "latet/gpt-5.1" } } },\n  models: {\n    providers: {\n'
        f'      latet: {{\n        baseUrl: "{gateway}/openai/v1",\n        apiKey: "${{OPENAI_API_KEY}}",\n'
        '        api: "openai-completions",\n        models: [{ id: "gpt-5.1", name: "GPT-5.1 through Latet" }],\n'
        "      },\n    },\n  },\n}\n",
        "utf-8",
    )
    temp = tmp_path / "temp"
    temp.mkdir()
    env = _env(**_windows_home(home), OPENAI_API_KEY="sk-test", OPENCLAW_NO_AUTO_UPDATE="1", DO_NOT_TRACK="1",
               TEMP=str(temp), TMP=str(temp))  # fmt: skip
    before = len(upstream.requests)
    done = _run([openclaw, "agent", "exec", PROMPT, "--json"], env, tmp_path)
    exited = done.returncode == 0 or (done.returncode == 1 and "Agent exec cleanup failed: EBUSY" in done.stdout)
    _assert_masked_and_restored(done, upstream, before, "/v1/chat/completions", exited=exited)
