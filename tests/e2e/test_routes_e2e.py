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
CLAUDE_CODE = {"user-agent": "claude-cli/2.1.294 (external, cli)", "x-claude-code-session-id": "cc-routes"}
KEY = {"authorization": "Bearer sk-test"}


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
    return {"model": "m", "stream": True, "messages": [{"role": "user", "content": text}]}


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


def test_the_path_is_forwarded_as_sent(client, upstream):
    r = client.post("/openai/v1/a%2Fb:c%3Ad/responses", json={"model": "m", "input": "hello"}, headers=KEY)
    assert r.status_code == 200
    assert upstream.requests[-1]["path"] == "/v1/a%2Fb:c%3Ad/responses"


def test_routes_change_without_a_restart(client, runtime, upstream):
    assert client.post("/acme/v1/chat/completions", json=_chat("hi"), headers=KEY).status_code == 404
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("acme", "/acme", upstream.url)])
    r = client.post("/acme/v1/chat/completions", json=_chat("hi acme"), headers=KEY)
    assert r.status_code == 200
    assert upstream.requests[-1]["path"] == "/v1/chat/completions"


def test_websocket_routes_change_without_a_restart(client, runtime, upstream):
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("acmews", "/acmews", upstream.url)])
    text = {"type": "input_text", "text": "hello websocket"}
    frame = {"type": "response.create", "model": "x", "input": [{"type": "message", "role": "user", "content": [text]}]}
    headers = {"host": "127.0.0.1:5283", "session-id": "s-routes"}
    with client.websocket_connect("/acmews/backend-api/codex/responses", headers=headers) as ws:
        ws.send_text(json.dumps(frame))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    assert "hello websocket" in upstream.ws_received[-1]


def test_a_block_answers_in_the_shape_of_its_protocol(client, runtime, upstream):
    runtime.gateway.routes.use([*runtime.gateway.upstreams, Upstream("gw", "/gw", upstream.url)])
    before = len(upstream.received)
    body = {"model": "x", "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
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
