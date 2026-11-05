"""A real-shaped Codex 0.162.0 Responses request through P2-2's body coverage and the production gateway.

The body's shape is the one the installed Codex 0.162.0 sent to the test fake on 2026-11-03 (members, item and tool
kinds, value types), with synthetic values: ``codex-api/src/common.rs:279-304`` at c1382380 declares the same members.
``client_metadata`` is a removable member, taken out before the send and counted, as ``x-codex-turn-metadata`` is: it
carries the repository's absolute path, its git remotes and HEAD. ``stream_options``, ``tool_choice`` and
``prompt_cache_key`` are named with Codex's grammars; a value outside one refuses, naming the member, never its value.
"""

from __future__ import annotations

import copy
import json

import pytest

from latet.gateway.body_coverage import BodyRefused, Ledger, inspect_body
from latet.gateway.routes import Upstream
from latet.protocols.base import BodyScan

from .test_request_coverage import counts, gateway
from .test_request_outcomes import EMAIL

THREAD = "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"
REPOSITORY = "C:\\Users\\mario.rossi\\src\\acme-payroll"
REMOTE = "git@github.com:acme-private/payroll.git"
TURN_METADATA = json.dumps(
    {
        "session_id": THREAD,
        "thread_id": THREAD,
        "turn_id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a20",
        "workspaces": {REPOSITORY: {"associated_remote_urls": {"origin": REMOTE}, "has_changes": True}},
    }
)


def _function(name: str) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": "Runs one step of the agent's work.",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "What the step works on."}},
            "required": ["target"],
            "additionalProperties": False,
        },
    }


def codex_body() -> dict:
    """Members, kinds and types as Codex 0.162.0 sent them (scratchpad rc\\profiles-fix\\codex_body_probe.py)."""
    return {
        "model": "gpt-5.2-codex",
        "stream": True,
        "input": [
            {
                "type": "message",
                "id": "msg_" + "0a" * 18,
                "role": "developer",
                "content": [{"type": "input_text", "text": "Work in the sandbox the user allowed."}],
            },
            {
                "type": "message",
                "id": "msg_" + "1b" * 18,
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": "Ask before running commands."},
                    {"type": "input_text", "text": "Answer briefly."},
                ],
            },
            {
                "type": "message",
                "id": "msg_" + "2c" * 18,
                "role": "user",
                "content": [{"type": "input_text", "text": "The shell is PowerShell."}],
            },
            {
                "type": "message",
                "id": "msg_" + "3d" * 18,
                "role": "user",
                "content": [{"type": "input_text", "text": "say hello"}],
            },
        ],
        "tools": [
            _function("exec_command"),
            _function("write_stdin"),
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "description": "Tools to work with other agents.",
                "tools": [_function("spawn_agent"), _function("wait_agent")],
            },
            {"type": "web_search", "external_web_access": False},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"summary": "auto"},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": THREAD,
        "client_metadata": {
            "root_turn_id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a20",
            "session_id": THREAD,
            "thread_id": THREAD,
            "turn_id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a20",
            "x-codex-window-id": THREAD + ":0",
            "x-codex-turn-metadata": TURN_METADATA,
            "x-codex-installation-id": "6c1f0e9b-8a7d-4c5b-9a3f-2e1d0c9b8a7f",
        },
    }


CODEX_HEADERS = {
    "authorization": "Bearer fake-openai-key",
    "accept": "text/event-stream",
    "content-type": "application/json",
    "originator": "codex_cli_rs",
    "user-agent": "codex_cli_rs/0.162.0 (Windows 10.0.19045; x86_64) WindowsTerminal",
    "session-id": THREAD,
    "thread-id": THREAD,
    "x-client-request-id": THREAD,
    "x-codex-window-id": THREAD + ":0",
    "x-codex-beta-features": "remote_compaction_v2",
    "x-codex-turn-metadata": TURN_METADATA,
}


def test_a_real_shaped_codex_body_leaves_without_its_client_metadata():
    body = codex_body()
    with gateway() as (client, transport, made, mediator):
        response = client.post("/openai/v1/responses", content=json.dumps(body), headers=CODEX_HEADERS)
        assert response.status_code == 200, response.text
        assert counts(made) == (1, 1)
        sent = transport.sent[0]
        expected = copy.deepcopy(body)
        del expected["client_metadata"]
        assert json.loads(sent.content) == expected
        assert REPOSITORY.encode() not in sent.content and REMOTE.encode() not in sent.content
        assert all(name.lower() != b"x-codex-turn-metadata" for name, _ in sent.headers)
        assert mediator.body_removed_members == 1


def test_codex_websocket_create_frame_leaves_without_its_client_metadata():
    from .test_request_outcomes import _mediator

    mediator = _mediator()
    prepared = None
    try:
        frame = {"type": "response.create", **codex_body(), "generate": False}
        session = mediator.engine.session("session-id:" + THREAD)
        sent, prepared = mediator.mask_ws_frame(
            json.dumps(frame), session, Upstream("openai", "/openai", "https://api.openai.com")
        )
        assert prepared is not None and prepared.block_reason is None, prepared and prepared.block_reason
        assert prepared.body_removed == ("client_metadata",)
        leaving = json.loads(sent)
        assert "client_metadata" not in leaving and leaving["generate"] is False
        assert REPOSITORY not in sent
    finally:
        if prepared is not None and prepared.lease is not None:
            prepared.lease.release()
        mediator.close()


def _inspect(body: dict, *, verify: bool = False):
    upstream = Upstream("openai", "/openai", "https://api.openai.com")
    return inspect_body(body, "responses", upstream, BodyScan(), verify, Ledger())


def _chat(body: dict):
    upstream = Upstream("openai", "/openai", "https://api.openai.com")
    return inspect_body({"model": "gpt-4o-mini", "messages": [], **body}, "chat", upstream, BodyScan(), False, Ledger())


@pytest.mark.parametrize(
    "options",
    [{"reasoning_summary_delivery": "sequential_cutoff"}, {"include_obfuscation": False}, {}],
)
def test_stream_options_accept_codex_s_and_the_responses_shape(options):
    assert _inspect({**codex_body(), "stream_options": options}).problem is None


def test_chat_stream_options_accept_include_usage():
    assert _chat({"stream": True, "stream_options": {"include_usage": True}}).problem is None


BAD = ["mario.rossi@acme.com", "C:\\Users\\mario.rossi\\repo", "has a space", "x" * 300]


@pytest.mark.parametrize(
    "options",
    [
        {"include_usage": "true"},
        {"reasoning_summary_delivery": "mario.rossi@acme.com"},
        {"unknown": True},
        "sequential_cutoff",
        {"include_usage": True, "note": "C:\\Users\\mario.rossi\\repo"},
    ],
)
def test_stream_options_outside_their_shape_refuse_naming_the_member(options):
    with pytest.raises(BodyRefused) as refused:
        _inspect({**codex_body(), "stream_options": options})
    assert str(refused.value) == "request body refused (stream_options)"


@pytest.mark.parametrize(
    "choice",
    ["auto", "none", "required", {"type": "function", "name": "exec_command"}, {"type": "web_search"}],
)
def test_responses_tool_choice_has_its_real_grammar(choice):
    assert _inspect({**codex_body(), "tool_choice": choice}).problem is None


def test_chat_tool_choice_has_its_real_grammar():
    for choice in ("auto", "none", "required", {"type": "function", "function": {"name": "lookup"}}):
        assert _chat({"tool_choice": choice}).problem is None


@pytest.mark.parametrize(
    "choice",
    [*BAD, "any", {"type": "function"}, {"type": "function", "name": "x", "note": "mario"}, {"type": "mario"}, 1],
)
def test_tool_choice_outside_its_grammar_refuses_naming_the_member(choice):
    with pytest.raises(BodyRefused) as refused:
        _inspect({**codex_body(), "tool_choice": choice})
    assert str(refused.value) == "request body refused (tool_choice)"
    assert not isinstance(choice, str) or choice not in str(refused.value)


@pytest.mark.parametrize("key", [THREAD, THREAD.upper(), "guardian:" + THREAD, "memory_consolidation:" + THREAD])
def test_prompt_cache_key_has_codex_s_id_grammar(key):
    assert _inspect({**codex_body(), "prompt_cache_key": key}).problem is None


@pytest.mark.parametrize("key", [*BAD, "user-mario-rossi", "mario:" + THREAD, THREAD + "0", ""])
def test_a_prompt_cache_key_outside_its_grammar_refuses_naming_the_member(key):
    problem = _inspect({**codex_body(), "prompt_cache_key": key}).problem
    assert problem == "request body refused (prompt_cache_key)"
    assert not key or key not in problem


@pytest.mark.parametrize("key", [1, ["guardian"], {"key": THREAD}, None])
def test_a_prompt_cache_key_that_is_no_text_refuses_naming_the_member(key):
    with pytest.raises(BodyRefused) as refused:
        _inspect({**codex_body(), "prompt_cache_key": key})
    assert str(refused.value) == "request body refused (prompt_cache_key)"


@pytest.mark.parametrize(
    "metadata",
    [
        "x-codex-turn-metadata",
        {"x-codex-turn-metadata": {"workspaces": {}}},
        {"x-codex-turn-metadata": 1},
        {"has a space": "1"},
        {f"key{n}": "1" for n in range(65)},
    ],
)
def test_client_metadata_outside_its_shape_refuses_naming_the_member(metadata):
    with pytest.raises(BodyRefused) as refused:
        _inspect({**codex_body(), "client_metadata": metadata})
    assert str(refused.value) == "request body refused (client_metadata)"


def test_client_metadata_is_never_read_as_text_and_never_kept():
    body = codex_body()
    inspection = _inspect(body)
    assert [key for _, key in inspection.removed] == ["client_metadata"]
    assert REPOSITORY not in inspection.checked and TURN_METADATA not in inspection.checked


def test_a_verify_only_profile_cannot_remove_client_metadata():
    with pytest.raises(BodyRefused) as refused:
        _inspect(codex_body(), verify=True)
    assert str(refused.value) == "request body refused (client_metadata)"


def test_the_removed_member_is_counted_only_once_it_left():
    body = codex_body()
    with gateway(status=500) as (client, _, _, mediator):
        client.post("/openai/v1/responses", content=json.dumps(body), headers=CODEX_HEADERS)
        assert mediator.body_removed_members == 1  # an error answer still reached the provider: it left removed
    body["prompt_cache_key"] = "user-mario-rossi"
    with gateway() as (client, _, made, mediator):
        response = client.post("/openai/v1/responses", content=json.dumps(body), headers=CODEX_HEADERS)
        assert response.status_code == 400 and "prompt_cache_key" in response.text
        assert "user-mario-rossi" not in response.text
        assert counts(made) == (0, 0) and mediator.body_removed_members == 0


# --- previous_response_id: a response id the provider issued, never bound to one connection ---------------------


@pytest.mark.parametrize(
    "response_id",
    ["resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3795b", "resp_1", "resp_" + "a" * 128],
)
def test_previous_response_id_has_the_documented_response_id_grammar(response_id):
    assert _inspect({**codex_body(), "previous_response_id": response_id}).problem is None


@pytest.mark.parametrize(
    "response_id", [*BAD, "resp_", "resp_" + "a" * 129, "msg_67ccd2be", "resp_mario.rossi", "resp_a b", "RESP_1"]
)
def test_a_previous_response_id_outside_its_grammar_refuses_naming_the_member(response_id):
    problem = _inspect({**codex_body(), "previous_response_id": response_id}).problem
    assert problem == "request body refused (previous_response_id)"
    assert response_id not in problem


def test_a_previous_response_id_that_is_no_text_refuses_naming_the_member():
    with pytest.raises(BodyRefused) as refused:
        _inspect({**codex_body(), "previous_response_id": 1})
    assert str(refused.value) == "request body refused (previous_response_id)"


async def test_a_resumed_websocket_turn_continues_from_an_earlier_connection_s_response():
    from .test_request_ws_coverage import socket_case

    # A response id no turn of this connection was answered with: a resumed or reconnected session sends one.
    earlier = {"type": "response.create", "model": "m", "input": "and then?", "previous_response_id": "resp_0f1e2d"}
    async with socket_case() as case:
        event = await case.frame(json.dumps(earlier))
        assert event["type"] == "response.created"
        assert json.loads(case.sent[0])["previous_response_id"] == "resp_0f1e2d"


# --- an incremental turn: the output of a call that the response it continues made ---------------------------------
# Codex 0.162.0 over a WebSocket sends, after its first turn, only the new items with previous_response_id
# (core/src/client.rs, ResponseCreateWsRequest { previous_response_id, input: incremental_items, .. }): after a tool
# call, a function_call_output whose call_id names a function_call of the previous response, which the body does not
# hold. A Responses client that continues with previous_response_id over HTTP (the OpenAI Agents SDK can) sends the
# same. Root's ruling: with a previous_response_id its own rule admits, such a reference passes, read by its provider-id
# grammar and checked as text by the final check as every reference is; without one it is refused, as before.

PREVIOUS = "resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3795b"
CALL = "call_" + "Xk3vQ9" * 4
WROTE = f"The report went to {EMAIL}."


def _output(call_id: str = CALL) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": WROTE}


def incremental(previous: str | None = PREVIOUS, call_id: str = CALL) -> dict:
    """Codex's incremental turn: its request's members, the new item alone as its input."""
    body = {**codex_body(), "input": [_output(call_id)]}
    if previous is not None:
        body["previous_response_id"] = previous
    return body


def test_an_incremental_turn_answers_a_call_of_the_response_it_continues():
    inspection = _inspect(incremental())
    assert inspection.problem is None
    assert CALL in inspection.checked and WROTE in inspection.checked  # read as text: the final check sees both


def test_an_output_that_names_a_call_the_body_does_not_hold_is_refused_without_previous_response_id():
    assert _inspect(incremental(previous=None)).problem == "request body refused (correlation reference)"


@pytest.mark.parametrize("call_id", [*BAD, "c1", "call_", "call_" + "a" * 129, "fc-mario.rossi"])
def test_an_undeclared_reference_outside_its_grammar_is_refused_though_the_turn_continues(call_id):
    problem = _inspect(incremental(call_id=call_id)).problem
    assert problem == "request body refused (correlation identifier)"
    assert call_id not in problem


@pytest.mark.parametrize("previous", ["resp_", "msg_67ccd2be", "resp_mario.rossi", "RESP_1"])
def test_a_previous_response_id_outside_its_grammar_admits_no_undeclared_reference(previous):
    problem = _inspect(incremental(previous=previous)).problem
    assert problem in {"request body refused (previous_response_id)", "request body refused (correlation reference)"}
    assert previous not in problem


def test_an_incremental_turn_over_http_leaves_once_its_output_masked():
    body = {"model": "gpt-4o", "previous_response_id": PREVIOUS, "input": [_output()]}
    headers = {"authorization": "Bearer fake-openai-key", "content-type": "application/json"}
    with gateway() as (client, transport, made, _):
        response = client.post("/openai/v1/responses", content=json.dumps(body), headers=headers)
        assert response.status_code == 200, response.text
        assert counts(made) == (1, 1)
        sent = json.loads(transport.sent[0].content)
        assert EMAIL not in transport.sent[0].content.decode()
        assert sent["previous_response_id"] == PREVIOUS and sent["input"][0]["call_id"] == CALL
    del body["previous_response_id"]
    with gateway() as (client, _, made, _):
        response = client.post("/openai/v1/responses", content=json.dumps(body), headers=headers)
        assert response.status_code == 400 and "correlation reference" in response.text
        assert counts(made) == (0, 0)


def test_codex_s_incremental_websocket_frame_is_read_and_masked():
    from .test_request_outcomes import _mediator

    mediator = _mediator()
    prepared = None
    try:
        frame = {"type": "response.create", **incremental()}
        session = mediator.engine.session("session-id:" + THREAD)
        sent, prepared = mediator.mask_ws_frame(
            json.dumps(frame), session, Upstream("openai", "/openai", "https://api.openai.com")
        )
        assert prepared is not None and prepared.block_reason is None, prepared and prepared.block_reason
        leaving = json.loads(sent)
        assert EMAIL not in sent and leaving["input"][0]["call_id"] == CALL
        assert leaving["previous_response_id"] == PREVIOUS
    finally:
        if prepared is not None and prepared.lease is not None:
            prepared.lease.release()
        mediator.close()


@pytest.mark.parametrize("continued", [True, False], ids=["continued", "not-continued"])
async def test_an_incremental_websocket_turn_goes_up_once_or_is_refused_without_previous_response_id(continued):
    from .test_request_ws_coverage import socket_case

    frame = {"type": "response.create", "model": "m", "input": [_output()]}
    if continued:
        frame["previous_response_id"] = PREVIOUS
    async with socket_case() as case:
        event = await case.frame(json.dumps(frame))
        if continued:
            assert event["type"] == "response.created"
            assert len(case.sent) == 1 and EMAIL not in case.sent[0]
            assert json.loads(case.sent[0])["input"][0]["call_id"] == CALL
        else:
            assert event["type"] == "error" and "correlation reference" in event["error"]["message"]
            assert case.sent == []


async def test_websocket_handshake_removals_are_counted_once_the_provider_answered():
    from .test_request_ws_coverage import socket_case

    extra = (
        (b"x-codex-turn-metadata", TURN_METADATA.encode()),
        (b"x-codex-turn-state", b"opaque-routing-token"),
        (b"x-openai-subagent", b"review"),
    )
    async with socket_case(extra=extra) as case:
        assert len(case.handshakes) == 1
        sent = {name.lower() for name, _ in case.handshakes[0][1]}
        assert not sent & {"x-codex-turn-metadata", "x-openai-subagent"} and "x-codex-turn-state" in sent
        assert case.mediator.envelope_removed_headers == 2
