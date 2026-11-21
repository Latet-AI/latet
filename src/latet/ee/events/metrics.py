"""What E4 counts for the server's metrics (`/_latet/metrics`, E0c): events kept and lost, each SIEM output's events
queued, sent, failed and dropped, and the events counted under the project ``other``. Labels are a type of event or an
output's kind (R13): never an output's name, nor a value. ``lost`` counts every event an instance took and never counted
in the log: too many waiting, the database gone while the instance stopped, or an event that was not of the schema."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, Protocol

from latet.ee.events.store import MOST_PROJECTS, OTHER
from latet.events import EVENT_TYPES

if TYPE_CHECKING:
    from latet.server.metrics import Metrics


class EventMetrics(Protocol):
    def kept(self, counts: Mapping[str, int]) -> None: ...
    def lost(self, n: int) -> None: ...
    def projects_full(self, n: int) -> None: ...
    def sent(self, output: str, n: int) -> None: ...
    def failed(self, output: str) -> None: ...
    def dropped(self, output: str, n: int) -> None: ...
    def queued(self, output: str, n: int) -> None: ...


class _Nothing:
    """No metrics: what the unit tests use."""

    def kept(self, counts: Mapping[str, int]) -> None:
        return None

    def lost(self, n: int) -> None:
        return None

    def projects_full(self, n: int) -> None:
        return None

    def sent(self, output: str, n: int) -> None:
        return None

    def failed(self, output: str) -> None:
        return None

    def dropped(self, output: str, n: int) -> None:
        return None

    def queued(self, output: str, n: int) -> None:
        return None


NO_METRICS: EventMetrics = _Nothing()

OUTPUT_KINDS = ("webhook", "splunk", "syslog")  # the SIEM's metrics' label (R13): an output's name is no set's
QUEUED_EVERY = 30.0  # seconds between two reads of what waits for the outputs (M12): one GROUP BY in PostgreSQL
_UNKNOWN = "other"  # E0c's word for a label value outside its set: an output whose kind cannot be told now


def _nothing_waits() -> Mapping[str, int]:
    return {}


def _no_kind(name: str) -> str | None:
    return None


class ServerMetrics:
    """E4's metrics in E0c's registry (`Server.metrics`): each instance counts what it did; `latet_siem_queued` reads
    what waits for each output in PostgreSQL, every ``QUEUED_EVERY`` seconds in the metrics' own threads, summed by
    the outputs' kind: the same on every instance (M12). One a registry (``of``: a name registers once, and E1's and
    E2's tests make a server's edition again on the same server, I10); ``bind`` names what it reads now: the queue's
    counts by output, and an output's kind by its name, as the settings say it. Nothing here raises to whoever counts:
    an output whose kind cannot be told now is counted as ``other``."""

    _made: ClassVar[weakref.WeakKeyDictionary[Metrics, ServerMetrics]] = weakref.WeakKeyDictionary()
    _making: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def of(cls, registry: Metrics) -> ServerMetrics:
        with cls._making:
            found = cls._made.get(registry)
            if found is None:
                found = cls._made[registry] = cls(registry)
            return found

    def __init__(self, registry: Metrics) -> None:
        # What it reads now, one pair: a read never sees the counts of one service and the kinds of another.
        self._bound: tuple[Callable[[], Mapping[str, int]], Callable[[str], str | None]] = (_nothing_waits, _no_kind)
        types = {"type": EVENT_TYPES}
        kinds = {"kind": OUTPUT_KINDS}
        self._kept = registry.counter(
            "latet_events_kept_total", "Events counted in the event log, by type.", ("type",), allowed=types
        )
        self._lost = registry.counter(
            "latet_events_lost_total",
            "Events lost before they were counted: too many waiting, the database gone while the instance stopped, "
            "or not of the schema.",
        )
        self._other = registry.counter(
            "latet_events_other_project_total",
            f"Events counted under the project {OTHER}: their identity had named more than {MOST_PROJECTS} projects "
            "in that hour on this instance.",
        )
        registry.gauge(
            "latet_siem_queued", "Events waiting for the SIEM outputs, by kind.", self._queued_now, ("kind",),
            allowed=kinds, every=QUEUED_EVERY,
        )  # fmt: skip
        self._sent = registry.counter(
            "latet_siem_sent_total", "Events the SIEM outputs took, by kind.", ("kind",), allowed=kinds
        )
        self._failed = registry.counter(
            "latet_siem_failed_total", "Batches a SIEM output could not take now, tried again later.", ("kind",),
            allowed=kinds,
        )  # fmt: skip
        self._dropped = registry.counter(
            "latet_siem_dropped_total", "Events dropped: refused by a SIEM output, or past its queue's limit.",
            ("kind",), allowed=kinds,
        )  # fmt: skip

    def bind(self, counts: Callable[[], Mapping[str, int]], kind_of: Callable[[str], str | None]) -> None:
        """The service whose queue and outputs the metrics read now (the last edition made on this server)."""
        self._bound = (counts, kind_of)

    def unbind(self, counts: Callable[[], Mapping[str, int]]) -> None:
        """A service closed: nothing is read of it any more, unless another was bound since (an edition made again
        before the one it replaces closed)."""
        if self._bound[0] == counts:
            self._bound = (_nothing_waits, _no_kind)

    def _kind(self, output: str, kind_of: Callable[[str], str | None] | None = None) -> str:
        """An output's kind, as the settings say it now; ``other`` when they cannot be read or name no such output:
        a count is never lost, nor raised, for it."""
        try:
            return (kind_of or self._bound[1])(output) or _UNKNOWN
        except Exception:  # the settings not read now: counted all the same
            return _UNKNOWN

    def _queued_now(self) -> dict[tuple[str, ...], float]:
        """In the metrics' reading threads: what waits for each output, now, by its kind."""
        counts, kind_of = self._bound
        out: dict[tuple[str, ...], float] = {}
        for name, n in counts().items():
            key = (self._kind(name, kind_of),)
            out[key] = out.get(key, 0.0) + float(n)
        return out

    def kept(self, counts: Mapping[str, int]) -> None:
        for kind, n in counts.items():
            self._kept.inc(kind, amount=n)

    def lost(self, n: int) -> None:
        self._lost.inc(amount=n)

    def projects_full(self, n: int) -> None:
        self._other.inc(amount=n)

    def sent(self, output: str, n: int) -> None:
        self._sent.inc(self._kind(output), amount=n)

    def failed(self, output: str) -> None:
        self._failed.inc(self._kind(output))

    def dropped(self, output: str, n: int) -> None:
        self._dropped.inc(self._kind(output), amount=n)

    def queued(self, output: str, n: int) -> None:
        return None  # what waits is read from PostgreSQL by the gauge itself (M12)
