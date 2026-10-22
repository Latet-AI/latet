"""Task 14 preparation: real consumers and the brief's declared service interfaces.

Prepared statically, never collected or executed during the preparation grant. The
``TestExistingConsumers`` tests use today's code. The other classes require the
declared service/winservice modules, which are absent at the preparation baseline;
they intentionally neither skip nor fabricate those implementations. Native ACL,
SCM, path-provenance and deadline evidence remains in the task's evidence matrix.
"""

import importlib
import subprocess
import threading
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any, cast

import pytest

from latet.server import instance
from latet.storage import folders
from latet.storage.folders import ADMINISTRATORS, SYSTEM, Grant

from .conftest import run_cli

ME = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"
SERVICE = "S-1-5-80-1-2-3-4-5"
ALL, READ = 0x1F01FF, 0x1200A9


class TestExistingConsumers:
    """Preserve personal-folder isolation while adding a separate service policy."""

    @pytest.mark.parametrize("owner", [ME, SYSTEM, ADMINISTRATORS])
    def test_personal_folder_accepts_only_known_private_metadata(self, tmp_path, monkeypatch, owner):
        grants = [Grant(ME, ALL, False), Grant(SYSTEM, ALL, False), Grant(ADMINISTRATORS, ALL, False)]
        monkeypatch.setattr(folders, "my_sid", lambda: ME)
        monkeypatch.setattr(folders, "folder_grants", lambda path: (owner, grants))
        assert folders._windows_folder_problem(tmp_path) is None

    @pytest.mark.parametrize("owner", [OTHER, SERVICE, None])
    def test_service_policy_must_not_trust_other_owners_for_personal_folders(self, tmp_path, monkeypatch, owner):
        monkeypatch.setattr(folders, "my_sid", lambda: ME)
        monkeypatch.setattr(folders, "folder_grants", lambda path: (owner, [Grant(ME, ALL, False)]))
        assert folders._windows_folder_problem(tmp_path) is not None

    @pytest.mark.parametrize("sid", [OTHER, SERVICE, "", "S-1-1-0", "S-1-5-32-545"])
    @pytest.mark.parametrize("inherit_only", [False, True])
    def test_service_policy_must_not_admit_foreign_personal_folder_grants(
        self, tmp_path, monkeypatch, sid, inherit_only
    ):
        grants = [Grant(ME, ALL, False), Grant(sid, READ, inherit_only)]
        monkeypatch.setattr(folders, "my_sid", lambda: ME)
        monkeypatch.setattr(folders, "folder_grants", lambda path: (ME, grants))
        assert folders._windows_folder_problem(tmp_path) is not None

    def test_unreadable_personal_metadata_fails_without_printing_os_error_words(self, tmp_path, monkeypatch):
        monkeypatch.setattr(folders, "my_sid", lambda: ME)

        def unreadable(path):
            raise PermissionError("synthetic-secret-acl-failure")

        monkeypatch.setattr(folders, "folder_grants", unreadable)
        problem = folders._windows_folder_problem(tmp_path)
        assert problem is not None and "cannot be read" in problem
        assert "synthetic-secret" not in problem

    @pytest.mark.parametrize("arguments", [(), ("--yes",), ("--print",), ("--yes", "--print")])
    def test_service_install_refuses_an_agent_before_settings_or_processes(self, monkeypatch, arguments):
        """A missing guard must reach a forbidden side effect or miss the agent refusal."""
        monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-task-14-agent")

        def forbidden(*args, **kwargs):
            pytest.fail("an agent reached service installation side effects")

        monkeypatch.setattr(instance, "settings", forbidden)
        monkeypatch.setattr(subprocess, "run", forbidden)
        result = run_cli("server", "service", "install", *arguments)
        assert result.exit_code == 2, result.output
        assert "agent" in result.output.lower(), result.output
        assert "Start-Service" not in result.output and "systemctl start" not in result.output


@pytest.fixture
def service_module():
    """Declared by the brief; absent at baseline, not an invented substitute API."""
    return importlib.import_module("latet.server.service")


@pytest.fixture
def winservice_module():
    return importlib.import_module("latet.server.winservice")


class TestDeclaredCommandConstructors:
    """Future capability tests of generated artifacts, with no command execution."""

    def test_command_construction_performs_no_writes_or_subprocesses(self, service_module, tmp_path, monkeypatch):
        from pathlib import Path

        def forbidden(*args, **kwargs):
            pytest.fail("pure service command construction changed the filesystem or ran a command")

        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(Path, "mkdir", forbidden)
        monkeypatch.setattr(Path, "open", forbidden)
        assert service_module.systemd_unit(["/opt/latet/python", "-m", "latet"])
        assert service_module.windows_commands([r"C:\Latet\python.exe", "-m", "latet.server.winservice"], tmp_path)

    def test_systemd_unit_limits_privileges_and_keeps_writes_in_state(self, service_module):
        unit = service_module.systemd_unit(["/opt/latet/python", "-m", "latet", "server", "run"])
        lines = set(unit.splitlines())
        assert {
            "User=latet",
            "Group=latet",
            "Restart=on-failure",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "StateDirectory=latet",
            "UMask=0077",
            "CapabilityBoundingSet=",
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
            "SystemCallFilter=@system-service",
            "WantedBy=multi-user.target",
        } <= lines
        assert not any(line.startswith("StandardOutput=") for line in lines)
        assert "MemoryDenyWriteExecute=yes" not in lines

    def test_systemd_serializes_argument_boundaries_and_expansions(self, service_module):
        unit = service_module.systemd_unit(
            ["/opt/Latet Tools/python", "-m", "latet", "server", "run", "--config", '/etc/latet/a "b" %n $HOME.yaml']
        )
        [line] = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        assert '"/opt/Latet Tools/python"' in line
        assert r"\"b\"" in line
        assert "%%n" in line and "$$HOME" in line
        assert "\n" not in line and "\r" not in line

    @pytest.mark.parametrize("control", ["\x00", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", chr(0x2028)])
    @pytest.mark.parametrize("builder", ["systemd_unit", "windows_commands"])
    def test_command_constructors_reject_controls(self, service_module, control, builder):
        with pytest.raises(ValueError, match=r"\S"):
            getattr(service_module, builder)(["/opt/latet/python", "--config", f"before{control}after"])

    @pytest.mark.parametrize(
        ("place", "homes", "found"),
        [
            (PurePosixPath("/home/emily/latet"), [PurePosixPath("/home")], True),
            (PurePosixPath("/root/latet"), [PurePosixPath("/root")], True),
            (PurePosixPath("/people/emily/latet"), [PurePosixPath("/people/emily")], True),
            (PurePosixPath("/homebrew/latet"), [PurePosixPath("/home")], False),
            (PurePosixPath("/opt/latet"), [PurePosixPath("/home"), PurePosixPath("/root")], False),
            (PureWindowsPath("C:/Users/Emily/latet"), [PureWindowsPath("c:/users")], True),
            (PureWindowsPath("D:/People/Emily/latet"), [PureWindowsPath("D:/People/Emily")], True),
            (PureWindowsPath("C:/UsersBackup/Latet"), [PureWindowsPath("C:/Users")], False),
        ],
    )
    def test_home_matching_respects_path_components(self, service_module, place, homes, found):
        assert (service_module.in_a_home([place], homes) is not None) is found

    def test_windows_binary_path_preserves_spaces_quotes_and_trailing_backslashes(self, service_module, tmp_path):
        steps = service_module.windows_commands(
            [r"C:\Program Files\Latet\python.exe", "-m", "latet.server.winservice", "--config", 'D:\\a "b"\\'],
            tmp_path / "Latet",
        )
        [create] = [step for step in steps if len(step) > 1 and step[1] == "create"]
        assert PureWindowsPath(create[0]).is_absolute(), create
        binary = create[create.index("binPath=") + 1]
        assert binary == r'"C:\Program Files\Latet\python.exe" -m latet.server.winservice --config "D:\a \"b\"\\"'
        assert create[create.index("start=") + 1] == "disabled"

    def test_windows_commands_use_service_account_and_restart_on_failure(self, service_module, tmp_path):
        steps = service_module.windows_commands([r"C:\Latet\python.exe", "-m", "latet.server.winservice"], tmp_path)
        by_verb = {
            step[1]: step for step in steps if len(step) > 1 and PureWindowsPath(step[0]).name.lower() == "sc.exe"
        }
        assert by_verb["create"][by_verb["create"].index("obj=") + 1] == r"NT SERVICE\LatetServer"
        assert steps[-1] == by_verb["config"] and steps[-1][steps[-1].index("start=") + 1] == "delayed-auto"
        assert by_verb["failure"][by_verb["failure"].index("actions=") + 1] == "restart/5000/restart/5000/restart/60000"
        assert by_verb["failureflag"][-1] == "1"
        assert all(PureWindowsPath(step[0]).is_absolute() for step in steps)


def _owned_instance(monkeypatch, *, fail_run=False):
    """Today's real Instance owns Server.close; only uvicorn's external run is doubled."""
    closed = []
    entered = threading.Event()
    release = threading.Event()
    settings = SimpleNamespace(certificate=None, host="127.0.0.1", port=5283)
    server = SimpleNamespace(settings=settings, instance="task-14", close=lambda: closed.append("closed"))
    uvicorn = SimpleNamespace(started=False, should_exit=False)

    def run(*, sockets):
        entered.set()
        if fail_run:
            raise RuntimeError("synthetic-secret-run-failure")
        uvicorn.started = True
        while not uvicorn.should_exit and not release.wait(0.01):
            pass

    uvicorn.run = run
    # GC freezing is process-wide and unrelated to this test's service lifecycle.
    monkeypatch.setattr(instance.gc, "collect", lambda: None)
    monkeypatch.setattr(instance.gc, "freeze", lambda: None)
    monkeypatch.setattr(instance.gc, "unfreeze", lambda: None)
    return instance.Instance(cast(Any, server), cast(Any, uvicorn), own=[]), closed, entered, release


def _host_thread(host):
    """Keep test-owned thread errors observable; never leak a pytest thread warning."""
    errors = []

    def run():
        try:
            host.run()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="task-14-test-host", daemon=True)
    thread.start()
    return thread, errors


class TestDeclaredServiceLifecycle:
    """The host consumes the actual Task 12 Instance shape, not the obsolete sketch."""

    def test_stop_uses_uvicorn_and_the_instance_closes_its_server_once(self, winservice_module, monkeypatch):
        made, closed, entered, release = _owned_instance(monkeypatch)
        running = threading.Event()
        states = []

        def report(state, hint, error=0):
            states.append((state, hint, error))
            if state == winservice_module.RUNNING:
                running.set()

        host = winservice_module.ServiceHost(lambda: made, report)
        thread, errors = _host_thread(host)
        try:
            assert entered.wait(2) and running.wait(2)
            assert host.control(winservice_module.CONTROL_STOP) == winservice_module.NO_ERROR
            thread.join(2)
            assert not thread.is_alive()
            assert made.uvicorn.should_exit
            assert closed == ["closed"] and errors == []
            assert [state for state, _, _ in states] == [
                winservice_module.START_PENDING,
                winservice_module.RUNNING,
                winservice_module.STOP_PENDING,
                winservice_module.STOPPED,
            ]
            assert states[-1][2] == 0
            assert any(hint >= 30_000 for state, hint, _ in states if state == winservice_module.STOP_PENDING)
            assert host.control(99) == winservice_module.ERROR_CALL_NOT_IMPLEMENTED
        finally:
            made.uvicorn.should_exit = True
            release.set()
            thread.join(2)

    def test_stop_during_make_never_reports_running(self, winservice_module, monkeypatch):
        made, closed, _, release_run = _owned_instance(monkeypatch)
        making = threading.Event()
        release_make = threading.Event()
        states = []

        def make():
            making.set()
            if not release_make.wait(2):
                raise TimeoutError("test did not release the factory")
            return made

        host = winservice_module.ServiceHost(make, lambda state, hint, error=0: states.append((state, error)))
        thread, errors = _host_thread(host)
        try:
            assert making.wait(2)
            assert host.control(winservice_module.CONTROL_STOP) == winservice_module.NO_ERROR
            release_make.set()
            thread.join(2)
            assert not thread.is_alive() and errors == []
            assert made.uvicorn.should_exit and closed == ["closed"]
            assert all(state != winservice_module.RUNNING for state, _ in states)
            assert states[-1] == (winservice_module.STOPPED, 0)
        finally:
            release_make.set()
            made.uvicorn.should_exit = True
            release_run.set()
            thread.join(2)

    @pytest.mark.parametrize("failure", ["make", "bind", "run"])
    def test_failures_are_reported_without_secret_words_or_tracebacks(
        self, winservice_module, monkeypatch, caplog, capsys, failure
    ):
        made, closed, _, release = _owned_instance(monkeypatch, fail_run=True)
        states = []

        def make():
            if failure == "bind":
                raise instance.CannotListen("synthetic-secret-bind-failure")
            if failure == "make":
                raise RuntimeError("synthetic-secret-make-failure")
            return made

        host = winservice_module.ServiceHost(make, lambda state, hint, error=0: states.append((state, error)))
        thread, errors = _host_thread(host)
        try:
            thread.join(2)
            assert not thread.is_alive() and errors == []
            assert states[0][0] == winservice_module.START_PENDING
            assert states[-1][0] == winservice_module.STOPPED and states[-1][1] != 0
            assert closed == (["closed"] if failure == "run" else [])
            output = capsys.readouterr()
            assert "synthetic-secret" not in caplog.text + output.out + output.err
            assert "Traceback" not in caplog.text + output.out + output.err
            assert all(record.exc_info is None for record in caplog.records)
        finally:
            release.set()
            thread.join(2)


class TestDeclaredRotatingStream:
    """Small owned files exercise byte and retention limits without large scratch data."""

    @pytest.mark.parametrize("text", ["a" * 32, chr(0xE9) * 16, "x" * 129])
    def test_every_retained_file_stays_within_its_byte_limit(self, winservice_module, tmp_path, text):
        path = tmp_path / "logs" / "server.log"
        stream = winservice_module.RotatingStream(path, limit=32, keep=4)
        try:
            for _ in range(9):
                stream.write(text)
            stream.flush()
            retained = list(path.parent.iterdir())
            assert 1 <= len(retained) <= 5
            assert all(item.stat().st_size <= 32 for item in retained)
            assert all(
                item.name in {"server.log", "server.log.1", "server.log.2", "server.log.3", "server.log.4"}
                for item in retained
            )
            if len(text.encode("utf-8")) <= 32:
                assert sum(item.stat().st_size for item in retained) > 0
        finally:
            stream.close()

    def test_normal_lines_rotate_and_keep_the_newest_line(self, winservice_module, tmp_path):
        path = tmp_path / "logs" / "server.log"
        stream = winservice_module.RotatingStream(path, limit=32, keep=4)
        try:
            for number in range(12):
                stream.write(f"line {number:02d} " + "x" * 16 + "\n")
            stream.flush()
            assert len(list(path.parent.iterdir())) == 5
            assert "line 11" in path.read_text(encoding="utf-8")
            assert "line 00" not in "".join(item.read_text(encoding="utf-8") for item in path.parent.iterdir())
        finally:
            stream.close()

    def test_close_flushes_and_releases_the_file(self, winservice_module, tmp_path):
        path = tmp_path / "logs" / "server.log"
        stream = winservice_module.RotatingStream(path, limit=32, keep=4)
        stream.write("last line\n")
        stream.close()
        stream.close()
        assert path.read_text(encoding="utf-8") == "last line\n"
        moved = path.with_name("closed.log")
        path.rename(moved)
        assert moved.read_text(encoding="utf-8") == "last line\n"
