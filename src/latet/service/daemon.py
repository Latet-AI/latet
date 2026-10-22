"""Running the gateway as a background daemon: start (detached), stop, status, and the
windowless ``latet-daemon`` entry point used by the login autostart."""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latet.config import data_dir, load_config
from latet.storage.folders import make_private_folder

PID_FILE = "daemon.pid"


@dataclass(slots=True)
class Status:
    running: bool
    version: str | None = None
    pid: int | None = None
    port: int = 5283
    masking: bool = False
    reason: str | None = None  # why it is not running, when known
    exited: int | None = None  # the exit code of a daemon that stopped while starting
    waiting: int = 0  # batches waiting for you in the Live Reviewer
    viewers: int = 0  # dashboards watching them
    unreadable: int = 0  # vault values the daemon's root key cannot open


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the refusal it is (an HTTPError): followed, it would carry the token to wherever it points."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def local_urlopen(request: urllib.request.Request | str, timeout: float) -> Any:
    """A call of the command line's to this computer's daemon, never through a proxy whatever HTTP_PROXY, HTTPS_PROXY
    and NO_PROXY say (urllib's own opener sends 127.0.0.1 to a proxy unless NO_PROXY names it), and following no
    redirect (urllib's own follows one to another host, the token with it): the bridge token it carries would reach
    either."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def status(port: int | None = None) -> Status:
    port = port or load_config().port
    pid = None
    pid_path = data_dir() / PID_FILE
    if pid_path.exists():
        with contextlib.suppress(ValueError, OSError):
            pid = int(pid_path.read_text().strip())
    # A closed port takes ~1-2 s to refuse on Windows: probe the socket briefly first.
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
    except OSError:
        return Status(False, pid=pid, port=port)
    try:
        with local_urlopen(_url(port, "/_latet/health"), timeout=2) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return Status(False, pid=pid, port=port)
    live = data.get("live") or {}
    return Status(
        True, data.get("version"), pid, port, bool(data.get("masking")),
        waiting=int(live.get("waiting") or 0), viewers=int(live.get("viewers") or 0),
        unreadable=int((data.get("vault") or {}).get("unreadable") or 0),
    )  # fmt: skip


def _daemon_command() -> list[str]:
    """The windowless launcher when installed, else this interpreter (pythonw on Windows)."""
    launcher = shutil.which("latet-daemon")
    if launcher:
        return [launcher]
    python = Path(sys.executable)
    if os.name == "nt":
        windowless = python.with_name("pythonw.exe")
        if windowless.exists():
            python = windowless
    return [str(python), "-m", "latet.service.daemon"]


# Folders the daemon reads from its environment: a relative one is made absolute for it, as the
# daemon runs in its own folder.
_PATH_VARIABLES = (
    "LATET_DATA_DIR", "LATET_CONFIG", "LATET_ROOT_KEY_FILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "XDG_CONFIG_HOME",
)  # fmt: skip


def _daemon_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _PATH_VARIABLES:
        if env.get(name):
            env[name] = str(Path(env[name]).resolve())
    return env


def start(wait: float = 15.0, patience: float = 90.0, slow: Callable[[], None] | None = None) -> Status:
    """Start the daemon detached; with ``wait=0`` return as soon as it is launched.

    A daemon still starting after ``wait`` seconds is waited for up to ``patience`` seconds in all:
    the first start after an install compiles Python's bytecode, and an antivirus checks the new
    files. ``slow`` is called once then, to say so. One that stops while starting is told at once."""
    from latet.service import ports
    from latet.storage.backup import restoring

    current = status()
    if current.running:
        return current
    if restoring(data_dir()):
        return Status(False, pid=current.pid, port=current.port, reason=RESTORING)
    state = ports.probe(current.port)
    if not state.usable:  # answer at once instead of waiting for a server that cannot bind
        return Status(False, pid=current.pid, port=current.port, reason=state.reason)
    log_dir = make_private_folder(data_dir() / "logs")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    # In a folder of its own: started from a project, the daemon would hold that folder, and Windows
    # would not let you rename or delete it while the daemon runs.
    kwargs["cwd"] = log_dir.resolve()
    kwargs["env"] = _daemon_env()
    out = (log_dir / "daemon.out").open("ab")
    process = subprocess.Popen(_daemon_command(), stdout=out, stderr=out, **kwargs)  # noqa: S603 - fixed argv
    if wait <= 0:
        return status()
    started = time.monotonic()
    told = False
    while (elapsed := time.monotonic() - started) < max(wait, patience):
        current = status()
        if current.running:
            return current
        code = process.poll()
        if code is not None:
            return Status(False, pid=current.pid, port=current.port, exited=code)
        if elapsed >= wait and not told:
            told = True
            if slow is not None:
                slow()
        time.sleep(0.25)
    return status()


def stop(timeout: float = 10.0) -> bool:
    """Ask the daemon to exit; kill it by PID if it does not."""
    from latet.storage.keys import load_bridge_token

    port = load_config().port
    token = load_bridge_token(data_dir())
    request = urllib.request.Request(  # noqa: S310 - fixed local URL
        _url(port, "/_latet/admin/shutdown"), method="POST", headers={"x-latet-token": token}
    )
    with contextlib.suppress(urllib.error.URLError, OSError):
        local_urlopen(request, timeout=3).close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not status(port).running:
            break
        time.sleep(0.2)
    current = status(port)
    if current.running and current.pid:
        _kill(current.pid)
        time.sleep(0.5)
    with contextlib.suppress(OSError):
        (data_dir() / PID_FILE).unlink()
    return not status(port).running


RESTORING = "a restore is under way: finish it, or `latet restore --abort`"


def wait_exit(pid: int, timeout: float = 15.0) -> bool:
    """Wait for process ``pid`` to end (its files closed); whether it did within ``timeout``."""
    if sys.platform == "win32":  # there os.kill(pid, 0) would end it
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True  # gone already
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # someone else's: still there
        time.sleep(0.1)
    return False


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)  # noqa: S603,S607
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)


def configure_file_logging() -> Path:
    """Under pythonw there is no console (sys.stdout is None): log to a rotating file instead."""
    log_dir = make_private_folder(data_dir() / "logs")
    path = log_dir / "daemon.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - process-lifetime sink
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    return path


def main() -> None:
    """``latet-daemon``: the gateway in the background, logging to a file."""
    configure_file_logging()
    from latet.cli.main import run_server

    run_server(log_config=None, background=True)


if __name__ == "__main__":
    main()
