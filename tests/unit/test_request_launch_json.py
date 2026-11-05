"""Actual HTTP confidentiality for a declared serialized tool argument."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

from latet.engine import escapes
from latet.engine.types import SlotKind
from latet.gateway import forward, mediate
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.outcome import Forward, Pass
from latet.gateway.routes import RouteTable

from .test_request_launch_confidentiality import _ANSWER, _SHAPE
from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-key", "protected-key"])
async def test_http_a_launch_key_as_a_declared_tool_argument_key_is_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    """A live launch key written (escaped) as a key of a tool's serialized argument: masked like any secret of a
    request Latet rewrites (R-103, root's ruling I-6), so it never reaches the provider, the capture, the history or the
    escaped reading's cache; an ordinary argument leaves as it came. (Before D5, an off project sent its requests as they
    came and the key had to be refused first; now nothing leaves an off project, so the project is on here.)"""
    key = _SHAPE if protected else "latet_lk_notes"
    spelling = r"\u006c" + key[1:]
    argument = '{"' + spelling + '":"ordinary value"}'
    assert len(argument.encode()) < 128 and json.loads(argument) == {key: "ordinary value"}
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
    assert _SHAPE not in argument and _SHAPE.encode() not in body
    request = _request(body)
    directory = tmp_path / "capture"
    answer = _Answer(_ANSWER)
    sends: list[bytes] = []
    passes: list[bool] = []
    scanned: list[bool] = []
    review_calls: list[bool] = []
    scan = mediate._scan
    finalize = forward.finalize

    def observe_scan(protocol: str, document: dict[str, Any]):
        selected = scan(protocol, document)
        function = document["messages"][1]["tool_calls"][0]["function"]
        matches = sum(
            slot.kind == SlotKind.TOOL_INPUT
            and slot.mode == "json"
            and slot.from_model
            and slot.container is function
            and slot.key == "arguments"
            and slot.get() == argument
            for slot in selected.slots
        )
        scanned.append(matches == 1)
        return selected

    def observe_finalize(arena, *args, **kwargs):
        decision = finalize(arena, *args, **kwargs)
        passes.append(isinstance(decision, Pass | Forward))
        return decision

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sends and len(incoming.content) <= 16 * 1024, "one bounded provider attempt"
        sends.append(incoming.content)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    def has_key(text: str) -> bool:
        # The spelling is a substring even when a storage layer doubles the backslash.
        return key in text or spelling in text

    monkeypatch.setattr(mediate, "_scan", observe_scan)
    monkeypatch.setattr(forward, "finalize", observe_finalize)
    with _sinks(tmp_path) as (mediator, recorder, store):
        local_items = mediator._review_items
        live_items = mediator._live_items

        def local_probe(*args, **kwargs):
            items = local_items(*args, **kwargs)
            review_calls.append(protected and has_key(repr(items)))  # what the reviewer would be given
            return items

        def live_probe(*args, **kwargs):
            items = live_items(*args, **kwargs)
            review_calls.append(protected and has_key(repr(items)))
            return items

        monkeypatch.setattr(mediator, "_review_items", local_probe)
        monkeypatch.setattr(mediator, "_live_items", live_probe)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            cached_source, cached_reading = escapes._last
            cache_clean = not has_key(cached_source) and (cached_reading is None or not has_key(cached_reading.text))
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
    history = json.dumps((public, details), ensure_ascii=False)
    sent = sends[0].decode() if len(sends) == 1 else ""
    checks = {
        "real scan selects the exact declared argument slot": scanned == [True],
        "one sendable decision, one provider attempt": passes == [True] and len(sends) == 1,
        "provider stream closure matches attempt": answer.closed == bool(sends),
        "the provider's answer handed back": status == 200 and wire == _ANSWER,
        "raw capture carries no protected key": not protected or not has_key(raw),
        "history carries no protected key": not protected or not has_key(history),
        "no local or live reviewer input holds the key": not any(review_calls),
    }
    if protected:
        argument_sent = json.loads(json.loads(sent)["messages"][1]["tool_calls"][0]["function"]["arguments"])
        checks["the key masked in the argument's key, its value kept"] = not has_key(sent) and list(
            argument_sent.values()
        ) == ["ordinary value"]
        checks["escaped reading cache holds no protected spelling or key"] = cache_clean
    else:
        checks["the ordinary argument leaves as it came"] = sends == [body]
    assert all(checks.values()), checks
