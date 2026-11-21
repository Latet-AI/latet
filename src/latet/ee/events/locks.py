"""The locks of E4's jobs: on a server Valkey's (`Bus.lock(name, seconds)`, which runs out after ``seconds`` when its
holder dies, and `Bus.lease`, which also says whether it is still the holder's); in the tests ``MemoryLocks``, the same
for every object given it, with a clock the test moves."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

LockFn = Callable[[str, float], AbstractContextManager[bool]]


class Lease(Protocol):
    """A lock taken or not (its truth), and whether it is still this holder's now, with ``margin`` seconds of it left
    at least (C1). ``release()`` gives it back now, from any thread, while it is still this holder's (never another's
    that took it since): a holder that stops before its work ends (E6's term sync, closed in the middle of a read)."""

    def __bool__(self) -> bool: ...
    def held(self, margin: float = 0.0) -> bool: ...
    def release(self) -> None: ...


LeaseFn = Callable[[str, float], AbstractContextManager[Lease]]


class Held:
    """``MemoryLocks``' lease."""

    def __init__(
        self, mine: bool, left: Callable[[], float | None], give_back: Callable[[], None] = lambda: None
    ) -> None:
        self._mine, self._left, self._give_back = mine, left, give_back

    def __bool__(self) -> bool:
        return self._mine

    def held(self, margin: float = 0.0) -> bool:
        if not self._mine:
            return False
        left = self._left()  # seconds; None when the lock is no longer this holder's
        return left is not None and left > 0 and left >= margin

    def release(self) -> None:
        if self._mine:
            self._give_back()


class MemoryLocks:
    """Locks in this process's memory, each running out ``seconds`` after it was taken, by ``clock``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._held: dict[str, tuple[str, float]] = {}
        self._mutex = threading.Lock()

    @contextmanager
    def lease(self, name: str, seconds: float) -> Iterator[Held]:
        """True: this holder runs the job now, until it leaves the block or ``seconds`` pass; ``held(margin)`` says
        whether it still does, with ``margin`` seconds left."""
        token = uuid.uuid4().hex
        with self._mutex:
            found = self._held.get(name)
            mine = found is None or found[1] <= self.clock()
            if mine:
                self._held[name] = (token, self.clock() + seconds)

        def left() -> float | None:
            with self._mutex:
                now = self._held.get(name)
                return now[1] - self.clock() if now is not None and now[0] == token else None

        def give_back() -> None:
            with self._mutex:
                if self._held.get(name, ("", 0.0))[0] == token:  # still its own: not one that ran out and was taken
                    del self._held[name]

        try:
            yield Held(mine, left, give_back)
        finally:
            if mine:
                give_back()

    @contextmanager
    def lock(self, name: str, seconds: float) -> Iterator[bool]:
        with self.lease(name, seconds) as taken:
            yield bool(taken)
