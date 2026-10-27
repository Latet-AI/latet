"""A Latet server's metrics, in Prometheus's text format (0.0.4), on a socket of their own behind their own token
(``latet.server.operations``). Counters are fed by `latet.observe` and by the requests ``Operations`` sees; gauges are
read when Prometheus asks. A label is a kind of request, a status, an agent, one of Latet's own types, a family of
Valkey's values or the version, each from a set given when its metric is made (anything else is counted, or read, as
``other``): never a value, a text, a token, a key, a route key, a one-time code, a path, an identity or someone's own
type. No metric names an identity or counts one identity alone: each count is summed over the instance or the server.
Each instance answers for itself: Prometheus asks each one by its address.

A scrape waits on no store longer than ``probe_timeout`` in all, and scrapes never queue: one render at a time for the
instance, in a thread of the metrics' own, a scrape that comes during one answered with its text, and a text served
again within ``SERVED_AGAIN``. The probes of PostgreSQL and Valkey run side by side, each in a thread of its own within
``probe_timeout``, and a store whose probe from an earlier scrape is still out is reported down, not asked again. A
gauge that reads a store (``every``) is read in the metrics' two reading threads, at most once an interval and one read
at a time, started by a render that never waits for it: the render serves its last reading while that is younger than
``SERVED_FOR`` or twice the interval, whichever is longer (three minutes for the 30 s gauges, ten for the vaults), and
leaves the gauge out after: a Prometheus that scrapes every two minutes or more often sees them at each scrape, and a
store that stops answering loses its readings within that time, its probe saying so at once. A metric that fails, or
reads what is no number, is left out alone, said in the log once a minute by its name and the error's type.

A writer of our own, not prometheus_client: a few metrics in a fixed text format, one registry per server (two
instances in one test process must not share one), and no dependency to add."""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from typing import TYPE_CHECKING, Literal

from latet import __version__, observe
from latet.logline import Paced, described

if TYPE_CHECKING:
    from latet.server.app import Server

log = logging.getLogger(__name__)

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
OTHER = "other"  # what a label value outside its set is counted, or read, as
AGENTS = frozenset({"claude-code", "codex", "opencode", "tutorial-agent", OTHER})
OCR_UNREAD_REASONS = frozenset({"busy", "too_long", "too_many_tiles", "too_wide", "unavailable"})
# What a request is (``operations.request_kind``): never its path, which may hold a route key.
KINDS = frozenset({"agent", "plugin", "computer", "apps", "admin", "dashboard", "health", "metrics", OTHER})
# The operations of the applications API (``latet.bridge.apps``).
APPS_OPERATIONS = frozenset({"scan", "mask", "unmask", "forget"})
# The statuses a request is counted by as themselves; any other from 100 to 599 by its class, anything else as ``other``
# (an identity's destination chooses its own statuses). ``0``: a request that ended unanswered; ``ws``: a WebSocket that
# did; a WebSocket's own is 101 once accepted, or its refused handshake's.
STATUSES = frozenset(
    {"101", "200", "201", "204", "206", "301", "302", "303", "304", "307", "308", "400", "401", "403", "404", "405",
     "408", "409", "413", "415", "422", "429", "500", "502", "503", "504", "507", "529"}
)  # fmt: skip
CODES = STATUSES | {"0", "ws", "1xx", "2xx", "3xx", "4xx", "5xx"}
LABEL_VALUES = 512  # values one label takes at most
SERIES = 4096  # series one metric has at most: its labels' sets, each with ``other``, multiplied
READ_EVERY = 30.0  # seconds between two reads of a gauge that reads a store, at most once each
VAULT_EVERY = 300.0  # ... and for the vaults near their limit, a GROUP BY over every vault's every row
# Seconds a store's reading is served at least, whatever its interval (twice the interval when that is longer): a
# reading is as old as the scrapes are apart, which is a minute by Prometheus's default.
SERVED_FOR = 180.0
SERVED_AGAIN = 1.0  # seconds a scrape's text is served again to the scrapes that follow
_NAME = re.compile(r"latet_[a-z][a-z0-9_]*")
_LABEL = re.compile(r"[a-z][a-z0-9_]*")
_CHAIN_OK = {"ok": 1.0, "broken": 0.0}
_NOT_READ = object()
_builtin: frozenset[str] | None = None


def builtin_types() -> frozenset[str]:
    """Latet's own types, the only ones a metric names: the detectors', the local reviewer's, strict data mode's."""
    global _builtin
    if _builtin is None:
        from latet.engine.build import reserved_types
        from latet.engine.reviewer.reviewer import CATEGORY_TYPES

        _builtin = frozenset(reserved_types() | {etype for etype, _ in CATEGORY_TYPES.values()} | {"DATA", "FIELD"})
    return _builtin


def metric_type(etype: str, eclass: str) -> str:
    """The `type` label of a value masked: one of Latet's own types, else CUSTOM (a term's, a recognizer's or a field
    rule's own type is someone's words, never a label)."""
    return etype if eclass != "CUSTOM" and etype in builtin_types() else "CUSTOM"


def code_label(status: int | None, *, websocket: bool) -> str:
    """The `code` label of a request answered ``status`` (None: it ended unanswered)."""
    if status is None:
        return "ws" if websocket else "0"
    if str(status) in STATUSES:
        return str(status)
    return f"{status // 100}xx" if 100 <= status <= 599 else OTHER


def _read_out(task: asyncio.Task[str]) -> None:
    """A render ended: its error read here, so that a render whose scrapes all went says nothing of it."""
    if not task.cancelled():
        task.exception()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: tuple[str, str] | None = None) -> str:
    pairs = [f'{name}="{_escape(value)}"' for name, value in zip(names, values, strict=True)]
    if extra is not None:
        pairs.append(f'{extra[0]}="{extra[1]}"')
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _counted(value: object) -> float:
    """``value`` as a metric's number. Raises TypeError for what is not an int or a float (a bool, a text, None), and
    ValueError for NaN: the metric that read it is left out of the scrape."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError("a reading that is not a number")
    number = float(value)
    if math.isnan(number):
        raise ValueError("a reading that is not a number")
    return number


def _checked(
    name: str, help_text: str, labels: tuple[str, ...], allowed: Mapping[str, Iterable[str]] | None
) -> dict[str, frozenset[str]]:
    """The sets of a new metric's labels. Raises ValueError, naming the metric, for a name that is not
    ``latet_<area>_<what>``, a help text that is not one line of printable characters without a backslash, a label that
    is ``le``, not a label's name, given twice or without its set, a set of more than ``LABEL_VALUES`` values, or labels
    that would make more than ``SERIES`` series."""
    if _NAME.fullmatch(name) is None:
        raise ValueError(f"{name!r}: a name of Latet's own, latet_<area>_<what>")
    if not help_text or not help_text.isprintable() or "\\" in help_text:
        raise ValueError(f"{name}: its help is one line of printable characters, with no backslash")
    if len(set(labels)) != len(labels) or any(_LABEL.fullmatch(label) is None or label == "le" for label in labels):
        raise ValueError(f"{name}: each label a name of its own, [a-z][a-z0-9_]*, and never le")
    sets: dict[str, frozenset[str]] = {}
    series = 1
    for label in labels:
        if allowed is None or label not in allowed:
            raise ValueError(f"{name}: each label takes the values of a set given with it ({label} has none)")
        values = frozenset(allowed[label])
        if len(values) > LABEL_VALUES or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{name}: a label's set holds {LABEL_VALUES} texts at most ({label})")
        sets[label] = values
        series *= len(values | {OTHER})
    if series > SERIES:
        raise ValueError(f"{name}: its labels would make more than {SERIES} series")
    return sets


class _Metric:
    def __init__(self, name: str, help_text: str, labels: tuple[str, ...], allowed: dict[str, frozenset[str]]) -> None:
        self.name, self.help, self.labels, self._allowed = name, help_text, labels, allowed

    def _key(self, values: tuple[object, ...]) -> tuple[str, ...]:
        """The label values as counted: each in its label's set, else ``other``. Raises ValueError for another count of
        them."""
        if len(values) != len(self.labels):
            raise ValueError(f"{self.name} takes the labels ({', '.join(self.labels)})")
        return tuple(
            value if isinstance(value, str) and value in self._allowed[label] else OTHER
            for label, value in zip(self.labels, values, strict=True)
        )

    def _head(self, kind: str) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {kind}"]

    def lines(self) -> list[str]:
        raise NotImplementedError


class Counter(_Metric):
    """A count that only grows, by its labels' values (``inc``), each one taken under a lock."""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...], allowed: dict[str, frozenset[str]]) -> None:
        super().__init__(name, help_text, labels, allowed)
        self._values: dict[tuple[str, ...], float] = {} if labels else {(): 0.0}
        self._lock = threading.Lock()

    def inc(self, *values: str, amount: float = 1.0) -> None:
        """One more (or ``amount``) for the label values ``values``, in the order of the labels. Raises ValueError for
        another count of values, or an amount that is negative or no number."""
        key = self._key(values)
        if not amount >= 0 or math.isinf(amount):
            raise ValueError(f"{self.name} only grows")
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *values: str) -> float:
        key = self._key(values)
        with self._lock:
            return self._values.get(key, 0.0)

    def lines(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return self._head("counter") + [f"{self.name}{_labels(self.labels, key)} {_number(v)}" for key, v in items]


class Histogram(_Metric):
    """How long things took, in cumulative buckets, by its labels' values (``observe``)."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...],
        labels: tuple[str, ...],
        allowed: dict[str, frozenset[str]],
    ) -> None:
        super().__init__(name, help_text, labels, allowed)
        self.buckets = buckets
        self._data: dict[tuple[str, ...], list[float]] = {}  # per labels: a count per bucket, then all, then the sum
        self._lock = threading.Lock()

    def observe(self, amount: float, *values: str) -> None:
        key = self._key(values)
        if math.isnan(amount):
            return
        with self._lock:
            data = self._data.setdefault(key, [0.0] * (len(self.buckets) + 2))
            for i, bound in enumerate(self.buckets):
                if amount <= bound:
                    data[i] += 1
            data[-2] += 1
            data[-1] += amount

    def lines(self) -> list[str]:
        with self._lock:
            items = sorted((key, list(data)) for key, data in self._data.items())
        out = self._head("histogram")
        for key, data in items:
            for bound, count in zip(self.buckets, data, strict=False):
                out.append(f"{self.name}_bucket{_labels(self.labels, key, ('le', _number(bound)))} {_number(count)}")
            out.append(f"{self.name}_bucket{_labels(self.labels, key, ('le', '+Inf'))} {_number(data[-2])}")
            out.append(f"{self.name}_sum{_labels(self.labels, key)} {_number(data[-1])}")
            out.append(f"{self.name}_count{_labels(self.labels, key)} {_number(data[-2])}")
        return out


class Gauge(_Metric):
    """A number read at each scrape (``read``: a number, ``{tuple of label values: number}``, or None to be left out);
    with ``kind`` counter, a count kept elsewhere that only grows."""

    def __init__(
        self,
        name: str,
        help_text: str,
        read: Callable[[], object],
        labels: tuple[str, ...],
        allowed: dict[str, frozenset[str]],
        kind: Literal["gauge", "counter"] = "gauge",
    ) -> None:
        super().__init__(name, help_text, labels, allowed)
        self.read, self.kind = read, kind

    def lines(self, *, reading: object = _NOT_READ) -> list[str]:
        """Its lines. Raises what its read raises, TypeError or ValueError for a reading that is not a number or a
        mapping of label values to numbers: it is left out of the scrape. ``reading`` overrides the read with a
        render's captured value, so gauges derived from one reading agree for that render."""
        reading = self.read() if reading is _NOT_READ else reading
        if reading is None:
            return []
        values = reading if isinstance(reading, Mapping) else {(): reading}
        rows: dict[tuple[str, ...], float] = {}
        for found, value in values.items():
            if not isinstance(found, tuple):
                raise TypeError("label values that are not a tuple")
            key = self._key(found)
            rows[key] = rows.get(key, 0.0) + _counted(value)  # two values outside the set: summed as ``other``
        return self._head(self.kind) + [
            f"{self.name}{_labels(self.labels, k)} {_number(v)}" for k, v in sorted(rows.items())
        ]


class _Reading:
    """What a gauge reads of PostgreSQL or Valkey (``read``), in the metrics' reading threads at most once every
    ``every`` seconds, one read at a time, started by a render and never waited for by it: its last answer is served
    while it is younger than ``SERVED_FOR`` or twice ``every``, whichever is longer. A read that fails, or is still out
    when its interval comes round, counts as that interval's: it is asked again an interval later, never at each scrape,
    and the answer before it is served for its time. Safe across threads."""

    def __init__(
        self,
        name: str,
        read: Callable[[], object],
        every: float,
        clock: Callable[[], float],
        failed: Callable[[str, BaseException], None],
    ) -> None:
        self.name, self.read, self.every = name, read, every
        self.served = max(SERVED_FOR, 2 * every)  # how long an answer is served
        self._clock, self._failed = clock, failed
        self._lock = threading.Lock()
        self._asked: float | None = None  # when the latest read was asked for
        self._out: Future[None] | None = None  # the read under way, or the latest
        self._answer: tuple[object, float] | None = None  # the latest answer, and when it came

    def refresh(self, pool: ThreadPoolExecutor, now: float) -> None:
        """A read started in ``pool`` when the latest was asked for an interval ago or more and has ended."""
        with self._lock:
            if self._asked is not None and now - self._asked < self.every:
                return
            self._asked = now
            if self._out is not None and not self._out.done():
                return  # still out: this interval's reading
            try:
                self._out = pool.submit(self._run)
            except RuntimeError:  # the instance stops: its threads are let go
                self._out = None

    def _run(self) -> None:
        try:
            answer = self.read()
        except Exception as exc:  # the store away, or slower than its own bounds: said by its type
            self._failed(self.name, exc)
            return
        with self._lock:
            self._answer = (answer, self._clock())

    def latest(self, now: float) -> object | None:
        """The latest answer, when it is younger than its time (``served``) at ``now``, the render's start; else
        None."""
        with self._lock:
            found = self._answer
        return found[0] if found is not None and now - found[1] < self.served else None


class _Probe:
    """A store's probe (``latet_<name>_up``, ``latet_<name>_seconds``): ``check`` asked in a thread of its own, one at a
    time, within the render's ``probe_timeout``; a probe still out from an earlier scrape is not asked again, and
    reports the store down, its seconds counted since it began."""

    def __init__(self, name: str, check: Callable[[], object], clock: Callable[[], float]) -> None:
        self.name, self.check, self._clock = name, check, clock
        self._pool = ThreadPoolExecutor(1, thread_name_prefix=f"latet-probe-{name}")
        self._out: Future[tuple[bool, float]] | None = None
        self._began = 0.0
        self.up, self.seconds = 0.0, 0.0

    def start(self, now: float) -> Future[tuple[bool, float]] | None:
        """The probe asked now, unless one is still out (then the store is down meanwhile): the new probe, or None."""
        if self._out is not None and not self._out.done():
            self.up, self.seconds = 0.0, now - self._began
            return None
        self._began = now
        try:
            self._out = self._pool.submit(self._asked)
        except RuntimeError:  # the instance stops: its threads are let go
            self._out, self.up, self.seconds = None, 0.0, 0.0
            return None
        return self._out

    def _asked(self) -> tuple[bool, float]:
        started = self._clock()
        try:
            up = self.check() is not False
        except Exception:  # down, refused, or past its driver's own time: down
            up = False
        return up, self._clock() - started

    def settle(self, probe: Future[tuple[bool, float]]) -> None:
        """What a probe this render started says, once the render's wait is over: down while it is still out."""
        if probe.done() and not probe.cancelled() and probe.exception() is None:
            up, took = probe.result()
            self.up, self.seconds = (1.0 if up else 0.0), took
        else:
            self.up, self.seconds = 0.0, self._clock() - self._began

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class Metrics:
    """One instance's metrics: what ``Operations`` serves, and what `latet.observe` reports to once ``watch`` has begun.
    A later part adds its own with ``counter``, ``histogram`` or ``gauge``, named ``latet_<area>_<what>``, each label
    with the set of its values (``allowed``): never a label naming an identity, a person, a computer, a project, a
    folder, a route or a path, and never a gauge that reads what is one identity's (its space, its vault, its requests,
    its queue). A gauge that reads PostgreSQL or Valkey says so (``every``): it is then read in the metrics' own
    threads, never by a render. ``clock``: seconds that never go back, for tests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, probe_timeout: float = 2.0) -> None:
        self.clock = clock
        self.probe_timeout = probe_timeout
        self._all: list[_Metric] = []
        self._readings: list[_Reading] = []
        self._chain_reading: _Reading | None = None
        self._probes: list[_Probe] = []
        # The render: one at a time for the instance, in a thread of its own, never the default executor (which the
        # spaces' builds hold for seconds) nor the event loop; the reads of what gauges read of the stores, in two more.
        self._rendering = ThreadPoolExecutor(1, thread_name_prefix="latet-metrics")
        self._reads = ThreadPoolExecutor(2, thread_name_prefix="latet-metrics-read")
        self._render: asyncio.Task[str] | None = None
        self._served: tuple[float, str] | None = None  # the latest text, and when it was rendered
        self._now = clock()  # when the render under way began: what readings are aged against
        self._left: dict[str, Paced] = {}  # metrics left out of a scrape, by name: said once a minute each
        self._waiting = 0
        self._waiting_lock = threading.Lock()
        self._watching = False
        self._unlisten: Callable[[], None] | None = None
        self._ocr_failed = Paced(clock=clock)
        self.ocr_unread_total = self.counter(
            "latet_ocr_unread_total",
            "Terminal unread OCR image occurrences on this instance, including cached failures on later occurrences; "
            "not internal retries or tiles.",
            ("reason",),
            allowed={"reason": OCR_UNREAD_REASONS},
        )
        self.requests = self.counter(
            "latet_http_requests_total",
            "Requests and WebSockets this instance answered, by kind and status (a status the set does not name by its "
            "class, 1xx to 5xx; 0 a request that ended unanswered, ws a WebSocket that did, 101 one accepted).",
            ("kind", "code"),
            allowed={"kind": KINDS, "code": CODES},
        )
        self.request_seconds = self.histogram(
            "latet_http_request_duration_seconds",
            "How long this instance took to answer a request over HTTP, by kind; an agent's includes the provider's "
            "answer.",
            BUCKETS,
            ("kind",),
            allowed={"kind": KINDS},
        )
        self.exchanges = self.counter(
            "latet_agent_exchanges_total",
            "Requests of agents and programs to a provider, over HTTP or a WebSocket's turn, by agent.",
            ("agent",),
            allowed={"agent": AGENTS},
        )
        self.stops = self.counter(
            "latet_agent_stops_total",
            "Of those, the requests Latet stopped before they left, by agent (the Claude Code plugin's own stops are "
            "not counted here).",
            ("agent",),
            allowed={"agent": AGENTS},
        )
        self.masked_values = self.counter(
            "latet_masking_values_total",
            "Values masked or redacted in the texts this instance's masker read, by type (a text sent again as it was "
            "masked is not read again; CUSTOM: your own terms, recognizers and field rules).",
            ("type",),
            allowed={"type": builtin_types() | {"CUSTOM"}},
        )
        self.apps_texts = self.counter(
            "latet_apps_texts_total",
            "Texts this instance's applications API took, by operation (scan, mask, unmask; forget: the values "
            "asked), in the calls it answered.",
            ("operation",),
            allowed={"operation": APPS_OPERATIONS},
        )
        self.apps_values = self.counter(
            "latet_apps_values_total",
            "Values this instance's applications API found (scan), replaced (mask) or restored (unmask) in the texts "
            "it answered, or took out of a vault (forget), by operation.",
            ("operation",),
            allowed={"operation": APPS_OPERATIONS},
        )
        self.gauge(
            "latet_approvals_waiting",
            "Requests and plugin calls held on this instance, waiting for a person's decision in the Live Reviewer.",
            lambda: self._waiting,
        )
        self.gauge(
            "latet_build_info",
            "The Latet version this instance runs.",
            lambda: {(__version__,): 1.0},
            ("version",),
            allowed={"version": {__version__}},
        )

    def _new(
        self, name: str, help_text: str, labels: tuple[str, ...], allowed: Mapping[str, Iterable[str]] | None
    ) -> dict[str, frozenset[str]]:
        sets = _checked(name, help_text, labels, allowed)
        if any(metric.name == name for metric in self._all):
            raise ValueError(f"{name}: a new name, latet_<area>_<what>")
        return sets

    def counter(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        *,
        allowed: Mapping[str, Iterable[str]] | None = None,
    ) -> Counter:
        metric = Counter(name, help_text, labels, self._new(name, help_text, labels, allowed))
        self._all.append(metric)
        return metric

    def histogram(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...],
        labels: tuple[str, ...] = (),
        *,
        allowed: Mapping[str, Iterable[str]] | None = None,
    ) -> Histogram:
        metric = Histogram(name, help_text, buckets, labels, self._new(name, help_text, labels, allowed))
        self._all.append(metric)
        return metric

    def gauge(
        self,
        name: str,
        help_text: str,
        read: Callable[[], object],
        labels: tuple[str, ...] = (),
        *,
        allowed: Mapping[str, Iterable[str]] | None = None,
        every: float | None = None,
    ) -> None:
        """``read`` answers at each scrape a number, ``{tuple of label values: number}``, or None (left out); with
        ``every`` (a gauge that reads PostgreSQL or Valkey), it is read in the metrics' own threads at most once every
        ``every`` seconds, and a scrape serves its last answer while it is younger than ``SERVED_FOR`` or twice
        ``every``, whichever is longer."""
        sets = self._new(name, help_text, labels, allowed)
        if every is not None:
            reading = self._reading(name, read, every)
            self._all.append(Gauge(name, help_text, lambda: reading.latest(self._now), labels, sets))
            return
        self._all.append(Gauge(name, help_text, read, labels, sets))

    def _count_of(self, name: str, help_text: str, read: Callable[[], object]) -> None:
        """A count kept elsewhere that only grows, read at each scrape (a counter, with no labels)."""
        self._all.append(Gauge(name, help_text, read, (), self._new(name, help_text, (), None), kind="counter"))

    def _reading(self, name: str, read: Callable[[], object], every: float) -> _Reading:
        reading = _Reading(name, read, every, self.clock, self._said)
        self._readings.append(reading)
        return reading

    def probe(self, name: str, check: Callable[[], object], what: str) -> None:
        """``latet_<name>_up`` (1 when ``check`` answered within the probe's time, and not False) and
        ``latet_<name>_seconds`` (how long it took), measured at each scrape, one probe out at most."""
        probe = _Probe(name, check, self.clock)
        up, seconds = f"latet_{name}_up", f"latet_{name}_seconds"
        up_help = f"1 when {what} answered this instance within the probe's time, else 0."
        seconds_help = f"How long {what} took to answer this instance (while it does not, the time since it was asked)."
        self._new(up, up_help, (), None)
        self._new(seconds, seconds_help, (), None)
        self._probes.append(probe)
        self._all.append(Gauge(up, up_help, lambda: probe.up, (), {}))
        self._all.append(Gauge(seconds, seconds_help, lambda: probe.seconds, (), {}))

    def watch(self, server: Server) -> None:
        """The gauges read from the server, read only at a scrape: its spaces in memory here, its enabled identities,
        PostgreSQL and Valkey, the messages and values of Valkey dropped, the calls waiting for the credentials'
        threads, the vaults near their limit, the administration log's chain, what the log dropped; and the reports of
        `latet.observe`, heard from now on until ``close``. Once: every app of the server serves this one registry."""
        if self._watching:
            return
        self._watching = True
        from latet.server import live, logs, machines, vault  # here: the server's, which this module is imported by

        self.gauge(
            "latet_spaces_open", "Identities' spaces kept in memory on this instance.", lambda: server.spaces.count()
        )
        self.gauge(
            "latet_identities_enabled",
            "People and API keys enabled on the server.",
            lambda: server.identities.enabled_count(),
            every=READ_EVERY,
        )
        self.probe("database", lambda: server.db.ping(), "PostgreSQL")
        self.probe("valkey", lambda: server.bus.ping(), "Valkey")
        self.gauge(
            "latet_bus_rejected",
            "Messages on Valkey this instance dropped because they did not verify (a signature that is not the "
            "server's, a text that is no message, a time too far from this clock, one heard already); it only grows.",
            lambda: server.bus.rejected,
        )
        self.gauge(
            "latet_valkey_unreadable",
            "Values of Valkey that did not open or could not be read on this instance, by family (written or moved "
            "there by hand, or sealed with another key); it only grows.",
            lambda: {(family,): count for family, count in live.UNREADABLE.snapshot().items()},
            ("family",),
            allowed={"family": live.UNREADABLE_FAMILIES},
        )
        self.gauge(
            "latet_credentials_waiting",
            "Calls waiting for this instance's credentials' threads now, the ones running included.",
            machines.waiting_calls,
        )
        self.gauge(
            "latet_vault_near_limit",
            "Identities whose vault holds 90% of its limit (an API key's own, else the server's vault_limit) or more, "
            "its forgotten values' retired fakes weighed with its values, 100 at most.",
            lambda: len(vault.vault_usage(server.db, server.settings.vault_limit)),
            every=VAULT_EVERY,
        )
        chain = self._chain_reading = self._reading("latet_audit_chain", lambda: server.chain.read(), READ_EVERY)
        self.gauge(
            "latet_audit_chain_ok",
            "1 when the latest check of the last two hours found the administration log's chain whole, 0 when broken; "
            "left out when none is known (latet_audit_chain_known 0).",
            lambda: _CHAIN_OK.get(str(chain.latest(self._now))),
        )
        self.gauge(
            "latet_audit_chain_known",
            "1 when a check of the administration log's chain from the last two hours is known, else 0: the hourly "
            "check has not run, failed, or its verdict cannot be read.",
            lambda: 1.0 if str(chain.latest(self._now)) in _CHAIN_OK else 0.0,
        )
        self._count_of(
            "latet_log_dropped_total",
            "Log records this instance dropped because its output did not take them in time: its log misses lines.",
            logs.dropped,
        )
        self._unlisten = observe.listen(self)

    def ocr_unread(self, reason: str) -> None:
        """One terminal image occurrence of this instance only; never broadcast to other registries."""
        if not isinstance(reason, str) or reason not in OCR_UNREAD_REASONS:
            return
        try:
            self.ocr_unread_total.inc(reason)
        except Exception as exc:  # metrics cannot change traffic; BaseException is not a metric failure
            if count := self._ocr_failed.due():
                log.warning(
                    "could not count an unread OCR occurrence: %s%s",
                    described(exc),
                    f" ({count} times since the line before)" if count > 1 else "",
                )

    # what latet.observe reports

    def masked(self, found: Iterable[tuple[str, str]]) -> None:
        for label, count in collections.Counter(metric_type(etype, eclass) for etype, eclass in found).items():
            self.masked_values.inc(label, amount=count)  # one lock taken for each type

    def exchange(self, agent: str, stopped: bool) -> None:
        self.exchanges.inc(agent)  # an agent the set does not name: other
        if stopped:
            self.stops.inc(agent)

    def apps(self, operation: str, texts: int, values: int) -> None:
        self.apps_texts.inc(operation, amount=texts)  # an operation the set does not name: other
        self.apps_values.inc(operation, amount=values)

    def waiting(self, delta: int) -> None:
        with self._waiting_lock:
            self._waiting = max(0, self._waiting + delta)

    # what a scrape answers

    async def scrape(self) -> str:
        """The text of the metrics for a scrape, rendered in the metrics' own thread: one render at a time, a scrape
        that comes during one answered with its text, and a text served again for ``SERVED_AGAIN`` seconds. Raises
        RuntimeError once the metrics are closed."""
        loop = asyncio.get_running_loop()
        served = self._served
        if served is not None and 0 <= self.clock() - served[0] < SERVED_AGAIN:
            return served[1]
        running = self._render
        if running is None or running.done() or running.get_loop() is not loop:
            running = loop.create_task(self._rendered(), name="latet-metrics-render")
            running.add_done_callback(_read_out)
            self._render = running
        return await asyncio.shield(running)  # a scrape that goes leaves the render to the others

    async def _rendered(self) -> str:
        text = await asyncio.get_running_loop().run_in_executor(self._rendering, self.render)
        self._served = (self.clock(), text)
        return text

    def render(self) -> str:
        """The metrics in Prometheus's text format: what the gauges that read a store read last (each due one asked
        anew, never waited for), the probes (``probe_timeout`` at most), then every metric, each in a catch of its own.
        Call it in a worker thread: it waits for the probes."""
        now = self._now = self.clock()
        for reading in list(self._readings):
            reading.refresh(self._reads, now)
        started = [(probe, asked) for probe in list(self._probes) if (asked := probe.start(now)) is not None]
        if started:
            wait_for([asked for _, asked in started], timeout=self.probe_timeout)
        for probe, asked in started:
            probe.settle(asked)
        # Both chain gauges see the same completed reading, even if its next answer lands between their lines.
        # Values belong to this render alone: concurrent renders never replace one another's snapshot.
        chain = str(self._chain_reading.latest(now)) if self._chain_reading is not None else ""
        chain_values = (
            {
                "latet_audit_chain_ok": _CHAIN_OK.get(chain),
                "latet_audit_chain_known": 1.0 if chain in _CHAIN_OK else 0.0,
            }
            if self._chain_reading is not None
            else {}
        )
        lines: list[str] = []
        for metric in list(self._all):
            try:
                if isinstance(metric, Gauge) and metric.name in chain_values:
                    lines.extend(metric.lines(reading=chain_values[metric.name]))
                else:
                    lines.extend(metric.lines())
            except Exception as exc:  # what it reads is away, or no number: this metric is left out, the others shown
                self._said(metric.name, exc)
        return "\n".join(lines) + "\n"

    def _said(self, name: str, error: BaseException) -> None:
        """A metric left out of a scrape, or a read of a store that failed: said once a minute for each, by its name (a
        fixed one) and the error's type."""
        paced = self._left.get(name) or self._left.setdefault(name, Paced())
        if count := paced.due():
            log.warning(
                "the metric %s could not be read (%s)%s",
                name,
                described(error),
                f" ({count} times since the line before)" if count > 1 else "",
            )

    def close(self) -> None:
        """No more reports heard, and the metrics' threads let go (a probe or a read still out ends with its store's
        answer)."""
        if self._unlisten is not None:
            self._unlisten()
        self._rendering.shutdown(wait=False, cancel_futures=True)
        self._reads.shutdown(wait=False, cancel_futures=True)
        for probe in self._probes:
            probe.close()
