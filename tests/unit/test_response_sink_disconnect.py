"""Real Starlette disconnects before a streamed response starts its relay."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import BinaryIO

import httpx
import pytest
from starlette.responses import StreamingResponse
from starlette.types import Message

from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.lease import ExchangeLease
from latet.gateway.routes import RouteTable

from .test_request_leaks import _renewers, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _REQUEST, _RESPONSE, _TIMEOUT, _detail, _sinks
from .test_response_sinks import _payload, _records


class _OwnedAnswer(httpx.AsyncByteStream):
    def __init__(self, resource: BinaryIO, failure: RuntimeError | None) -> None:
        self.resource = resource
        self.failure = failure
        self.iterations = 0
        self.closes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterations += 1
        yield self.resource.read()

    async def aclose(self) -> None:
        self.closes += 1
        self.resource.close()
        if self.failure is not None:
            raise self.failure


@pytest.mark.parametrize("faulty_close", [True, False], ids=["close-fault", "ordinary-close"])
async def test_unstarted_stream_keeps_normal_starlette_disconnect(tmp_path, monkeypatch, faulty_close):
    directory = tmp_path / "capture"
    response_file = tmp_path / "provider-answer"
    response_file.write_bytes(_RESPONSE)
    close_fault = RuntimeError("test-upstream-close") if faulty_close else None
    moved: list[ExchangeLease] = []
    released: list[bool] = []
    cancelled: list[bool] = []
    transfer, release, cancel = ExchangeLease.transfer, ExchangeLease.release, ExchangeLease.cancel_renewer

    def remember_transfer(lease: ExchangeLease) -> ExchangeLease:
        heir = transfer(lease)
        moved.append(heir)
        return heir

    monkeypatch.setattr(ExchangeLease, "transfer", remember_transfer)
    with response_file.open("rb") as resource, _sinks(tmp_path) as (mediator, recorder, store):
        answer = _OwnedAnswer(resource, close_fault)
        requests = 0

        def provider(_: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            request = _request(_REQUEST)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            assert isinstance(response, StreamingResponse)
            [owner] = moved
            [renewer] = _renewers()

            def observe_release(lease: ExchangeLease) -> None:
                release(lease)
                if lease is owner:
                    released.append(True)

            def observe_cancel(lease: ExchangeLease) -> None:
                cancel(lease)
                if lease is owner:
                    cancelled.append(True)

            monkeypatch.setattr(ExchangeLease, "release", observe_release)
            monkeypatch.setattr(ExchangeLease, "cancel_renewer", observe_cancel)
            start_entered = asyncio.Event()
            block_start = asyncio.Event()
            starts: list[int] = []
            body_sends = disconnects = 0

            async def send(message: Message) -> None:
                nonlocal body_sends
                if message["type"] == "http.response.start":
                    starts.append(message["status"])
                    start_entered.set()
                    await asyncio.wait_for(block_start.wait(), _TIMEOUT)
                else:
                    body_sends += 1

            async def receive() -> Message:
                nonlocal disconnects
                await asyncio.wait_for(start_entered.wait(), _TIMEOUT)
                disconnects += 1
                return {"type": "http.disconnect"}

            scope = dict(request.scope)
            scope["asgi"] = {"version": "3.0", "spec_version": "2.0"}
            raised: Exception | None = None
            try:
                try:
                    await asyncio.wait_for(response(scope, receive, send), _TIMEOUT)
                except Exception as exc:
                    raised = exc
                await asyncio.sleep(0)
                lease_released = released == [True] and owner.released
                renewer_drained = cancelled == [True] and renewer.cancelled()
                upstream_drained = answer.closes == 1 and resource.closed
            finally:
                # Fallback starts after the observations and bypasses the probes.
                release(owner)
                cancel(owner)
                await asyncio.wait_for(asyncio.gather(renewer, return_exceptions=True), _TIMEOUT)

        history = _detail(recorder, store).get("response") or {}
        raw = _records(directory)
        checks = {
            "normal Starlette disconnect returned normally": raised is None,
            "only the injected fault could have escaped": raised is None or raised is close_fault,
            "real disconnect arrived while response start was blocked": starts == [200] and disconnects == 1,
            "relay never started": requests == 1 and answer.iterations == 0 and body_sends == 0,
            "upstream closed once and released its resource": upstream_drained,
            "actual lease released once": lease_released,
            "actual renewer cancelled and drained": renewer_drained,
            "one incomplete finalization": sum(row["t"] == "end" for row in raw) == 1
            and [row["reason"] for row in raw if row["t"] == "omitted"] == ["response-incomplete"]
            and history.get("omitted") == "response-incomplete",
            "whole payload absent from both sinks": not _payload(raw) and not history.get("text"),
        }
        assert all(checks.values()), checks
