"""The parallel gate must account for every case and preserve failures."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "test_parallel.py"
    assert path.is_file(), "the bounded parallel runner is not implemented yet"
    spec = importlib.util.spec_from_file_location("test_parallel_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parameterized_quiet_selector_leaves_other_cases_of_the_module_together(runner):
    nodes = [
        "tests/test_a.py::test_fast[1]",
        "tests/test_a.py::test_fast[2]",
        "tests/test_a.py::test_budget[x]",
        "tests/test_a.py::test_budget[y]",
        "tests/test_b.py::test_one",
        "tests/test_c.py::test_two",
    ]
    plan = runner.make_plan(nodes, ["tests/test_a.py::test_budget"], workers=2)
    assert plan[-1]["label"] == "quiet"
    assert plan[-1]["nodeids"] == [
        "tests/test_a.py::test_budget[x]",
        "tests/test_a.py::test_budget[y]",
    ]
    ordinary = [entry["nodeids"] for entry in plan[:-1]]
    assert any(set(nodes[:2]).issubset(lane) for lane in ordinary)
    flattened = [node for entry in plan for node in entry["nodeids"]]
    assert sorted(flattened) == sorted(nodes)
    assert len(flattened) == len(set(flattened))


def test_recorded_durations_balance_whole_ordinary_modules(runner):
    nodes = [f"tests/test_{name}.py::test_case" for name in "abcd"]
    plan = runner.make_plan(
        nodes,
        [],
        workers=2,
        durations={"tests/test_a.py": 9, "tests/test_b.py": 8, "tests/test_c.py": 2, "tests/test_d.py": 1},
    )
    assert {frozenset(entry["nodeids"]) for entry in plan} == {
        frozenset((nodes[0], nodes[3])),
        frozenset((nodes[1], nodes[2])),
    }


def test_unknown_quiet_selector_is_rejected_instead_of_silently_running_under_load(runner):
    with pytest.raises(ValueError, match="quiet"):
        runner.make_plan(["tests/test_a.py::test_case"], ["tests/test_a.py::test_typo"], workers=2)


def _collection():
    return {
        "run_id": "contract-run",
        "nodeids": ["tests/test_a.py::test_ok[1]", "tests/test_a.py::test_setup_skip"],
        "collection_skips": [{"nodeid": "tests/test_unavailable.py", "reason": "optional tool absent"}],
        "collection_errors": [],
        "deselected": [],
    }


def _plan():
    return [
        {
            "label": "worker-1",
            "nodeids": _collection()["nodeids"],
            "modules": ["tests/test_a.py", "tests/test_unavailable.py"],
        }
    ]


def _report():
    return {
        **_collection(),
        "label": "worker-1",
        "selected": _collection()["nodeids"],
        "exitstatus": 0,
        "process_exit": 0,
        "active_processes": 0,
        "phases": [
            {"nodeid": "tests/test_a.py::test_ok[1]", "when": "setup", "outcome": "passed", "duration": 0.1},
            {"nodeid": "tests/test_a.py::test_ok[1]", "when": "call", "outcome": "passed", "duration": 0.2},
            {"nodeid": "tests/test_a.py::test_ok[1]", "when": "teardown", "outcome": "passed", "duration": 0.1},
            {"nodeid": "tests/test_a.py::test_setup_skip", "when": "setup", "outcome": "skipped", "duration": 0.1},
            {"nodeid": "tests/test_a.py::test_setup_skip", "when": "teardown", "outcome": "passed", "duration": 0.1},
        ],
    }


def test_setup_skip_is_terminal_and_collection_skip_remains_attributable(runner):
    result = runner.reconcile(_collection(), _plan(), [_report()])
    assert result["counts"] == {"passed": 1, "skipped": 1}
    assert result["collection_skips"] == _collection()["collection_skips"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unexpected", "collection_drift"])
def test_changed_or_incomplete_inventory_fails_closed(runner, mutation):
    report = _report()
    if mutation == "missing":
        report["phases"] = report["phases"][:3]
    elif mutation == "duplicate":
        report["phases"].append(dict(report["phases"][1]))
    elif mutation == "unexpected":
        report["selected"].append("tests/test_a.py::test_unexpected")
    else:
        report["nodeids"] = report["nodeids"][:1]
    with pytest.raises(ValueError, match=r"incomplete|duplicate|inventory"):
        runner.reconcile(_collection(), _plan(), [report])


def test_missing_child_report_fails_closed(runner):
    with pytest.raises(ValueError, match=r"missing.*report"):
        runner.reconcile(_collection(), _plan(), [])


@pytest.mark.parametrize(("field", "value"), [("process_exit", 1), ("exitstatus", 1), ("active_processes", 1)])
def test_child_failure_or_surviving_descendant_cannot_be_reported_successful(runner, field, value):
    report = _report()
    report[field] = value
    with pytest.raises(ValueError, match=r"child failed|descendants"):
        runner.reconcile(_collection(), _plan(), [report])


def test_a_teardown_failure_cannot_be_hidden_by_a_passing_call(runner):
    report = _report()
    report["phases"][2]["outcome"] = "failed"
    with pytest.raises(ValueError, match="failed test phase"):
        runner.reconcile(_collection(), _plan(), [report])


def test_a_changed_collection_skip_fails_closed(runner):
    report = _report()
    report["collection_skips"] = []
    with pytest.raises(ValueError, match="collection skip"):
        runner.reconcile(_collection(), _plan(), [report])


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_partial_phase_reports_cannot_turn_an_interrupted_case_into_a_pass(runner, phase):
    report = _report()
    report["phases"] = [item for item in report["phases"] if item["when"] != phase]
    with pytest.raises(ValueError, match="incomplete"):
        runner.reconcile(_collection(), _plan(), [report])


def test_foreign_run_report_cannot_satisfy_this_run(runner):
    report = _report()
    report["run_id"] = "another-run"
    with pytest.raises(ValueError, match="foreign"):
        runner.reconcile(_collection(), _plan(), [report])


def test_collection_error_prevents_accepting_any_worker_results(runner):
    initial = _collection()
    initial["collection_errors"] = [{"nodeid": "tests/test_broken.py", "reason": "import failed"}]
    with pytest.raises(ValueError, match="baseline collection"):
        runner.reconcile(initial, _plan(), [_report()])


@pytest.fixture
def synthetic_suite(tmp_path):
    """A real pytest checkout with no application imports or external services."""
    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    namespace = tmp_path / "owned"
    (namespace / "homes").mkdir(parents=True)
    (namespace / "temp").mkdir()
    marker = namespace / ".latet-e0c-owned"
    marker.write_bytes(b"parallel-runner-contract-marker!")
    metadata = namespace.stat()
    record = tmp_path / "namespace.json"
    record.write_text(
        json.dumps(
            {
                "root": str(namespace),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"environment": {"LATET_TEST_REQUIRE_SERVICES": "1"}}), encoding="utf-8")
    output = tmp_path / "reports"
    quiet_file = tmp_path / "quiet.txt"
    quiet_file.write_text("", encoding="utf-8")

    def write(name, source):
        (root / "tests" / name).write_text(textwrap.dedent(source), encoding="utf-8")

    def run(*arguments, timeout=40):
        script = Path(__file__).resolve().parents[2] / "scripts" / "test_parallel.py"
        assert script.is_file(), "the bounded parallel runner is not implemented yet"
        environment = os.environ.copy()
        environment.update(PYTEST_ADDOPTS="--invalid-inherited-option", LATET_CONNECTION="inherited-private-connection")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(script),
                "--root",
                str(root),
                "--namespace-record",
                str(record),
                "--config",
                str(config),
                "--output",
                str(output),
                "--quiet-file",
                str(quiet_file),
                "--workers",
                "2",
                "--seconds",
                "30",
                "--child-seconds",
                "15",
                *arguments,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        summary = output / "summary.json"
        return result, json.loads(summary.read_text(encoding="utf-8")) if summary.is_file() else None

    return root, output, write, run


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
def test_real_workers_overlap_use_private_homes_and_finish_before_quiet_cases(synthetic_suite):
    root, _, write, run = synthetic_suite
    source = """
        import json
        import os
        from pathlib import Path
        import time

        def record(name):
            start = time.monotonic()
            Path("events").mkdir(exist_ok=True)
            if name.startswith("ordinary-"):
                Path("events", name + ".ready").write_text("ready")
                deadline = time.monotonic() + 5
                while len(list(Path("events").glob("*.ready"))) != 2:
                    assert time.monotonic() < deadline, "ordinary workers did not overlap"
                    time.sleep(0.02)
            homes = {key: os.environ[key] for key in (
                "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
                "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA",
            )}
            assert "LATET_CONNECTION" not in os.environ
            assert "PYTEST_ADDOPTS" not in os.environ
            assert os.environ["LATET_TEST_REQUIRE_SERVICES"] == "1"
            time.sleep(0.4)
            Path("events", name + ".json").write_text(json.dumps({
                "start": start, "end": time.monotonic(), "homes": homes,
                "temp": os.environ["TEMP"], "pid": os.getpid(),
            }))

        def test_ordinary():
            record("NAME")
    """
    write(
        "test_a.py",
        textwrap.dedent(source.replace("NAME", "ordinary-a"))
        + """

def test_quiet():
    record("quiet")
""",
    )
    write("test_b.py", source.replace("NAME", "ordinary-b"))
    result, summary = run("--quiet-selector", "tests/test_a.py::test_quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["counts"] == {"passed": 3}
    events = {path.stem: json.loads(path.read_text()) for path in (root / "events").glob("*.json")}
    first, second, quiet = (events[name] for name in ("ordinary-a", "ordinary-b", "quiet"))
    assert max(first["start"], second["start"]) < min(first["end"], second["end"])
    assert quiet["start"] >= max(first["end"], second["end"])
    assert first["pid"] != second["pid"]
    assert set(first["homes"].values()).isdisjoint(second["homes"].values())
    assert first["temp"] != second["temp"]
    assert all(lane["active_processes"] == 0 for lane in summary["children"])


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
def test_real_setup_and_collection_skips_are_reconciled(synthetic_suite):
    _, _, write, run = synthetic_suite
    write(
        "test_cases.py",
        """
        import pytest

        @pytest.mark.parametrize("value", [1, 2])
        def test_ok(value):
            assert value > 0

        @pytest.fixture
        def absent():
            pytest.skip("fixture unavailable")

        def test_setup_skip(absent):
            raise AssertionError("must not execute")
    """,
    )
    write(
        "test_unavailable.py",
        """
        import pytest
        pytest.skip("optional tool absent", allow_module_level=True)
    """,
    )
    result, summary = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["counts"] == {"passed": 2, "skipped": 1}
    assert [skip["nodeid"] for skip in summary["collection_skips"]] == ["tests/test_unavailable.py"]


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
@pytest.mark.parametrize("body", ["assert False", "import os; os._exit(0)"])
def test_real_failure_or_missing_report_retains_logs_and_cannot_pass(synthetic_suite, body):
    root, output, write, run = synthetic_suite
    write(
        "test_failure.py",
        'from pathlib import Path\ndef test_case():\n    Path("entered").write_text("yes")\n    ' + body + "\n",
    )
    result, summary = run()
    assert result.returncode != 0
    assert summary is not None and summary["success"] is False
    assert (root / "entered").read_text() == "yes"
    assert summary["children"][0]["exitstatus"] == 0
    assert (output / "worker-1.log").is_file()


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
def test_external_collection_selector_is_rejected_before_test_execution(synthetic_suite):
    root, _, write, run = synthetic_suite
    write(
        "test_cases.py",
        """
        def test_one():
            raise AssertionError("must not execute")

        def test_two():
            raise AssertionError("must not execute")
    """,
    )
    (root / "tests" / "conftest.py").write_text(
        textwrap.dedent("""
        def pytest_collection_modifyitems(config, items):
            removed = items.pop()
            config.hook.pytest_deselected(items=[removed])
    """),
        encoding="utf-8",
    )
    result, summary = run()
    assert result.returncode != 0
    assert "deselected" in summary["error"]
    assert len(summary["children"]) == 1
    assert len(summary["children"][0]["nodeids"]) == 2
    assert len(summary["children"][0]["deselected"]) == 1


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
def test_timeout_reaps_owned_grandchild_and_leaves_unrelated_process_alive(synthetic_suite):
    import ctypes

    root, _, write, run = synthetic_suite
    write(
        "test_timeout.py",
        """
        from pathlib import Path
        import subprocess
        import sys
        import time

        def test_case():
            child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
            Path("grandchild.pid").write_text(str(child.pid))
            time.sleep(60)
    """,
    )
    unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        result, summary = run("--child-seconds", "3")
        assert result.returncode != 0
        assert summary is not None and summary["success"] is False
        assert unrelated.poll() is None
        pid = int((root / "grandchild.pid").read_text())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, 0, pid)
        if handle:
            code = ctypes.c_uint32()
            try:
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 259, "owned grandchild survived the timeout"
            finally:
                kernel.CloseHandle(handle)
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
def test_nested_conftest_fixture_survives_interleaved_module_assignment(synthetic_suite):
    root, _, write, run = synthetic_suite
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    nested = root / "tests" / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef space():\n    return 41\n", encoding="utf-8"
    )
    write(
        "nested/test_a.py",
        """
        import pytest
        @pytest.mark.parametrize("value", [1, 2, 3])
        def test_first(space, value):
            assert space == 41 and value > 0
    """,
    )
    write(
        "test_middle.py",
        """
        import pytest
        @pytest.mark.parametrize("value", [1, 2])
        def test_middle(value):
            assert value > 0
    """,
    )
    write(
        "nested/test_z.py",
        """
        def test_last(space):
            assert space == 41
    """,
    )
    result, summary = run("--workers", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["counts"] == {"passed": 6}


_PRIMARY_DATABASE = "postgresql://latet:test@127.0.0.1:15432/fixture"
_TLS_DATABASE = "postgresql://latet:test@127.0.0.1:15433/fixture?sslmode=verify-full&sslrootcert=fake-ca"


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
@pytest.mark.parametrize("quiet_tls", [False, True], ids=["default-database", "quiet-tls"])
def test_real_database_environment_routes_only_the_quiet_child(synthetic_suite, quiet_tls):
    root, _, write, run = synthetic_suite
    config: dict[str, object] = {
        "environment": {
            "LATET_TEST_DATABASE_URL": _PRIMARY_DATABASE,
            "LATET_TEST_TLS_DATABASE_URL": _TLS_DATABASE,
        }
    }
    if quiet_tls:
        config["quiet_database"] = "tls"
    (root.parent / "config.json").write_text(json.dumps(config), encoding="utf-8")
    write(
        "test_lanes.py",
        """
        import json
        import os
        from pathlib import Path

        home = Path(os.environ["LATET_TEST_HOME_ROOT"]).name
        lane = "collection" if home.endswith("-collection") else "quiet" if home.endswith("-quiet") else "ordinary"
        Path("database-" + lane + ".json").write_text(json.dumps({
            "primary": os.environ["LATET_TEST_DATABASE_URL"],
            "tls": os.environ["LATET_TEST_TLS_DATABASE_URL"],
        }))

        def test_ordinary():
            pass

        def test_quiet():
            pass
    """,
    )
    result, summary = run("--quiet-selector", "tests/test_lanes.py::test_quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["counts"] == {"passed": 2}
    observed = {
        lane: json.loads((root / ("database-" + lane + ".json")).read_text())
        for lane in ("collection", "ordinary", "quiet")
    }
    assert observed["collection"]["primary"] == _PRIMARY_DATABASE
    assert observed["ordinary"]["primary"] == _PRIMARY_DATABASE
    assert observed["quiet"]["primary"] == (_TLS_DATABASE if quiet_tls else _PRIMARY_DATABASE)
    assert {value["tls"] for value in observed.values()} == {_TLS_DATABASE}


@pytest.mark.skipif(os.name != "nt", reason="the bounded runner uses Windows Job Objects")
@pytest.mark.parametrize(
    ("option", "tls_url"),
    [
        ("other", _TLS_DATABASE),
        ("", _TLS_DATABASE),
        (None, _TLS_DATABASE),
        ("tls", None),
        ("tls", ""),
        ("tls", 17),
    ],
    ids=["unknown-option", "empty-option", "nonstring-option", "missing-tls", "empty-tls", "nonstring-tls"],
)
def test_invalid_quiet_database_configuration_stops_before_any_child(synthetic_suite, option, tls_url):
    root, output, write, run = synthetic_suite
    environment = {"LATET_TEST_DATABASE_URL": _PRIMARY_DATABASE}
    if tls_url is not None:
        environment["LATET_TEST_TLS_DATABASE_URL"] = tls_url
    (root.parent / "config.json").write_text(
        json.dumps({"environment": environment, "quiet_database": option}), encoding="utf-8"
    )
    write(
        "test_never.py",
        """
        from pathlib import Path
        Path("imported").write_text("collection reached application/test code")

        def test_never():
            raise AssertionError("must not run")
    """,
    )
    result, summary = run()
    assert result.returncode != 0
    assert summary is not None and "quiet_database" in summary["error"]
    assert summary["children"] == []
    assert not list(output.glob("*.spec.json"))
    assert not (root / "imported").exists()
