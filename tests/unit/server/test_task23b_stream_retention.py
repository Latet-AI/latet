"""A stalled real approval feed must release its snapshot and subscription."""

import asyncio
import contextlib
import contextvars
import gc
import weakref

import pytest
from starlette.requests import Request

from latet.api import rest
from latet.gateway.approvals import Approvals, Batch
from latet.server import spaces

from .test_task23_connections import _identity
from .test_task23_dashboard import _dashboard, _loop_checkpoint


class _Snapshot(dict):
    """The normal serializable summary, with weak-reference support for its lifetime witness."""


async def _feed():
    summaries, queues, unwatched = [], [], []

    class ObservedBatch(Batch):
        def summary(self):
            snapshot = _Snapshot(super().summary())
            summaries.append(weakref.ref(snapshot))
            return snapshot

    class ObservedApprovals(Approvals):
        async def watch(self):
            queue = await super().watch()
            queues.append(weakref.ref(queue))
            return queue

        def unwatch(self, queue):
            unwatched.append(True)
            super().unwatch(queue)

    approvals = ObservedApprovals()
    batch = ObservedBatch(
        "cc:owned",
        None,
        "claude-code",
        [{"kind": "user_text", "original": "owned " * 2048, "masked": "owned", "replacements": [], "suggestions": []}],
    )
    await approvals.backend.store(batch, lambda: None)
    dashboard = _dashboard()
    dashboard.rt.mediator = type("Mediator", (), {"approvals": approvals})()

    async def app(scope, receive, send):
        response = await dashboard.approvals_stream(Request(scope, receive))
        await response(scope, receive, send)

    space = spaces.Space(_identity("owned"), None, app, None, contextlib.AsyncExitStack(), 0)
    return space, approvals, summaries, queues, unwatched


def _scope(spec="2.4"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "method": "GET",
        "path": f"{rest.API}/approvals/stream",
        "headers": [],
        "query_string": b"",
    }


@pytest.mark.parametrize(("boundary", "spec"), [("start", "2.4"), ("snapshot", "2.4"), ("snapshot", "2.0")])
async def test_real_approval_feed_bounds_each_send_and_releases_its_snapshot(monkeypatch, boundary, spec):
    original_wait_for = asyncio.wait_for
    send_bound = contextvars.ContextVar("task23b_send_bound", default=None)
    tested_feed = contextvars.ContextVar("task23b_tested_feed", default=False)
    deadline_ready = asyncio.Event()
    blocked_bounds = []

    async def short_contract(awaitable, timeout):  # noqa: ASYNC109 - observing asyncio.wait_for's existing signature
        if timeout == 60:
            token = send_bound.set(timeout)
            try:
                if not tested_feed.get():
                    return await original_wait_for(awaitable, timeout)
                sending = asyncio.create_task(awaitable)
                ready = asyncio.create_task(deadline_ready.wait())
                try:
                    await asyncio.wait((sending, ready), return_when=asyncio.FIRST_COMPLETED)
                    if sending.done():
                        return sending.result()
                    return await original_wait_for(sending, 0.01)
                finally:
                    sending.cancel()
                    ready.cancel()
                    await asyncio.gather(sending, ready, return_exceptions=True)
            finally:
                send_bound.reset(token)
        return await original_wait_for(awaitable, timeout)

    # Observe the adopted 60-second contract at asyncio's existing seam; no prospective constant is required.
    monkeypatch.setattr(spaces.asyncio, "wait_for", short_contract)
    space, approvals, summaries, queues, unwatched = await _feed()
    other, colleague, _, _, colleague_unwatched = await _feed()
    blocked, never, colleague_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempted = []

    async def receive():
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        attempted.append(message["type"])
        at_boundary = (
            message["type"] == "http.response.start"
            if boundary == "start"
            else (message["type"] == "http.response.body" and b"event: snapshot" in message.get("body", b""))
        )
        if at_boundary:
            blocked_bounds.append(send_bound.get())
            blocked.set()
            await never.wait()

    async def colleague_receive():
        await colleague_read.wait()
        return {"type": "http.disconnect"}

    async def colleague_send(message):
        if b"event: snapshot" in message.get("body", b""):
            colleague_read.set()

    token = tested_feed.set(True)
    try:
        call = asyncio.create_task(spaces._held(space, _scope(spec), receive, send))
    finally:
        tested_feed.reset(token)
    healthy = None
    try:
        await original_wait_for(blocked.wait(), 2)
        assert approvals.viewers == 1 and len(summaries) == len(queues) == 1
        assert summaries[0]() is not None and queues[0]() is not None
        healthy = asyncio.create_task(spaces._held(other, _scope("2.0"), colleague_receive, colleague_send))
        await original_wait_for(colleague_read.wait(), 2)
        await original_wait_for(healthy, 2)
        assert colleague.viewers == 0 and colleague_unwatched == [True]
        await _loop_checkpoint()
        assert blocked_bounds == [60], "the real held stream sent without the adopted 60-second send bound"
        before = list(attempted)
        deadline_ready.set()  # only after positive ownership and colleague observations; the send now expires
        await original_wait_for(call, 2)
        assert attempted == before, "a stalled stream attempted a further or final send"
        assert approvals.viewers == 0 and unwatched == [True]
        assert not space.long and not space.ended and not space.long_scopes
        gc.collect()
        assert summaries[0]() is None and queues[0]() is None
    finally:
        for task in (call, healthy):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (call, healthy) if task is not None), return_exceptions=True)
