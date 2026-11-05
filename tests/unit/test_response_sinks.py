"""R144 response sinks: real HTTP streaming, private raw capture and FULL history.

These two finite witnesses use no provider listener, registry or model. The unsafe
case joins a synthetic launch credential only within one Chat text lane; the
positive control keeps the same supported protocol and authorized sinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import StreamingResponse
from starlette.types import Message

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Recorder
from latet.gateway.routes import RouteTable
from latet.storage.exchanges import ExchangeStore

from ..conftest import ROOT_KEY
from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI

_PREFIX = "ordinary response prefix "
_END = (
    b'data: {"id":"chatcmpl-sink","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    b"data: [DONE]\n\n"
)
_TOKENS = {"input": 7, "output": 3, "cache_read": 0, "cache_write": 0}


def _delta(text: str) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-sink",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            },
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode()


def _chunk(parts: tuple[str, ...]) -> bytes:
    data = b"".join(_delta(part) for part in parts)
    # Forwarder may request 64 KiB HTTPX chunks for opaque observation. Reach that
    # boundary before pausing, so the real relay has consumed every text delta.
    assert len(data) < 64 * 1024 - 4
    return data + b": " + b" " * (64 * 1024 - len(data) - 4) + b"\n\n"


class _PausedAnswer(_Answer):
    """All deltas consumed, but neither the protocol terminal nor EOF delivered."""

    def __init__(self, chunk: bytes) -> None:
        super().__init__(chunk)
        self.before_end = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body
        self.before_end.set()
        await asyncio.wait_for(self.release.wait(), 10)
        yield _END


def _records(directory: Path) -> list[dict[str, Any]]:
    [path] = list(directory.glob("*.jsonl"))
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _payload(records: list[dict[str, Any]]) -> str:
    return "".join(item["data"] for item in records if item["t"] in ("chunk", "body"))


async def _exchange(tmp_path: Path, parts: tuple[str, ...]) -> tuple[list[dict[str, Any]], dict[str, Any], bool, bool]:
    """One real Forwarder exchange, fully drained before returning sink observations."""
    built = build(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    recorder = Recorder(store, engine=built.engine, history="full")
    directory = tmp_path / "capture"
    answer = _PausedAnswer(_chunk(parts))
    requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=answer)

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
    task: asyncio.Task[None] | None = None
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    body = b'{"model":"m","stream":true,"messages":[{"role":"user","content":"Please reply briefly."}]}'
    request = _request(body)
    try:
        response = await asyncio.wait_for(forwarder.handle(request), 10)
        assert isinstance(response, StreamingResponse)
        task = asyncio.create_task(response(dict(request.scope), request.receive, send))
        await asyncio.wait_for(answer.before_end.wait(), 10)
        early_payload = bool(_payload(_records(directory)))
        answer.release.set()
        await asyncio.wait_for(asyncio.shield(task), 10)
        assert recorder.drain(10), "the one history job must finish"
        assert requests == 1 and answer.closed
        [summary] = store.list()[0]
        detail = store.detail(summary["id"])
        assert detail is not None
        starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
        wire = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        wire_unchanged = starts == [200] and wire == answer.body + _END
        return _records(directory), detail, early_payload, wire_unchanged
    finally:
        answer.release.set()
        with contextlib.ExitStack() as cleanup:
            # close() does not wait; both owned executors must really finish.
            cleanup.callback(mediator.bridge_executor.shutdown, wait=True)
            cleanup.callback(mediator.executor.shutdown, wait=True)
            cleanup.callback(mediator.close)
            cleanup.callback(store.close)
            cleanup.callback(recorder.close)
            try:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            finally:
                await client.aclose()


async def test_http_stream_omits_an_unsafe_turn_from_raw_and_history(tmp_path):
    """Early raw writes or history-only filtering leave this whole-turn omission witness red."""
    # Canonical base64url for synthetic 32 bytes, never issued; split both the prefix and body.
    parts = (_PREFIX, "latet_", "lk_" + "Ab3_" * 5, "Ab3_" * 5 + "XYw")
    raw, detail, early_payload, wire_unchanged = await _exchange(tmp_path, parts)
    response = detail.get("response") or {}
    serialized_sinks = json.dumps((raw, detail), ensure_ascii=False)
    checks = {
        "wire status and bytes preserved": wire_unchanged,
        "no raw payload before completion": not early_payload,
        "whole raw turn omitted": not _payload(raw),
        "whole history turn omitted": not response.get("text") and not response.get("tool_calls"),
        "no response fragments retained in sink metadata": not any(part in serialized_sinks for part in parts),
        "screened usage retained": detail["summary"]["tokens"] == _TOKENS,
    }
    assert all(checks.values()), checks  # only categorical booleans, never a clear canary in a failure


async def test_http_stream_keeps_a_complete_safe_turn_in_raw_and_history(tmp_path):
    """A sink fix that drops every response fails this positive fidelity control."""
    parts = (_PREFIX, "complete", ".")
    raw, detail, _early_payload, wire_unchanged = await _exchange(tmp_path, parts)
    response = detail.get("response") or {}
    checks = {
        "wire status and bytes preserved": wire_unchanged,
        "raw content and order preserved": _payload(raw) == (_chunk(parts) + _END).decode(),
        "history text preserved": response.get("text") == "ordinary response prefix complete.",
        "history tools preserved": response.get("tool_calls") == [],
        "screened usage retained": detail["summary"]["tokens"] == _TOKENS,
    }
    assert all(checks.values()), checks
