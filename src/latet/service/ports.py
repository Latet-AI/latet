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
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

FALLBACK_PORTS = range(5284, 5336)
_IN_USE = {errno.EADDRINUSE, 10048}  # 10048: WSAEADDRINUSE
_DENIED = {errno.EACCES, 10013}  # 10013: WSAEACCES (also an excluded range on Windows)


@dataclass(frozen=True, slots=True)
class PortState:
    port: int
    usable: bool
    reason: str  # "free", "latet", "in use", "reserved by Windows (a-b)", "not allowed"


class NoFreePortError(Exception):
    pass


@cache
def excluded_ranges() -> tuple[tuple[int, int], ...]:
    """TCP port ranges Windows keeps for itself (IPv4 and IPv6); none on other systems."""
    if os.name != "nt":
        return ()
    found: set[tuple[int, int]] = set()
    for family in ("ipv4", "ipv6"):
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv
                ["netsh", "int", family, "show", "excludedportrange", "protocol=tcp"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found.update((int(a), int(b)) for a, b in re.findall(r"^\s*(\d+)\s+(\d+)", out, re.MULTILINE))
    return tuple(sorted(found))


def _reserved(port: int) -> tuple[int, int] | None:
    return next(((a, b) for a, b in excluded_ranges() if a <= port <= b), None)


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
    reserved = _reserved(port)
    if reserved is not None:
        return PortState(port, False, f"reserved by Windows ({reserved[0]}-{reserved[1]})")
    error = _bind_error(port)
    if error is None and not _listened(port):
        return PortState(port, True, "free")
    if error is None or error in _IN_USE:
        from latet.service import daemon

        if daemon.status(port).running:
            return PortState(port, True, "latet")
        return PortState(port, False, "in use by another program")
    return PortState(port, False, "not allowed")


def choose(preferred: int, fallback: Iterable[int] = FALLBACK_PORTS) -> PortState:
    """The preferred port if usable, else the first usable fallback."""
    for port in (preferred, *(p for p in fallback if p != preferred)):
        state = probe(port)
        if state.usable:
            return state
    raise NoFreePortError(f"no usable port among {preferred} and {min(fallback)}-{max(fallback)}")
