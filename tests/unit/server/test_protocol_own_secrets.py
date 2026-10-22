"""Reviewed Latet secret types obey the protocol consumers' masking, collision and refusal contracts."""

import json

import pytest

from latet.engine import wellformed
from latet.engine.types import SlotKind
from latet.gateway.mediate import SSETransform, WSResponseRewriter

from ..test_latet_secrets import ROUTE, TOKENS, URL
from ..test_protocol_buffering import _arguments, _delta, _finish, _start, _wire
from ..test_what_leaves import ANTHROPIC, BEARER, OPENAI, _mediator
from .test_protocol_read_or_refuse import (
    ANTHROPIC_KEY,
    CLAUDE_CODE,
    CLIENTS,
    HEADERS,
    _anthropic,
    _client,
    _content,
    _document,
    _fallback,
    _search,
    _upstream,
)

OWN_SECRETS = [*((token, token) for token, _ in TOKENS), (URL, ROUTE)]
SECRET_IDS = ["personal-token", "api-key", "admin-token", "computer-key", "route-address"]


@pytest.mark.parametrize(("value", "secret"), OWN_SECRETS, ids=SECRET_IDS)
@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
def test_own_secrets_in_fallback_schema_keys_and_strings_are_masked_or_refused(space, value, secret, headers, caplog):
    """Skipping fallback traversal or exempting these secret types exposes the original to the actual upstream."""
    rt, _ = space
    seen = _upstream(rt)
    fmt = {
        "type": "json_schema",
        "description": value,
        "schema": {"type": "object", "properties": {value: {"type": "string", "description": value}}},
    }
    body = _anthropic(fallbacks=[{"model": "safe"}, _fallback(fmt)])
    raw = json.dumps(body).encode()
    with _client(rt) as client:
        response = client.post("/llm/v1/messages", content=raw, headers=headers)
    if headers is ANTHROPIC_KEY:
        assert response.status_code == 200 and len(seen) == 1
        assert secret.encode() not in seen[0].content
        sent = json.loads(seen[0].content)
        assert sent["fallbacks"][0] == {"model": "safe"}
        assert sent["fallbacks"][1]["model"] == "fallback-model"
        assert sent["fallbacks"][1]["output_config"]["effort"] == "high"
    else:
        assert response.status_code == 400 and seen == [] and "Nothing was sent" in response.text
    assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize(("value", "secret"), OWN_SECRETS, ids=SECRET_IDS)
@pytest.mark.parametrize("kind", ["name", "search_result", "title", "context"])
def test_own_secrets_in_unsupported_fields_have_no_role_or_nested_bypass(space, value, secret, kind, caplog):
    """The named server refusals still apply to own secrets even in assistant history or nested tool results."""
    rt, _ = space
    seen = _upstream(rt)
    if kind == "name":
        path, headers = "/llm/v1/chat/completions", BEARER
        body = {"model": "m", "messages": [{"role": "assistant", "name": value, "content": "done"}]}
    else:
        path, headers = "/llm/v1/messages", CLAUDE_CODE
        block = _search(value) if kind == "search_result" else _document(**{kind: value})
        body = _anthropic(messages=[{"role": "assistant", "content": _content(block, True)}])
    with _client(rt) as client:
        response = client.post(path, json=body, headers=headers)
    assert response.status_code == 400 and seen == [] and "Nothing was sent" in response.text
    assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize(("value", "secret"), OWN_SECRETS, ids=SECRET_IDS)
@pytest.mark.parametrize("protocol", ["chat", "anthropic"])
async def test_own_secret_streamed_json_keys_keep_both_values_and_colliding_history_stays_refused(
    value, secret, protocol
):
    """Restoration must preserve both keys; accepted model provenance cannot exempt their colliding replay."""
    mediator = _mediator(ner="off")
    mediator.on_server = True
    session = mediator.engine.session("session-id:own-secret")
    transform = None
    try:
        fake = mediator.engine.mask_text(value, SlotKind.TOOL_RESULT, session).text
        assert secret not in fake
        raw = json.dumps({"owners": {fake: "owner", value: "viewer"}})
        transform = SSETransform(protocol, mediator.restore_context(session), None, lambda rid: None, mediator=mediator)
        wire = b""
        for event in [_start(protocol), _delta(protocol, raw[:17]), _delta(protocol, raw[17:])]:
            wire += b"".join([part async for part in transform.restored(_wire(protocol, event))])
        assert _arguments(protocol, wire) == ""
        wire += b"".join([part async for part in transform.restored(_wire(protocol, _finish(protocol, raw)))])
        wire += await transform.restored_end()
        arguments = _arguments(protocol, wire)
        read = wellformed.read(arguments)
        assert not read.repeated and read.doc == {"owners": {fake: "owner", value: "viewer"}}
        if protocol == "chat":
            message = {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call-0", "type": "function", "function": {"name": "grant", "arguments": arguments}}
                ],
            }
            upstream, path, headers = OPENAI, "/openai/v1/chat/completions", BEARER
            result = {"role": "tool", "tool_call_id": "call-0", "content": "done"}
        else:
            message = {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-0", "name": "grant", "input": read.doc}],
            }
            upstream, path, headers = ANTHROPIC, "/anthropic/v1/messages", ANTHROPIC_KEY
            result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-0", "content": "done"}]}
        body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "continue"}, message, result]}
        headers = {**headers, "session-id": "own-secret"}
        for _ in range(2):
            refused = await mediator.run(mediator.prepare, upstream, "POST", path, headers, json.dumps(body).encode())
            reason = refused.block_reason or ""
            assert reason.startswith("masking failed: two keys of one of its JSON objects")
            assert secret not in reason and fake not in reason
        body["messages"] = [{"role": "user", "content": "start again"}]
        clean = await mediator.run(
            mediator.prepare,
            upstream,
            "POST",
            path,
            {**headers, "session-id": "new-own-secret"},
            json.dumps(body).encode(),
        )
        assert clean.block_reason is None
    finally:
        if transform is not None:
            transform.close()
        await mediator.run(lambda: None)
        mediator.close()


@pytest.mark.parametrize(("value", "secret"), OWN_SECRETS, ids=SECRET_IDS)
async def test_own_secret_websocket_json_keys_keep_both_values_without_a_history_bypass(value, secret):
    """The Responses WebSocket caller must make the same collision decision and refuse the next colliding turn."""
    mediator = _mediator(ner="off")
    mediator.on_server = True
    session = mediator.engine.session("own-secret-ws")
    rewriter = WSResponseRewriter(mediator, session)
    try:
        fake = mediator.engine.mask_text(value, SlotKind.TOOL_RESULT, session).text
        assert secret not in fake
        raw = json.dumps({"owners": {fake: "owner", value: "viewer"}})
        frames = []
        for event in [_start("responses"), _delta("responses", raw[:17]), _delta("responses", raw[17:])]:
            frames.extend(await rewriter.restored(json.dumps(event)))
        wire = b"".join(_wire("responses", json.loads(frame)) for frame in frames)
        assert _arguments("responses", wire) == ""
        frames.extend(await rewriter.restored(json.dumps(_finish("responses", raw))))
        wire = b"".join(_wire("responses", json.loads(frame)) for frame in frames)
        arguments = _arguments("responses", wire)
        read = wellformed.read(arguments)
        assert not read.repeated and read.doc == {"owners": {fake: "owner", value: "viewer"}}
        turn = {
            "type": "response.create",
            "model": "m",
            "input": [{"type": "function_call", "call_id": "tool-0", "name": "grant", "arguments": arguments}],
        }
        for _ in range(2):
            _, refused = await mediator.run(mediator.mask_ws_frame, json.dumps(turn), session)
            assert refused is not None
            reason = refused.block_reason or ""
            assert reason.startswith("masking failed: two keys of one of its JSON objects")
            assert secret not in reason and fake not in reason
        _, clean = await mediator.run(
            mediator.mask_ws_frame,
            json.dumps({"type": "response.create", "model": "m", "input": "start again"}),
            mediator.engine.session("new-own-secret-ws"),
        )
        assert clean is not None and clean.block_reason is None
    finally:
        rewriter.close()
        await mediator.run(lambda: None)
        mediator.close()
