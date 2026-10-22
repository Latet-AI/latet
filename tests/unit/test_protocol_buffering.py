"""Completed tool arguments keep both colliding keys; held work is bounded and finalized safely.

These tests exercise the SSE transport, including the async path used by the gateway. They intentionally start
before the protocol batch's implementation; execution waits for the coordinator's exclusive test slot.
"""

import asyncio
import json
import threading
from typing import Any

import pytest

from latet.engine import wellformed
from latet.engine.rehydrate import Restore
from latet.gateway.mediate import SSETransform
from latet.protocols.rewrite import RestoreContext, restore_anthropic_message, restore_chat_completion
from latet.protocols.sse import ResponseError, SSEParser, serialize

from .test_json_keys import API_KEY, _taught
from .test_what_leaves import ANTHROPIC, BEARER, OPENAI, _mediator

FAKE = "masked-owner@example.test"
ORIGINAL = "emily.carter@acme.com"
PROTOCOLS = ["chat", "anthropic", "responses"]


def _context(remember=lambda masked, restored: None, original=ORIGINAL, fake=FAKE):
    text = Restore(fake, original, "owner-id", False)
    encoded = Restore(json.dumps(fake)[1:-1], json.dumps(original)[1:-1], "owner-id", False)
    return RestoreContext([text], [encoded], remember)


def _wire(protocol, event):
    return serialize(event.get("type") if protocol != "chat" else None, json.dumps(event))


def _start(protocol, index=0):
    if protocol == "anthropic":
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": f"call-{index}", "name": "grant", "input": {}},
        }
    if protocol == "responses":
        return {
            "type": "response.output_item.added",
            "output_index": index,
            "item": {
                "type": "function_call",
                "id": f"call-{index}",
                "call_id": f"tool-{index}",
                "name": "grant",
                "arguments": "",
            },
        }
    return {
        "id": "chat-1",
        "choices": [
            {
                "index": index,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "grant", "arguments": ""},
                        }
                    ]
                },
            }
        ],
    }


def _delta(protocol, piece, index=0):
    if protocol == "anthropic":
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": piece},
        }
    if protocol == "responses":
        return {
            "type": "response.function_call_arguments.delta",
            "item_id": f"call-{index}",
            "output_index": index,
            "delta": piece,
        }
    return {
        "id": "chat-1",
        "choices": [{"index": index, "delta": {"tool_calls": [{"index": index, "function": {"arguments": piece}}]}}],
    }


def _finish(protocol, arguments, index=0):
    if protocol == "anthropic":
        return {"type": "content_block_stop", "index": index}
    if protocol == "responses":
        return {
            "type": "response.function_call_arguments.done",
            "item_id": f"call-{index}",
            "output_index": index,
            "arguments": arguments,
        }
    return {"id": "chat-1", "choices": [{"index": index, "delta": {}, "finish_reason": "tool_calls"}]}


def _events(wire) -> list[dict[str, Any]]:
    # The consumer parser must not inherit the lowered provider limits under test.
    parser = SSEParser(limit=2**20, events=10_000)
    events = []
    for event in parser.feed(wire):
        doc = event.json()
        if isinstance(doc, dict):
            events.append(doc)
    return events


def _arguments(protocol, wire, index=0):
    parts = []
    for event in _events(wire):
        if protocol == "anthropic" and event.get("index") == index:
            parts.append(event.get("delta", {}).get("partial_json", ""))
        elif protocol == "responses" and event.get("type") == "response.function_call_arguments.delta":
            if event.get("item_id") == f"call-{index}":
                assert event.get("output_index") == index
                parts.append(event.get("delta", ""))
        elif protocol == "chat":
            for choice in event.get("choices", []):
                if choice.get("index") == index:
                    for call in choice.get("delta", {}).get("tool_calls", []):
                        assert call["index"] == index
                        parts.append(call.get("function", {}).get("arguments", ""))
    return "".join(parts)


def _assert_error(protocol, transform, wire, remembered):
    events = _events(wire)
    errors = [event for event in events if "error" in event or event.get("type") == "error"]
    assert len(errors) == 1 and transform.stopped is not None
    diagnostic = json.dumps(errors)
    assert ORIGINAL not in diagnostic and FAKE not in diagnostic
    assert _arguments(protocol, wire) == "" and remembered == []
    assert not transform.rewriter.channels
    assert not getattr(transform.rewriter, "held", None)
    assert not any(choice.get("finish_reason") for event in events for choice in event.get("choices", []))
    assert not any(
        event.get("type") in ("content_block_stop", "response.function_call_arguments.done", "response.completed")
        for event in events
    )
    assert transform.feed(_wire(protocol, _finish(protocol, "{}"))) == b""
    assert transform.end() == b""


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("reverse", [False, True])
def test_streamed_arguments_hold_every_prefix_and_preserve_colliding_keys_at_every_cut(protocol, reverse):
    """Per-fragment restoration or bypassing the whole-document collision branch loses an owner here."""
    pairs = [(FAKE, "owner"), (ORIGINAL, "viewer")]
    if reverse:
        pairs.reverse()
    users = dict(pairs)
    doc = {
        "users": users,
        "records": [{FAKE: "editor", ORIGINAL: "reader"}],
        "nested": [{'quoted\\"key': FAKE, "escaped": "\N{GREEK SMALL LETTER ALPHA}"}],
        "n": 7,
    }
    raw = json.dumps(doc)
    expected = {**doc, "nested": [{'quoted\\"key': ORIGINAL, "escaped": "\N{GREEK SMALL LETTER ALPHA}"}]}
    for cut in range(1, len(raw)):
        transform = SSETransform(protocol, _context(), None, lambda rid: None)
        prefix = transform.feed(_wire(protocol, _start(protocol)))
        prefix += transform.feed(_wire(protocol, _delta(protocol, raw[:cut])))
        prefix += transform.feed(_wire(protocol, _delta(protocol, raw[cut:])))
        assert _arguments(protocol, prefix) == "", f"argument released before completion at cut {cut}"
        wire = prefix + transform.feed(_wire(protocol, _finish(protocol, raw))) + transform.end()
        read = wellformed.read(_arguments(protocol, wire))
        assert not read.repeated and read.doc == expected
        assert not transform.rewriter.channels
        events = _events(wire)
        if protocol == "chat":
            assert sum(bool(c.get("finish_reason")) for e in events for c in e.get("choices", [])) == 1
            assert transform.feed(b"data: [DONE]\n\n") == b"data: [DONE]\n\n"
        else:
            assert sum(e.get("type") == _finish(protocol, raw)["type"] for e in events) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    ("fake", "original"),
    [('masked\\"owner', 'clear\\"owner'), ("masked-\N{GREEK SMALL LETTER ALPHA}", "clear-\N{GREEK SMALL LETTER BETA}")],
    ids=["escaped-quote-and-backslash", "unicode-escape"],
)
def test_colliding_keys_split_inside_json_escapes_keep_both_values(protocol, fake, original):
    raw = json.dumps({fake: "owner", original: "viewer"})
    for cut in range(1, len(raw)):
        transform = SSETransform(protocol, _context(fake=fake, original=original), None, lambda rid: None)
        prefix = transform.feed(_wire(protocol, _start(protocol)))
        prefix += transform.feed(_wire(protocol, _delta(protocol, raw[:cut])))
        prefix += transform.feed(_wire(protocol, _delta(protocol, raw[cut:])))
        assert _arguments(protocol, prefix) == ""
        wire = prefix + transform.feed(_wire(protocol, _finish(protocol, raw))) + transform.end()
        read = wellformed.read(_arguments(protocol, wire))
        assert not read.repeated and read.doc == {fake: "owner", original: "viewer"}


def test_two_chat_tools_of_the_same_choice_complete_once_with_separate_arguments():
    transform = SSETransform("chat", _context(), None, lambda rid: None)
    documents = [json.dumps({FAKE: "owner", ORIGINAL: "viewer"}), json.dumps({"owner": FAKE})]
    wire = b""
    for index, raw in enumerate(documents):
        for event in (_start("chat", index), _delta("chat", raw[:13], index), _delta("chat", raw[13:], index)):
            event["choices"][0]["index"] = 0
            wire += transform.feed(_wire("chat", event))
    assert all(
        not call["function"].get("arguments")
        for event in _events(wire)
        for choice in event.get("choices", [])
        for call in choice["delta"].get("tool_calls", [])
    )
    wire += transform.feed(_wire("chat", _finish("chat", "")))
    wire += transform.feed(b"data: [DONE]\n\n") + transform.end()
    arguments = {0: "", 1: ""}
    finishes = 0
    for event in _events(wire):
        for choice in event.get("choices", []):
            assert choice["index"] == 0
            finishes += bool(choice.get("finish_reason"))
            for call in choice["delta"].get("tool_calls", []):
                arguments[call["index"]] += call["function"].get("arguments", "")
    assert finishes == 1 and wire.count(b"data: [DONE]") == 1
    assert wellformed.read(arguments[0]).doc == {FAKE: "owner", ORIGINAL: "viewer"}
    assert wellformed.read(arguments[1]).doc == {"owner": ORIGINAL}


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_interleaved_calls_keep_their_arguments_and_completion_identity(protocol):
    raw = [json.dumps({FAKE: "owner", ORIGINAL: "viewer"}), json.dumps({"value": FAKE, "n": 3})]
    transform = SSETransform(protocol, _context(), None, lambda rid: None)
    wire = b""
    for index in range(2):
        wire += transform.feed(_wire(protocol, _start(protocol, index)))
    for turn in range(2):
        for index in range(2):
            cut = len(raw[index]) // 2
            piece = raw[index][:cut] if turn == 0 else raw[index][cut:]
            wire += transform.feed(_wire(protocol, _delta(protocol, piece, index)))
    assert all(_arguments(protocol, wire, index) == "" for index in range(2))
    for index in (1, 0):
        wire += transform.feed(_wire(protocol, _finish(protocol, raw[index], index)))
    wire += transform.end()
    assert wellformed.read(_arguments(protocol, wire, 0)).doc == {FAKE: "owner", ORIGINAL: "viewer"}
    assert wellformed.read(_arguments(protocol, wire, 1)).doc == {"value": ORIGINAL, "n": 3}
    assert not transform.rewriter.channels


def test_responses_argument_delta_done_item_and_completed_body_agree():
    raw = json.dumps({"users": {FAKE: "owner", ORIGINAL: "viewer"}})
    item = {"type": "function_call", "id": "call-0", "call_id": "tool-0", "name": "grant", "arguments": raw}
    transform = SSETransform("responses", _context(), None, lambda rid: None)
    events = [
        _start("responses"),
        _delta("responses", raw[:12]),
        _delta("responses", raw[12:]),
        _finish("responses", raw),
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "resp-1", "output": [item]}},
    ]
    for number, event in enumerate(events):
        event["sequence_number"] = number
    wire = b"".join(transform.feed(_wire("responses", event)) for event in events) + transform.end()
    emitted = _events(wire)
    forms = [_arguments("responses", wire)]
    forms += [event["arguments"] for event in emitted if event["type"] == "response.function_call_arguments.done"]
    forms += [event["item"]["arguments"] for event in emitted if event["type"] == "response.output_item.done"]
    forms += [event["response"]["output"][0]["arguments"] for event in emitted if event["type"] == "response.completed"]
    assert len(forms) == 4
    for form in forms:
        read = wellformed.read(form)
        assert not read.repeated and read.doc == {"users": {FAKE: "owner", ORIGINAL: "viewer"}}
    numbers = [event["sequence_number"] for event in emitted]
    assert numbers == sorted(set(numbers))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_provider_written_duplicate_keys_retain_the_existing_last_value_contract(protocol):
    raw = '{"ordinary":"first","ordinary":"last","value":' + json.dumps(FAKE) + "}"
    transform = SSETransform(protocol, _context(), None, lambda rid: None)
    wire = (
        b"".join(
            transform.feed(_wire(protocol, event))
            for event in [_start(protocol), _delta(protocol, raw), _finish(protocol, raw)]
        )
        + transform.end()
    )
    read = wellformed.read(_arguments(protocol, wire))
    assert read.repeated and read.doc == {"ordinary": "last", "value": ORIGINAL}


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_text_channels_still_release_text_before_their_finish(protocol):
    transform = SSETransform(protocol, _context(), None, lambda rid: None)
    if protocol == "chat":
        events = [{"choices": [{"index": 0, "delta": {"content": "hello "}}]}]
    elif protocol == "anthropic":
        events = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello "}},
        ]
    else:
        events = [
            {"type": "response.output_text.delta", "item_id": "text-1", "content_index": 0, "delta": "hello "},
            {"type": "response.output_text.delta", "item_id": "text-1", "content_index": 0, "delta": "next "},
        ]
    wire = b"".join(transform.feed(_wire(protocol, event)) for event in events)
    assert "hello " in wire.decode() and transform.stopped is None


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("ending", ["finish", "eof"])
def test_incomplete_held_arguments_end_once_without_payload_or_provenance(protocol, ending):
    remembered = []
    transform = SSETransform(protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    raw = '{"owner":' + json.dumps(FAKE)
    wire = transform.feed(_wire(protocol, _start(protocol)))
    wire += transform.feed(_wire(protocol, _delta(protocol, raw)))
    wire += transform.feed(_wire(protocol, _finish(protocol, raw))) if ending == "finish" else b""
    wire += transform.end()
    _assert_error(protocol, transform, wire, remembered)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_even_valid_held_json_without_a_protocol_completion_is_not_trusted_at_eof(protocol):
    remembered = []
    transform = SSETransform(protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    wire = transform.feed(_wire(protocol, _start(protocol)))
    wire += transform.feed(_wire(protocol, _delta(protocol, json.dumps({"owner": FAKE}))))
    wire += transform.end()
    _assert_error(protocol, transform, wire, remembered)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_aggregate_held_channels_share_one_response_bound(monkeypatch, protocol):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 4096)
    remembered = []
    transform = SSETransform(protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    wire = b""
    index = -1
    for index in range(40):
        wire += transform.feed(_wire(protocol, _start(protocol, index)))
        wire += transform.feed(_wire(protocol, _delta(protocol, '{"pad":"' + "x" * 256, index)))
        if transform.stopped is not None:
            break
    assert index < 39
    _assert_error(protocol, transform, wire, remembered)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_event_limit_failure_discards_all_held_arguments(monkeypatch, protocol):
    monkeypatch.setattr("latet.protocols.sse.MAX_EVENTS", 12)
    remembered = []
    transform = SSETransform(protocol, _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    wire = b""
    for index in range(10):
        wire += transform.feed(_wire(protocol, _start(protocol, index)))
        wire += transform.feed(_wire(protocol, _delta(protocol, '{"value":' + json.dumps(FAKE), index)))
        if transform.stopped is not None:
            break
    _assert_error(protocol, transform, wire, remembered)


def test_responses_retained_channel_identifiers_are_part_of_the_aggregate_bound(monkeypatch):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 4096)
    remembered = []
    transform = SSETransform("responses", _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    wire = b""
    for index in range(20):
        event = _delta("responses", "{", index)
        event["item_id"] = f"call-{index}-" + "m" * 512
        wire += transform.feed(_wire("responses", event))
        if transform.stopped is not None:
            break
    _assert_error("responses", transform, wire, remembered)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "original",
    ["x" * 8192, '"' * 2100, "\N{GREEK SMALL LETTER ALPHA}" * 2100],
    ids=["text-growth", "json-escaping", "utf8-bytes"],
)
def test_restoration_expansion_is_refused_before_sending_or_remembering_it(monkeypatch, protocol, original):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 4096)
    remembered = []
    ctx = _context(lambda m, r: remembered.append((m, r)), original=original)
    transform = SSETransform(protocol, ctx, None, lambda rid: None)
    raw = json.dumps({"value": FAKE})
    wire = (
        b"".join(
            transform.feed(_wire(protocol, event))
            for event in [_start(protocol), _delta(protocol, raw), _finish(protocol, raw)]
        )
        + transform.end()
    )
    _assert_error(protocol, transform, wire, remembered)
    assert b"x" * 128 not in wire


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("async_path", [False, True])
def test_rewriter_response_errors_become_one_safe_protocol_error(monkeypatch, protocol, async_path):
    remembered = []
    ctx = _context(lambda m, r: remembered.append((m, r)))
    transform = SSETransform(protocol, ctx, None, lambda rid: None)
    prefix = transform.feed(_wire(protocol, _start(protocol)))
    prefix += transform.feed(_wire(protocol, _delta(protocol, json.dumps({"value": FAKE}))))

    def fail(*args):
        raise ResponseError("the provider's tool arguments could not be read")

    name = "on_chunk" if protocol == "chat" else "on_event"
    monkeypatch.setattr(transform.rewriter, name, fail)
    chunk = _wire(protocol, _finish(protocol, json.dumps({"value": FAKE})))

    async def relay():
        return b"".join([out async for out in transform.restored(chunk)]) + await transform.restored_end()

    wire = prefix + (asyncio.run(relay()) if async_path else transform.feed(chunk) + transform.end())
    _assert_error(protocol, transform, wire, remembered)


@pytest.mark.parametrize("async_path", [False, True])
def test_responses_finalizer_errors_are_converted_on_sync_and_async_end(monkeypatch, async_path):
    remembered = []
    transform = SSETransform("responses", _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    prefix = transform.feed(_wire("responses", _start("responses")))
    prefix += transform.feed(_wire("responses", _delta("responses", json.dumps({"owner": FAKE}))))

    def fail():
        raise ResponseError("the provider's tool arguments did not finish")

    monkeypatch.setattr(transform.rewriter, "on_end", fail)
    tail = asyncio.run(transform.restored_end()) if async_path else transform.end()
    _assert_error("responses", transform, prefix + tail, remembered)


def _anthropic_argument_sources(wire):
    sources = []
    fragments = []
    for event in _events(wire):
        if event.get("type") == "content_block_start" and event.get("content_block", {}).get("input"):
            sources.append(event["content_block"]["input"])
        if event.get("type") == "content_block_delta":
            fragments.append(event.get("delta", {}).get("partial_json", ""))
    if "".join(fragments):
        sources.append(json.loads("".join(fragments)))
    return sources


@pytest.mark.parametrize("empty_delta", [False, True])
def test_a_nonempty_anthropic_start_is_one_complete_collision_safe_argument_source(empty_delta):
    transform = SSETransform("anthropic", _context(), None, lambda rid: None)
    start = _start("anthropic")
    start["content_block"]["input"] = {"users": {FAKE: "owner", ORIGINAL: "viewer"}, "value": FAKE}
    prefix = transform.feed(_wire("anthropic", start))
    if empty_delta:
        prefix += transform.feed(_wire("anthropic", _delta("anthropic", "")))
    assert _anthropic_argument_sources(prefix) == []
    wire = prefix + transform.feed(_wire("anthropic", _finish("anthropic", ""))) + transform.end()
    assert _anthropic_argument_sources(wire) == [{"users": {FAKE: "owner", ORIGINAL: "viewer"}, "value": ORIGINAL}]
    assert transform.stopped is None and not transform.rewriter.channels


def test_a_second_nonempty_anthropic_argument_source_is_refused_without_merge_or_success():
    remembered = []
    transform = SSETransform("anthropic", _context(lambda m, r: remembered.append((m, r))), None, lambda rid: None)
    start = _start("anthropic")
    start["content_block"]["input"] = {"owner": FAKE}
    wire = transform.feed(_wire("anthropic", start))
    wire += transform.feed(_wire("anthropic", _delta("anthropic", '{"other":1}')))
    assert transform.stopped is not None
    assert len([event for event in _events(wire) if event.get("type") == "error"]) == 1
    assert _anthropic_argument_sources(wire) == [] and remembered == []
    wire += transform.feed(_wire("anthropic", _finish("anthropic", ""))) + transform.end()
    assert _anthropic_argument_sources(wire) == []
    _assert_error("anthropic", transform, wire, remembered)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_tiny_finish_finalizes_large_held_arguments_on_the_engine_thread(monkeypatch, protocol):
    mediator = _mediator()
    ctx = _context()
    transform = SSETransform(protocol, ctx, None, lambda rid: None, mediator=mediator)
    on_loop = threading.get_ident()
    finalized = []
    original = RestoreContext.json_text

    def where(self, value):
        if len(value) > 64 * 1024:
            finalized.append(threading.get_ident())
        return original(self, value)

    monkeypatch.setattr(RestoreContext, "json_text", where)
    raw = json.dumps({"pad": "a" * (70 * 1024), "owner": FAKE})

    async def relay():
        wire = b""
        events = [_start(protocol)] + [_delta(protocol, raw[n : n + 1024]) for n in range(0, len(raw), 1024)]
        for event in events:
            wire += b"".join([out async for out in transform.restored(_wire(protocol, event))])
        assert _arguments(protocol, wire) == ""
        # Responses' done carries the document as well. Anthropic/chat finish events are tiny.
        wire += b"".join([out async for out in transform.restored(_wire(protocol, _finish(protocol, raw)))])
        return wire + await transform.restored_end()

    wire = asyncio.run(relay())
    assert json.loads(_arguments(protocol, wire)) == {"pad": "a" * (70 * 1024), "owner": ORIGINAL}
    assert finalized and all(thread != on_loop for thread in finalized)


@pytest.mark.parametrize("protocol", ["chat", "anthropic"])
@pytest.mark.parametrize("streamed", [False, True], ids=["whole-characterization", "streamed-regression"])
def test_preserved_colliding_tool_history_stays_refused_until_the_call_is_removed(protocol, streamed):
    """EOP-20 is a documented limitation: preserving data does not grant a history/privacy exception."""
    mediator = _mediator()
    mediator.on_server = True
    route = "chat completions" if protocol == "chat" else "anthropic with an API key"
    fake = _taught(mediator, route)
    session = mediator.engine.session("session-id:s-1")
    raw = json.dumps({"users": {fake: "owner", ORIGINAL: "viewer"}})
    ctx = mediator.restore_context(session)
    if streamed:
        transform = SSETransform(protocol, ctx, None, lambda rid: None)
        wire = (
            b"".join(
                transform.feed(_wire(protocol, event))
                for event in [
                    _start(protocol),
                    _delta(protocol, raw[:15]),
                    _delta(protocol, raw[15:]),
                    _finish(protocol, raw),
                ]
            )
            + transform.end()
        )
        arguments = _arguments(protocol, wire)
    elif protocol == "chat":
        call = {"id": "call-0", "type": "function", "function": {"name": "grant", "arguments": raw}}
        restored = restore_chat_completion(ctx, {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]})
        arguments = restored["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    else:
        restored = restore_anthropic_message(
            ctx, {"content": [{"type": "tool_use", "id": "call-0", "name": "grant", "input": json.loads(raw)}]}
        )
        arguments = json.dumps(restored["content"][0]["input"])
    read = wellformed.read(arguments)
    assert not read.repeated and read.doc == {"users": {fake: "owner", ORIGINAL: "viewer"}}
    if protocol == "chat":
        history = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call-0", "type": "function", "function": {"name": "grant", "arguments": arguments}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-0", "content": "done"},
        ]
        upstream, path, headers = OPENAI, "/openai/v1/chat/completions", BEARER
    else:
        history = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-0", "name": "grant", "input": read.doc}],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-0", "content": "done"}]},
        ]
        upstream, path, headers = ANTHROPIC, "/anthropic/v1/messages", API_KEY
    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "go on"}, *history]}
    headers = {**headers, "session-id": "s-1"}
    for _ in range(2):
        refused = mediator.prepare(upstream, "POST", path, headers, json.dumps(body).encode())
        reason = refused.block_reason or ""
        assert reason.startswith("masking failed: two keys of one of its JSON objects")
        assert fake not in reason and ORIGINAL not in reason
    body["messages"] = [{"role": "user", "content": "start again"}]
    clean = mediator.prepare(
        upstream, "POST", path, {**headers, "session-id": "new-conversation"}, json.dumps(body).encode()
    )
    assert clean.block_reason is None
