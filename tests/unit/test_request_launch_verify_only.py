"""HTTP verify-only launch-shape refusal ahead of model-written evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.engine import escapes
from latet.engine.response_guard import safe_text
from latet.gateway import forward
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import VERIFY
from latet.gateway.outcome import Forward, Pass
from latet.gateway.routes import RouteTable, Upstream

from .test_request_launch_confidentiality import _SHAPE, _has_canary
from .test_request_leaks import _Answer, _request
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire

_PATH = "/company/v1/messages"
_SESSION = "7e57a000-0000-4000-8000-000000000002"  # a Claude Code session id: a UUID
_ROUTE = Upstream(
    "company",
    "/company",
    "http://127.0.0.1:1",
    envelope_expected=frozenset({("model", "m")}),
)
_ANSWER = b'{"content":[{"type":"text","text":"ordinary provider reply"}],"stop_reason":"end_turn"}'


@pytest.mark.parametrize("protected", [False, True], ids=["verify-ordinary", "verify-protected"])
async def test_http_request_shape_precedes_written_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    spelling = _SHAPE if protected else "latet_lk_notes"
    slot = f"write to emily.carter@acme.com and inspect {spelling} here"
    body = json.dumps(
        {
            "model": "m",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "Please reply."}, {"role": "assistant", "content": slot}],
        }
    ).encode()
    assert len(body) <= 16 * 1024 and len(_ANSWER) <= 16 * 1024
    assert safe_text(slot) is not protected, "fixture classification"

    original = _request(body)
    scope = dict(original.scope)
    scope.update(path=_PATH, raw_path=_PATH.encode())
    scope["headers"] = [
        *original.scope["headers"],
        (b"x-claude-code-session-id", _SESSION.encode()),
        (b"anthropic-version", b"2023-06-01"),
    ]
    request = Request(scope, original.receive)
    directory = tmp_path / "capture"
    answer = _Answer(_ANSWER)
    sends: list[bool] = []
    decisions: list[bool] = []
    lookups: list[bool] = []
    finalize = forward.finalize

    def observe(arena, *args, **kwargs):
        decision = finalize(arena, *args, **kwargs)
        decisions.append(arena.mode == VERIFY and not arena.off and isinstance(decision, (Forward, Pass)))
        return decision

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sends and len(incoming.content) <= 16 * 1024, "one bounded provider attempt"
        sent = json.loads(incoming.content)["messages"][1]["content"]
        sends.append(sent == slot)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    monkeypatch.setattr(forward, "finalize", observe)
    with _sinks(tmp_path) as (mediator, recorder, store):
        verifier = mediator.verifier
        key = f"cc:{_SESSION}"
        verifier.wrote(key, (slot,))
        written_source = verifier.written_source
        assert written_source(key, slot) == "observed", "real digest setup"

        def written_probe(session: str, text: str) -> str | None:
            source = written_source(session, text)
            if session == key and text == slot:
                lookups.append(source == "observed")
            return source

        monkeypatch.setattr(verifier, "written_source", written_probe)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([_ROUTE]), Capture(directory), client, mediator, recorder)
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
        "actual enabled VERIFY sends only ordinary input": decisions == [True] if not protected else not any(decisions),
        "real observed-written callback only for ordinary": lookups == ([True] if not protected else []),
        "provider attempt count": len(sends) == int(not protected),
        "provider exact input if attempted": sends == ([True] if not protected else []),
        "provider stream closed if attempted": answer.closed == bool(sends),
        "ordinary raw preserved or protected value absent": slot in raw if not protected else not _has_canary(raw),
        "ordinary history preserved or protected value absent": (
            slot in history if not protected else not _has_canary(history)
        ),
        "escaped reading cache does not retain original or reading": cache_clean,
    }
    if protected:
        refusal = json.loads(wire) if 400 <= status < 500 else {}
        error = refusal.get("error", {})
        checks["fixed provider-compatible refusal"] = (
            refusal.get("type") == "error"
            and isinstance(error, dict)
            and error.get("type") == "invalid_request_error"
            and response.headers.get("x-should-retry") == "false"
            and not _has_canary(json.dumps(refusal, ensure_ascii=False))
        )
    else:
        checks["ordinary exact response"] = status == 200 and wire == _ANSWER
    assert all(checks.values()), checks
