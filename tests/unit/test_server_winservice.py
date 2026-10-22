"""Task 14's actual lifecycle, ctypes boundary and private rotating-stream consumers.

All instances, files and descriptors belong to these tests. Native service and ACL
calls are replaced at their external boundary; no service is installed or started.
"""

import builtins
import ctypes
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.server import instance
from latet.server import winservice as win

from .test_server_service import _host_thread, _owned_instance


@pytest.fixture
def log_metadata(monkeypatch):
    """Synthetic metadata; actual owned file opens/rotation remain in production."""
    monkeypatch.setattr(win.folders, "namespace_folder_problem", lambda path, found: None)
    monkeypatch.setattr(win.folders, "private_folder_problem", lambda path: None)
    monkeypatch.setattr(win.folders, "_windows_folder_problem", lambda path: None)
    from latet.server import service

    monkeypatch.setattr(service, "log_file_problem", lambda path, principal: None)


def _quick_deadlines(monkeypatch):
    monkeypatch.setattr(win, "START_SECONDS", 0.03)
    monkeypatch.setattr(win, "STOP_SECONDS", 0.04)
    monkeypatch.setattr(win, "CHECKPOINT_SECONDS", 0.005)
    monkeypatch.setattr(win, "POLL_SECONDS", 0.001)


@pytest.mark.parametrize("phase", ["make", "startup", "running"])
def test_noncooperative_work_is_bounded_and_late_factory_observes_stop(monkeypatch, phase):
    """Removing either deadline leaves the host alive beyond its finite wait."""
    _quick_deadlines(monkeypatch)
    entered, release, cleaned, running = (threading.Event() for _ in range(4))
    target = SimpleNamespace(started=False, should_exit=False)

    def run():
        entered.set()
        target.started = phase == "running"
        release.wait(2)
        cleaned.set()

    made = SimpleNamespace(uvicorn=target, run=run)

    def make():
        if phase == "make":
            entered.set()
            release.wait(2)
        return made

    states = []

    def report(state, hint, error=0):
        states.append((state, hint, error))
        if state == win.RUNNING:
            running.set()

    host = win.ServiceHost(make, report)
    thread, errors = _host_thread(host)
    try:
        assert entered.wait(1)
        if phase == "running":
            assert running.wait(1)
            host.control(win.CONTROL_STOP)
        thread.join(1)
        assert not thread.is_alive() and errors == []
        assert host.timed_out and states[-1] == (win.STOPPED, 0, 1)
        assert sum(state == win.STOP_PENDING for state, _, _ in states) > 1
        release.set()
        assert cleaned.wait(1)
        assert target.should_exit
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize("phase", ["make", "run"])
def test_cancellation_reports_failure_without_leaking_or_double_cleanup(monkeypatch, caplog, capsys, phase):
    made, closed, _, release = _owned_instance(monkeypatch)
    states, unhandled = [], []
    monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(type(args.exc_value)))

    def cancelled(**kwargs):
        raise KeyboardInterrupt("synthetic-secret-cancellation")

    made.uvicorn.run = cancelled

    def make():
        if phase == "make":
            cancelled()
        return made

    host = win.ServiceHost(make, lambda state, hint, error=0: states.append((state, error)))
    thread, errors = _host_thread(host)
    try:
        thread.join(2)
        assert not thread.is_alive() and errors == [] and unhandled == []
        assert host.failed and states[-1] == (win.STOPPED, 1)
        assert closed == (["closed"] if phase == "run" else [])
        output = capsys.readouterr()
        assert "synthetic-secret" not in caplog.text + output.out + output.err
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        release.set()
        thread.join(2)


def test_normal_early_run_exit_is_failure(monkeypatch):
    """A run return without a stop request must activate SCM failure recovery."""
    made, closed, _, _ = _owned_instance(monkeypatch)
    made.uvicorn.run = lambda **kwargs: None
    states = []
    win.ServiceHost(lambda: made, lambda state, hint, error=0: states.append((state, error))).run()
    assert states[-1] == (win.STOPPED, 1)
    assert closed == ["closed"]


def test_running_then_run_failure_is_failure_and_closes_once(monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    made, closed, _, _ = _owned_instance(monkeypatch)

    def run(**kwargs):
        made.uvicorn.started = True
        entered.set()
        assert release.wait(2)
        raise RuntimeError("synthetic-secret-running-failure")

    made.uvicorn.run = run
    states = []
    running = threading.Event()

    def report(state, hint, error=0):
        states.append((state, error))
        if state == win.RUNNING:
            running.set()

    host = win.ServiceHost(lambda: made, report)
    thread, errors = _host_thread(host)
    try:
        assert entered.wait(1) and running.wait(1)
        release.set()
        thread.join(2)
        assert not thread.is_alive() and not errors
        assert states[-1] == (win.STOPPED, 1) and closed == ["closed"]
        assert "synthetic-secret" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize("bad_state", [win.START_PENDING, win.RUNNING, win.STOP_PENDING, win.STOPPED])
def test_report_failure_never_leaves_a_cooperative_instance_running(monkeypatch, bad_state, caplog):
    made, closed, _, release = _owned_instance(monkeypatch)
    states, built = [], []

    def make():
        built.append(True)
        return made

    def report(state, hint, error=0):
        states.append((state, error))
        if state == bad_state:
            raise OSError("synthetic-secret-report-failure")
        if state == win.RUNNING and bad_state in (win.STOP_PENDING, win.STOPPED):
            host.control(win.CONTROL_STOP)

    host = win.ServiceHost(make, report)
    thread, errors = _host_thread(host)
    try:
        thread.join(2)
        assert not thread.is_alive() and errors == [] and host.failed
        assert states[-1][0] == win.STOPPED
        assert built == ([] if bad_state == win.START_PENDING else [True])
        assert closed == ([] if bad_state == win.START_PENDING else ["closed"])
        assert "synthetic-secret" not in caplog.text
    finally:
        made.uvicorn.should_exit = True
        release.set()
        thread.join(2)


def test_repeated_controls_do_not_extend_deadline_or_reenter_stopped(monkeypatch):
    _quick_deadlines(monkeypatch)
    release, running = threading.Event(), threading.Event()
    made = SimpleNamespace(uvicorn=SimpleNamespace(started=True, should_exit=False), run=lambda: release.wait(2))
    states = []

    def report(state, hint, error=0):
        states.append((state, hint, error))
        if state == win.RUNNING:
            running.set()

    host = win.ServiceHost(lambda: made, report)
    thread, errors = _host_thread(host)
    try:
        assert running.wait(1)
        host.control(win.CONTROL_STOP)
        until = time.monotonic() + 1
        while thread.is_alive() and time.monotonic() < until:
            host.control(win.CONTROL_SHUTDOWN)
            host.control(win.CONTROL_INTERROGATE)
            thread.join(0.005)
        before = len(states)
        assert not thread.is_alive() and errors == [] and host.timed_out
        host.control(win.CONTROL_STOP)
        host.control(win.CONTROL_SHUTDOWN)
        assert len(states) == before
        host.control(win.CONTROL_INTERROGATE)
        assert len(states) == before  # STOPPED closes SCM's RPC status context.
    finally:
        release.set()
        thread.join(2)


def test_full_thirty_second_instance_grace_fits_scm_hints(monkeypatch):
    """The real 30-second application grace must not be cut off by the host."""
    made, closed, _, release = _owned_instance(monkeypatch)
    running, draining = threading.Event(), threading.Event()

    def run(**kwargs):
        made.uvicorn.started = True
        while not made.uvicorn.should_exit:
            if release.wait(0.01):
                return
        draining.set()
        release.wait(30.05)

    made.uvicorn.run = run
    states = []

    def report(state, hint, error=0):
        states.append((state, hint, error, time.monotonic()))
        if state == win.RUNNING:
            running.set()

    host = win.ServiceHost(lambda: made, report)
    thread, errors = _host_thread(host)
    try:
        assert running.wait(2)
        stopped_at = time.monotonic()
        host.control(win.CONTROL_STOP)
        assert draining.wait(1)
        thread.join(33)
        assert not thread.is_alive() and errors == [] and not host.timed_out
        pending = [(hint, at) for state, hint, _, at in states if state == win.STOP_PENDING]
        assert pending[0][0] >= 30_000 and len(pending) >= 25
        assert states[-1][0:3] == (win.STOPPED, 0, 0)
        assert states[-1][3] - stopped_at >= 30
        assert closed == ["closed"]
    finally:
        release.set()
        thread.join(2)


def _scm(monkeypatch, *, fail="", observe=None):
    """Exercise real ctypes callbacks against an owned, synthetic advapi boundary."""
    reports, made = [], []
    registered = []
    target = SimpleNamespace(started=True, should_exit=False)

    def run():
        until = time.monotonic() + 2
        while not target.should_exit and time.monotonic() < until:
            time.sleep(0.001)

    def register(name, handler, context):
        registered.append(handler)
        return 0 if fail == "register" else 17

    def status(handle, pointer):
        record = pointer._obj
        if observe is not None:
            observe(record)
        reports.append(tuple(getattr(record, name) for name, _ in record._fields_))
        if record.dwCurrentState == win.RUNNING:
            registered[0](win.CONTROL_INTERROGATE, 0, None, None) if len(reports) == 2 else None
            registered[0](win.CONTROL_STOP, 0, None, None)
        return 0 if fail == "report" else 1

    def dispatch(table):
        if fail == "dispatch":
            return 0
        table[0].lpServiceProc(0, None)
        return 1

    api = SimpleNamespace(
        RegisterServiceCtrlHandlerExW=register, SetServiceStatus=status, StartServiceCtrlDispatcherW=dispatch
    )
    monkeypatch.setattr(win.ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(win.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(win.sys, "platform", "win32")

    def make():
        made.append(True)
        return SimpleNamespace(uvicorn=target, run=run)

    return make, reports, made


def test_scm_fields_controls_checkpoint_and_success_code(monkeypatch):
    make, reports, made = _scm(monkeypatch)
    win._dispatch(make)
    assert made == [True]
    assert reports[0] == (0x10, 2, 0, 0, 0, 1, 30_000)
    assert any(row == (0x10, 4, 5, 0, 0, 0, 0) for row in reports)
    assert any(row[1] == 3 and row[2] == 0 and row[5] >= 1 and row[6] >= 30_000 for row in reports)
    assert reports[-1] == (0x10, 1, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("failure", ["register", "report", "dispatch"])
def test_failed_scm_calls_refuse_start_without_native_side_effects(monkeypatch, failure):
    make, reports, made = _scm(monkeypatch, fail=failure)
    with pytest.raises(OSError, match="service"):
        win._dispatch(make)
    assert made == []
    if failure == "report":
        assert reports[-1][1] == 1 and reports[-1][3:5] == (1066, 1)


@pytest.mark.usefixtures("log_metadata")
@pytest.mark.parametrize("which", ["active", "backup", "parent"])
def test_link_or_reparse_metadata_is_rejected_before_log_open(tmp_path, monkeypatch, which):
    """Removing the plain-path guard reaches the forbidden real open boundary."""
    path = tmp_path / "logs" / "server.log"
    path.parent.mkdir()
    selected = {"active": path, "backup": path.with_suffix(".log.1"), "parent": path.parent}[which]
    if which != "parent":
        selected.write_text("untouched", encoding="utf-8")
    real = Path.lstat

    def lstat(self):
        found = real(self)
        if self != selected:
            return found
        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o600, st_nlink=1, st_file_attributes=0x400)

    monkeypatch.setattr(Path, "lstat", lstat)
    opened = []
    monkeypatch.setattr(win, "_open_log", lambda path: opened.append(path))
    with pytest.raises(OSError, match="private service log"):
        win.RotatingStream(path)
    assert opened == []


@pytest.mark.usefixtures("log_metadata")
def test_hardlinked_log_does_not_write_an_outside_owned_sentinel(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    folder = tmp_path / "logs"
    folder.mkdir()
    os.link(sentinel, folder / "server.log")
    with pytest.raises(OSError, match="private service log"):
        win.RotatingStream(folder / "server.log")
    assert sentinel.read_text(encoding="utf-8") == "untouched"


@pytest.mark.usefixtures("log_metadata")
@pytest.mark.parametrize("unsafe", ["namespace", "privacy"])
def test_unsafe_directory_metadata_refuses_before_open(tmp_path, monkeypatch, unsafe):
    if unsafe == "namespace":
        monkeypatch.setattr(win.folders, "namespace_folder_problem", lambda path, found: "unsafe")
    else:
        monkeypatch.setattr(win.folders, "private_folder_problem", lambda path: "unsafe")
    opened = []
    monkeypatch.setattr(win, "_open_log", lambda path: opened.append(path))
    with pytest.raises(OSError, match="private service log"):
        win.RotatingStream(tmp_path / "logs" / "server.log")
    assert opened == []


@pytest.mark.usefixtures("log_metadata")
def test_concurrent_writes_keep_intact_lines_and_byte_bounds(tmp_path):
    path = tmp_path / "logs" / "server.log"
    stream = win.RotatingStream(path, limit=512, keep=4)
    barrier = threading.Barrier(5)
    errors = []

    def writer(number):
        try:
            barrier.wait(2)
            for index in range(100):
                stream.write(f"writer={number};record={index:03d}\n")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(number,), daemon=True) for number in range(4)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(2)
        for thread in threads:
            thread.join(3)
        assert not errors and all(not thread.is_alive() for thread in threads)
        stream.close()
        retained = list(path.parent.iterdir())
        assert len(retained) == 5 and all(file.stat().st_size <= 512 for file in retained)
        valid = {f"writer={number};record={index:03d}" for number in range(4) for index in range(100)}
        lines = [line for file in retained for line in file.read_text(encoding="utf-8").splitlines()]
        assert lines and len(lines) == len(set(lines)) and set(lines) <= valid
    finally:
        stream.close()
        for thread in threads:
            thread.join(3)


@pytest.mark.usefixtures("log_metadata")
def test_rotation_failure_closes_handle_and_never_exposes_error_words(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "server.log"
    stream = win.RotatingStream(path, limit=16)
    stream.write("first record\n")

    def broken(*args):
        raise OSError("synthetic-secret-rotation")

    monkeypatch.setattr(win.os, "replace", broken)
    with pytest.raises(OSError, match="service log could not be written") as error:
        stream.write("second record\n")
    assert "synthetic-secret" not in str(error.value) and stream.closed
    stream.close()
    stream.flush()
    path.rename(path.with_name("closed.log"))
    with pytest.raises(ValueError, match="closed"):
        stream.write("last record")


@pytest.mark.usefixtures("log_metadata")
def test_utf8_write_larger_in_bytes_than_characters_never_exceeds_limit(tmp_path):
    path = tmp_path / "logs" / "server.log"
    stream = win.RotatingStream(path, limit=32)
    try:
        assert stream.write("é" * 32) == 32
        stream.flush()
        assert all(file.stat().st_size <= 32 for file in path.parent.iterdir())
    finally:
        stream.close()


@pytest.mark.usefixtures("log_metadata")
def test_extra_preexisting_backup_refuses_unbounded_retention(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "server.log.5").write_text("old private output", encoding="utf-8")
    with pytest.raises(OSError, match="private service log"):
        win.RotatingStream(folder / "server.log")
    assert not (folder / "server.log").exists()


@pytest.mark.usefixtures("log_metadata")
def test_noncanonical_backup_refuses_unbounded_retention(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "server.log.01").write_text("old private output", encoding="utf-8")
    with pytest.raises(OSError, match="private service log"):
        win.RotatingStream(folder / "server.log")
    assert not (folder / "server.log").exists()


@pytest.mark.usefixtures("log_metadata")
def test_default_stream_retains_five_files_of_at_most_ten_mib(tmp_path):
    assert shutil.disk_usage(tmp_path).free >= 80 * 1024 * 1024, "owned default-limit witness needs 80 MiB free"
    path = tmp_path / "logs" / "server.log"
    stream = win.RotatingStream(path)
    try:
        for number in range(7):
            stream.write(f"{number}" + "x" * (10 * 1024 * 1024 - 1))
        stream.write("oversized" + "x" * (10 * 1024 * 1024))
        stream.flush()
        retained = list(path.parent.iterdir())
        assert len(retained) == 5
        assert all(file.stat().st_size == 10 * 1024 * 1024 for file in retained)
        assert path.read_bytes()[:1] == b"6"
        assert (path.parent / "server.log.4").read_bytes()[:1] == b"2"
    finally:
        stream.close()


@pytest.fixture
def main_environment(tmp_path, monkeypatch, log_metadata):
    from latet.server import service

    folder = tmp_path / "Latet"
    folder.mkdir()
    (folder / "data").mkdir()
    (folder / "logs").mkdir()
    config = folder / "server.yaml"
    config.write_text("{}\n", encoding="utf-8")
    for variable in _SECRET_VARIABLES.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(service, "windows_folder", lambda: folder)
    monkeypatch.setattr(service, "windows_path_problem", lambda path, **kwargs: None)
    streams = []
    original = win.RotatingStream

    def stream(path):
        made = original(path)
        streams.append(made)
        return made

    monkeypatch.setattr(win, "RotatingStream", stream)
    try:
        yield folder, config, streams
    finally:
        for stream in streams:
            stream.close()


@pytest.mark.parametrize("failure", ["", "settings", "make", "dispatch", "deadline"])
def test_main_wires_stream_restores_outputs_and_closes_on_each_exit(main_environment, monkeypatch, failure):
    folder, config, streams = main_environment
    before = sys.stdout, sys.stderr
    calls, exits = [], []
    settings = object()

    def load(path):
        assert path == config
        if failure == "settings":
            raise ValueError("synthetic-secret-settings")
        return settings

    def make(found, *, stream):
        assert found is settings and stream is streams[0] and sys.stdout is stream and sys.stderr is stream
        calls.append("make")
        if failure == "make":
            raise RuntimeError("synthetic-secret-make")
        stream.write("instance output\n")
        return object()

    def dispatch(factory, *, cleanup):
        if failure == "dispatch":
            raise OSError("synthetic-secret-dispatch")
        if failure == "deadline":
            raise win.ServiceDeadline("synthetic-secret-deadline")
        factory()
        cleanup()

    def terminate(code):
        assert (sys.stdout, sys.stderr) == before and streams[0].closed
        exits.append(code)

    monkeypatch.setattr(instance, "settings", load)
    monkeypatch.setattr(instance, "make_instance", make)
    monkeypatch.setattr(win, "_dispatch", dispatch)
    monkeypatch.setattr(win.os, "_exit", terminate)
    monkeypatch.setenv("LATET_DATA_DIR", "preserved-data")
    monkeypatch.setenv("LATET_KEYRING", "preserved-keyring")
    if failure:
        with pytest.raises(SystemExit) as error:
            win.main(["--config", str(config)])
        assert error.value.code == 1
    else:
        win.main(["--config", str(config)])
    assert (sys.stdout, sys.stderr) == before
    assert streams[0].closed and exits == ([1] if failure == "deadline" else [])
    assert os.environ["LATET_DATA_DIR"] == "preserved-data"
    assert os.environ["LATET_KEYRING"] == "preserved-keyring"
    if not failure:
        assert (folder / "logs" / "server.log").read_text(encoding="utf-8") == "instance output\n"


def test_invalid_arguments_do_not_print_values_or_open_logs(main_environment, capsys):
    _, _, streams = main_environment
    with pytest.raises(SystemExit) as error:
        win.main(["--synthetic-secret-invalid"])
    assert error.value.code == 1 and streams == []
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err and "Traceback" not in output.err


def test_main_closes_and_restores_outputs_before_native_stopped(main_environment, monkeypatch):
    """SCM may terminate the process as soon as it receives the final STOPPED."""
    folder, config, streams = main_environment
    # Own the real file before replacing WinDLL with the synthetic SCM boundary.
    stream = win.RotatingStream(folder / "logs" / "server.log")
    before = sys.stdout, sys.stderr
    observations = []

    def observe(record):
        if record.dwCurrentState == win.STOPPED:
            observations.append((stream.closed, (sys.stdout, sys.stderr) == before))

    make, _, _ = _scm(monkeypatch, observe=observe)
    monkeypatch.setattr(win, "RotatingStream", lambda path: stream)
    monkeypatch.setattr(instance, "settings", lambda path: object())
    monkeypatch.setattr(instance, "make_instance", lambda settings, **kwargs: make())
    try:
        win.main(["--config", str(config)])
        assert streams == [stream]
        assert observations == [(True, True)]
    finally:
        stream.close()


_SECRET_VARIABLES = {
    "master_key_file": "LATET_MASTER_KEY_FILE",
    "database_password_file": "LATET_SERVER_DATABASE_PASSWORD_FILE",
    "valkey_password_file": "LATET_SERVER_VALKEY_PASSWORD_FILE",
    "metrics_token_file": "LATET_SERVER_METRICS_TOKEN_FILE",
    "reviewer_key_file": "LATET_SERVER_REVIEWER_KEY_FILE",
    "key": "LATET_SERVER_KEY",
}


def _selected_secret_opens(monkeypatch, selected, opened):
    """Trap content opens of owned secrets while retaining real config/log I/O."""
    for namespace in (os, builtins, io):
        original = namespace.open

        def guarded(path, *args, _open=original, **kwargs):
            if isinstance(path, (str, os.PathLike)) and Path(path) in selected:
                opened.append(Path(path))
                raise OSError("synthetic-secret-content-open")
            return _open(path, *args, **kwargs)

        monkeypatch.setattr(namespace, "open", guarded)


def _service_secret_descriptor(service, *, unsafe=False):
    entries = tuple(
        service.WindowsACE(sid, mask, 0)
        for sid, mask in (
            (service.SYSTEM, service.FULL),
            (service.ADMINISTRATORS, service.FULL),
            (service.SERVICE_SID, service.READ_EXECUTE),
        )
    )
    if unsafe:
        entries += (service.WindowsACE("S-1-5-21-1-2-3-1002", service.READ_EXECUTE, 0),)
    return service.WindowsSecurity(service.SYSTEM, True, entries, False)


@pytest.mark.parametrize("field", list(_SECRET_VARIABLES))
@pytest.mark.parametrize("metadata", ["unsafe", "unknown"])
def test_main_refuses_selected_secret_metadata_before_settings_or_content(
    main_environment, monkeypatch, capsys, caplog, field, metadata
):
    """Removing runtime preflight reaches the settings trap before checking a secret."""
    from latet.server import service

    folder, config, streams = main_environment
    secret = folder / "synthetic-private-path.secret"
    secret.write_text("synthetic-secret-value", encoding="utf-8")
    config.write_text(json.dumps({field: str(secret)}), encoding="utf-8")
    for variable in _SECRET_VARIABLES.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(service, "platform", lambda: "windows")
    monkeypatch.setattr(service, "_namespace_problem", lambda path, **kwargs: None)
    inspected, settings_called, opened = [], [], []

    def inspect(path):
        inspected.append(path)
        assert path == secret
        if metadata == "unknown":
            raise OSError("synthetic-secret-access-metadata")
        return _service_secret_descriptor(service, unsafe=True)

    def settings(path):
        settings_called.append(path)
        raise RuntimeError("synthetic-secret-settings-before-metadata")

    def dispatch(factory, *, cleanup):
        factory()
        cleanup()

    monkeypatch.setattr(service, "windows_security", inspect)
    monkeypatch.setattr(instance, "settings", settings)
    monkeypatch.setattr(win, "_dispatch", dispatch)
    _selected_secret_opens(monkeypatch, {secret}, opened)
    before = sys.stdout, sys.stderr
    with pytest.raises(SystemExit) as error:
        win.main(["--config", str(config)])
    assert error.value.code == 1
    assert settings_called == [] and opened == []
    assert inspected == [secret]
    assert (sys.stdout, sys.stderr) == before and streams[0].closed
    output = capsys.readouterr()
    assert output.out == "" and output.err == "The Latet Windows service could not run.\n"
    visible = output.out + output.err + caplog.text + (folder / "logs" / "server.log").read_text(encoding="utf-8")
    assert "synthetic-secret" not in visible and str(secret) not in visible and "Traceback" not in visible


@pytest.mark.parametrize("selection", ["configuration", "environment"])
def test_main_accepts_private_secret_with_nonempty_environment_precedence(main_environment, monkeypatch, selection):
    """A safe selected descriptor starts; a nonempty env path supersedes unsafe YAML."""
    from latet.server import service

    folder, config, streams = main_environment
    safe = folder / "safe-selected.secret"
    unsafe = folder / "unsafe-unselected.secret"
    safe.write_text("synthetic-secret-safe", encoding="utf-8")
    unsafe.write_text("synthetic-secret-unsafe", encoding="utf-8")
    config.write_text(
        json.dumps({"master_key_file": str(safe if selection == "configuration" else unsafe)}), encoding="utf-8"
    )
    for variable in _SECRET_VARIABLES.values():
        monkeypatch.delenv(variable, raising=False)
    if selection == "environment":
        monkeypatch.setenv("LATET_MASTER_KEY_FILE", str(safe))
    monkeypatch.setattr(service, "platform", lambda: "windows")
    monkeypatch.setattr(service, "_namespace_problem", lambda path, **kwargs: None)

    def inspect(path):
        assert path in {safe, unsafe}
        return _service_secret_descriptor(service, unsafe=path == unsafe)

    calls, opened = [], []
    selected_settings = object()

    def settings(path):
        assert path == config
        calls.append("settings")
        return selected_settings

    def make(found, *, stream):
        assert found is selected_settings and stream is streams[0]
        calls.append("instance")
        stream.write("private configuration accepted\n")
        return object()

    def dispatch(factory, *, cleanup):
        factory()
        cleanup()

    monkeypatch.setattr(service, "windows_security", inspect)
    monkeypatch.setattr(instance, "settings", settings)
    monkeypatch.setattr(instance, "make_instance", make)
    monkeypatch.setattr(win, "_dispatch", dispatch)
    _selected_secret_opens(monkeypatch, {safe, unsafe}, opened)
    before = sys.stdout, sys.stderr
    win.main(["--config", str(config)])
    assert calls == ["settings", "instance"] and opened == []
    assert (sys.stdout, sys.stderr) == before and streams[0].closed
    assert (folder / "logs" / "server.log").read_text(encoding="utf-8") == "private configuration accepted\n"
