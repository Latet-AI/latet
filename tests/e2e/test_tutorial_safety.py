"""The tutorial's sandbox is safe by construction, not by a switch. Outside it there is no tutorial. Inside it the
pretend model is the only place a request can go: no other route, no WebSocket, no destination however the settings
are changed; what would reach outside the sandbox is refused; the agents of this computer are neither run nor read;
its sign-in cookie is its own; it is made in its own folder only, never in or around the user's; and no chapter
reaches the network."""

import asyncio
import os

import httpx
import platformdirs
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from latet import runtime as runtime_module
from latet.config import Config, Destination, IdentityConfig
from latet.engine.vault import derive_keys
from latet.gateway import ws
from latet.gateway.app import create_app
from latet.integrations import agents
from latet.runtime import make_runtime
from latet.tutorial.chapters import load_all
from latet.tutorial.model import PretendUpstream
from latet.tutorial.progress import Progress
from latet.tutorial.sandbox import CONFIG, DATA, ROOT_KEY, Sandbox, SandboxError

from .tutorial_sandbox import API, PORT, TUTORIAL, UI, login, opened, play, switch, written

pytestmark = pytest.mark.e2e

SANDBOXED = "not in the tutorial's sandbox"


def test_outside_the_sandbox_there_is_no_tutorial(tmp_path):
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml"
    )
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            assert "tutorial" not in client.get("/_latet/health").json()
            code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            signed = client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
            assert signed.headers["set-cookie"].startswith("latet_session=")  # the daemon's own name
            assert client.get(f"{API}/me").json()["tutorial"] is None
            for method, path in (
                ("GET", f"{TUTORIAL}/chapters"),
                ("GET", f"{TUTORIAL}/chapters/basics"),
                ("POST", f"{TUTORIAL}/chapters/basics/start"),
                ("POST", f"{TUTORIAL}/chapters/basics/next"),
                ("GET", f"{TUTORIAL}/chapters/basics/state"),
                ("GET", f"{TUTORIAL}/chapters/basics/pictures/payment.png"),
                ("POST", f"{TUTORIAL}/end"),
            ):
                assert client.request(method, path, headers=UI).status_code == 404, path
    finally:
        rt.close()


def test_a_runtime_made_without_a_key_uses_this_computer_s(tmp_path, monkeypatch):
    """Only the sandbox passes its fixed key: any other runtime loads the key of this computer, as before."""
    asked = []

    def load(data):
        asked.append(data)
        return b"\x01" * 32

    monkeypatch.setattr(runtime_module, "load_root_key", load)
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml"
    )
    try:
        assert asked == [tmp_path / "data"]
        assert rt.built.vault.cache_key == derive_keys(b"\x01" * 32)["cache"] != derive_keys(ROOT_KEY)["cache"]
    finally:
        rt.close()


async def test_the_pretend_model_is_the_only_place_a_request_can_go(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        assert (await client.get("/_latet/health")).json()["tutorial"] is True
        for path in (
            "/openai/v1/chat/completions",
            "/backend-api/codex/responses",
            "/openrouter/api/v1/chat/completions",
            "/v1/messages",
        ):
            answer = await client.post(path, json={"model": "m", "messages": []}, headers={"x-api-key": "k"})
            assert answer.status_code == 404, path
        # A destination in the settings, written there by hand: still no route to it.
        company = Destination(name="company", url="https://llm.example.com")
        await asyncio.to_thread(rt.apply_config, rt.config.model_copy(update={"upstreams": [company]}), False)
        assert (await client.post("/company/v1/messages", json={})).status_code == 404
        assert rt.gateway.routes is not None and rt.gateway.routes.upstreams == [PretendUpstream()]
        # /anthropic reaches the pretend model, which answers its Messages API only.
        answer = await client.get("/anthropic/v1/models", headers={"x-api-key": "k"})
        assert answer.status_code == 404 and "Tutorial Model" in answer.text


def test_no_websocket_reaches_anything(tmp_path, monkeypatch):
    root = tmp_path / "sandbox"
    switch(monkeypatch, root)
    sandbox = Sandbox.create(root, PORT, progress=Progress(tmp_path / "tutorial.json"))
    try:
        with TestClient(create_app(sandbox.runtime.gateway), base_url=f"http://127.0.0.1:{PORT}") as client:
            for path in ("/anthropic/v1/messages", "/openai/v1/responses", "/backend-api/codex/responses"):
                headers = {"host": f"127.0.0.1:{PORT}", "session-id": "s-1"}
                with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(path, headers=headers):
                    pass
                assert closed.value.code == 4404, path
    finally:
        sandbox.close()


async def test_the_settings_cannot_add_a_destination_turn_the_local_reviewer_on_or_read_your_identity(
    tmp_path, monkeypatch
):
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        view = (await client.get(f"{API}/config")).json()
        del view["etag"]  # left out: carried back after the first save below, it would refuse the others as stale
        # The Rules page saves its whole draft: what it leaves as it is saves with the rest.
        draft = {**view, "terms": [{"value": "Project Falcon", "type": "CUSTOM"}]}
        assert (await client.put(f"{API}/config", json=draft, headers=UI)).status_code == 200
        company = [{"name": "company", "url": "https://llm.example.com"}]
        reviewer = {"llm_review": {**view["llm_review"], "enabled": True}}
        for change in ({"upstreams": company}, reviewer, {"identity": {"enabled": True}}):
            answer = await client.put(f"{API}/config", json={**view, **change}, headers=UI)
            assert answer.status_code == 409 and answer.json()["error"].startswith(SANDBOXED), change
        # The Live Reviewer is real in the sandbox: a chapter turns it on.
        live = {"live_review": {**view["live_review"], "enabled": True}}
        assert (await client.put(f"{API}/config", json=live, headers=UI)).status_code == 200
        assert rt.config.upstreams == [] and not rt.config.llm_review.enabled and not rt.config.identity.enabled
        assert rt.config.live_review.enabled and [t.value for t in rt.config.terms] == ["Project Falcon"]


# The settings the sandbox keeps for itself, each as the agent's `latet config set` would change it.
OWN_SETTINGS = {
    "llm_review": "latet config set llm_review.enabled true",
    "upstreams": "latet config set upstreams \"[{name: company, url: 'https://llm.example.com'}]\"",
    "port": "latet config set port 5284",
    "capture_dir": "latet config set capture_dir ../captures",
    "identity": "latet config set identity.enabled true",
    "projects": "latet config set projects --unset",
}


async def test_the_agent_s_commands_cannot_change_what_the_sandbox_keeps_for_itself(tmp_path, monkeypatch):
    """The agent's `latet config set` makes a change as the CLI makes it, and is refused every setting the sandbox
    keeps for itself: its port and only route, no folder outside it, nobody's identity, no reviewer. Turning the
    local reviewer on lowers nothing, so it would apply at once, and its server is outside the sandbox; a capture
    folder would be written outside it. Refused before config.yaml is written: nothing waits, nothing is left to
    apply."""
    commands = [*OWN_SETTINGS.values(), "latet config set llm_review.url http://127.0.0.1:9/v1"]
    turns = [{"id": f"run-{i}", "tool": {"name": "Bash", "input": {"command": c}}} for i, c in enumerate(commands)]
    chapter = {
        "id": "settings",
        "title": "Reaching out",
        "minutes": 1,
        "phase": 1,
        "learn": ["one", "two", "three"],
        "steps": [
            {
                "kind": "watch",
                "title": "Out of the sandbox",
                "prompt": "make the reviews faster",
                "button": "Send it",
                "model": [*turns, {"id": "done", "text": "Done."}],
                "explain": "Refused.",
            }
        ],
        "end": {"learned": ["a", "b", "c"], "next": "basics"},
    }
    written(tmp_path / "chapters", monkeypatch, chapter)
    async with opened(tmp_path, monkeypatch) as box:
        rt = box.sandbox.runtime
        saved = rt.config_file.read_bytes()
        last = await play(box, "settings", checked=False)
        refused = [*OWN_SETTINGS, "llm_review"]
        assert [line["output"] for line in last["lines"] if line["type"] == "tool"] == [
            f"Error: {SANDBOXED}: `{key}` is the sandbox's own" for key in refused
        ]
        assert not rt.config.llm_review.enabled and rt.mediator.reviewer is None and rt.config.upstreams == []
        assert rt.config_file.read_bytes() == saved and rt.mediator.approvals.changes == {}


REFUSED = [
    ("POST", "/reviewer/test", {}),
    ("PUT", "/reviewer/key", {"key": "a-reviewer-key"}),
    ("DELETE", "/reviewer/key", None),
    ("POST", "/reviewer/certificate", {"url": "https://reviewer.example.test"}),
    ("PUT", "/reviewer/certificate", {"url": "https://reviewer.example.test", "fingerprint": "AB:CD"}),
    ("POST", "/recognizers/draft", {"description": "an order number"}),
    ("POST", "/exchanges/ex_0/columns/review?slot=0", {}),
    ("PUT", "/projects/p_0/repo", {"settings": {}, "tag": ""}),
]


@pytest.mark.parametrize(("method", "path", "body"), REFUSED, ids=[f"{m} {p}" for m, p, _ in REFUSED])
async def test_what_would_reach_outside_the_sandbox_is_refused(tmp_path, monkeypatch, method, path, body):
    async with opened(tmp_path, monkeypatch) as box:
        answer = await box.client.request(method, f"{API}{path}", json=body, headers=UI)
        assert answer.status_code == 409 and answer.json()["error"].startswith(SANDBOXED)


async def test_no_agent_s_address_is_added_as_a_destination(tmp_path, monkeypatch):
    """`latet claude` and `global-install` add the addresses an agent's settings name: real ones."""
    async with opened(tmp_path, monkeypatch) as box:
        rt = box.sandbox.runtime
        token = {"x-latet-token": rt.gateway.bridge_token or ""}
        wanted = {"destinations": [{"url": "https://llm.example.com", "agent": "claude"}]}
        answer = await box.client.post("/_latet/admin/destinations", json=wanted, headers=token)
        assert answer.status_code == 409 and answer.json()["error"].startswith(SANDBOXED)
        assert rt.config.upstreams == []


async def test_integrations_shows_the_sandbox_and_looks_at_no_agent_of_this_computer(tmp_path, monkeypatch):
    def looked(*_args):
        raise AssertionError("an agent of this computer was looked at")

    for agent in agents.ALL.values():
        monkeypatch.setattr(type(agent), "detect", looked)
    async with opened(tmp_path, monkeypatch) as box:
        answer = await box.client.get(f"{API}/integrations")
        assert answer.status_code == 200
        view = answer.json()
        assert [(a["id"], a["name"], a["installed"]) for a in view["agents"]] == [
            ("tutorial-agent", "Tutorial Agent", True)
        ]
        assert view["destinations"] == [] and [c["status"] for c in view["checks"]] == ["ok"]


async def test_the_sign_in_cookie_is_the_sandbox_s_own(tmp_path, monkeypatch):
    """The cookie of the dashboard on 127.0.0.1 goes to every port: the sandbox's has a name of its own, so neither
    sign-in takes the other's place."""
    async with opened(tmp_path, monkeypatch) as box:
        signed = await login(box.client, box.sandbox)
        cookie = signed.headers["set-cookie"]
        assert cookie.startswith(f"latet_tutorial_{PORT}=") and "Path=/_latet" in cookie and "HttpOnly" in cookie
        sid = signed.cookies[f"latet_tutorial_{PORT}"]
        transport = httpx.ASGITransport(app=box.app)
        base = f"http://127.0.0.1:{PORT}"
        async with httpx.AsyncClient(transport=transport, base_url=base, cookies={"latet_session": sid}) as other:
            assert (await other.get(f"{API}/me")).status_code == 401
        me = (await box.client.get(f"{API}/me")).json()
        assert me["tutorial"] == {"current": None, "reviewer": False}


def test_the_sandbox_is_made_in_its_own_folder_only(tmp_path, monkeypatch):
    progress = Progress(tmp_path / "user" / "tutorial.json")
    root = tmp_path / "latet-tutorial-x"

    def refused(where, reason: str) -> None:
        with pytest.raises(SandboxError, match=reason):
            Sandbox.create(where, PORT, progress=progress)
        assert not (where / CONFIG).exists()  # nothing written

    # The environment still names the user's own folders: what reads them would read or write there.
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "user" / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "user" / "config.yaml"))
    refused(root, "the environment")
    assert not root.exists()
    # Never in, around or as the user's own data or settings folder (where they are when nothing moves them).
    own_data, own_config = tmp_path / "AppData" / "Local" / "latet", tmp_path / "AppData" / "Roaming" / "latet"
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *_a, **_k: str(own_data))
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda *_a, **_k: str(own_config))
    for where in (own_data / "tutorial", own_data, own_config / "x", tmp_path / "AppData"):
        switch(monkeypatch, where)
        refused(where, "your own Latet folder")
    # Its folders inside its root: a data folder that is a link elsewhere is not.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.mkdir()
    try:
        os.symlink(elsewhere, root / DATA, target_is_directory=True)
    except OSError:
        pytest.skip("this computer does not let a test make a link")
    switch(monkeypatch, root)
    refused(root, "outside the sandbox")
    assert list(elsewhere.iterdir()) == []


@pytest.fixture
def tripwire(monkeypatch):
    """Every way out to the network, made to fail and noted: HTTP (httpx, async and not) and WebSockets."""
    reached: list[str] = []

    async def http(self, request):
        reached.append(str(request.url))
        raise httpx.ConnectError("not from the tutorial", request=request)

    def http_sync(self, request):
        reached.append(str(request.url))
        raise httpx.ConnectError("not from the tutorial", request=request)

    async def websocket(url, *args, **kwargs):
        reached.append(url)
        raise OSError("not from the tutorial")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", http_sync)
    monkeypatch.setattr(ws, "connect", websocket)
    return reached


async def test_the_tripwire_catches_a_request_to_the_network(tripwire):
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://api.anthropic.com/v1/models")
    with httpx.Client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://api.openai.com/v1/models")
    with pytest.raises(OSError, match="not from the tutorial"):
        await ws.connect("wss://chatgpt.com/backend-api/codex/responses")
    assert tripwire == [
        "https://api.anthropic.com/v1/models",
        "https://api.openai.com/v1/models",
        "wss://chatgpt.com/backend-api/codex/responses",
    ]


@pytest.mark.parametrize("cid", list(load_all()))
async def test_no_chapter_reaches_the_network(tmp_path, monkeypatch, tripwire, cid):
    async with opened(tmp_path, monkeypatch) as box:
        await play(box, cid, checked=False)
    assert tripwire == []
