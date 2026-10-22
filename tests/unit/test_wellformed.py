r"""Half of a character in what an agent sends: a JavaScript agent that cuts a tool's output at a fixed length can
cut an emoji in two, and JSON writes the half it keeps as an escape (`\ud83d`). Python reads it as a lone surrogate,
which UTF-8 cannot write: every detector and hash failed on it, the request was never sent, and every later request
of the conversation carried the same text."""

import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from latet.bridge.api import Bridge
from latet.config import Config
from latet.engine import wellformed
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.mediate import REWRITE, VERIFY, Mediator, Prepared, WSResponseRewriter
from latet.gateway.recorder import ResponseCapture
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY
from .test_reviewer import FakeServer, send
from .test_reviewer import mediator as reviewing
from .test_reviewer import request as reviewed_request

HALF = "\ud83d"  # the first half of U+1F600, an emoji that UTF-16 writes in two: D83D DE00
SMILE = "\N{GRINNING FACE}"  # U+1F600 whole, which JSON writes as its two halves' escapes
REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
UPSTREAM = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
GATEWAY = {"content-type": "application/json"}  # an agent with its own key: what it sends, Latet may rewrite
CLAUDE_CODE = {
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11",
}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"t": "done \\ud83d"}',  # a first half, as JSON escapes it
        b'{"t": "done \\uDE00"}',  # a second half, in capitals
        '{"t": "done \\ud83d"}',  # in a text already read from bytes (a WebSocket frame, a tool's output)
        '{"t": "done \ud83d"}',  # written out in such a text
        b'{"t": "done \xed\xa0\xbd"}',  # written out in UTF-8, as no encoder should: Python reads it all the same
        '{"t": "done \ud83d"}'.encode("utf-16-le", "surrogatepass"),  # UTF-16, which JSON allows
    ],
)
def test_each_half_is_read_as_the_replacement_character(raw):
    assert wellformed.loads_mended(raw) == ({"t": f"done {REPLACEMENT}"}, True)


def test_whole_characters_and_escaped_backslashes_are_read_as_they_are():
    raw = json.dumps({"emoji": SMILE, "path": "C:\\ud83d", "accent": "Nicol\u00f2"})
    assert "\\ud83d\\ude00" in raw  # the emoji as its two halves' escapes
    assert wellformed.loads_mended(raw) == ({"emoji": SMILE, "path": "C:\\ud83d", "accent": "Nicol\u00f2"}, False)


def test_halves_are_replaced_one_for_one_in_keys_and_values():
    raw = json.dumps({f"k{HALF}": [f"{HALF}{SMILE}\ude00{HALF}", {"n": 1, "x": None}]})
    doc = wellformed.loads(raw)
    assert doc == {f"k{REPLACEMENT}": [f"{REPLACEMENT}{SMILE}{REPLACEMENT}{REPLACEMENT}", {"n": 1, "x": None}]}


def _mediator() -> Mediator:
    built = build(Config(), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def _through_gateway(raw: bytes, mediator: Mediator | None = None) -> Prepared:
    return (mediator or _mediator()).prepare(UPSTREAM, "POST", "/anthropic/v1/messages", GATEWAY, raw)


def _claude_code(output: str) -> bytes:
    """A Claude Code turn as it reaches Latet: its billing block first, a Bash call, and what the call printed. JSON
    writes the half as an escape, as JavaScript's JSON.stringify does."""
    call = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "npm run build"}}
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "stream": True,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.291; cc_entrypoint=cli; cch=00000;"},
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "run the build"}]},
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": output}]},
        ],
        "metadata": {"user_id": "user_0f3a_account__session_4f6c1a52"},
    }
    return json.dumps(body).encode()


def test_a_claude_code_request_with_half_a_character_is_checked_and_sent_as_it_came():
    raw = _claude_code(f"build log: done {HALF}")
    assert b"done \\ud83d" in raw
    mediator = _mediator()
    prepared = mediator.prepare(UPSTREAM, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY
    assert prepared.block_reason is None
    assert prepared.body == raw  # Claude Code's requests are checked, never rewritten
    # The next turn sends the same history again: it goes too.
    again = mediator.prepare(UPSTREAM, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert again.block_reason is None


def _tool_output(output: str) -> bytes:
    """An agent with its own key: no session header, so its conversation is named by its opening, the half in it."""
    body = {
        "model": "x",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}]}],
    }
    return json.dumps(body).encode()


def _sent_output(prepared) -> str:
    return json.loads(prepared.body)["messages"][0]["content"][0]["content"]


def test_an_agent_with_its_own_key_sends_the_half_as_the_replacement_character():
    prepared = _through_gateway(_tool_output(f"build log: done {HALF} and {SMILE}"))
    assert prepared.mode == REWRITE
    assert prepared.block_reason is None
    # The provider reads the text Latet checked: the half as U+FFFD, the whole emoji as it was.
    assert _sent_output(prepared) == f"build log: done {REPLACEMENT} and {SMILE}"
    assert b"\\ud83d" not in prepared.body


def test_values_beside_the_half_are_masked():
    prepared = _through_gateway(_tool_output(f"deploy with {GH_PAT}, then mail mario.rossi@acme.it: done {HALF}"))
    sent = _sent_output(prepared)
    assert prepared.masked_count == 2
    assert GH_PAT not in sent
    assert "mario.rossi@acme.it" not in sent
    assert sent.endswith(f": done {REPLACEMENT}")


def test_the_half_in_a_tool_output_written_as_json_is_read_too():
    """A command's output that agents wrap as JSON (Codex's ``{"output": ...}``): the half is an escape in it."""
    output = json.dumps({"output": f"mail mario.rossi@acme.it: done {HALF}", "metadata": {"exit_code": 0}})
    assert "done \\ud83d" in output
    prepared = _through_gateway(_tool_output(output))
    assert prepared.block_reason is None
    sent = json.loads(_sent_output(prepared))
    assert "mario.rossi@acme.it" not in sent["output"]
    assert sent["output"].endswith(f": done {REPLACEMENT}")


def test_the_claude_code_plugin_gets_the_half_back_as_the_replacement_character():
    """The plugin hands each tool output to the daemon before Claude Code builds a request, and puts the text it gets
    back in its place: with the half as U+FFFD there, every request of the conversation is well formed."""
    client = TestClient(Starlette(routes=Bridge(_mediator(), "token").routes()))

    def call(route: str, body: dict) -> dict:
        headers = {"x-latet-token": "token", "content-type": "application/json"}
        r = client.post(f"/_latet/engine/v1/{route}", content=json.dumps({"session": "s1", **body}), headers=headers)
        assert r.status_code == 200
        return r.json()

    items = [{"text": f"mail mario.rossi@acme.it: done {HALF}", "kind": "tool_result"}]
    [item] = call("mask", {"items": items})["items"]
    assert "mario.rossi@acme.it" not in item["text"]
    assert item["text"].endswith(f": done {REPLACEMENT}")
    # The turn's final check reads such a text too, and the screen shows it with the real values.
    assert call("verify", {"slots": [{"text": f"build log: done {HALF}", "kind": "tool_result"}]})["blocking"] == []
    on_screen = item["text"].replace(REPLACEMENT, HALF)
    assert call("rehydrate", {"items": [on_screen]})["items"] == [f"mail mario.rossi@acme.it: done {REPLACEMENT}"]


def test_a_turn_over_a_websocket_is_sent_with_the_replacement_character():
    """Codex's WebSocket: each turn is a frame, read as a request body is."""
    mediator = _mediator()
    content = [{"type": "input_text", "text": f"build log: done {HALF}"}]
    message = {"type": "message", "role": "user", "content": content}
    frame = {"type": "response.create", "model": "x", "input": [message]}
    text, prepared = mediator.mask_ws_frame(json.dumps(frame), mediator.engine.session("session-id:s-1"))
    assert prepared is not None
    assert prepared.block_reason is None
    assert json.loads(text)["input"][0]["content"][0]["text"] == f"build log: done {REPLACEMENT}"


def test_a_bedrock_token_count_with_half_a_character_is_checked_and_sent_as_it_came():
    """Bedrock counts the tokens of a whole request, which it is sent in base64: read as a request is."""
    inner = base64.b64encode(_claude_code(f"build log: done {HALF}")).decode()
    raw = json.dumps({"input": {"invokeModel": {"body": inner}}}).encode()
    path = "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/count-tokens"
    prepared = _mediator().prepare(UPSTREAM, "POST", path, GATEWAY, raw)
    assert prepared.mode == VERIFY
    assert prepared.block_reason is None
    assert prepared.body == raw


async def test_the_local_reviewer_reads_the_half_in_a_tool_output_written_as_json():
    """The local reviewer reads a tool output written as JSON as its strings, one per line: the half among them."""
    server = FakeServer()
    output = json.dumps({"output": f"the customer Ferri called: done {HALF}", "metadata": {"exit_code": 0}})
    prepared = await send(reviewing(server), reviewed_request(output))
    assert prepared.block_reason is None
    assert any(f"the customer Ferri called: done {REPLACEMENT}" in text for text in server.texts)
    sent = json.loads(json.loads(prepared.body)["messages"][2]["content"][0]["content"])
    assert sent["output"].endswith(f"called: done {REPLACEMENT}")
    assert "Ferri" not in sent["output"]  # what the reviewer found is masked


# --- answers: a provider's answer can hold half of a character too (a gateway written in JavaScript can cut one in
# two), and an event Latet rewrites to restore a value carries U+FFFD in its place -----------------------------------


def _masked() -> tuple[Mediator, Prepared, str]:
    """A conversation in which an address was masked, and the fake the provider was sent in its place."""
    mediator = _mediator()
    prepared = _through_gateway(_tool_output("mail mario.rossi@acme.it"), mediator)
    return mediator, prepared, _sent_output(prepared).removeprefix("mail ")


def _sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _streamed_text(raw: bytes) -> str:
    data = [line[6:] for line in raw.decode().split("\n") if line.startswith("data: ")]
    deltas = [json.loads(d).get("delta") or {} for d in data]
    return "".join(d.get("text", "") for d in deltas if d.get("type") == "text_delta")


def _text_block(text: str) -> list[dict]:
    return [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
    ]


def test_a_streamed_answer_with_half_a_character_is_restored():
    mediator, prepared, fake = _masked()
    transform = mediator.response_transform(prepared, "text/event-stream", None)
    assert transform is not None
    out = transform.feed(_sse(*_text_block(f"I wrote to {fake}: done {HALF}"))) + transform.end()
    assert _streamed_text(out) == f"I wrote to mario.rossi@acme.it: done {REPLACEMENT}"


def test_an_answer_in_one_piece_with_half_a_character_is_restored():
    mediator, prepared, fake = _masked()
    transform = mediator.response_transform(prepared, "application/json", None)
    assert transform is not None
    body = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": f"to {fake}: done {HALF}"}]}
    out = transform.feed(json.dumps(body).encode()) + transform.end()
    assert json.loads(out)["content"][0]["text"] == f"to mario.rossi@acme.it: done {REPLACEMENT}"


def test_an_answer_over_a_websocket_with_half_a_character_is_restored():
    mediator, prepared, fake = _masked()
    assert prepared.session is not None
    rewriter = WSResponseRewriter(mediator, prepared.session)
    where = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    text = f"I wrote to {fake}: done {HALF}"
    frames = [
        {"type": "response.output_text.delta", **where, "delta": text},
        {"type": "response.output_text.done", **where, "text": text},
    ]
    out = [json.loads(sent) for frame in frames for sent in rewriter.on_frame(json.dumps(frame))]
    restored = f"I wrote to mario.rossi@acme.it: done {REPLACEMENT}"
    assert [e.get("delta") or e.get("text") for e in out] == [restored, restored]


def test_tool_call_arguments_with_half_a_character_are_restored():
    """A tool call's arguments are JSON text, restored as text: the half stays there as the provider wrote it."""
    mediator = _mediator()
    upstream = Upstream("openai", "/openai", "http://127.0.0.1:1")
    raw = json.dumps({"model": "x", "messages": [{"role": "user", "content": "mail mario.rossi@acme.it"}]}).encode()
    prepared = mediator.prepare(upstream, "POST", "/openai/v1/chat/completions", {"authorization": "Bearer t"}, raw)
    fake = json.loads(prepared.body)["messages"][0]["content"].removeprefix("mail ")
    transform = mediator.response_transform(prepared, "application/json", None)
    assert transform is not None
    arguments = json.dumps({"to": fake, "note": f"wrote to {fake}: done {HALF}"})
    call = {"id": "call_1", "type": "function", "function": {"name": "send", "arguments": arguments}}
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    out = json.loads(transform.feed(json.dumps(body).encode()) + transform.end())
    sent = json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert sent == {"to": "mario.rossi@acme.it", "note": f"wrote to mario.rossi@acme.it: done {HALF}"}


def test_the_dashboard_keeps_an_answer_with_half_a_character():
    """What the dashboard keeps of an answer is stored in UTF-8: it keeps U+FFFD in place of the half."""
    streamed = ResponseCapture("anthropic", "text/event-stream")
    streamed.feed(_sse(*_text_block(f"done {HALF}")))
    streamed.end()
    streamed.settle()
    whole = ResponseCapture("anthropic", "application/json")
    whole.feed(json.dumps({"content": [{"type": "text", "text": f"done {HALF}"}]}).encode())
    whole.end()
    whole.settle()
    assert "".join(streamed.text) == "".join(whole.text) == f"done {REPLACEMENT}"
