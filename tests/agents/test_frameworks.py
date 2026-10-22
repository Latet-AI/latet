"""AutoGen and CrewAI through Latet's gateway to a fake OpenAI on this computer, each in an environment of its own: their
dependencies do not fit the test environment's (CrewAI wants an older OpenAI SDK, AutoGen an older protobuf). Opt-in, as
the other real agents: LATET_AGENT_TESTS=1, with LATET_AUTOGEN_PYTHON and LATET_CREWAI_PYTHON naming an interpreter that
has `autogen-agentchat` and `autogen-ext[openai]`, or `crewai`. Each script runs with what `latet run` sets, no telemetry,
and a proxy at a port nothing listens on for everything but 127.0.0.1 (``test_real_agents._env``)."""

import os
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

AUTOGEN = """
import asyncio, os
from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient

async def main():
    client = OpenAIChatCompletionClient(model="gpt-4o")  # its base URL and key from what `latet run` sets
    result = await AssistantAgent("assistant", model_client=client).run(task=os.environ["PROMPT"])
    print(result.messages[-1].content)
    await client.close()

asyncio.run(main())
"""

CREWAI = """
import os
from crewai import LLM, Agent, Crew, Task

llm = LLM(model="openai/gpt-4o")  # its base URL and key from what `latet run` sets
print("CALL:", llm.call([{"role": "user", "content": os.environ["PROMPT"]}]))
agent = Agent(role="Assistant", goal="Answer the request", backstory="You help a small team.", llm=llm, max_iter=1)
task = Task(description=os.environ["PROMPT"], expected_output="The answer", agent=agent)
print("CREW:", Crew(agents=[agent], tasks=[task]).kickoff().raw)
"""


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream(real=True).start()  # answers as OpenAI does, which the SDKs read
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


def _run(variable: str, script: str, gateway: str, tmp_path) -> str:
    python = os.environ.get(variable)
    if not python:
        pytest.skip(f"{variable} names no interpreter")
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)  # where Windows keeps a program's data (CrewAI's, here)
    (home / "AppData" / "Roaming").mkdir()
    env = _env(
        HOME=str(home),
        USERPROFILE=str(home),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
        APPDATA=str(home / "AppData" / "Roaming"),
        OPENAI_BASE_URL=f"{gateway}/openai/v1",  # what `latet run` sets
        OPENAI_API_BASE=f"{gateway}/openai/v1",
        OPENAI_API_KEY="sk-test",
        PROMPT=PROMPT,
        CREWAI_DISABLE_TELEMETRY="true",
        CREWAI_DISABLE_TRACKING="true",
        OTEL_SDK_DISABLED="true",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        PYTHONIOENCODING="utf-8",
    )
    done = subprocess.run(
        [python, "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=300, encoding="utf-8"
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-3000:]
    return done.stdout


def test_autogen_gets_its_answer_back_and_the_provider_reads_no_secret(gateway, upstream, tmp_path):
    out = _run("LATET_AUTOGEN_PYTHON", AUTOGEN, gateway, tmp_path)
    assert "You said:" in out and EMAIL in out and GH_PAT in out  # put back on the way in
    sent = upstream.all_received()
    assert upstream.requests and EMAIL not in sent and GH_PAT not in sent


def test_crewai_gets_its_answer_back_and_the_provider_reads_no_secret(gateway, upstream, tmp_path):
    out = _run("LATET_CREWAI_PYTHON", CREWAI, gateway, tmp_path)
    call = next(line for line in out.splitlines() if line.startswith("CALL:"))
    assert EMAIL in call and GH_PAT in call  # its LLM, put back on the way in
    assert "CREW:" in out and EMAIL in out.split("CREW:", 1)[1]  # a crew's task, the same
    sent = upstream.all_received()
    assert upstream.requests and EMAIL not in sent and GH_PAT not in sent
