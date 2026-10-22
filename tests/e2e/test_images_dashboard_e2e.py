"""The dashboard's record of images: the image as it was sent, and the text read in it."""

from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime
from latet.vision.masking import ImageMasking

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
EMAIL = "mario.rossi@acme.it"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _runtime(tmp_path, monkeypatch, upstream, history="full"):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False), history=history)
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("anthropic", "/anthropic", upstream.url)]
    reader = FakeOcr()
    rt.mediator.vision = ImageMasking(reader)
    return rt, reader


def _send(client, data: str) -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    body = {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}],
    }
    assert client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "t"}).status_code == 200


def _detail(client, rt) -> dict:
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token}).json()["code"]
    client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
    rt.recorder.drain()
    [summary] = client.get(f"{API}/exchanges").json()["items"]
    return client.get(f"{API}/exchanges/{summary['id']}").json()


def test_the_exchange_shows_the_image_as_sent_and_the_text_read_in_it(tmp_path, monkeypatch, upstream):
    rt, reader = _runtime(tmp_path, monkeypatch, upstream)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            data = reader.screenshot([f"Email: {EMAIL}"])
            _send(client, data)
            detail = _detail(client, rt)
            assert "image:masked" in detail["summary"]["flags"]
            [image] = detail["request"]["images"]
            assert image["state"] == "masked" and image["kept"] and image["path"].endswith(".source.data")
            [text] = [s for s in detail["request"]["slots"] if s["kind"] == "image_text"]
            assert text["block"] == image["block"] and text["path"] == image["path"] + "#text"
            assert EMAIL in text["original"] and EMAIL not in text["masked"]
            sent = client.get(f"{API}/images/{image['id']}")
            assert sent.status_code == 200 and sent.headers["content-type"] == "image/png"
            forwarded = json.loads(upstream.received[-1])["messages"][0]["content"][1]["source"]["data"]
            assert sent.content == base64.b64decode(forwarded)
            assert sent.content != base64.b64decode(data)  # the masked image, never the original
    finally:
        rt.close()


def test_with_metadata_history_no_image_is_kept(tmp_path, monkeypatch, upstream):
    rt, reader = _runtime(tmp_path, monkeypatch, upstream, history="metadata")
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            _send(client, reader.screenshot([f"Email: {EMAIL}"]))
            detail = _detail(client, rt)
            assert "image:masked" in detail["summary"]["flags"] and detail["request"]["images"] == []
    finally:
        rt.close()
