"""The history's public summary through the real gateway (P2-5's safe view, root's ruling of 2026-11-03): what the
dashboard lists and the tutorial's checks read. A rewritten request says `rewrite`; a stopped one says its category
code, its words kept in the full history's private view; the conversation by its opaque key, the destination's host and
the route template the envelope admitted; never a value, a header or the body."""

import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, Destination, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
WORD = "Quillfeather"  # a word of the body no rule masks: it leaves as it is, and is never in a summary
PROMPT = f"deploy with key {GH_PAT}, notify {EMAIL}, codename {WORD}"
CC_SESSION = "9a4c2e7f-1b3d-4e58-b6a0-3f7d5c8e2b91"
CLAUDE = {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": CC_SESSION}
SUBSCRIPTION = "Bearer sk-ant-oat01-" + "Zr4Wq8Lm2Tx6Pv0Ks3Hn"  # Claude Code with Pro or Max: only checked
KEY = "Bearer sk-" + "Bq7Nw2Xc5Vm8Lz1Kd4Hs6Jt"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(params=["full", "metadata"])
def runtime(request, tmp_path, upstream):
    config = Config(identity=IdentityConfig(enabled=False), history=request.param)
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [
        Upstream("anthropic", "/anthropic", upstream.url),
        Upstream("openai", "/openai", upstream.url),
    ]
    yield rt
    rt.close()


def _summaries(rt) -> list[tuple[dict, dict]]:
    """Each recorded exchange's list row and detail, newest first."""
    assert rt.recorder.drain()
    rows, _ = rt.exchanges.list()
    return [(row, rt.exchanges.detail(row["id"])) for row in rows]


def _public(rt) -> str:
    """Everything a safe reader is given, and the summary column as the computer's file holds it in clear."""
    shown = [[row, detail["summary"]] for row, detail in _summaries(rt)]
    clear = [summary for (summary,) in rt.exchanges._db.execute("SELECT summary FROM exchanges")]
    return json.dumps([shown, clear])


def test_a_rewritten_request_says_rewrite(runtime, upstream):
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": PROMPT}]}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        assert client.post("/openai/v1/chat/completions", json=body, headers={"authorization": KEY}).status_code == 200
    [(row, detail)] = _summaries(runtime)
    for summary in (row, detail["summary"]):
        assert summary["mode"] == "rewrite", "a rewritten exchange never reads passthrough"
        assert summary["protocol"] == "chat" and summary["path"] == "/{destination}/v1/chat/completions"
        assert summary["host"] == "custom"  # the pretend provider's address is no provider's public endpoint
        assert summary["session"].startswith("hg:l1:")
        assert summary["block_reason"] is None and "blocked" not in summary["flags"]
        assert summary["verified"] is True and summary["model"] == "gpt-4.1-mini"
        assert type(summary["overhead_ms"]) is int


def test_a_stopped_request_says_its_category(runtime, upstream):
    body = {"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": PROMPT}]}
    headers = {**CLAUDE, "authorization": SUBSCRIPTION}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        stopped = client.post("/anthropic/v1/messages", json=body, headers=headers)
    assert stopped.status_code == 400 and "would leave in clear" in stopped.text
    [(row, detail)] = _summaries(runtime)
    for summary in (row, detail["summary"]):
        assert summary["block_reason"] == "refused" and "blocked" in summary["flags"]
        assert summary["mode"] == "verify" and summary["path"] == "/{destination}/v1/messages"
        assert summary["host"] == "custom" and summary["verified"] is False and summary["overhead_ms"] is None
        assert summary["session"] == runtime.exchanges.history_group(f"cc:{CC_SESSION}")
    if runtime.config.history == "full":  # the words told the agent, in the authorized private view alone
        assert "would leave in clear" in detail["sensitive"]["block_reason"]
        assert GH_PAT not in detail["sensitive"]["block_reason"]
    else:
        assert detail["sensitive"] is None


def test_no_summary_carries_a_value_a_header_or_the_body(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        rewritten = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": PROMPT}]}
        assert client.post("/openai/v1/chat/completions", json=rewritten, headers={"authorization": KEY}).is_success
        checked = {"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": PROMPT}]}
        headers = {**CLAUDE, "authorization": SUBSCRIPTION}
        assert client.post("/anthropic/v1/messages", json=checked, headers=headers).status_code == 400
    public = _public(runtime)
    assert len(_summaries(runtime)) == 2
    for canary in (GH_PAT, EMAIL, WORD, SUBSCRIPTION, KEY, CC_SESSION, "GITHUB_PAT[40]", "would leave in clear"):
        assert canary not in public, canary


def _moving(runtime, times: int) -> None:
    """The rules move ``times`` times, each while a preparation runs: past the bound, it is refused because its settings
    changed (``settings-changed``, minors after 0.11, item 2 and the fix round's Q-M1)."""
    prepare, left = runtime.mediator.prepare_http, [times]

    def prepared(*args, **kwargs):
        if left[0] > 0:
            left[0] -= 1
            runtime.mediator.engine.rules_changed()
        return prepare(*args, **kwargs)

    runtime.mediator.prepare_http = prepared


def test_a_request_the_final_decision_refuses_without_words_is_stopped_by_its_category(runtime, monkeypatch):
    from latet.gateway import forward, request_transport

    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hello"}]}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        _moving(runtime, forward._REDERIVE + 1)
        assert client.post("/openai/v1/chat/completions", json=body, headers={"authorization": KEY}).status_code == 400
        monkeypatch.setattr(
            request_transport, "validate_send", lambda *_a, **_k: request_transport.SendRefusal("coverage-limit")
        )
        refused = client.post("/openai/v1/chat/completions", json=body, headers={"authorization": KEY})
        assert refused.status_code >= 400
    codes = [row["block_reason"] for row, _ in _summaries(runtime)]
    assert codes == ["coverage-limit", "settings-changed"]  # the newest first
    for row, detail in _summaries(runtime):
        assert "blocked" in row["flags"] and row["verified"] is False and row["overhead_ms"] is None
        assert detail["summary"]["block_reason"] == row["block_reason"]


@pytest.mark.parametrize("history", ["full", "metadata"])
def test_a_destination_of_ones_own_is_custom_and_named_on_its_owners_list_alone(tmp_path, upstream, history):
    """The summary says `custom` (its name and address keyed in the private view); the dashboard's list, read by the
    history's own owner, names it by the owner's own settings."""
    config = Config(
        identity=IdentityConfig(enabled=False),
        history=history,
        upstreams=[Destination(name="acme-llm", url=upstream.url)],
    )
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    try:
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": PROMPT}]}
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            sent = client.post("/acme-llm/v1/chat/completions", json=body, headers={"authorization": KEY})
            assert sent.is_success, sent.text
            assert rt.recorder is not None and rt.recorder.drain()
            _login(client, rt)
            [listed] = client.get("/_latet/api/v1/exchanges").json()["items"]
        assert (listed["upstream"], listed["host"], listed["destination"]) == ("custom", "custom", "acme-llm")
        [(row, detail)] = _summaries(rt)
        assert (row["upstream"], row["host"]) == ("custom", "custom") and "destination" not in row
        assert "acme-llm" not in _public(rt) and upstream.url.removeprefix("http://") not in _public(rt)
        if history == "full":  # keyed in the private view too
            assert detail["sensitive"]["upstream"].startswith("dest:v1:")
            assert detail["sensitive"]["host"].startswith("host:v1:")
    finally:
        rt.close()


def _login(client, rt) -> None:
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
    assert client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False).status_code == 303
