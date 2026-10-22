"""The local reviewer shown the images, in the gateway and for the plugin: what it sees and the text read
missed is masked on the image; what it sees and Latet cannot place withholds the image."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.reviewer import ReviewClient, Reviewer
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.vision.masking import ImageMasking

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr
from ..unit.test_review_images import VisionServer
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

TOKEN = "bridge-token"
LINES = ["Ticket 4417: call Giulia Conti", "about the invoice"]


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _client(upstream, server, reader, **review):
    config = Config(llm_review={"enabled": True, "model": "fake-model", **review})
    built = build(config, Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.llm_review = config.llm_review
    client = ReviewClient(config.llm_review.url, "fake-model", transport=httpx.MockTransport(server))
    mediator.reviewer = Reviewer(config.llm_review, b"r" * 32, client=client)
    mediator.vision = ImageMasking(reader)
    gateway = GatewayConfig(
        port=5283,
        upstreams=[Upstream("anthropic", "/anthropic", upstream.url)],
        mediator=mediator,
        bridge_token=TOKEN,
    )
    return TestClient(create_app(gateway), base_url="http://127.0.0.1:5283")


def _send(c, data: str):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": [image]}]}
    return c.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "t"})


def _sent_block(upstream) -> dict:
    return json.loads(upstream.received[-1])["messages"][0]["content"][0]


def test_a_name_the_reviewer_sees_is_covered_on_the_image(upstream):
    reader = FakeOcr()
    data = reader.screenshot(LINES)
    server = VisionServer({"Giulia Conti": "person"})
    with _client(upstream, server, reader, images=True) as c:
        assert _send(c, data).status_code == 200
    [asked] = server.bodies
    assert asked["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    block = _sent_block(upstream)
    assert block["type"] == "image" and block["source"]["data"] != data  # painted over
    assert "Giulia" not in upstream.all_received()


def test_a_value_the_reviewer_sees_and_latet_cannot_place_withholds_the_image(upstream):
    reader = FakeOcr()
    data = reader.screenshot(LINES)
    server = VisionServer({"Paolo Neri": "person"})  # in the image, says the reviewer; not in the text read
    with _client(upstream, server, reader, images=True) as c:
        assert _send(c, data).status_code == 200
        block = _sent_block(upstream)
        assert block["type"] == "text" and "the local reviewer saw a value in it (person)" in block["text"]
        assert _send(c, data).status_code == 200  # sent again: its answer is remembered, withheld again
    assert len(server.bodies) == 1 and _sent_block(upstream)["type"] == "text"


def test_without_images_on_the_reviewer_reads_the_text_alone(upstream):
    reader = FakeOcr()
    data = reader.screenshot(LINES)
    server = VisionServer({"Paolo Neri": "person"})
    with _client(upstream, server, reader) as c:
        assert _send(c, data).status_code == 200
    [asked] = server.bodies
    assert asked["messages"][1]["content"] == "\n".join(LINES)
    assert _sent_block(upstream) == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }  # nothing found in it: sent as it was


def test_the_plugin_gets_the_image_withheld_too(upstream):
    reader = FakeOcr()
    data = reader.screenshot(LINES)
    server = VisionServer({"Paolo Neri": "person"})
    with _client(upstream, server, reader, images=True) as c:
        r = c.post(
            "/_latet/engine/v1/mask",
            json={
                "session": "s1",
                "items": [{"image": {"data": data, "media_type": "image/png"}, "kind": "tool_result"}],
            },
            headers={"x-latet-token": TOKEN},
        )
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert item["image"] is None and "the local reviewer saw a value in it" in item["withheld"]
