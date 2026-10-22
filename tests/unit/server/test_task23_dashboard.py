"""Task 23B regressions through existing Dashboard routes and its existing body reader.

No prospective executor helper is imported. Thread names are observed inside the blocking work.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Router

from latet.api import rest
from latet.config import Config

from .conftest import EMILY, URL, as_identity


def _dashboard():
    async def work(job):
        return job()

    rt = SimpleNamespace(
        gateway=SimpleNamespace(port=8765, on_server=SimpleNamespace(url=URL, identity=EMILY.id), work=work),
        backends=None,
        identity=EMILY,
        exchanges=SimpleNamespace(),
        config=Config(),
        built=SimpleNamespace(vault=SimpleNamespace()),
    )
    return rest.Dashboard(rt)


def _request(body, declared, receive):
    headers = [(b"content-type", b"application/json")]
    if declared:
        headers.append((b"content-length", str(len(body)).encode()))
    return Request({"type": "http", "method": "POST", "path": f"{rest.API}/playground", "headers": headers}, receive)


async def _loop_checkpoint():
    # All tasks created before this callback get their first turn; no time-based scheduling assumption.
    reached = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(reached.set_result, None)
    await reached


@pytest.mark.parametrize("declared", [True, False], ids=["large-content-length", "unknown-content-length"])
async def test_twenty_four_megabyte_bodies_take_a_turn_before_receive(declared):
    dashboard = _dashboard()
    body = json.dumps({"text": "x" * (4 * 1024 * 1024)}).encode()
    reading, release = [], asyncio.Event()

    def receiver(index):
        async def receive():
            reading.append(index)
            await release.wait()
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    calls = [asyncio.create_task(dashboard._json(_request(body, declared, receiver(index)))) for index in range(20)]
    try:
        await _loop_checkpoint()
        assert reading == [0]
        release.set()
        answers = await asyncio.wait_for(asyncio.gather(*calls), 10)
        assert reading == list(range(20))
        assert all(answer == {"text": "x" * (4 * 1024 * 1024)} for answer in answers)
    finally:
        release.set()
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.parametrize("failure", ["cancel", "receive-error", "invalid-json", "too-large"])
async def test_a_large_body_turn_is_released_after_each_unsuccessful_read(failure):
    dashboard = _dashboard()
    entered, release = asyncio.Event(), asyncio.Event()
    body = b"{" if failure == "invalid-json" else b"x" * (rest._MAX_BODY + 1) if failure == "too-large" else b"{}"

    async def first_receive():
        entered.set()
        await release.wait()
        if failure == "receive-error":
            raise ConnectionError("owned receive failure")
        return {"type": "http.request", "body": body, "more_body": False}

    first = asyncio.create_task(dashboard._json(_request(body, False, first_receive)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if failure == "cancel":
            first.cancel()
        release.set()
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await first
        elif failure == "receive-error":
            with pytest.raises(ConnectionError, match="owned receive failure"):
                await first
        else:
            assert await asyncio.wait_for(first, 2) is None

        async def following_receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        assert await asyncio.wait_for(dashboard._json(_request(b"{}", False, following_receive)), 2) == {}
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_small_declared_bodies_keep_the_existing_loop_parse(monkeypatch):
    dashboard, threads = _dashboard(), []
    parse = rest._parsed

    def noted(body):
        threads.append(threading.current_thread())
        return parse(body)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    monkeypatch.setattr(rest, "_parsed", noted)
    assert await dashboard._json(_request(b"{}", True, receive)) == {}
    assert threads == [threading.current_thread()]


@pytest.mark.parametrize(
    ("route", "target", "attribute", "result", "expected"),
    [
        ("/stats", "dashboard", "_stats", JSONResponse({"witness": "stats"}), "stats"),
        ("/vault", "dashboard", "_vault_page", {"witness": "vault"}, "vault"),
        ("/exchanges", "store", "list", ([{"witness": "exchanges"}], None), "exchanges"),
        ("/reviews", "store", "reviews", [{"witness": "reviews"}], "reviews"),
        ("/sessions", "store", "sessions", [{"witness": "sessions"}], "sessions"),
        ("/images/owned", "store", "image", ("image/png", b"owned-image"), "owned-image"),
        ("/config", "dashboard", "_config_answer", JSONResponse({"witness": "config"}), "config"),
        ("/integrations", "dashboard", "_server_integrations", {"witness": "integrations"}, "integrations"),
        ("/exchanges/owned/columns?slot=0", "dashboard", "_output_columns", ([], {"witness": "columns"}), "columns"),
    ],
)
async def test_existing_read_routes_run_their_work_in_dashboard_threads(
    monkeypatch, route, target, attribute, result, expected
):
    dashboard, threads = _dashboard(), []

    def observed(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return result

    monkeypatch.setattr(
        dashboard if target == "dashboard" else dashboard.rt.exchanges, attribute, observed, raising=False
    )
    app = as_identity(Router(routes=dashboard.routes()), EMILY.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False) as client:
        answer = await client.get(f"{rest.API}{route}")
    assert answer.status_code == 200 and expected in answer.text
    assert len(threads) == 1 and all(name.startswith("latet-dashboard") for name in threads), threads


@pytest.mark.parametrize(
    "route", ["/stats", "/vault", "/sessions", "/reviews", "/exchanges", "/config", "/integrations"]
)
async def test_existing_server_reads_do_not_call_the_default_executor(monkeypatch, route):
    dashboard = _dashboard()
    dashboard.rt.exchanges = SimpleNamespace(list=lambda *a: ([], None), reviews=lambda *a: [], sessions=lambda *a: [])
    monkeypatch.setattr(dashboard, "_stats", lambda *a: JSONResponse({"ok": True}))
    monkeypatch.setattr(dashboard, "_vault_page", lambda *a: {"items": []})
    monkeypatch.setattr(dashboard, "_config_answer", lambda: JSONResponse({"ok": True}))
    monkeypatch.setattr(dashboard, "_server_integrations", lambda: {"ok": True})

    async def forbidden(*args, **kwargs):
        pytest.fail("server dashboard submitted work to asyncio.to_thread")

    monkeypatch.setattr(rest.asyncio, "to_thread", forbidden)
    app = as_identity(Router(routes=dashboard.routes()), EMILY.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False) as client:
        answer = await client.get(f"{rest.API}{route}")
    assert answer.status_code == 200


@pytest.mark.parametrize(
    ("method", "route", "body"),
    [
        ("DELETE", "/vault/owned", None),
        ("DELETE", "/exchanges", None),
        ("POST", "/changes/owned", {"action": "approve"}),
    ],
)
async def test_forget_clear_and_decide_use_the_existing_saved_turn(monkeypatch, method, route, body):
    dashboard, work_calls, side_effects = _dashboard(), [], []
    dashboard.rt.forget = lambda identifier: side_effects.append(("forget", identifier)) or object()
    dashboard.rt.decide_change = lambda *args: side_effects.append(("decide", args)) or "approved"
    dashboard.rt.exchanges.clear = lambda: side_effects.append(("clear",))

    async def work(job):
        assert dashboard._turn("save").locked()
        work_calls.append(True)
        return job()

    async def forbidden(*args, **kwargs):
        pytest.fail("server mutation submitted work to asyncio.to_thread")

    dashboard.rt.gateway.work = work
    monkeypatch.setattr(rest.asyncio, "to_thread", forbidden)
    app = as_identity(Router(routes=dashboard.routes()), EMILY.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False) as client:
        answer = await client.request(method, f"{rest.API}{route}", json=body, headers={"x-latet-ui": "1"})
    assert answer.status_code == 200 and answer.json()["ok"] is True
    assert work_calls == [True] and len(side_effects) == 1


async def test_computer_stats_retain_to_thread(monkeypatch):
    dashboard, calls = _dashboard(), []
    dashboard.rt.gateway.work = None
    dashboard.rt.gateway.on_server = None
    monkeypatch.setattr(dashboard, "_stats", lambda since: JSONResponse({"ok": True}))

    async def to_thread(job, *args, **kwargs):
        calls.append(threading.current_thread())
        return job(*args, **kwargs)

    monkeypatch.setattr(rest.asyncio, "to_thread", to_thread)
    request = Request(
        {"type": "http", "method": "GET", "path": f"{rest.API}/stats", "query_string": b"", "headers": []}
    )
    assert (await dashboard.stats(request)).status_code == 200
    assert calls == [threading.current_thread()]
