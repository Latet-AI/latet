"""Strict data mode, guided, end to end: a database output masked in strict data mode, its columns and
their proposals read from the exchange the dashboard keeps, and the local reviewer's opinion on request."""

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from latet import columns as columns_module
from latet.config import Config, IdentityConfig, load_config, save_config
from latet.engine.masker import Masker
from latet.engine.reviewer import client as client_module
from latet.engine.reviewer import reviewer as reviewer_module
from latet.engine.reviewer.client import ReviewClient
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from ..unit.test_reviewer import FakeServer
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
ORDERS = """\
 id  |   stato   | creato_il  |   cliente    | importo
-----+-----------+------------+--------------+---------
 101 | SPEDITO   | 2026-09-01 | Mario Rossi  | 1250.50
 102 | IN_CORSO  | 2026-09-03 | Anna Bianchi |   80.00
 103 | SPEDITO   | 2026-09-04 | Luca Verdi   |  430.00
 104 | ANNULLATO | 2026-09-05 | Sara Neri    |   12.00
(4 rows)
"""


class ColumnsServer(FakeServer):
    """Also gives an opinion on columns when asked to."""

    opinion = json.dumps({"columns": [{"name": "importo", "keep": True, "why": "order totals, no person"}]})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            if body["messages"][0]["content"].startswith("You help decide which columns"):
                chunk = {"choices": [{"delta": {"content": self.opinion}}]}
                stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())
        return await super().__call__(request)


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
    rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
    return rt


def _login(client, rt):
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
    client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)


def _orders_exchange(
    client, rt, tool: str = "run_sql", output: str = ORDERS, folder: str | None = None
) -> tuple[str, int]:
    """A chat whose tool ran a query (``folder``: its working directory): the exchange's id, and the index of the
    tool's output in it."""
    call = {"id": "call_1", "type": "function", "function": {"name": tool, "arguments": "{}"}}
    messages = [
        *([{"role": "system", "content": f"Working directory: {folder}"}] if folder else []),
        {"role": "user", "content": "show me the orders"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": output},
        {"role": "user", "content": "which ones shipped?"},
    ]
    body = {"model": "m", "stream": True, "messages": messages}
    sent = client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer k"})
    assert sent.status_code == 200
    rt.recorder.drain()
    [summary] = client.get(f"{API}/exchanges").json()["items"]
    slots = client.get(f"{API}/exchanges/{summary['id']}").json()["request"]["slots"]
    return summary["id"], next(i for i, slot in enumerate(slots) if slot["kind"] == "tool_result")


def test_the_columns_of_an_output_masked_in_strict_data_mode(tmp_path, monkeypatch, upstream):
    rt = _daemon(tmp_path, monkeypatch, upstream)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        found = {c["name"]: c for c in answer["columns"]}
        assert answer["strict"] is True and answer["reviewer"] is False and answer["project"] is None
        assert (found["stato"]["proposal"], found["stato"]["reason"]) == ("keep", "states")
        assert found["stato"]["masked"] == 4 and found["stato"]["examples"] == ["SPEDITO", "IN_CORSO", "ANNULLATO"]
        assert found["cliente"]["proposal"] == "mask" and found["creato_il"]["proposal"] == "keep"
        assert "values" not in found["stato"]  # what the panel needs, not the whole column
        assert found["stato"]["everywhere"] and not found["stato"]["kept_now"]
        assert client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": 0}).status_code == 404
        review = client.post(f"{API}/exchanges/{exchange}/columns/review", params={"slot": slot}, headers=UI)
        assert review.status_code == 409
    rt.close()


def test_a_column_a_repository_file_masks_is_said_held_by_the_file_whatever_you_choose(tmp_path, monkeypatch, upstream):
    """Review minor 4 and re-review minor 1 (Ruling 098-R2): a column a project's latet.yaml masks stays masked there
    whatever you choose, for this project or for everywhere, the stricter of the two rules applying: the panel says the
    file holds it (where it advised This project, which changes nothing for it), and that a rule for everywhere would
    change a column the file has no rule for. A rule of the file set aside there (its type let through by your
    policies) holds nothing."""
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_text("fields: [{name: stato}, {name: cliente, type: URL}]\n", "utf-8")
    rt = _daemon(tmp_path, monkeypatch, upstream, policies={"URL": "allow"})
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt, folder=str(root))
        answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        found = {c["name"]: c for c in answer["columns"]}
        assert answer["project"] is not None and answer["project"]["loaded"] is True
        assert found["stato"]["everywhere"] is False and found["stato"]["held_by_file"] is True
        assert found["creato_il"]["everywhere"] is True and found["creato_il"]["held_by_file"] is False
        assert found["cliente"]["everywhere"] is True and found["cliente"]["held_by_file"] is False
    rt.close()


GUESTS = (
    "<bash-stdout>┌─────────┬─────────────────────┬──────────────────────┐\n"
    "│ (index) │ ospite              │ proprieta            │\n"
    "├─────────┼─────────────────────┼──────────────────────┤\n"
    "│ 0       │ 'Francesca Solieri' │ 'Milano City Center' │\n"
    "└─────────┴─────────────────────┴──────────────────────┘</bash-stdout><bash-stderr></bash-stderr>"
)


@pytest.mark.parametrize("kind", ["attachment", "other"])
def test_a_command_you_run_with_a_bang_in_claude_code_is_masked_as_the_bash_tools_output(
    tmp_path, monkeypatch, upstream, kind
):
    """Claude Code puts the output of a command you run with ! (bash mode) in your conversation as a text of yours,
    between <bash-stdout> tags: it is a command's output all the same, and strict data mode masks its values."""
    rt = _daemon(tmp_path, monkeypatch, upstream)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            token = {"x-latet-token": rt.gateway.bridge_token}
            body = {"session": "bang-1", "items": [{"text": GUESTS, "kind": kind}]}
            [item] = client.post("/_latet/engine/v1/mask", json=body, headers=token).json()["items"]
    finally:
        rt.close()
    assert "Milano City Center" not in item["text"] and "Francesca Solieri" not in item["text"]
    assert item["text"].startswith("<bash-stdout>┌") and item["text"].endswith(
        "</bash-stdout><bash-stderr></bash-stderr>"
    )


def test_a_prompt_that_quotes_a_command_output_stays_a_prompt(tmp_path, monkeypatch, upstream):
    """What you type is a prompt, even when you paste such an output in it: strict data mode is for outputs."""
    rt = _daemon(tmp_path, monkeypatch, upstream)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            token = {"x-latet-token": rt.gateway.bridge_token}
            body = {"session": "bang-2", "items": [{"text": GUESTS, "kind": "user_text"}]}
            [item] = client.post("/_latet/engine/v1/mask", json=body, headers=token).json()["items"]
    finally:
        rt.close()
    assert "Milano City Center" in item["text"]


def test_the_columns_of_an_output_with_strict_data_mode_off(tmp_path, monkeypatch, upstream):
    """The dashboard asks for them to tell an exchange holds structure strict data mode would mask: the
    output's structure is read the same with the mode off, and the answer says it is off."""
    rt = _daemon(tmp_path, monkeypatch, upstream, data="normal")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        assert b"SPEDITO" in upstream.received[-1]  # the values left as they are
        answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        found = {c["name"]: c for c in answer["columns"]}
        assert answer["strict"] is False and answer["applies"] is True  # a query: strict data mode would mask it
        assert set(found) == {"id", "stato", "creato_il", "cliente", "importo"}
        assert all(column["masked"] == 0 for column in found.values())
        slots = client.get(f"{API}/exchanges/{exchange}").json()["request"]["slots"]
        prompt = next(i for i, s in enumerate(slots) if s["kind"] == "user_text")
        assert client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": prompt}).status_code == 404
    rt.close()


ROWS = json.dumps([{"id": 101, "stato": "SPEDITO"}, {"id": 102, "stato": "IN_CORSO"}])


@pytest.mark.parametrize(
    ("tool", "data_tools", "applies"),
    [
        ("Read", [], False),  # a file read: code and documents, never data
        ("WebFetch", [], False),  # a web page: public content
        ("WebFetch", ["WebFetch"], True),  # unless `data_tools` names the web tool
        ("run_sql", [], True),  # any other tool
        ("run_sql", ["mcp__postgres__*"], False),  # `data_tools` names others: only those
    ],
)
def test_the_answer_says_whether_strict_data_mode_covers_the_tool(
    tmp_path, monkeypatch, upstream, tool, data_tools, applies
):
    """The same rule as the masking: with strict data mode on, the output's values leave masked exactly when the
    answer says it applies, and its columns are read either way."""
    rt = _daemon(tmp_path, monkeypatch, upstream, data_tools=data_tools)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt, tool, ROWS)
        assert (b"SPEDITO" in upstream.received[-1]) is not applies
        answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        assert answer["applies"] is applies and answer["strict"] is True
        assert {c["name"] for c in answer["columns"]} == {"id", "stato"}
    rt.close()


@pytest.mark.parametrize(("tool", "applies"), [("run_sql", True), ("Read", False)])
def test_the_light_answer_runs_no_detector(tmp_path, monkeypatch, upstream, tool, applies):
    """What the exchange's notice asks (`?shape=1`): the names of the output's columns, whether strict data mode is on
    and whether it covers the tool, as the full answer gives them, with no proposal worked out: no detector runs, name
    detection included. For a tool strict data mode does not cover, the output is not even read for columns."""
    rt = _daemon(tmp_path, monkeypatch, upstream, data="normal")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt, tool, ROWS)
        calls: list[str] = []

        def counted(name, real):
            def call(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)

            return call

        monkeypatch.setattr(columns_module, "propose", counted("propose", columns_module.propose))
        monkeypatch.setattr(columns_module, "columns_of", counted("columns_of", columns_module.columns_of))
        monkeypatch.setattr(Masker, "detect", counted("detect", Masker.detect))  # every masker, a project's too
        full = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        assert full["applies"] is applies and full["strict"] is False
        assert {"columns_of", "propose", "detect"} <= set(calls)  # what is counted is what the full answer runs
        calls.clear()
        light = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot, "shape": 1})
        assert light.status_code == 200, light.text
        names = [column["name"] for column in full["columns"]] if applies else []
        assert light.json() == {"columns": names, "strict": False, "applies": applies}
        assert calls == (["columns_of"] if applies else [])
        assert client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": 0, "shape": 1}).status_code == 404
    rt.close()


def test_the_light_answer_says_strict_data_mode_is_on(tmp_path, monkeypatch, upstream):
    rt = _daemon(tmp_path, monkeypatch, upstream)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        light = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot, "shape": "1"}).json()
        assert light == {"columns": ["id", "stato", "creato_il", "cliente", "importo"], "strict": True, "applies": True}
    rt.close()


def test_the_max_profile_alone_turns_strict_data_mode_on(tmp_path, monkeypatch, upstream):
    rt = _daemon(tmp_path, monkeypatch, upstream, data=None, profile="max")  # `data` not set: max decides
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        assert b"SPEDITO" not in upstream.received[-1]  # the dull values leave masked too
        answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        assert answer["strict"] is True and {c["name"]: c for c in answer["columns"]}["stato"]["masked"] == 4
    rt.close()


@pytest.fixture
def server(monkeypatch):
    fake = ColumnsServer(slots=1)
    real = ReviewClient

    def faked(url, model=None, api_key=None, **kwargs):
        return real(url, model, api_key, transport=httpx.MockTransport(fake), timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr(client_module, "ReviewClient", faked)
    monkeypatch.setattr(reviewer_module, "ReviewClient", faked)
    return fake


def test_the_local_reviewer_gives_its_opinion_and_changes_nothing(tmp_path, monkeypatch, upstream, server):
    rt = _daemon(tmp_path, monkeypatch, upstream, llm_review={"enabled": True, "model": "fake-model"})
    before = (tmp_path / "config.yaml").read_text("utf-8")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        review = client.post(f"{API}/exchanges/{exchange}/columns/review", params={"slot": slot}, headers=UI)
        assert review.status_code == 200, review.text
        assert review.json()["opinions"] == {"importo": {"keep": True, "why": "order totals, no person"}}
    assert (tmp_path / "config.yaml").read_text("utf-8") == before
    assert load_config(tmp_path / "config.yaml").fields == []
    rt.close()


def test_an_opinion_that_cannot_be_read_is_said(tmp_path, monkeypatch, upstream, server):
    server.opinion = "no idea"
    rt = _daemon(tmp_path, monkeypatch, upstream, llm_review={"enabled": True, "model": "fake-model"})
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        exchange, slot = _orders_exchange(client, rt)
        review = client.post(f"{API}/exchanges/{exchange}/columns/review", params={"slot": slot}, headers=UI)
        assert review.status_code == 502 and "could not be read" in review.json()["error"]
    rt.close()


def test_without_the_texts_there_is_nothing_to_read(tmp_path, monkeypatch, upstream):
    rt = _daemon(tmp_path, monkeypatch, upstream, history="metadata")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        _login(client, rt)
        call = {"id": "c", "type": "function", "function": {"name": "run_sql", "arguments": "{}"}}
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c", "content": ORDERS},
        ]
        client.post(
            "/openai/v1/chat/completions",
            json={"model": "m", "stream": True, "messages": messages},
            headers={"authorization": "Bearer k"},
        )
        rt.recorder.drain()
        [summary] = client.get(f"{API}/exchanges").json()["items"]
        answer = client.get(f"{API}/exchanges/{summary['id']}/columns", params={"slot": 0})
        assert answer.status_code == 409 and "history" in answer.json()["error"]
    rt.close()
