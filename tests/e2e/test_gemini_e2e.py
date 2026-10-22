"""Leak canaries through Google's Gemini API end to end: a real gateway, a fake Google. No canary reaches the fake, and
every one comes back to the client restored, on the Gemini API and on Vertex, streamed, whole or as an array; a
conversation sent back leaves as the model wrote it; the API key reaches Google and no record; what Latet does not read
is refused, in Google's words."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.cloud import VertexUpstream
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
IBAN = "IT60 X054 2811 1010 0000 0123 456"
CANARIES = (GH_PAT, EMAIL, IBAN)
PROMPT = f'deploy with key {GH_PAT}, notify {EMAIL}, pay to {IBAN} and quote "done"'
KEY = "AIzaSyFAKE-gemini-0123456789"
STREAM = "/gemini/v1beta/models/gemini-3-pro:streamGenerateContent"
VERTEX = "/vertex/v1beta1/projects/p/locations/us-central1/publishers/google/models/gemini-3-pro:streamGenerateContent"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _routes(upstream) -> list:
    return [Upstream("gemini", "/gemini", upstream.url, refuses_unread=True), VertexUpstream(base=upstream.url)]


@pytest.fixture
def client(upstream):
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(port=5283, upstreams=_routes(upstream), mediator=Mediator(built.engine, built.verifier))
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as c:
        yield c


def _body(*contents: dict) -> dict:
    return {"systemInstruction": {"parts": [{"text": "You are terse."}]}, "contents": list(contents)}


def _user(text: str) -> dict:
    return {"role": "user", "parts": [{"text": text}]}


def _events(raw: str) -> list[dict]:
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def _parts(answers: list[dict]) -> list[dict]:
    return [part for answer in answers for part in answer["candidates"][0]["content"]["parts"]]


def _text(answers: list[dict]) -> str:
    return "".join(part.get("text", "") for part in _parts(answers))


def _call(answers: list[dict]) -> dict:
    return next(part["functionCall"] for part in _parts(answers) if "functionCall" in part)


def _never_sent(upstream) -> None:
    sent = upstream.all_received()
    for canary in CANARIES:
        assert canary not in sent


def test_a_stream_comes_back_restored_and_google_reads_no_canary(client, upstream):
    r = client.post(f"{STREAM}?alt=sse", json=_body(_user(PROMPT)), headers={"x-goog-api-key": KEY})
    assert r.status_code == 200, r.text
    answers = _events(r.text)
    assert _text(answers) == "You said: " + PROMPT
    assert _call(answers)["args"] == {"content": PROMPT}
    assert upstream.requests[-1]["headers"]["x-goog-api-key"] == KEY
    _never_sent(upstream)


def test_a_whole_answer_and_an_array_come_back_restored(client, upstream):
    whole = client.post(STREAM.replace("streamGenerateContent", "generateContent"), json=_body(_user(PROMPT)))
    assert _text([whole.json()]) == "You said: " + PROMPT and _call([whole.json()])["args"] == {"content": PROMPT}
    array = client.post(STREAM, json=_body(_user(PROMPT)))
    assert _text(array.json()) == "You said: " + PROMPT
    _never_sent(upstream)


@pytest.mark.parametrize(
    "path",
    [VERTEX, "/vertex/v1beta1/publishers/google/models/gemini-3-pro:streamGenerateContent"],
)
def test_vertex_comes_back_restored(client, upstream, path):
    r = client.post(f"{path}?alt=sse", json=_body(_user(PROMPT)), headers={"authorization": "Bearer ya29.fake"})
    assert r.status_code == 200, r.text
    assert _text(_events(r.text)) == "You said: " + PROMPT
    assert upstream.requests[-1]["path"] == path.removeprefix("/vertex")
    assert upstream.requests[-1]["headers"]["authorization"] == "Bearer ya29.fake"
    _never_sent(upstream)


def test_a_conversation_sent_back_leaves_as_the_model_wrote_it(client, upstream):
    first = client.post(f"{STREAM}?alt=sse", json=_body(_user(PROMPT)))
    masked_prompt = json.loads(upstream.received[-1])["contents"][0]["parts"][0]["text"]
    answers = _events(first.text)
    turn = {"role": "model", "parts": [{"text": _text(answers)}, {"functionCall": _call(answers)}]}
    result = {"role": "user", "parts": [{"functionResponse": {"name": "write", "response": {"output": "written"}}}]}
    client.post(f"{STREAM}?alt=sse", json=_body(_user(PROMPT), turn, result))
    sent = json.loads(upstream.received[-1])["contents"]
    assert sent[1]["parts"][0]["text"] == "You said: " + masked_prompt
    assert sent[1]["parts"][1]["functionCall"]["args"] == {"content": masked_prompt}
    _never_sent(upstream)


def test_token_counts_and_embeddings_are_masked(client, upstream):
    counted = client.post("/gemini/v1beta/models/g:countTokens", json={"contents": [_user(PROMPT)]})
    assert counted.json() == {"totalTokens": 12}
    embedded = client.post("/gemini/v1beta/models/e:embedContent", json={"content": {"parts": [{"text": PROMPT}]}})
    assert embedded.status_code == 200
    _never_sent(upstream)


def test_what_latet_does_not_read_is_refused_in_googles_words(client, upstream):
    before = len(upstream.received)
    r = client.post("/gemini/v1beta/interactions", json={"model": "gemini-3-pro", "input": PROMPT})
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["code"], error["status"]) == (400, "INVALID_ARGUMENT") and error["message"].startswith("Latet")
    assert len(upstream.received) == before
    listed = client.get("/gemini/v1beta/models", headers={"x-goog-api-key": KEY})
    assert listed.status_code == 200  # no body: nothing to read


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml"
    )
    rt.gateway.upstreams = _routes(upstream)
    rt.gateway.capture_dir = tmp_path / "capture"
    yield rt
    rt.close()


def test_the_key_is_in_no_record(runtime, upstream, tmp_path):
    headers = {"user-agent": "GeminiCLI/0.64.0 (win32; x64)"}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        r = client.post(f"{STREAM}?alt=sse&key={KEY}", json=_body(_user(PROMPT)), headers=headers)
        assert r.status_code == 200
    assert upstream.requests[-1]["query"] == f"alt=sse&key={KEY}"
    runtime.recorder.drain()
    [last] = runtime.exchanges.list(limit=1)[0]
    assert last["agent"] == "gemini-cli" and last["path"] == STREAM
    assert KEY not in json.dumps(last)
    captured = "".join(path.read_text("utf-8") for path in (tmp_path / "capture").iterdir())
    assert captured and KEY not in captured


def test_the_protected_agents_page_knows_gemini_cli(runtime, monkeypatch):
    from .test_dashboard_e2e import API, _login

    monkeypatch.setattr("latet.integrations.agents.ALL", {})
    monkeypatch.setattr(
        "latet.integrations.agents.shutil.which", lambda name: "C:/bin/gemini.cmd" if name == "gemini" else None
    )
    monkeypatch.setattr("latet.integrations.agents._version", lambda exe: "0.64.0")
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, runtime)
        view = client.get(f"{API}/integrations").json()
    [gemini] = [agent for agent in view["agents"] if agent["id"] == "gemini"]
    assert (gemini["name"], gemini["installed"], gemini["version"], gemini["status"]) == (
        "Gemini CLI",
        True,
        "0.64.0",
        "wrapper",
    )
    assert "latet gemini" in gemini["route"]["summary"]
