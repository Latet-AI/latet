"""Canonical launch-control paths and the server's physical hold reservations."""

from __future__ import annotations

import re
from collections.abc import Callable
from sys import getsizeof

from starlette.types import Scope

from latet.gateway.launch_keys import HOLD_BYTES, HOLDS

LAUNCH_HOLD_TICKET = "latet.launch_hold_ticket"
_LAUNCH = "/_latet/engine/v1/launches"
_CONTROLS = frozenset((_LAUNCH, f"{_LAUNCH}/prepare", f"{_LAUNCH}/recover"))
_ITEM_CONTROL = re.compile(rf"{_LAUNCH}/[0-9a-f]{{32}}/(?:hold|end|exit)")


def launch_control(path: str) -> bool:
    return path in _CONTROLS or _ITEM_CONTROL.fullmatch(path) is not None


def launch_hold(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "GET":
        return False
    path = scope.get("path", "")
    return path.endswith("/hold") and _ITEM_CONTROL.fullmatch(path) is not None


class LaunchHoldAdmission:
    """One counter per Server, used only on its request event loop."""

    __slots__ = ("_count", "_fits")

    def __init__(self) -> None:
        self._count = 0
        self._fits: Callable[[], bool] | None = None

    def bind_budget(self, fits: Callable[[], bool]) -> None:
        self._fits = fits

    @property
    def count(self) -> int:
        return self._count

    @property
    def retained_bytes(self) -> int:
        """The owner and one candidate application envelope per outstanding ticket."""
        return getsizeof(self) + getsizeof(self._count) + getsizeof(self._fits) + self._count * HOLD_BYTES

    def reserve(self) -> LaunchHoldTicket | None:
        if self._count >= HOLDS:
            return None
        ticket = LaunchHoldTicket(self)
        self._count += 1
        if self._fits is not None and not self._fits():
            ticket.release_unadopted()
            return None
        return ticket


class LaunchHoldTicket:
    """Lent by the gate until adoption, then owned by the physical drain."""

    __slots__ = ("_adopted", "_owner")

    def __init__(self, owner: LaunchHoldAdmission) -> None:
        self._owner: LaunchHoldAdmission | None = owner
        self._adopted = False

    def adopt(self) -> bool:
        if self._owner is None or self._adopted:
            return False
        self._adopted = True
        return True

    def release_unadopted(self) -> None:
        if not self._adopted:
            self._release()

    def close(self) -> None:
        if self._adopted:
            self._release()

    def _release(self) -> None:
        owner, self._owner = self._owner, None
        if owner is not None:
            owner._count -= 1
