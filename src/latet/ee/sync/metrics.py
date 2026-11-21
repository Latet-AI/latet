"""Term sync's metrics in E0c's registry (``Server.metrics``), as E5's ``ApprovalMetrics`` registers its own (R25): once
a registry (``of``), bound to the term sync of the edition made last on this server. What this instance masks, how far
it trails the server (B-R6) and the terms it cannot open are its own; the terms kept and the sources' last syncs are
read from PostgreSQL (every instance says the same: take the largest); the runs are counted where they ran (add them
up). Kinds, states and outcomes only: never a source's name, a term's type or a term."""

from __future__ import annotations

import threading
import weakref
from typing import TYPE_CHECKING, ClassVar, Final

from latet.ee.sync.records import CODES
from latet.ee.sync.store import uncounted

if TYPE_CHECKING:
    from latet.ee.sync.service import TermSync
    from latet.ee.sync.store import Run
    from latet.server.metrics import Metrics

KINDS: Final = ("file", "url", "graph", "ldap")
STATES: Final = ("ok", "failed", "never")
KEPT: Final = ("in-source", "gone")
OUTCOMES: Final = ("ok", *CODES)
EVERY: Final = 30.0  # seconds between two reads of PostgreSQL, in the metrics' own threads


class TermMetrics:
    _made: ClassVar[weakref.WeakKeyDictionary[Metrics, TermMetrics]] = weakref.WeakKeyDictionary()
    _making: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def of(cls, registry: Metrics) -> TermMetrics:
        with cls._making:
            found = cls._made.get(registry)
            if found is None:
                found = cls._made[registry] = cls(registry)
            return found

    def __init__(self, registry: Metrics) -> None:
        self._bound: TermSync | None = None
        registry.gauge("latet_terms_masked", "Synced terms this instance masks now.", self._masked)
        registry.gauge(
            "latet_terms_behind_seconds",
            "Seconds this instance has masked older synced terms than the server holds (0: none): it cannot read them "
            "again.",
            self._behind,
        )  # B-R6: each instance its own, no label
        registry.gauge(
            "latet_terms_kept",
            "Synced terms kept, still in their source or no longer (masked until someone removes them).",
            self._kept, ("state",), allowed={"state": KEPT}, every=EVERY,
        )  # fmt: skip
        registry.gauge(
            "latet_terms_unreadable", "Synced terms this instance cannot open: another master key sealed them.",
            self._unreadable,
        )  # fmt: skip
        registry.gauge(
            "latet_terms_sources", "Term sync's sources, by kind and how their last sync went.",
            self._sources, ("kind", "state"), allowed={"kind": KINDS, "state": STATES}, every=EVERY,
        )  # fmt: skip
        self._runs = registry.counter(
            "latet_terms_runs_total", "Syncs run on this instance, by the source's kind and how they ended.",
            ("kind", "outcome"), allowed={"kind": KINDS, "outcome": OUTCOMES},
        )  # fmt: skip

    def bind(self, sync: TermSync) -> None:
        """The term sync whose terms the metrics read, and whose runs they count, now (the last edition made on this
        server)."""
        self._bound = sync
        sync.counted = self.counted

    def unbind(self, sync: TermSync) -> None:
        """An edition closed: its runs count nothing any more, and its terms are read no more unless another was bound
        since (an edition made again before the one it replaces closed)."""
        if self._bound is sync:
            self._bound = None
        sync.counted = uncounted

    def counted(self, run: Run) -> None:
        self._runs.inc(run.kind, "ok" if run.outcome == "ok" else run.code or "error")  # outside its set: other

    # Each reading answers None while no term sync is bound: left out of the scrape. What this instance masks and how
    # far it trails are left out too until its terms first loaded (review Minor 3): until then it builds no space, and 0
    # for both would read as a healthy instance with no terms; an alert asks for their absence.

    def _masked(self) -> float | None:
        sync = self._bound
        return None if sync is None or not sync.loaded else float(sync.live.count)

    def _unreadable(self) -> float | None:
        sync = self._bound
        return None if sync is None else float(sync.unreadable)

    def _behind(self) -> float | None:
        """B-R6: how long the terms this instance masks have trailed those the server holds; 0 while they do not."""
        sync = self._bound
        if sync is None or not sync.loaded:
            return None
        return float(b["seconds"]) if (b := sync.behind()) else 0.0

    def _kept(self) -> dict[tuple[str, ...], float] | None:
        """In the metrics' reading threads: the terms kept, still in their source or no longer."""
        sync = self._bound
        if sync is None:
            return None
        counts = sync.store.counts()
        return {
            ("in-source",): float(sum(c["terms"] for c in counts.values())),
            ("gone",): float(sum(c["gone"] for c in counts.values())),
        }

    def _sources(self) -> dict[tuple[str, ...], float] | None:
        """In the metrics' reading threads: the sources set up, by kind and how their last run went (`never`: none
        recorded), every pair said."""
        sync = self._bound
        if sync is None:
            return None
        runs = sync.store.runs()
        found: dict[tuple[str, ...], float] = {(kind, state): 0.0 for kind in KINDS for state in STATES}
        for source in sync.sources():
            last = runs.get(source.name)
            pair = (source.kind, "never" if last is None else "ok" if last.outcome == "ok" else "failed")
            found[pair] = found.get(pair, 0.0) + 1  # a kind outside KINDS: counted as other by the registry
        return found
