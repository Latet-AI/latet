"""The fixtures of the client's tests: the stand-in for a Latet server (``latet_stand_in``) and the waits not waited."""

from __future__ import annotations

import pytest

from latet_client import client as module
from latet_stand_in import Fake


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The seconds the client was to wait before trying again, none of them waited."""
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)

    async def asleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(module, "_sleep", sleep)
    monkeypatch.setattr(module, "_asleep", asleep)
    return waited
