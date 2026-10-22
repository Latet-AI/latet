"""The local reviewer in the dashboard's API: its settings, Try it, drafts, and what it said of each block."""

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.config import MAX_GUIDANCE, Config, IdentityConfig
from latet.engine.reviewer import ReviewClient, Reviewer
from latet.engine.reviewer import client as client_module
from latet.engine.reviewer import probe as probe_module
from latet.engine.reviewer import reviewer as reviewer_module
from latet.engine.reviewer.prompt import GUIDANCE_INTRO, PROMPT_VERSION, SYSTEM, system_for
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from ..unit.test_reviewer import FakeServer
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}  # what the dashboard sends with every change
PROMPT = "mia moglie Maria ha la chemio giovedì: scrivi al dottor Ferri"


class DraftingServer(FakeServer):
    """Also drafts a recognizer when asked to."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            if body["messages"][0]["content"].startswith("You write recognizers"):
                draft = {
                    "name": "Badge Number",
                    "type": "badge",
                    "description": "Badge numbers: BD, a dash and six digits",
                    "pattern": r"\bBD-\d{6}\b",
                    "examples": {"match": ["badge BD-381904"], "no_match": ["BD-12"]},
                }
                chunk = {"choices": [{"delta": {"content": json.dumps(draft)}}]}
                stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())
        return await super().__call__(request)


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def server(monkeypatch):
    fake = DraftingServer(slots=1)
    real = ReviewClient

    def faked(url, model=None, api_key=None, **kwargs):
        return real(url, model, api_key, transport=httpx.MockTransport(fake), timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr(client_module, "ReviewClient", faked)
    monkeypatch.setattr(probe_module, "ReviewClient", faked)
    monkeypatch.setattr(reviewer_module, "ReviewClient", faked)  # client_for builds them there
    return fake


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream, server):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False), llm_review={"enabled": True, "model": "fake-model"})
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
    key = rt.mediator.reviewer._cache_key
    client = ReviewClient(config.llm_review.url, "fake-model", transport=httpx.MockTransport(server))
    rt.mediator.reviewer = Reviewer(config.llm_review, key, client=client, memory=rt.store)
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        r = c.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
        c.get(f"/_latet/ui/login?code={r.json()['code']}", follow_redirects=False)
        yield c


def test_what_the_reviewer_said_of_a_block_is_in_the_history(client, runtime):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    assert (
        client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk"}).status_code == 200
    )
    runtime.recorder.drain()
    [exchange] = client.get(f"{API}/exchanges").json()["items"]
    detail = client.get(f"{API}/exchanges/{exchange['id']}").json()
    [review] = client.get(f"{API}/reviews", params={"session": exchange["session"]}).json()["items"]
    assert review["inline"] is True and review["failed"] is None
    assert {f["category"] for f in review["findings"]} == {"person", "health"}
    slot = next(s for s in detail["request"]["slots"] if s.get("block") == review["block"])
    masked = slot["masked"]
    for finding in review["findings"]:
        assert finding["masked"] and finding["entity"]
        start, end = finding["at"]
        assert masked[start:end] not in PROMPT  # the surrogate, never the value
    assert "Maria" not in json.dumps(review)


def test_the_settings_try_it_and_a_draft(client, runtime, server):
    config = client.get(f"{API}/config").json()
    assert config["llm_review"]["enabled"] is True and config["llm_review"]["on_failure"] == "stop"
    tried = client.post(f"{API}/reviewer/test", json={"llm_review": {"model": "fake-model"}}, headers=UI).json()
    assert tried["ok"] and tried["model"] == "fake-model" and tried["slots"] == 1
    assert tried["view"]  # what it read
    assert (
        client.post(f"{API}/reviewer/test", json={"llm_review": {"url": "http://10.0.0.1/v1"}}, headers=UI).status_code
        == 400
    )
    drafted = client.post(
        f"{API}/recognizers/draft", json={"description": "badge numbers BD-123456"}, headers=UI
    ).json()
    assert drafted["draft"]["name"] == "badge-number" and drafted["draft"]["type"] == "BADGE"
    assert drafted["report"]["state"] == "active" and not drafted["report"]["errors"]
    # Saving new settings keeps the reviewer when its server is the same.
    reviewer = runtime.mediator.reviewer
    saved = client.put(f"{API}/config", json={"llm_review": {**config["llm_review"], "block": True}}, headers=UI)
    assert saved.status_code == 200 and runtime.mediator.reviewer is reviewer and reviewer.config.block


def test_the_reviewer_s_own_instructions_and_your_guidance(client, runtime, server):
    shown = client.get(f"{API}/reviewer/prompt").json()
    assert shown == {"system": SYSTEM, "intro": GUIDANCE_INTRO, "version": PROMPT_VERSION, "max_guidance": MAX_GUIDANCE}
    # Try it, from a project's page: your guidance (not saved yet), then the project's.
    body = {"llm_review": {"guidance": "Ward names are confidential."}, "project_guidance": "PRJ- codes are projects."}
    assert client.post(f"{API}/reviewer/test", json=body, headers=UI).json()["ok"]
    assert server.systems[-1] == system_for("Ward names are confidential.\n\nPRJ- codes are projects.")
    too_long = {"project_guidance": "x" * (MAX_GUIDANCE + 1)}
    assert client.post(f"{API}/reviewer/test", json=too_long, headers=UI).status_code == 400
    # Saved from the dashboard: it applies at once, and the reviewer stays.
    reviewer = runtime.mediator.reviewer
    settings = client.get(f"{API}/config").json()["llm_review"]
    saved = client.put(f"{API}/config", json={"llm_review": {**settings, "guidance": "Ward names."}}, headers=UI)
    assert saved.status_code == 200 and runtime.mediator.reviewer is reviewer
    assert runtime.mediator.llm_review.guidance == "Ward names."
    assert client.get(f"{API}/config").json()["llm_review"]["guidance"] == "Ward names."


def test_the_key_is_saved_from_the_dashboard_and_never_sent_back(client, runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("LATET_LLM_API_KEY", raising=False)
    assert client.get(f"{API}/reviewer/key").json()["source"] is None
    assert client.put(f"{API}/reviewer/key", json={"key": "sk-dash-1"}).status_code == 403  # the UI header
    assert client.put(f"{API}/reviewer/key", json={"key": "two\nlines"}, headers=UI).status_code == 400
    saved = client.put(f"{API}/reviewer/key", json={"key": "sk-dash-1"}, headers=UI)
    assert saved.status_code == 200 and saved.json()["source"] == "saved" and saved.json()["stored"] == "file"
    assert "sk-dash-1" not in saved.text and "sk-dash-1" not in client.get(f"{API}/reviewer/key").text
    assert (tmp_path / "keys" / "reviewer.key").read_text("utf-8") == "sk-dash-1"
    assert runtime.mediator.reviewer.client.key_id is not None  # asked with the key from now on
    assert client.delete(f"{API}/reviewer/key", headers=UI).json()["source"] is None
    assert runtime.mediator.reviewer.client.key_id is None


def test_a_remote_server_is_tried_only_once_confirmed(client):
    remote = {"url": "http://192.168.1.20:8080/v1", "model": "fake-model"}
    assert client.post(f"{API}/reviewer/test", json={"llm_review": remote}, headers=UI).status_code == 400
    tried = client.post(f"{API}/reviewer/test", json={"llm_review": {**remote, "remote": True}}, headers=UI)
    assert tried.status_code == 200 and tried.json()["ok"]


def test_latet_llm_status_says_when_the_server_does_not_answer(tmp_path, monkeypatch):
    from latet.cli import admin  # noqa: F401 - registers the commands, `llm` among them
    from latet.cli.main import app

    monkeypatch.setattr(probe_module, "ReviewClient", ReviewClient)  # the real one: nothing listens on port 1
    monkeypatch.setattr(reviewer_module, "ReviewClient", ReviewClient)
    path = tmp_path / "config.yaml"
    path.write_text("llm_review:\n  enabled: true\n  url: http://127.0.0.1:1/v1\n", "utf-8")
    monkeypatch.setenv("LATET_CONFIG", str(path))
    result = CliRunner().invoke(app, ["llm", "status"])
    assert result.exit_code == 1
    assert "enabled:     yes" in result.output and "could not be reached" in result.output


def test_a_value_marked_not_sensitive_is_said_so(client, runtime):
    runtime.apply_config(runtime.config.model_copy(update={"allow_values": ["Maria"]}), save=False)
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk"})
    runtime.recorder.drain()
    [exchange] = client.get(f"{API}/exchanges").json()["items"]
    [review] = client.get(f"{API}/reviews", params={"session": exchange["session"]}).json()["items"]
    by_category = {f["category"]: f for f in review["findings"]}
    person = next(f for f in review["findings"] if f["category"] == "person" and not f["masked"])
    assert person["allowed"] is True  # left in clear: you marked it Not sensitive
    assert by_category["health"]["masked"] and not by_category["health"]["allowed"]
