"""One drained owner for response settlement, history admission and raw publication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from latet.gateway.mediate import RESTORED_ON_LOOP
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture, _RequestSnapshot
from latet.waiting import shielded

if TYPE_CHECKING:
    import httpx

    from latet.gateway.capture import Exchange


async def drain_task[T](task: asyncio.Task[T], stopped: Callable[[], None], *, propagate: bool = True) -> T:
    """Cancellation of an async view never abandons its actual completion owner."""
    cancelled = False
    while not task.done():
        try:
            await shielded(task)
        except asyncio.CancelledError:
            stopped()
            cancelled = True
    result = task.result()
    if cancelled and propagate:
        raise asyncio.CancelledError
    return result


# The settlements of answers relayed whole that settle alone, their waiters gone (a WebSocket's relay ended at its
# client's close: F2 of the P2 baseline), each held here until it ends, by the engine's executor that screens it: at
# most ``ALONE_PER_ENGINE`` for one engine (on a server, one identity's), each holding one answer's capture, within
# ``sse.MAX_DECODED``, and its request's snapshot. Past it, a waiter given up waits for its settlement, as before.
ALONE_PER_ENGINE = 8
_ALONE: dict[ThreadPoolExecutor, set[asyncio.Task[None]]] = {}


def _alone_ended(executor: ThreadPoolExecutor, task: asyncio.Task[None]) -> None:
    """A settlement left alone ended: nothing of it is held any more, its engine's entry gone with its last (its task's
    result is taken, so that a failure nobody awaited is not logged with its traceback)."""
    if not task.cancelled():
        task.exception()
    held = _ALONE.get(executor)
    if held is not None:
        held.discard(task)
        if not held:
            del _ALONE[executor]


@dataclass(slots=True, repr=False)
class _Work:
    capture: ResponseCapture | None
    exchange: Exchange | None = None
    status: int = 0
    terminal: bool = True


def _work(handoff: list[_Work]) -> bool:
    """Consume queued arguments on entry; Future/WorkItem tails retain no payload."""
    job = handoff.pop()
    capture, exchange = job.capture, job.exchange
    job.capture = job.exchange = None
    try:
        if capture is None:
            return False
        if exchange is None:
            return capture.screen()
        capture.publish(exchange, job.status, terminal=job.terminal)
        return True
    except BaseException:
        if capture is not None:
            if exchange is None:
                capture.omit("response-worker")
                capture.clear_payload()
                capture.headers = ()
            else:
                capture._raw.clear()
        return False
    finally:
        capture = exchange = None


def _here(job: _Work) -> bool:
    """Whether ``job`` runs where it is asked, on the event loop, instead of on the engine's thread, so that the end of
    an answer waits behind no masking of the identity's (another request's preparation, its record as left): only a
    screening (``screen`` reads the capture alone, no vault nor engine state), of an answer already omitted (cancelled,
    incomplete, past its budget: nothing of its payload read, its headers alone, within their 64 KiB of metadata), or of
    one over HTTP, not compressed, of ``RESTORED_ON_LOOP`` bytes at most, as a little is restored where it is read. A
    larger or compressed answer, or a WebSocket's, is screened on the engine's thread, and a raw publication (file
    writes) is always made there. A WebSocket's close waited there for a paused worker, until it gave up its stream."""
    capture = job.capture
    if job.exchange is not None or capture is None:
        return False
    return capture.broken or (
        not capture._websocket and not capture.content_encoding and capture._raw_bytes <= RESTORED_ON_LOOP
    )


class ResponseFinalizer:
    """OPEN -> SEALED -> SCREENING -> COMMITTED_SAFE/OMITTED -> DONE.

    The only executor is Mediator's engine executor. Two sequential actual
    operations at most; the completion task survives every transport cancel.
    Request state is frozen synchronously and never submitted to that worker.

    An answer its client was given whole (``relayed``) is recorded whole: no
    stop omits it any more, and a wait on it given up leaves its settlement to
    end alone (``leave``), so that its relay's close waits for none of it.
    """

    def __init__(
        self,
        capture: ResponseCapture,
        executor: ThreadPoolExecutor,
        recorder: Recorder | None,
        exchange: Exchange | None,
        status: int,
        *,
        terminal: bool = True,
    ) -> None:
        self.capture = capture
        self._executor = executor
        self._recorder = recorder
        self._exchange = exchange
        self._status = status
        self._terminal = terminal
        self._request: _RequestSnapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._actual: Future[bool] | None = None
        self._stopped = False
        self._relayed = False
        self.state = "OPEN"

    def seal(self, outcome: Outcome | None, *, complete: bool, error: str | None = None) -> None:
        if self.state != "OPEN":
            return
        self.state = "SEALED"
        if complete and error is None:
            self.capture.end()
        else:
            self.capture.end()
            self.capture.omit("response-cancelled" if error == "CancelledError" else "response-incomplete")
        if outcome is not None:
            outcome.capture = None
            outcome.response_headers = []
            outcome.error = self.capture.omission
            if self._recorder is not None:
                self._request = self._recorder.freeze_request(outcome)

    def relayed(self) -> None:
        """Its answer, sealed whole, reached its client whole (a WebSocket turn's last event relayed, restored): it is
        recorded whole whatever its client does next, a stop no longer omitting it (F2 of the P2 baseline: the relay's
        close stopped it while the engine's thread screened it). A turn cut off mid-answer is never marked so."""
        if self.state != "OPEN" and self.capture.omission is None:
            self._relayed = True

    def stop(self) -> None:
        # Only the loop mutates this scalar. Never clear a running worker's owner.
        if self.state not in ("COMMITTED_SAFE", "DONE") and not self._relayed:
            self._stopped = True

    def _started(self) -> asyncio.Task[None]:
        if self.state == "OPEN":
            raise RuntimeError("response-not-sealed")
        if self._task is None:
            self._task = asyncio.create_task(self._complete(), name="latet-response-finalizer", context=Context())
        return self._task

    def leave(self, then: Callable[[], None] | None = None) -> bool:
        """Whether its settlement now ends alone, waited on by nobody (an answer relayed whole, whose relay goes):
        started if it was not, and held until it ends (``_ALONE``), by nothing else; then it holds nothing (its
        capture's bytes and its request's snapshot let go as it ends). ``then`` runs once it has ended (at once if it
        has): what its caller would have done after waiting for it. False, ``then`` not run, its caller waits for it as
        before, when it was not relayed whole, or when its engine has ``ALONE_PER_ENGINE`` settling alone already."""
        if not self._relayed:
            return False
        task = self._started()
        if not task.done():
            held = _ALONE.setdefault(self._executor, set())
            if task not in held:
                if len(held) >= ALONE_PER_ENGINE:
                    if not held:
                        del _ALONE[self._executor]
                    return False
                held.add(task)
                task.add_done_callback(partial(_alone_ended, self._executor))
            if then is not None:
                task.add_done_callback(lambda _: then())
            return True
        if then is not None:
            then()
        return True

    async def _operation(self, job: _Work) -> bool:
        handoff = [job]
        if _here(job):
            return _work(handoff)  # synchronous: no cancel can abandon it, and no await comes between it and its use
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        try:
            actual = self._actual = self._executor.submit(_work, handoff)
        except RuntimeError:
            handoff.clear()
            if job.exchange is None and job.capture is not None:
                job.capture.headers = ()
            job.capture = job.exchange = None
            return False

        def completed(_: Future[bool]) -> None:
            loop.call_soon_threadsafe(ready.set, context=Context())

        actual.add_done_callback(completed)
        await ready.wait()
        try:
            return actual.result()
        finally:
            self._actual = None

    async def _complete(self) -> None:
        capture = self.capture
        self.state = "SCREENING"
        screened = await self._operation(_Work(capture))
        # No await between consuming the actual result and publication admission.
        if self._stopped or not screened:
            capture.omit("response-cancelled" if self._stopped else "response-worker")
            capture.clear_payload()
            self.state = "OMITTED"
        else:
            self.state = "COMMITTED_SAFE"
        if self._recorder is not None:
            self._recorder.submit_safe(self._request, capture, capture.headers, capture.omission)
        self._request = None
        if self._exchange is not None:
            await self._operation(_Work(capture, self._exchange, self._status, self._terminal))
        capture._raw.clear()
        self._exchange = None
        self._recorder = None
        self.state = "DONE"

    async def complete(self, *, propagate: bool = True) -> None:
        task = self._started()
        if not self._relayed:
            await drain_task(task, self.stop, propagate=propagate)
            return
        # Relayed whole: a cancel of this wait leaves the settlement to end alone, and goes at once (``leave``); past
        # the engine's bound, it waits as ``drain_task`` does. Never stopped either way.
        cancelled = False
        while not task.done():
            try:
                await shielded(task)
            except asyncio.CancelledError:
                cancelled = True
                if self.leave():
                    break
        if not task.done():
            if propagate:
                raise asyncio.CancelledError
            return
        task.result()
        if cancelled and propagate:
            raise asyncio.CancelledError


async def close_response(answer: httpx.Response, finalizer: ResponseFinalizer, error: str | None) -> None:
    """Drain close and finalization; cleanup cannot replace an existing transport failure."""
    failure: BaseException | None = None
    try:
        closing = asyncio.create_task(answer.aclose(), name="latet-response-close", context=Context())
        await drain_task(closing, finalizer.stop)
    except BaseException as exc:
        failure = exc
        finalizer.stop()
    try:
        await finalizer.complete(propagate=error is None and failure is None)
    except BaseException:
        if error is None and failure is None:
            raise
    finally:
        if failure is not None and error is None:
            raise failure
