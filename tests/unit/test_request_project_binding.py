"""A request's project: trusted only through a binding Latet made, claimed otherwise (RC-REC-7, D5, D7).

A project named by a request (the ``x-latet-project`` header, or a folder its text says, ``cwd_from``) is a claim: its
rules that add protection apply, those that lower it do not, and a project where Latet is off refuses the request
(R-83). A project the plugin's bridge bound for a Claude Code session of a registered folder, or the folder of a
verified launch key, is trusted: its rules apply whole. Latet off in a trusted project refuses as well (D5): 0.11 sends
no request unread. The history names the project whose rules applied, and whether it was trusted or claimed, and a
claim binds no conversation."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from latet.config import Config, Destination, IdentityConfig, ProjectEntry, RequestException
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY

API = "/_latet/api/v1"
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
CC_SESSION = "6b1f0c2e-7a3d-4c58-9e10-2d4f6a8c0b13"
CLAUDE = {"user-agent": "claude-cli/2.1.316 (external, cli)", "x-claude-code-session-id": CC_SESSION}
CLAUDE_MODEL, GPT = "claude-sonnet-4-5", "gpt-4.1-mini"
# What D5's refusal says: Latet is off there, and how to reach the provider directly for that folder.
OFF_WORDS = ("Latet is off", "directly")
_ANSWER = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": GPT,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
}
_CLAUDE_ANSWER = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": CLAUDE_MODEL,
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class _Provider:
    """The providers, in memory: each request they receive, and an ordinary answer."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.headers: list[httpx.Headers] = []
        self.queries: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request.content)
        self.headers.append(request.headers)
        self.queries.append(request.url.query)
        answer = _CLAUDE_ANSWER if request.url.path.endswith("/messages") else _ANSWER
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Stream(json.dumps(answer)))


class _Stream(httpx.AsyncByteStream):
    def __init__(self, text: str) -> None:
        self.body = text.encode()

    async def __aiter__(self):
        yield self.body


@pytest.fixture
def provider() -> _Provider:
    return _Provider()


@pytest.fixture
def runtime(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False), ner="off"),
        data=tmp_path / "data",
        config_file=tmp_path / "config.yaml",
    )
    rt.gateway.upstreams = [
        Upstream("anthropic", "/anthropic", "https://api.anthropic.com"),
        Upstream("openai", "/openai", "https://api.openai.com"),
    ]
    rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        yield c


def _project(tmp_path, text: str = "", name: str = "acme"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_text(text, "utf-8")
    return root.resolve()


def _personal(runtime, root, **settings) -> None:
    entry = ProjectEntry(path=str(root), **settings)
    runtime.update_config(lambda c: c.model_copy(update={"projects": [entry]}))


def _login(client, runtime) -> None:
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": runtime.gateway.bridge_token})
    client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)


def _exchanges(client, runtime) -> list[dict]:
    runtime.recorder.drain()
    _login(client, runtime)
    return client.get(f"{API}/exchanges").json()["items"]


def _chat(client, content: str, *, claim: str | None = None, header: str | None = None) -> httpx.Response:
    """An OpenAI chat request (rewritten by Latet) whose folder is claimed by its text or by Latet's header."""
    messages = [{"role": "system", "content": f"Working directory: {claim}"}] if claim else []
    body = {"model": GPT, "messages": [*messages, {"role": "user", "content": content}]}
    headers = {"authorization": "Bearer sk-test"} | ({"x-latet-project": header} if header else {})
    return client.post("/openai/v1/chat/completions", json=body, headers=headers)


def _bind(client, runtime, root) -> None:
    """The plugin's bridge binds the Claude Code session to its folder, as its hooks do before a turn."""
    token = {"x-latet-token": runtime.gateway.bridge_token}
    bound = client.post(
        "/_latet/engine/v1/mask", json={"session": CC_SESSION, "cwd": str(root), "items": []}, headers=token
    )
    assert bound.status_code == 200


def _claude(client, content: str, *, system: str | None = None) -> httpx.Response:
    body = {"model": CLAUDE_MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": content}]}
    if system is not None:
        body["system"] = system
    return client.post("/anthropic/v1/messages", json=body, headers=CLAUDE)


# --- D5: Latet off in a project refuses its requests, trusted or claimed ------------------------------------------------


def test_a_claimed_project_that_is_off_is_refused_over_a_websocket(client, runtime, provider, tmp_path):
    """D5 (Minor 8): a socket whose header claims a project where Latet is off is refused at its handshake, close 1008
    with D5's words, before any provider is reached."""
    from starlette.websockets import WebSocketDisconnect

    root = _project(tmp_path)
    _personal(runtime, root, enabled=False)
    headers = {"host": "127.0.0.1:5283", "authorization": "Bearer sk-test", "x-latet-project": str(root)}
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/openai/v1/responses", headers=headers),
    ):
        pass
    assert closed.value.code == 1008 and "Latet is off" in (closed.value.reason or "")
    assert provider.received == []


def test_an_off_projects_websocket_is_recorded_as_its_refusal_over_http_is(client, runtime, provider, tmp_path):
    """D5, F1 of the 0.11 characterization (its row h11-project-off-ws): the socket refused at its handshake is in the
    history as the same refusal over HTTP is, its category and flags, its profile's route template, no header kept, as
    a handshake refused for its envelope is (``ws._envelope_closed``)."""
    from starlette.websockets import WebSocketDisconnect

    root = _project(tmp_path)
    _personal(runtime, root, enabled=False)
    assert _chat(client, "an ordinary question", header=str(root)).status_code == 400
    headers = {"host": "127.0.0.1:5283", "authorization": "Bearer sk-rc-socket", "x-latet-project": str(root)}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/openai/v1/responses", headers=headers):
        pass
    assert provider.received == []
    items = {item["transport"]: item for item in _exchanges(client, runtime)}
    assert set(items) == {"http", "ws"}
    over_http, over_ws = items["http"], items["ws"]
    assert over_ws["status"] == over_http["status"] == 400
    assert over_ws["block_reason"] == over_http["block_reason"] == "refused"
    assert {"blocked", "project-off"} <= set(over_ws["flags"]) and over_ws["verified"] is False
    assert over_http["path"] == "/{destination}/v1/chat/completions" and over_http["profile"] == "openai.chat.1"
    assert over_ws["path"] == "/{destination}/v1/responses" and over_ws["profile"] == "openai.responses.1.ws-envelope-1"
    detail = client.get(f"{API}/exchanges/{over_ws['id']}").json()
    assert detail["request"]["headers"] == [] and "sk-rc-socket" not in json.dumps(detail)


@pytest.mark.parametrize(
    "loosening",
    [
        {"allow_patterns": ["ghp_[A-Za-z0-9]{36}"]},
        {"policies": {"GITHUB_PAT": "allow"}},
    ],
    ids=["allow-patterns", "allow-policy"],
)
def test_a_claim_never_takes_a_projects_other_loosenings(client, runtime, provider, tmp_path, loosening):
    """D7 (Minor 8): beside ``allow_values``, each other way your settings for a project let a value through applies
    under the plugin's trusted binding alone; claimed by a header, the token is masked as anywhere else."""
    root = _project(tmp_path)
    _personal(runtime, root, **loosening)
    claimed = _chat(client, f"deploy with {GH_PAT}", header=str(root))
    assert claimed.status_code == 200 and GH_PAT.encode() not in provider.received[-1]
    _bind(client, runtime, root)
    trusted = _claude(client, f"deploy with {GH_PAT}")
    assert trusted.status_code == 200 and GH_PAT.encode() in provider.received[-1]


@pytest.mark.parametrize("how", ["trusted-plugin", "claimed-header", "claimed-text"])
def test_an_off_project_is_refused_over_http(client, runtime, provider, tmp_path, how):
    root = _project(tmp_path)
    _personal(runtime, root, enabled=False)
    if how == "trusted-plugin":
        _bind(client, runtime, root)
        answer = _claude(client, "an ordinary question")
    elif how == "claimed-header":
        answer = _chat(client, "an ordinary question", header=str(root))
    else:
        answer = _chat(client, "an ordinary question", claim=str(root))
    assert answer.status_code == 400
    assert all(words in answer.text for words in OFF_WORDS), answer.text
    assert provider.received == []  # nothing sent, unread or otherwise
    [summary] = _exchanges(client, runtime)
    assert "project-off" in summary["flags"] and "blocked" in summary["flags"] and summary["verified"] is False


# --- D7: a claim tightens, never loosens; the history says which -------------------------------------------------------


@pytest.mark.parametrize("how", ["header", "text"])
def test_a_claimed_folder_tightens_but_never_loosens(client, runtime, provider, tmp_path, how):
    # The repository adds a term; your own settings for the folder let a token through: a loosening.
    root = _project(tmp_path, "terms: [{value: Project Heron, type: PROJECT}]\n")
    _personal(runtime, root, allow_values=[GH_PAT])
    claim = {"header": str(root)} if how == "header" else {"claim": str(root)}
    sent = _chat(client, f"Project Heron ships with {GH_PAT}", **claim)
    assert sent.status_code == 200
    [out] = provider.received
    assert b"Project Heron" not in out  # the project's added term applies under a claim
    assert GH_PAT.encode() not in out  # its allowance does not: the token is masked as anywhere else
    [summary] = _exchanges(client, runtime)
    assert summary["project"] is not None and "project-claimed" in summary["flags"]


def test_a_claimed_project_is_recorded_and_binds_no_conversation(client, runtime, provider, tmp_path):
    root = _project(tmp_path, "terms: [{value: Project Heron, type: PROJECT}]\n")
    assert _chat(client, "status of Project Heron?", claim=str(root)).status_code == 200
    [summary] = _exchanges(client, runtime)
    project = summary["project"]
    assert project is not None and "project-claimed" in summary["flags"]
    listed = client.get(f"{API}/exchanges", params={"project": project["id"]}).json()["items"]
    assert [item["id"] for item in listed] == [summary["id"]]  # the dashboard finds it under its project
    assert all(s.project_id is None for s in runtime.built.engine.sessions())  # a claim binds no conversation


def test_the_plugins_trusted_binding_applies_tightenings_and_loosenings(client, runtime, provider, tmp_path):
    root = _project(tmp_path, "terms: [{value: Project Heron, type: PROJECT}]\n")
    _personal(runtime, root, allow_values=[GH_PAT])
    # Claimed by text alone, the allowance does not apply: Claude Code's request is checked and stopped.
    stopped = _claude(client, f"deploy with {GH_PAT}", system=f"Working directory: {root}")
    assert stopped.status_code == 400 and provider.received == []
    _bind(client, runtime, root)
    allowed = _claude(client, f"deploy with {GH_PAT}")
    assert allowed.status_code == 200 and GH_PAT.encode() in provider.received[-1]
    heron = _claude(client, "status of Project Heron?")
    assert heron.status_code == 400  # the project's added term still stops it, trusted or not
    summaries = _exchanges(client, runtime)
    assert all(s["project"] is not None for s in summaries)
    bindings = sorted("claimed" if "project-claimed" in s["flags"] else "trusted" for s in summaries)
    assert bindings == ["claimed", "trusted", "trusted"], json.dumps(bindings)


# --- a launch's key and a claim: projects are compared, not folders (R-105, D7) ---------------------------------------


@contextmanager
def _launched(client, runtime, folder):
    """A launch Latet opened for ``folder``, held, as a launcher holds it: the header carrying its key."""
    token = {"x-latet-token": runtime.gateway.bridge_token}
    opened = client.post("/_latet/engine/v1/launches", headers=token, json={"folder": str(folder)})
    assert opened.status_code == 200
    fields = opened.json()
    hold = runtime.launch_keys.reserve_hold(fields["launch_id"], "computer", 0)
    assert hold.acknowledge() == 1
    try:
        yield {"x-latet-launch-key": fields["key"]}
    finally:
        hold.stop()
        hold.close()


@pytest.mark.parametrize("where", ["same-project-subfolder", "another-project-inside"])
def test_a_launch_key_with_a_claim_compares_projects_not_folders(client, runtime, provider, tmp_path, where):
    root = _project(tmp_path)
    (root / "src").mkdir()
    inner = root / "sub"
    inner.mkdir()
    (inner / "latet.yaml").write_text("terms: [{value: Project Heron, type: PROJECT}]\n", "utf-8")  # its own project
    _personal(runtime, root, allow_values=[GH_PAT])
    claim = root / "src" if where == "same-project-subfolder" else inner
    with _launched(client, runtime, root) as key:
        body = {"model": GPT, "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
        headers = {"authorization": "Bearer sk-test", "x-latet-project": str(claim), **key}
        answer = client.post("/openai/v1/chat/completions", json=body, headers=headers)
    if where == "same-project-subfolder":  # the key's project: its loosening applies
        assert answer.status_code == 200 and GH_PAT.encode() in provider.received[-1]
    else:  # another project: refused, never the key's loosenings with the other's added rules skipped
        assert answer.status_code == 400 and "launch authority refused" in answer.text and provider.received == []


# --- request exceptions at the envelope: exact, approved, visible, and project-scoped only when trusted --------------

ACME = "llm-acme"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _acme(runtime, *exceptions: dict, clock: datetime = NOW, decided: datetime | None = None) -> list:
    """A company destination of yours, with ``exceptions`` approved through the approval flow at ``clock``."""
    runtime.exception_clock = lambda: clock
    destination = Destination(name=ACME, url="https://llm.acme.example")
    runtime.update_config(lambda c: c.model_copy(update={"upstreams": [destination]}))
    if exceptions:
        rows = [
            RequestException.model_validate({"destination": ACME, "profile": "openai.chat.1", **e}) for e in exceptions
        ]
        change = runtime.propose(runtime.config.model_copy(update={"request_exceptions": rows}), "command")
        assert change is not None and runtime.decide_change(change.id, approve=True) == "approved"
    when = decided or clock + timedelta(hours=1)  # the requests' decisions: an hour later unless said
    runtime.exception_clock = lambda: when
    return list(runtime.config.request_exceptions)


def _team(name: str = "x-team", role: str = "checked", **more) -> dict:
    return {"components": [{"kind": "header", "name": name, "role": role}], "reason": "custom-header", **more}


def _acme_chat(client, content: str = "an ordinary question", **headers) -> httpx.Response:
    body = {"model": GPT, "messages": [{"role": "user", "content": content}]}
    return client.post(f"/{ACME}/v1/chat/completions", json=body, headers={"authorization": "Bearer k", **headers})


def test_a_users_own_header_is_refused_naming_it_until_an_exception_names_it(client, runtime, provider):
    _acme(runtime)
    refused = _acme_chat(client, **{"x-team": "data-science"})
    assert refused.status_code == 400 and "x-team" in refused.text and provider.received == []  # D4: named
    [row] = _acme(runtime, _team())
    sent = _acme_chat(client, **{"x-team": "data-science"})
    assert sent.status_code == 200 and provider.headers[-1]["x-team"] == "data-science"
    assert row.id is not None and row.description is None


def test_a_checked_header_value_still_passes_the_final_check(client, runtime, provider):
    _acme(runtime, _team())
    assert _acme_chat(client, **{"x-team": f"deploy {GH_PAT}"}).status_code == 400 and provider.received == []


def test_a_destination_credential_header_is_sent_as_it_came_and_never_read(client, runtime, provider):
    _acme(runtime, _team("x-acme-key", "credential", reason="destination-credential"))
    secret = "acme_live_" + "Q" * 32
    assert _acme_chat(client, **{"x-acme-key": secret}).status_code == 200
    assert provider.headers[-1]["x-acme-key"] == secret


def test_a_credential_an_exception_names_is_never_recorded_in_clear(runtime, provider, tmp_path):
    """I-3, D4: a destination's credential that an exception lets through unread is never kept or shown in clear, in
    the history or in a capture, whatever its header's name (this one has no secret marker): redacted by its role."""
    runtime.gateway.capture_dir = tmp_path / "capture"
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        _acme(runtime, _team("x-acme-pass", "credential", reason="destination-credential"))
        secret = "acme_live_" + "Q" * 32
        assert _acme_chat(client, **{"x-acme-pass": secret}).status_code == 200
        assert provider.headers[-1]["x-acme-pass"] == secret  # sent as it came
        [item] = _exchanges(client, runtime)
        detail = client.get(f"{API}/exchanges/{item['id']}")
        assert detail.status_code == 200 and "x-acme-pass" in detail.text and secret not in detail.text
    captured = "".join(path.read_text("utf-8") for path in (tmp_path / "capture").glob("*.jsonl"))
    assert "x-acme-pass" in captured and secret not in captured


def test_an_exception_never_loosens_beyond_what_it_names(client, runtime, provider):
    _acme(runtime, _team())
    assert _acme_chat(client, **{"x-team": "a", "x-cost": "b"}).status_code == 400  # another header: refused
    body = {"model": GPT, "input": "an ordinary question"}
    responses = client.post(f"/{ACME}/v1/responses", json=body, headers={"authorization": "Bearer k", "x-team": "a"})
    assert responses.status_code == 400  # another profile of the same destination: refused
    assert _chat(client, "hello", header=None).status_code == 200  # (the provider's own route still works)
    openai = client.post(
        "/openai/v1/chat/completions",
        json={"model": GPT, "messages": [{"role": "user", "content": "hi"}]},
        headers={"authorization": "Bearer sk-test", "x-team": "a"},
    )
    assert openai.status_code == 400  # another destination: refused
    assert all(b"x-cost" not in r for r in provider.received)


def test_a_query_parameter_exception_names_it_exactly(client, runtime, provider):
    _acme(runtime, {"components": [{"kind": "query", "name": "tenant"}], "reason": "query-parameter"})
    body = {"model": GPT, "messages": [{"role": "user", "content": "hi"}]}
    sent = client.post(f"/{ACME}/v1/chat/completions?tenant=acme", json=body, headers={"authorization": "Bearer k"})
    assert sent.status_code == 200 and provider.queries[-1] == b"tenant=acme"
    other = client.post(f"/{ACME}/v1/chat/completions?region=eu", json=body, headers={"authorization": "Bearer k"})
    assert other.status_code == 400


def test_a_refusal_after_expiry_names_the_expired_exception(client, runtime, provider):
    [row] = _acme(runtime, _team(), clock=NOW - timedelta(days=31), decided=NOW)
    refused = _acme_chat(client, **{"x-team": "data-science"})
    assert refused.status_code == 400 and provider.received == []
    assert row.id in refused.text and "expired" in refused.text
    assert (NOW - timedelta(days=1)).date().isoformat() in refused.text  # its date, never its description


def test_a_refusal_after_admission_is_recorded_with_its_category_and_no_header(client, runtime, provider):
    """An envelope refused once its request was prepared (an exception that ended, judged by the decision's clock) is
    in the history as one refused at its admission is (D4, R-79; P2-8a): its category, no header kept."""
    _acme(runtime, _team(), clock=NOW - timedelta(days=31), decided=NOW)
    assert _acme_chat(client, **{"x-team": "data-science"}).status_code == 400 and provider.received == []
    [item] = _exchanges(client, runtime)
    assert item["block_reason"] == "envelope-exception-expired"
    detail = client.get(f"{API}/exchanges/{item['id']}")
    assert detail.status_code == 200 and "data-science" not in detail.text and "Bearer k" not in detail.text


def test_a_permanent_exception_never_expires(client, runtime, provider):
    _acme(runtime, _team(permanent=True), clock=NOW - timedelta(days=400), decided=NOW)
    assert _acme_chat(client, **{"x-team": "data-science"}).status_code == 200


def test_an_exception_approved_after_the_decisions_clock_grants_nothing(client, runtime, provider):
    """Minor 5: an approval time ahead of the decision's clock is never Latet's stamp (a hand-written one, a clock set
    ahead): it would stretch the exception's lifetime past 90 days. It grants nothing."""
    _acme(runtime, _team(), clock=NOW + timedelta(days=60), decided=NOW)
    refused = _acme_chat(client, **{"x-team": "data-science"})
    assert refused.status_code == 400 and "x-team" in refused.text and provider.received == []


def test_a_header_naming_a_project_confers_no_project_exception(client, runtime, provider, tmp_path):
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root)))
    claimed = _acme_chat(client, **{"x-team": "data-science", "x-latet-project": str(root)})
    assert claimed.status_code == 400 and provider.received == []


def test_body_text_naming_a_project_confers_no_project_exception(client, runtime, provider, tmp_path):
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root)))
    body = {
        "model": GPT,
        "messages": [{"role": "system", "content": f"Working directory: {root}"}, {"role": "user", "content": "hi"}],
    }
    claimed = client.post(
        f"/{ACME}/v1/chat/completions", json=body, headers={"authorization": "Bearer k", "x-team": "data-science"}
    )
    assert claimed.status_code == 400 and provider.received == []


def test_a_verified_launch_key_binds_its_project(client, runtime, provider, tmp_path):
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root)))
    with _launched(client, runtime, root) as key:
        sent = _acme_chat(client, **{"x-team": "data-science", **key})
    assert sent.status_code == 200 and provider.headers[-1]["x-team"] == "data-science"
    other = _project(tmp_path, name="elsewhere")
    _personal(runtime, other)
    with _launched(client, runtime, other) as key:  # another project's key: no exception of this project
        assert _acme_chat(client, **{"x-team": "data-science", **key}).status_code == 400


def test_identity_level_exceptions_apply_to_unbound_sessions(client, runtime, provider):
    _acme(runtime, _team())
    assert _acme_chat(client, **{"x-team": "data-science"}).status_code == 200  # no project, no binding: applies


def test_an_exception_whose_visibility_cannot_be_reserved_sends_nothing(client, runtime, provider, monkeypatch):
    """M1 (P2-5's carry): an exception in use is visible while it is used; when its status cannot be reserved, the
    request is refused (503) rather than sent unseen."""
    _acme(runtime, _team())
    monkeypatch.setattr(type(runtime.request_audit), "reserve_exception", lambda self, reference: None)
    refused = _acme_chat(client, **{"x-team": "data-science"})
    assert refused.status_code == 503 and provider.received == []


# --- the plugin's binding lives while the plugin calls the bridge (R-85, R-88, R-91) ------------------------------------


class _Clock:
    """The binding's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    from latet.gateway import local_launch

    fake = _Clock()
    monkeypatch.setattr(local_launch, "plugin_clock", fake)
    return fake


GH_PAT2 = "ghp_" + "W3nL6hJ1cF5dS0aY2eU4iR7tK2pQ9xZ4mW8v"  # another token the project lets through


def _bound_project(runtime, tmp_path):
    root = _project(tmp_path, "terms: [{value: Project Heron, type: PROJECT}]\n")
    _personal(runtime, root, allow_values=[GH_PAT, GH_PAT2])
    return root


def test_an_idle_binding_gives_identity_exceptions_only(client, runtime, provider, tmp_path, clock):
    root = _bound_project(runtime, tmp_path)
    _bind(client, runtime, root)
    clock.now += 29 * 60
    assert _claude(client, f"deploy with {GH_PAT}").status_code == 200  # bound: the project's loosening applies
    clock.now += 31 * 60  # half an hour after the bridge's last call: the binding has expired
    assert _claude(client, f"deploy with {GH_PAT2}").status_code == 400  # no loosening any more (D7)
    assert len(provider.received) == 1


def test_a_bridge_call_refreshes_the_binding(client, runtime, provider, tmp_path, clock):
    root = _bound_project(runtime, tmp_path)
    _bind(client, runtime, root)
    clock.now += 25 * 60
    _bind(client, runtime, root)  # the plugin's next turn: its hook calls the bridge
    clock.now += 25 * 60
    assert _claude(client, f"deploy with {GH_PAT}").status_code == 200


def test_a_gateway_request_never_refreshes_the_binding(client, runtime, provider, tmp_path, clock):
    root = _bound_project(runtime, tmp_path)
    _bind(client, runtime, root)
    clock.now += 20 * 60
    assert _claude(client, f"deploy with {GH_PAT}").status_code == 200
    clock.now += 15 * 60  # 35 minutes after the bridge's call, 15 after the gateway's request
    assert _claude(client, f"deploy with {GH_PAT2}").status_code == 400


def test_claude_codes_calls_before_the_plugins_first_binding_bridge_call_are_identity_level(
    client, runtime, provider, tmp_path, clock
):
    """P1-0 D-13, F-5: before the plugin's first bridge call binds the session, its calls get the person's own rules
    alone: neither the project's added rules nor its loosenings, and they are not held."""
    _bound_project(runtime, tmp_path)
    assert _claude(client, f"deploy with {GH_PAT}").status_code == 400  # no loosening of the project
    assert _claude(client, "status of Project Heron?").status_code == 200  # nor its added term
    [summary, *_] = _exchanges(client, runtime)
    assert summary["project"] is None


# --- body members an exception names (body_selectors, wired in here) ---------------------------------------------------


def _tenant(**more) -> dict:
    return {
        "components": [{"kind": "body", "name": "/x_tenant", "type": "string"}],
        "reason": "other",
        **more,
    }


def _tenant_chat(client, value: str = "acme", **headers) -> httpx.Response:
    body = {"model": GPT, "messages": [{"role": "user", "content": "hi"}], "x_tenant": value}
    return client.post(f"/{ACME}/v1/chat/completions", json=body, headers={"authorization": "Bearer k", **headers})


@pytest.fixture
def decisions(monkeypatch) -> list:
    from latet.gateway import forward

    seen: list = []
    real = forward.finalize

    def kept(*args, **kwargs):
        decision = real(*args, **kwargs)
        seen.append(decision)
        return decision

    monkeypatch.setattr(forward, "finalize", kept)
    return seen


def test_a_body_member_an_exception_names_passes_checked_and_the_pass_names_it(client, runtime, provider, decisions):
    from latet.gateway.outcome import Pass

    _acme(runtime)
    assert _tenant_chat(client).status_code == 400 and provider.received == []  # unnamed: refused
    [row] = _acme(runtime, _tenant())
    assert row.body_revision == "chat.body.1"  # stamped with its body protocol's revision (R-100)
    sent = _tenant_chat(client)
    assert sent.status_code == 200 and json.loads(provider.received[-1])["x_tenant"] == "acme"
    passed = decisions[-1]
    assert type(passed) is Pass and passed.evidence.exceptions == (row.id,)
    assert _tenant_chat(client, f"deploy {GH_PAT}").status_code == 400  # checked by the final check, never masked


def test_a_body_exception_of_a_project_applies_only_where_latet_trusts_it(client, runtime, provider, tmp_path):
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _tenant(project=str(root)))
    assert _tenant_chat(client, **{"x-latet-project": str(root)}).status_code == 400  # claimed: none
    with _launched(client, runtime, root) as key:
        assert _tenant_chat(client, **key).status_code == 200  # the launch's project: it applies


def test_an_expired_body_exception_is_refused_naming_it(client, runtime, provider):
    [row] = _acme(runtime, _tenant(), clock=NOW - timedelta(days=31), decided=NOW)
    refused = _tenant_chat(client)
    assert refused.status_code == 400 and row.id in refused.text and provider.received == []


def test_a_body_revision_that_changes_a_named_member_suspends_its_exception(client, runtime, provider, monkeypatch):
    from latet.gateway import body_selectors, request_policy

    _acme(runtime, _tenant())
    changed = {
        **body_selectors.HISTORY,
        "chat": (*body_selectors.HISTORY["chat"], body_selectors.BodyRevision("chat.body.2", ("/x_tenant",))),
    }
    monkeypatch.setattr(body_selectors, "HISTORY", changed)
    assert request_policy.in_force(runtime.config, NOW) == ()
    [listed] = request_policy.listed(runtime.config, NOW)
    assert listed["state"] == "needs approval again (profile revised)"


# --- the plugin's binding as the source of a project's exceptions (RC-REC-7, R-73) --------------------------------------

ON_MESSAGES = {"destination": "anthropic", "profile": "anthropic.messages.1"}


def _claude_team(client, **more) -> httpx.Response:
    body = {"model": CLAUDE_MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "an ordinary question"}]}
    return client.post("/anthropic/v1/messages", json=body, headers={**CLAUDE, "x-team": "data-science", **more})


def test_a_plugin_bound_session_of_a_registered_project_uses_its_exception(client, runtime, provider, tmp_path):
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root), **ON_MESSAGES))
    assert _claude_team(client).status_code == 400  # not bound yet: identity-level alone
    _bind(client, runtime, root)
    assert _claude_team(client).status_code == 200 and provider.headers[-1]["x-team"] == "data-science"


def test_a_bound_session_id_from_another_profile_gets_identity_exceptions_only(client, runtime, provider, tmp_path):
    """R-73: a Chat Completions request carrying a plugin-bound session's id reaches no binding: the project's
    exception does not apply to it."""
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root), destination="openai"))
    _bind(client, runtime, root)
    body = {"model": GPT, "messages": [{"role": "user", "content": "hi"}]}
    headers = {"authorization": "Bearer sk-test", "x-claude-code-session-id": CC_SESSION, "x-team": "data-science"}
    assert client.post("/openai/v1/chat/completions", json=body, headers=headers).status_code == 400
    assert provider.received == []


def test_a_session_moved_between_folders_cannot_keep_the_first_folders_exception(client, runtime, provider, tmp_path):
    first, second = _project(tmp_path, name="first"), _project(tmp_path, name="second")
    entries = [ProjectEntry(path=str(first)), ProjectEntry(path=str(second))]
    runtime.update_config(lambda c: c.model_copy(update={"projects": entries}))
    _acme(runtime, _team(project=str(first), **ON_MESSAGES))
    _bind(client, runtime, first)
    assert _claude_team(client).status_code == 200
    _bind(client, runtime, second)  # the session's folder is the second project's now
    assert _claude_team(client).status_code == 400 and len(provider.received) == 1


def test_an_unregistered_folder_binds_no_project_exception(client, runtime, provider, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()  # no latet.yaml, no settings of yours: not a registered project
    _acme(runtime, _team(project=str(plain.resolve()), **ON_MESSAGES))
    _bind(client, runtime, plain.resolve())
    assert _claude_team(client).status_code == 400 and provider.received == []


def test_two_projects_with_their_own_exception_for_one_header_each_get_theirs(client, runtime, provider, tmp_path):
    """I-5: each project's exception for the same header applies to that project's trusted requests, whichever comes
    first in the settings; a third project gets neither."""
    first, second, third = (_project(tmp_path, name=name) for name in ("first", "second", "third"))
    entries = [ProjectEntry(path=str(folder)) for folder in (first, second, third)]
    runtime.update_config(lambda c: c.model_copy(update={"projects": entries}))
    _acme(runtime, _team(project=str(first), **ON_MESSAGES), _team(project=str(second), **ON_MESSAGES))
    _bind(client, runtime, second)
    assert _claude_team(client).status_code == 200
    _bind(client, runtime, first)
    assert _claude_team(client).status_code == 200
    _bind(client, runtime, third)
    assert _claude_team(client).status_code == 400 and len(provider.received) == 2


def test_a_header_two_exceptions_name_in_two_roles_is_checked(client, runtime, provider, tmp_path):
    """I-5: a header the identity's exception names as checked and a project's as a credential is checked, whichever
    applies: the one that applies never lets more through than it names (here the identity's, which applies to every
    request)."""
    root = _project(tmp_path)
    _personal(runtime, root)
    _acme(runtime, _team(**ON_MESSAGES), _team(role="credential", project=str(root), **ON_MESSAGES))
    _bind(client, runtime, root)
    assert _claude_team(client).status_code == 200
    assert _claude_team(client, **{"x-team": f"deploy {GH_PAT}"}).status_code == 400  # read, and stopped
    assert all(GH_PAT.encode() not in str(headers).encode() for headers in provider.headers)


def test_a_project_exceptions_folder_is_never_compared_on_the_event_loop(
    client, runtime, provider, tmp_path, monkeypatch
):
    """I-6: comparing a project exception's folder with the request's project reads a computer's repository (its
    latet.yaml, its .git): it is done where the request is prepared, never on the gateway's event loop, which a share
    that hangs would stop for every agent."""
    from latet.projects import Projects

    root = _project(tmp_path)
    (root / "src").mkdir()
    _personal(runtime, root)
    _acme(runtime, _team(project=str(root / "src"), **ON_MESSAGES))  # a subfolder of the project: compared, R-105
    compared = Projects.compared_id
    on_loop: list[str] = []

    def watched(self, folder: str) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            on_loop.append(folder)
        return compared(self, folder)

    monkeypatch.setattr(Projects, "compared_id", watched)
    _bind(client, runtime, root)
    assert _claude_team(client).status_code == 200 and provider.headers[-1]["x-team"] == "data-science"
    assert on_loop == []


# --- a change of the rules while a request is decided (generation, RC-REC-3) --------------------------------------------


@pytest.mark.parametrize(
    "when", ["while-prepared", "expired-while-reviewed", "before-send", "freshness-unavailable", "unread-after-review"]
)
def test_policy_change_invalidates_waiting_approval_retry_and_socket(client, runtime, provider, monkeypatch, when):
    """An exception revoked while a request that uses it is prepared changes its route (the exception was bound to it):
    the request keeps its refusal, never sent on the earlier rules; one that ends while its request waits for your
    answer (the Live Reviewer waits until you answer) is judged by the clock of its send's decision, after the wait, and
    refused naming it; revoked after its decision and before its send, the decision is stale and nothing is sent; when
    the state an exception is judged by cannot be read, before it is prepared or after its review, the request is
    refused (503), never sent on the exception's word. (A WebSocket takes no exception in 0.11.0, F-2; an AWS retry
    keeps its first attempt's authority, P2-4.)"""
    from latet.gateway import request_transport

    if when == "expired-while-reviewed":  # it ends 30 minutes after the request is admitted
        [row] = _acme(runtime, _team(), clock=NOW - timedelta(days=30) + timedelta(minutes=30), decided=NOW)
    else:
        [row] = _acme(runtime, _team())

    def revoke() -> None:
        runtime.update_config(lambda c: c.model_copy(update={"request_exceptions": []}))

    prepared: list[int] = []
    if when == "while-prepared":
        real = runtime.mediator.prepare_http

        def preparing(*args, **kwargs):
            prepared.append(1)
            if len(prepared) == 1:
                revoke()  # committed while the first preparation runs
            return real(*args, **kwargs)

        monkeypatch.setattr(runtime.mediator, "prepare_http", preparing)
    elif when == "before-send":
        real_send = request_transport.send

        async def revoked_first(http, built):
            await asyncio.to_thread(revoke)  # after its decision, before the provider is reached
            return await real_send(http, built)

        monkeypatch.setattr(request_transport, "send", revoked_first)
    elif when in ("expired-while-reviewed", "unread-after-review"):
        real_live = runtime.mediator.live_prepared

        async def answered_later(prepared, *args, **kwargs):
            if when == "expired-while-reviewed":
                runtime.exception_clock = lambda: NOW + timedelta(hours=2)  # you answer two hours later
            else:

                async def unreadable():
                    raise OSError("the store does not answer")

                monkeypatch.setattr(runtime.gateway, "exception_fresh", unreadable)
            return await real_live(prepared, *args, **kwargs)

        monkeypatch.setattr(runtime.mediator, "live_prepared", answered_later)
    else:

        async def unreadable():
            raise OSError("the store does not answer")

        monkeypatch.setattr(runtime.gateway, "exception_fresh", unreadable)
    answer = _acme_chat(client, **{"x-team": "data-science"})
    assert provider.received == []  # never sent on a revoked or unjudged exception
    if when == "while-prepared":
        assert answer.status_code == 400 and len(prepared) == 1  # its route changed meanwhile: refused
    elif when == "expired-while-reviewed":
        assert answer.status_code == 400 and row.id in answer.text and "expired" in answer.text
    elif when == "before-send":
        assert answer.status_code >= 400
    else:
        assert answer.status_code == 503
