"""The real Gemini CLI through Latet's gateway to a fake Google on this computer, with an API key: its answer comes back
with the originals, and the fake never reads the address or the token. Opt-in, as the other real agents:
LATET_AGENT_TESTS=1, with Gemini CLI at LATET_GEMINI_CLI (its executable; installed for the tests alone, say with `npm
install --prefix <folder> @google/gemini-cli`) or on the PATH. Gemini CLI is a third-party program that pytest-socket
does not fence: it runs with its usage statistics and update checks off in a home of its own, and a proxy at a port
nothing listens on for everything but 127.0.0.1 (``test_real_agents._env``)."""

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
from .test_real_agents import EMAIL, GH_PAT, _env, _free_port

pytestmark = [
    pytest.mark.agents,
    pytest.mark.skipif(os.environ.get("LATET_AGENT_TESTS") != "1", reason="the real agents: LATET_AGENT_TESTS=1"),
]

IBAN = "IT60 X054 2811 1010 0000 0123 456"
PROMPT = f"Write a short welcome email to {EMAIL}; the deploy key is {GH_PAT}; pay to {IBAN}."
# Gemini CLI's own settings for the test: the API key chosen (with a base URL set it would otherwise pick a gateway,
# which its headless mode refuses), no usage statistics, no update check.
SETTINGS = {
    "security": {"auth": {"selectedType": "gemini-api-key"}},
    "privacy": {"usageStatisticsEnabled": False},
    "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
}


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream(real=True).start()  # answers as Google does, which Gemini CLI reads
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def gateway(upstream):
    port = _free_port()
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=port,
        upstreams=[Upstream("gemini", "/gemini", upstream.url, refuses_unread=True)],
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


def test_gemini_cli_gets_its_answer_back_and_the_provider_reads_no_secret(gateway, upstream, tmp_path):
    gemini = os.environ.get("LATET_GEMINI_CLI") or shutil.which("gemini")
    if not gemini:
        pytest.skip("Gemini CLI is not installed")
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(json.dumps(SETTINGS), "utf-8")
    env = {k: v for k, v in _env().items() if not k.upper().startswith(("GEMINI_", "GOOGLE_"))}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        GEMINI_CLI_HOME=str(home),
        GOOGLE_GEMINI_BASE_URL=f"{gateway}/gemini",  # what `latet gemini` sets
        GEMINI_API_KEY="fake-gemini-key",
        GEMINI_TELEMETRY_ENABLED="false",
    )
    argv = [gemini, "-p", PROMPT, "--output-format", "json", "--skip-trust", "-m", "gemini-3-pro"]
    done = subprocess.run(argv, env=env, cwd=home, capture_output=True, text=True, timeout=300, encoding="utf-8")
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    answer = json.loads(done.stdout)["response"]
    assert "You said:" in answer and EMAIL in answer and GH_PAT in answer and IBAN in answer  # put back on the way in
    sent = upstream.all_received()
    assert upstream.requests and EMAIL not in sent and GH_PAT not in sent and IBAN not in sent
    [call, *_] = [r for r in upstream.requests if r["path"].endswith(":streamGenerateContent")]
    assert call["headers"]["x-goog-api-key"] == "fake-gemini-key"
    assert call["headers"]["user-agent"].startswith("GeminiCLI")  # how the dashboard names it
