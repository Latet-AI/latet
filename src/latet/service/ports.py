"""Can the daemon listen on its port? Free, Latet already, another program, or reserved by Windows.

5283 is unassigned in the IANA registry and not used by common software, but Windows (Hyper-V,
WSL, Docker) reserves blocks of ports that change from one boot to the next. ``latet
global-install`` moves to the next free port when 5283 cannot be used; ``latet start`` never moves it on its own,
because the agents are configured with the port in use.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import sys
from collections.abc import Collection
from dataclasses import dataclass
from functools import cache
from typing import Literal

_IN_USE = {errno.EADDRINUSE, 10048}  # 10048: WSAEADDRINUSE
_DENIED = {errno.EACCES, 10013}  # 10013: WSAEACCES (also an excluded range on Windows)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows only: netsh opens no console window


@dataclass(frozen=True, slots=True)
class PortState:
    port: int
    usable: bool
    reason: str  # "free", "latet", "in use", "reserved by Windows (a-b)", "not allowed"


class NoFreePortError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExcludedRange:
    start: int
    end: int
    administered: bool  # reserved by an administrator (netsh's `*`), not by Windows itself


Reservation = Literal["reserved", "held", "at-risk", "safe"]


class TableUnreadable(OSError):
    """Windows' table of excluded ports could not be read: netsh could not be run, failed, or printed no table."""


_ROW = re.compile(r"^\s*(\d+)\s+(\d+)(\s+\*)?\s*$", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*-+[ \t]+-+[ \t]*$", re.MULTILINE)  # the line under the table's headings, with no row too


def system_folder() -> str:
    """The system folder as Windows itself gives it (GetSystemDirectoryW), where Windows' own netsh.exe and cmd.exe
    are: never from an environment variable, which any program of the user's can set (and then approve its own cmd.exe
    in the UAC dialog), nor a bare name, which Windows looks for in the program's and the current folder first. Raises
    OSError off Windows, or when Windows does not say."""
    if sys.platform != "win32":
        raise OSError("Windows' system folder is there on Windows only")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise OSError("The Windows system directory cannot be determined.")
    return buffer.value.rstrip(chr(92))


def _run_netsh(*args: str) -> subprocess.CompletedProcess[str]:
    """Windows' own netsh.exe run with ``args``, with no console window (the windowless daemon's Port check reads these
    tables); raises OSError or SubprocessError when it cannot be run or does not end in time."""
    return subprocess.run(  # noqa: S603 - fixed argv, netsh.exe from Windows' own system folder
        [system_folder() + "\\netsh.exe", "int", "ipv4", *args],
        capture_output=True,
        text=True,
        errors="replace",  # netsh writes the console's OEM code page; only digits, * and - are read
        timeout=10,
        check=False,
        creationflags=_NO_WINDOW,
    )


def _netsh(*args: str) -> str:
    try:
        return _run_netsh(*args).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ranges(text: str) -> tuple[ExcludedRange, ...]:
    rows = _ROW.findall(text)
    return tuple(sorted((ExcludedRange(int(a), int(b), bool(star)) for a, b, star in rows), key=lambda r: r.start))


@cache
def excluded_ranges() -> tuple[ExcludedRange, ...]:
    """IPv4 TCP port ranges excluded on Windows, Windows' own and an administrator's; none on other systems, nor when
    netsh cannot be read. Never raises: for ``probe`` and ``reservation``, which only read."""
    if os.name != "nt":
        return ()
    return _ranges(_netsh("show", "excludedportrange", "protocol=tcp"))


def read_excluded_ranges() -> tuple[ExcludedRange, ...]:
    """The same table read again now, for a decision that changes something (what Latet records it reserved): raises
    TableUnreadable off Windows, and when netsh cannot be run, fails or prints no table. An empty tuple then means that
    no range is there, never that the table could not be read."""
    if os.name != "nt":
        raise TableUnreadable("Windows' table of excluded ports is read on Windows only")
    try:
        done = _run_netsh("show", "excludedportrange", "protocol=tcp")
    except (OSError, subprocess.SubprocessError) as exc:
        raise TableUnreadable(f"netsh could not be run: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise TableUnreadable(f"netsh exited with code {done.returncode}")
    if not _RULE.search(done.stdout or ""):
        raise TableUnreadable("netsh printed no table of excluded ports")
    return _ranges(done.stdout)


@cache
def dynamic_range() -> tuple[int, int] | None:
    """The first and last port Windows gives to outgoing connections; None off Windows or when unreadable."""
    if os.name != "nt":
        return None
    numbers = re.findall(r"\d+", _netsh("show", "dynamicport", "tcp"))[:2]
    if len(numbers) < 2 or int(numbers[1]) < 1:
        return None
    first = int(numbers[0])
    return first, first + int(numbers[1]) - 1


def _windows_range(port: int) -> ExcludedRange | None:
    """The range of Windows' own that holds the port; an administrator's never does."""
    return next((r for r in excluded_ranges() if not r.administered and r.start <= port <= r.end), None)


def reservation(port: int) -> Reservation:
    """reserved: an administrator's range holds it; held: one of Windows' own; at-risk: Windows may hand it to an
    outgoing connection at boot; safe: none of these."""
    if os.name != "nt":
        return "safe"
    ranges = excluded_ranges()
    if any(r.administered and r.start <= port <= r.end for r in ranges):
        return "reserved"
    if _windows_range(port) is not None:
        return "held"
    dynamic = dynamic_range()
    if dynamic is not None and dynamic[0] <= port <= dynamic[1]:
        return "at-risk"
    return "safe"


def _bind_error(port: int) -> int | None:
    """None when the port can be bound on 127.0.0.1 as the daemon binds it, else the error number: on POSIX with
    SO_REUSEADDR, as uvicorn does, so that the closed connections of a daemon just stopped (TIME_WAIT) are no conflict
    while a program listening there still is; on Windows exclusively, where SO_REUSEADDR would share a bound port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return exc.errno if exc.errno is not None else -1
    finally:
        sock.close()
    return None


def _listened(port: int) -> bool:
    """Whether a program accepts connections on 127.0.0.1:port. A bind there succeeds beside a program that listens on
    every address (0.0.0.0, ::) on macOS with SO_REUSEADDR, and on Windows even an exclusive one: the daemon would then
    take that program's connections to 127.0.0.1. Closed connections (TIME_WAIT) accept none. The system answers a
    connection to this computer at once; a refused one takes Windows about two seconds, given up at a quarter of one.
    A connection to itself is no program: where the port is among the system's own for outgoing connections (on
    Windows they may start at 1024), the probe may be given that very port and meet itself."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return False
        return sock.getsockname() != sock.getpeername()


def probe(port: int) -> PortState:
    held = _windows_range(port)
    if held is not None:
        return PortState(port, False, f"reserved by Windows ({held.start}-{held.end})")
    error = _bind_error(port)
    if error is None and not _listened(port):
        return PortState(port, True, "free")
    if error is None or error in _IN_USE:
        from latet.service import daemon

        if daemon.status(port).running:
            return PortState(port, True, "latet")
        return PortState(port, False, "in use by another program")
    return PortState(port, False, "not allowed")


def free_port(start: int, below: int = 49152, ours: Collection[int] = ()) -> PortState:
    """The first port from `start` up, below `below`, that no excluded range holds and that is usable: past Windows'
    own ranges, and past an administrator's too, which is someone's reservation (Latet's new port would then be
    recorded as Latet's own), unless the port is one of ``ours``, the ports Latet's record says it reserved."""
    port = start
    while port < below:
        held = next(
            (r for r in excluded_ranges() if r.start <= port <= r.end and not (r.administered and port in ours)),
            None,
        )
        if held is not None:
            later = (p for p in ours if port < p <= held.end) if held.administered else ()
            port = min(later, default=held.end + 1)
            continue
        state = probe(port)
        if state.usable:
            return state
        port += 1
    raise NoFreePortError(f"no usable port from {start} up to {below - 1}")


def choose(preferred: int, ours: Collection[int] = ()) -> PortState:
    """The preferred port if usable, else the first free one above it (``free_port``, with ``ours``)."""
    state = probe(preferred)
    return state if state.usable else free_port(preferred + 1, ours=ours)
