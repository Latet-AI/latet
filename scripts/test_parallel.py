"""Run a complete pytest suite in bounded Windows processes, with an exclusive quiet lane.

The caller supplies an owned namespace, explicit service/model config and exclusive campaign slot.
No services are started or reset. See test_parallel.md for invocation and report semantics.
"""

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path


def module_of(node):
    return node.split("::", 1)[0]


def interpreter_identity():
    base_executable = getattr(sys, "_base_executable", None)
    if not isinstance(base_executable, str) or not Path(base_executable).is_file():
        raise ValueError("a valid CPython base executable is required")
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "base_executable": base_executable,
    }


def matches(node, selector):
    return node == selector or node.startswith(selector + "::") or node.startswith(selector + "[")


def make_plan(nodeids, quiet_selectors, workers, durations=None, collection_modules=()):
    if workers < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("positive worker count and unique inventory required")
    for selector in quiet_selectors:
        if not any(matches(node, selector) for node in [*nodeids, *collection_modules]):
            raise ValueError(f"unknown quiet selector: {selector}")
    quiet = [node for node in nodeids if any(matches(node, selector) for selector in quiet_selectors)]
    quiet_set = set(quiet)
    ordinary = defaultdict(list)
    for node in nodeids:
        if node not in quiet_set:
            ordinary[module_of(node)].append(node)
    durations = durations or {}
    lanes = [{"label": f"worker-{i + 1}", "nodeids": [], "modules": []} for i in range(workers)]
    loads = [0.0] * workers
    for module in sorted(ordinary, key=lambda name: (-durations.get(name, len(ordinary[name])), name)):
        index = min(range(workers), key=lambda i: (loads[i], i))
        lanes[index]["nodeids"].extend(ordinary[module])
        lanes[index]["modules"].append(module)
        loads[index] += durations.get(module, len(ordinary[module]))
    lanes = [lane for lane in lanes if lane["nodeids"]]
    if quiet:
        lanes.append({"label": "quiet", "nodeids": quiet, "modules": sorted({module_of(n) for n in quiet})})
    for module in sorted(set(collection_modules)):
        if any(module in lane["modules"] for lane in lanes):
            continue
        quiet_module = any(matches(module, selector) for selector in quiet_selectors)
        candidates = [lane for lane in lanes if (lane["label"] == "quiet") == quiet_module]
        if not candidates:
            lane = {"label": "quiet" if quiet_module else "worker-1", "nodeids": [], "modules": []}
            lanes.insert(len(lanes) if quiet_module else 0, lane)
            candidates = [lane]
        candidates[0]["modules"].append(module)
    return lanes


def _skip_counter(skips):
    return Counter((item["nodeid"], item["reason"]) for item in skips)


def reconcile(initial, plan, reports, *, complete=True):
    inventory = initial["nodeids"]
    if initial["collection_errors"] or len(inventory) != len(set(inventory)):
        raise ValueError("baseline collection failed or duplicated cases")
    planned = Counter(node for lane in plan for node in lane["nodeids"])
    if (complete and planned != Counter(inventory)) or planned - Counter(inventory):
        raise ValueError("planned inventory differs from complete collection")
    if Counter(report["label"] for report in reports) != Counter(lane["label"] for lane in plan):
        raise ValueError("missing or duplicate child report")
    counts = Counter()
    durations = defaultdict(float)
    for lane in plan:
        report = next(report for report in reports if report["label"] == lane["label"])
        if report["run_id"] != initial["run_id"]:
            raise ValueError("foreign run report")
        allowed_exit = 5 if not lane["nodeids"] and report["collection_skips"] else 0
        if report["process_exit"] != allowed_exit or report["exitstatus"] != allowed_exit:
            raise ValueError(f"child failed: {lane['label']}")
        if report["active_processes"] != 0 or report["collection_errors"]:
            raise ValueError("child collection failed or descendants survived")
        expected = inventory
        if Counter(report["nodeids"]) != Counter(expected) or Counter(report["selected"]) != Counter(lane["nodeids"]):
            raise ValueError("child inventory drifted, duplicated, omitted or deselected cases")
        if Counter(report["deselected"]) != Counter(expected) - Counter(lane["nodeids"]):
            raise ValueError("unexpected child deselection")
        skips = initial["collection_skips"]
        if _skip_counter(report["collection_skips"]) != _skip_counter(skips):
            raise ValueError("collection skip drift")
        phases = defaultdict(dict)
        for phase in report["phases"]:
            node, when = phase["nodeid"], phase["when"]
            if node not in lane["nodeids"] or when not in {"setup", "call", "teardown"} or when in phases[node]:
                raise ValueError("unexpected or duplicate phase report")
            phases[node][when] = phase
            durations[module_of(node)] += phase["duration"]
        for node in lane["nodeids"]:
            parts = phases[node]
            if "setup" not in parts or "teardown" not in parts:
                raise ValueError("incomplete setup/teardown report")
            if any(part["outcome"] == "failed" for part in parts.values()):
                raise ValueError("failed test phase")
            setup = parts["setup"]
            required = {"setup", "call", "teardown"} if setup["outcome"] == "passed" else {"setup", "teardown"}
            if set(parts) != required or setup["outcome"] not in {"passed", "skipped"}:
                raise ValueError("incomplete or invalid test outcome")
            terminal = parts.get("call", setup)
            outcome = terminal["outcome"]
            if outcome not in {"passed", "skipped"} or parts["teardown"]["outcome"] != "passed":
                raise ValueError("invalid terminal outcome")
            if terminal.get("wasxfail"):
                outcome = "xfailed" if outcome == "skipped" else "xpassed"
            counts[outcome] += 1
    return {
        "counts": dict(counts),
        "collection_skips": initial["collection_skips"],
        "module_durations": dict(sorted(durations.items(), key=lambda pair: -pair[1])),
    }


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int64),
        ("job", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int64),
        ("kernel", ctypes.c_int64),
        ("period_user", ctypes.c_int64),
        ("period_kernel", ctypes.c_int64),
        ("faults", ctypes.c_uint32),
        ("total", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
        ("terminated", ctypes.c_uint32),
    ]


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.OpenJobObjectW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class Job:
    def __init__(self, aggregate=False):
        self.kernel = kernel_api()
        self.name = "Local\\latet-parallel-" + uuid.uuid4().hex
        self.handle = checked(self.kernel.CreateJobObjectW(None, self.name))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        if aggregate:
            limits.basic.flags |= 0x100 | 0x200
            limits.process_memory = 4 * 1024**3
            limits.job_memory = 16 * 1024**3
        try:
            checked(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            checked(self.kernel.CloseHandle(self.handle))
            raise

    def active(self):
        info = Accounting()
        checked(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.active

    def terminate(self):
        checked(self.kernel.TerminateJobObject(self.handle, 124))

    def close(self):
        checked(self.kernel.CloseHandle(self.handle))


def join_jobs(names):
    kernel = kernel_api()
    for name in names:
        handle = checked(kernel.OpenJobObjectW(1, False, name))
        try:
            checked(kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()))
        finally:
            checked(kernel.CloseHandle(handle))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def plain_directory(path):
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise ValueError("owned directory changed or redirected")


def owned_namespace(record_path):
    if record_path.stat().st_size > 4096:
        raise ValueError("namespace record too large")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    namespace = Path(record["root"])
    if not namespace.is_absolute():
        raise ValueError("namespace must be absolute")
    for parent in [namespace, *namespace.parents]:
        plain_directory(parent)
    metadata = namespace.stat()
    if (metadata.st_dev, metadata.st_ino) != (record["device"], record["inode"]):
        raise ValueError("namespace native identity changed")
    marker = namespace / ".latet-e0c-owned"
    if marker.is_symlink() or marker.is_junction() or marker.stat().st_size != 32:
        raise ValueError("namespace ownership marker changed")
    if hashlib.sha256(marker.read_bytes()).hexdigest() != record["marker_sha256"]:
        raise ValueError("namespace ownership marker does not match")
    for child in ("homes", "temp"):
        plain_directory(namespace / child)
    return namespace


def child_environment(namespace, label, explicit):
    homes = namespace / "homes" / label
    temporary = namespace / "temp" / label
    homes.mkdir()
    temporary.mkdir()
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(("LATET_", "PYTEST_")) or name in {
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGREQUIREAUTH",
            "PGHOSTADDR",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONEXECUTABLE",
            "__PYVENV_LAUNCHER__",
        }:
            environment.pop(name)
    allowed = {
        "LATET_TEST_DATABASE_URL",
        "LATET_TEST_VALKEY_URL",
        "LATET_TEST_TLS_DATABASE_URL",
        "LATET_NER_MODEL_DIR",
        "LATET_OCR_DATA_DIR",
        "LATET_TEST_REQUIRE_SERVICES",
    }
    if set(explicit) - allowed:
        raise ValueError("config accepts only explicit test service/model environment")
    environment.update(explicit)
    for name, suffix in {
        "HOME": "",
        "USERPROFILE": "",
        "CLAUDE_CONFIG_DIR": ".claude",
        "CODEX_HOME": ".codex",
        "XDG_CONFIG_HOME": ".config",
        "APPDATA": "roaming",
        "LOCALAPPDATA": "local",
    }.items():
        folder = homes / suffix
        folder.mkdir(exist_ok=True)
        environment[name] = str(folder)
    environment.update(
        {
            "HOMEDRIVE": homes.drive,
            "HOMEPATH": str(homes)[len(homes.drive) :],
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "LATET_TEST_HOME_ROOT": str(homes),
            "LATET_TEST_REQUIRE_SERVICES": "1",
            "LATET_KEYRING": "off",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "UV_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
            "PSModuleAnalysisCachePath": str(homes / "local" / "ModuleAnalysisCache"),
        }
    )
    return environment, temporary


def child_main(spec_path):
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    join_jobs([spec["aggregate_job"], spec["job"]])
    identity = interpreter_identity()
    if identity != spec["python_identity"]:
        raise ValueError("child Python environment identity mismatch")
    write_json(Path(spec["ready"]), {"run_id": spec["run_id"], "pid": os.getpid()})
    root = Path(spec["root"])
    sys.path.insert(0, str(root / "src"))
    if (root / "src" / "latet").is_dir():
        origin = importlib.util.find_spec("latet")
        if origin is None or origin.origin is None or not Path(origin.origin).resolve().is_relative_to(root / "src"):
            raise ValueError("requested checkout source provenance mismatch")
    import pytest

    payload = {
        "run_id": spec["run_id"],
        "label": spec["label"],
        "nodeids": [],
        "selected": [],
        "deselected": [],
        "collection_skips": [],
        "collection_errors": [],
        "phases": [],
        "warnings": [],
        "python_identity": identity,
    }
    selected = set(spec["nodeids"])

    class Recorder:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_collection_modifyitems(self, session, config, items):
            payload["nodeids"] = [item.nodeid for item in items]
            yield
            if not spec["collect_only"]:
                removed = [item for item in items if item.nodeid not in selected]
                items[:] = [item for item in items if item.nodeid in selected]
                config.hook.pytest_deselected(items=removed)

        def pytest_deselected(self, items):
            payload["deselected"].extend(item.nodeid for item in items)

        def pytest_collection_finish(self, session):
            payload["selected"] = [item.nodeid for item in session.items]

        def pytest_collectreport(self, report):
            if report.skipped or report.failed:
                key = "collection_skips" if report.skipped else "collection_errors"
                payload[key].append({"nodeid": report.nodeid, "reason": str(report.longrepr)})

        def pytest_runtest_logreport(self, report):
            payload["phases"].append(
                {
                    "nodeid": report.nodeid,
                    "when": report.when,
                    "outcome": report.outcome,
                    "duration": report.duration,
                    "wasxfail": getattr(report, "wasxfail", None),
                    "reason": str(report.longrepr) if report.skipped or report.failed else None,
                }
            )

        def pytest_warning_recorded(self, warning_message, when, nodeid, location):
            payload["warnings"].append(
                {
                    "nodeid": nodeid,
                    "when": when,
                    "category": warning_message.category.__name__,
                    "message": str(warning_message.message),
                }
            )

    arguments = [
        "-q",
        "-ra",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        f"--rootdir={root}",
        f"--confcutdir={root}",
        "--disable-socket",
        "--allow-hosts=127.0.0.1,::1",
        f"--basetemp={spec['basetemp']}",
    ]
    if spec["collect_only"]:
        arguments.append("--collect-only")
    arguments.append("tests")
    started = time.monotonic()
    status = int(pytest.main(arguments, plugins=[Recorder()]))
    payload.update(
        exitstatus=status,
        elapsed=time.monotonic() - started,
        pid=os.getpid(),
        hypothesis_profile=os.environ.get("HYPOTHESIS_PROFILE", "dev"),
        source_root=str(root / "src"),
    )
    payload["environment"] = {
        name: os.environ.get(name)
        for name in (
            "LATET_TEST_HOME_ROOT",
            "TEMP",
            "UV_OFFLINE",
            "PIP_NO_INDEX",
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "LATET_KEYRING",
            "PYTHON_KEYRING_BACKEND",
            "LATET_TEST_REQUIRE_SERVICES",
        )
    }
    loaded = sys.modules.get("latet")
    if loaded is not None:
        if loaded.__file__ is None:
            raise ValueError("requested checkout source has no file provenance")
        source = Path(loaded.__file__).resolve()
        payload["latet_source"] = str(source)
        if not source.is_relative_to(root / "src"):
            payload["collection_errors"].append({"nodeid": "latet", "reason": "source provenance mismatch"})
            status = payload["exitstatus"] = 1
    write_json(Path(spec["report"]), payload)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--namespace-record", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=1200)
    parser.add_argument("--child-seconds", type=float, default=1200)
    parser.add_argument("--quiet-selector", action="append", default=[])
    parser.add_argument("--quiet-file", type=Path, required=True)
    parser.add_argument("--durations", type=Path)
    options = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Windows Job Objects required; use the existing runner on this platform")
    if options.workers < 1 or any(
        not math.isfinite(bound) or bound <= 0 for bound in (options.seconds, options.child_seconds)
    ):
        parser.error("positive worker count and time bounds required")
    started = time.monotonic()
    deadline = started + options.seconds
    root = options.root.absolute()
    if not (root / "pyproject.toml").is_file() or not (root / "src").is_dir():
        parser.error("root must explicitly identify the requested source checkout")
    output = options.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        "success": False,
        "target_met": False,
        "run_id": uuid.uuid4().hex,
        "root": str(root),
        "workers": options.workers,
        "children": [],
        "limits": {"process_gib": 4, "aggregate_gib": 16},
        "hypothesis_profile": os.environ.get("HYPOTHESIS_PROFILE", "dev"),
    }
    aggregate = None
    children = []

    def launch(lane, collect_only=False):
        if aggregate is None:
            raise RuntimeError("aggregate containment must exist before child launch")
        if time.monotonic() >= deadline:
            raise TimeoutError("whole-run deadline expired before child launch")
        job = Job()
        label = lane["label"]
        record = {
            "label": label,
            "job": job,
            "process": None,
            "log": None,
            "started": time.monotonic(),
            "finished": None,
        }
        children.append(record)
        lane_environment = explicit
        if label == "quiet" and quiet_database == "tls":
            lane_environment = {**explicit, "LATET_TEST_DATABASE_URL": explicit["LATET_TEST_TLS_DATABASE_URL"]}
        environment, temporary = child_environment(namespace, summary["run_id"] + "-" + label, lane_environment)
        if sys.prefix != sys.base_prefix:
            environment["__PYVENV_LAUNCHER__"] = sys.executable
        spec = {
            **lane,
            "root": str(root),
            "run_id": summary["run_id"],
            "collect_only": collect_only,
            "python_identity": interpreter,
            "aggregate_job": aggregate.name,
            "job": job.name,
            "basetemp": str(temporary / "pytest"),
            "report": str(output / (label + ".json")),
            "ready": str(output / (label + ".ready.json")),
        }
        spec_path = output / (label + ".spec.json")
        write_json(spec_path, spec)
        record["spec"] = spec
        record["log"] = (output / (label + ".log")).open("w", encoding="utf-8")
        record["process"] = subprocess.Popen(
            [
                interpreter["base_executable"],
                "-I",
                "-B",
                "-u",
                str(Path(__file__).resolve()),
                "--child",
                str(spec_path),
            ],
            cwd=root,
            env=environment,
            stdout=record["log"],
            stderr=subprocess.STDOUT,
        )
        return record

    def finish(records):
        pending = list(records)
        while pending:
            now = time.monotonic()
            for record in pending[:]:
                process = record["process"]
                if now >= deadline or now - record["started"] >= options.child_seconds:
                    raise TimeoutError(f"deadline exceeded: {record['label']}")
                if process.poll() is None:
                    continue
                process.wait()
                record["finished"] = time.monotonic()
                if record["job"].active() != 0:
                    raise ValueError(f"descendant survived pytest exit: {record['label']}")
                report = json.loads(Path(record["spec"]["report"]).read_text(encoding="utf-8"))
                ready = json.loads(Path(record["spec"]["ready"]).read_text(encoding="utf-8"))
                if ready != {"run_id": summary["run_id"], "pid": report["pid"]}:
                    raise ValueError("child containment gate identity mismatch")
                report.update(
                    process_exit=process.returncode,
                    launcher_pid=process.pid,
                    active_processes=0,
                    started=record["started"] - started,
                    finished=record["finished"] - started,
                )
                record["report"] = report
                summary["children"].append(report)
                pending.remove(record)
            if pending:
                time.sleep(min(0.03, max(0, deadline - time.monotonic())))
        return [record["report"] for record in records]

    try:
        interpreter = interpreter_identity()
        namespace = owned_namespace(options.namespace_record)
        configuration = json.loads(options.config.read_text(encoding="utf-8"))
        explicit = configuration["environment"]
        quiet_database = configuration.get("quiet_database")
        if "quiet_database" in configuration:
            if quiet_database != "tls":
                raise ValueError("quiet_database must be tls when supplied")
            tls_url = explicit.get("LATET_TEST_TLS_DATABASE_URL")
            if not isinstance(tls_url, str) or not tls_url.strip():
                raise ValueError("quiet_database tls requires a nonempty LATET_TEST_TLS_DATABASE_URL")
        aggregate = Job(aggregate=True)
        initial = finish([launch({"label": "collection", "nodeids": [], "modules": ["tests"]}, True)])[0]
        if initial["exitstatus"] != 0 or initial["process_exit"] != 0 or initial["collection_errors"]:
            raise ValueError("complete initial collection failed")
        if not initial["nodeids"] or initial["nodeids"] != initial["selected"] or initial["deselected"]:
            raise ValueError("initial full collection is empty or deselected cases")
        selectors = list(options.quiet_selector)
        if options.quiet_file:
            selectors.extend(
                line.strip()
                for line in options.quiet_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        durations = json.loads(options.durations.read_text(encoding="utf-8")) if options.durations else None
        if durations and "module_durations" in durations:
            durations = durations["module_durations"]
        plan = make_plan(
            initial["nodeids"],
            selectors,
            options.workers,
            durations,
            [module_of(item["nodeid"]) for item in initial["collection_skips"]],
        )
        write_json(output / "plan.json", plan)
        ordinary = [launch(lane) for lane in plan if lane["label"] != "quiet"]
        reports = finish(ordinary)
        reconcile(initial, [lane for lane in plan if lane["label"] != "quiet"], reports, complete=False)
        if aggregate.active() != 0:
            raise ValueError("ordinary descendants remain before quiet phase")
        quiet = [launch(lane) for lane in plan if lane["label"] == "quiet"]
        reports.extend(finish(quiet))
        summary.update(reconcile(initial, plan, reports))
        summary["success"] = True
    except (Exception, KeyboardInterrupt) as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup_errors = []
        for record in children:
            try:
                if record["job"].active():
                    record["job"].terminate()
                process = record["process"]
                if process is not None:
                    if process.poll() is None and record["job"].active() == 0:
                        process.kill()  # the owned bootstrap may not yet have joined its Job
                    process.wait(timeout=max(0.1, min(5, deadline - time.monotonic())))
                until = min(deadline, time.monotonic() + 5)
                while record["job"].active() and time.monotonic() < until:
                    time.sleep(0.02)
                if record["job"].active():
                    raise RuntimeError("owned descendants did not drain")
            except Exception as error:
                cleanup_errors.append(f"{record['label']}: {type(error).__name__}")
            finally:
                if record["log"]:
                    record["log"].close()
                try:
                    record["job"].close()
                except OSError:
                    cleanup_errors.append("child Job close failed")
        if aggregate is not None:
            try:
                if aggregate.active():
                    aggregate.terminate()
                    raise RuntimeError("aggregate descendants survived child cleanup")
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            finally:
                try:
                    aggregate.close()
                except OSError:
                    cleanup_errors.append("aggregate Job close failed")
        if cleanup_errors:
            summary.update(success=False, cleanup_errors=cleanup_errors)
        summary["elapsed"] = time.monotonic() - started
        summary["target_met"] = summary["success"] and summary["elapsed"] < 600
        if summary["elapsed"] >= options.seconds:
            summary.update(success=False, target_met=False, error="whole-run hard deadline exceeded")
        write_json(output / "summary.json", summary)
    print(json.dumps({key: summary[key] for key in ("success", "target_met", "elapsed")}), flush=True)
    print(f"Reports: {output}", flush=True)
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        raise SystemExit(child_main(Path(sys.argv[2])))
    raise SystemExit(main())
