"""The history and its retention: pruned at the start with your retention, at once when you make it shorter, and by
a timer while the daemon runs; what the recorder asks its backends (the blocks recorded, the job's lock, the feed)."""

import asyncio
import logging
import sqlite3
import threading
import time

import pytest

from latet.config import Config, IdentityConfig, load_config
from latet.engine.types import SlotKind
from latet.gateway import recorder as recorder_module
from latet.gateway.app import create_app
from latet.gateway.mediate import VERIFY, Prepared
from latet.gateway.recorder import Broadcaster, Outcome, Recorder
from latet.protocols.base import SlotRef
from latet.runtime import make_runtime
from latet.state import LocalJobLock, LocalSentBlocks
from latet.storage.exchanges import ExchangeRecord, ExchangeStore

from ...conftest import ROOT_KEY
from .conftest import _until


def _history(path, ages_in_days):
    store = ExchangeStore(path, ROOT_KEY)
    now = time.time()
    for days in ages_in_days:
        store.record(ExchangeRecord({"ts": now - days * 86400, "session": f"{days}d", "agent": "codex", "flags": []}))
    return store


def test_opening_the_history_prunes_nothing(tmp_path):
    _history(tmp_path / "exchanges.db", [30]).close()
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY, retention_days=14)
    try:
        assert [s["session"] for s in store.list()[0]] == ["30d"]  # its retention is the settings', not yet known
    finally:
        store.close()


@pytest.mark.parametrize(
    ("retention_days", "ages_in_days", "kept"),
    [
        pytest.param(60, [10, 30, 100], ["10d", "30d"], id="longer-than-the-default"),
        pytest.param(7, [3, 10, 30], ["3d"], id="shorter-than-the-default"),
    ],
)
def test_the_start_prunes_the_history_with_the_retention_set(tmp_path, retention_days, ages_in_days, kept):
    data = tmp_path / "data"
    _history(data / "exchanges.db", ages_in_days).close()
    config = Config(identity=IdentityConfig(enabled=False), retention_days=retention_days)
    rt = make_runtime(config, data=data, config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        assert [s["session"] for s in rt.exchanges.list()[0]] == kept  # its own retention, not the default 14 days
    finally:
        rt.close()


def _recorded(recorder, texts):
    pending = [(SlotRef(texts, i, SlotKind.USER_TEXT), [], False) for i in range(len(texts))]
    prepared = Prepared(VERIFY, "anthropic", b"", None, document=texts, pending=pending)
    recorder.finish(Outcome({"user-agent": "claude-cli/2"}, "anthropic", "/v1/messages", "http", time.time(), prepared))
    assert recorder.drain()


def test_the_recorder_asks_its_backends_what_was_recorded_and_whether_to_prune(tmp_path, monkeypatch):
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    sent, jobs = LocalSentBlocks(), LocalJobLock()
    recorder = Recorder(store, sent=sent, jobs=jobs)
    monkeypatch.setattr(recorder_module, "_PRUNE_EVERY", -1)  # every exchange would prune
    pruned = []
    monkeypatch.setattr(store, "prune", lambda now=None: pruned.append(now) or 0)
    with jobs.hold("history-prune", 60):  # another process prunes it now
        _recorded(recorder, ["call Ada Byron"])
    _recorded(recorder, ["call Ada Byron"])
    recorder.close()
    # Refused the lock: what is a day past the retention, and only that. The whole of it once, under the lock.
    assert len(pruned) == 2 and pruned[0] <= time.time() - 86400 and pruned[1] is None
    assert not sent.first("unscoped", store.block_id("call Ada Byron"))  # recorded in its conversation already
    [second, first] = store.list()[0]
    # The text is new to its conversation the first time and not the second: the flag each block was recorded with.
    assert [store.detail(x["id"])["request"]["slots"][0]["new"] for x in (first, second)] == [True, False]
    assert len(store.in_order("unscoped")) == 2
    store.close()


class NoViewers(Broadcaster):
    """A feed that is false when asked: a server's may answer how many watch it, and nobody does yet."""

    def __len__(self):
        return 0


def test_the_recorder_tells_the_feed_it_is_given_even_one_that_is_false_when_asked():
    feed = NoViewers()
    assert not feed and Recorder(None, broadcaster=feed).broadcaster is feed


def _aged_runtime(tmp_path, **settings):
    """A runtime with a retention of 60 days, whose history holds exchanges 3, 10 and 30 days old (named by their age)."""
    data = tmp_path / "data"
    _history(data / "exchanges.db", [3, 10, 30]).close()
    config = Config(identity=IdentityConfig(enabled=False), retention_days=60, **settings)
    return make_runtime(config, data=data, config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)


@pytest.fixture
def aged(tmp_path, request):
    """That runtime, with the settings a test gives it (``indirect`` parametrization): the history on, by default."""
    rt = _aged_runtime(tmp_path, **getattr(request, "param", {}))
    yield rt
    rt.close()


HISTORY_ON_OR_OFF = pytest.mark.parametrize(
    "aged", [{}, {"history": "off"}], indirect=True, ids=["recorded", "history-off"]
)


def _kept(rt):
    return [s["session"] for s in rt.exchanges.list()[0]]


def _retention(rt, days):
    """The settings with another retention, applied; the pruning they ask for is queued on the recorder's thread."""
    rt.update_config(lambda current: current.model_copy(update={"retention_days": days}))
    assert rt.recorder.drain()


def _pruning(rt, monkeypatch):
    """The prunes of the runtime's history, as they happen."""
    pruned, prune = [], rt.exchanges.prune
    monkeypatch.setattr(rt.exchanges, "prune", lambda now=None: pruned.append(now) or prune(now))
    return pruned


@HISTORY_ON_OR_OFF  # what is on disk is the history all the same, whether or not more is recorded
def test_a_retention_made_shorter_prunes_the_history_at_once(aged):
    assert _kept(aged) == ["3d", "10d", "30d"]
    _retention(aged, 7)
    assert _kept(aged) == ["3d"]  # not at the next start, nor with the first exchange recorded six hours from now
    _retention(aged, 60)
    assert _kept(aged) == ["3d"]


def test_a_retention_made_longer_or_left_as_it_is_prunes_nothing(aged, monkeypatch):
    pruned = _pruning(aged, monkeypatch)
    _retention(aged, 90)
    _retention(aged, 90)  # as it is
    aged.update_config(lambda current: current.model_copy(update={"history": "metadata"}))  # another setting
    assert aged.recorder.drain() and pruned == [] and _kept(aged) == ["3d", "10d", "30d"]


def test_a_retention_made_shorter_is_pruned_once_and_only_when_no_other_process_is_pruning(aged, monkeypatch):
    pruned = _pruning(aged, monkeypatch)
    with aged.recorder.jobs.hold("history-prune", 60):  # another process prunes it now: its prune is the one
        _retention(aged, 30)
    # Refused the lock: what is a day past the retention alone, here none of them.
    assert [now is None for now in pruned] == [False] and _kept(aged) == ["3d", "10d", "30d"]
    _retention(aged, 7)
    _retention(aged, 7)  # as it is
    assert [now is None for now in pruned] == [False, True] and _kept(aged) == ["3d"]


def test_the_exchange_recorded_after_a_pruning_that_was_asked_for_does_not_prune_again(aged, monkeypatch):
    pruned = _pruning(aged, monkeypatch)
    monkeypatch.setattr(aged.recorder, "_last_prune", time.time() - 2 * recorder_module._PRUNE_EVERY)  # due
    _retention(aged, 7)
    _recorded(aged.recorder, ["call Ada Byron"])  # the interval's prune would come with this exchange
    assert pruned == [None]


def test_settings_that_cannot_be_saved_do_not_cut_the_history(aged, monkeypatch):
    def disk_full(config, path=None):
        raise OSError("disk full")

    monkeypatch.setattr("latet.state.local.save_config", disk_full)  # what writes config.yaml, for a computer
    with pytest.raises(OSError, match="disk full"):
        aged.update_config(lambda current: current.model_copy(update={"retention_days": 7}))
    # config.yaml keeps its 60 days, and so does the history: it is cut once the settings are kept.
    assert aged.recorder.drain() and _kept(aged) == ["3d", "10d", "30d"]


def test_settings_that_cannot_be_saved_leave_the_historys_retention_as_it_was(aged, monkeypatch):
    """The history takes the new retention once the settings are kept: a save that fails leaves its own as it was, so
    that the next interval's prune does not cut it by settings that were never kept."""

    def disk_full(config, path=None):
        raise OSError("disk full")

    monkeypatch.setattr("latet.state.local.save_config", disk_full)
    with pytest.raises(OSError, match="disk full"):
        aged.update_config(lambda current: current.model_copy(update={"retention_days": 7}))
    assert aged.exchanges.retention_days == 60
    aged.recorder.prune_now()  # the next interval's prune
    assert aged.recorder.drain() and _kept(aged) == ["3d", "10d", "30d"]


def test_a_pruning_that_fails_is_logged_and_does_not_fail_the_settings(aged, monkeypatch, caplog):
    def disk_error(now=None):  # the store's error, which a store's words can fill with what it holds
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(aged.exchanges, "prune", disk_error)
    _retention(aged, 7)  # not raised; and ``drain`` answered: nothing is left pending
    assert aged.config.retention_days == 7 and load_config(aged.config_file).retention_days == 7
    [failure] = [r for r in caplog.records if r.getMessage() == "could not prune the history: OperationalError"]
    assert failure.levelno == logging.ERROR and failure.exc_info is None and "disk I/O error" not in caplog.text
    assert _kept(aged) == ["3d", "10d", "30d"]  # the next interval prunes again


def test_a_pruning_that_cannot_be_queued_leaves_nothing_pending(aged, caplog):
    aged.recorder._executor.shutdown()  # the thread has ended, as when ``close`` races a settings change
    aged.recorder.prune_now()
    assert aged.recorder.drain(1)  # a count left one too high would wait for ever
    failed = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in failed] == ["could not queue the pruning of the history: RuntimeError"]
    assert failed[0].exc_info is None  # by its type alone


def test_an_exchange_that_cannot_be_queued_leaves_nothing_pending(aged, caplog):
    aged.recorder._executor.shutdown()  # the thread has ended, as when ``close`` races the last exchange
    pending = [(SlotRef(["call Ada Byron"], 0, SlotKind.USER_TEXT), [], False)]
    prepared = Prepared(VERIFY, "anthropic", b"", None, document=["call Ada Byron"], pending=pending)
    aged.recorder.finish(
        Outcome({"user-agent": "claude-cli/2"}, "anthropic", "/v1/messages", "http", time.time(), prepared)
    )
    assert aged.recorder.drain(1)  # a count left one too high would wait for ever
    [failure] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert failure.getMessage() == "could not queue an exchange for the dashboard: RuntimeError"


def test_a_due_pruning_that_fails_does_not_lose_the_exchange_it_came_with(aged, monkeypatch, caplog):
    """The interval's prune, due when an exchange is recorded, is a job of its own on the recorder's thread: its failure
    is said, and the exchange is recorded all the same."""

    def disk_error(now=None):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(aged.exchanges, "prune", disk_error)
    monkeypatch.setattr(aged.recorder, "_last_prune", time.time() - 2 * recorder_module._PRUNE_EVERY)  # due
    before = len(aged.exchanges.list()[0])
    _recorded(aged.recorder, ["call Ada Byron"])
    assert len(aged.exchanges.list()[0]) == before + 1
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert said == ["could not prune the history: OperationalError"]


def test_a_failed_job_is_said_before_it_stops_counting_as_pending(aged, monkeypatch, caplog):
    """What waits for the recorder (``drain``: the tests, a space's close) finds its failure said already."""

    def disk_error(now=None):
        time.sleep(0.1)  # under way when its end is waited for, as a pruning is: said on the recorder's thread
        raise sqlite3.OperationalError("disk I/O error")

    error, said = recorder_module.log.error, threading.Event()

    def slow_error(*args, **kwargs):
        time.sleep(0.2)  # a log that takes a while to write
        error(*args, **kwargs)
        said.set()

    monkeypatch.setattr(aged.exchanges, "prune", disk_error)
    monkeypatch.setattr(recorder_module.log, "error", slow_error)
    aged.recorder.prune_now()
    assert aged.recorder.drain() and said.is_set()


async def test_asking_for_the_prune_when_it_is_due_goes_on_after_a_failure(aged, monkeypatch, caplog):
    """The task that asks for the history's prune by the clock outlives a failure of its own, said by its type."""
    asked = []

    def due():
        asked.append(len(asked))
        if len(asked) == 1:
            raise RuntimeError("the history's clock failed")
        return 0.01

    monkeypatch.setattr(aged.recorder, "_prune_if_due", due)
    monkeypatch.setattr(recorder_module, "_PRUNE_RETRY", 0.01)  # a minute, in use
    task = asyncio.ensure_future(aged.recorder.prune_when_due())
    try:
        await _until(lambda: len(asked) >= 3)
    finally:
        task.cancel()
    said = [r.getMessage() for r in caplog.records if r.name == "latet.gateway.recorder"]
    assert said[0].startswith("could not ask for the pruning of the history: RuntimeError")


def test_closing_right_after_a_retention_made_shorter_still_prunes_before_the_store_closes(tmp_path):
    rt = _aged_runtime(tmp_path)
    rt.update_config(lambda current: current.model_copy(update={"retention_days": 7}))
    rt.close()  # no ``drain`` first: the pruning may be queued still
    store = ExchangeStore(tmp_path / "data" / "exchanges.db", ROOT_KEY)
    try:
        assert [s["session"] for s in store.list()[0]] == ["3d"]
    finally:
        store.close()


@pytest.fixture
def quick(monkeypatch):
    """The recorder's prune interval, a tenth of a second for the test. The recorder reads it each time it looks
    whether a prune is due, so it holds for a runtime made before this fixture as for one made after."""
    monkeypatch.setattr(recorder_module, "_PRUNE_EVERY", 0.1)


def _past_its_retention(rt):
    """An exchange 100 days old in a history kept 60 days, as one that became so while the daemon ran."""
    old = {"ts": time.time() - 100 * 86400, "session": "100d", "agent": "codex", "flags": []}
    rt.exchanges.record(ExchangeRecord(old))
    assert _kept(rt) == ["3d", "10d", "30d", "100d"]


@HISTORY_ON_OR_OFF
async def test_a_daemon_that_records_nothing_prunes_its_history_when_the_interval_has_passed(quick, aged):
    _past_its_retention(aged)  # nothing is recorded: no exchange comes to carry the prune
    app = create_app(aged.gateway)
    async with app.router.lifespan_context(app):
        await _until(lambda: "100d" not in _kept(aged))
    assert _kept(aged) == ["3d", "10d", "30d"]


async def test_a_daemon_waits_for_the_interval_before_it_prunes_again(aged):
    _past_its_retention(aged)
    app = create_app(aged.gateway)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.3)  # the start pruned already (``make_runtime``): the next prune is an interval away
        assert aged.recorder.drain() and "100d" in _kept(aged)


def test_the_timer_asks_once_per_interval_whoever_pruned_last(aged, monkeypatch):
    recorder, interval, asked = aged.recorder, recorder_module._PRUNE_EVERY, []
    prune_now = recorder.prune_now
    monkeypatch.setattr(recorder, "prune_now", lambda: asked.append(1) or prune_now())
    assert 0 < recorder._prune_if_due() <= interval and asked == []  # pruned at the start: it falls due in an interval
    recorder._last_prune -= interval + 1  # an interval passed, and nothing was recorded to carry the prune
    assert recorder._prune_if_due() == interval and len(asked) == 1  # asked, and a whole interval to the next
    assert recorder._prune_if_due() > interval - 5 and len(asked) == 1  # not again at once
    recorder._last_prune -= interval + 1
    _recorded(recorder, ["call Ada Byron"])  # the interval's prune rides on this exchange: asked as a job of its own
    assert len(asked) == 2
    assert recorder._prune_if_due() > interval - 5 and len(asked) == 2  # so the timer does not ask for it again


async def test_stopping_the_daemon_ends_the_timer_that_prunes(aged):
    app = create_app(aged.gateway)
    async with app.router.lifespan_context(app):
        [timer] = [t for t in asyncio.all_tasks() if t.get_name() == "latet-prune"]
        assert not timer.done()
    assert timer.cancelled()  # ended, not left pending for the loop to destroy


async def test_a_daemon_with_no_history_starts_no_timer(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(
        config, data=tmp_path / "d", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY, record=False
    )
    try:
        assert rt.recorder is None
        app = create_app(rt.gateway)
        async with app.router.lifespan_context(app):
            assert [t for t in asyncio.all_tasks() if t.get_name() == "latet-prune"] == []
    finally:
        rt.close()
