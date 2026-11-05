"""Declared JSON occurrence, compatibility and accounting regressions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.responses import StreamingResponse

from latet.engine import escapes
from latet.engine.response_guard import safe_text
from latet.engine.types import SlotKind
from latet.gateway import body_coverage, coverage
from latet.gateway.body_coverage import BodyRefused, Ledger
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.routes import RouteTable
from latet.protocols.base import JsonRef, SlotRef

from .test_request_launch_confidentiality import _ANSWER, _SHAPE
from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire

_SEMANTIC = "%6C" + _SHAPE[1:]
_QUOTED = r"\u0025" + _SEMANTIC[1:]
_ARGUMENTS = {
    "semantic-escape": '{"' + _QUOTED + '":"ordinary"}',
    "losing-duplicate": '{"same":{"' + _QUOTED + '":"first"},"same":{"ordinary":"last"}}',
    "ordinary-duplicate": '{"same":"first","same":"last"}',
    "ordinary-malformed": '{"ordinary":"value",}',
}


@pytest.mark.parametrize("case", tuple(_ARGUMENTS))
async def test_http_declared_json_occurrences_never_send_a_launch_key(tmp_path: Path, case: str) -> None:
    """A tool's serialized argument whose occurrences hold a live launch key's shape only once read (an escape inside
    an escape, a losing duplicate): no shape leaves or stays in a sink. The escaped key, which no masking places, refuses
    the request, saying how to go on; the losing duplicate is dropped as the request is written anew from what was read
    (root's ruling I-6: a key Latet can mask is masked). Ordinary occurrences leave. (Before D5 these ran in an off
    project, which sent its requests as they came; nothing leaves an off project now, so the project is on here.)"""
    argument = _ARGUMENTS[case]
    protected = case in ("semantic-escape", "losing-duplicate")
    if protected:
        assert safe_text(argument), "the whole raw unit cannot establish semantic refusal"
    if case == "losing-duplicate":
        assert _SEMANTIC not in json.dumps(json.loads(argument)), "the parsed winner discards the protected occurrence"

    body = json.dumps(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "Please query."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_c1", "type": "function", "function": {"name": "query", "arguments": argument}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_c1", "content": "ordinary tool result"},
            ],
        }
    ).encode()
    assert len(body) <= 16 * 1024 and len(_ANSWER) <= 16 * 1024
    request = _request(body)
    directory = tmp_path / "capture"
    answer = _Answer(_ANSWER)
    sends: list[bytes] = []

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sends and len(incoming.content) <= 16 * 1024, "one bounded provider attempt"
        sends.append(incoming.content)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    def has_shape(text: str) -> bool:
        return any(value in text for value in (_SHAPE, _SEMANTIC, _QUOTED))

    with _sinks(tmp_path) as (mediator, recorder, store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            cached_source, cached_reading = escapes._last
            cache_clean = not has_shape(cached_source) and (
                cached_reading is None or not has_shape(cached_reading.text)
            )
            del cached_source, cached_reading
            if isinstance(response, StreamingResponse):
                status, wire = await asyncio.wait_for(_wire(response, request), _TIMEOUT)
            else:
                status, wire = response.status_code, bytes(response.body)
        assert recorder.drain(_TIMEOUT), "one request history drain"
        public = store.list()[0]
        assert len(public) <= 1, "at most one request history entry"
        details = [store.detail(row["id"]) for row in public]

    raw_paths = list(directory.glob("*.jsonl"))
    assert len(raw_paths) <= 1, "at most one request raw capture"
    raw = "".join(path.read_text("utf-8") for path in raw_paths)
    refused = case == "semantic-escape"
    checks = {
        "one attempt, none for the refused key": len(sends) == (0 if refused else 1),
        "provider stream closure matches attempt": answer.closed == bool(sends),
        "nothing sent holds the shape": not any(has_shape(sent.decode()) for sent in sends),
    }
    if case == "losing-duplicate":
        checks["the request goes on without the losing occurrence"] = status == 200 and wire == _ANSWER
        checks["raw history and cache contain no protected unit"] = (
            not has_shape(raw) and not has_shape(json.dumps((public, details))) and cache_clean
        )
    elif refused:
        refusal = json.loads(wire) if 400 <= status < 500 else {}
        error = refusal.get("error", {})
        message = error.get("message") if isinstance(error, dict) else None
        checks["fixed refusal without original"] = (
            isinstance(message, str)
            and "request contains a protected launch credential" in message
            and response.headers.get("x-should-retry") == "false"
            and not has_shape(wire.decode())
        )
        checks["raw history and cache contain no protected unit"] = (
            not has_shape(raw) and not has_shape(json.dumps((public, details))) and cache_clean
        )
    else:
        checks["ordinary response and sinks present"] = (
            status == 200 and wire == _ANSWER and bool(raw) and len(public) == 1
        )
    assert all(checks.values()), checks


def _serialized(value: str) -> SlotRef:
    return SlotRef({"argument": value}, "argument", SlotKind.TOOL_INPUT, True, mode="json")


def test_declared_occurrences_share_outer_and_future_ledger_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger(limits=replace(coverage.LIMITS, occurrences=8), envelope=1)
    ledger.preflight(b'{"outer":"value"}')
    raw, links = tuple(ledger.raw), dict(ledger.links)

    assert ledger.declared_slot_safe(_serialized('{"key":"value"}'))
    assert ledger.declared_slot_safe(_serialized("[]"))
    assert ledger.declared_occurrences == 4 and tuple(ledger.raw) == raw and ledger.links == links

    def no_decode(*_args: object) -> bool:
        pytest.fail("an exhausted occurrence must not reach token decoding")

    monkeypatch.setattr(body_coverage, "_declared_quoted_safe", no_decode)
    with pytest.raises(BodyRefused, match="request coverage limit exceeded"):
        ledger.declared_slot_safe(_serialized('"next"'))
    with pytest.raises(BodyRefused, match="request coverage limit exceeded"):
        ledger.charge(1, 0)
    assert ledger.declared_occurrences == 4


def test_declared_serialized_depth_matches_outer_preflight() -> None:
    depth = coverage.LIMITS.depth
    nested = "[" * depth + '"ordinary"' + "]" * depth
    ledger = Ledger()
    assert ledger.declared_slot_safe(_serialized(nested))
    with pytest.raises(BodyRefused, match="request coverage limit exceeded"):
        ledger.declared_slot_safe(_serialized("[" + nested + "]"))

    malformed = '{"' + _QUOTED + '":"ordinary",}'
    assert safe_text(malformed), "the raw fallback alone cannot see the protected key"
    assert not ledger.declared_slot_safe(_serialized(malformed)), "a decodable token is still screened"


def test_declared_json_ref_reads_existing_tree_at_exact_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger()

    def no_serialize(_slot: JsonRef) -> str:
        pytest.fail("admission must not serialize an existing JsonRef")

    monkeypatch.setattr(JsonRef, "get", no_serialize)
    tree: object = "ordinary"
    for _ in range(coverage.LIMITS.depth):
        tree = [tree]
    slot = JsonRef({"schema": tree}, "schema", SlotKind.TOOL_DESCRIPTION, mode="json")
    assert ledger.declared_slot_safe(slot)
    assert ledger.declared_occurrences == 0
    slot.container[slot.key] = {_SEMANTIC: "ordinary"}
    assert not ledger.declared_slot_safe(slot), "declared keys are checked as individual units"
    slot.container[slot.key] = [tree]
    with pytest.raises(BodyRefused, match="request coverage limit exceeded"):
        ledger.declared_slot_safe(slot)
