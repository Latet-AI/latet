"""The waits whose shared work goes on when a waiter goes: a credential's lookup, a space's build, a dashboard's read, a
scrape's render, a response's settlement drained, and one relayed whole and left to end alone. Each work's failure,
once its waiter has gone, is its own readers' to read and nobody's to log: Python 3.14's ``asyncio.shield`` handed it
to the loop's exception handler, logged as an error with its traceback, where 3.12 and 3.13 said nothing
(``latet.waiting.shielded``)."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from latet.api.rest import Dashboard
from latet.gateway.response_finalizer import ResponseFinalizer, drain_task
from latet.server.machines import OneAtATime
from latet.server.metrics import Metrics
from latet.server.spaces import Spaces


class Down(RuntimeError):
    """The work's own failure, after its waiter went."""


@pytest.fixture
async def heard():
    """What the running loop's exception handler hears during the test."""
    loop = asyncio.get_running_loop()
    previous, found = loop.get_exception_handler(), []
    loop.set_exception_handler(lambda _loop, context: found.append(context))
    try:
        yield found
    finally:
        loop.set_exception_handler(previous)


async def _went(waiter: asyncio.Task) -> None:
    """The waiter started waiting, then went."""
    for _ in range(3):
        await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def _settled() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_lookup_whose_request_went_fails_for_its_readers_and_is_logged_by_nobody(heard):
    lookups, release = OneAtATime(), asyncio.Event()

    async def lookup():
        await release.wait()
        raise Down("the stores did not answer")

    await _went(asyncio.create_task(lookups.run("token", b"hashed", 1, lookup)))
    ((_, running),) = lookups._running.values()
    assert not running.done()  # it goes on for the requests that come next
    release.set()
    await _settled()
    assert running.done() and type(running.exception()) is Down and not lookups._running
    assert heard == []


async def test_a_build_whose_request_went_fails_for_its_readers_and_is_logged_by_nobody(heard):
    release = asyncio.Event()

    async def failing(_identity):
        await release.wait()
        raise Down("Valkey was not heard")

    listening = asyncio.Event()
    listening.set()
    spaces = SimpleNamespace(server=SimpleNamespace(listening=listening), _spaces={}, _building={}, _open=failing)
    identity = SimpleNamespace(id="i_emily")
    await _went(asyncio.create_task(Spaces._space(spaces, identity)))  # type: ignore[arg-type]
    building = spaces._building["i_emily"]
    assert not building.done()
    release.set()
    await _settled()
    assert building.done() and type(building.exception()) is Down
    assert heard == []


async def test_a_dashboard_read_whose_request_went_fails_for_its_readers_and_is_logged_by_nobody(heard):
    entered, release = threading.Event(), threading.Event()

    def read():
        entered.set()
        assert release.wait(5)
        raise Down("PostgreSQL did not answer")

    dashboard = SimpleNamespace(rt=SimpleNamespace(gateway=SimpleNamespace(work=object())), _turns={})
    dashboard._turn = lambda what: Dashboard._turn(dashboard, what)  # type: ignore[arg-type]
    waiter = asyncio.create_task(Dashboard._in_pool(dashboard, read))  # type: ignore[arg-type]
    assert await asyncio.to_thread(entered.wait, 5)
    await _went(waiter)
    turn = dashboard._turn("pool")
    assert turn.locked()  # held until the read ends: it goes on
    release.set()
    for _ in range(100):
        if not turn.locked():
            break
        await asyncio.sleep(0.01)
    await _settled()
    assert not turn.locked() and heard == []


async def test_a_render_whose_scrape_went_fails_for_its_readers_and_is_logged_by_nobody(heard):
    metrics, release = Metrics(), asyncio.Event()

    async def rendered():
        await release.wait()
        raise Down("the metrics are closed")

    metrics._rendered = rendered  # type: ignore[method-assign]
    await _went(asyncio.create_task(metrics.scrape()))
    running = metrics._render
    assert running is not None and not running.done()
    release.set()
    await _settled()
    assert running.done() and type(running.exception()) is Down
    assert heard == []


async def test_a_drained_settlement_whose_waiter_was_cancelled_fails_to_it_alone(heard):
    release, stops = asyncio.Event(), []

    async def settling():
        await release.wait()
        raise Down("the screening failed")

    task = asyncio.create_task(settling())
    waiter = asyncio.create_task(drain_task(task, lambda: stops.append(True)))
    await _settled()
    waiter.cancel()  # drained anyway: the waiter waits on for its settlement
    await _settled()
    assert stops == [True] and not waiter.done() and not task.done()
    release.set()
    with pytest.raises(Down):
        await waiter
    await _settled()
    assert heard == []


async def test_a_settlement_left_to_end_alone_fails_for_its_readers_and_is_logged_by_nobody(heard):
    release = asyncio.Event()

    async def settling():
        await release.wait()
        raise Down("the screening failed")

    task = asyncio.create_task(settling())
    task.add_done_callback(lambda done: done.exception())  # as _alone_ended reads it
    finalizer = SimpleNamespace(_started=lambda: task, _relayed=True, leave=lambda: True)
    waiter = asyncio.create_task(ResponseFinalizer.complete(finalizer))  # type: ignore[arg-type]
    await _went(waiter)
    assert not task.done()  # ending alone
    release.set()
    await _settled()
    assert task.done() and type(task.exception()) is Down
    assert heard == []
