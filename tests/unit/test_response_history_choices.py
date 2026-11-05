"""Causal History witness for two independently safe Chat choices."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.responses import StreamingResponse

from latet.engine.response_guard import safe_text
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.recorder import ResponseCapture
from latet.gateway.routes import RouteTable

from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _REQUEST, _TIMEOUT, _sinks, _wire
from .test_response_sinks import _TOKENS, _payload, _records


def _event(choices: list[dict[str, object]], **extra: object) -> bytes:
    chunk = {
        "id": "chatcmpl-sink",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": choices,
        **extra,
    }
    return ("data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n").encode()


async def test_history_preserves_independent_chat_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = ("latet_", "lk_" + "Ab3_" * 10 + "XYw")
    shape = "".join(parts)
    events = (
        *(
            _event([{"index": index, "delta": {"content": part}, "finish_reason": None}])
            for index, part in enumerate(parts)
        ),
        _event([{"index": index, "delta": {}, "finish_reason": "stop"} for index in range(2)]),
        _event([], usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
        b"data: [DONE]\n\n",
    )
    expected = b"".join(events)
    assert len(events) == 5 and len(expected) <= 16 * 1024
    assert all(safe_text(part) for part in parts) and not safe_text(shape)
    assert all(safe_text(event.decode()) for event in events)

    answer = _Answer(expected)
    directory = tmp_path / "capture"
    requests = 0
    admissions: list[bool] = []
    screen = ResponseCapture.screen

    def observe(capture: ResponseCapture) -> bool:
        allowed = screen(capture)
        admissions.append(allowed and capture.omission is None)
        return allowed

    def provider(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

    monkeypatch.setattr(ResponseCapture, "screen", observe)
    with _sinks(tmp_path) as (mediator, recorder, store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            assert isinstance(response, StreamingResponse)
            status, wire = await asyncio.wait_for(_wire(response, request), _TIMEOUT)
        assert recorder.drain(_TIMEOUT), "the history job did not drain"
        public = store.list()[0]
        assert len(public) == 1, "exactly one history row required"
        detail = store.detail(public[0]["id"])
        assert detail is not None

    raw = _payload(_records(directory))
    persisted = detail.get("response") or {}
    history_text = persisted.get("text") or ""
    first = history_text.find(parts[0])
    second = history_text.find(parts[1], first + len(parts[0]))
    checks = {
        "one provider exchange closed": requests == 1 and answer.closed,
        "one actual screen admitted": admissions == [True],
        "wire status and bytes preserved": status == 200 and wire == expected,
        "raw original event boundaries preserved": raw == expected.decode(),
        "history preserves both fragments in order": first >= 0 and second >= 0,
        "history usage and no omission": detail["summary"]["tokens"] == _TOKENS and not persisted.get("omitted"),
        "public history has no synthesized shape": shape not in json.dumps(public, ensure_ascii=False),
        "sensitive history has no synthesized shape": shape not in json.dumps(detail, ensure_ascii=False),
    }
    assert all(checks.values()), checks
