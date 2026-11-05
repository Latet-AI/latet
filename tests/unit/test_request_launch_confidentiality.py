"""Actual HTTP confidentiality before the existing project-off shortcut."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.config import Config, ProjectEntry
from latet.engine import escapes
from latet.engine.response_guard import safe_text
from latet.gateway import forward
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.outcome import Pass
from latet.gateway.routes import RouteTable
from latet.projects import Projects

from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire

_SHAPE = "latet_" + "lk_" + "Ab3_" * 10 + "XYw"
_ESCAPED = "%6C" + _SHAPE[1:]
_CASES = {"short-prefix": "latet_lk_notes", "literal": _SHAPE, "escaped": _ESCAPED}
_ANSWER = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'


def _has_canary(text: str) -> bool:
    return _SHAPE in text or _ESCAPED in text


@pytest.mark.parametrize("kind", ["short-prefix", "literal", "escaped"])
async def test_http_request_shape_precedes_project_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    benign = kind == "short-prefix"
    slot = f"inspect {_CASES[kind]} here"
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": slot}]}).encode()
    assert len(body) <= 16 * 1024 and len(_ANSWER) <= 16 * 1024
    fixture_shape = {
        "expected semantic classification": safe_text(slot) == benign,
        "escaped specimen contains no literal shape": kind != "escaped" or _SHAPE not in slot,
    }
    assert all(fixture_shape.values()), fixture_shape
    original = _request(body)
    project = tmp_path / "off-project"
    project.mkdir()
    scope = dict(original.scope)
    scope["headers"] = [*original.scope["headers"], (b"x-latet-project", str(project).encode())]
    request = Request(scope, original.receive)
    directory = tmp_path / "capture"
    answer = _Answer(_ANSWER)
    sends: list[bool] = []
    off_passes: list[bool] = []
    review_calls: list[bool] = []
    finalize = forward.finalize

    def observe(arena, *args, **kwargs):
        decision = finalize(arena, *args, **kwargs)
        off_passes.append(arena.off and isinstance(decision, Pass))
        return decision

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sends and len(incoming.content) <= 16 * 1024, "one bounded provider attempt"
        sent = json.loads(incoming.content)["messages"][0]["content"]
        sends.append(sent == slot)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    monkeypatch.setattr(forward, "finalize", observe)
    with _sinks(tmp_path) as (mediator, recorder, store):
        config = Config(ner="off", projects=[ProjectEntry(path=str(project), enabled=False)])
        mediator.engine.scopes = Projects(config, mediator.engine.vault, files=False)
        local_items = mediator._review_items
        live_items = mediator._live_items

        def local_probe(*args, **kwargs):
            review_calls.append(True)
            return local_items(*args, **kwargs)

        def live_probe(*args, **kwargs):
            review_calls.append(True)
            return live_items(*args, **kwargs)

        monkeypatch.setattr(mediator, "_review_items", local_probe)
        monkeypatch.setattr(mediator, "_live_items", live_probe)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            cached_source, cached_reading = escapes._last
            cache_clean = not _has_canary(cached_source) and (
                cached_reading is None or not _has_canary(cached_reading.text)
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
    history = json.dumps((public, details), ensure_ascii=False)
    checks = {
        # D5: Latet off in the project refuses the request, benign or not; a protected value is refused first.
        "no Pass in an off project": not any(off_passes),
        "no provider attempt": sends == [],
        "provider stream closed if attempted": answer.closed == bool(sends),
        "protected value absent from the raw capture": benign or not _has_canary(raw),
        "protected value absent from the history": benign or not _has_canary(history),
        "no actual local or live review input": not review_calls,
        "escaped reading cache does not retain original or reading": cache_clean,
    }
    if benign:
        checks["refused as off, saying how to go on"] = status == 400 and b"Latet is off" in wire
    elif 400 <= status < 500:
        refusal = json.loads(wire)
        error = refusal.get("error", {})
        checks["fixed provider-compatible refusal"] = (
            isinstance(error, dict)
            and error.get("code") == "latet_blocked"
            and response.headers.get("x-should-retry") == "false"
            and not _has_canary(json.dumps(refusal, ensure_ascii=False))
        )
    else:
        checks["fixed provider-compatible refusal"] = False
    assert all(checks.values()), checks
