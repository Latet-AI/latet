"""Central approvals as whoever waits meets them (Latet Enterprise; its edition a stand-in for a company whose security
team decides): the dashboard refuses the person's own approval and takes their undo; a save in the dashboard that
lowers protection waits, and in an API key's space keeps who made it; the Live Reviewer's texts wait for the team,
through the gateway and through the plugin, which is told; and when the company's record does not answer, nothing
waits and nothing leaves."""

import base64
import contextlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from latet.api.rest import ACTING
from latet.config import Config, IdentityConfig, LiveReview, load_config, save_config
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime
from latet.state import IdentityRef

from ..central import Company, Queue
from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
MARK = IdentityRef("u_markellisa", "Mark Ellis", "person")
PROMPT = "ask emily.carter@acme.com about Project Falcon today"
IMPORT = {"file": "allow_values: [ACME]\n", "apply": True, "all": True}  # a rules file, with what lowers protection


def _acting(app, person_id):
    """The app as E3's API key space hands it a request: with the person working for the key in the scope."""

    async def acting(scope, receive, send):
        await app({**scope, ACTING: person_id} if scope["type"] == "http" else scope, receive, send)

    return acting


@contextlib.contextmanager
def _running(tmp_path, monkeypatch, company, *, raise_server_exceptions=True, acting=None):
    """A daemon with ``company``'s edition, its dashboard signed in; with ``acting``, as an API key's space with that
    person working in it (E3)."""
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True))
    save_config(config, tmp_path / "config.yaml")
    rt = make_runtime(data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=company)
    app = create_app(rt.gateway)
    if acting is not None:
        app = _acting(app, acting)
    try:
        with TestClient(app, base_url="http://127.0.0.1:5283", raise_server_exceptions=raise_server_exceptions) as c:
            code = c.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            c.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
            yield rt, c
    finally:
        rt.close()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    with _running(tmp_path, monkeypatch, Company(Queue(["Daniel Hayes", "Mark Ellis"]), texts="security")) as running:
        yield running


def _chat(client):
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": PROMPT}]}
    return client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-test"})


def _stopped(client, sent, timeout=10.0):
    """Whatever still waits in the Live Reviewer rejected until ``sent``, a request that waits for a decision, is
    answered, for ``timeout`` seconds at most: a test that fails ends instead of holding the request, and the thread
    that sent it, for ever (the review of Task 3, I1). A text has no time limit by default."""
    deadline = time.monotonic() + timeout
    while not sent.done() and time.monotonic() < deadline:
        for left in client.get(f"{API}/approvals").json()["batches"]:
            client.post(f"{API}/approvals/{left['id']}", json={"action": "reject"}, headers=UI)
        time.sleep(0.05)


def test_a_save_in_the_dashboard_that_lowers_protection_waits_for_the_team(daemon):
    rt, client = daemon
    saved = client.put(f"{API}/config", json={"allow_values": ["ACME"]}, headers=UI)
    assert saved.status_code == 409 and "security team" in saved.json()["error"]
    assert saved.json()["reason"] == "settings_waiting_for_others"
    pending = saved.json()["pending"]
    assert pending["source"] == "dashboard" and pending["decides"] == "security"
    assert pending["deciders"] == ["Daniel Hayes", "Mark Ellis"] and rt.config.allow_values == []
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["id"] == pending["id"] and change["decides"] == "security"
    # What adds protection saves at once, as before.
    assert client.put(f"{API}/config", json={"profile": "max"}, headers=UI).status_code == 200


def test_the_person_cannot_approve_it_but_can_undo_it(daemon):
    rt, client = daemon
    pending = client.put(f"{API}/config", json={"allow_values": ["ACME"]}, headers=UI).json()["pending"]
    refused = client.post(f"{API}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403 and "security team" in refused.json()["error"]
    assert rt.config.allow_values == []
    undone = client.post(f"{API}/changes/{pending['id']}", json={"action": "reject"}, headers=UI)
    assert undone.json() == {"ok": True, "state": "rejected"}
    assert load_config().allow_values == []


def test_a_save_in_an_api_key_s_space_keeps_who_made_it_and_they_cannot_approve_it(tmp_path, monkeypatch):
    company = Company(Queue(["Daniel Okafor"]), changes="controllers")
    with _running(tmp_path, monkeypatch, company, acting="u_emilycarte") as (rt, client):
        saved = client.put(f"{API}/config", json={"allow_values": ["ACME"]}, headers=UI)
        assert saved.status_code == 409 and "controls this API key" in saved.json()["error"]
        pending = saved.json()["pending"]
        assert pending["asked_by"] == "u_emilycarte" and pending["deciders"] == ["Daniel Okafor"]
        assert company.queue.askers == ["u_emilycarte"]
        refused = client.post(f"{API}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
        assert refused.status_code == 403 and "Company approvals" in refused.json()["error"]
        assert rt.decide_change(pending["id"], True, by=MARK) == "approved"  # another who controls the key
        assert rt.config.allow_values == ["ACME"]


@pytest.mark.parametrize(
    ("route", "body", "changes", "acting"),
    [
        ("rules/import", IMPORT, "security", None),
        ("rules/import", IMPORT, "controllers", "u_emilycarte"),
        ("allow", {"value": "ACME"}, "controllers", "u_emilycarte"),
    ],
)
def test_an_import_of_rules_and_a_value_let_through_wait_as_a_save_does(
    tmp_path, monkeypatch, route, body, changes, acting
):
    """The review of Task 3, I2: an import of rules with what lowers protection (``all``: chosen in the dashboard),
    which saves apart from the other routes, waits for whoever decides as ``PUT /config`` does, nothing applied; in an
    API key's space it keeps who made it, and so does ``POST /allow``, one of the routes that save through the same
    step as ``PUT /config``."""
    company = Company(Queue(["Daniel Okafor"]), changes=changes)
    with _running(tmp_path, monkeypatch, company, acting=acting) as (rt, client):
        saved = client.post(f"{API}/{route}", json=body, headers=UI)
        assert saved.status_code == 409 and saved.json()["reason"] == "settings_waiting_for_others"
        pending = saved.json()["pending"]
        assert (pending["source"], pending["decides"], pending["asked_by"]) == ("dashboard", changes, acting)
        assert pending["deciders"] == ["Daniel Okafor"] and company.queue.askers == [acting]
        assert rt.config.allow_values == [] and load_config().allow_values == []
        [change] = client.get(f"{API}/changes").json()["changes"]
        assert change["id"] == pending["id"]


def test_the_plugin_is_told_that_the_security_team_reviews_the_text(daemon):
    rt, client = daemon
    token = {"x-latet-token": rt.gateway.bridge_token}
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    first = client.post("/_latet/engine/v1/mask", json=items, headers=token).json()
    assert first["decides"] == "security" and first["deciders"] == ["Daniel Hayes", "Mark Ellis"]
    [batch] = client.get(f"{API}/approvals").json()["batches"]
    assert batch["decides"] == "security"
    refused = client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403
    assert client.post(f"{API}/approvals/approve-all", json={}, headers=UI).json() == {"approved": 0}
    client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI)
    stopped = client.post("/_latet/engine/v1/approvals/wait", json={"id": first["pending"]}, headers=token).json()
    assert stopped["items"][0]["blocked"] == "you did not approve it in the Live Reviewer"


def test_a_request_through_the_gateway_waits_for_the_team(daemon):
    rt, client = daemon
    upstream = FakeUpstream().start()
    try:
        rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
        with ThreadPoolExecutor(1) as pool:
            sent = pool.submit(_chat, client)
            try:
                deadline = time.monotonic() + 10
                while not (batches := client.get(f"{API}/approvals").json()["batches"]):
                    assert time.monotonic() < deadline, "nothing came to wait"
                    time.sleep(0.05)
                [batch] = batches
                assert batch["decides"] == "security" and not sent.done()
                refused = client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
                assert refused.status_code == 403
                client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI)
                assert sent.result(timeout=10).status_code == 400
            finally:
                _stopped(client, sent)
        assert "Falcon" not in upstream.all_received()
    finally:
        upstream.stop()


def test_when_the_company_s_record_does_not_answer_nothing_waits_and_nothing_leaves(tmp_path, monkeypatch):
    company = Company(Queue(broken=True), texts="security")
    upstream = FakeUpstream().start()
    try:
        with _running(tmp_path, monkeypatch, company, raise_server_exceptions=False) as (rt, client):
            saved = client.put(f"{API}/config", json={"allow_values": ["ACME"]}, headers=UI)
            assert saved.status_code == 500 and rt.config.allow_values == []
            assert client.get(f"{API}/changes").json()["changes"] == []
            rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
            assert _chat(client).status_code >= 500
            assert client.get(f"{API}/approvals").json()["batches"] == []
        assert "Falcon" not in upstream.all_received()
    finally:
        upstream.stop()


def test_no_field_of_a_request_names_who_decided(daemon):
    """Task 1's review, C4: who decided is never read from a request (the dashboard's routes pass no ``by``), so
    naming a decider in the body approves nothing; and every other save that waits answers as ``PUT /config`` does,
    with the person's undo still theirs."""
    rt, client = daemon
    waiting = client.post(f"{API}/allow", json={"value": "ACME", "by": MARK.id}, headers=UI)
    assert waiting.status_code == 409 and waiting.json()["reason"] == "settings_waiting_for_others"
    change_id = waiting.json()["pending"]["id"]
    by = {"id": MARK.id, "name": MARK.name, "kind": MARK.kind}
    refused = client.post(f"{API}/changes/{change_id}", json={"action": "approve", "by": by}, headers=UI)
    assert refused.status_code == 403 and rt.config.allow_values == []
    token = {"x-latet-token": rt.gateway.bridge_token}
    items = {"session": "team-2", "items": [{"text": PROMPT, "kind": "user_text"}]}
    first = client.post("/_latet/engine/v1/mask", json=items, headers=token).json()
    named = {"action": "approve", "by": by, "masks": [], "images": []}
    assert client.post(f"{API}/approvals/{first['pending']}", json=named, headers=UI).status_code == 403
    assert [b["id"] for b in client.get(f"{API}/approvals").json()["batches"]] == [first["pending"]]
    assert client.post(f"{API}/changes/{change_id}", json={"action": "reject"}, headers=UI).status_code == 200
