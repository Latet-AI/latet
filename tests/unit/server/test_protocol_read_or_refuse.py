"""Fallback answer schemas are inspected; the adopted unsupported text forms never reach a server's provider."""

import json

import pytest

from .test_reads_or_refuses import (
    ANTHROPIC,
    ANTHROPIC_KEY,
    BEARER,
    CLAUDE_CODE,
    EMAIL,
    FIELDS_REFUSED,
    SUBSCRIPTION,
    _anthropic,
    _client,
    _prepare,
    _upstream,
)

HEADERS = [ANTHROPIC_KEY, CLAUDE_CODE, SUBSCRIPTION]
CLIENTS = ["rewrite", "claude-code", "subscription"]


def _fallback(fmt):
    return {"model": "fallback-model", "output_config": {"format": fmt, "effort": "high"}}


SENSITIVE_FORMATS = [
    {"type": "json_schema", "description": EMAIL, "schema": {"type": "object"}},
    {"type": "json_schema", "schema": {"type": "object", "description": EMAIL}},
    {"type": "json_schema", "schema": {"type": "object", "properties": {EMAIL: {"type": "string"}}}},
    {"type": "json_schema", "schema": {"type": "string", "enum": [EMAIL]}},
    {"type": "json_schema", "schema": {"type": "string", "default": EMAIL}},
    {"type": "json_schema", "schema": {"type": "string", "examples": [EMAIL]}},
    {"type": "json_schema", "schema": {"properties": {"nested": {"items": {"description": EMAIL}}}}},
]
SAFE_FORMAT = {"type": "json_schema", "schema": {"type": "object", "properties": {"status": {"type": "string"}}}}


@pytest.mark.parametrize(
    "fmt",
    SENSITIVE_FORMATS,
    ids=["format-description", "schema-description", "key", "enum", "default", "example", "nested"],
)
@pytest.mark.parametrize("position", [0, 1])
def test_every_fallback_schema_string_leaves_masked_without_changing_settings(fmt, position):
    fallbacks = [_fallback(SAFE_FORMAT), _fallback(SAFE_FORMAT)]
    fallbacks[position] = _fallback(fmt)
    body = _anthropic(fallbacks=fallbacks, output_config={"format": SAFE_FORMAT})
    prepared, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert prepared.block_reason is None and EMAIL.encode() not in prepared.body
    sent = json.loads(prepared.body)
    assert sent["fallbacks"][1 - position] == fallbacks[1 - position]
    assert all(
        entry["model"] == "fallback-model" and entry["output_config"]["effort"] == "high" for entry in sent["fallbacks"]
    )
    assert sent["output_config"] == body["output_config"]
    assert sent["fallbacks"][position]["output_config"]["format"] != fmt


@pytest.mark.parametrize("headers", [CLAUDE_CODE, SUBSCRIPTION], ids=CLIENTS[1:])
@pytest.mark.parametrize("fmt", SENSITIVE_FORMATS)
def test_fallback_schemas_are_checked_before_the_claude_tool_exception(headers, fmt):
    body = _anthropic(fallbacks=[{"model": "first"}, _fallback(fmt)])
    refused, raw = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    assert refused.block_reason is not None and refused.body == raw
    assert EMAIL not in refused.block_reason


@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
@pytest.mark.parametrize(
    "fallbacks",
    [
        [],
        [{"model": "backup"}],
        [_fallback(SAFE_FORMAT)],
        [{"model": "backup", "output_config": {"effort": "high"}}],
        [_fallback({"type": "json_schema", "schema": True})],
        [_fallback({"type": "json_schema", "schema": False})],
        [_fallback({})],
    ],
)
def test_safe_and_model_only_fallbacks_keep_their_original_bytes(headers, fallbacks):
    prepared, raw = _prepare(True, "/anthropic/v1/messages", _anthropic(fallbacks=fallbacks), ANTHROPIC, headers)
    assert prepared.block_reason is None and prepared.body == raw


MALFORMED_FALLBACKS = [
    "not-a-list",
    {"model": EMAIL},
    [None],
    [EMAIL],
    [3],
    [{"model": "first"}, EMAIL],
    [{"output_config": EMAIL}],
    [{"output_config": []}],
    [{"output_config": {"format": EMAIL}}],
    [{"output_config": {"format": []}}],
    [_fallback({"type": "json_schema", "schema": EMAIL})],
    [_fallback({"type": "json_schema", "description": {"text": EMAIL}})],
]


@pytest.mark.parametrize("fallbacks", MALFORMED_FALLBACKS)
@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
def test_malformed_fallback_schema_shapes_are_refused_with_content_free_reasons(headers, fallbacks):
    body = _anthropic(fallbacks=fallbacks)
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    reason = refused.block_reason or ""
    assert reason.startswith(FIELDS_REFUSED)
    assert EMAIL not in reason and "not-a-list" not in reason
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize(
    ("headers", "malformed"),
    [(CLAUDE_CODE, False), (SUBSCRIPTION, False), (ANTHROPIC_KEY, True), (CLAUDE_CODE, True), (SUBSCRIPTION, True)],
    ids=["checked-claude", "checked-subscription", "malformed-rewrite", "malformed-claude", "malformed-subscription"],
)
def test_space_gateway_does_not_forward_an_unsafe_or_unreadable_fallback_schema(space, headers, malformed, caplog):
    rt, _ = space
    seen = _upstream(rt)
    fallback = {"output_config": EMAIL} if malformed else _fallback(SENSITIVE_FORMATS[2])
    body = _anthropic(fallbacks=[{"model": "safe"}, fallback])
    with _client(rt) as client:
        response = client.post("/llm/v1/messages", json=body, headers=headers)
    assert response.status_code == 400 and "Nothing was sent" in response.text and seen == []
    assert EMAIL not in response.text and EMAIL not in caplog.text


@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
def test_space_gateway_passes_safe_fallbacks_and_masks_rewrite_schemas(space, headers):
    rt, _ = space
    seen = _upstream(rt)
    checked = headers is not ANTHROPIC_KEY
    body = _anthropic(fallbacks=[{"model": "safe"}, _fallback(SAFE_FORMAT if checked else SENSITIVE_FORMATS[2])])
    raw = json.dumps(body, indent=2).encode()
    with _client(rt) as client:
        response = client.post("/llm/v1/messages", content=raw, headers=headers)
    assert response.status_code == 200 and len(seen) == 1
    assert EMAIL.encode() not in seen[0].content
    if checked:
        assert seen[0].content == raw
    assert json.loads(seen[0].content)["fallbacks"][0] == {"model": "safe"}


def _document(**fields):
    return {"type": "document", "source": {"type": "url", "url": "https://example.com/report.pdf"}, **fields}


def _search(value):
    return {"type": "search_result", "source": value, "title": value, "content": [{"type": "text", "text": value}]}


def _content(block, nested):
    return [{"type": "tool_result", "tool_use_id": "call-1", "content": [block]}] if nested else [block]


@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("kind", ["search_result", "title", "context"])
@pytest.mark.parametrize("value", ["ordinary text", EMAIL, None, {"unexpected": EMAIL}])
def test_named_anthropic_forms_are_refused_even_when_images_allow_them(headers, nested, role, kind, value):
    block = _search(value) if kind == "search_result" else _document(**{kind: value})
    body = _anthropic(messages=[{"role": role, "content": _content(block, nested)}])
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    reason = refused.block_reason or ""
    assert reason.startswith(FIELDS_REFUSED)
    assert EMAIL not in reason and "ordinary text" not in reason
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize("role", ["user", "assistant", "function"])
@pytest.mark.parametrize("name", ["query", EMAIL, None, 3, {"unexpected": EMAIL}])
def test_any_chat_message_name_is_refused_including_legacy_function_names(role, name):
    body = {"model": "m", "messages": [{"role": role, "name": name, "content": "done"}]}
    refused, _ = _prepare(True, "/openai/v1/chat/completions", body)
    reason = refused.block_reason or ""
    assert reason.startswith(FIELDS_REFUSED)
    assert EMAIL not in reason and "query" not in reason
    computer, raw = _prepare(False, "/openai/v1/chat/completions", body)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
@pytest.mark.parametrize("nested", [False, True])
def test_an_untitled_document_keeps_the_existing_server_policy(headers, nested):
    body = _anthropic(messages=[{"role": "user", "content": _content(_document(), nested)}])
    prepared, raw = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    assert prepared.block_reason is None and prepared.body == raw and prepared.uninspected == ["document"]


@pytest.mark.parametrize("kind", ["name", "search_result", "title", "context"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("headers", HEADERS, ids=CLIENTS)
def test_space_gateway_refuses_named_uninspected_text_without_forwarding(space, kind, nested, headers, caplog):
    rt, _ = space
    seen = _upstream(rt)
    if kind == "name":
        path = "/llm/v1/chat/completions"
        body = {"model": "m", "messages": [{"role": "assistant", "name": EMAIL, "content": "done"}]}
        headers = BEARER
    else:
        path = "/llm/v1/messages"
        block = _search(EMAIL) if kind == "search_result" else _document(**{kind: EMAIL})
        body = _anthropic(messages=[{"role": "user", "content": _content(block, nested)}])
    with _client(rt) as client:
        response = client.post(path, json=body, headers=headers)
    assert response.status_code == 400 and seen == [] and "Nothing was sent" in response.text
    assert EMAIL not in response.text and EMAIL not in caplog.text
