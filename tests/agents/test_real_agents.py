"""The real Claude Code and Codex, through a real Latet, to fake providers on this computer: what they
really send to a company's gateway, to Bedrock (its API key, a profile Latet signs for, a model ARN) and
to Vertex. Opt-in: LATET_AGENT_TESTS=1, with `claude` and `codex` installed. Both are third-party programs
that pytest-socket does not fence: `_env` gives them no update check, no telemetry, and a proxy at a port
nothing listens on for everything but 127.0.0.1."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from latet.config import Config, Destination, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.aws import AwsCredentialStore, profile_tag
from latet.gateway.cloud import BedrockUpstream, VertexUpstream
from latet.integrations.routes import ROUTE_VARS
from latet.runtime import make_runtime
from latet.wrappers import plugin_dir

from ..e2e.fake_upstream import FakeUpstream

pytestmark = [
    pytest.mark.agents,
    pytest.mark.skipif(os.environ.get("LATET_AGENT_TESTS") != "1", reason="the real agents: LATET_AGENT_TESTS=1"),
]

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
_OWN = ("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "LATET_", "OPENAI_", "CODEX_", "CLOUD_ML_")
_PROXIES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")  # this computer's own never reach an agent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fake_aws(argv, **kwargs):
    export = {"Version": 1, "AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "the-secret", "SessionToken": "the-token"}
    return subprocess.CompletedProcess(argv, 0, json.dumps(export), "")


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def daemon(tmp_path_factory, upstream):
    """A real Latet on a free port, every provider and the clouds pointed at the fake."""
    folder = tmp_path_factory.mktemp("agents")
    port = _free_port()
    destinations = [
        Destination(name="acme-claude", url=upstream.url),
        Destination(name="acme-codex", url=f"{upstream.url}/v1"),
    ]
    config = Config(port=port, identity=IdentityConfig(enabled=False), upstreams=destinations)
    rt = make_runtime(config, data=folder / "data", config_file=folder / "config.yaml")
    rt.gateway.upstreams = [
        BedrockUpstream(runtime=upstream.url, control=upstream.url),
        VertexUpstream(base=upstream.url),
    ]
    rt.gateway.aws = AwsCredentialStore(which=lambda name: "aws", run=_fake_aws)
    server = uvicorn.Server(uvicorn.Config(create_app(rt.gateway), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield rt, port
    server.should_exit = True
    thread.join(timeout=10)
    rt.close()


def _env(**extra: str) -> dict[str, str]:
    """What an agent runs with. pytest-socket fences the sockets of this process, not those of the programs it starts, so
    an agent is also told to look for no update and send no telemetry, and every connection it makes but those to
    127.0.0.1 goes to a proxy at a port nothing listens on, where it fails. Nothing of this computer's own proxy
    settings reaches it."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ROUTE_VARS and not k.upper().startswith(_OWN) and k.upper() not in _PROXIES
    }
    env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
    nowhere = f"http://127.0.0.1:{_free_port()}"
    env.update(HTTP_PROXY=nowhere, HTTPS_PROXY=nowhere, NO_PROXY="127.0.0.1")
    env.update(NODE_USE_ENV_PROXY="1")  # Node reads the proxy variables only when told to
    return {**env, **extra}


def _claude(daemon, tmp_path, prompt: str, **route: str) -> subprocess.CompletedProcess[str]:
    rt, port = daemon
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Claude Code is not installed")
    env = _env(
        CLAUDE_CONFIG_DIR=str(tmp_path / "claude"),
        LATET_BRIDGE_URL=f"http://127.0.0.1:{port}",
        LATET_BRIDGE_TOKEN=rt.gateway.bridge_token,
        LATET_AUTOSTART="0",
        ENABLE_TOOL_SEARCH="true",
        **route,
    )
    argv = [claude, "--plugin-dir", str(plugin_dir()), "-p", prompt, "--max-turns", "1"]
    done = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=240, encoding="utf-8")
    rt.recorder.drain()
    return done


def _sent(upstream, ending: str) -> list[dict]:
    return [r for r in upstream.requests if r["path"].endswith(ending)]


def test_claude_code_through_a_company_gateway(daemon, upstream, tmp_path):
    _, port = daemon
    _claude(daemon, tmp_path, f"deploy with {GH_PAT}", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}/acme-claude",
            ANTHROPIC_API_KEY="sk-ant-api03-fake")  # fmt: skip
    assert (sent := _sent(upstream, "/v1/messages"))
    assert GH_PAT not in upstream.all_received()
    # Latet knows Claude Code by what it adds to each request, not by its user agent (another agent sends that too):
    # every request of the real Claude Code carries its session id, so none of them is ever rewritten.
    assert all("x-claude-code-session-id" in r["headers"] for r in sent), [r["headers"].keys() for r in sent]


def test_claude_code_on_bedrock_with_its_api_key(daemon, upstream, tmp_path):
    _, port = daemon
    _claude(daemon, tmp_path, f"deploy with {GH_PAT}", CLAUDE_CODE_USE_BEDROCK="1", AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="fake-bedrock-key",
            ANTHROPIC_BEDROCK_BASE_URL=f"http://127.0.0.1:{port}/bedrock/us-east-1")  # fmt: skip
    [turn, *_] = _sent(upstream, "/invoke-with-response-stream")
    assert turn["headers"]["authorization"] == "Bearer fake-bedrock-key"
    assert GH_PAT not in upstream.all_received()


def test_claude_code_on_bedrock_signed_by_latet(daemon, upstream, tmp_path):
    rt, port = daemon
    route = f"http://127.0.0.1:{port}/bedrock/us-east-1/profile/work/{profile_tag(rt.gateway.bridge_token, 'work')}"
    before = len(upstream.requests)
    _claude(daemon, tmp_path, "hello", CLAUDE_CODE_USE_BEDROCK="1", AWS_REGION="us-east-1",
            CLAUDE_CODE_SKIP_BEDROCK_AUTH="1", ANTHROPIC_BEDROCK_BASE_URL=route)  # fmt: skip
    signed = [r for r in upstream.requests[before:] if r["path"].endswith("/invoke-with-response-stream")]
    assert signed and signed[0]["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/")


def test_a_model_arn_reaches_bedrock_as_sent(daemon, upstream, tmp_path):
    _, port = daemon
    _claude(daemon, tmp_path, "hello", CLAUDE_CODE_USE_BEDROCK="1", AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="fake-bedrock-key", ANTHROPIC_MODEL=ARN, ANTHROPIC_SMALL_FAST_MODEL=ARN,
            ANTHROPIC_BEDROCK_BASE_URL=f"http://127.0.0.1:{port}/bedrock/us-east-1")  # fmt: skip
    assert any("application-inference-profile%2Fabc123/invoke" in r["path"] for r in upstream.requests)


def test_claude_code_on_vertex(daemon, upstream, tmp_path):
    _, port = daemon
    _claude(daemon, tmp_path, f"deploy with {GH_PAT}", CLAUDE_CODE_USE_VERTEX="1", CLAUDE_CODE_SKIP_VERTEX_AUTH="1",
            ANTHROPIC_VERTEX_PROJECT_ID="p", CLOUD_ML_REGION="us-east5",
            ANTHROPIC_VERTEX_BASE_URL=f"http://127.0.0.1:{port}/vertex/v1")  # fmt: skip
    assert any(r["path"].startswith("/v1/projects/p/locations/us-east5/") for r in upstream.requests)
    assert GH_PAT not in upstream.all_received()


def _codex_config(port: int) -> str:
    """Codex's config.toml: Acme, a company provider, here the fake behind Latet on ``port``; and no update check and no
    analytics, which would otherwise reach hosts outside this computer."""
    return (
        'model = "gpt-5.1"\nmodel_provider = "acme"\ncheck_for_update_on_startup = false\n[analytics]\nenabled = false\n'
        '[model_providers.acme]\nname = "Acme"\n'
        f'base_url = "http://127.0.0.1:{port}/acme-codex"\nenv_key = "ACME_KEY"\nwire_api = "responses"\n'
        'http_headers = { "x-fake-text-only" = "1" }\n'  # the fake answers text: no tool call to run again
    )


def test_codex_through_a_company_provider(daemon, upstream, tmp_path):
    _, port = daemon
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("the Codex CLI is not installed")
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(_codex_config(port), "utf-8")
    env = _env(CODEX_HOME=str(home), ACME_KEY="fake-acme-key")
    argv = [codex, "exec", "--skip-git-repo-check", f"email {EMAIL} the invoice"]
    done = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=240, encoding="utf-8")
    assert _sent(upstream, "/v1/responses")
    assert EMAIL not in upstream.all_received()
    assert EMAIL in done.stdout  # the answer, restored on its way back
