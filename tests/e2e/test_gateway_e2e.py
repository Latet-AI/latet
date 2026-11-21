"""Leak-canary end-to-end: real gateway, fake upstream. No canary may reach the upstream, and
every canary must come back to the client restored, across all wire formats."""

import contextlib
import json
import random
import socket
import string
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from latet.config import Config, TermConfig
from latet.engine.build import build
from latet.engine.types import EntityClass
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
# Session ids within their agents' grammars (P2-1): Claude Code's UUID, Codex's ThreadId UUID.
CC_SESSION = "0c1d2e3f-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
S_1 = "019a4c2e-7d1b-7c3e-9f2a-000000000001"
S_LEFT = "019a4c2e-7d1b-7c3e-9f2a-00000000001e"
S_BIG = "019a4c2e-7d1b-7c3e-9f2a-0000000000b1"
S_COUNTED = "019a4c2e-7d1b-7c3e-9f2a-0000000000c0"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream()
    opened = False
    try:
        fake.start()
        assert fake.server.started
        opened = True
        print(f"P24_PORT_OPEN {fake.port}")
        yield fake
    finally:
        fake.stop()
        assert not fake.thread.is_alive()
        if opened:
            print(f"P24_PORT_CLOSED {fake.port}")


@pytest.fixture
def client(upstream):
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[
            Upstream(
                "anthropic",
                "/anthropic",
                upstream.url,
                envelope_expected=frozenset({("model", "x")}),
            ),
            Upstream("openai", "/openai", upstream.url, envelope_expected=frozenset({("model", "x")})),
            Upstream(
                "chatgpt",
                "/backend-api/codex",
                upstream.url,
                keep_prefix=True,
                envelope_expected=frozenset({("model", "x")}),
            ),
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
        "/backend-api/codex/responses", headers={"host": "127.0.0.1:5283", "session-id": S_1}
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


def test_hermes_signed_in_with_chatgpt_replays_its_conversation_over_http(client, upstream):
    """Hermes Agent's ChatGPT requests over HTTP (issue 17): a conversation of its unified runtime (a ``local-`` session
    id), a workspace's residency, and its history replayed with each assistant message's ``phase``, a tool call and its
    output. Masked on the way out, restored on the way back; the residency reaches the provider as it was sent."""
    body = {
        "model": "x",
        "stream": True,
        "store": False,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "check the contact"}]},
            {"type": "message", "role": "assistant", "status": "completed", "phase": "commentary",
             "content": [{"type": "output_text", "text": "Reading the file first."}]},
            {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": f"Contact {EMAIL}"},
            {"type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "The contact is in the file."}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
        ],
    }  # fmt: skip
    before = len(upstream.requests)
    headers = {
        "authorization": "Bearer fake-chatgpt-token",
        "session_id": "local-" + "9f2c4e1a" * 8,
        "x-openai-internal-codex-residency": "eu",
    }
    response = client.post("/backend-api/codex/responses", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert len(upstream.requests) == before + 1
    assert upstream.requests[-1]["headers"]["x-openai-internal-codex-residency"] == "eu"
    sent = json.loads(upstream.received[-1])["input"]
    assert [item.get("phase") for item in sent] == [None, "commentary", None, None, "final_answer", None]
    events = _sse_data(response.text)
    assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == "You said: " + PROMPT
    _assert_no_leak(upstream)


PORTAL = "https://portal.acme-intra.it/home"


def _conversation(prompt: str, *outputs: str) -> dict:
    """A request to Anthropic's API: ``prompt``, then a command and its output for each of ``outputs``."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    for n, output in enumerate(outputs):
        messages.append(
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_t{n}", "name": "Bash", "input": {}}]}
        )
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_t{n}", "content": output}]}
        )
    return {"model": "x", "messages": messages}


def _gateway(upstream, vault: Vault | None = None, config: Config | None = None):
    built = build(config or Config(), vault if vault is not None else Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    config = GatewayConfig(
        port=5283,
        upstreams=[
            Upstream("anthropic", "/anthropic", upstream.url, envelope_expected=frozenset({("model", "x")})),
            Upstream("openai", "/openai", upstream.url, envelope_expected=frozenset({("model", "x")})),
            Upstream(
                "chatgpt",
                "/backend-api/codex",
                upstream.url,
                keep_prefix=True,
                envelope_expected=frozenset({("model", "x")}),
            ),
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


_NAMED_TOKEN = "Abcdefghij0123456789KLMNopq"


@pytest.mark.parametrize(
    "named",
    [
        f"the docs call it {_NAMED_TOKEN} throughout",
        json.dumps({"note": f"the docs call it {_NAMED_TOKEN} throughout"}),
    ],
    ids=["a text", "a JSON document"],
)
def test_an_output_that_left_in_clear_is_masked_anew_once_the_vault_learns_its_value_is_a_secret(
    upstream, monkeypatch, named
):
    """As above, but what the later output teaches is a secret (a bearer token the first output named in passing): the
    first output is not sent again with it in clear. It is masked anew once, on the request that teaches it, and then
    leaves as it was masked anew, request after request; a secret is never sent again for the provider's cache (issue
    21). A JSON document is asked of what it leaves as, never masked anew on every request (the review of 125a2bcf)."""
    token = _NAMED_TOKEN
    teaches = f"curl -H 'Authorization: Bearer {token}' https://api.x"
    headers = {"x-api-key": "test"}
    anew: list[str] = []
    mask_new = Mediator._mask_new

    def counted(self, value, *args, **kwargs):
        if value == named:
            anew.append(value)
        return mask_new(self, value, *args, **kwargs)

    monkeypatch.setattr(Mediator, "_mask_new", counted)
    client, _ = _gateway(upstream)
    with client:
        assert client.post("/anthropic/v1/messages", json=_conversation("which?", named), headers=headers).status_code
        assert token in json.loads(upstream.received[-1])["messages"][2]["content"][0]["content"]  # left in clear
        outputs = [named, teaches]
        sent_again = []
        for step in range(3):
            anew.clear()
            response = client.post("/anthropic/v1/messages", json=_conversation("which?", *outputs), headers=headers)
            assert response.status_code == 200, response.text
            assert token not in upstream.received[-1].decode()
            sent_again.append(json.loads(upstream.received[-1])["messages"][2]["content"][0]["content"])
            assert bool(anew) is (step == 0)  # masked anew on the request that teaches the secret alone
            outputs.append(f"step {step}")
        assert sent_again[0] == sent_again[1] == sent_again[2]  # stable from then
        fake = json.loads(upstream.received[-1])["messages"][4]["content"][0]["content"].split("Bearer ")[1]
        assert fake.split("'")[0] in sent_again[0]  # the same fake as the later output's


def test_a_known_value_written_with_escapes_leaves_as_a_fake_and_comes_back_exactly(upstream):
    """A value the vault knows, in a command's output that writes it in escapes (JSON lines, a URL) and in a JSON
    output's string that holds another document: the provider is sent a fake each time, never the value, the link
    stays a link; and a prompt with the link comes back from the model's answer exactly as it was written."""
    name = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"
    as_json = json.dumps(name)[1:-1]  # with ASCII alone: the `ò` as an escape
    vault = Vault(ROOT_KEY)
    vault.intern("PERSON", EntityClass.NER, name)
    lines = f'{{"id": 1, "name": "{as_json}"}}\n{{"id": 2, "name": "{as_json}"}}\n'
    nested = json.dumps({"payload": json.dumps({"name": name})})  # the inner document's escape, escaped again
    link = "https://crm.example/api/people?q=Niccol%C3%B2%20Ferri&cursor=abc"
    log = f"GET {link} 200"
    prompt = f"open {link} and tell me who it is"
    client, _ = _gateway(upstream, vault)
    with client:
        request = _conversation("who wrote these?", lines, nested, log)
        assert client.post("/anthropic/v1/messages", json=request, headers={"x-api-key": "test"}).status_code == 200
        sent = [m["content"][0]["content"] for m in json.loads(upstream.received[-1])["messages"][2::2]]
        echoed = {"model": "x", "messages": [{"role": "user", "content": prompt}]}
        answer = client.post("/anthropic/v1/messages", json=echoed, headers={"x-api-key": "test"}).json()["content"]
    for text in (*sent, upstream.received[-1].decode()):
        assert "Ferri" not in text and "Niccol" not in text
    assert json.loads(sent[0].split("\n")[0])["name"] != name
    assert json.loads(json.loads(sent[1])["payload"])["name"] != name
    shown = sent[2].split(" ")[1]
    assert shown != link and " " not in shown and shown.endswith("&cursor=abc")  # one link, its fields as they were
    assert answer[0]["text"] == "You said: " + prompt  # the agent gets the link back as it was written
    assert answer[1]["input"] == {"content": prompt}


_GLUED_IBAN = "IT60X0542811101000000123456"
# A value of a type that is masked inside an address let through (EMAIL: allow): an IBAN, which the address outranks,
# and a term of yours typed DOMAIN, which outranks the address (0.9.9 masked the whole address for it).
_LET_THROUGH = {
    "iban": (
        Config(ner="off", policies={"EMAIL": "allow"}),
        _GLUED_IBAN,
        [f"pay {_GLUED_IBAN}.ops@acme-intra.it", f"pay {_GLUED_IBAN}@acme-intra.it",
         f"pay ops.{_GLUED_IBAN}@acme-intra.it"],
    ),
    "domain-term": (
        Config(ner="off", policies={"EMAIL": "allow"}, terms=[TermConfig(value="acme-intra.it", type="DOMAIN")]),
        "acme-intra.it",
        ["write ops@acme-intra.it today", "see acme-intra.it or write ops@acme-intra.it"],
    ),
}  # fmt: skip


def _carrying(where: str, text: str) -> list[dict]:
    """A conversation that carries ``text`` in the model's own text, a tool call's input (both sent again in the next
    request, where the gateway masks them again and the final check does not read them), a tool output or a prompt."""
    if where == "model text":
        return [{"role": "user", "content": "who do we pay?"},
                {"role": "assistant", "content": [{"type": "text", "text": text}]},
                {"role": "user", "content": "thanks"}]  # fmt: skip
    if where == "tool input":
        return [{"role": "user", "content": "send it"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_t1", "name": "Bash", "input": {"c": text}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_t1", "content": "ok"}]}]  # fmt: skip
    if where == "tool output":
        return [{"role": "user", "content": "look"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_t1", "name": "Bash", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_t1", "content": text}]}]  # fmt: skip
    return [{"role": "user", "content": text}]


def _carrying_chat(where: str, text: str) -> list[dict]:
    """``_carrying`` on the OpenAI chat route: the model's own text, a tool call's arguments, a tool's message, a prompt."""
    call = {"id": "call_c1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"c": text})}}
    if where == "model text":
        return [{"role": "user", "content": "who do we pay?"}, {"role": "assistant", "content": text},
                {"role": "user", "content": "thanks"}]  # fmt: skip
    if where == "tool input":
        return [{"role": "user", "content": "send it"}, {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "call_c1", "content": "ok"}]  # fmt: skip
    if where == "tool output":
        empty = {**call, "function": {"name": "bash", "arguments": "{}"}}
        return [{"role": "user", "content": "look"}, {"role": "assistant", "content": None, "tool_calls": [empty]},
                {"role": "tool", "tool_call_id": "call_c1", "content": text}]  # fmt: skip
    return [{"role": "user", "content": text}]


_ROUTES = {
    "anthropic": ("/anthropic/v1/messages", _carrying, {"x-api-key": "test"}),
    "openai-chat": ("/openai/v1/chat/completions", _carrying_chat, {"authorization": "Bearer test"}),
}


@pytest.mark.parametrize("route", list(_ROUTES))
@pytest.mark.parametrize("case", list(_LET_THROUGH))
def test_a_masked_value_inside_an_address_let_through_never_reaches_the_provider(upstream, case, route):
    """Through the real gateway, with addresses let through: an IBAN written in one, or a term of yours typed DOMAIN, in
    the model's own text, a tool call's input, a tool output or a prompt, never reaches the provider, and no request is
    stopped for it (0.9.9 sent the IBAN from the model's text and a tool input, which the gateway masks again and the
    final check does not read, and stopped the others)."""
    config, value, texts = _LET_THROUGH[case]
    path, carrying, headers = _ROUTES[route]
    client, _ = _gateway(upstream, config=config)
    with client:
        for where in ("model text", "tool input", "tool output", "prompt"):
            for text in texts:
                before = len(upstream.received)
                request = {"model": "x", "messages": carrying(where, text)}
                status = client.post(path, json=request, headers=headers).status_code
                sent = b"".join(upstream.received[before:]).decode("utf-8", "replace")
                assert status == 200, (where, text)
                assert sent and value not in sent and json.dumps(value)[1:-1] not in sent, (where, text)


def test_two_known_values_that_overlap_in_a_tool_output_leave_masked_whole(upstream):
    """`Anna Maria` and `Maria Rossi`, both known (in one part of the vault's values), in `Anna Maria Rossi`: the
    provider is sent a fake for the whole of them, no word of either."""
    vault = Vault(ROOT_KEY)
    for value in ("Anna Maria", "Maria Rossi"):
        vault.intern("PERSON", EntityClass.NER, value)
    output = "ticket #4521 opened by Anna Maria Rossi, assigned to the support team"
    client, _ = _gateway(upstream, vault)
    with client:
        request = _conversation("who opened it?", output)
        assert client.post("/anthropic/v1/messages", json=request, headers={"x-api-key": "test"}).status_code == 200
    sent = json.loads(upstream.received[-1])["messages"][2]["content"][0]["content"]
    union = vault.by_original("Anna Maria Rossi")
    assert union is not None and sent == output.replace("Anna Maria Rossi", union.surrogate)


def test_a_tool_output_of_a_thousand_customer_addresses_leaves_masked_whole(upstream):
    """A query of a service's customers: a thousand addresses, a first name alone or a first and a last name, at a
    mailbox provider or at the company. The provider is sent a fake for each, no address of theirs: the vault ran out
    of fakes for one word at a mailbox within the first hundred, and the gateway stopped the request."""
    rng = random.Random(10)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9)))

    addresses: dict[str, None] = {}
    while len(addresses) < 1_000:
        local = word() if rng.random() < 0.5 else f"{word()}.{word()}"
        addresses[f"{local}@{rng.choice(('gmail.com', 'outlook.com', 'acme-corp.it'))}"] = None
    output = "id,email\n" + "".join(f"{n},{address}\n" for n, address in enumerate(addresses, 1))
    vault = Vault(ROOT_KEY)
    client, _ = _gateway(upstream, vault)
    with client:
        request = _conversation("who are our customers?", output)
        assert client.post("/anthropic/v1/messages", json=request, headers={"x-api-key": "test"}).status_code == 200
    sent = json.loads(upstream.received[-1])["messages"][2]["content"][0]["content"]
    lines = sent.splitlines()[1:]
    assert len(lines) == len(addresses) and not [a for a in addresses if a in sent]
    fakes = [line.split(",", 1)[1] for line in lines]
    assert len(set(fakes)) == len(fakes)
    assert all(vault.by_surrogate(fake) is not None for fake in fakes)


def test_what_a_websocket_turn_carried_is_recorded_as_left_once_it_left(upstream):
    frame = {
        "type": "response.create",
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "the page is up"}]}],
    }
    client, mediator = _gateway(upstream)
    headers = {"host": "127.0.0.1:5283", "session-id": S_LEFT}
    with client, client.websocket_connect("/backend-api/codex/responses", headers=headers) as ws:
        ws.send_text(json.dumps(frame))
        while json.loads(ws.receive_text())["type"] != "response.completed":
            pass
    mediator.executor.submit(lambda: None).result(5)  # what was handed to the engine's thread is done
    assert mediator.verifier.sent(f"session-id:{S_LEFT}", "the page is up")


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
        upstreams=[
            Upstream(
                "chatgpt",
                "/backend-api/codex",
                upstream.url,
                keep_prefix=True,
                envelope_expected=frozenset({("model", "x")}),
            )
        ],
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
    headers = {"host": "127.0.0.1:5283", "session-id": S_COUNTED}
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
        if not self.server.started:
            self.server.should_exit = True
            self.thread.join(timeout=5)
            assert not self.thread.is_alive()
            raise AssertionError("owned upstream did not start")
        print(f"P24_PORT_OPEN {self.port}")
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        print(f"P24_PORT_CLOSED {self.port}")


def test_a_websocket_message_larger_than_latet_reads_ends_the_turn_with_an_error(monkeypatch):
    """The upstream's message is never inflated past what Latet reads (a WebSocket's messages may come compressed): the
    agent is told, and the socket closes as one closes on a message too big."""
    monkeypatch.setattr(ws_relay, "MAX_DECODED", 2**20)
    frame = {"type": "response.create", "model": "x", "input": "the page is up"}
    with _Answers(2 * 2**20) as answers:
        client, _ = _gateway(answers)
        headers = {"host": "127.0.0.1:5283", "session-id": S_BIG}
        with client, client.websocket_connect("/backend-api/codex/responses", headers=headers) as socket_:
            socket_.send_text(json.dumps(frame))
            told = json.loads(socket_.receive_text())
            with pytest.raises(WebSocketDisconnect) as closed:
                socket_.receive_text()
    assert told["type"] == "error" and "larger than Latet reads" in told["error"]["message"]
    assert closed.value.code == 1009


def test_claude_code_is_verified_not_rewritten(client, upstream):
    headers = {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": CC_SESSION}
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


def _claude_code_2_1_323(text: str = "hello there") -> dict:
    """Claude Code 2.1.323's members, as captured by shape (tests/unit/test_request_claude_code_body.py): a system
    message in the conversation with its effort, context editing, and the effort xhigh."""
    system_turn = {
        "role": "system",
        "content": [{"type": "text", "text": "The tools changed.", "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": "xhigh"},
    }
    return {
        "model": "x",
        "stream": True,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}, system_turn],
        "thinking": {"type": "adaptive", "display": "omitted"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "xhigh"},
    }


@pytest.mark.parametrize("leak_at", [("messages", 1, "content", 0, "text"), ("context_management", "edits", 0, "note")])
def test_claude_code_2_1_323_s_request_is_verified_and_forwarded_byte_for_byte(client, upstream, leak_at):
    """Its own request is checked and leaves as it was sent; a value in the system message's text, or in a text beside
    the new members, stops it, and nothing leaves."""
    headers = {"user-agent": "claude-cli/2.1.323 (external, cli)", "x-claude-code-session-id": CC_SESSION}
    raw = json.dumps(_claude_code_2_1_323(), separators=(",", ":")).encode()
    r = client.post("/anthropic/v1/messages", content=raw, headers={**headers, "content-type": "application/json"})
    assert r.status_code == 200, r.text
    assert upstream.received[-1] == raw
    leaking = json.loads(raw)
    holder = leaking
    for step in leak_at[:-1]:
        holder = holder[step]
    holder[leak_at[-1]] = PROMPT
    before = len(upstream.received)
    r = client.post("/anthropic/v1/messages", json=leaking, headers=headers)
    assert r.status_code == 400
    assert "Latet" in r.json()["error"]["message"]
    assert len(upstream.received) == before


def test_another_agent_s_system_message_with_its_effort_is_masked_and_restored(client, upstream):
    """The same members from an agent Latet masks (an Anthropic API key, no Claude Code): the conversation's values
    leave as fakes and the answer comes back with the real ones."""
    body = _claude_code_2_1_323(PROMPT)
    del body["thinking"]["display"]  # declared nowhere: only Claude Code's own request reads it, as checked text
    r = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 200, r.text
    sent = json.loads(upstream.received[-1])
    assert sent["messages"][1]["output_config"] == {"effort": "xhigh"}
    assert sent["context_management"] == body["context_management"]
    text = "".join(e["delta"]["text"] for e in _sse_data(r.text) if e.get("delta", {}).get("type") == "text_delta")
    assert text == "You said: " + PROMPT
    _assert_no_leak(upstream)


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
