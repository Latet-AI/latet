"""Strict data mode for files and web addresses, end to end: a file an agent reads, which a rule of `data_files` names,
leaves with every value masked and its lines as they were; a web tool's result from a host `data_hosts` names leaves
masked; the history keeps where each output came from, and the dashboard says strict data mode covers it."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, save_config
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
CUSTOMER = '{\n  "customer": {\n    "name": "Ada Byron",\n    "tier": "platinum"\n  },\n  "sku": "A-100-XL"\n}'
ROWS = json.dumps([{"id": 101, "stato": "SPEDITO"}, {"id": 102, "stato": "IN_CORSO"}])


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _daemon(tmp_path, monkeypatch, upstream, **settings):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(**{"identity": IdentityConfig(enabled=False), "data": "strict", **settings})
    save_config(config, tmp_path / "config.yaml")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [
        Upstream("anthropic", "/anthropic", upstream.url),
        Upstream("openai", "/openai", upstream.url),
    ]
    return rt


def _login(client, rt):
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
    client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)


def _numbered(text: str) -> str:
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(text.split("\n"), 1))


def _read(client, path: str) -> None:
    """An agent read the file at ``path`` (``CUSTOMER``) with its Read tool, and asks about it."""
    call = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": path}}
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": _numbered(CUSTOMER)}
    messages = [
        {"role": "user", "content": "what tier is the customer?"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [result]},
    ]
    body = {"model": "m", "max_tokens": 64, "stream": True, "messages": messages}
    assert client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "k"}).status_code == 200


@pytest.mark.parametrize("data", ["strict", "normal"])  # a file named is data with strict data mode off too
def test_a_json_file_a_rule_names_leaves_with_every_value_masked_and_its_lines(tmp_path, monkeypatch, upstream, data):
    rt = _daemon(tmp_path, monkeypatch, upstream, data=data, data_files=["*.json"])
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _read(client, "/work/acme/exports/customer.json")
        sent = json.loads(upstream.received[-1])
        content = sent["messages"][2]["content"][0]["content"]
        for value in ("Ada Byron", "platinum", "A-100-XL"):
            assert value not in content
        lines = content.split("\n")
        assert [line.split("\t")[0] for line in lines] == [
            line.split("\t")[0] for line in _numbered(CUSTOMER).split("\n")
        ]
        assert '"customer": {' in content and '"sku": "' in content
    rt.close()


@pytest.mark.parametrize("settings", [{}, {"data_files": ["*.csv"]}, {"data": "normal", "data_files": ["*.csv"]}])
def test_a_file_no_rule_names_leaves_as_today(tmp_path, monkeypatch, upstream, settings):
    rt = _daemon(tmp_path, monkeypatch, upstream, **settings)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _read(client, "/work/acme/exports/customer.json")
        assert b"platinum" in upstream.received[-1] and b"A-100-XL" in upstream.received[-1]
    rt.close()


def _called(client, tool: str, arguments: dict[str, str]) -> None:
    """An agent called ``tool`` with ``arguments``, and sends its output (``ROWS``) to the model."""
    call = {"id": "call_1", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}
    messages = [
        {"role": "user", "content": "show me the orders"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": ROWS},
    ]
    body = {"model": "m", "stream": True, "messages": messages}
    sent = client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer k"})
    assert sent.status_code == 200


@pytest.mark.parametrize(
    ("tool", "arguments", "settings", "applies", "kept"),
    [
        ("Read", {"file_path": "/work/acme/orders.json"}, {"data_files": ["*.json"]}, True, "/work/acme/orders.json"),
        ("Read", {"file_path": "/work/acme/orders.json"}, {"data_files": ["*.csv"]}, False, "/work/acme/orders.json"),
        (
            "WebFetch",
            {"url": "https://api.internal.example.com/o"},
            {"data_hosts": ["*.internal.example.com"]},
            True,
            "https://api.internal.example.com/",
        ),
        ("WebFetch", {"url": "https://example.org/o"}, {"data_hosts": ["*.internal.example.com"]}, False, None),
    ],
)
def test_the_history_keeps_where_an_output_came_from_and_the_dashboard_says_it_is_covered(
    tmp_path, monkeypatch, upstream, tool, arguments, settings, applies, kept
):
    rt = _daemon(tmp_path, monkeypatch, upstream, **settings)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        _called(client, tool, arguments)
        assert (b"SPEDITO" in upstream.received[-1]) is not applies
        rt.recorder.drain()
        [summary] = client.get(f"{API}/exchanges").json()["items"]
        slots = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"]
        slot = next(i for i, s in enumerate(slots) if s["kind"] == "tool_result")
        assert (slots[slot]["tool"], slots[slot]["source"]) == (tool, kept or "https://example.org/")
        answer = client.get(f"{API}/exchanges/{summary['id']}/columns", params={"slot": slot}).json()
        assert answer["applies"] is applies and answer["strict"] is True
        light = client.get(f"{API}/exchanges/{summary['id']}/columns", params={"slot": slot, "shape": 1}).json()
        assert light["applies"] is applies
    rt.close()


EMAIL = "ada.byron@acme-intra.it"


@pytest.mark.parametrize(
    ("tool", "arguments", "settings", "kept"),
    [
        (
            "Read",
            {"file_path": f"/work/clients/{EMAIL}/orders.json"},
            {"data_files": ["clients/**"]},
            f"/work/clients/{EMAIL}/orders.json",
        ),
        (
            "WebFetch",
            {"url": f"https://crm.internal.example.com/contacts/{EMAIL}?email={EMAIL}&token=s3cr3t-t0ken#notes"},
            {"data_hosts": ["crm.internal.example.com"]},
            "https://crm.internal.example.com/",
        ),
    ],
)
def test_the_history_keeps_where_an_output_came_from_as_it_left_and_forgets_a_value_in_it(
    tmp_path, monkeypatch, upstream, tool, arguments, settings, kept
):
    """Where an output came from is kept as the request carried it, masked, and shown with the values from the vault:
    a value forgotten from the vault is gone from it too. A web address is kept by its scheme and host alone."""
    rt = _daemon(tmp_path, monkeypatch, upstream, **settings)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        _called(client, tool, arguments)
        assert EMAIL.encode() not in upstream.received[-1] and b"SPEDITO" not in upstream.received[-1]
        rt.recorder.drain()
        [summary] = client.get(f"{API}/exchanges").json()["items"]
        stored = rt.exchanges.detail(summary["id"])["request"]["slots"]
        slot = next(i for i, s in enumerate(stored) if s["kind"] == "tool_result")
        for clear in (EMAIL, "s3cr3t-t0ken", "contacts", "notes"):
            assert clear not in json.dumps(stored[slot]["source"]), clear
        shown = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"][slot]
        assert shown["source"] == kept
        assert client.get(f"{API}/exchanges/{summary['id']}/columns", params={"slot": slot}).json()["applies"]
        entity = rt.built.vault.by_original(EMAIL)
        assert client.delete(f"{API}/vault/{entity.id}", headers={"x-latet-ui": "1"}).json() == {"ok": True}
        shown = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"][slot]
        assert EMAIL not in shown["source"]
        assert shown["source"] == kept.replace(EMAIL, entity.surrogate)
    rt.close()
