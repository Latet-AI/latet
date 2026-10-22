"""Requests that carry images, through the gateway."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.vision.policy import ImagePolicy

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

URL = "data:image/png;base64,iVBORw0KGgo="
TOKEN = "bridge-token"
BRIDGE = "/_latet/engine/v1/"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def mediator():
    built = build(Config(), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def _config(upstream, mediator, tutorial: bool = False) -> GatewayConfig:
    return GatewayConfig(
        port=5283,
        upstreams=[
            Upstream("anthropic", "/anthropic", upstream.url),
            Upstream("openai", "/openai", upstream.url),
            Upstream("chatgpt", "/backend-api/codex", upstream.url, keep_prefix=True),
        ],
        mediator=mediator,
        bridge_token=TOKEN,
        tutorial=tutorial,
    )


@pytest.fixture
def client(upstream, mediator):
    with TestClient(create_app(_config(upstream, mediator)), base_url="http://127.0.0.1:5283") as c:
        yield c


def _turn(text: str) -> dict:
    content = [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": URL}]
    return {"type": "response.create", "model": "x", "input": [{"type": "message", "role": "user", "content": content}]}


def test_a_websocket_turn_with_an_image_obeys_images_block(client, upstream, mediator):
    mediator.images = ImagePolicy("block", "setting")
    before = len(upstream.received)
    with client.websocket_connect(
        "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": "s-img"}
    ) as ws:
        ws.send_text(json.dumps(_turn("what is in this screenshot?")))
        error = json.loads(ws.receive_text())
    assert error["type"] == "error" and error["error"]["type"] == "latet_blocked"
    assert "images: block" in error["error"]["message"]
    assert len(upstream.received) == before  # nothing left


@pytest.mark.parametrize(
    ("tutorial", "see"),
    [
        (False, ". Nothing was sent; run `latet dashboard` to see where it was"),
        (True, ". Nothing was sent; see where it was in Latet's pane"),  # `latet dashboard` opens your own
    ],
    ids=["daemon", "tutorial"],
)
def test_a_websocket_turn_stopped_is_told_as_a_request_over_http_is(upstream, mediator, tutorial, see):
    """Codex over its WebSocket is told what an agent over HTTP is: Latet named once, why, that nothing was sent,
    and where to see it."""
    mediator.images = ImagePolicy("block", "setting")
    with TestClient(create_app(_config(upstream, mediator, tutorial)), base_url="http://127.0.0.1:5283") as client:
        body = {"model": "x", "input": _turn("look")["input"]}
        over_http = client.post("/openai/v1/responses", json=body, headers={"authorization": "Bearer test"})
        told = over_http.json()["error"]["message"]
        with client.websocket_connect(
            "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": "s-img-told"}
        ) as ws:
            ws.send_text(json.dumps(_turn("look")))
            error = json.loads(ws.receive_text())
    assert told.startswith("Latet: ") and told.endswith(see) and "images: block" in told
    assert error["error"] == {"type": "latet_blocked", "message": told}


def test_a_websocket_turn_with_an_image_goes_when_images_are_allowed(client, upstream):
    with client.websocket_connect(
        "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": "s-img-ok"}
    ) as ws:
        ws.send_text(json.dumps(_turn("what is in this screenshot?")))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass


def test_http_with_images_block_says_why(client, upstream, mediator):
    mediator.images = ImagePolicy("block", "setting")
    body = {"model": "x", "input": _turn("look")["input"]}
    r = client.post("/openai/v1/responses", json=body, headers={"authorization": "Bearer test"})
    assert r.status_code == 400
    message = r.json()["error"]["message"]
    assert "images: block" in message and "max profile" not in message


# --- images read and masked ---------------------------------------------------------------------------

EMAIL = "mario.rossi@acme.it"


@pytest.fixture
def reader(mediator):
    from latet.vision.masking import ImageMasking

    from ..fake_ocr import FakeOcr

    fake = FakeOcr()
    mediator.vision = ImageMasking(fake)
    return fake


def _anthropic(data: str, text: str = "what does this screenshot say?") -> dict:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    return {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}, image]}],
    }


def _sent(upstream) -> dict:
    return json.loads(upstream.received[-1])


def test_the_text_in_an_image_is_masked_and_painted(client, upstream, reader, mediator):
    data = reader.screenshot([f"Email: {EMAIL}", "Order 4417 shipped"])
    r = client.post("/anthropic/v1/messages", json=_anthropic(data), headers={"x-api-key": "test"})
    assert r.status_code == 200
    sent = _sent(upstream)["messages"][0]["content"][1]["source"]
    assert sent["data"] != data and sent["media_type"] == "image/png"  # painted over
    assert EMAIL not in upstream.all_received()
    entity = mediator.engine.vault.by_original(EMAIL)  # in the vault: the answer restores its fake
    assert entity is not None and entity.surrogate not in data


def test_an_image_with_nothing_sensitive_goes_byte_for_byte(client, upstream, reader):
    data = reader.screenshot(["Order 4417 shipped", "All tests passed"])
    client.post("/anthropic/v1/messages", json=_anthropic(data), headers={"x-api-key": "test"})
    assert _sent(upstream)["messages"][0]["content"][1]["source"]["data"] == data


def test_the_same_image_comes_out_the_same_in_every_request(client, upstream, reader):
    data = reader.screenshot([f"Email: {EMAIL}"])
    body = _anthropic(data)
    client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    first = _sent(upstream)["messages"][0]["content"][1]["source"]["data"]
    client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert _sent(upstream)["messages"][0]["content"][1]["source"]["data"] == first
    assert reader.reads == 1  # read once


def test_an_image_that_cannot_be_read_is_withheld(client, upstream, reader):
    reader.fail = True
    data = reader.screenshot([f"Email: {EMAIL}"])
    r = client.post("/anthropic/v1/messages", json=_anthropic(data), headers={"x-api-key": "test"})
    assert r.status_code == 200
    block = _sent(upstream)["messages"][0]["content"][1]
    assert block["type"] == "text" and block["text"].startswith("[image withheld by Latet")
    assert data not in upstream.all_received()


def test_images_mask_refuses_what_it_cannot_read(client, upstream, mediator):
    mediator.images = ImagePolicy("mask", "setting")  # and no text recognition installed
    body = _anthropic("iVBORw0KGgo=")
    before = len(upstream.received)
    r = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 400 and len(upstream.received) == before
    assert "images: mask is set" in r.text and "latet ocr install" in r.text


def test_chat_and_responses_images_are_masked_too(client, upstream, reader):
    data = reader.screenshot([f"Email: {EMAIL}"])
    url = f"data:image/png;base64,{data}"
    chat = {"model": "x", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    client.post("/openai/v1/chat/completions", json=chat, headers={"authorization": "Bearer t"})
    assert _sent(upstream)["messages"][0]["content"][0]["image_url"]["url"] != url
    responses = {"model": "x", "input": [{"role": "user", "content": [{"type": "input_image", "image_url": url}]}]}
    client.post("/openai/v1/responses", json=responses, headers={"authorization": "Bearer t"})
    assert _sent(upstream)["input"][0]["content"][0]["image_url"] != url
    assert EMAIL not in upstream.all_received()


def test_a_websocket_turn_has_its_image_masked(client, upstream, reader):
    data = reader.screenshot([f"Email: {EMAIL}"])
    url = f"data:image/png;base64,{data}"
    turn = {
        "type": "response.create",
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": url}]}],
    }
    with client.websocket_connect(
        "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": "s-img-mask"}
    ) as ws:
        ws.send_text(json.dumps(turn))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    assert url not in upstream.ws_received[-1] and EMAIL not in upstream.all_received()


def test_claude_code_images_are_checked_not_changed(client, upstream, reader):
    headers = {
        "user-agent": "claude-cli/2.1.294 (external, cli)",
        "x-claude-code-session-id": "cc-img",
        "x-api-key": "t",
    }
    clean = reader.screenshot(["Order 4417 shipped"])
    r = client.post("/anthropic/v1/messages", json=_anthropic(clean), headers=headers)
    assert r.status_code == 200 and _sent(upstream)["messages"][0]["content"][1]["source"]["data"] == clean
    leaking = reader.screenshot([f"Email: {EMAIL}"])
    before = len(upstream.received)
    r = client.post("/anthropic/v1/messages", json=_anthropic(leaking), headers=headers)
    assert r.status_code == 400 and "EMAIL" in r.json()["error"]["message"] and len(upstream.received) == before


# --- the Claude Code plugin's images -------------------------------------------------------------------


def _bridge(client, path: str, body: dict) -> dict:
    r = client.post(BRIDGE + path, json={"session": "cc-plugin", **body}, headers={"x-latet-token": TOKEN})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_plugin_gets_its_image_back_masked_and_the_proxy_lets_it_through(client, upstream, reader):
    data = reader.screenshot([f"Email: {EMAIL}", "Order 4417 shipped"])
    [item] = _bridge(
        client,
        "mask",
        {"items": [{"image": {"data": data, "media_type": "image/png"}, "kind": "tool_result", "tool": "Read"}]},
    )["items"]
    assert item["count"] == 1 and item["image"]["data"] != data and "text" not in item
    # Claude Code then sends the masked image: the proxy knows it, reads nothing again, lets it go.
    headers = {
        "user-agent": "claude-cli/2.1.294 (external, cli)",
        "x-claude-code-session-id": "cc-plugin",
        "x-api-key": "t",
    }
    reads = reader.reads
    r = client.post("/anthropic/v1/messages", json=_anthropic(item["image"]["data"]), headers=headers)
    assert r.status_code == 200 and reader.reads == reads


def test_the_plugin_is_told_when_an_image_is_withheld(client, reader):
    reader.fail = True
    data = reader.screenshot([f"Email: {EMAIL}"])
    [item] = _bridge(client, "mask", {"items": [{"image": {"data": data, "media_type": "image/png"}}]})["items"]
    assert item["image"] is None and "could not be read" in item["withheld"]


def test_without_text_recognition_the_plugin_gets_the_image_as_it_was(client):
    [item] = _bridge(client, "mask", {"items": [{"image": {"data": "iVBORw0KGgo=", "media_type": "image/png"}}]})[
        "items"
    ]
    assert item == {"image": {"data": "iVBORw0KGgo=", "media_type": "image/png"}, "count": 0, "unread": True}


def test_the_final_check_reads_the_images_too(client, reader):
    clean = reader.screenshot(["Order 4417 shipped"])
    verdict = _bridge(client, "verify", {"slots": [], "images": [{"data": clean, "media_type": "image/png"}]})
    assert verdict["blocking"] == []
    leaking = reader.screenshot([f"Email: {EMAIL}"])
    verdict = _bridge(client, "verify", {"slots": [], "images": [{"data": leaking, "media_type": "image/png"}]})
    assert [b["type"] for b in verdict["blocking"]] == ["EMAIL"] and verdict["blocking"][0]["kind"] == "image_text"
