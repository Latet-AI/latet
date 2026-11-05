"""Bounded residual response sink and response-derived provenance qualification."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.responses import Response

from latet.config import Config
from latet.engine.build import build
from latet.engine.response_guard import safe_text
from latet.engine.vault import Vault
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.recorder import ResponseCapture
from latet.gateway.routes import RouteTable
from latet.gateway.written import STEP, Written

from ..conftest import ROOT_KEY
from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_history_choices import _event
from .test_response_sink_failures import _TIMEOUT, _detail, _sinks
from .test_response_sinks import _payload, _records

_SHAPE = "latet_" + "lk_" + "Ab3_" * 10 + "XYw"
_ESCAPED = "%6C" + _SHAPE[1:]


@pytest.mark.parametrize("count", [64, 65], ids=["lane-64", "lane-65"])
def test_capture_lane_descriptor_edge(count: int) -> None:
    events = [_event([{"index": index, "delta": {"content": "x"}, "finish_reason": "stop"}]) for index in range(count)]
    events.append(b"data: [DONE]\n\n")
    assert len(events) <= 66 and sum(map(len, events)) <= 16 * 1024
    capture = ResponseCapture("chat", "text/event-stream")
    for event in events:
        capture.feed(event)
    capture.end()
    accepted = capture.screen()
    checks = {
        "screen category": accepted == (count == 64),
        "omission category": capture.omission == (None if count == 64 else "response-budget"),
        "retained text at edge": capture.text == (["x"] * 64 if count == 64 else []),
        "descriptor ceiling distinct from metadata exhaustion": capture._metadata < 65_536,
    }
    assert all(checks.values()), checks


@pytest.mark.parametrize("length", [15_563, 15_564], ids=["metadata-exact", "metadata-next-charge"])
def test_capture_metadata_descriptor_edge(length: int, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = ResponseCapture(None, "text/plain")
    charge = ResponseCapture._charge
    refused: list[tuple[int, int]] = []

    def observe(owner: ResponseCapture, amount: int) -> bool:
        before = owner._metadata
        admitted = charge(owner, amount)
        if not admitted:
            refused.append((before + amount, len(owner._rendered)))
        return admitted

    monkeypatch.setattr(ResponseCapture, "_charge", observe)
    capture.metadata([("x-bound", "a" * length)])
    capture.feed(b"x")
    capture.end()
    accepted = capture.screen()
    exact = length == 15_563
    checks = {
        "screen category": accepted == exact,
        "metadata ledger": capture._metadata == (65_536 if exact else 65_284),
        "next charge refused before piece retention": refused == ([] if exact else [(65_540, 0)]),
        "omission category": capture.omission == (None if exact else "response-budget"),
        "retained text at edge": capture.text == (["x"] if exact else []),
    }
    assert all(checks.values()), checks


@pytest.mark.parametrize("kind", ["masked-only", "restored-only", "ordinary"])
def test_model_output_cache_screens_both_arguments(kind: str) -> None:
    engine = build(Config(ner="off"), Vault(ROOT_KEY)).engine
    masked = _ESCAPED if kind == "masked-only" else "ordinary masked output"
    restored = _ESCAPED if kind == "restored-only" else f"ordinary restored {kind}"
    key = engine._key("R", restored)
    checks = {
        "expected argument alone is unsafe": (
            safe_text(masked) == (kind != "masked-only") and safe_text(restored) == (kind != "restored-only")
        ),
        "initial real cache miss": engine.conversations.recall(key) is None,
    }
    engine.remember_model_output(masked, restored)
    checks["real cache admission"] = engine.conversations.recall(key) == (masked if kind == "ordinary" else None)
    assert all(checks.values()), checks


@pytest.mark.parametrize("large", [False, True], ids=["written-inline", "written-worker"])
async def test_written_provenance_screens_escaped_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, large: bool
) -> None:
    ordinary = "ordinary provider text"
    body = json.dumps(
        {
            "content": [{"type": "text", "text": _ESCAPED}, {"type": "text", "text": ordinary}],
            "padding": " " * (66 * 1024 if large else 0),
        },
        separators=(",", ":"),
    ).encode()
    if large:
        assert 65_536 < len(body) <= 72 * 1024
    else:
        assert len(body) <= 4 * 1024
    with _sinks(tmp_path) as (mediator, _, _):
        session = mediator.engine.session("response-written-bound")
        verifier = mediator.verifier
        written = Written("application/json", None)
        submit = mediator.executor.submit
        jobs: list[bool] = []

        def observe(function, *args, **kwargs):
            jobs.append(function == mediator._write_down)
            return submit(function, *args, **kwargs)

        async def deliver() -> None:
            for offset in range(0, len(body), STEP):
                await mediator.read_written(written, session, body[offset : offset + STEP])
            await mediator.read_written(written, session, None)

        monkeypatch.setattr(mediator.executor, "submit", observe)
        await asyncio.wait_for(deliver(), _TIMEOUT)
        checks = {
            "escaped only": _SHAPE not in _ESCAPED and not safe_text(_ESCAPED),
            "selected executor path": jobs == ([True] if large else []),
            "real parser completed": not written.broken,
            "unsafe leaf absent from observed provenance": (
                verifier.written_source(session.key, _ESCAPED) is None
                and verifier.written_source(session.key, _SHAPE) is None
            ),
            "ordinary leaf retained as provider provenance": verifier.written_source(session.key, ordinary)
            == "observed",
        }
        assert all(checks.values()), checks


async def test_whole_http_restores_client_and_keeps_provider_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "emily.carter@acme.com"
    prompt = f"write to {original}"
    request = _request(json.dumps({"model": "m", "messages": [{"role": "user", "content": prompt}]}).encode())
    directory = tmp_path / "capture"
    answers: list[_Answer] = []
    provider_texts: list[str] = []
    branches: list[bool] = []
    whole = Forwarder._restored_whole

    async def observe(forwarder: Forwarder, *args, **kwargs) -> Response:
        branches.append(True)
        return await whole(forwarder, *args, **kwargs)

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not answers and len(incoming.content) <= 16 * 1024, "one bounded provider request"
        text = json.loads(incoming.content)["messages"][0]["content"]
        provider_texts.append(text)
        answer = {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]}
        body = json.dumps(answer).encode()
        assert len(body) <= 16 * 1024, "bounded whole response"
        stream = _Answer(body)
        answers.append(stream)
        return httpx.Response(
            201, headers={"content-type": "application/json", "x-response-observation": "ordinary"}, stream=stream
        )

    monkeypatch.setattr(Forwarder, "_restored_whole", observe)
    with _sinks(tmp_path) as (mediator, recorder, store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
        assert type(response) is Response, "whole client response expected"
        detail = _detail(recorder, store)

    assert len(answers) == len(provider_texts) == 1, "one provider exchange expected"
    answer = answers[0]
    masked = provider_texts[0]
    client_text = json.loads(bytes(response.body))["choices"][0]["message"]["content"]
    checks = {
        "actual whole transform and provider close": branches == [True] and answer.closed,
        "actual request masked before provider echo": masked.startswith("write to ") and original not in masked,
        "client restored exact text": client_text == prompt,
        "client status and headers": (
            response.status_code == 201
            and response.headers.get("x-response-observation") == "ordinary"
            and response.headers.get("content-type") == "application/json"
            and response.headers.get("content-length") == str(len(response.body))
        ),
        "raw keeps exact provider bytes": _payload(_records(directory)) == answer.body.decode(),
        "history keeps original provider text": (detail.get("response") or {}).get("text") == masked,
    }
    assert all(checks.values()), checks
