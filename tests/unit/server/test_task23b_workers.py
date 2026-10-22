"""Deferred worker witnesses through existing stats and JSON consumers, without timing claims."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from latet.api import rest

from .test_task23_connections import _identity
from .test_task23_dashboard import _dashboard, _loop_checkpoint, _request

BOUND = 3


@pytest.fixture
def process_pool(monkeypatch):
    made = []

    def factory(*args, **kwargs):
        pool = ThreadPoolExecutor(*args, **kwargs)
        made.append((pool, args, kwargs))
        return pool

    monkeypatch.setattr(rest, "_DASHBOARD_POOL", None)
    monkeypatch.setattr(rest, "ThreadPoolExecutor", factory)
    yield made
    for pool, _, _ in made:
        pool.shutdown(wait=True, cancel_futures=True)


def _stats_request():
    return Request({"type": "http", "method": "GET", "path": f"{rest.API}/stats", "query_string": b"", "headers": []})


async def test_one_lazy_process_pool_starts_two_real_workers(process_pool, monkeypatch):
    dashboards = [_dashboard() for _ in range(3)]
    for index, dashboard in enumerate(dashboards):
        dashboard.rt.identity = _identity(str(index))
    assert process_pool == []
    loop, entered, release = asyncio.get_running_loop(), asyncio.Queue(), threading.Event()
    threads = []

    def stats(_since):
        thread = threading.current_thread()
        threads.append(thread)
        loop.call_soon_threadsafe(entered.put_nowait, thread)
        assert release.wait(BOUND), "owned worker was not released"
        return JSONResponse({"ok": True})

    for dashboard in dashboards:
        monkeypatch.setattr(dashboard, "_stats", stats)
    calls = [asyncio.create_task(d.stats(_stats_request())) for d in dashboards]
    try:
        first = await asyncio.wait_for(entered.get(), BOUND)
        second = await asyncio.wait_for(entered.get(), BOUND)
        assert first is not second
        assert all(t.name.startswith("latet-dashboard") for t in (first, second))
        assert len(process_pool) == 1
        _, args, kwargs = process_pool[0]
        assert args == (2,) and kwargs == {"thread_name_prefix": "latet-dashboard"}
        await _loop_checkpoint()
        assert entered.empty()
        release.set()
        assert all(answer.status_code == 200 for answer in await asyncio.wait_for(asyncio.gather(*calls), BOUND))
        assert len(set(threads)) == 2
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)


async def test_stats_and_body_wait_on_the_loop_while_another_identity_uses_the_other_worker(process_pool, monkeypatch):
    first, second = _dashboard(), _dashboard()
    second.rt.identity = _identity("two")
    loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
    parsed, names, ownership = [], [], []
    stats_turn, body_turn, pool_turn = (first._turn(what) for what in ("stats", "body", "pool"))
    parse = rest._parsed

    def stats(_since):
        names.append(threading.current_thread().name)
        ownership.append((stats_turn.locked(), pool_turn.locked()))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(BOUND), "owned stats worker was not released"
        return JSONResponse({"owner": "one"})

    def colleague(_since):
        names.append(threading.current_thread().name)
        return JSONResponse({"owner": "two"})

    def parsing(body):
        parsed.append((body_turn.locked(), pool_turn.locked()))
        return parse(body)

    monkeypatch.setattr(first, "_stats", stats)
    monkeypatch.setattr(second, "_stats", colleague)
    monkeypatch.setattr(rest, "_parsed", parsing)
    body = b'{"text":"' + b"x" * 8192 + b'"}'
    reads = []

    async def receive():
        reads.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    stats_call = asyncio.create_task(first.stats(_stats_request()))
    body_call = None
    try:
        await asyncio.wait_for(entered.wait(), BOUND)
        body_call = asyncio.create_task(first._json(_request(body, True, receive)))
        await _loop_checkpoint()
        assert reads == [True] and parsed == [] and body_turn.locked()
        answer = await asyncio.wait_for(second.stats(_stats_request()), BOUND)
        assert answer.body == b'{"owner":"two"}' and not release.is_set()
        assert len(set(names)) == 2 and ownership == [(True, True)]
        body_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await body_call
        assert not body_turn.locked() and pool_turn.locked() and parsed == []
        body_call = asyncio.create_task(first._json(_request(body, True, receive)))
        await _loop_checkpoint()
        assert reads == [True, True] and parsed == [] and body_turn.locked()
        release.set()
        assert (await asyncio.wait_for(stats_call, BOUND)).status_code == 200
        assert await asyncio.wait_for(body_call, BOUND) == {"text": "x" * 8192}
        assert parsed == [(True, True)]
        assert not any(turn.locked() for turn in (stats_turn, body_turn, pool_turn))
    finally:
        release.set()
        await asyncio.gather(*(call for call in (stats_call, body_call) if call), return_exceptions=True)


async def test_cancelling_a_submitted_parse_keeps_the_large_body_turn_until_its_worker_ends(process_pool, monkeypatch):
    dashboard = _dashboard()
    loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
    parse, reads = rest._parsed, []
    body = b'{"text":"' + b"x" * 8192 + b'"}'

    def parsing(raw):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(BOUND), "owned parser was not released"
        return parse(raw)

    def receiver(index):
        async def receive():
            reads.append(index)
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    monkeypatch.setattr(rest, "_parsed", parsing)
    first = asyncio.create_task(dashboard._json(_request(body, True, receiver(0))))
    following = None
    try:
        await asyncio.wait_for(entered.wait(), BOUND)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert dashboard._turn("pool").locked()
        following = asyncio.create_task(dashboard._json(_request(body, True, receiver(1))))
        await _loop_checkpoint()
        assert reads == [0], "a following body was retained while the cancelled request's parse still ran"
        release.set()
        assert await asyncio.wait_for(following, BOUND) == {"text": "x" * 8192}
        assert reads == [0, 1]
        assert not dashboard._turn("pool").locked() and not dashboard._turn("body").locked()
    finally:
        release.set()
        await asyncio.gather(*(call for call in (first, following) if call), return_exceptions=True)
