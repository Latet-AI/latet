"""Routes end to end: the path as sent, routes changed while running, Latet's own headers kept here."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
# Claude Code's session id is a UUID (crypto.randomUUID()); its model and OpenAI's are documented ids.
CLAUDE_CODE = {
    "user-agent": "claude-cli/2.1.294 (external, cli)",
    "x-claude-code-session-id": "6a2d8f4b-9e1c-4f37-8d6a-1c5b3e9f2a74",
}
KEY = {"authorization": "Bearer sk-test"}
GPT, CLAUDE = "gpt-4.1-mini", "claude-sonnet-4-5"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [
        Upstream("anthropic", "/anthropic", upstream.url),
        Upstream("openai", "/openai", upstream.url),
    ]
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        yield c


def _chat(text: str) -> dict:
    return {"model": GPT, "stream": True, "messages": [{"role": "user", "content": text}]}


def test_latet_headers_stay_on_this_computer(client, upstream, tmp_path):
    root = tmp_path / "acme"
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_text("terms: [{value: Project Heron, type: PROJECT}]\n", "utf-8")
    headers = {**KEY, "x-latet-project": str(root)}
    assert (
        client.post("/openai/v1/chat/completions", json=_chat("status of Project Heron?"), headers=headers).status_code
        == 200
    )
    sent = upstream.requests[-1]
    assert not [name for name in sent["headers"] if name.startswith("x-latet-")]
    assert sent["headers"]["authorization"] == "Bearer sk-test"
    assert "Project Heron" not in upstream.all_received()  # the header still named the project


def test_the_path_is_forwarded_as_sent(client, runtime, upstream):
    """Gemini's encoded colon (a GenAI SDK may send `%3A`) is read as the colon it is, and sent as it was written."""
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("gemini", "/gemini", upstream.url)])
    body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    path = "/gemini/v1beta/models/gemini-2.5-flash%3AgenerateContent"
    r = client.post(path, json=body, headers={"x-goog-api-key": "AIzaSyFAKE-gemini-0123456789"})
    assert r.status_code == 200
    assert upstream.requests[-1]["path"] == "/v1beta/models/gemini-2.5-flash%3AgenerateContent"


def test_routes_change_without_a_restart(client, runtime, upstream):
    assert client.post("/acme/v1/chat/completions", json=_chat("hi"), headers=KEY).status_code == 404
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("acme", "/acme", upstream.url)])
    r = client.post("/acme/v1/chat/completions", json=_chat("hi acme"), headers=KEY)
    assert r.status_code == 200
    assert upstream.requests[-1]["path"] == "/v1/chat/completions"


def test_websocket_routes_change_without_a_restart(client, runtime, upstream):
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("acmews", "/acmews", upstream.url)])
    text = {"type": "input_text", "text": "hello websocket"}
    frame = {
        "type": "response.create",
        "model": "gpt-5.1-codex",
        "input": [{"type": "message", "role": "user", "content": [text]}],
    }
    headers = {"host": "127.0.0.1:5283", "session-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2d01"}  # Codex's UUIDv7
    with client.websocket_connect("/acmews/v1/responses", headers=headers) as ws:  # a destination's Responses route
        ws.send_text(json.dumps(frame))
        while (event := json.loads(ws.receive_text()))["type"] != "response.completed":
            assert event["type"] != "error", event  # an error ends the turn: never wait past it
    assert "hello websocket" in upstream.ws_received[-1]


def test_a_block_answers_in_the_shape_of_its_protocol(client, runtime, upstream):
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("gw", "/gw", upstream.url)])
    before = len(upstream.received)
    body = {"model": CLAUDE, "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
    r = client.post("/gw/v1/messages", json=body, headers=CLAUDE_CODE)
    assert r.status_code == 400
    assert r.json()["type"] == "error" and r.json()["error"]["message"].startswith("Latet: ")
    assert len(upstream.received) == before  # nothing left the machine


def test_a_path_written_with_escapes_is_read_all_the_same(client, upstream):
    """`complet%69ons` is `completions` to any server: Latet reads it so too, and sends it as written."""
    before = len(upstream.received)
    sent = client.post("/openai/v1/chat/complet%69ons", json=_chat(f"deploy with {GH_PAT}"), headers=KEY)
    assert sent.status_code == 200 and len(upstream.received) == before + 1
    assert GH_PAT.encode() not in upstream.received[-1]
    assert upstream.requests[-1]["path"] == "/v1/chat/complet%69ons"
