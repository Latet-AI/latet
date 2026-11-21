"""A day of use through a runtime whose events a pipeline keeps, as on a server: its events add up to the history's
summaries, whatever the history keeps, and neither the events nor the log's rows hold a value, a fake, a path or a
session key."""

import time
from collections import Counter
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from latet.ee.events.pipeline import EventPipeline, Pseudonyms
from latet.ee.events.reports import Filters, Names, Period, Viewer, build_report, export_file
from latet.ee.events.store import MemoryEvents
from latet.ee.settings import EnterpriseSettings, ReportSettings
from latet.events import FIELDS
from latet.gateway.app import create_app
from latet.storage.exchanges import _history_types

from .day import a_day, forbidden, runtime, strings
from .fakes import Features

pytestmark = pytest.mark.e2e
ALL = ("type", "identity", "kind", "group", "agent", "project", "code", "decider")


def _pipeline() -> EventPipeline:
    settings = EnterpriseSettings(reports=ReportSettings(people="named"))
    return EventPipeline(MemoryEvents(), settings=lambda: settings, licensing=Features("reports", "siem"),
                         pseudonyms=Pseudonyms(lambda: b"k" * 32), group_of=lambda identity: "g_payments",
                         every=0.01)  # fmt: skip


def _rows(pipeline, by=ALL):
    return pipeline.store.query(0, time.time() + 3600, by=by)


def test_the_events_of_a_day_add_up_to_the_history_and_hold_no_value(tmp_path, upstream):
    pipeline = _pipeline()
    seen: list[dict] = []
    put = pipeline.put
    pipeline.put = lambda event: seen.append(dict(event)) or put(event)
    rt = runtime(tmp_path, upstream, pipeline)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        a_day(client, tmp_path)
    assert rt.recorder.drain() and pipeline.flush()
    summaries = rt.exchanges.summaries_since(0)
    rows = _rows(pipeline)
    requests = sum(r["events"] for r in rows if r["type"] in ("exchange", "stopped") and not r["category"])
    stopped = sum(r["events"] for r in rows if r["type"] == "stopped" and not r["category"])
    masked, expected = Counter(), Counter()
    known = _history_types()
    for row in rows:
        if row["category"]:
            # The events name each type by its code (E4), a term's too; the history names a type of Latet's own
            # detectors as it is and an owner's own (a term's) OTHER (0.11's safe summary): compared so.
            masked[row["category"] if row["category"] in known else "OTHER"] += row["masked"]
    for summary in summaries:
        expected.update(summary["new_by_type"])
    assert requests == len(summaries) == 5 and stopped == sum("blocked" in s["flags"] for s in summaries) == 1
    assert masked == +expected and masked["EMAIL"] == 1  # sent three times, counted once
    assert {r["category"] for r in rows if r["masked"]} >= {"CLIENT", "EMAIL"}  # the term by its own code
    assert {(r["identity"], r["group"]) for r in rows} == {("u_emilycarte", "g_payments")}
    bad = forbidden(rt, tmp_path)
    for item in [*seen, *rows]:
        assert set(item) <= {*FIELDS, *ALL, "category", "events", "masked"}
        for text in strings(item):
            assert not any(value in text for value in bad), text
    rt.close()
    pipeline.close()


@pytest.mark.parametrize("history", ["metadata", "off"])
def test_the_log_counts_the_same_whatever_the_history_keeps(tmp_path, upstream, history):
    counts = []
    for kept in ("full", history):
        pipeline = _pipeline()
        rt = runtime(tmp_path / kept, upstream, pipeline, history=kept)
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            a_day(client, tmp_path / kept)
        assert rt.recorder.drain() and pipeline.flush()
        rows = _rows(pipeline, ("type", "project", "code"))
        counts.append(
            sorted((r["type"], r["project"], r["code"], r["category"], r["events"], r["masked"]) for r in rows)
        )
        rt.close()
        pipeline.close()
    assert counts[0] == counts[1]


def test_a_report_by_group_and_its_exports_hold_no_value(tmp_path, upstream):
    pipeline = _pipeline()
    rt = runtime(tmp_path, upstream, pipeline)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        a_day(client, tmp_path)
    assert rt.recorder.drain() and pipeline.flush()
    period = Period(date.today() - timedelta(days=1), date.today() + timedelta(days=1))
    viewer = Viewer(None, "named", "edit")
    names = Names(groups={"g_payments": "Payments"}, identities={"u_emilycarte": "Emily Carter"},
                  kinds={"u_emilycarte": "person"})  # fmt: skip
    report = build_report(pipeline.store, period, Filters(), viewer, names)
    texts = [export_file(pipeline.store, period, Filters(), viewer, names, fmt)[2] for fmt in ("csv", "json")]
    assert report["totals"]["exchanges"] == 5 and [r["label"] for r in report["by_group"]] == ["Payments"]
    bad = forbidden(rt, tmp_path)
    for text in [*strings(report), *texts]:
        assert not any(value in text for value in bad), text
    rt.close()
    pipeline.close()
