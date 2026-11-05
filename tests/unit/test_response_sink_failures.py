"""Finite sink witnesses for the four source-3 review findings."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import zlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Message

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Outcome, Recorder, RecordSnapshot, ResponseCapture
from latet.gateway.response_finalizer import ResponseFinalizer
from latet.gateway.routes import RouteTable
from latet.storage.exchanges import ExchangeStore

from ..conftest import ROOT_KEY
from .test_request_leaks import _Answer, _renewers, _request
from .test_request_outcomes import OPENAI
from .test_response_sinks import _END, _delta, _payload, _records

_TIMEOUT = 5
_REQUEST = b'{"model":"m","stream":true,"messages":[{"role":"user","content":"Please reply briefly."}]}'
_TEXT = "ordinary response"
_RESPONSE = _delta(_TEXT) + _END


@contextlib.contextmanager
def _sinks(tmp_path: Path) -> Iterator[tuple[Mediator, Recorder, ExchangeStore]]:
    built = build(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    recorder = Recorder(store, engine=built.engine, history="full")
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(mediator.bridge_executor.shutdown, wait=True)
        cleanup.callback(mediator.executor.shutdown, wait=True)
        cleanup.callback(mediator.close)
        cleanup.callback(store.close)
        cleanup.callback(recorder.close)
        yield mediator, recorder, store


def _detail(recorder: Recorder, store: ExchangeStore) -> dict[str, Any]:
    assert recorder.drain(_TIMEOUT), "the history job did not drain"
    [summary] = store.list()[0]
    detail = store.detail(summary["id"])
    assert detail is not None
    return detail


@pytest.mark.parametrize("refused", [True, False], ids=["shutdown", "available"])
async def test_response_headers_require_screen_admission(tmp_path, monkeypatch, refused):
    value = "latet_" + "lk_" + "Ab3_" * 10 + "XYw" if refused else "ordinary header"
    headers = [("x-observation", value)]
    capture = ResponseCapture(None, "text/plain")
    capture.metadata(headers)
    text = _TEXT * 4000  # past RESTORED_ON_LOOP: screened on the engine's thread, which a shutdown refuses
    capture.feed(text.encode())

    with _sinks(tmp_path) as (mediator, recorder, store):
        normal_snapshot: list[bool] = []
        record = recorder._record

        def observe(snapshot: RecordSnapshot) -> dict[str, Any]:
            normal_snapshot.append(any(pair == ("x-observation", value) for pair in snapshot.response_headers))
            return record(snapshot)

        monkeypatch.setattr(recorder, "_record", observe)
        if refused:
            mediator.executor.shutdown(wait=True)
        finalizer = ResponseFinalizer(capture, mediator.executor, recorder, None, 200)
        finalizer.seal(Outcome({}, "openai", "/v1/chat/completions", "http", time.time(), status=200), complete=True)
        await asyncio.wait_for(finalizer.complete(), _TIMEOUT)
        detail = _detail(recorder, store)
        response = detail.get("response") or {}
        checks = {
            "exactly one normal snapshot": len(normal_snapshot) == 1,
            "snapshot only retains screened metadata": normal_snapshot == [not refused],
            "history only retains screened metadata": (value in json.dumps(response)) is not refused,
            "only screened payload retained": response.get("text") == ("" if refused else text),
        }
        assert all(checks.values()), checks


async def _wire(response: StreamingResponse, request: Request) -> tuple[int, bytes]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await response(dict(request.scope), request.receive, send)
    [status] = [message["status"] for message in sent if message["type"] == "http.response.start"]
    return status, b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")


@pytest.mark.parametrize(
    ("coding", "truncated"),
    [("gzip", True), ("gzip", False), ("deflate", True), ("deflate", False)],
    ids=["gzip-truncated", "gzip-complete", "deflate-truncated", "deflate-complete"],
)
async def test_encoded_response_requires_complete_trailer(tmp_path, coding, truncated):
    bits = 31 if coding == "gzip" else 15
    complete = zlib.compress(_RESPONSE, wbits=bits)
    wire = complete[:-1] if truncated else complete
    decoded = zlib.decompressobj(bits)
    result = decoded.decompress(wire) + decoded.flush()
    assert len(wire) <= 16 * 1024 and len(result) <= 16 * 1024
    assert result == _RESPONSE and decoded.eof is not truncated, "the fixture must isolate the missing trailer"

    answer = _Answer(wire)
    directory = tmp_path / "capture"
    requests = 0

    def provider(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "content-encoding": coding}, stream=answer
        )

    with _sinks(tmp_path) as (mediator, recorder, store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            assert isinstance(response, StreamingResponse)
            status, sent = await asyncio.wait_for(_wire(response, request), _TIMEOUT)
        detail = _detail(recorder, store)
        raw = _records(directory)
        history = detail.get("response") or {}
        checks = {
            "one closed upstream answer": requests == 1 and answer.closed,
            "client status and encoded bytes preserved": status == 200 and sent == wire,
            "raw only keeps a complete response": _payload(raw) == ("" if truncated else _RESPONSE.decode()),
            "history only keeps a complete response": history.get("text") == ("" if truncated else _TEXT),
        }
        assert all(checks.values()), checks


class _ReadFailure(_Answer):
    def __init__(self, failure: httpx.ReadError) -> None:
        super().__init__(_delta(_TEXT))
        self.failure = failure
        self.closes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body
        raise self.failure

    async def aclose(self) -> None:
        self.closes += 1
        await super().aclose()


async def test_relay_keeps_primary_failure_when_lease_cleanup_fails(tmp_path, monkeypatch):
    transport_error = httpx.ReadError("test-transport-failure")
    cleanup_error = RuntimeError("test-cleanup-failure")
    answer = _ReadFailure(transport_error)
    directory = tmp_path / "capture"
    moved: list[ExchangeLease] = []
    cleaned: list[bool] = []
    cancelled: list[bool] = []
    transfer, release, cancel = ExchangeLease.transfer, ExchangeLease.release, ExchangeLease.cancel_renewer

    def remember_transfer(lease: ExchangeLease) -> ExchangeLease:
        heir = transfer(lease)
        moved.append(heir)
        return heir

    monkeypatch.setattr(ExchangeLease, "transfer", remember_transfer)

    def provider(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

    with _sinks(tmp_path) as (mediator, recorder, store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            assert isinstance(response, StreamingResponse)
            [owner] = moved
            [renewer] = _renewers()

            def fail_release(lease: ExchangeLease) -> None:
                release(lease)
                if lease is owner:
                    cleaned.append(True)
                    raise cleanup_error

            def observe_cancel(lease: ExchangeLease) -> None:
                cancel(lease)
                if lease is owner:
                    cancelled.append(True)

            monkeypatch.setattr(ExchangeLease, "release", fail_release)
            monkeypatch.setattr(ExchangeLease, "cancel_renewer", observe_cancel)
            raised: Exception | None = None
            try:
                try:
                    await asyncio.wait_for(_wire(response, request), _TIMEOUT)
                except Exception as exc:
                    raised = exc
                await asyncio.sleep(0)
                renewer_ended = renewer.done()
            finally:
                # Fixture fallback is after the observation and does not pass through the spy.
                cancel(owner)
                await asyncio.wait_for(asyncio.gather(renewer, return_exceptions=True), _TIMEOUT)
        detail = _detail(recorder, store)
        raw = _records(directory)
        checks = {
            "transport exception identity preserved": raised is transport_error,
            "lease released once": cleaned == [True] and owner.released,
            "real renewer cancelled and drained by relay": cancelled == [True] and renewer_ended,
            "upstream close and raw finalization happened once": answer.closes == 1
            and sum(row["t"] == "end" for row in raw) == 1,
            "whole incomplete payload omitted in both sinks": not _payload(raw)
            and not (detail.get("response") or {}).get("text"),
        }
        assert all(checks.values()), checks


@pytest.mark.parametrize("oversized", [True, False], ids=["oversized-kind", "ordinary-kind"])
def test_anthropic_response_block_metadata_fits_owner_budget(oversized):
    events: list[dict[str, Any]] = []
    if oversized:
        events.extend(
            [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "x" * 70_000}},
                {"type": "content_block_stop", "index": 0},
            ]
        )
    events.extend(
        [
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": _TEXT}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        ]
    )
    body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    assert len(events) <= 5 and len(body) <= 128 * 1024
    capture = ResponseCapture("anthropic", "text/event-stream")
    capture.feed(body)
    capture.end()
    admitted = capture.screen()
    history = capture.history()
    checks = {
        "only supported bounded metadata admits payload": admitted is not oversized,
        "only a screened turn retains the ordinary text": history.text == (() if oversized else (_TEXT,)),
    }
    assert all(checks.values()), checks
