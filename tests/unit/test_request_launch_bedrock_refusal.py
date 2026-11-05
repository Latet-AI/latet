"""A configured Bedrock Invoke uses its native request-refusal envelope."""

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
from latet.gateway.body_coverage import PROTECTED_LAUNCH
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import VERIFY
from latet.gateway.routes import RouteTable, Upstream

from .test_request_launch_confidentiality import _SHAPE, _has_canary
from .test_request_leaks import _Answer, _request
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire

_ROUTE = Upstream("company", "/company", "https://company.invalid")
_PATH = "/company/model/approved-model/invoke"
_REFUSAL = PROTECTED_LAUNCH  # a request Latet only checks: nothing masks the key, so it says how to go on


async def test_http_launch_refusal_preserves_configured_bedrock_error_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = f"inspect {_SHAPE} here"
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": slot}],
        }
    ).encode()
    assert len(body) <= 16 * 1024 and not safe_text(slot), "bounded protected semantic slot"
    original = _request(body)
    scope = dict(original.scope)
    scope.update(path=_PATH, raw_path=_PATH.encode())
    scope["headers"] = [
        (b"content-type", b"application/json"),
        (b"authorization", b"AWS4-HMAC-SHA256 native-company-signature"),
        (b"x-amz-date", b"20261102T120000Z"),
    ]
    request = Request(scope, original.receive)
    directory = tmp_path / "capture"
    sends: list[bool] = []
    admission: list[bool] = []

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sends and len(incoming.content) <= 16 * 1024, "one bounded provider attempt"
        sends.append(True)
        answer = _Answer(b'{"content":[{"type":"text","text":"ordinary"}],"stop_reason":"end_turn"}')
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    with _sinks(tmp_path) as (mediator, recorder, store):
        prepare = mediator.prepare_http

        def observe(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            admission.append(
                prepared.mode == VERIFY
                and prepared.protocol == "anthropic"
                and prepared.block_reason == _REFUSAL
                and prepared.body == b""
                and prepared.document is None
                and not prepared.pending
            )
            return prepared

        monkeypatch.setattr(mediator, "prepare_http", observe)
        routes = RouteTable([_ROUTE], (("company", _ROUTE.base_url, "model", "approved-model"),))
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(routes, Capture(directory), client, mediator, recorder)
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
        assert recorder.drain(_TIMEOUT), "one bounded history drain"
        public = store.list()[0]
        assert len(public) <= 1, "at most one history entry"
        details = [store.detail(row["id"]) for row in public]

    raw_paths = list(directory.glob("*.jsonl"))
    assert len(raw_paths) <= 1, "at most one capture file"
    raw = "".join(path.read_text("utf-8") for path in raw_paths)
    refusal = json.loads(wire) if 400 <= status < 500 else {}
    error = refusal.get("error", {})
    message = error.get("message") if isinstance(error, dict) else None
    checks = {
        "actual selected credential guard refused in preparation": admission == [True],
        "categorical client refusal": (
            refusal.get("type") == "error"
            and isinstance(error, dict)
            and error.get("type") == "invalid_request_error"
            and isinstance(message, str)
            and _REFUSAL in message
            and response.headers.get("x-should-retry") == "false"
        ),
        "native Bedrock top-level message": isinstance(message, str) and refusal.get("message") == message,
        "provider not contacted": not sends,
        "original absent from client and raw": not _has_canary(wire.decode()) and not _has_canary(raw),
        "original absent from public and sensitive history": not _has_canary(json.dumps((public, details))),
        "escaped cache free of original or reading": cache_clean,
    }
    assert all(checks.values()), checks
