"""ES.4/7 consumer contracts, prepared without claiming runtime or Task 23B evidence."""

import asyncio
import builtins
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from latet.api import rest
from latet.logline import Paced
from latet.server import admin, machines

READERS = [(rest._within, bytearray), (machines._within, bytes)]


@pytest.mark.parametrize("compare", [rest._same_etag, admin._same])
@pytest.mark.parametrize("given", ["tag", "other", "", chr(0xE9), chr(0xD800)])
def test_etags_compare_utf8_bytes_even_for_arbitrary_caller_text(monkeypatch, compare, given):
    original = rest.hmac.compare_digest
    seen = []

    def compared(left, right):
        seen.append((left, right))
        return original(left, right)

    monkeypatch.setattr(rest.hmac, "compare_digest", compared)
    assert compare(given, "tag") is (given == "tag")
    assert seen == [(given.encode("utf-8", "surrogatepass"), b"tag")]


def _request(chunks, length=None, disconnect=False):
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1 or disconnect}
        for i, chunk in enumerate(chunks)
    ]
    if disconnect:
        messages.append({"type": "http.disconnect"})
    read = []

    async def receive():
        read.append(True)
        assert messages, "the reader went beyond the final body message"
        return messages.pop(0)

    headers = [] if length is None else [(b"content-length", length.encode())]
    return Request({"type": "http", "headers": headers}, receive), read


@pytest.mark.parametrize(("reader", "representation"), READERS)
@pytest.mark.parametrize(
    ("chunks", "length", "expected"),
    [
        ([b""], None, b""),
        ([b"1234"], "4", b"1234"),
        ([b"1", b"2", b"3", b"4"], None, b"1234"),
        ([b"12", b"34"], "broken", b"1234"),
        ([b"12", b"34"], "0", b"1234"),
        ([b"12345"], None, None),
        ([b"1", b"2345"], "1", None),
        ([b"1", b"x" * 64], "broken", None),
        ([b""], "5", None),
    ],
)
async def test_bounded_body_consumers_keep_their_representation_and_count_actual_bytes(
    reader, representation, chunks, length, expected
):
    request, read = _request(chunks, length)
    body = await reader(request, 4)
    if expected is None:
        assert body is None
    else:
        assert type(body) is representation and body == expected
    if length == "5":
        assert read == []


@pytest.mark.parametrize(("reader", "representation"), READERS)
async def test_a_body_disconnect_propagates(reader, representation):
    request, _ = _request([b"12"], disconnect=True)
    with pytest.raises(ClientDisconnect):
        await reader(request, 4)


@pytest.mark.parametrize(("reader", "representation"), READERS)
async def test_cancelling_a_body_read_propagates(reader, representation):
    async def receive():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reader(Request({"type": "http", "headers": []}, receive), 4)


async def test_direct_worker_faults_are_paced_type_only_and_answered_without_the_payload(space, monkeypatch, caplog):
    rt, _ = space
    sentinel = "private-route-and-settings-ES7"
    for name, value in list(vars(rest).items()):
        if isinstance(value, Paced):
            monkeypatch.setattr(rest, name, Paced())

    async def broken(job):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(rt.gateway, "work", broken)
    dashboard = cast(rest.Dashboard, rt.gateway.dashboard)
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            answer = await dashboard._in_work(lambda: JSONResponse({"ok": True}))
            assert answer.status_code == 503 and answer.headers["retry-after"] == "1"
            assert sentinel not in bytes(answer.body).decode()
    faults = [r for r in caplog.records if "RuntimeError" in r.getMessage()]
    assert len(faults) == 1
    assert sentinel not in caplog.text and all(r.exc_info is None for r in faults)


async def test_the_normal_worker_logs_a_fault_once_without_claiming_nothing_happened(space, monkeypatch, caplog):
    rt, _ = space
    monkeypatch.setattr(machines, "UNANSWERED", Paced())
    monkeypatch.setattr(rt.gateway, "work", machines.in_work)
    dashboard = cast(rest.Dashboard, rt.gateway.dashboard)

    def broken():
        raise RuntimeError("private-ES7-sentinel")

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            answer = await dashboard._in_work(broken)
            assert answer.status_code == 503
            assert json.loads(bytes(answer.body))["error"]
    faults = [r for r in caplog.records if "RuntimeError" in r.getMessage()]
    assert len(faults) == 1
    assert "private-ES7-sentinel" not in caplog.text
    assert "nothing done" not in caplog.text


async def test_cancelled_work_is_not_relabelled_as_a_store_failure(space, monkeypatch, caplog):
    rt, _ = space

    async def cancelled(job):
        raise asyncio.CancelledError

    monkeypatch.setattr(rt.gateway, "work", cancelled)
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await cast(rest.Dashboard, rt.gateway.dashboard)._in_work(lambda: JSONResponse({"ok": True}))
    assert caplog.records == []


def _body_buffers():
    """Observe actual bytearray retention, not the length of a buffer after a refusal might have cleared it."""
    made, weights = [], []

    class BodyBuffer(bytearray):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
            weights.append(len(self))

        def __iadd__(self, chunk):
            result = super().__iadd__(chunk)
            weights.append(len(self))
            return result

        def extend(self, chunk):
            super().extend(chunk)
            weights.append(len(self))

    return BodyBuffer, made, weights


@pytest.mark.parametrize(("reader", "representation"), READERS)
async def test_body_buffers_refuse_an_oversized_chunk_before_retaining_it(monkeypatch, reader, representation):
    request, read = _request([b"1", b"x" * 64, b"unread"])
    buffer_type, made, weights = _body_buffers()
    # The fixture receive returns immediately: this scoped interception yields to no other event-loop task.
    # It follows the buffer allocation through either consumer and, later, their common helper.
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "bytearray", buffer_type)
        body = await reader(request, 4)
    assert body is None and len(read) == 2
    assert made and max(weights) <= 4
    assert bytes(made[0]) == b"1"


async def test_dashboard_body_returns_its_original_buffer_without_a_full_copy(monkeypatch):
    request, _ = _request([b"12", b"34"])
    buffer_type, made, weights = _body_buffers()
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "bytearray", buffer_type)
        body = await rest._within(request, 4)
    assert len(made) == 1 and body is made[0] and body == b"1234"
    assert max(weights) == 4


def test_neutral_http_helpers_import_and_work_without_server_extras():
    """A fresh interpreter cannot import server extras, even when this pytest process already imported them."""
    source = Path(__file__).resolve().parents[3] / "src"
    code = """
import asyncio
import sys
sys.path.insert(0, sys.argv[1])
for name in ('psycopg', 'psycopg_pool', 'redis'):
    sys.modules[name] = None
from latet.http_inputs import bounded_body, same_etag
from starlette.requests import Request
assert same_etag('tag', 'tag')
assert not same_etag(chr(0xD800), 'tag')
assert not any(name.startswith('latet.server') for name in sys.modules)
async def receive():
    return {'type': 'http.request', 'body': b'abcd', 'more_body': False}
async def check():
    body = await bounded_body(Request({'type': 'http', 'headers': []}, receive), 4)
    assert type(body) is bytearray and body == b'abcd'
asyncio.run(check())
"""
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(source)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert done.returncode == 0, done.stderr
