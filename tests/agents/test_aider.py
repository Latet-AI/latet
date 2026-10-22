"""The real Aider through Latet's gateway to a fake OpenAI on this computer: what "Scripts and SDKs" promises for a tool
built on the OpenAI API (through LiteLLM). Opt-in, as the other real agents: LATET_AGENT_TESTS=1, with `aider` installed.
Aider is a third-party program that pytest-socket does not fence: it runs with no update check, no analytics, no model
list fetched, and a proxy at a port nothing listens on for everything but 127.0.0.1 (``test_real_agents._env``)."""

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
from .test_real_agents import EMAIL, GH_PAT, _env, _free_port

pytestmark = [
    pytest.mark.agents,
    pytest.mark.skipif(os.environ.get("LATET_AGENT_TESTS") != "1", reason="the real agents: LATET_AGENT_TESTS=1"),
]

PROMPT = f"Write a short welcome email to {EMAIL}; the deploy key is {GH_PAT}."


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream(real=True).start()  # answers as OpenAI does, which LiteLLM reads
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def gateway(upstream):
    port = _free_port()
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=port,
        upstreams=[Upstream("openai", "/openai", upstream.url)],
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


def test_aider_gets_its_answer_back_and_the_provider_reads_no_secret(gateway, upstream, tmp_path):
    aider = shutil.which("aider")
    if aider is None:
        pytest.skip("Aider is not installed")
    home = tmp_path / "home"
    home.mkdir()
    env = _env(
        HOME=str(home),
        USERPROFILE=str(home),
        OPENAI_API_BASE=f"{gateway}/openai/v1",  # what `latet run` sets
        OPENAI_API_KEY="sk-test",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        AIDER_ANALYTICS="false",
        AIDER_CHECK_UPDATE="false",
    )
    argv = [
        aider, "--model", "openai/your-model", "--message", PROMPT, "--no-git", "--yes-always", "--no-pretty",
        "--no-show-model-warnings", "--no-check-update", "--analytics-disable", "--map-tokens", "0",
        "--no-show-release-notes", "--no-detect-urls",
    ]  # fmt: skip
    done = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=300, encoding="utf-8")
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "You said:" in done.stdout and EMAIL in done.stdout and GH_PAT in done.stdout  # put back on the way in
    sent = upstream.all_received()
    assert upstream.requests and EMAIL not in sent and GH_PAT not in sent
