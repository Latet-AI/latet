"""What term sync adds to an instance's metrics (Latet Enterprise, R25): the terms it masks, how far it trails the
server (B-R6), the terms kept and those it cannot open, its sources by kind and how their last sync went, and the runs
by kind and how they ended; never a source's name, a term's type or a term."""

import time

from latet.ee.events.locks import MemoryLocks
from latet.ee.sync.metrics import EVERY, TermMetrics
from latet.ee.sync.records import SourceError
from latet.server.metrics import Metrics

from .fakes import Clock, make_sync, sources
from .test_service import CUSTOMERS, PROJECTS


class Ticks:
    """The registry's clock: moved past EVERY, the store is read again at the next render."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _sample(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + "{") and all(f'{key}="{value}"' in line for key, value in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


def _until(registry: Metrics, ticks: Ticks, name: str, value: float, **labels: str) -> str:
    """A render once the gauge read the store (in the metrics' own threads, never waited for by a scrape)."""
    for _ in range(300):
        text = registry.render()
        if _sample(text, name, **labels) == value:
            return text
        ticks.now += EVERY + 1
        time.sleep(0.01)
    raise AssertionError(f"{name} {labels} never read {value}")


def test_the_metrics_count_terms_and_runs_by_kind_and_never_name_a_term_a_source_or_a_type():
    locks = MemoryLocks(Clock())

    def refused(source, secret):
        raise SourceError("refused", "the address answered 401")

    both = [{"company": "Halden Insurance"}, {"company": "Kestrel Analytics"}]
    settings = {"now": sources(CUSTOMERS, PROJECTS)}
    sync = make_sync(locks=locks, settings=settings, readers={"file": lambda source, secret: both, "url": refused})
    ticks = Ticks()
    registry = Metrics(clock=ticks)  # as E5's test makes its registry
    try:
        TermMetrics.of(registry).bind(sync)
        sync.tick()
        _until(registry, ticks, "latet_terms_sources", 1.0, kind="url", state="failed")  # both readings answered once
        text = _until(registry, ticks, "latet_terms_kept", 2.0, state="in-source")
        assert _sample(text, "latet_terms_sources", kind="url", state="failed") == 1.0
        assert _sample(text, "latet_terms_runs_total", kind="url", outcome="refused") == 1.0
        assert _sample(text, "latet_terms_runs_total", kind="file", outcome="ok") == 1.0
        assert "latet_terms_masked 2" in text and "latet_terms_behind_seconds 0" in text  # B-R6
        for word in ("Halden", "Kestrel", "customers", "projects", "CLIENT"):
            assert word not in text
        TermMetrics.of(registry).unbind(sync)
    finally:
        sync.close()
        registry.close()


def test_one_registry_has_one_term_metrics_and_an_edition_closed_leaves_the_one_bound_after_it():
    """As E5's ``ApprovalMetrics`` (I10): once a registry, so that an edition made again on a server binds the same;
    the closing one's unbinding leaves the newer bound, and its runs count nothing; once none is bound, the term sync's
    gauges are left out of the scrape and its runs counted nowhere."""
    clock = Clock()
    locks = MemoryLocks(clock)
    readers = {"file": lambda source, secret: [{"company": "Halden Insurance"}]}
    old = make_sync(locks=locks, settings={"now": sources(CUSTOMERS)}, readers=readers)
    new = make_sync(locks=locks, settings={"now": sources(CUSTOMERS)}, readers=readers)
    ticks = Ticks()
    registry = Metrics(clock=ticks)
    metrics = TermMetrics.of(registry)
    try:
        assert TermMetrics.of(registry) is metrics
        metrics.bind(old)
        metrics.bind(new)
        metrics.unbind(old)
        old.tick()  # the closed edition's run: counted nowhere
        new.reload()  # the newer one's terms loaded: none yet
        text = registry.render()
        assert _sample(text, "latet_terms_runs_total", kind="file", outcome="ok") is None
        assert "latet_terms_masked 0" in text  # the newer one's, which has run nothing
        new.tick()
        text = _until(registry, ticks, "latet_terms_sources", 1.0, kind="file", state="ok")
        assert _sample(text, "latet_terms_runs_total", kind="file", outcome="ok") == 1.0
        metrics.unbind(new)
        text = registry.render()
        assert "latet_terms_masked " not in text and "latet_terms_behind_seconds " not in text
        assert "latet_terms_unreadable " not in text
        clock.now += 3600  # its source due again: run, and counted nowhere
        assert [run.outcome for run in new.tick()] == ["ok"]
        assert _sample(registry.render(), "latet_terms_runs_total", kind="file", outcome="ok") == 1.0
    finally:
        metrics.unbind(new)
        old.close()
        new.close()
        registry.close()


def test_a_source_never_run_is_never_and_one_whose_run_failed_is_failed():
    locks = MemoryLocks(Clock())
    sync = make_sync(locks=locks, settings={"now": sources(CUSTOMERS, PROJECTS)}, readers={})
    ticks = Ticks()
    registry = Metrics(clock=ticks)
    metrics = TermMetrics.of(registry)
    try:
        metrics.bind(sync)
        text = _until(registry, ticks, "latet_terms_sources", 1.0, kind="url", state="never")
        assert _sample(text, "latet_terms_sources", kind="file", state="never") == 1.0
        assert _sample(text, "latet_terms_sources", kind="graph", state="never") == 0.0
        assert _sample(text, "latet_terms_kept", state="gone") == 0.0
        sync.tick()  # no reader for either kind: both fail `not-installed`
        text = _until(registry, ticks, "latet_terms_sources", 1.0, kind="file", state="failed")
        assert _sample(text, "latet_terms_sources", kind="file", state="never") == 0.0
        assert _sample(text, "latet_terms_runs_total", kind="url", outcome="not-installed") == 1.0
    finally:
        metrics.unbind(sync)
        sync.close()
        registry.close()


def test_an_instance_whose_terms_never_loaded_says_neither_what_it_masks_nor_how_far_it_trails():
    """Review Minor 3: until its first load, an instance masks no space (each is refused), so it says no count of its
    terms and no lag, rather than 0 for both, which reads as a healthy instance with no terms: an alert asks for the
    gauges' absence. What it cannot open is said all the same (B-I2)."""
    locks = MemoryLocks(Clock())
    sync = make_sync(locks=locks, settings={"now": sources(CUSTOMERS)}, readers={})
    sync.licensing.knows_ = sync.licensing.licensed_ = False  # the license never read: nothing can be loaded
    registry = Metrics(clock=Ticks())
    metrics = TermMetrics.of(registry)
    try:
        metrics.bind(sync)
        sync.tick()
        text = registry.render()
        assert not sync.loaded and "latet_terms_unreadable 0" in text
        assert "latet_terms_masked" not in text and "latet_terms_behind_seconds" not in text
        sync.licensing.knows_ = sync.licensing.licensed_ = True
        sync.reload()
        text = registry.render()
        assert "latet_terms_masked 0" in text and "latet_terms_behind_seconds 0" in text
    finally:
        metrics.unbind(sync)
        sync.close()
        registry.close()
