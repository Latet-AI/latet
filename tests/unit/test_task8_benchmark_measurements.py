"""Task8 measurement behavior; no service, timing target, allocation or containment process is exercised."""

import contextlib
import ctypes
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_bench_helpers():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.append(str(ROOT / "scripts"))

    import bench_apps
    import bench_apps_server
    import bench_engine
    import terms_bench
    import vault_fuzz

    return bench_apps, bench_apps_server, bench_engine, terms_bench, vault_fuzz


bench_apps, bench_apps_server, bench_engine, terms_bench, vault_fuzz = _load_bench_helpers()


def helper(name):
    path = ROOT / "notes" / "captures" / "perf0102" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_http_cpu_boundary_excludes_json_and_preserves_success_denominator(monkeypatch):
    events = []
    ticks = iter([(10, 100), (12, 110), (12, 110), (22, 150)])
    times = iter([0.0, 1.0, 1.0, 10.0])

    def cpu():
        events.append("cpu")
        return next(ticks)

    monkeypatch.setattr(bench_engine, "_cpu_times", cpu)
    monkeypatch.setattr(bench_apps.time, "perf_counter", lambda: next(times))

    class Answer:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            events.append("json")
            return (
                {"results": [{"text": "masked", "replacements": [{}]}]}
                if self.status_code == 200
                else {"error": "limit"}
            )

    statuses = iter([200, 422])

    class Client:
        def post(self, *args, **kwargs):
            events.append("post")
            return Answer(next(statuses))

    measured = bench_apps.run(Client(), "mask", ["first", "second"], 1, "data")
    assert measured.seconds == [1.0] and measured.texts == 1
    assert measured.answers == ["masked", None] and measured.refused == {422: 1}
    assert events == ["cpu", "post", "cpu", "json", "cpu", "post", "cpu", "json"]
    assert [record["seconds"] for record in measured.requests] == [1.0, 9.0]
    assert [record["status"] for record in measured.requests] == [200, 422]
    deltas = [record["cpu"] for record in measured.requests]
    assert [(sample["idle"], sample["total"], sample["percent"]) for sample in deltas] == [(2, 10, 80), (10, 40, 75)]
    assert 100 * (1 - sum(sample["idle"] for sample in deltas) / sum(sample["total"] for sample in deltas)) == 76


@pytest.mark.parametrize("end", [None, (0, 0), (11, 10), (float("nan"), 10)])
def test_unavailable_or_invalid_machine_cpu_is_not_a_qualified_zero(monkeypatch, end):
    ticks = iter([(0, 0), end])
    monkeypatch.setattr(bench_engine, "_cpu_times", lambda: next(ticks))
    sample = bench_engine.Load().sample()
    assert sample["status"] != "ok" and sample["percent"] is None
    json.dumps(sample, allow_nan=False)


def test_failed_http_call_retains_finite_interval_before_reraising(monkeypatch, capsys):
    ticks = iter([(0, 0), (1, 4)])
    times = iter([4.0, 6.0])
    monkeypatch.setattr(bench_engine, "_cpu_times", lambda: next(ticks))
    monkeypatch.setattr(bench_apps.time, "perf_counter", lambda: next(times))

    class Client:
        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("synthetic")

    with pytest.raises(httpx.ReadTimeout):
        bench_apps.run(Client(), "mask", ["one"], 1, "data")
    sample = json.loads(capsys.readouterr().out)["request_failure"]
    assert sample["status"] is None and sample["error"] == "ReadTimeout"
    assert sample["seconds"] == 2 and sample["cpu"]["percent"] == 75


def test_actual_cached_and_joined_observation_delegates_without_preflight():
    texts = [f"fixture{n}" for n in range(501)]
    calls, joined_calls = [], []
    sentinel = object()

    def cached(text, *args, **kwargs):
        calls.append(text)
        return text != texts[-1]

    def joined(unread, *args, **kwargs):
        joined_calls.append(list(unread))
        return sentinel

    engine, masker = SimpleNamespace(cached=cached), SimpleNamespace(joined=joined)
    with bench_apps_server.cached_observation(engine, masker, texts) as proof:
        assert calls == joined_calls == []
        unread = [text for text in texts if not engine.cached(text, "kind", "session")]
        assert masker.joined(unread, "kind") is sentinel
    assert calls == texts and joined_calls == [[texts[-1]]]
    assert engine.cached is cached and masker.joined is joined
    bench_apps_server.require_cached_proof(proof, "500+1")


@pytest.mark.parametrize("change", ["extra-probe", "wrong-joined", "new-was-cached"])
def test_cached_qualification_rejects_incorrect_actual_path_evidence(change):
    proof = {"decisions": [[i, True] for i in range(500)] + [[500, False]], "joined": [[500]]}
    if change == "extra-probe":
        proof["decisions"].append([500, False])
    elif change == "wrong-joined":
        proof["joined"] = [[0, 500]]
    else:
        proof["decisions"][-1][1] = True
    with pytest.raises(RuntimeError, match="actual cache/joined"):
        bench_apps_server.require_cached_proof(proof, "500+1")


def cached_raw_request(arm):
    return {
        "operation": "mask",
        "workload": arm,
        "request_index": 0,
        "texts": 501 if arm == "500+1" else 1,
        "seconds": 0.5 if arm == "500+1" else 0.1,
        "cpu": {"status": "ok", "idle": 1, "total": 4, "percent": 75.0},
        "status": 200,
    }


ALONE_REPEATS = 2


def cached_record_requests(arm):
    """What an arm's record holds of its timed requests: the one request of 500+1; the one text's requests sent back to
    back and their window (``bench_apps_server.repeated``): 0.2 s over two, 0.1 s a request."""
    if arm == "500+1":
        return {"requests": [cached_raw_request(arm)]}
    requests = [cached_raw_request(arm) for _ in range(ALONE_REPEATS)]
    for request in requests:
        request["cpu"] = {"status": "invalid", "idle": 0, "total": 0, "percent": None}  # under the clock's tick
    window = {
        "repeats": ALONE_REPEATS,
        "seconds": 0.2,
        "cpu": {"status": "ok", "idle": 2, "total": 8, "percent": 75.0},
        "per_request": {"seconds": 0.1, "cpu_idle": 1.0, "cpu_total": 4.0, "cpu_percent": 75.0},
    }
    return {"requests": requests, "window": window}


@pytest.mark.parametrize("difference", ["counter", "answer", "fixture", "order", None])
def test_six_cached_records_require_symmetric_fixture_and_fixed_order(tmp_path, capsys, difference):
    order = [(0, "500+1"), (0, "1-alone"), (1, "1-alone"), (1, "500+1"), (2, "500+1"), (2, "1-alone")]
    paths = []
    for number, (pair, arm) in enumerate(order):
        record = {
            "pair": pair,
            "arm": arm,
            "qualified": True,
            "fixture_sha256": "fixed",
            "new_answer_sha256": "same",
            "state_before": {"counter": 12, "population": 1000000},
            **cached_record_requests(arm),
        }
        if number == 1:
            if difference == "counter":
                record["state_before"]["counter"] += 1
            elif difference == "answer":
                record["new_answer_sha256"] = "different"
            elif difference == "fixture":
                record["fixture_sha256"] = "different"
            elif difference == "order":
                record["arm"] = "500+1"
        path = tmp_path / f"arm{number}.json"
        path.write_text(json.dumps(record))
        paths.append(path)
    args = SimpleNamespace(results=paths)
    if difference:
        with pytest.raises((RuntimeError, ValueError)):
            bench_apps_server.cached_pairs(args)
    else:
        bench_apps_server.cached_pairs(args)
        result = json.loads(capsys.readouterr().out)
        assert [pair["ratio"] for pair in result["pairs"]] == [5, 5, 5]
        assert [pair["alone_repeats"] for pair in result["pairs"]] == [ALONE_REPEATS] * 3
        assert [pair["alone_cpu_busy_seconds"] for pair in result["pairs"]] == [3.0] * 3  # (8 - 2) / 2
        assert [pair["mixed_cpu_busy_seconds"] for pair in result["pairs"]] == [3] * 3
        assert result["numeric_acceptance_threshold"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-request",
        "missing-request",
        "http-refusal",
        "cpu-unavailable",
        "cpu-nonfinite",
        "seconds-nan",
        "seconds-infinity",
        "seconds-negative-infinity",
        "seconds-zero",
        "seconds-negative",
        "seconds-true",
        "seconds-false",
        "wrong-operation",
        "wrong-workload",
        "wrong-cardinality",
        "wrong-request-index",
        "cpu-zero-total",
        "cpu-negative-idle",
        "cpu-idle-over-total",
        "cpu-inconsistent-percent",
        "cpu-boolean",
    ],
)
def test_cached_pairs_reject_invalid_raw_request_even_when_qualified_is_true(tmp_path, mutation):
    order = [(0, "500+1"), (0, "1-alone"), (1, "1-alone"), (1, "500+1"), (2, "500+1"), (2, "1-alone")]
    paths = []
    seconds = {
        "seconds-nan": float("nan"),
        "seconds-infinity": float("inf"),
        "seconds-negative-infinity": float("-inf"),
        "seconds-zero": 0,
        "seconds-negative": -0.5,
        "seconds-true": True,
        "seconds-false": False,
    }
    for number, (pair, arm) in enumerate(order):
        record = {
            "pair": pair,
            "arm": arm,
            "qualified": True,
            "fixture_sha256": "fixed",
            "new_answer_sha256": "same",
            "state_before": {"counter": 12, "population": 1000000},
            **cached_record_requests(arm),
        }
        request = record["requests"][0]
        if number == 0:
            if mutation == "extra-request":
                record["requests"].append(cached_raw_request(arm))
            elif mutation == "missing-request":
                record["requests"] = []
            elif mutation == "http-refusal":
                request["status"] = 422
            elif mutation == "cpu-unavailable":
                request["cpu"] = {"status": "unavailable", "idle": None, "total": None, "percent": None}
            elif mutation == "cpu-nonfinite":
                request["cpu"]["percent"] = float("nan")
            elif mutation == "wrong-operation":
                request["operation"] = "unmask"
            elif mutation == "wrong-workload":
                request["workload"] = "1-alone"
            elif mutation == "wrong-cardinality":
                request["texts"] = 500
            elif mutation == "wrong-request-index":
                request["request_index"] = 1
            elif mutation == "cpu-zero-total":
                request["cpu"]["total"] = 0
            elif mutation == "cpu-negative-idle":
                request["cpu"]["idle"] = -1
            elif mutation == "cpu-idle-over-total":
                request["cpu"]["idle"] = 5
            elif mutation == "cpu-inconsistent-percent":
                request["cpu"]["percent"] = 50.0
            elif mutation == "cpu-boolean":
                request["cpu"]["idle"] = True
            else:
                request["seconds"] = seconds[mutation]
        path = tmp_path / f"arm{number}.json"
        # Intentionally permit JSON's nonfinite extension here to test hostile/invalid saved records.
        path.write_text(json.dumps(record))
        paths.append(path)
    with pytest.raises((RuntimeError, ValueError)):
        bench_apps_server.cached_pairs(SimpleNamespace(results=paths))


@pytest.mark.parametrize(
    "mutation",
    [
        "no-window",
        "one-repeat",
        "short-window",
        "repeats-not-requests",
        "window-cpu-invalid",
        "window-cpu-inconsistent",
        "window-seconds-nan",
        "request-refused",
        "request-wrong-cardinality",
        "request-seconds-zero",
    ],
)
def test_cached_pairs_reject_an_invalid_window_of_the_one_text_arm(tmp_path, mutation):
    """The one text's arm is read from its window alone (``repeated``): one that is missing, shorter than
    ``CACHED_WINDOW_SECONDS``, of fewer than two requests or of other requests than it counts, or whose processors'
    time says nothing true, is refused, as is any of its requests that is not the arm's successful one."""
    order = [(0, "500+1"), (0, "1-alone"), (1, "1-alone"), (1, "500+1"), (2, "500+1"), (2, "1-alone")]
    paths = []
    for number, (pair, arm) in enumerate(order):
        record = {
            "pair": pair,
            "arm": arm,
            "qualified": True,
            "fixture_sha256": "fixed",
            "new_answer_sha256": "same",
            "state_before": {"counter": 12, "population": 1000000},
            **cached_record_requests(arm),
        }
        if number == 1:
            window, request = record.get("window"), record["requests"][-1]
            if mutation == "no-window":
                del record["window"]
            elif mutation == "one-repeat":
                window["repeats"] = 1
                record["requests"] = record["requests"][:1]
            elif mutation == "short-window":
                window["seconds"] = bench_apps_server.CACHED_WINDOW_SECONDS / 2
            elif mutation == "repeats-not-requests":
                record["requests"].append(cached_raw_request(arm))
            elif mutation == "window-cpu-invalid":
                window["cpu"] = {"status": "invalid", "idle": 0, "total": 0, "percent": None}
            elif mutation == "window-cpu-inconsistent":
                window["cpu"]["percent"] = 50.0
            elif mutation == "window-seconds-nan":
                window["seconds"] = float("nan")
            elif mutation == "request-refused":
                request["status"] = 422
            elif mutation == "request-wrong-cardinality":
                request["texts"] = 2
            else:
                request["seconds"] = 0
        path = tmp_path / f"arm{number}.json"
        path.write_text(json.dumps(record))
        paths.append(path)
    with pytest.raises((RuntimeError, ValueError)):
        bench_apps_server.cached_pairs(SimpleNamespace(results=paths))


def test_the_one_text_arm_is_timed_as_requests_back_to_back_in_one_window(monkeypatch):
    """A request of a few milliseconds is shorter than the clock that counts the processors' time (15.6 ms on
    Windows): the one text's arm sends it again and again until the window is ``CACHED_WINDOW_SECONDS`` at least (two
    at least), the engine's cache of the text dropped between two, never before the first nor after the last; the
    window's seconds and processors' time are read once at each end, and each request's share is the window's over
    their number."""
    now = [0.0]
    events = []

    def send():
        events.append("send")
        now[0] += 0.007
        return len(events)

    class Load:
        def sample(self):
            events.append("cpu-end")
            return {"status": "ok", "idle": 0.3, "total": 1.2, "percent": 75.0}

    monkeypatch.setattr(bench_engine, "Load", Load)
    results, window = bench_apps_server.repeated(send, lambda: events.append("between"), clock=lambda: now[0])
    repeats = window["repeats"]
    assert repeats == 15 and len(results) == 15  # 15 x 7 ms: the first past 100 ms
    assert events == ["send"] + ["between", "send"] * 14 + ["cpu-end"]
    assert window["seconds"] == pytest.approx(0.105)
    assert window["cpu"] == {"status": "ok", "idle": 0.3, "total": 1.2, "percent": 75.0}
    assert window["per_request"] == pytest.approx(
        {"seconds": 0.007, "cpu_idle": 0.3 / 15, "cpu_total": 1.2 / 15, "cpu_percent": 75.0}
    )
    # Bounded: past ``CACHED_REPEATS_AT_MOST`` it stops however short the window.
    events.clear()
    results, window = bench_apps_server.repeated(send, lambda: None, clock=lambda: 0.0, at_most=5)
    assert window["repeats"] == 5 and window["seconds"] == 0.0


def manifest(tmp_path, monkeypatch):
    root = tmp_path / "source"
    paths = [
        "scripts/bench_engine.py",
        "scripts/vault_fuzz.py",
        "scripts/terms_bench.py",
        "src/latet/server/vault.py",
        "src/latet/server/index.py",
        "src/latet/engine/stored.py",
    ]
    pins = {}
    for relative in paths:
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("synthetic source")
        pins[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(bench_engine, "HERE", root / "scripts")
    schema = "latet_t_keyfuzz_task8known_0123456789ab_0"
    record = {
        "version": "task8-measurement-v1",
        "purpose": "known",
        "campaign": "0123456789ab",
        "ordinal": 0,
        "schema": schema,
        "count": 5000,
        "index": "on",
        "phase": "planned",
        "source_root": str(root),
        "pins": pins,
        "output": str(tmp_path / "result.json"),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(record))
    return schema, path, record


@pytest.mark.parametrize("fault", ["schema", "count", "pin", "reuse", "output"])
def test_manifest_rejection_precedes_database_or_store_allocation(tmp_path, monkeypatch, fault):
    schema, path, record = manifest(tmp_path, monkeypatch)
    if fault == "schema":
        record["schema"] += "_other"
    elif fault == "count":
        record["count"] = 4999
    elif fault == "pin":
        record["pins"]["scripts/bench_engine.py"] = "0" * 64
    elif fault == "reuse":
        record["phase"] = "allocated"
    else:
        record["output"] = str(tmp_path.parent / "elsewhere.json")
    path.write_text(json.dumps(record))

    def forbidden(*args, **kwargs):
        pytest.fail("manifest rejection reached allocation")

    monkeypatch.setattr(bench_engine, "_measurement_connection", forbidden)
    monkeypatch.setattr(vault_fuzz, "PgStores", forbidden)
    expected = {
        "schema": "^exact schema mismatch$",
        "count": "^fixed corpus count mismatch$",
        "pin": "^source pin mismatch$",
        "reuse": "^measurement manifest already used$",
        "output": "^output must be beside its manifest$",
    }
    with (
        pytest.raises(ValueError, match=expected[fault]),
        bench_engine.owned_fuzz_stores(schema, path, "known", index=True),
    ):
        pytest.fail("invalid manifest entered measurement")


@pytest.mark.parametrize("failure", ["collision", "constructor", "primary-close-drop", None])
def test_cleanup_owns_only_successful_create_and_preserves_all_failures(tmp_path, monkeypatch, failure):
    schema, path, _ = manifest(tmp_path, monkeypatch)
    commands, constructed = [], []

    class Connection:
        def execute(self, query, params=None):
            statement = query if isinstance(query, str) else query.as_string()
            commands.append((statement, params))
            if statement.startswith("CREATE") and failure == "collision":
                raise RuntimeError("collision")
            if statement.startswith("DROP") and failure == "primary-close-drop":
                raise OSError("drop")
            return self

        def fetchall(self):
            return []

    @contextlib.contextmanager
    def connection():
        yield Connection()

    class Stores:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            if failure == "constructor":
                raise ValueError("constructor")

        def close(self, *, drop):
            assert drop is False
            if failure == "primary-close-drop":
                raise LookupError("close")

    monkeypatch.setattr(bench_engine, "_measurement_connection", connection)
    monkeypatch.setattr(vault_fuzz, "PgStores", Stores)

    def run():
        with bench_engine.owned_fuzz_stores(schema, path, "known", index=True):
            if failure == "primary-close-drop":
                raise ArithmeticError("primary")

    if failure:
        with pytest.raises(BaseExceptionGroup) as caught:
            run()
        if failure == "primary-close-drop":
            assert [type(error) for error in caught.value.exceptions] == [ArithmeticError, LookupError, OSError]
    else:
        run()
    record = json.loads(path.read_text())
    drops = [query for query, _ in commands if query.startswith("DROP")]
    if failure == "collision":
        assert not drops and not constructed and record["cleanup_verified"] is False
    else:
        assert len(drops) == 1 and f'"{schema}"' in drops[0]
        assert record["cleanup_verified"] is (failure != "primary-close-drop")
    if failure == "constructor":
        assert record["outcome"] == "failed" and record["phase"] == "cleanup_verified"


def test_partial_pg_constructor_closes_pool_and_preserves_migration_and_close_errors(monkeypatch):
    from latet.server import db

    closed = []

    class Database:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            closed.append(True)
            raise OSError("close")

    def migrate(_, *, master):
        raise ValueError("migration")

    monkeypatch.setenv("LATET_TEST_DATABASE_URL", "synthetic")
    monkeypatch.setattr(db, "Database", Database)
    monkeypatch.setattr(db, "migrate", migrate)
    with pytest.raises(BaseExceptionGroup) as caught:
        vault_fuzz.PgStores(schema="latet_t_keyfuzz_task8known_0123456789ab_0")
    assert closed == [True]
    assert [type(error) for error in caught.value.exceptions] == [ValueError, OSError]


def test_finite_exact_cleanup_connection_options(monkeypatch):
    import psycopg

    calls = []
    monkeypatch.setenv("LATET_TEST_DATABASE_URL", "synthetic")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: calls.append((args, kwargs)))
    bench_engine._measurement_connection()
    assert calls == [
        (
            ("synthetic",),
            {"autocommit": True, "connect_timeout": 5, "options": "-c statement_timeout=10000 -c lock_timeout=5000"},
        )
    ]


def test_populated_index_cannot_qualify_empty_ready_or_stale_watermark():
    interior = helper("task8-interior-measure.py")

    class Store:
        identity = "synthetic"
        index = SimpleNamespace(ready=True, watermark=0, nbytes=0)
        builds = 0

        def build_index(self):
            self.builds += 1
            return True

        def count_entities(self):
            return 1000

        def prints_complete(self):
            return True

        @contextlib.contextmanager
        def connection(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (3,)))

    store = Store()
    store.db = store
    with pytest.raises(RuntimeError, match="watermark"):
        interior.require_populated_index(store, 1000, prepare=True)
    assert store.builds == 1
    store.index.watermark = 3
    assert interior.require_populated_index(store, 1000, prepare=False)["population"] == 1000


def test_indexed_sql_counts_actual_print_rows_separately_from_budget_metadata():
    interior = helper("task8-interior-measure.py")
    from latet.server import vault as pg

    rows = [("id", "EMAIL", 1, b"123", b"4567", 0, 0, "rule", False, 9999, [12, 34])]
    observer = interior.SqlObservation()
    raw = SimpleNamespace(execute=lambda *args: None, fetchmany=lambda size: rows)
    cursor = observer.cursor(raw)
    assert cursor.execute(pg._BY_PRINTS).fetchmany(512) is rows
    result = observer.report()
    assert result["statements"] == {"by_prints": 1} and result["rows"] == {"by_prints": 1}
    assert result["entity_variable_payload_bytes"] == 2 + 5 + 3 + 4 + 4
    assert result["print_metadata_items"] == 2
    assert interior.sql_kind(pg._BOUNDED_IDS) == "bounded_ids"
    assert interior.sql_kind(pg._BOUNDED_HELD) == "retained"
    assert interior.sql_kind("SELECT counter FROM change_counters WHERE owner = %s") == "counter"
    assert "excludes PrintIndex" in result["scope"]


def test_span_oracle_preserves_known_echo_order_and_changed_spans():
    interior = helper("task8-interior-measure.py")
    from latet.engine.types import EntityClass, Span

    first = Span(1, 3, "EMAIL", EntityClass.PII, "known")
    other = Span(2, 3, "EMAIL", EntityClass.PII, "known")
    digest = interior.span_digest([([first], [])])
    assert digest != interior.span_digest([([], [first])])
    assert digest != interior.span_digest([([other], [])])
    assert digest == interior.span_digest([([first], [])])


def test_memory_native_peaks_keep_os_names_and_current_private_behavior(monkeypatch):
    def process_info(handle, pointer, size):
        counters = pointer._obj
        counters.PrivateUsage = 101000000
        counters.WorkingSetSize = 202000000
        counters.PeakWorkingSetSize = 303000000
        counters.PeakPagefileUsage = 404000000
        return 1

    kernel = SimpleNamespace(GetCurrentProcess=lambda: 1)
    psapi = SimpleNamespace(GetProcessMemoryInfo=process_info)
    monkeypatch.setattr(terms_bench.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel, psapi=psapi), raising=False)
    counters = terms_bench.memory_counters()
    assert counters["current_private_bytes"] == 101000000
    assert counters["lifetime_peak_working_set_bytes"] == 303000000
    assert counters["lifetime_peak_pagefile_bytes"] == 404000000
    assert "peak_private_bytes" not in counters
    assert terms_bench.memory_mb() == 101


def test_memory_sample_high_is_labelled_as_lower_bound_and_includes_reservation(monkeypatch):
    samples = iter([{"status": "ok", "current_private_bytes": 100}, {"status": "ok", "current_private_bytes": 150}])
    monkeypatch.setattr(bench_engine, "memory_counters", lambda: next(samples))
    store = SimpleNamespace(
        _building_index=SimpleNamespace(building_bytes=320), index=SimpleNamespace(nbytes=80), index_bytes=600
    )
    observation = bench_engine.MemoryObservation(lambda: store)
    observation.sample()
    store._building_index = None
    store.index_bytes = 100
    observation.sample()
    report = observation.report()
    assert report["sampled_high_lower_bounds"] == {
        "current_private_bytes": 150,
        "reserved_build_bytes": 320,
        "published_array_bytes": 80,
        "store_index_bytes": 600,
    }
    assert report["samples"] == 2 and report["sample_interval_seconds"] == 0.01


def test_memory_sample_keeps_reservation_of_zero_length_building_index_with_old_index_retained(monkeypatch):
    class Index:
        def __init__(self, prints, building_bytes):
            self.prints = prints
            self.nbytes = prints * 8
            self.building_bytes = building_bytes

        def __len__(self):
            return self.prints

    class Store:
        def __init__(self):
            self.index = Index(10, 0)
            self._building_index = Index(0, 320)

        @property
        def index_bytes(self):
            return self.index.nbytes + self._building_index.nbytes + self._building_index.building_bytes

    store = Store()
    assert not store._building_index
    assert store._building_index.building_bytes == 320
    monkeypatch.setattr(bench_engine, "memory_counters", lambda: {"status": "ok", "current_private_bytes": 100})
    observation = bench_engine.MemoryObservation(lambda: store)
    observation.sample()
    report = observation.report()
    assert report["errors"] == []
    assert report["sampled_high_lower_bounds"]["published_array_bytes"] == 80
    assert report["sampled_high_lower_bounds"]["store_index_bytes"] == 400
    assert report["sampled_high_lower_bounds"]["reserved_build_bytes"] == 320


def test_readiness_clock_includes_opening_request_and_uses_actual_new_key_population(tmp_path, monkeypatch):
    events = []

    class Memory:
        def __init__(self, store):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def report(self):
            return {"qualified": True}

    class Load:
        def sample(self):
            events.append("cpu-end")
            return {"status": "ok", "percent": 25}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            events.append("opening-post")
            return SimpleNamespace(raise_for_status=lambda: None)

    ticks = iter([10.0, 12.5])

    def clock():
        events.append("timer")
        return next(ticks)

    monkeypatch.setattr(bench_engine, "MemoryObservation", Memory)
    monkeypatch.setattr(bench_engine, "Load", Load)
    monkeypatch.setattr(bench_apps_server.httpx, "Client", Client)
    monkeypatch.setattr(bench_apps_server.time, "perf_counter", clock)
    store = SimpleNamespace(
        index=SimpleNamespace(ready=True, nbytes=0, watermark=0), count_entities=lambda: 0, prints_complete=lambda: True
    )
    server = SimpleNamespace(
        spaces=SimpleNamespace(_spaces={"new": SimpleNamespace(parts=SimpleNamespace(vault=store))})
    )
    token = tmp_path / "token"
    token.write_text("synthetic")
    result = bench_apps_server.ready_identity(server, "http://synthetic", token, "new")
    assert events == ["timer", "opening-post", "timer", "cpu-end"]
    assert result["seconds"] == 2.5 and result["population"] == 0


def test_single_schema_audit_does_not_turn_failed_measurement_into_qualification(tmp_path):
    audit = helper("task8-verify-owned-pg-schemas.py")
    record = {
        "version": "task8-measurement-v1",
        "purpose": "interior",
        "campaign": "0123456789ab",
        "ordinal": 1,
        "schema": "latet_t_keyfuzz_task8interior_0123456789ab_1",
        "count": 1000,
        "index": "on",
        "source_root": str(ROOT),
        "pins": {"source": "a" * 64},
        "output": str(tmp_path / "result.json"),
        "outcome": "failed",
        "phase": "cleanup_verified",
        "cleanup_verified": True,
        "errors": ["ValueError"],
    }
    with pytest.raises(AssertionError, match="did not complete"):
        audit.exact_schemas(record, tmp_path / "manifest.json")
    schemas, completed = audit.exact_schemas(record, tmp_path / "manifest.json", allow_incomplete=True)
    assert schemas == [record["schema"]] and completed is False


@pytest.mark.parametrize("arm", ["500+1", "1-alone"])
@pytest.mark.parametrize("drift", [False, True])
def test_real_arm_helper_primes_both_arms_identically_and_refuses_fixture_drift(tmp_path, monkeypatch, arm, drift):
    texts = [f"text-{i}" for i in range(501)]
    answers = [f"masked-{i}" for i in range(501)]
    ordinals = {text: i for i, text in enumerate(texts)}
    cache, posts = set(), []
    engine = SimpleNamespace(vault=object())
    engine.cached = lambda text, *args, **kwargs: text in cache
    engine.rules_changed = cache.clear  # the blocks masked before no longer match
    engine.masker = SimpleNamespace(joined=lambda texts, *args, **kwargs: contextlib.nullcontext())
    state_now = {"counter": 5, "population": 1000000}
    space = SimpleNamespace(runtime=SimpleNamespace(built=SimpleNamespace(engine=engine)))

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, path, *, json):
            sent = json["texts"]
            posts.append((path, list(sent)))
            if path.endswith("/mask"):
                unread = [text for text in sent if not engine.cached(text, "data", "session")]
                with engine.masker.joined(unread, "data"):
                    cache.update(sent)
                result = {"results": [{"text": answers[ordinals[text]], "replacements": [{}]} for text in sent]}
                if drift and len(posts) == 2:
                    state_now["counter"] += 1
            else:
                result = {"results": [{"text": texts[answers.index(text)], "restored": 1} for text in sent]}
            return SimpleNamespace(status_code=200, json=lambda: result)

    monkeypatch.setattr(bench_apps_server.httpx, "Client", Client)
    monkeypatch.setattr(bench_apps, "lines", lambda *args: texts)
    monkeypatch.setattr(bench_apps_server, "_fixture_source", lambda: {})
    monkeypatch.setattr(bench_apps_server, "_fixture_state", lambda _: dict(state_now))
    monkeypatch.setattr(bench_apps_server, "_known_cleartext_check", lambda *args: 1)
    monkeypatch.setattr(bench_engine, "Load", lambda: SimpleNamespace(sample=lambda: {"status": "ok", "percent": 25}))
    monkeypatch.setattr(bench_apps_server, "CACHED_WINDOW_SECONDS", 0.0)  # two of the one text, as fast as they go
    token = tmp_path / "token"
    token.write_text("synthetic")
    state = SimpleNamespace(seed=1, key="key", schema="owned", token=token)
    fixture = {
        "version": "task8-cached-v1",
        "schema": "owned",
        "identity": "key",
        "seed": 1,
        "working": 5000,
        "held": bench_apps_server.KEY_HELD_SECRETS,
        "kind": "data",
        "texts_sha256": bench_apps_server._digest(texts),
        "state": dict(state_now),
        "source": {},
        "answer_hashes": [bench_apps_server._digest(answer) for answer in answers],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    output = tmp_path / "arm.json"
    args = SimpleNamespace(
        new_key=False,
        working=5000,
        held=bench_apps_server.KEY_HELD_SECRETS,
        wait_index=True,
        cached_output=output,
        cached_fixture=fixture_path,
        cached_pair=0,
        cached_arm=arm,
    )
    server = SimpleNamespace(spaces=SimpleNamespace(_spaces={"key": space}))
    if drift:
        with pytest.raises(RuntimeError, match="priming changed"):
            bench_apps_server.cached_arm(args, server, "http://synthetic", state)
        assert len(posts) == 2 and json.loads(output.read_text())["qualified"] is False
    else:
        bench_apps_server.cached_arm(args, server, "http://synthetic", state)
        repeats = 1 if arm == "500+1" else 2
        assert [sent for _, sent in posts[2 : 2 + repeats]] == [texts if arm == "500+1" else texts[-1:]] * repeats
        result = json.loads(output.read_text())
        assert result["qualified"] is True and result["proof"]["joined"] == [[500]] * repeats
        assert len(result["requests"]) == repeats and len(posts) == 2 + repeats + 1
        assert ("window" in result) is (arm == "1-alone")
    assert posts[0][1] == posts[1][1] == texts[:500]


def test_memory_index_on_rejected_before_fixture_population(tmp_path, monkeypatch):
    interior = helper("task8-interior-measure.py")
    monkeypatch.setattr(interior, "lines", lambda *args, **kwargs: pytest.fail("invalid mode constructed corpus"))
    with pytest.raises(SystemExit):
        interior.main([str(ROOT), "invalid", "memory", "--index", "on", "--output", str(tmp_path / "out.json")])


def test_store_close_failure_does_not_hide_the_primary_measurement_failure():
    def fail_close():
        raise OSError("close")

    stores = SimpleNamespace(new=lambda: SimpleNamespace(close=fail_close))
    with pytest.raises(BaseExceptionGroup) as caught, bench_engine.measurement_store(stores):
        raise ValueError("primary")
    assert [type(error) for error in caught.value.exceptions] == [ValueError, OSError]


def test_failed_native_memory_api_is_unavailable_not_zero(monkeypatch):
    def failed(*args):
        return 0

    monkeypatch.setattr(terms_bench.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(GetCurrentProcess=lambda: 1), psapi=SimpleNamespace(GetProcessMemoryInfo=failed)
        ),
        raising=False,
    )
    assert terms_bench.memory_counters()["status"] == "unavailable"
    with pytest.raises(RuntimeError, match="unavailable"):
        terms_bench.memory_mb()


def test_production_rebuild_uses_real_store_build_with_old_index_retained_three_times(monkeypatch, capsys):
    from latet.engine import stored

    events = []
    store = SimpleNamespace(
        index=SimpleNamespace(ready=True, nbytes=80, watermark=7),
        _cache=SimpleNamespace(nbytes=20),
        index_bytes=100,
        _building_index=None,
    )
    old_references = []

    def build():
        events.append("build")
        old_references.append(store.index)
        store.index = SimpleNamespace(ready=True, nbytes=88, watermark=7)
        return True

    store.build_index = build

    @contextlib.contextmanager
    def bench_store(*args, **kwargs):
        assert kwargs["index"] is True
        yield SimpleNamespace(seed=1), object(), store

    class Memory:
        def __init__(self, provider):
            assert provider() is store

        def __enter__(self):
            events.append("memory-start")
            return self

        def __exit__(self, *args):
            events.append("memory-end")

        def report(self):
            return {"qualified": True, "native_after": {"lifetime_peak_pagefile_bytes": 700}}

    monkeypatch.setattr(bench_engine, "_bench_store", bench_store)
    monkeypatch.setattr(bench_engine, "MemoryObservation", Memory)
    monkeypatch.setattr(bench_engine, "Load", lambda: SimpleNamespace(sample=lambda: {"status": "ok"}))
    monkeypatch.setattr(bench_apps, "lines", lambda count, *args: ["same"] * count)
    monkeypatch.setattr(stored, "StoredVault", lambda *args, **kwargs: object())
    monkeypatch.setattr(bench_engine, "read_call", lambda vault, lines, scoped: events.append(("warm", len(lines))))
    assert bench_engine.production_rebuild(Path("unused")) == 0
    assert events == [("warm", 500), "memory-start", "build", "memory-end"] * 3
    assert len({id(index) for index in old_references}) == 3
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 3 and all(report["measurement"] == "production-store-rebuild" for report in reports)
    assert reports[0]["before"]["published_array_bytes"] == 80
    assert all(report["published_array_bytes"] == 88 and report["qualified"] for report in reports)


def test_a_serve_with_the_index_off_gives_every_key_store_no_index_and_waits_for_none(monkeypatch):
    """Task 8 fix round 1: the SQL lookup on the same source, to set the index's share of a serve apart."""
    from latet.server import backends

    made = []
    monkeypatch.setattr(backends, "PgVaultStore", lambda *args, **kwargs: made.append(kwargs) or kwargs)
    with bench_apps_server.indexing(False):
        backends.PgVaultStore("db", "identity", b"key", index=True, cache=5000)
    with bench_apps_server.indexing(True):
        backends.PgVaultStore("db", "identity", b"key", index=True, cache=5000)
    assert [kwargs["index"] for kwargs in made] == [False, True]
    args = bench_apps_server.parser().parse_args(["serve", "--index", "off", "--no-wait-index"])
    assert args.index == "off" and not args.wait_index
    assert bench_apps_server.parser().parse_args(["serve"]).index == "on"
    with pytest.raises(SystemExit, match="--index off"):
        bench_apps_server.serve(bench_apps_server.parser().parse_args(["serve", "--index", "off"]), [])


def test_interior_oracle_is_covered_characters_merged_spans_and_the_maskers_output():
    """Task 8 fix round 1 (the controller's ruling): exact span tuples say which detector part found what, which a
    stored vault arranges otherwise by design; what qualifies is what is masked and given back."""
    interior = helper("task8-interior-measure.py")
    from latet.engine.types import EntityClass, Span

    text = "user=ada@x.example key=tok_123456"
    email = Span(5, 18, "EMAIL", EntityClass.PII, "known-values")
    domain = Span(9, 18, "DOMAIN", EntityClass.PII, "known-values")
    token = Span(23, 33, "API_KEY", EntityClass.SECRET, "known-values")
    plain = [([email, token], [])]
    duplicated = [([token, token, email], [])]  # a held secret read by the parts and by the text's own values
    cross_part = [([domain, token, email], [])]  # an overlap one vault cuts and the other leaves to the merge
    shorter = [([Span(5, 17, "EMAIL", EntityClass.PII, "known-values"), token], [])]
    renamed = [([Span(5, 18, "DOMAIN", EntityClass.PII, "known-values"), token], [])]
    assert interior.span_digest(plain) != interior.span_digest(duplicated)  # the old oracle refused both
    for same in (duplicated, cross_part):
        assert interior.covered_differences([text], plain, same) == (0, 0)
        assert interior.merged_digest([text], same) == interior.merged_digest([text], plain)
    assert interior.covered_differences([text], plain, shorter) == (1, 0)
    assert interior.merged_digest([text], renamed) != interior.merged_digest([text], plain)
    masked = SimpleNamespace(text="user=<a> key=<b>")
    other = SimpleNamespace(text="user=<c> key=<b>")
    summary = interior.masker_comparison([text, text], [masked, masked], [text, text], [masked, other], [text, "x"])
    assert summary == {
        "texts": 2,
        "masked_differ": [1],
        "plain_restore_exact": 2,
        "stored_restore_exact": 1,
        "qualified": False,
    }
    assert interior.masker_comparison([text], [masked], [text], [masked], [text])["qualified"]
