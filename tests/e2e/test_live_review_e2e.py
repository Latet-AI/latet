"""The Live Reviewer end to end: a request through the gateway, and a call of the Claude Code plugin,
wait for you in the dashboard and leave as you said."""

import base64
import contextlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, LiveReview
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI_HEADERS = {"x-latet-ui": "1"}
EMAIL = "mario.rossi@acme.it"
CODENAME = "Fenice"
PROMPT = f"ask {EMAIL} about project {CODENAME} today"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _runtime(tmp_path, monkeypatch, upstream, **settings):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    settings.setdefault("live_review", LiveReview(enabled=True))
    config = Config(identity=IdentityConfig(enabled=False), **settings)
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
    return rt


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream):
    rt = _runtime(tmp_path, monkeypatch, upstream)
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        r = c.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
        c.get(f"/_latet/ui/login?code={r.json()['code']}", follow_redirects=False)
        yield c


def _chat(client, content=PROMPT):
    body = {"model": "m", "messages": [{"role": "user", "content": content}]}
    return client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-test"})


@contextlib.contextmanager
def _sending(client):
    """A thread for calls that wait for approval; whatever still waits at the end is stopped."""
    with ThreadPoolExecutor(1) as pool:
        try:
            yield pool
        finally:
            for batch in client.get(f"{API}/approvals").json()["batches"]:
                client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI_HEADERS)


def _waiting(client, count=1, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batches = client.get(f"{API}/approvals").json()["batches"]
        if len(batches) >= count:
            return batches
        time.sleep(0.05)
    raise AssertionError("nothing came to wait for approval")


def _mask_of(item, value, category):
    start = item["original"].index(value)
    return {"item": 0, "start": start, "end": start + len(value), "category": category}


def test_a_request_waits_and_leaves_with_what_you_masked(client, upstream):
    with _sending(client) as pool:
        sent = pool.submit(_chat, client)
        [batch] = _waiting(client)
        assert batch["agent"] == "other" and not sent.done()
        [item] = batch["items"]
        assert item["original"] == PROMPT and EMAIL not in item["masked"] and CODENAME in item["masked"]
        assert {item["original"][r["start"] : r["end"]] for r in item["replacements"]} == {EMAIL}
        # You can only add masking: what is masked already, or a fake, is refused.
        taken = _mask_of(item, EMAIL, "email")
        refused = client.post(
            f"{API}/approvals/{batch['id']}", json={"action": "approve", "masks": [taken]}, headers=UI_HEADERS
        )
        assert refused.status_code == 422 and not sent.done()
        mask = _mask_of(item, CODENAME, "confidential")
        r = client.post(
            f"{API}/approvals/{batch['id']}", json={"action": "approve", "masks": [mask]}, headers=UI_HEADERS
        )
        assert r.status_code == 200
        response = sent.result(timeout=10)
    assert response.status_code == 200
    assert CODENAME not in upstream.all_received() and EMAIL not in upstream.all_received()
    assert client.get(f"{API}/approvals").json()["batches"] == []
    # Approved once: the same text does not wait again in this conversation, and is sent the same way.
    again = _chat(client)
    assert again.status_code == 200 and CODENAME not in upstream.all_received()


def test_a_request_you_stop_is_not_sent(client, upstream):
    before = upstream.all_received()
    with _sending(client) as pool:
        sent = pool.submit(_chat, client, f"only for you, {CODENAME}")
        [batch] = _waiting(client)
        r = client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI_HEADERS)
        assert r.status_code == 200
        response = sent.result(timeout=10)
    assert response.status_code == 400
    said = "Latet did not send this request: you did not approve it in the Live Reviewer. Nothing was sent"
    assert response.json()["error"]["message"].startswith(said)  # Latet named once
    assert response.json()["error"]["message"].endswith("; run `latet dashboard` to see where it was")
    assert upstream.all_received() == before
    gone = client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI_HEADERS)
    assert gone.status_code == 409


def test_approve_all_and_clean_texts(tmp_path, monkeypatch, upstream):
    rt = _runtime(tmp_path, monkeypatch, upstream, live_review=LiveReview(enabled=True, auto_approve_clean=True))
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            r = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            client.get(f"/_latet/ui/login?code={r.json()['code']}", follow_redirects=False)
            assert _chat(client, "nothing to hide here").status_code == 200  # clean: it does not wait
            with _sending(client) as pool:
                sent = pool.submit(_chat, client)
                _waiting(client)
                r = client.post(f"{API}/approvals/approve-all", headers=UI_HEADERS)
                assert r.json() == {"approved": 1}
                assert sent.result(timeout=10).status_code == 200
    finally:
        rt.close()


def test_the_plugin_polls_until_you_decide(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    items = {"session": "live-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    first = client.post("/_latet/engine/v1/mask", json=items, headers=token).json()
    assert set(first) == {"pending", "viewers"}
    [batch] = _waiting(client)
    assert batch["id"] == first["pending"] and batch["agent"] == "claude-code"
    with _sending(client) as pool:
        polled = pool.submit(
            client.post, "/_latet/engine/v1/approvals/wait", json={"id": first["pending"]}, headers=token
        )
        mask = _mask_of(batch["items"][0], CODENAME, "confidential")
        client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve", "masks": [mask]}, headers=UI_HEADERS)
        [item] = polled.result(timeout=25).json()["items"]
    assert CODENAME not in item["text"] and EMAIL not in item["text"]
    # The final check enforces it: the value in clear in a request is stopped.
    check = {"session": "live-1", "slots": [{"text": f"the {CODENAME} files", "kind": "user_text"}]}
    verified = client.post("/_latet/engine/v1/verify", json=check, headers=token).json()
    assert [leak["type"] for leak in verified["blocking"]] == ["CONFIDENTIAL"]
    # Masked by hand: the vault keeps it, so it is masked wherever it appears from now on.
    later = {"session": "live-1", "items": [{"text": f"{CODENAME} again", "kind": "tool_result"}]}
    pending = client.post("/_latet/engine/v1/mask", json=later, headers=token).json()["pending"]
    [batch] = _waiting(client)
    assert CODENAME not in batch["items"][0]["masked"]
    client.post(f"{API}/approvals/{pending}", json={"action": "reject"}, headers=UI_HEADERS)
    stopped = client.post("/_latet/engine/v1/approvals/wait", json={"id": pending}, headers=token).json()
    assert stopped["items"][0]["blocked"] == "you did not approve it in the Live Reviewer"
    other = {"session": "live-2", "items": [{"text": f"{CODENAME} again", "kind": "tool_result"}]}
    client.post("/_latet/engine/v1/mask", json=other, headers=token)
    [batch] = _waiting(client)
    assert CODENAME not in batch["items"][0]["masked"]
    surrogates = {r["entity"] for r in batch["items"][0]["replacements"]}
    assert len(surrogates) == 1  # the same fake as in the first conversation


def test_off_by_project_and_history_off(tmp_path, monkeypatch, upstream):
    rt = _runtime(tmp_path, monkeypatch, upstream, history="off")
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            r = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            client.get(f"/_latet/ui/login?code={r.json()['code']}", follow_redirects=False)
            token = {"x-latet-token": rt.gateway.bridge_token}
            items = {"session": "live-3", "items": [{"text": PROMPT, "kind": "user_text"}]}
            assert "pending" in client.post("/_latet/engine/v1/mask", json=items, headers=token).json()
            assert client.get(f"{API}/approvals").json()["batches"]  # the queue works with the history off
            unknown = client.post("/_latet/engine/v1/approvals/wait", json={"id": "nope"}, headers=token)
            assert unknown.status_code == 404
    finally:
        rt.close()


def test_the_plugin_gets_a_text_approved_as_it_was(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    items = {"session": "live-4", "items": [{"text": PROMPT, "kind": "user_text"}]}
    pending = client.post("/_latet/engine/v1/mask", json=items, headers=token).json()["pending"]
    approved = client.post(f"{API}/approvals/{pending}", json={"action": "approve"}, headers=UI_HEADERS)
    assert approved.status_code == 200
    answer = client.post("/_latet/engine/v1/approvals/wait", json={"id": pending}, headers=token)
    assert answer.status_code == 200
    [item] = answer.json()["items"]
    assert EMAIL not in item["text"] and CODENAME in item["text"] and "blocked" not in item
