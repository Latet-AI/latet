"""Positive History compatibility witnesses for the exact admitted projection."""

from __future__ import annotations

import asyncio
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
from .test_response_history_choices import _event
from .test_response_sink_failures import _REQUEST, _TIMEOUT, _sinks, _wire
from .test_response_sinks import _TOKENS, _payload, _records


async def _observe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deltas: tuple[tuple[int, str], ...]
) -> tuple[str, dict[str, bool]]:
    events = (
        *(_event([{"index": index, "delta": {"content": text}, "finish_reason": None}]) for index, text in deltas),
        _event(
            [{"index": index, "delta": {}, "finish_reason": "stop"} for index in range(2)],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        ),
        b"data: [DONE]\n\n",
    )
    expected = b"".join(events)
    assert len(events) <= 5 and len(expected) <= 16 * 1024
    assert all(safe_text(event.decode()) for event in events), "the raw events must be independently safe"

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
    text = persisted.get("text") or ""
    assert isinstance(text, str), "history text retains its existing schema"
    return text, {
        "one provider exchange closed": requests == 1 and answer.closed,
        "one actual screen admitted": admissions == [True],
        "wire status and bytes preserved": status == 200 and wire == expected,
        "raw original event boundaries preserved": raw == expected.decode(),
        "history usage and no omission": detail["summary"]["tokens"] == _TOKENS and not persisted.get("omitted"),
    }


async def test_history_keeps_the_ordinary_join(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deltas = ((0, "ordinary "), (0, "same-lane "), (1, "next choice"))
    text, checks = await _observe(tmp_path, monkeypatch, deltas)
    checks["ordinary history join is byte-identical"] = text == "ordinary same-lane next choice"
    assert all(checks.values()), checks


async def test_history_checks_the_escaped_cross_choice_join(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = ("%6Catet_", "lk_" + "Ab3_" * 10 + "XYw")
    clear = "latet_" + parts[1]
    joined = "".join(parts)
    fixture = {
        "each complete lane is safe": all(safe_text(part) for part in parts),
        "shape exists only in the escaped reading": clear not in joined and not safe_text(joined),
    }
    assert all(fixture.values()), fixture

    text, checks = await _observe(tmp_path, monkeypatch, ((0, parts[0]), (1, parts[1])))
    checks["history keeps original parts at a real boundary"] = text == "\n".join(parts)
    checks["exact persisted projection passes escaped screening"] = safe_text(text)
    assert all(checks.values()), checks
