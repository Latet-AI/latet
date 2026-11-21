"""What an instance hears for the Enterprise Edition: messages on Valkey's channels (ee:license, ee:settings,
identities; each later part adds its own) saying that another instance changed what this one keeps a copy of. A thread
of its own iterates E0b's Bus.listen in an event loop of its own, calls each channel's handler, and listens again when
the connection drops. A message lost meanwhile is made up for by the copies read again every REFRESH_SECONDS
(latet.ee.server).

A message only ever makes an instance read PostgreSQL again: the edition's handlers drop a copy and take nothing from
what the message holds, whoever could publish it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from latet.server.bus import Bus

log = logging.getLogger(__name__)

Handler = Callable[[Mapping[str, Any]], None]
LONGEST_WAIT = 15.0  # between two tries to listen again, while Valkey does not answer


class Listener:
    def __init__(self, bus: Bus, handlers: Mapping[str, Handler], *, retry: float = 1.0) -> None:
        self.bus = bus
        self.handlers = dict(handlers)
        self.retry = retry
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread = threading.Thread(target=self._run, name="latet-ee-listener", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._started.wait(5)  # its loop is there: stop() can reach it

    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        """Stop listening, at once, even while waiting for a message. Stopping again does nothing more."""
        self._stopping.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            with contextlib.suppress(RuntimeError):  # its loop already ended
                loop.call_soon_threadsafe(task.cancel)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._started.set()
        delay = self.retry
        while not self._stopping.is_set():
            try:
                async for channel, message in self.bus.listen(*self.handlers):
                    delay = self.retry
                    self._handle(channel, message)
            except asyncio.CancelledError as exc:
                if self._stopping.is_set():  # stop(): the only end
                    return
                self._not_stopped(exc, delay)
            except Exception as exc:  # the connection dropped, Valkey restarted: listen again
                # By its type: Valkey's words can name its address.
                log.warning(
                    "not hearing the Enterprise channels (%s): listening again in %g s", type(exc).__name__, delay
                )
            if self._stopping.is_set():
                return
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError as exc:
                if self._stopping.is_set():
                    return
                self._not_stopped(exc, delay)
            delay = min(delay * 2, LONGEST_WAIT)

    def _not_stopped(self, exc: BaseException, delay: float) -> None:
        """A cancellation that stop() did not make (a driver's own): said by its type, and the listener listens again
        rather than ending, which would leave the instance hearing nothing until it restarts."""
        task = asyncio.current_task()
        if task is not None and task.cancelling():  # taken back: the next waits are not cancelled by it
            task.uncancel()
        log.warning(
            "the Enterprise channels' listening was cancelled (%s), not stopped: listening again in %g s",
            type(exc).__name__,
            delay,
        )

    def _handle(self, channel: str, message: Mapping[str, Any]) -> None:
        for name, handler in self.handlers.items():
            if channel == name or channel.endswith(":" + name):  # heard with the bus's prefix, or without
                try:
                    handler(message)
                except Exception as exc:  # by its type: its words can quote what it applied
                    log.warning("the Enterprise Edition could not apply a message on %s (%s)", name, type(exc).__name__)
                return
