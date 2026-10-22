"""Counts of what happens, for a Latet server's metrics (`/_latet/metrics`): reported where it happens (the engine
masking a text, an exchange ending, a request waiting for a person's decision) and heard by whoever listens. On a
computer nobody listens, and a report costs a function call. A report hands over what its caller holds, as it is: what a
listener hears of it (each value's type and class, an exchange's agent and whether Latet stopped it) is made here,
inside the catch that keeps a metric from ever breaking the traffic it counts. Never a value: a type, an agent, a
count."""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

from latet.logline import Paced, described

if TYPE_CHECKING:
    from latet.engine.types import Replacement
    from latet.gateway.recorder import Outcome

log = logging.getLogger(__name__)

# The transports of an exchange with a provider: a request over HTTP, a WebSocket's turn. The Claude Code plugin's own
# checks reach the recorder too (a route check, a tool's call, the final check: ``plugin``), and are none.
EXCHANGES = frozenset({"http", "ws"})


class Listener(Protocol):
    def masked(self, found: list[tuple[str, str]]) -> None: ...  # (type, class) of each value masked

    def exchange(self, agent: str, stopped: bool) -> None: ...

    def waiting(self, delta: int) -> None: ...


_listeners: weakref.WeakSet[Listener] = weakref.WeakSet()  # a server's Metrics, while it lives
_failed = Paced()  # reports that could not be made or heard: said once a minute


def listen(listener: Listener) -> Callable[[], None]:
    """``listener`` hears every report from now on, until the function returned is called or it is gone."""
    _listeners.add(listener)
    return lambda: _listeners.discard(listener)


def listening() -> bool:
    return len(_listeners) > 0


def _tell(name: str, made: Callable[[], tuple[Any, ...] | None]) -> None:
    """What ``made`` makes of a report (None: nothing to tell) told to each listener's ``name``: nothing it raises, nor
    anything a listener raises, reaches the caller, whose traffic it counts (said in the log by its type, once a
    minute)."""
    try:
        told = made()
        heard = list(_listeners)
    except Exception as exc:
        _said(name, exc)
        return
    if told is None:
        return
    for listener in heard:
        try:
            getattr(listener, name)(*told)
        except Exception as exc:
            _said(name, exc)


def _said(name: str, error: Exception) -> None:
    if count := _failed.due():
        log.warning(
            "a report to the metrics (%s) failed (%s): it is not counted%s",
            name,
            described(error),
            f" ({count} times since the line before)" if count > 1 else "",
        )


def masked(replacements: Iterable[Replacement]) -> None:
    """The values of a text masked for the first time here, as the masker replaced them (``MaskResult.replacements``):
    each one's type and class is heard."""
    if _listeners:
        _tell("masked", lambda: _found(replacements))


def _found(replacements: Iterable[Replacement]) -> tuple[list[tuple[str, str]]] | None:
    found = [(r.etype, r.eclass.name) for r in replacements]
    return (found,) if found else None


def exchange(outcome: Outcome) -> None:
    """An exchange that ended (``Recorder.finish``), whatever the history keeps: its agent is heard, and whether Latet
    stopped it. The plugin's checks are none (``EXCHANGES``)."""
    if _listeners:
        _tell("exchange", lambda: _exchanged(outcome))


def _exchanged(outcome: Outcome) -> tuple[str, bool] | None:
    from latet.gateway.recorder import agent_of  # here: the recorder imports this module

    if outcome.transport not in EXCHANGES:
        return None
    stopped = outcome.prepared is not None and bool(outcome.prepared.block_reason)
    return agent_of(outcome.headers, outcome.path)[0], stopped


def waiting(delta: int) -> None:
    """A request or a plugin's call held for a person's decision in the Live Reviewer (``delta`` 1), or no longer
    (-1)."""
    if _listeners:
        _tell("waiting", lambda: (delta,))
