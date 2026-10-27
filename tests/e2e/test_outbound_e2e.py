"""Tools that run outside this computer, given a stand-in: the Claude Code plugin stops them, and a
tool the provider already ran is said in the dashboard."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway import outbound
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
EMAIL = "mario.rossi@acme.it"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml"
    )
    rt.gateway.upstreams = [Upstream("anthropic", "/anthropic", upstream.url)]
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        code = c.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
        c.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
        yield c


def _stand_in(client, runtime, session="out-1"):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    items = {"session": session, "items": [{"text": f"write to {EMAIL}", "kind": "user_text"}]}
    [item] = client.post("/_latet/engine/v1/mask", json=items, headers=token).json()["items"]
    entity = runtime.built.vault.by_original(EMAIL)
    assert entity is not None and entity.surrogate in item["text"]
    return entity.surrogate


def _exchanges(client, runtime):
    runtime.recorder.drain()
    return client.get(f"{API}/exchanges").json()["items"]


def test_the_plugin_is_told_to_stop_a_web_search_with_a_stand_in(client, runtime):
    fake = _stand_in(client, runtime)
    token = {"x-latet-token": runtime.gateway.bridge_token}
    asked = {"session": "out-1", "tool": "WebSearch", "values": [f"who is {fake}"]}
    answer = client.post("/_latet/engine/v1/outbound", json=asked, headers=token).json()
    assert answer["found"] == ["EMAIL"]
    assert "WebSearch" in answer["reason"] and "search for someone or something else" in answer["reason"]
    assert EMAIL not in answer["reason"] and fake not in answer["reason"]  # never a value
    clean = {"session": "out-1", "tool": "WebSearch", "values": ["redis creator"]}
    assert client.post("/_latet/engine/v1/outbound", json=clean, headers=token).json() == {
        "found": [],
        "reason": None,
    }
    [stopped] = [e for e in _exchanges(client, runtime) if e["path"] == "/plugin/tool-call"]
    assert stopped["flags"] == ["blocked", "outbound:WebSearch"] and stopped["transport"] == "plugin"
    [slot] = client.get(f"{API}/exchanges/{stopped['id']}").json()["request"]["slots"]
    assert slot["kind"] == "outbound_tool" and slot["original"] == f"who is {EMAIL}"  # rebuilt here, for Not sensitive


def test_a_search_the_provider_ran_is_said_once(client, runtime, upstream):
    fake = _stand_in(client, runtime, session="out-2")
    search = {
        "type": "server_tool_use",
        "id": "srvtoolu_1",
        "name": "web_search",
        "input": {"query": f"{fake} profile"},
    }
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {"role": "user", "content": f"find {EMAIL}"},  # the conversation knows the real one
            {"role": "assistant", "content": [search, {"type": "text", "text": "Found it."}]},
            {"role": "user", "content": "thanks"},
        ],
    }
    headers = {"x-api-key": "sk-test", "session-id": "out-2"}
    for _ in range(2):  # the history is sent again with the next turn
        assert client.post("/anthropic/v1/messages", json=body, headers=headers).status_code == 200
    first, second = sorted(
        (e for e in _exchanges(client, runtime) if e["path"] == "/anthropic/v1/messages"), key=lambda e: e["ts"]
    )
    assert "outbound:web_search" in first["flags"] and "blocked" not in first["flags"]
    assert "outbound:web_search" not in second["flags"]
    slots = client.get(f"{API}/exchanges/{first['id']}").json()["request"]["slots"]
    [tool_input] = [s for s in slots if s["kind"] == "outbound_tool"]
    assert tool_input["original"] == f"{EMAIL} profile"


def test_what_the_model_is_told():
    assert "open the wrong page" in outbound.reason("WebFetch", ["EMAIL"])
    told = outbound.reason("mcp__claude_ai_Gmail__send_message", ["EMAIL", "PERSON"])
    assert "wrong address" in told and "values (EMAIL, PERSON)" in told


@pytest.mark.parametrize("format", ["environment", "claude", "codex"])
def test_latet_configuration_reaches_the_provider_masked_and_local_tools_restored(client, runtime, upstream, format):
    tail = "Q7wZ2k4NBX5pL3mD_r8S-v6Hj9Ta0Bc1Eo2Fi3Gu4J5"
    tokens = {prefix: f"latet_{prefix}_" + tail for prefix in ("pat", "key", "adm", "scim", "brg")}
    tokens["code"] = "latet_code_" + tail[:32]
    route = "Em1ly_Cart3r-Rout3K3y_7q9"
    url = "https://latet.acme.lan/u/" + route
    env = {
        "ANTHROPIC_BASE_URL": url + "/anthropic",
        "LATET_BRIDGE_TOKEN": tokens["pat"],
        "BILLING_API_KEY": tokens["key"],
        "LATET_SERVER_ADMIN_TOKEN": tokens["adm"],
        "IDP_NOTES": f"paste {tokens['scim']} into the provider",  # a value no secret's name points at
        "HANDOVER": f"the daemon's token is {tokens['brg']}; give them {tokens['code']} once",
    }
    if format == "environment":
        text = "\n".join(f"{name}={value}" for name, value in env.items())
    elif format == "claude":
        text = json.dumps({"env": env}, indent=2)
    else:
        text = (
            f'[model_providers.latet]\nbase_url = "{url}/openai/v1"\n'
            f'http_headers = {{ "Authorization" = "Bearer {tokens["pat"]}" }}\n'
            f'BILLING_API_KEY = "{tokens["key"]}"\nLATET_SERVER_ADMIN_TOKEN = "{tokens["adm"]}"\n'
            f'IDP_NOTES = "paste {tokens["scim"]} into the provider"\n'
            f'HANDOVER = "the daemon token is {tokens["brg"]}; give them {tokens["code"]} once"\n'
        )
    body = {"model": "m", "max_tokens": 1000, "messages": [{"role": "user", "content": text}]}
    answer = client.post(
        "/anthropic/v1/messages", json=body, headers={"x-api-key": "sk-test", "session-id": "latet-config"}
    )
    assert answer.status_code == 200
    sent = upstream.received[-1].decode()
    assert all(value not in sent for value in (*tokens.values(), route))
    assert "https://latet.acme.lan/u/" in sent
    [call] = [block for block in answer.json()["content"] if block["type"] == "tool_use"]
    assert call["input"]["content"] == text
    headers = {"x-latet-token": runtime.gateway.bridge_token}
    for original, etype in [
        *(
            (tokens[prefix], etype)
            for prefix, etype in (
                ("pat", "LATET_PERSONAL_TOKEN"),
                ("key", "LATET_API_KEY"),
                ("adm", "LATET_ADMIN_TOKEN"),
                ("scim", "LATET_SCIM_TOKEN"),
                ("brg", "LATET_BRIDGE_TOKEN"),
                ("code", "LATET_ONE_TIME_CODE"),
            )
        ),
        (route, "LATET_ROUTE_KEY"),
    ]:
        entity = runtime.built.vault.by_original(original)
        assert entity is not None and entity.etype == etype
        value = url.replace(route, entity.surrogate) if original == route else entity.surrogate
        result = client.post(
            "/_latet/engine/v1/outbound",
            headers=headers,
            json={"session": "latet-config", "tool": "WebFetch", "values": [value]},
        )
        assert result.status_code == 200
        assert result.json()["found"] == [etype] and "stopped" in result.json()["reason"]
        assert original not in result.text and entity.surrogate not in result.text
