"""Images in the Live Reviewer: shown with their boxes, rectangles drawn by hand, withheld; and the
same image sent again after a restart comes out as you left it."""

from __future__ import annotations

import base64
import contextlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, LiveReview
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime
from latet.vision.masking import ImageMasking

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
EMAIL = "mario.rossi@acme.it"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@contextlib.contextmanager
def _daemon(tmp_path, monkeypatch, upstream, reader):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("anthropic", "/anthropic", upstream.url)]
    rt.mediator.vision = ImageMasking(reader, rt.store)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
            yield client
    finally:
        rt.close()


def _send(client, data: str):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": [image]}]}
    return client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "t"})


def _waiting(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        batches = client.get(f"{API}/approvals").json()["batches"]
        if batches:
            return batches[0]
        time.sleep(0.05)
    raise AssertionError("nothing came to wait for approval")


def _sent_image(upstream) -> str:
    return json.loads(upstream.received[-1])["messages"][0]["content"][0]["source"]["data"]


def test_an_image_waits_with_its_boxes_and_leaves_with_your_rectangles(tmp_path, monkeypatch, upstream):
    reader = FakeOcr()
    data = reader.screenshot([f"Email: {EMAIL}", "Customer since 2019, VIP"])
    with _daemon(tmp_path, monkeypatch, upstream, reader) as client, ThreadPoolExecutor(1) as pool:
        sent = pool.submit(_send, client, data)
        batch = _waiting(client)
        [item] = batch["items"]
        image = item["image"]
        assert item["kind"] == "image_text" and EMAIL in item["original"]
        assert image["width"] > 0 and len(image["lines"]) == 2 and image["covered"] and not image["withheld"]
        [box] = image["covered"]
        assert item["original"][box["start"] : box["end"]] == EMAIL
        original = client.get(f"{API}/approvals/{batch['id']}/images/0")
        assert original.status_code == 200 and original.content == base64.b64decode(data)
        # A rectangle outside the image is refused; one over the second line is taken.
        bad = {"action": "approve", "images": [{"item": 0, "rects": [[0, 0, 99999, 10]]}]}
        assert client.post(f"{API}/approvals/{batch['id']}", json=bad, headers=UI).status_code == 422
        line = image["lines"][1]["box"]
        rect = [line[0], line[1], line[2], line[3]]
        ok = {"action": "approve", "images": [{"item": 0, "rects": [rect]}]}
        assert client.post(f"{API}/approvals/{batch['id']}", json=ok, headers=UI).status_code == 200
        assert sent.result(timeout=10).status_code == 200
        first = _sent_image(upstream)
    assert first != data and EMAIL not in upstream.all_received()
    # Restarted: the same image, sent again, waits with your rectangle already drawn, and leaves the same.
    with _daemon(tmp_path, monkeypatch, upstream, reader) as client, ThreadPoolExecutor(1) as pool:
        sent = pool.submit(_send, client, data)
        batch = _waiting(client)
        assert batch["items"][0]["image"]["rects"] == [rect]
        assert client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI).status_code == 200
        assert sent.result(timeout=10).status_code == 200
        assert _sent_image(upstream) == first


def test_an_image_you_withhold_is_not_sent(tmp_path, monkeypatch, upstream):
    reader = FakeOcr()
    data = reader.screenshot([f"Email: {EMAIL}"])
    with _daemon(tmp_path, monkeypatch, upstream, reader) as client, ThreadPoolExecutor(1) as pool:
        sent = pool.submit(_send, client, data)
        batch = _waiting(client)
        decision = {"action": "approve", "images": [{"item": 0, "withhold": True}]}
        assert client.post(f"{API}/approvals/{batch['id']}", json=decision, headers=UI).status_code == 200
        assert sent.result(timeout=10).status_code == 200
    block = json.loads(upstream.received[-1])["messages"][0]["content"][0]
    assert block == {"type": "text", "text": "[image withheld by Latet: you withheld it in the Live Reviewer]"}
