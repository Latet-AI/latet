"""Reserving Latet's port on Windows as an administered TCP port exclusion, and the record of what Latet reserved.

The one elevated step runs a fixed command line and nothing else: Windows' own ``netsh.exe`` with one line, or, for
several, ``cmd.exe /d /c "<netsh> <line> & <netsh> <line>"``; the full paths of Windows' own programs (the system folder
asked of Windows, ``ports.system_folder``, never of an environment variable), and integers checked to be ports as its
only variables. It never runs a script file or text from anywhere else. The single function that calls Windows is
``_shell_execute``; the tests replace it, so no test shows a UAC dialog or runs netsh.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any

from latet.config import data_dir, write_private_where_allowed
from latet.service import ports
from latet.storage.folders import make_private_folder

RECORD = "port-reservations.json"

_ERROR_CANCELLED = 1223  # the UAC dialog was declined


class ElevationDeclined(Exception):
    """The UAC dialog was declined."""


class OutcomeUnknown(ports.TableUnreadable):
    """The elevated step ran, but Windows' table could not be read afterwards: what it changed is not known, and the
    record keeps each port it touched as it was."""


class ReservedAlready(Exception):
    """An administered range Latet did not make holds the port already: Windows cannot take it, nothing ran, and Latet
    records nothing, so that it never takes that exclusion away."""

    def __init__(self, port: int) -> None:
        super().__init__(f"port {port} is reserved already")
        self.port = port


class RecordUnreadable(OSError):
    """Latet's record of the ports it reserved is there but cannot be read: it is never written over."""


class RecordNotUpdated(Exception):
    """The elevated delete took ports away (``gone``, with those found gone before it), but the record could not be
    written afterwards: it still names them. ``error`` is the write's."""

    def __init__(self, gone: list[int], error: OSError) -> None:
        super().__init__(str(error))
        self.gone = gone
        self.error = error


def _checked(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"not a port: {port!r}")
    return port


def _line(verb: str, port: int) -> str:
    return f"int ipv4 {verb} excludedportrange protocol=tcp startport={_checked(port)} numberofports=1 store=persistent"


def add_line(port: int) -> str:
    """The netsh arguments (without the executable) that exclude ``port`` for good."""
    return _line("add", port)


def delete_line(port: int) -> str:
    """The netsh arguments (without the executable) that take that exclusion away."""
    return _line("delete", port)


def manual_command(port: int) -> str:
    """The line to run by hand in a PowerShell run as administrator."""
    return f"netsh {add_line(port)}"


def _failure(code: int) -> OSError | ElevationDeclined:
    """The exception for the error code ShellExecuteExW left."""
    if code == _ERROR_CANCELLED:
        return ElevationDeclined()
    return OSError(f"ShellExecuteExW failed ({code})")


def _console_window() -> int | None:
    """The window of the console Latet runs in (GetConsoleWindow), or None without one."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    return kernel32.GetConsoleWindow()


def _execute_info(file: str, parameters: str) -> Any:
    """SHELLEXECUTEINFOW (a structure this function defines, hence Any) for ``file`` with ``parameters``: as
    administrator (``runas``), hidden, its process handle kept, and the console's window as the owner of the UAC
    dialog, so that the dialog comes to the front."""
    if sys.platform != "win32":
        raise OSError("port reservation is for Windows")
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x00000040
    sw_hide = 0
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = see_mask_nocloseprocess
    info.hwnd = _console_window()
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = parameters
    info.nShow = sw_hide
    return info


def _shell_execute(file: str, parameters: str, timeout: float) -> int | None:
    """``file`` with ``parameters`` started as administrator, then waited for: its exit code, or None when it does not
    end within ``timeout``. ShellExecuteExW returns only once the UAC dialog is answered, so ``timeout`` bounds the wait
    for the elevated process (netsh) alone, never the dialog, which waits for you. Raises ElevationDeclined when the
    dialog is declined. The only call into Windows."""
    if sys.platform != "win32":
        raise OSError("port reservation is for Windows")
    import ctypes
    from ctypes import wintypes

    wait_timeout = 0x102
    info = _execute_info(file, parameters)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):  # back once the dialog is answered
        error = ctypes.get_last_error()
        raise _failure(error)
    handle = info.hProcess
    if not handle:
        return None
    try:
        if kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == wait_timeout:
            return None
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value)
    finally:
        kernel32.CloseHandle(handle)


def run_elevated(lines: Sequence[str], timeout: float = 120.0) -> int | None:
    """One elevated process running the netsh ``lines`` one after the other: for one line Windows' own netsh.exe itself,
    which the UAC dialog then names; for several, Windows' own cmd.exe with ``/d`` (no AutoRun commands). Its exit
    code, or None when it does not end within ``timeout`` once the dialog was answered. Raises ElevationDeclined when
    the UAC dialog is declined."""
    if not lines:
        raise ValueError("no netsh line to run")
    folder = ports.system_folder()
    netsh = folder + "\\netsh.exe"
    if len(lines) == 1:
        return _shell_execute(netsh, lines[0], timeout)
    command = " & ".join(f"{netsh} {line}" for line in lines)
    return _shell_execute(folder + "\\cmd.exe", f'/d /c "{command}"', timeout)


def read_record() -> list[int]:
    """The ports Latet reserved; empty when there is no record. Raises RecordUnreadable when the record is there but
    cannot be read, which no record must never stand for: `--remove` would say Latet reserved nothing, and the next
    reservation would write over it."""
    path = data_dir() / RECORD
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:  # a UnicodeDecodeError is a ValueError
        from latet.logline import described

        raise RecordUnreadable(
            f"Latet's record of the ports it reserved, {path}, cannot be read ({described(exc)})"
        ) from None
    unreadable = RecordUnreadable(
        f"Latet's record of the ports it reserved, {path}, cannot be read (not a list of ports)"
    )
    try:
        listed = json.loads(text)["ports"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise unreadable from None
    if not isinstance(listed, list):
        raise unreadable
    found: list[int] = []
    for item in listed:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65535:
            raise unreadable
        found.append(item)
    return found


def recorded() -> list[int]:
    """The ports Latet reserved, for what only lists them: empty when the record is missing or cannot be read
    (``read_record`` tells the two apart). Never raises."""
    try:
        return read_record()
    except RecordUnreadable:
        return []


def _write_record(found: list[int]) -> None:
    folder = make_private_folder(data_dir())
    write_private_where_allowed(folder / RECORD, json.dumps({"ports": found}, indent=1))


def _fresh_ranges() -> tuple[ports.ExcludedRange, ...]:
    """Windows' table read again now, for what changes the record: raises ports.TableUnreadable (an OSError) when it
    cannot be read, which an empty table must never stand for (every port Latet reserved would look gone). The cached
    reader reads it again too."""
    ranges = ports.read_excluded_ranges()
    with contextlib.suppress(AttributeError):
        ports.excluded_ranges.cache_clear()
    return ranges


def _ranges_after() -> tuple[ports.ExcludedRange, ...]:
    """``_fresh_ranges`` once the elevated step ran: a table that cannot be read is an outcome not known."""
    try:
        return _fresh_ranges()
    except ports.TableUnreadable as exc:
        raise OutcomeUnknown(str(exc)) from None


def _held(port: int, ranges: Sequence[ports.ExcludedRange]) -> bool:
    return any(r.administered and r.start <= port <= r.end for r in ranges)


def reserve(port: int, release: Sequence[int] = ()) -> bool:
    """Reserve ``port`` and give up the ``release`` ones in one elevated step. Windows' table is read first: where an
    administered range holds ``port`` already, no line adds it; one Latet did not make (``port`` not on the record)
    raises ReservedAlready before anything runs, so that Latet never records an exclusion of someone else's as its own.
    True when an administered range holds ``port`` afterwards, read fresh whatever the exit code says; the record gains
    it then, and loses each released port no administered range still holds. Lets ElevationDeclined through, and
    ports.TableUnreadable when the table cannot be read before anything runs; raises OutcomeUnknown (a
    ports.TableUnreadable) when it cannot be read afterwards, the record staying as it was; and RecordUnreadable when
    ``port`` was reserved but the record could not be read, which is never written over then."""
    deletes = [delete_line(p) for p in release]
    adding = add_line(port)
    unreadable: RecordUnreadable | None = None
    try:
        before = read_record()
    except RecordUnreadable as exc:
        before, unreadable = [], exc
    ranges = _fresh_ranges()
    if _held(port, ranges):
        if unreadable is not None or port not in before:
            raise ReservedAlready(port)
        lines = deletes  # Latet's own already: only what it gives up
    else:
        lines = [adding, *deletes]
    if lines:
        run_elevated(lines)
        ranges = _ranges_after()
    done = _held(port, ranges)
    if unreadable is not None:
        if done:
            raise unreadable
        return False
    kept = [p for p in before if p not in release or _held(p, ranges)]
    if done and port not in kept:
        kept.append(port)
    if kept != before:
        _write_record(kept)
    return done


def remove(found: Sequence[int]) -> list[int]:
    """Take away each recorded port in ``found`` that an administered range of exactly that one port holds, in one
    elevated step. A recorded one no administered range holds any more (taken away by hand) is gone already: off the
    record first, with no elevation. Returns the ports gone, those after a fresh read, taken off the record. A record
    that cannot be read raises RecordUnreadable, and a table that cannot be read ports.TableUnreadable: read first,
    they change neither the record nor the machine; read after the elevated step, the table is OutcomeUnknown, and the
    ports that step touched stay on the record. A record that cannot be written after the step raises
    RecordNotUpdated, with the ports gone all the same."""
    before = read_record()
    asked = [p for p in found if p in before]
    if not asked:
        return []
    ranges = _fresh_ranges()
    unheld = [p for p in asked if not _held(p, ranges)]
    if unheld:  # off the record even when the dialog below is declined
        before = [p for p in before if p not in unheld]
        _write_record(before)
    ours = [p for p in asked if any(r.administered and r.start == r.end == p for r in ranges)]
    gone: list[int] = []
    if ours:
        run_elevated([delete_line(p) for p in ours])
        ranges = _ranges_after()
        gone = [p for p in ours if not _held(p, ranges)]
        if gone:
            try:
                _write_record([p for p in before if p not in gone])
            except OSError as exc:  # taken away all the same: never "nothing taken away"
                raise RecordNotUpdated([p for p in asked if p in unheld or p in gone], exc) from None
    return [p for p in asked if p in unheld or p in gone]
