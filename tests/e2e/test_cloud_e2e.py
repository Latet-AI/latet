"""Bedrock and Vertex end to end: checked, never rewritten; what Latet cannot read never leaves."""

import base64
import hashlib
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.cloud import BedrockUpstream, VertexUpstream
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import BEDROCK_STREAM, FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
CLAUDE_CODE = {"user-agent": "claude-cli/2.1.294 (external, sdk-cli)", "x-claude-code-session-id": "cc-cloud"}
TURN = "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/invoke-with-response-stream"
VERTEX = "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/claude-opus-5-5:streamRawPredict"


def _body(text: str) -> bytes:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": text}],
    }
    return json.dumps(body, separators=(",", ":")).encode()


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "c.yaml"
    )
    rt.gateway.upstreams = [
        BedrockUpstream(runtime=upstream.url, control=upstream.url),
        VertexUpstream(base=upstream.url),
        Upstream("llm-acme-com", "/llm-acme-com", upstream.url),
    ]
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        yield c


def _post(client, path, raw, headers=CLAUDE_CODE):
    return client.post(path, content=raw, headers={**headers, "content-type": "application/json"})


def test_a_value_in_clear_never_leaves_for_bedrock(client, upstream):
    before = len(upstream.received)
    r = _post(client, TURN, _body(f"deploy with {GH_PAT}"))
    assert r.status_code == 400
    answer = r.json()
    assert answer["message"] == answer["error"]["message"] and answer["message"].startswith("Latet: ")
    assert len(upstream.received) == before


def test_bedrock_requests_and_answers_pass_unchanged(client, upstream):
    raw = _body("hello there")
    r = _post(client, TURN, raw)
    assert r.status_code == 200
    assert r.content == BEDROCK_STREAM.read_bytes()
    assert hashlib.sha256(upstream.received[-1]).digest() == hashlib.sha256(raw).digest()
    assert upstream.requests[-1]["path"] == "/model/us.anthropic.claude-opus-5-5/invoke-with-response-stream"


def test_the_model_list_reaches_the_control_plane(client, upstream):
    r = client.get("/bedrock/us-east-1/inference-profiles?type=SYSTEM_DEFINED", headers={"authorization": "Bearer k"})
    assert r.status_code == 200
    assert (upstream.requests[-1]["path"], upstream.requests[-1]["query"]) == (
        "/inference-profiles",
        "type=SYSTEM_DEFINED",
    )


def test_vertex_is_checked_and_passes_unchanged(client, upstream):
    raw = _body("hello vertex")
    assert _post(client, VERTEX, raw).status_code == 200
    assert upstream.received[-1] == raw
    assert upstream.requests[-1]["path"] == VERTEX.removeprefix("/vertex")
    assert _post(client, VERTEX, _body(f"key {GH_PAT}")).status_code == 400


def test_a_token_count_is_read_before_it_leaves(client, upstream):
    def count(text: str) -> bytes:
        inner = base64.b64encode(_body(text)).decode()
        return json.dumps({"input": {"invokeModel": {"body": inner}}}).encode()

    path = "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/count-tokens"
    before = len(upstream.received)
    assert _post(client, path, count(f"key {GH_PAT}")).status_code == 400
    assert _post(client, path, b'{"input": {"invokeModel": {"body": "not base64!"}}}').status_code == 400
    assert len(upstream.received) == before
    clean = count("how many tokens?")
    assert _post(client, path, clean).status_code == 200 and upstream.received[-1] == clean


def test_what_latet_cannot_read_is_refused(client, upstream):
    before = len(upstream.received)
    r = _post(client, "/bedrock/us-east-1/model/x/converse", _body("hello"))
    assert r.status_code == 404
    assert r.json()["message"] == "Latet does not read this kind of request, so it does not send it"  # once
    assert len(upstream.received) == before


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"prompt": f"deploy with {GH_PAT}", "max_gen_len": 10}).encode(),  # another model's body
        json.dumps([{"role": "user", "content": f"deploy with {GH_PAT}"}]).encode(),
        json.dumps({"inputText": f"deploy with {GH_PAT}"}).encode(),
    ],
)
def test_a_body_that_is_not_claude_s_messages_never_leaves(client, upstream, raw):
    before = len(upstream.received)
    assert _post(client, TURN, raw).status_code == 400
    assert _post(client, VERTEX, raw).status_code == 400
    assert len(upstream.received) == before


def test_a_token_count_holds_nothing_beside_the_request_it_counts(client, upstream):
    inner = base64.b64encode(_body("how many tokens?")).decode()
    converse = {"messages": [{"role": "user", "content": [{"text": f"deploy with {GH_PAT}"}]}]}
    path = "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/count-tokens"
    before = len(upstream.received)
    raw = json.dumps({"input": {"invokeModel": {"body": inner}, "converse": converse}}).encode()
    assert _post(client, path, raw).status_code == 400
    assert len(upstream.received) == before


def test_a_gateway_s_bedrock_pass_through_is_checked_not_rewritten(client, upstream):
    other = {"user-agent": "my-script/1.0"}
    path = "/llm-acme-com/model/us.anthropic.claude-opus-5-5/invoke"
    r = _post(client, path, _body(f"key {GH_PAT}"), headers=other)
    assert r.status_code == 400 and "checked" in r.json()["message"]
    raw = _body("hello gateway")
    assert _post(client, path, raw, headers=other).status_code == 200 and upstream.received[-1] == raw


def test_the_record_says_where_a_request_went(client, runtime, upstream):
    _post(client, TURN, _body("hello record"))
    runtime.recorder.drain()
    [last] = runtime.exchanges.list(limit=1)[0]
    assert last["host"] == upstream.url.removeprefix("http://")
    assert last["mode"] == "verify" and last["model"] == "us.anthropic.claude-opus-5-5"
    assert last["agent"] == "claude-code"
