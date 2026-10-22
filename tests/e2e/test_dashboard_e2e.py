"""Dashboard end to end: traffic through the real gateway is recorded, and the API shows what left
(masked), what it was (rebuilt from the vault) and what came back."""

import base64
import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from latet.config import AutoModeReview, Config, IdentityConfig, load_config
from latet.engine.detectors import checksums
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI_HEADERS = {"x-latet-ui": "1"}
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
PROMPT = f"deploy with key {GH_PAT} and notify {EMAIL}"
CLAUDE = {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": "cc-dash"}
SUBSCRIPTION = {"authorization": "Bearer sk-ant-oat01-test"}  # Claude Code signed in with Pro or Max: only checked


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
        Upstream("chatgpt", "/backend-api/codex", upstream.url, keep_prefix=True),
    ]
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        yield c


def _login(client, runtime):
    r = client.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
    assert r.status_code == 200
    r = client.get(f"/_latet/ui/login?code={r.json()['code']}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/_latet/ui/"
    return r


def _chat(client, content=PROMPT, history=()):
    body = {"model": "m", "stream": True, "messages": [*history, {"role": "user", "content": content}]}
    r = client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-test"})
    assert r.status_code == 200
    return r


def _exchanges(client, runtime, **params):
    runtime.recorder.drain()
    r = client.get(f"{API}/exchanges", params=params)
    assert r.status_code == 200
    return r.json()["items"]


def test_rewrite_exchange_shows_masked_original_and_response(client, runtime):
    _chat(client)
    _login(client, runtime)
    [summary] = _exchanges(client, runtime)
    assert summary["protocol"] == "chat"
    assert summary["mode"] == "rewrite"
    assert summary["model"] == "m"
    assert summary["masked"] == summary["new_masked"] >= 2
    assert {"EMAIL"} <= set(summary["by_type"])
    assert summary["by_class"]["PII"] >= 1 and summary["by_class"]["SECRET"] >= 1
    assert summary["by_module"]["secrets"] >= 1 and summary["by_module"]["personal"] >= 1
    assert summary["restored"] >= 2
    assert summary["verified"] is True
    assert summary["overhead_ms"] is not None

    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    [slot] = detail["request"]["slots"]
    assert slot["path"] == "$.messages[0].content"
    assert GH_PAT not in slot["masked"] and EMAIL not in slot["masked"]
    assert slot["original"] == PROMPT  # rebuilt from the vault
    originals = {slot["original"][r["start"] : r["end"]] for r in slot["replacements"]}
    assert {GH_PAT, EMAIL} <= originals
    found = {(r["rule"], r["module"], r["via"]) for r in slot["replacements"]}
    assert {("gitleaks:github-pat", "secrets", "detector"), ("pii:email", "personal", "detector")} <= found
    for r in slot["replacements"]:
        assert slot["masked"][r["masked_start"] : r["masked_end"]] != slot["original"][r["start"] : r["end"]]
    headers = dict(detail["request"]["headers"])
    assert headers["authorization"].startswith("<redacted")
    response = detail["response"]
    assert GH_PAT not in response["text_masked"]
    assert response["text_restored"] == "You said: " + PROMPT
    restored = {response["text_restored"][s["start"] : s["end"]] for s in response["text_spans"]}
    assert {GH_PAT, EMAIL} <= restored  # spans line the answer up on both sides
    # Restored values say so, and still name the rule that masked them.
    assert {s["via"] for s in response["text_spans"]} == {"restore"}
    assert {"gitleaks:github-pat", "pii:email"} <= {s["rule"] for s in response["text_spans"]}
    [call] = response["tool_calls"]
    assert json.loads(call["args_restored"]) == {"content": PROMPT}
    assert GH_PAT not in call["args_masked"] and call["spans"]


def test_history_is_stored_once_and_marked_not_new(client, runtime):
    _chat(client)
    _chat(client, "and now?", history=[{"role": "user", "content": PROMPT}, {"role": "assistant", "content": "ok"}])
    _login(client, runtime)
    second, first = _exchanges(client, runtime)
    assert first["new_masked"] >= 2
    assert second["masked"] >= 2 and second["new_masked"] == 0  # the history left already
    slots = client.get(f"{API}/exchanges/{second['id']}").json()["request"]["slots"]
    assert [s["new"] for s in slots] == [False, True, True]
    assert slots[0]["original"] == PROMPT


def test_forgetting_a_value_removes_it_from_history(client, runtime):
    _chat(client)
    _login(client, runtime)
    [summary] = _exchanges(client, runtime)
    [entity] = [e for e in client.get(f"{API}/vault", params={"q": "mario"}).json()["items"] if e["type"] == "EMAIL"]
    assert "original" not in entity
    revealed = client.post(f"{API}/vault/{entity['id']}/reveal", headers=UI_HEADERS).json()
    assert revealed == {"original": EMAIL}
    assert client.delete(f"{API}/vault/{entity['id']}", headers=UI_HEADERS).json() == {"ok": True}
    slot = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"][0]
    assert EMAIL not in slot["original"] and GH_PAT in slot["original"]
    assert any(r["known"] is False for r in slot["replacements"])


def test_access_needs_a_login_and_the_ui_header(client, runtime):
    assert client.get(f"{API}/exchanges").status_code == 401
    assert client.post("/_latet/admin/login-code").status_code == 401
    r = client.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
    code = r.json()["code"]
    assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).headers["location"] == "/_latet/ui/"
    reused = client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
    assert reused.headers["location"].endswith("login=expired")  # a code works once
    assert client.get(f"{API}/me").json()["profile"] == "balanced"
    assert client.post(f"{API}/allow", json={"value": "x"}).status_code == 403  # no X-Latet-UI
    foreign = {**UI_HEADERS, "origin": "http://evil.example"}
    assert client.post(f"{API}/allow", json={"value": "x"}, headers=foreign).status_code == 403
    client.cookies.clear()
    assert client.get(f"{API}/me").status_code == 401


def test_signing_out_ends_the_session_and_takes_its_cookie_away(client, runtime):
    _login(client, runtime)
    sid = client.cookies.get("latet_session")
    assert client.post(f"{API}/logout").status_code == 403  # no X-Latet-UI: no page of another origin signs you out
    out = client.post(f"{API}/logout", headers=UI_HEADERS)
    assert out.status_code == 200 and out.json() == {"ok": True}
    cookie = out.headers["set-cookie"].lower()
    assert "latet_session=" in cookie and "max-age=0" in cookie and "secure" not in cookie  # http on a computer
    assert client.get(f"{API}/me").status_code == 401
    client.cookies.set("latet_session", sid, domain="127.0.0.1", path="/_latet")  # a copy of the cookie
    assert client.get(f"{API}/me").status_code == 401


def test_rules_apply_at_once_and_playground_uses_them(client, runtime, upstream):
    _login(client, runtime)
    r = client.put(
        f"{API}/config", json={"terms": [{"value": "Project Falcon", "type": "PROJECT"}]}, headers=UI_HEADERS
    )
    assert r.json()["ok"] is True
    assert load_config(runtime.config_file).terms[0].value == "Project Falcon"  # saved
    assert client.get(f"{API}/config").json()["terms"][0]["value"] == "Project Falcon"
    _chat(client, "status of Project Falcon?")
    assert "Project Falcon" not in upstream.all_received()
    played = client.post(f"{API}/playground", json={"text": "Project Falcon ships"}, headers=UI_HEADERS).json()
    assert "Project Falcon" not in played["masked"]
    [r] = played["replacements"]
    assert (r["start"], r["end"], r["type"]) == (0, len("Project Falcon"), "PROJECT")
    bad = client.put(f"{API}/config", json={"profile": "nope"}, headers=UI_HEADERS)
    assert bad.status_code == 400


def test_the_playground_tries_a_text_with_another_profile_and_changes_no_rule(client, runtime):
    """The same text as Secrets only, Balanced and Max would mask it, your rules left as they are."""
    _login(client, runtime)

    def played(text, kind="user_text", **extra):
        body = {"text": text, "kind": kind, **extra}
        return client.post(f"{API}/playground", json=body, headers=UI_HEADERS)

    mail = "write to ann.ross@acme.com"
    assert "ann.ross@acme.com" in played(mail, profile="secrets").json()["masked"]
    assert "ann.ross@acme.com" not in played(mail, profile="balanced").json()["masked"]
    table = "status,code\nSHIPPED,XQ-4411\n"
    assert "SHIPPED" in played(table, "tool_result", profile="balanced").json()["masked"]
    assert "SHIPPED" not in played(table, "tool_result", profile="max").json()["masked"]  # strict data mode
    assert "SHIPPED" in played(table, "tool_result").json()["masked"]  # yours: balanced
    assert played(mail, profile="nope").status_code == 400
    assert client.get(f"{API}/config").json()["profile"] == "balanced"


def test_a_term_of_signs_alone_is_refused_and_said_why(client, runtime, tmp_path):
    _login(client, runtime)
    bad = client.put(f"{API}/config", json={"terms": [{"value": "Falcon"}, {"value": "--"}]}, headers=UI_HEADERS)
    [detail] = bad.json()["details"]
    assert bad.status_code == 400 and detail["loc"] == ["terms", 1, "value"] and "a letter or a digit" in detail["msg"]
    always = client.post(f"{API}/terms", json={"value": "###"}, headers=UI_HEADERS)
    assert always.status_code == 400 and "a letter or a digit" in always.json()["error"]
    assert runtime.config.terms == [] and load_config(runtime.config_file).terms == []
    project = client.post(f"{API}/projects", json={"path": str(_project(tmp_path, ""))}, headers=UI_HEADERS).json()
    mine = client.put(f"{API}/projects/{project['id']}/personal", json={"terms": [{"value": "+"}]}, headers=UI_HEADERS)
    assert mine.status_code == 400 and mine.json()["details"][0]["loc"] == ["terms", 0, "value"]
    shared = {"settings": {"terms": [{"value": "~~"}]}, "tag": project["repo"]["tag"]}
    repo = client.put(f"{API}/projects/{project['id']}/repo", json=shared, headers=UI_HEADERS)
    assert repo.status_code == 400 and repo.json()["details"][0]["loc"] == ["settings", "terms", 0, "value"]


def test_a_term_of_signs_alone_already_in_config_yaml_stops_no_request(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    path = tmp_path / "config.yaml"
    path.write_text("identity: {enabled: false}\nterms:\n  - value: '--'\n  - value: Project Falcon\n", "utf-8")
    rt = make_runtime(load_config(path), data=tmp_path / "data", config_file=path)
    rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            _chat(client, "run it -- for Project Falcon")
            sent = upstream.received[-1].decode()
            assert "run it -- for " in sent and "Project Falcon" not in sent
            # Kept as it is: saving the rules with it still there works, and only a new one is refused.
            _login(client, rt)
            terms = [{"value": "--"}, {"value": "Project Falcon"}, {"value": "Banca Ligure"}]
            assert client.put(f"{API}/config", json={"terms": terms}, headers=UI_HEADERS).json()["ok"] is True
            more = client.put(f"{API}/config", json={"terms": [*terms, {"value": "###"}]}, headers=UI_HEADERS)
            assert more.status_code == 400 and more.json()["details"][0]["loc"] == ["terms", 3, "value"]
    finally:
        rt.close()


def test_claude_code_exchange_rebuilt_from_the_plugin_session(client, runtime, upstream):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    masked = client.post(
        "/_latet/engine/v1/mask", json={"session": "cc-dash", "items": [{"text": PROMPT}]}, headers=token
    ).json()["items"][0]["text"]
    assert EMAIL not in masked
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": masked}]}
    assert client.post("/anthropic/v1/messages", json=body, headers=CLAUDE).status_code == 200
    _login(client, runtime)
    [summary] = _exchanges(client, runtime)
    assert (summary["mode"], summary["agent"], summary["session"]) == ("verify", "claude-code", "cc:cc-dash")
    assert summary["masked"] >= 2
    slot = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"][0]
    assert slot["masked"] == masked
    assert slot["original"] == PROMPT
    # Masked by the plugin through the bridge: the rule that found each value is known all the same.
    assert {r["via"] for r in slot["replacements"]} == {"plugin"}
    assert {"gitleaks:github-pat", "pii:email"} <= {r["rule"] for r in slot["replacements"]}
    assert summary["by_module"]["secrets"] >= 1 and summary["by_module"]["personal"] >= 1


def test_blocked_request_is_recorded_with_its_leak(client, runtime):
    body = {"model": "m", "messages": [{"role": "user", "content": PROMPT}]}
    assert client.post("/anthropic/v1/messages", json=body, headers=CLAUDE).status_code == 400
    _login(client, runtime)
    [summary] = _exchanges(client, runtime, flag="blocked")
    assert summary["status"] == 400 and summary["verified"] is False
    assert "would leave in clear" in summary["block_reason"] and GH_PAT not in summary["block_reason"]
    assert "leak" in summary["flags"]
    leaks = client.get(f"{API}/exchanges/{summary['id']}").json()["leaks"]
    assert leaks and all(GH_PAT not in json.dumps(leak) for leak in leaks)
    assert _exchanges(client, runtime, flag="uninspected") == []


def _stop(client, content, headers=SUBSCRIPTION):
    body = {"model": "m", "messages": [{"role": "user", "content": content}]}
    return client.post("/anthropic/v1/messages", json=body, headers=headers)


def _stopped(client, runtime):
    """The detail of the one request that was stopped, and where in it the value that stopped it is."""
    _login(client, runtime)
    [summary] = _exchanges(client, runtime, flag="blocked")
    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    [leak] = detail["leaks"]
    [slot, *_] = [s for s in detail["request"]["slots"] if "block" in leak and s["block"] == leak["block"]] or [None]
    assert slot is not None, "the leak does not say which block of the request it is in"
    return detail, leak, slot


def test_a_value_that_stopped_a_request_is_pointed_at_in_it(client, runtime):
    text = f"deploy with key {GH_PAT} and tell the team"
    assert _stop(client, text).status_code == 400
    detail, leak, slot = _stopped(client, runtime)
    assert slot["original"] == text
    assert slot["original"][leak["start"] : leak["end"]] == GH_PAT
    assert slot["masked"][leak["masked_start"] : leak["masked_end"]] == GH_PAT  # it would have left as it is
    assert (leak["type"], leak["class"], leak["rule"]) == ("GITHUB_PAT", "SECRET", "gitleaks:github-pat")
    assert (leak["kind"], leak["new"]) == ("user_text", True)
    assert GH_PAT not in json.dumps(leak)  # where it is, never what it is
    assert GH_PAT not in json.dumps(detail["summary"])


def test_a_stop_found_by_a_fields_name_keeps_the_fields_kind_and_never_its_name(client, runtime, tmp_path):
    """A column's name can be a value itself (a name before a secret's word): the rule that found the stopped value
    names the field, and a computer's history keeps what stopped a request in clear. It keeps the field's kind alone."""
    table = "| laura carter password | note |\n|---|---|\n| hunter2222 | rotate it |"
    assert _stop(client, f"the logins:\n{table}").status_code == 400
    _, leak, _ = _stopped(client, runtime)
    assert (leak["type"], leak["class"], leak["rule"]) == ("PASSWORD", "SECRET", "field-secret:password")
    runtime.recorder.drain()
    with sqlite3.connect(tmp_path / "data" / "exchanges.db") as conn:
        kept = [leaks for (leaks,) in conn.execute("SELECT leaks FROM exchanges")]
    assert kept and not any(name in leaks for leaks in kept for name in ("laura", "carter"))


def test_a_stopped_value_is_not_pointed_at_once_its_block_is_pruned(client, runtime):
    assert _stop(client, f"deploy with key {GH_PAT}").status_code == 400
    detail, leak, _ = _stopped(client, runtime)
    runtime.exchanges._db.execute("DELETE FROM blocks")  # retention takes blocks away before the page opens
    again = client.get(f"{API}/exchanges/{detail['summary']['id']}")
    assert again.status_code == 200 and again.json()["request"]["slots"] == []
    [left] = again.json()["leaks"]
    assert left["block"] == leak["block"] and "start" not in left and GH_PAT not in json.dumps(left)


def test_a_stopped_value_beside_masked_ones_is_found_on_both_sides(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    items = [{"text": f"mail {EMAIL}"}]
    mail = client.post("/_latet/engine/v1/mask", json={"session": "cc-dash", "items": items}, headers=token)
    masked = mail.json()["items"][0]["text"]
    assert EMAIL not in masked
    assert _stop(client, f"{masked}, key {GH_PAT}", {**CLAUDE, **SUBSCRIPTION}).status_code == 400
    _, leak, slot = _stopped(client, runtime)
    assert slot["original"] == f"mail {EMAIL}, key {GH_PAT}"
    assert slot["masked"][leak["masked_start"] : leak["masked_end"]] == GH_PAT
    assert slot["original"][leak["start"] : leak["end"]] == GH_PAT
    assert leak["start"] != leak["masked_start"]  # the masked email has not the length of the real one
    assert [slot["original"][r["start"] : r["end"]] for r in slot["replacements"]] == [EMAIL]


def test_a_stopped_value_is_not_pointed_at_when_the_history_keeps_no_text(client, runtime):
    _login(client, runtime)
    assert client.put(f"{API}/config", json={"history": "metadata"}, headers=UI_HEADERS).json()["ok"]
    assert _stop(client, f"deploy with key {GH_PAT}").status_code == 400
    [summary] = _exchanges(client, runtime, flag="blocked")
    assert "leak" in summary["flags"] and summary["verified"] is False
    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    assert detail["request"]["slots"] == []
    assert detail["leaks"] == [{"type": "GITHUB_PAT", "kind": "user_text", "preview": "GITHUB_PAT[40]", "new": True}]


def test_a_value_the_plugin_stopped_is_pointed_at_too(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    slots = [{"text": "", "kind": "user_text"}, {"text": f"deploy with {GH_PAT}", "kind": "user_text"}]
    verdict = client.post("/_latet/engine/v1/verify", json={"session": "cc-final", "slots": slots}, headers=token)
    assert verdict.json()["blocking"] and GH_PAT not in verdict.text
    _, leak, slot = _stopped(client, runtime)
    assert slot["original"] == f"deploy with {GH_PAT}"  # not the empty one before it
    assert slot["original"][leak["start"] : leak["end"]] == GH_PAT


def test_not_sensitive_lets_the_next_identical_request_through(client, runtime, upstream):
    text = f"deploy with key {GH_PAT} and tell the team"
    assert _stop(client, text).status_code == 400
    _, leak, slot = _stopped(client, runtime)
    value = slot["original"][leak["start"] : leak["end"]]  # what the page sends when you choose Not sensitive
    before = len(upstream.received)
    assert client.post(f"{API}/allow", json={"value": value}, headers=UI_HEADERS).json()["ok"] is True
    assert _stop(client, text).status_code == 200
    assert len(upstream.received) == before + 1 and GH_PAT.encode() in upstream.received[-1]
    newest, stopped = _exchanges(client, runtime)
    assert newest["verified"] is True and "leak" not in newest["flags"] and "blocked" not in newest["flags"]
    assert "blocked" in stopped["flags"]  # the one before stays as it was
    # Only that value: another secret in the same request is stopped still.
    other = "ghp_" + "Q8wE3rT6yU9iO2pA5sD7fG1hJ4kL0zX3cV6b"
    assert _stop(client, f"{text} and {other}").status_code == 400


def _pem_key():
    """A private key as a PEM block, made here: a fake, its body is counted bytes."""
    body = base64.encodebytes(bytes((i * 7 + 3) % 256 for i in range(1000))).decode()
    return f"-----BEGIN PRIVATE KEY-----\n{body}-----END PRIVATE KEY-----"


def test_a_key_read_twice_passes_with_the_texts_the_page_marks_as_one_value(client, runtime, upstream):
    key = _pem_key()
    text = f"deploy with this key:\n{key}\nthen tell the team"
    assert _stop(client, text).status_code == 400
    _login(client, runtime)
    [summary] = _exchanges(client, runtime, flag="blocked")
    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    [slot] = detail["request"]["slots"]
    # One key, read whole by one rule and by its body inside it by another: two leaks, one inside the other.
    spans = sorted((leak["masked_start"], leak["masked_end"]) for leak in detail["leaks"])
    assert len(spans) == 2 and spans[0][0] < spans[1][0] < spans[1][1] < spans[0][1]
    whole, body = (slot["masked"][start:end] for start, end in spans)
    assert whole == key and body in key
    allow = f"{API}/allow"
    # The page marks the two as one value, and Not sensitive allows both: the whole text alone does not pass it.
    assert client.post(allow, json={"value": whole}, headers=UI_HEADERS).json()["ok"] is True
    assert _stop(client, text).status_code == 400
    assert client.post(allow, json={"value": body}, headers=UI_HEADERS).json()["ok"] is True
    before = len(upstream.received)
    assert _stop(client, text).status_code == 200
    assert len(upstream.received) == before + 1 and json.dumps(key)[1:-1].encode() in upstream.received[-1]
    assert {whole, body} <= set(load_config(runtime.config_file).allow_values)  # kept for the next start too


def test_codex_websocket_turn_is_one_exchange(client, runtime):
    frame = {
        "type": "response.create",
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
    }
    with client.websocket_connect(
        "/backend-api/codex/responses",
        headers={"host": "127.0.0.1:5283", "session-id": "s-ws", "originator": "codex_cli_rs"},
    ) as ws:
        ws.send_text(json.dumps(frame))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    _login(client, runtime)
    [summary] = _exchanges(client, runtime)
    assert (summary["transport"], summary["status"], summary["agent"]) == ("ws", 101, "codex")
    assert summary["masked"] >= 2 and summary["restored"] >= 2
    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    assert detail["request"]["slots"][0]["original"] == PROMPT
    assert detail["response"]["text_restored"] == "You said: " + PROMPT


def _codex(client, session, **headers):
    """Codex signed in with ChatGPT: its WebSocket."""
    return client.websocket_connect(
        "/backend-api/codex/responses",
        headers={"host": "127.0.0.1:5283", "session-id": session, "originator": "codex_cli_rs", **headers},
    )


def _codex_turn(text, image=False):
    content = [{"type": "input_text", "text": text}]
    if image:
        content.append({"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="})
    return {"type": "response.create", "model": "m", "input": [{"type": "message", "role": "user", "content": content}]}


def test_a_websocket_turn_that_is_stopped_is_on_the_dashboard_as_a_request_over_http_is(client, runtime, upstream):
    _login(client, runtime)
    assert client.put(f"{API}/config", json={"images": "block"}, headers=UI_HEADERS).json()["ok"]
    text = "what is in this screenshot?"
    turn = _codex_turn(text, image=True)
    body = {"model": "m", "input": turn["input"]}
    assert (
        client.post("/openai/v1/responses", json=body, headers={"authorization": "Bearer sk-test"}).status_code == 400
    )
    sent = len(upstream.ws_received)
    with _codex(client, "s-ws-stopped") as ws:
        ws.send_text(json.dumps(turn))
        error = json.loads(ws.receive_text())
    assert error["error"]["type"] == "latet_blocked" and len(upstream.ws_received) == sent  # nothing left
    stopped = _exchanges(client, runtime, flag="blocked")
    assert [s["transport"] for s in stopped] == ["ws", "http"]  # the newest first
    over_ws, over_http = stopped
    assert (over_ws["status"], over_ws["agent"], over_ws["session"]) == (400, "codex", "session-id:s-ws-stopped")
    assert over_ws["block_reason"] == over_http["block_reason"] and "images: block" in over_ws["block_reason"]
    assert over_ws["overhead_ms"] is None and over_ws["verified"] is False  # never forwarded
    assert {"blocked", "uninspected:image"} <= set(over_ws["flags"])
    detail = client.get(f"{API}/exchanges/{over_ws['id']}").json()
    assert detail["response"] is None and [s["original"] for s in detail["request"]["slots"]] == [text]
    assert detail["request"]["body_bytes"] > 0 and dict(detail["request"]["headers"])["originator"] == "codex_cli_rs"


def test_a_websocket_turn_that_is_stopped_leaves_the_turn_still_answering_alone(client, runtime):
    _login(client, runtime)
    assert client.put(f"{API}/config", json={"images": "block"}, headers=UI_HEADERS).json()["ok"]
    with _codex(client, "s-ws-held", **{"x-fake-hold": "1"}) as ws:  # its answer ends when the test says so
        ws.send_text(json.dumps(_codex_turn("a question")))
        assert json.loads(ws.receive_text())["type"] == "response.created"  # answering
        ws.send_text(json.dumps(_codex_turn("a screenshot", image=True)))
        while (event := json.loads(ws.receive_text()))["type"] != "error":
            pass
        assert event["error"]["type"] == "latet_blocked"
        [stop] = _exchanges(client, runtime)  # the first turn goes on: the stopped frame never reached the upstream
        assert (stop["transport"], stop["status"]) == ("ws", 400)
        ws.send_text(json.dumps({"type": "fake.release"}))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    stop, first = _exchanges(client, runtime)
    assert (stop["status"], first["status"]) == (400, 101)
    assert "error" not in first["flags"]  # it ended on its own: the stop did not end it, as a new turn would


def test_stats_sessions_and_integrations(client, runtime):
    _chat(client)
    _login(client, runtime)
    _exchanges(client, runtime)
    stats = client.get(f"{API}/stats", params={"days": 7}).json()
    assert stats["by_day"][0]["exchanges"] == 1
    assert stats["by_type"].get("EMAIL") == 1
    assert stats["latency_ms"]["p50"] is not None
    [session] = client.get(f"{API}/sessions").json()["items"]
    assert session["exchanges"] == 1 and session["masked"] >= 2
    assert client.get(f"{API}/nope").status_code == 404
    assert client.delete(f"{API}/exchanges", headers=UI_HEADERS).json() == {"ok": True}
    assert client.get(f"{API}/exchanges").json()["items"] == []


def test_metadata_history_keeps_no_text(client, runtime):
    _login(client, runtime)
    assert client.put(f"{API}/config", json={"history": "metadata"}, headers=UI_HEADERS).json()["ok"]
    _chat(client)
    [summary] = _exchanges(client, runtime)
    assert summary["masked"] >= 2  # counts are kept
    detail = client.get(f"{API}/exchanges/{summary['id']}").json()
    assert detail["request"]["slots"] == [] and detail["response"] is None
    assert client.put(f"{API}/config", json={"history": "off"}, headers=UI_HEADERS).json()["ok"]
    _chat(client)
    assert len(_exchanges(client, runtime)) == 1  # nothing new recorded


def test_ui_is_served_with_spa_fallback(client, runtime, tmp_path):
    runtime.gateway.dashboard.web_dir = tmp_path / "not-built"
    assert "not part of this install" in client.get("/_latet/ui/").text
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    runtime.gateway.dashboard.web_dir = web
    index = client.get("/_latet/ui/")
    assert "id=root" in index.text
    assert "frame-ancestors 'none'" in index.headers["content-security-policy"]
    assert "id=root" in client.get("/_latet/ui/sessions/abc").text  # client-side route
    asset = client.get("/_latet/ui/assets/app.js")
    assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
    assert client.get("/_latet/ui/assets/missing.js").status_code == 404
    assert client.get("/_latet/ui/..%2f..%2fconfig.yaml").status_code == 404
    assert client.get("/_latet/ui", follow_redirects=False).status_code == 308


def test_ui_fonts_are_served_as_fonts(client, runtime, tmp_path):
    # Python's guess on Windows knows no .woff2: the type is given, not guessed.
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    (web / "assets" / "inter.woff2").write_bytes(b"wOF2")
    (web / "assets" / "inter.woff").write_bytes(b"wOFF")
    runtime.gateway.dashboard.web_dir = web
    assert client.get("/_latet/ui/assets/inter.woff2").headers["content-type"] == "font/woff2"
    assert client.get("/_latet/ui/assets/inter.woff").headers["content-type"] == "font/woff"


def test_request_stopped_by_the_plugin_is_recorded(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    slots = [{"text": f"deploy with {GH_PAT}", "kind": "user_text"}]
    verdict = client.post("/_latet/engine/v1/verify", json={"session": "cc-final", "slots": slots}, headers=token)
    assert verdict.json()["blocking"]
    _login(client, runtime)
    [summary] = _exchanges(client, runtime)
    assert (summary["transport"], summary["agent"], summary["session"]) == ("plugin", "claude-code", "cc:cc-final")
    assert "blocked" in summary["flags"] and summary["verified"] is False
    assert client.get(f"{API}/exchanges/{summary['id']}").json()["leaks"]


def test_a_conversation_older_than_latet_is_told_to_start_again(client, runtime):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    old = [{"text": f"deploy with {GH_PAT}", "kind": "user_text"}, {"text": "done", "kind": "assistant_text"}]
    new = [{"text": "and now the tests", "kind": "user_text"}]
    body = {"session": "cc-old", "slots": old + new, "history": 2}
    verdict = client.post("/_latet/engine/v1/verify", json=body, headers=token).json()
    assert verdict["blocking"] and verdict["inherited"] is True
    # A value in clear in the new message is no history of anyone's.
    fresh = {"session": "cc-new", "slots": [{"text": "hi", "kind": "user_text"}, *old[::-1]], "history": 1}
    assert client.post("/_latet/engine/v1/verify", json=fresh, headers=token).json()["inherited"] is False
    _login(client, runtime)
    summaries = {s["session"]: s for s in _exchanges(client, runtime)}
    assert "history" in summaries["cc:cc-old"]["flags"] and "history" not in summaries["cc:cc-new"]["flags"]
    detail = client.get(f"{API}/exchanges/{summaries['cc:cc-old']['id']}").json()
    assert "/clear" in (detail.get("block_reason") or detail["summary"]["block_reason"])


def test_the_plugin_hears_what_claude_md_holds_for_the_reviewer(client, runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project = tmp_path / "shop"
    project.mkdir()
    (project / "CLAUDE.md").write_text("db: postgres://app:S3cr3tPa55w0rd@db.internal/shop\n", "utf-8")
    token = {"x-latet-token": runtime.gateway.bridge_token}
    body = {"session": "cc-md", "cwd": str(project)}
    r = client.post("/_latet/engine/v1/instructions", json=body, headers=token)
    [found] = r.json()["findings"]
    assert (found["type"], found["line"]) == ("URL_PASSWORD", 1) and "S3cr3t" not in r.text
    assert client.post("/_latet/engine/v1/instructions", json=body).status_code != 200  # the bridge token


def test_max_profile_does_not_send_images(client, runtime, upstream):
    _login(client, runtime)
    assert client.put(f"{API}/config", json={"profile": "max"}, headers=UI_HEADERS).json()["ok"]
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}]}
    before = len(upstream.received)
    r = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 400 and "max profile" in r.json()["error"]["message"]
    assert len(upstream.received) == before  # nothing left
    [summary] = _exchanges(client, runtime, flag="blocked")
    assert "uninspected:image" in summary["flags"]
    assert client.put(f"{API}/config", json={"profile": "balanced"}, headers=UI_HEADERS).json()["ok"]
    assert client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"}).status_code == 200


def test_strict_data_mode_follows_the_profile_until_you_set_it(client, runtime, tmp_path):
    def data() -> tuple[str | None, str]:
        view = client.get(f"{API}/config").json()
        return view["data"], view["data_effective"]

    _login(client, runtime)
    assert data() == (None, "normal")
    assert client.put(f"{API}/config", json={"profile": "max"}, headers=UI_HEADERS).json()["ok"]
    assert data() == (None, "strict")  # not set: the profile decides
    assert load_config(runtime.config_file).data is None
    root = _project(tmp_path, "")
    project = client.post(f"{API}/projects", json={"path": str(root)}, headers=UI_HEADERS).json()
    assert project["effective"]["data"] == "strict"  # a project goes with it
    assert client.put(f"{API}/config", json={"data": "normal"}, headers=UI_HEADERS).json()["ok"]
    assert data() == ("normal", "normal")  # set by you, from the dashboard: it applies
    assert client.put(f"{API}/config", json={"data": None}, headers=UI_HEADERS).json()["ok"]
    assert data() == (None, "strict")  # back to the profile's


def test_auto_mode_review_lets_through_only_the_allowed_details(tmp_path, upstream):
    identity = IdentityConfig(enabled=True, usernames=["jdoe"], full_name="Jane Doe", emails=["jane.doe@studio-doe.it"])
    rt = make_runtime(Config(identity=identity), data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("anthropic", "/anthropic", upstream.url)]
    # A short user name: elsewhere it is masked only inside home paths, but never missed here.
    review = "You are a security monitor for autonomous AI coding agents.\n\n## Context\nUser identity: `jdoe`\n"

    def body(action):
        system = [{"type": "text", "text": "x-anthropic-billing-header: cch=0;"}, {"type": "text", "text": review}]
        return {"model": "m", "system": system, "messages": [{"role": "user", "content": f"Action: {action}"}]}

    def allow(**choices):
        rt.apply_config(rt.config.model_copy(update={"auto_mode_review": AutoModeReview(**choices)}), save=False)

    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as c:

            def send(action):
                return c.post("/anthropic/v1/messages", json=body(action), headers=CLAUDE)

            edit = "edit /home/jdoe/project/app.py"
            blocked = send(edit)
            assert blocked.status_code == 400
            message = blocked.json()["error"]["message"]
            assert "your user name and your user folder in paths" in message
            assert "auto_mode_review: {user_name: allow, paths: allow}" in message
            allow(user_name="allow")
            blocked = send(edit)
            assert blocked.status_code == 400 and "{paths: allow}" in blocked.json()["error"]["message"]
            allow(user_name="allow", paths="allow")
            assert send(edit).status_code == 200
            # The git author's email is personal data too: letting the author through lets it through.
            commit = "git commit --author 'Jane Doe <jane.doe@studio-doe.it>'"
            blocked = send(commit)
            assert blocked.status_code == 400 and "your git author" in blocked.json()["error"]["message"]
            allow(user_name="allow", paths="allow", git_author="allow")
            assert send(commit).status_code == 200
            assert send(f"export T={GH_PAT}").status_code == 400  # never a secret
            assert send("mail mario.rossi@acme.it").status_code == 400  # nor someone else's data
            allow()  # blocked again: the reviews let through before block again
            assert send(edit).status_code == 400
            rt.recorder.drain()
            items = rt.exchanges.list()[0]
            assert all("auto-mode-review" in s["flags"] for s in items)
            let_through = [s for s in items if s["status"] == 200]
            assert len(let_through) == 2 and not any("leak" in s["flags"] for s in let_through)
            detail = rt.exchanges.detail(let_through[0]["id"])
            leaks = [(x["type"], x.get("detail"), x.get("allowed")) for x in detail["leaks"]]
            # The email is found as personal data too: it goes through as part of the git author.
            assert leaks.count(("EMAIL", "git_author", True)) == 2
            assert ("USERNAME", "user_name", True) in leaks
    finally:
        rt.close()


def test_user_name_surrogate_needs_the_local_token(client, runtime, monkeypatch):
    import latet.config

    monkeypatch.setattr(latet.config, "os_user_name", lambda: "jdoe")
    assert client.post("/_latet/admin/user-name").status_code == 401
    token = {"x-latet-token": runtime.gateway.bridge_token}
    r = client.post("/_latet/admin/user-name", headers=token)
    assert r.status_code == 200
    surrogate = r.json()["surrogate"]
    assert surrogate != "jdoe" and runtime.built.vault.by_original("jdoe").surrogate == surrogate
    assert client.post("/_latet/admin/user-name", headers=token).json()["surrogate"] == surrogate


def test_countries_choose_the_id_numbers(client, runtime):
    _login(client, runtime)
    view = client.get(f"{API}/config").json()
    assert view["id_regions"] == ["IT", "US", "GB"] and "DE" in view["id_regions_available"]
    assert {"US_SSN", "IT_FISCAL_CODE", "VAT_NUMBER"} <= set(view["pii_effective"])
    assert {"type": "DE_TAX_ID", "country": "DE"} in view["pii_catalog"]
    assert client.put(f"{API}/config", json={"id_regions": ["DE"]}, headers=UI_HEADERS).json()["ok"]
    view = client.get(f"{API}/config").json()
    assert "DE_TAX_ID" in view["pii_effective"] and "US_SSN" not in view["pii_effective"]
    assert load_config(runtime.config_file).id_regions == ["DE"]
    bad = client.put(f"{API}/config", json={"id_regions": ["XX"]}, headers=UI_HEADERS)
    assert bad.status_code == 400


CARD_REF = {
    "name": "card-ref",
    "type": "CARD_REF",
    "pattern": r"\bCR(?P<value>\d{8})\b",
    "checksum": "luhn",
    "class": "secret",
}


def test_custom_recognizers_from_the_dashboard(client, runtime):
    _login(client, runtime)
    broken = {"name": "broken", "pattern": "(unclosed"}
    bad = client.put(f"{API}/config", json={"recognizers": [broken]}, headers=UI_HEADERS)
    assert bad.status_code == 400 and "broken: pattern" in bad.json()["details"][0]["msg"]
    assert client.put(f"{API}/config", json={"recognizers": [CARD_REF]}, headers=UI_HEADERS).json()["ok"]
    view = client.get(f"{API}/config").json()
    assert view["recognizers"] == [CARD_REF] and view["recognizer_status"][0]["state"] == "active"
    assert {"name": "luhn", "description": checksums.CHECKSUMS["luhn"].description} in view["checksums"]
    played = client.post(f"{API}/playground", json={"text": "pay ref CR12345674"}, headers=UI_HEADERS).json()
    assert [r["rule"] for r in played["replacements"]] == ["recognizer:card-ref"]
    # A custom secret in a Claude Code request that the plugin did not mask: the daemon stops it.
    body = {"model": "m", "messages": [{"role": "user", "content": "pay ref CR12345674"}]}
    blocked = client.post("/anthropic/v1/messages", json=body, headers=CLAUDE)
    assert blocked.status_code == 400 and "CARD_REF" in blocked.json()["error"]["message"]


def test_the_recognizer_editor_tries_a_draft(client, runtime):
    _login(client, runtime)
    r = client.post(
        f"{API}/recognizers/test",
        json={"recognizer": {**CARD_REF, "examples": {"match": ["ref CR12345678"]}}, "text": "pay ref CR12345674"},
        headers=UI_HEADERS,
    )
    assert r.status_code == 200
    item = r.json()
    assert item["state"] == "active" and not item["examples"][0]["ok"]
    assert "luhn check fails" in item["examples"][0]["reason"]
    assert item["try"]["found"] == ["12345674"] and item["try"]["spans"] == [{"start": 10, "end": 18}]
    broken = client.post(
        f"{API}/recognizers/test", json={"recognizer": {"name": "x", "pattern": "(a"}}, headers=UI_HEADERS
    )
    assert broken.json()["state"] == "error" and "RE2" in broken.json()["errors"][0]
    assert not runtime.config.recognizers  # trying never saves


def test_the_recognizer_editor_says_why_signs_alone_are_not_found(client, runtime):
    _login(client, runtime)
    lines = {"name": "rule-line", "pattern": "-{3,}", "examples": {"match": ["a\n---\nb"]}}
    item = client.post(
        f"{API}/recognizers/test", json={"recognizer": lines, "text": "| a |\n|---|"}, headers=UI_HEADERS
    ).json()
    assert not item["examples"][0]["ok"] and "no letter or digit" in item["examples"][0]["reason"]
    assert item["try"]["spans"] == [] and "signs alone are never masked" in item["try"]["reason"]


def _project(tmp_path, text):
    root = tmp_path / "acme"
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_text(text, "utf-8")
    return root


def test_not_sensitive_in_a_project_lets_through_that_project_only(client, runtime, upstream, tmp_path):
    root = _project(tmp_path, "")
    here = {"model": "m", "system": f"Working directory: {root}"}
    here["messages"] = [{"role": "user", "content": f"deploy with key {GH_PAT}"}]
    elsewhere = {"model": "m", "messages": here["messages"]}
    _login(client, runtime)
    pid = client.post(f"{API}/projects", json={"path": str(root)}, headers=UI_HEADERS).json()["id"]

    def stop(body):
        return client.post("/anthropic/v1/messages", json=body, headers=SUBSCRIPTION)

    assert stop(here).status_code == 400
    detail, leak, slot = _stopped(client, runtime)
    assert detail["summary"]["project"]["id"] == pid  # the page offers the project first
    value = slot["original"][leak["start"] : leak["end"]]
    saved = client.put(f"{API}/projects/{pid}/personal", json={"allow_values": [value]}, headers=UI_HEADERS)
    assert saved.json()["ok"] is True
    assert stop(here).status_code == 200 and GH_PAT.encode() in upstream.received[-1]
    assert stop(elsewhere).status_code == 400  # in no project it is still a secret


def test_a_project_file_masks_in_that_project_only(client, runtime, upstream, tmp_path):
    root = _project(tmp_path, "terms: [{value: Project Heron, type: PROJECT}]\n")
    system = {"role": "system", "content": f"<env>\n  Working directory: {root}\n</env>"}
    _chat(client, "elsewhere Project Heron is a bird", history=[{"role": "system", "content": "no folder here"}])
    assert "Project Heron is a bird" in upstream.all_received()
    _chat(client, "status of Project Heron?", history=[system])
    assert "status of Project Heron?" not in upstream.all_received()
    _login(client, runtime)
    projects = client.get(f"{API}/projects").json()["items"]
    [acme] = [p for p in projects if p["name"] == "acme"]
    assert acme["repo_file"].endswith("latet.yaml") and acme["enabled"] is True
    [mine] = _exchanges(client, runtime, project=acme["id"])
    assert mine["project"] == {"id": acme["id"], "name": "acme"} and mine["by_module"]["terms"] == 1
    # The Claude Code plugin names the folder itself.
    token = {"x-latet-token": runtime.gateway.bridge_token}
    item = {"text": "and Project Heron?"}
    masked = client.post(
        "/_latet/engine/v1/mask", json={"session": "cc-p", "cwd": str(root), "items": [item]}, headers=token
    )
    assert "Project Heron" not in masked.json()["items"][0]["text"]


def test_projects_api_writes_only_what_adds_protection(client, runtime, tmp_path):
    root = _project(tmp_path, "profile: max\n")
    _login(client, runtime)
    project = client.post(f"{API}/projects", json={"path": str(root)}, headers=UI_HEADERS).json()
    assert project["repo"]["settings"] == {"profile": "max"} and project["effective"]["profile"] == "max"
    pid, tag = project["id"], project["repo"]["tag"]
    bad = client.put(
        f"{API}/projects/{pid}/repo", json={"settings": {"allow_values": ["x"]}, "tag": tag}, headers=UI_HEADERS
    )
    assert bad.status_code == 400 and bad.json()["details"][0]["loc"] == ["settings", "allow_values"]
    ok = client.put(
        f"{API}/projects/{pid}/repo", json={"settings": {"id_regions": ["DE"]}, "tag": tag}, headers=UI_HEADERS
    )
    assert ok.status_code == 200 and ok.json()["repo"]["settings"] == {"id_regions": ["DE"]}
    assert "id_regions" in (root / "latet.yaml").read_text("utf-8")
    stale = client.put(f"{API}/projects/{pid}/repo", json={"settings": {}, "tag": tag}, headers=UI_HEADERS)
    assert stale.status_code == 409  # changed since it was read
    assert client.get(f"{API}/projects/missing").status_code == 404


def test_latet_off_in_a_project_sends_it_as_it_is(client, runtime, upstream, tmp_path):
    root = _project(tmp_path, "")
    _login(client, runtime)
    pid = client.post(f"{API}/projects", json={"path": str(root)}, headers=UI_HEADERS).json()["id"]
    r = client.put(f"{API}/projects/{pid}/personal", json={"enabled": False}, headers=UI_HEADERS)
    assert r.json()["ok"] is True
    assert load_config(runtime.config_file).projects[0].enabled is False  # saved in your config.yaml
    system = {"role": "system", "content": f"Working directory: {root}"}
    _chat(client, f"mail {EMAIL}", history=[system])
    assert EMAIL in upstream.all_received()
    [summary] = _exchanges(client, runtime, project=pid)
    assert "project-off" in summary["flags"] and summary["verified"] is False
    assert client.delete(f"{API}/projects/{pid}/personal", headers=UI_HEADERS).json()["ok"] is True
    assert load_config(runtime.config_file).projects == []


def test_rules_exported_and_imported_from_the_dashboard(client, runtime, tmp_path):
    _login(client, runtime)
    client.put(f"{API}/config", json={"terms": [{"value": "Project Falcon", "type": "PROJECT"}]}, headers=UI_HEADERS)
    exported = client.get(f"{API}/rules/export").json()
    assert exported["name"].startswith("latet-rules-") and "Project Falcon" in exported["file"]
    file = "terms: [{value: Banca Ligure, type: CLIENT}]\nallow_values: [ACME]\nport: 7000\n"
    assert client.post(f"{API}/rules/import", json={"file": file}).status_code == 403  # the UI header
    preview = client.post(f"{API}/rules/import", json={"file": file}, headers=UI_HEADERS).json()
    assert preview["applied"] is False and preview["report"]["added"] == ["terms: Banca Ligure"]
    assert preview["report"]["held"] == ["values never to mask: ACME"] and len(preview["report"]["notices"]) == 1
    assert [t.value for t in runtime.config.terms] == ["Project Falcon"]
    done = client.post(f"{API}/rules/import", json={"file": file, "apply": True}, headers=UI_HEADERS).json()
    assert done["applied"] is True and runtime.config.allow_values == []
    assert [t.value for t in runtime.config.terms] == ["Project Falcon", "Banca Ligure"]
    every = {"file": file, "apply": True, "all": True}
    assert client.post(f"{API}/rules/import", json=every, headers=UI_HEADERS).json()["applied"] is True
    assert runtime.config.allow_values == ["ACME"]  # chosen here, in the dashboard
    newer = client.post(f"{API}/rules/import", json={"file": "latet_rules: 9\n"}, headers=UI_HEADERS)
    assert newer.status_code == 422 and "newer" in newer.json()["error"]
    # A project's own settings: their own file.
    root = _project(tmp_path, "")
    pid = client.post(f"{API}/projects", json={"path": str(root)}, headers=UI_HEADERS).json()["id"]
    into = {"file": "terms: [{value: Studio Ferraris, type: PARTNER}]\n", "project": pid, "apply": True}
    assert client.post(f"{API}/rules/import", json=into, headers=UI_HEADERS).json()["applied"] is True
    mine = client.get(f"{API}/rules/export?project={pid}").json()
    assert "Studio Ferraris" in mine["file"] and "Banca Ligure" not in mine["file"]
    huge = client.post(
        f"{API}/rules/import",
        content=b"{" + b" " * 9_000_000 + b"}",
        headers={**UI_HEADERS, "content-type": "application/json"},
    )
    assert huge.status_code == 400  # read no further than the cap
