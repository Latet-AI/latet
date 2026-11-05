"""What one exchange holds of its conversation's vault while its answer is awaited (the request-coverage plan's
``lease.py``; RC-REC-11): the opaque handle ``Engine.hold`` returns (an API key's or a person's ``StoredVault``: what
its release hands back, so that it takes its own count and never a newer request's), the renewer Task 9 starts while the
answer is awaited (``forward.renewing``), and the handle ``Engine.reacquire`` returns once the Live Reviewer's decision
was applied, which is the one released from then on. Over the engine's seam alone: ``hold``, ``renew``, ``reacquire``,
``release``; no vault internals.

A lease releases exactly once, and only what it holds: a release before anything was held takes nothing, a second
release nothing more, and a hold taken by an executor step that ends after the exchange was given up (the masking, or
the Live Reviewer's decision applied, after a cancellation) is released at once, once. Its renewer renews nothing once
it is released and ends at its next turn, as Task 9's does. Its ownership is handed on explicitly (``transfer``) to the
response or the WebSocket turn that ends the exchange; the lease handed on releases nothing. The engine is held weakly,
as the renewer holds it: an exchange whose answer never ends keeps no closed space alive.

The value of a decision (``outcome``) holds none of this: resources and execution facts stay outside it."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from latet.engine.pipeline import Engine, Session


class ExchangeLease:
    """One exchange's hold of ``session``'s values in ``engine``'s vault (module docstring). ``acquire`` and ``adopt``
    in the thread that masks; ``release`` wherever the exchange ends."""

    __slots__ = ("_engine", "_handle", "_holds", "_lock", "_moved", "_released", "_renewer", "_session", "_taking")

    def __init__(self, engine: Engine, session: Session) -> None:
        self._engine: weakref.ref[Any] = weakref.ref(engine)
        self._session = session
        self._handle: object | None = None
        self._holds = False
        self._released = False
        self._moved = False
        self._renewer: asyncio.Future[Any] | None = None
        self._taking = False  # a hold being taken (``acquire``, ``adopt``): no second one is
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        """Whether it holds now: acquired (or adopted), neither released nor handed on."""
        return self._holds and not self._released and not self._moved

    @property
    def released(self) -> bool:
        return self._released

    @property
    def taken(self) -> bool:
        """Whether a hold was taken (acquired or adopted), released or handed on since or not."""
        return self._holds

    @property
    def handle(self) -> object | None:
        """The handle it holds (``Engine.hold``'s, or ``Engine.reacquire``'s since), None before any."""
        return self._handle

    def acquire(self) -> None:
        """``Engine.hold``: the handle it returns registered as part of the acquisition, which is marked before the hold
        is asked (a second taker meanwhile is refused). Once; nothing for a lease already released or handed on, or
        whose engine is gone."""
        with self._lock:
            if self._holds or self._taking:
                raise RuntimeError("a lease holds once")
            if self._released or self._moved:
                return
            self._taking = True
        engine = self._engine()
        try:
            handle = engine.hold(self._session) if engine is not None else None
        except BaseException:
            with self._lock:
                self._taking = False
            raise
        if engine is None:
            with self._lock:
                self._taking = False
            return
        self._own(engine, handle)

    def adopt(self, handle: object | None) -> None:
        """A hold the masking took (``Mediator._mask_slots``' ``Engine.hold``): its handle owned from now on, and
        released at once when the exchange was given up meanwhile. A lease that holds, or is taking a hold, releases the
        newcomer once and refuses it."""
        engine = self._engine()
        with self._lock:
            busy = self._holds or self._taking
            if not busy:
                self._taking = True
        if busy:
            if engine is not None:
                engine.release(self._session, handle)
            raise RuntimeError("a lease holds once")
        if engine is None:
            with self._lock:
                self._taking = False
            return
        self._own(engine, handle)

    def _own(self, engine: Any, handle: object | None) -> None:
        with self._lock:
            self._taking = False
            if not self._released and not self._moved and not self._holds:
                self._handle, self._holds = handle, True
                return
        engine.release(self._session, handle)  # the exchange was given up while it was taken: released at once, once

    def reacquire(self) -> None:
        """``Engine.reacquire`` once the Live Reviewer's decision was applied: the handle it returns is the one this
        lease releases from then on. Nothing for a lease that does not hold; a lease released while it ran releases what
        it returned at once."""
        with self._lock:
            if not self.held:
                return
            handle = self._handle
        engine = self._engine()
        if engine is None:
            return
        again = engine.reacquire(self._session, handle)
        with self._lock:
            if not self._released and not self._moved:
                self._handle = again
                return
        if again is not handle:  # released meanwhile, its earlier handle with it: the new one too, once
            engine.release(self._session, again)

    def renew(self) -> bool:
        """``Engine.renew`` while it holds: False once released, handed on, never held, or its engine gone, and its
        renewer then ends."""
        if not self.held:
            return False
        engine = self._engine()
        return engine is not None and bool(engine.renew(self._session))

    def own_renewer(self, renewer: asyncio.Future[Any]) -> None:
        """The renewer started for the exchange (``forward.renewing``): it renews through ``renew`` and ends at its next
        turn once the lease is released; ``cancel_renewer`` ends it at once."""
        with self._lock:
            self._renewer = renewer

    def cancel_renewer(self) -> None:
        """Its renewer ended at once (an answer that streams nothing more: ``Forwarder.handle``'s ``finally``)."""
        with self._lock:
            renewer, self._renewer = self._renewer, None
        if renewer is not None:
            _cancelled(renewer)

    def release(self) -> None:
        """``Engine.release`` of its own handle, once; nothing for a lease that holds nothing, was released or handed
        on. Its renewer renews nothing more."""
        with self._lock:
            if self._released or self._moved:
                return
            self._released = True
            holds, handle = self._holds, self._handle
        if not holds:
            return
        engine = self._engine()
        if engine is not None:
            engine.release(self._session, handle)

    def transfer(self) -> ExchangeLease:
        """Its ownership handed to the response or turn that ends the exchange: a lease holding what this one held (its
        handle and renewer); this one releases nothing from then on."""
        with self._lock:
            if self._released or self._moved or self._taking:
                raise RuntimeError("a lease released, handed on or taking a hold has nothing to hand on")
            heir = ExchangeLease.__new__(ExchangeLease)
            heir._engine, heir._session, heir._lock = self._engine, self._session, threading.Lock()
            heir._handle, heir._holds, heir._renewer = self._handle, self._holds, self._renewer
            heir._released = heir._moved = heir._taking = False
            self._moved, self._renewer = True, None
        return heir


def _cancelled(renewer: asyncio.Future[Any]) -> None:
    """``renewer`` cancelled on its own loop, from that loop or from another thread."""
    loop = renewer.get_loop()
    try:
        here: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    if here is loop:
        renewer.cancel()
        return
    with contextlib.suppress(RuntimeError):  # its loop closed already: nothing runs it any more
        loop.call_soon_threadsafe(renewer.cancel)
