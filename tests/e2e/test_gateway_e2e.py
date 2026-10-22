"""Leak-canary end-to-end: real gateway, fake upstream. No canary may reach the upstream, and
every canary must come back to the client restored, across all wire formats."""

import contextlib
import json
import socket
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway import ws as ws_relay
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
IBAN = "IT60 X054 2811 1010 0000 0123 456"
CANARIES = (GH_PAT, EMAIL, IBAN)
PROMPT = f'deploy with key {GH_PAT}, notify {EMAIL}, pay to {IBAN} and quote "done"'


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def client(upstream):
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[
            Upstream("anthropic", "/anthropic", upstream.url),
            Upstream("openai", "/openai", upstream.url),
            Upstream("chatgpt", "/backend-api/codex", upstream.url, keep_prefix=True),
        ],
        mediator=Mediator(built.engine, built.verifier),
    )
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as c:
        yield c


def _sse_data(raw: str) -> list[dict]:
    out = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                out.append(json.loads(line[6:]))
    return out


def _assert_no_leak(upstream):
    sent = upstream.all_received()
    for canary in CANARIES:
        assert canary not in sent
        assert json.dumps(canary)[1:-1] not in sent


def test_anthropic_streaming_restores_text_and_tool_input(client, upstream):
    body = {"model": "x", "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    r = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 200
    events = _sse_data(r.text)
    text = "".join(e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta")
    args = "".join(e["delta"]["partial_json"] for e in events if e.get("delta", {}).get("type") == "input_json_delta")
    thinking = "".join(e["delta"]["thinking"] for e in events if e.get("delta", {}).get("type") == "thinking_delta")
    assert text == "You said: " + PROMPT
    assert json.loads(args) == {"content": PROMPT}
    assert GH_PAT not in thinking  # thinking is never rewritten
    assert any(e["type"] == "ping" for e in events)
    _assert_no_leak(upstream)


def test_anthropic_non_streaming(client, upstream):
    body = {"model": "x", "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]}
    r = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    content = r.json()["content"]
    assert content[0]["text"] == "You said: " + PROMPT
    assert content[1]["input"] == {"content": PROMPT}
    _assert_no_leak(upstream)


def test_history_round_trip_is_byte_stable(client, upstream):
    first = {"model": "x", "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    client.post("/anthropic/v1/messages", json=first, headers={"x-api-key": "test"})
    sent_first = json.loads(upstream.received[-1])
    # The client sends back its (restored) history plus a new turn, as agents do.
    second = {
        "model": "x",
        "stream": True,
        "messages": [
            {"role": "user", "content": PROMPT},
            {"role": "assistant", "content": [{"type": "text", "text": "You said: " + PROMPT}]},
            {"role": "user", "content": "thanks"},
        ],
    }
    client.post("/anthropic/v1/messages", json=second, headers={"x-api-key": "test"})
    sent_second = json.loads(upstream.received[-1])
    assert sent_second["messages"][0] == sent_first["messages"][0]  # identical prefix: cache-friendly
    assert GH_PAT not in json.dumps(sent_second)
    _assert_no_leak(upstream)


def test_chat_streaming(client, upstream):
    body = {"model": "x", "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    r = client.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer test"})
    chunks = _sse_data(r.text)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    args = "".join(
        tc["function"]["arguments"] for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls") or []
    )
    assert text == "You said: " + PROMPT
    assert json.loads(args) == {"content": PROMPT}
    assert r.text.rstrip().endswith("data: [DONE]")
    _assert_no_leak(upstream)


def test_responses_streaming_keeps_sequence_numbers(client, upstream):
    body = {
        "model": "x",
        "stream": True,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
    }
    r = client.post("/openai/v1/responses", json=body, headers={"authorization": "Bearer test"})
    events = _sse_data(r.text)
    text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    args = "".join(e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta")
    assert text == "You said: " + PROMPT
    assert json.loads(args) == {"content": PROMPT}
    done = next(e for e in events if e["type"] == "response.completed")
    assert done["response"]["output"][0]["content"][0]["text"] == "You said: " + PROMPT
    assert json.loads(done["response"]["output"][1]["arguments"]) == {"content": PROMPT}
    seqs = [e["sequence_number"] for e in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    _assert_no_leak(upstream)


def test_codex_websocket(client, upstream):
    frame = {
        "type": "response.create",
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
    }
    with client.websocket_connect(
        "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": "s-1"}
    ) as ws:
        ws.send_text(json.dumps(frame))
        events = []
        while True:
            event = json.loads(ws.receive_text())
            events.append(event)
            if event["type"] == "response.completed":
                break
    text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert text == "You said: " + PROMPT
    _assert_no_leak(upstream)


PORTAL = "https://portal.acme-intra.it/home"


def _conversation(prompt: str, *outputs: str) -> dict:
    """A request to Anthropic's API: ``prompt``, then a command and its output for each of ``outputs``."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    for n, output in enumerate(outputs):
        messages.append(
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {}}]}
        )
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": output}]}
        )
    return {"model": "x", "messages": messages}


def _gateway(upstream):
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    config = GatewayConfig(
        port=5283,
        upstreams=[
            Upstream("anthropic", "/anthropic", upstream.url),
            Upstream("chatgpt", "/backend-api/codex", upstream.url, keep_prefix=True),
        ],
        mediator=mediator,
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1:5283"), mediator


def test_an_output_that_left_in_clear_is_sent_as_it_left_once_the_gateway_saw_it_leave(upstream):
    """An agent through the gateway: an output with a domain the vault did not know left in clear. When a later output
    of the conversation teaches the vault the domain, the first is sent again as it left (the provider's cache holds
    it), since the gateway recorded what it sent: no final check stands between the gateway and the provider."""
    site = json.dumps({"site": PORTAL})
    headers = {"x-api-key": "test"}
    client, _ = _gateway(upstream)
    with client:
        first = _conversation("is the portal up?", site)
        assert client.post("/anthropic/v1/messages", json=first, headers=headers).status_code == 200
        later = _conversation("is the portal up?", site, "write to ops@acme-intra.it")
        assert client.post("/anthropic/v1/messages", json=later, headers=headers).status_code == 200
    sent = json.loads(upstream.received[-1])["messages"]
    assert sent[2]["content"][0]["content"] == site and "acme-intra.it" not in sent[4]["content"][0]["content"]


def test_what_a_websocket_turn_carried_is_recorded_as_left_once_it_left(upstream):
    frame = {
        "type": "response.create",
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "the page is up"}]}],
    }
    client, mediator = _gateway(upstream)
    headers = {"host": "127.0.0.1:5283", "session-id": "s-left"}
    with client, client.websocket_connect("/backend-api/codex/responses", headers=headers) as ws:
        ws.send_text(json.dumps(frame))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    mediator.executor.submit(lambda: None).result(5)  # what was handed to the engine's thread is done
    assert mediator.verifier.sent("session-id:s-left", "the page is up")


class _Counted:
    """What a server's gate gives a WebSocket's app (``latet.turns``): each turn begun and ended, refused when told."""

    def __init__(self, refuse: int = 0) -> None:
        self.steps: list[str] = []
        self.refuse = refuse  # the turns refused first

    def begin(self) -> str | None:
        self.steps.append("begin")
        if self.steps.count("begin") > self.refuse:
            return None
        return "latet: this identity has 1 requests in flight on this instance, the most it may"

    def end(self) -> None:
        self.steps.append("end")


def _counted_gateway(upstream, counted: _Counted) -> TestClient:
    """The gateway, its WebSockets' turns counted by ``counted`` as a server's gate would count them."""
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("chatgpt", "/backend-api/codex", upstream.url, keep_prefix=True)],
        mediator=Mediator(built.engine, built.verifier),
    )
    app = create_app(config)

    async def with_turns(scope, receive, send):
        if scope["type"] == "websocket":
            scope = {**scope, "latet.turns": counted}
        await app(scope, receive, send)

    return TestClient(with_turns, base_url="http://127.0.0.1:5283")


def test_a_websocket_turn_is_counted_from_its_frame_to_its_last_event(upstream):
    """A-16: on a server, a turn holds a place of its identity's requests in flight from the frame that asks until its
    answer's last event: an idle socket holds none, and a turn past the cap is refused with nothing of it sent."""
    frame = {"type": "response.create", "model": "x", "input": "the page is up"}
    counted = _Counted()
    headers = {"host": "127.0.0.1:5283", "session-id": "s-counted"}
    with (
        _counted_gateway(upstream, counted) as client,
        client.websocket_connect("/backend-api/codex/responses", headers=headers) as ws,
    ):
        assert counted.steps == []  # open and idle
        for turn in (1, 2):
            ws.send_text(json.dumps(frame))
            while json.loads(ws.receive_text())["type"] != "response.completed":
                pass
            assert counted.steps == ["begin", "end"] * turn  # its place given back once its answer ended
    refusing = _Counted(refuse=1)
    with (
        _counted_gateway(upstream, refusing) as client,
        client.websocket_connect("/backend-api/codex/responses", headers=headers) as ws,
    ):
        before = len(upstream.ws_received)
        ws.send_text(json.dumps({**frame, "input": "the first turn"}))  # past the cap: refused
        told = json.loads(ws.receive_text())
        assert told["type"] == "error" and "requests in flight" in told["error"]["message"]
        ws.send_text(json.dumps({**frame, "input": "the second turn"}))  # a place free again: answered
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    sent = upstream.ws_received[before:]
    assert len(sent) == 1 and "the second turn" in sent[0] and "the first turn" not in sent[0]  # nothing of it sent
    assert refusing.steps == ["begin", "begin", "end"]


class _Answers:
    """A WebSocket upstream on 127.0.0.1 that answers a turn with one message of ``size`` bytes."""

    def __init__(self, size: int) -> None:
        self.size = size
        app = Starlette(routes=[WebSocketRoute("/backend-api/codex/responses", self.turn)])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def turn(self, websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.receive_text()
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "response.output_text.delta", "delta": "x" * self.size}))
            await websocket.receive_text()  # until the gateway closes

    def __enter__(self) -> "_Answers":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def test_a_websocket_message_larger_than_latet_reads_ends_the_turn_with_an_error(monkeypatch):
    """The upstream's message is never inflated past what Latet reads (a WebSocket's messages may come compressed): the
    agent is told, and the socket closes as one closes on a message too big."""
    monkeypatch.setattr(ws_relay, "MAX_DECODED", 2**20)
    frame = {"type": "response.create", "model": "x", "input": "the page is up"}
    with _Answers(2 * 2**20) as answers:
        client, _ = _gateway(answers)
        headers = {"host": "127.0.0.1:5283", "session-id": "s-big"}
        with client, client.websocket_connect("/backend-api/codex/responses", headers=headers) as socket_:
            socket_.send_text(json.dumps(frame))
            told = json.loads(socket_.receive_text())
            with pytest.raises(WebSocketDisconnect) as closed:
                socket_.receive_text()
    assert told["type"] == "error" and "larger than Latet reads" in told["error"]["message"]
    assert closed.value.code == 1009


def test_claude_code_is_verified_not_rewritten(client, upstream):
    headers = {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": "cc-1"}
    clean = {"model": "x", "stream": True, "messages": [{"role": "user", "content": "hello there"}]}
    raw = json.dumps(clean, separators=(",", ":")).encode()
    r = client.post("/anthropic/v1/messages", content=raw, headers={**headers, "content-type": "application/json"})
    assert r.status_code == 200
    assert upstream.received[-1] == raw  # forwarded byte for byte
    leaking = {"model": "x", "messages": [{"role": "user", "content": PROMPT}]}
    before = len(upstream.received)
    r = client.post("/anthropic/v1/messages", json=leaking, headers=headers)
    assert r.status_code == 400
    assert r.headers["x-should-retry"] == "false"
    assert "Latet" in r.json()["error"]["message"]
    assert len(upstream.received) == before  # nothing left the machine


def test_another_agent_that_calls_itself_claude_cli_is_masked_not_blocked(client, upstream):
    """Qwen Code, on its Anthropic provider, sends Claude Code's user agent with its own version (`claude-cli/0.22.0`)
    and none of what Claude Code adds to each request: no session id, no attribution block. It is masked and restored
    as any other agent, not checked and blocked as Claude Code."""
    headers = {"user-agent": "claude-cli/0.22.0 (external, cli)", "x-app": "cli", "authorization": "Bearer sk-ant-test"}
    body = {"model": "x", "stream": True, "system": [{"type": "text", "text": "You are Qwen Code."}],
            "messages": [{"role": "user", "content": PROMPT}]}  # fmt: skip
    r = client.post("/anthropic/v1/messages", json=body, headers=headers)
    assert r.status_code == 200
    text = "".join(e["delta"]["text"] for e in _sse_data(r.text) if e.get("delta", {}).get("type") == "text_delta")
    assert text == "You said: " + PROMPT
    _assert_no_leak(upstream)


@pytest.mark.parametrize("stream", [True, False])
def test_half_a_character_leaves_as_the_replacement_character_and_comes_back(client, upstream, stream):
    """Half of an emoji, as a JavaScript agent that cuts a tool's output at a fixed length can send (JSON writes it as
    an escape): the provider is sent U+FFFD in its place, fakes in place of the values, and the answer that quotes
    them comes back with the real values."""
    body = {"model": "x", "stream": stream, "messages": [{"role": "user", "content": f"{PROMPT} \ud83d"}]}
    headers = {"x-api-key": "test", "content-type": "application/json"}
    r = client.post("/anthropic/v1/messages", content=json.dumps(body), headers=headers)
    assert r.status_code == 200
    echoed = f"{PROMPT} \N{REPLACEMENT CHARACTER}"
    if stream:
        deltas = [e["delta"] for e in _sse_data(r.text) if "delta" in e]
        text = "".join(d["text"] for d in deltas if d.get("type") == "text_delta")
        args = "".join(d["partial_json"] for d in deltas if d.get("type") == "input_json_delta")
        assert text == "You said: " + echoed
        assert json.loads(args) == {"content": echoed}
    else:
        content = r.json()["content"]
        assert content[0]["text"] == "You said: " + echoed
        assert content[1]["input"] == {"content": echoed}
    assert b"\\ud83d" not in upstream.received[-1]
    _assert_no_leak(upstream)
