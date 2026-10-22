"""The Windows Service Control Manager host and its private, bounded log files.

The instance owns its server and sockets, including cleanup in ``Instance.run``.
A worker that ignores cancellation cannot safely be killed by Python: the host
reports failure and the executable ends the process after closing its log stream.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TextIO, cast

from latet.server.instance import GRACEFUL_SECONDS, CannotListen
from latet.storage import folders

STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
CONTROL_STOP, CONTROL_INTERROGATE, CONTROL_SHUTDOWN = 1, 4, 5
NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED, ERROR_SERVICE_SPECIFIC = 0, 120, 1066
SERVICE_WIN32_OWN_PROCESS, ACCEPT_STOP, ACCEPT_SHUTDOWN = 0x10, 1, 4
START_SECONDS = 30.0
STOP_SECONDS = GRACEFUL_SECONDS + 5.0  # permit the full application grace and its cleanup
CHECKPOINT_SECONDS = 1.0
POLL_SECONDS = 0.05
LOG_LIMIT, LOG_KEEP = 10 * 1024 * 1024, 4
SERVICE_NAME = "LatetServer"
Report = Callable[[int, int, int], None]
log = logging.getLogger(__name__)


class ServiceDeadline(RuntimeError):
    """An instance worker still owns resources beyond its shutdown deadline."""


class ServiceHost:
    """Report actual instance progress; request a stop without closing its resources twice."""

    def __init__(self, make: Callable[[], Any], report: Report) -> None:
        self.make, self.report = make, report
        self.server: Any = None
        self.stopping = False
        self.failed = False
        self.timed_out = False
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._state = START_PENDING
        self._hint = int(START_SECONDS * 1000)
        self._error = 0
        self._stop_deadline: float | None = None
        self._finished = False

    def _report(self, state: int, hint: int = 0, error: int = 0) -> bool:
        self._state, self._hint, self._error = state, hint, error
        try:
            self.report(state, hint, error)
        except BaseException:
            self.failed = True
            log.error("the service status could not be reported")
            return False
        return True

    def _target(self) -> Any:
        return getattr(self.server, "uvicorn", self.server)

    def _stop(self) -> None:
        if not self.stopping:
            self.stopping = True
            self._stop_deadline = time.monotonic() + STOP_SECONDS
            self._report(STOP_PENDING, int(STOP_SECONDS * 1000))
        if self.server is not None:
            self._target().should_exit = True

    def _work(self) -> None:
        try:
            made = self.make()
            with self._lock:
                self.server = made
                if self.stopping:
                    self._target().should_exit = True
            # Even a stop during make enters run: its finally owns every resource
            # returned by make_instance, including sockets already bound there.
            made.run()
        except CannotListen:
            self.failed = True
            log.error("the Latet service could not bind its listening sockets")
        except BaseException:
            self.failed = True
            log.error("the Latet service instance failed")
        finally:
            self._done.set()

    def run(self) -> None:
        with self._lock:
            if self._finished:
                return
            if not self._report(START_PENDING, int(START_SECONDS * 1000)):
                self._finished = True
                self._report(STOPPED, 0, 1)
                return
        startup_deadline = time.monotonic() + START_SECONDS
        checkpoint = time.monotonic() + CHECKPOINT_SECONDS
        worker = threading.Thread(target=self._work, name="latet-server", daemon=True)
        try:
            worker.start()
            while not self._done.wait(POLL_SECONDS):
                with self._lock:
                    now = time.monotonic()
                    if not self.stopping and self._state != RUNNING:
                        if self.server is not None and self._target().started:
                            if not self._report(RUNNING):
                                self._stop()
                        elif now >= startup_deadline:
                            self.failed = True
                            log.error("the Latet service startup deadline expired")
                            self._stop()
                    deadline = self._stop_deadline if self.stopping else startup_deadline
                    if self.stopping and deadline is not None and now >= deadline:
                        self.failed = self.timed_out = True
                        log.error("the Latet service shutdown deadline expired")
                        break
                    if self._state in (START_PENDING, STOP_PENDING) and now >= checkpoint:
                        hint = max(1, math.ceil(((deadline or now) - now) * 1000))
                        if not self._report(self._state, hint):
                            self._stop()
                        checkpoint = now + CHECKPOINT_SECONDS
        except BaseException:
            self.failed = True
            log.error("the Latet service host failed")
            with self._lock:
                self._stop()
            if worker.is_alive() and not self._done.wait(STOP_SECONDS):
                self.timed_out = True
        finally:
            with self._lock:
                self._finished = True
                self._report(STOPPED, 0, int(self.failed or not self.stopping))

    def control(self, code: int) -> int:
        with self._lock:
            if code == CONTROL_INTERROGATE:
                if self._finished:
                    return NO_ERROR  # STOPPED has closed SCM's status context.
                if not self._report(self._state, self._hint, self._error) and not self._finished:
                    self._stop()
                return NO_ERROR
            if code in (CONTROL_STOP, CONTROL_SHUTDOWN):
                if not self._finished:
                    self._stop()
                return NO_ERROR
            return ERROR_CALL_NOT_IMPLEMENTED


def _plain(path: Path, *, directory: bool) -> os.stat_result:
    found = path.lstat()
    if (
        bool(getattr(found, "st_file_attributes", 0) & 0x400)
        or (not stat.S_ISDIR(found.st_mode) if directory else not stat.S_ISREG(found.st_mode))
        or (not directory and found.st_nlink != 1)
    ):
        raise OSError("the service log path is not a plain private path")
    return found


def _log_parent(path: Path) -> None:
    # Inspect every ancestor without resolving links. Only trusted namespaces may
    # precede the private directory, so an unrelated user cannot swap a component.
    for parent in [*reversed(path.parent.parents), path.parent]:
        try:
            found = _plain(parent, directory=True)
        except FileNotFoundError:
            parent.mkdir(mode=0o700)
            found = _plain(parent, directory=True)
        if folders.namespace_folder_problem(parent, found) is not None:
            raise OSError("the service log parent is unsafe")
    if folders.private_folder_problem(path.parent) is not None:
        raise OSError("the service log directory is not private")


def _log_file(path: Path) -> os.stat_result | None:
    try:
        found = _plain(path, directory=False)
    except FileNotFoundError:
        return None
    if sys.platform == "win32":
        from latet.server import service

        if service.log_file_problem(path, folders.my_sid()) is not None:
            raise OSError("the service log file is not private")
    elif found.st_uid not in (0, os.getuid()) or found.st_mode & 0o077:
        raise OSError("the service log file is not private")
    return found


def _open_log(path: Path) -> BinaryIO:
    """Open the final component without following reparse points or symbolic links."""
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        from latet.server import service

        advapi, kernel = service._windows_api()
        create = kernel.CreateFileW
        create.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        descriptor = ctypes.c_void_p()
        sddl = service.windows_sddl(writable=True, directory=False, owner=None, principal=folders.my_sid())
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError("the private service log descriptor is unavailable")
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # Append only; readers allowed, other writers and rename excluded while open.
        try:
            handle = create(str(path), 0x84, 1, ctypes.byref(attributes), 4, 0x00200000 | 0x08000000, None)
        finally:
            kernel.LocalFree(descriptor)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("the service log could not be opened")
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    else:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise OSError("a no-follow service log open is unavailable")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | nofollow | os.O_NONBLOCK, 0o600)
    try:
        opened = os.fstat(descriptor)
        checked = _log_file(path)
        if checked is None or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError("the service log is not a plain file")
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise OSError("the service log changed during open")
        return os.fdopen(descriptor, "ab", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


class RotatingStream:
    """Serialize UTF-8 writes, retaining at most five 10 MiB files by default.

    A write larger than one file is discarded whole: no partial record or fragment
    is copied into a diagnostic. Path and I/O failures carry fixed words only.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: Path, limit: int = LOG_LIMIT, keep: int = LOG_KEEP) -> None:
        if limit <= 0 or keep < 0:
            raise ValueError("service log limits must be positive with nonnegative retention")
        self.path = Path(os.path.abspath(path))
        self.limit, self.keep = limit, keep
        self._lock = threading.RLock()
        self._file: BinaryIO | None = None
        self.closed = False
        try:
            self._validate()
            self._file = _open_log(self.path)
            if os.fstat(self._file.fileno()).st_size > self.limit:
                raise OSError("an existing service log exceeds its limit")
        except BaseException:
            self.close()
            raise OSError("the private service log could not be opened") from None

    def _backup(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{number}")

    def _validate(self) -> None:
        _log_parent(self.path)
        # Refuse an older/unrecognized retention set; never remove somebody's
        # existing output to make the bound appear satisfied. Bound the scan too.
        prefix = self.path.name + "."
        retained = {str(number) for number in range(1, self.keep + 1)}
        with os.scandir(self.path.parent) as entries:
            for index, entry in enumerate(entries):
                suffix = entry.name.removeprefix(prefix)
                if index >= 1024 or (entry.name.startswith(prefix) and suffix.isdecimal() and suffix not in retained):
                    raise OSError("the existing service log retention is unsafe")
        for path in [self.path, *(self._backup(number) for number in range(1, self.keep + 1))]:
            found = _log_file(path)
            if found is not None and found.st_size > self.limit:
                raise OSError("an existing service log exceeds its limit")

    def _rotate(self) -> None:
        self._validate()
        assert self._file is not None
        self._file.close()
        self._file = None
        if self.keep:
            for number in range(self.keep - 1, 0, -1):
                if self._backup(number).exists():
                    os.replace(self._backup(number), self._backup(number + 1))
            os.replace(self.path, self._backup(1))
        else:
            self.path.unlink()
        self._file = _open_log(self.path)

    def write(self, text: str) -> int:
        with self._lock:
            if self.closed:
                raise ValueError("the service log is closed")
            # Bound encoding allocation even when a library hands us an enormous line.
            if len(text) > self.limit:
                return len(text)
            content = text.encode(self.encoding, errors=self.errors)
            if len(content) > self.limit:
                return len(text)
            try:
                assert self._file is not None
                if os.fstat(self._file.fileno()).st_size + len(content) > self.limit:
                    self._rotate()
                assert self._file is not None
                self._file.write(content)
            except BaseException:
                self.close()
                raise OSError("the service log could not be written") from None
            return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                except OSError:
                    self.close()
                    raise OSError("the service log could not be flushed") from None

    def close(self) -> None:
        with self._lock:
            opened, self._file = self._file, None
            self.closed = True
            if opened is not None:
                try:
                    opened.close()
                except OSError:
                    raise OSError("the service log could not be closed") from None

    def isatty(self) -> bool:
        return False


def _dispatch(make: Callable[[], Any], *, cleanup: Callable[[], None] | None = None) -> None:
    """Run only when called by SCM; preserve callback lifetimes until dispatch returns."""
    if sys.platform != "win32":
        raise OSError("a Windows service runs on Windows")
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)

    class ServiceStatus(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "dwServiceType",
                "dwCurrentState",
                "dwControlsAccepted",
                "dwWin32ExitCode",
                "dwServiceSpecificExitCode",
                "dwCheckPoint",
                "dwWaitHint",
            )
        ]

    class TableEntry(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", main_type)]

    advapi.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, handler_type, wintypes.LPVOID]
    advapi.SetServiceStatus.restype = wintypes.BOOL
    advapi.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(ServiceStatus)]
    advapi.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    advapi.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(TableEntry)]
    handle = wintypes.HANDLE()
    checkpoint = 0
    failed = False

    def report(state: int, hint: int, error: int = 0) -> None:
        nonlocal checkpoint, failed
        if state == STOPPED and cleanup is not None:
            try:
                cleanup()
            except BaseException:
                failed = True
                error = 1
        checkpoint = checkpoint + 1 if state in (START_PENDING, STOP_PENDING) else 0
        accepted = ACCEPT_STOP | ACCEPT_SHUTDOWN if state == RUNNING else 0
        code = ERROR_SERVICE_SPECIFIC if error else NO_ERROR
        status = ServiceStatus(SERVICE_WIN32_OWN_PROCESS, state, accepted, code, error, checkpoint, hint)
        if not advapi.SetServiceStatus(handle, ctypes.byref(status)):
            raise OSError("the service status could not be reported")

    host = ServiceHost(make, report)

    @handler_type
    def handler(control: int, event: int, data: Any, context: Any) -> int:
        try:
            return host.control(control)
        except BaseException:
            host.failed = True
            return ERROR_CALL_NOT_IMPLEMENTED

    @main_type
    def service_main(argc: int, argv: Any) -> None:
        nonlocal failed
        try:
            handle.value = advapi.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler, None)
            if not handle.value:
                failed = True
                return
            host.run()
        except BaseException:
            failed = True

    table = (TableEntry * 2)(TableEntry(SERVICE_NAME, service_main), TableEntry(None, main_type()))
    if not advapi.StartServiceCtrlDispatcherW(table):
        raise OSError("the service dispatcher could not be started")
    if host.timed_out:
        raise ServiceDeadline("the service worker did not stop within its deadline")
    if failed or host.failed:
        raise OSError("the service could not run")


class _Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse normally repeats unknown arguments, which may contain credentials.
        raise ValueError("the service arguments are invalid")


def main(argv: list[str] | None = None) -> None:
    """SCM entry point, restoring Python outputs and closing the log on every exit."""
    from latet.server import instance, service

    before = sys.stdout, sys.stderr
    stream: RotatingStream | None = None
    fatal = False
    failed = False

    def cleanup() -> None:
        nonlocal stream
        sys.stdout, sys.stderr = before
        opened, stream = stream, None
        if opened is not None:
            opened.close()

    try:
        parser = _Arguments(prog="latet.server.winservice")
        parser.add_argument("--config", type=Path, default=None)
        args = parser.parse_args(argv)
        folder = service.windows_folder()
        config = args.config or folder / "server.yaml"
        for path, writable in ((folder, False), (folder / "data", True), (folder / "logs", True), (config, False)):
            if service.windows_path_problem(path, writable=writable) is not None:
                raise OSError("the service paths are not private")
        os.environ.setdefault("LATET_DATA_DIR", str(folder / "data"))
        os.environ.setdefault("LATET_KEYRING", "off")
        stream = RotatingStream(folder / "logs" / "server.log")
        output = cast(TextIO, stream)
        sys.stdout = sys.stderr = output

        def make() -> Any:
            if service.configuration_problem(config) is not None:
                raise OSError("the service configuration is not private")
            return instance.make_instance(instance.settings(config), stream=output)

        _dispatch(make, cleanup=cleanup)
    except ServiceDeadline:
        fatal = failed = True
    except Exception:
        failed = True
    finally:
        try:
            cleanup()
        except OSError:
            failed = True
    if failed:
        # No exception text or traceback, even if startup failed before logging existed.
        with suppress(OSError, AttributeError):
            before[1].write("The Latet Windows service could not run.\n")
        if fatal:
            os._exit(1)  # only process exit can stop a noncooperative Python worker
        raise SystemExit(1)


if __name__ == "__main__":
    main()
