"""Causal HTTP witnesses for Brotli and zstd encoded completion."""

from __future__ import annotations

import asyncio

import brotli
import httpx
import pytest
import zstandard
from starlette.responses import StreamingResponse

from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.routes import RouteTable

from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _REQUEST, _RESPONSE, _TEXT, _TIMEOUT, _detail, _sinks, _wire
from .test_response_sinks import _payload, _records

_LIMIT = 16_384
_STEP = 65_536


def _brotli_fixture(incomplete: bool) -> tuple[bytes, bytes, bool]:
    compressor = brotli.Compressor(quality=1, lgwin=20)
    prefix = compressor.process(_RESPONSE) + compressor.flush()
    terminal = compressor.finish()
    wire = prefix if incomplete else prefix + terminal
    if not (0 < len(wire) <= _LIMIT and len(_RESPONSE) <= _LIMIT and terminal):
        pytest.fail("Brotli fixture bound", pytrace=False)
    decoder = brotli.Decompressor()
    decoded = decoder.process(wire, output_buffer_limit=_STEP)
    return wire, decoded, decoder.is_finished()


def _zstd_fixture(incomplete: bool) -> tuple[bytes, bytes, bool]:
    complete = zstandard.ZstdCompressor(write_checksum=True, write_content_size=False, threads=0).compress(_RESPONSE)
    wire = complete[:-1] if incomplete else complete
    if not (0 < len(wire) <= _LIMIT and len(_RESPONSE) <= _LIMIT):
        pytest.fail("zstd fixture bound", pytrace=False)
    decoder = zstandard.ZstdDecompressor(max_window_size=8 * 1024 * 1024).decompressobj(
        write_size=_STEP, read_across_frames=False
    )
    decoded = decoder.decompress(wire)
    return wire, decoded, decoder.eof


@pytest.mark.parametrize(
    ("coding", "incomplete"),
    [("br", True), ("br", False), ("zstd", True), ("zstd", False)],
    ids=["br-incomplete", "br-complete", "zstd-incomplete", "zstd-complete"],
)
async def test_encoded_sink_requires_positive_stream_end(tmp_path, coding, incomplete):
    fixture = _brotli_fixture if coding == "br" else _zstd_fixture
    try:
        wire, decoded, ended = fixture(incomplete)
    except (brotli.error, zstandard.ZstdError):
        pytest.fail("native fixture did not produce a readable stream", pytrace=False)
    fixture_checks = {
        "entire terminal SSE already decoded": decoded == _RESPONSE and len(decoded) <= _LIMIT,
        "native end state distinguishes the encoded fixture": ended is not incomplete,
    }
    assert all(fixture_checks.values()), fixture_checks

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
            "raw only keeps a complete response": _payload(raw) == ("" if incomplete else _RESPONSE.decode()),
            "history only keeps a complete response": history.get("text") == ("" if incomplete else _TEXT),
        }
        assert all(checks.values()), checks
