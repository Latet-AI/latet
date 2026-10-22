"""``latet tutorial``: a sandbox on this computer, opened in the browser, gone when it ends.

The command makes a temporary folder, holds a free port, runs the sandbox's server in this process (the environment
switched to the sandbox's for as long as it runs), opens the browser on it through a one-time sign-in link, and cleans
up when you press Ctrl+C or the page ends it. A run that stops on an error prints the last lines of the server's log,
and keeps the folder for that log until the next run. Your own daemon, data folder and settings are never touched:
the one file written there is `tutorial.json`, the ids of the chapters you finished. Two things are read there: that
file, for the chapters the menu and `--list` show as done, and your text recognition models (`latet ocr install`),
which a chapter on images needs: the sandbox looks for them in your data folder each time, as they may be installed
while the tutorial runs.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from latet.cli.main import app

if TYPE_CHECKING:
    import uvicorn

    from latet.tutorial.chapters import Chapter
    from latet.tutorial.progress import Progress
    from latet.tutorial.sandbox import Finder

PREFIX = "latet-tutorial-"  # a sandbox's folder, in the temporary folder
PID_FILE = "sandbox.pid"  # the command that made it, so that what a cut-off run left can be told from what runs
LOG_FILE = "tutorial.log"  # the server's warnings and errors, in the sandbox's folder
TAIL = 20  # the lines of its log a run that stopped on an error prints
WEB = Path(__file__).resolve().parent.parent / "web"  # the dashboard's files, where the server looks for them


@contextlib.contextmanager
def sandbox_env(root: Path, port: int) -> Iterator[None]:
    """The sandbox's environment (``latet.tutorial.sandbox.environment``) for as long as the block runs: its own data
    folder, settings and port, none of your keys or tokens. What it was comes back, an unset variable unset."""
    from latet.tutorial.sandbox import environment

    changes = environment(root, port)
    before = {name: os.environ.get(name) for name in changes}

    def put(values: dict[str, str | None]) -> None:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    try:
        put(changes)  # Ctrl+C can come halfway: what was switched by then is put back too
        yield
    finally:
        put(before)


def alive(pid: int) -> bool:
    """Whether process ``pid`` is there (`os.kill(pid, 0)` would end it on Windows). When it cannot be told, yes."""
    if pid <= 0 or pid > 0xFFFFFFFF:  # no process has that number (Windows would read it cut down to another's)
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it is there, and not yours
        try:
            return kernel32.WaitForSingleObject(handle, 0) != 0  # 0: it has ended
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep(temp: Path) -> None:
    """Delete the sandboxes an earlier run left in ``temp`` (it was killed, or its window closed): those that name a
    command that is gone. A kept one (``--keep``) names none, and one that is not readable is left, as is any other
    folder."""
    with contextlib.suppress(OSError):
        for folder in temp.glob(f"{PREFIX}*"):
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                pid = int((folder / PID_FILE).read_text("utf-8").strip())
                gone = not alive(pid)
            except (OSError, ValueError, OverflowError):
                continue
            if gone:
                shutil.rmtree(folder, ignore_errors=True)


def _hold_a_port() -> socket.socket:
    """A socket bound to a free port of this computer's loopback, held until the server takes it: no other program can
    take the port in between. Never `ports.choose()`: that can pick the port of your own daemon."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":  # Windows lets a program that asks (SO_REUSEADDR) share a port that is bound
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
    except OSError:
        sock.close()
        raise
    return sock


def _delete(root: Path, attempts: int = 10, pause: float = 0.3) -> bool:
    """Delete ``root``, trying again: Windows holds a file a moment after its program let it go (an antivirus, the
    indexer). Whether it is gone."""
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause)
        try:
            shutil.rmtree(root)
        except OSError:
            if not root.exists():
                return True
        else:
            return True
    return False


def _tail(log: Path) -> list[str]:
    """The last lines of ``log``: none when it is empty, or cannot be read."""
    try:
        return log.read_text("utf-8", errors="replace").splitlines()[-TAIL:]
    except OSError:
        return []


@contextlib.contextmanager
def _logging_to(path: Path) -> Iterator[None]:
    """The warnings and errors of the server, and of everything under it, go to ``path``, not to the terminal: uvicorn's
    own too, whoever set its loggers up before (its default setup keeps them from the root logger). The file is let go
    of at the end (Windows deletes no folder that holds a file open)."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root, uvicorn_log = logging.getLogger(), logging.getLogger("uvicorn")
    propagate = uvicorn_log.propagate
    root.addHandler(handler)
    uvicorn_log.propagate = True
    try:
        yield
    finally:
        uvicorn_log.propagate = propagate
        root.removeHandler(handler)
        handler.close()


def _when_started(server: uvicorn.Server, ended: threading.Event, then: Callable[[], None]) -> None:
    """Call ``then`` once the server answers; not at all when the run ends first."""
    while not server.started:
        if ended.wait(0.05):
            return
    then()


def run_sandbox(
    root: Path,
    port: int,
    *,
    requested: str | None,
    progress: Progress,
    announce: Callable[[str], None],
    sock: socket.socket,
    reader: Finder | None = None,
) -> None:
    """Make the sandbox in ``root`` and serve it on ``port`` until it ends: Ctrl+C (the server shuts down, then
    KeyboardInterrupt is raised again) or the page's end. Run it in `sandbox_env(root, port)`. ``sock``: the socket
    bound to ``port``, handed to the server (no other program can take the port meanwhile). ``requested``: the chapter
    named on the command line. ``announce(link)``: called with the sign-in link once the server answers. ``reader``:
    finds a reader of your text recognition models, which the sandbox asks for while it runs (none: no chapter that
    needs them can start). The runtime is closed when this ends, however it ends: only then can the folder be
    deleted."""
    from latet.api.rest import UI
    from latet.cli.main import uvicorn_server
    from latet.gateway.app import create_app
    from latet.tutorial.sandbox import Sandbox

    with _logging_to(root / LOG_FILE):
        sandbox = Sandbox.create(root, port, progress=progress, requested=requested, reader=reader)
        try:
            gateway = sandbox.runtime.gateway
            server = uvicorn_server(create_app(gateway), "127.0.0.1", port, "warning", log_config=None)
            # The code for the link is made here, before the server runs, and works once, for two minutes.
            link = f"http://127.0.0.1:{port}{UI}/login?code={gateway.dashboard.issue_code()}"
            ended = threading.Event()
            threading.Thread(target=_when_started, args=(server, ended, lambda: announce(link)), daemon=True).start()
            try:
                server.run(sockets=[sock])
            finally:
                ended.set()
        finally:
            sandbox.close()


def _announcer(port: int, print_url: bool) -> Callable[[str], None]:
    """What to do with the sign-in link once the server answers: open the browser on it, or print it."""

    def announce(link: str) -> None:
        try:
            opened = not print_url and webbrowser.open(link)
        except (webbrowser.Error, OSError):
            opened = False
        if not opened:
            typer.echo(link)
            typer.echo("The link works once, for two minutes.", err=True)
        typer.echo(f"Tutorial at http://127.0.0.1:{port} — Ctrl+C ends it")

    return announce


def _list(written: dict[str, Chapter], done: list[str]) -> None:
    from latet.tutorial.chapters import ORDER

    rows = [
        (
            str(ORDER.index(cid) + 1),
            cid,
            chapter.title,
            f"{chapter.minutes} min",
            "done" if cid in done else "to do",
        )
        for cid, chapter in written.items()
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    for row in rows:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    typer.echo()
    typer.echo(f"`latet tutorial {next(iter(written))}` opens one; `latet tutorial` opens the menu.")


@app.command()
def tutorial(
    chapter: Annotated[
        str | None, typer.Argument(help="The chapter to open first, such as basics (`--list` shows them).")
    ] = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the sandbox's folder when it ends, and say where it is.")
    ] = False,
    list_chapters: Annotated[
        bool, typer.Option("--list", help="Print the chapters and which you finished, then stop.")
    ] = False,
    print_url: Annotated[
        bool, typer.Option("--print-url", help="Print the sign-in link instead of opening it in the browser.")
    ] = False,
) -> None:
    """See Latet at work in your browser, on a sandbox of its own with invented data.

    A pretend agent talks to a pretend model through the real Latet, and the dashboard shows what it did, chapter
    by chapter. Nothing leaves your computer. Your own settings, vault and history are never touched: the sandbox is
    a temporary folder on a port of its own, deleted when you press Ctrl+C or end it in the page. The one file
    written outside it is `tutorial.json` in your data folder: the ids of the chapters you finished.
    """
    from latet.cli.admin import _host_only
    from latet.config import data_dir
    from latet.tutorial.chapters import ChapterError, load_all
    from latet.tutorial.progress import Progress
    from latet.tutorial.sandbox import SandboxError, user_reader

    _host_only("The tutorial", "Install Latet on your computer, and run `latet tutorial` there.")
    try:
        written = load_all()
    except ChapterError as exc:
        typer.secho(f"The tutorial's chapters cannot be read: {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    progress = Progress(data_dir() / "tutorial.json")  # yours: taken here, before the sandbox's folders replace it
    reader = user_reader(data_dir())  # your text recognition models: looked for there while the tutorial runs
    if list_chapters:
        _list(written, progress.done())
        return
    if chapter is not None and chapter not in written:
        typer.secho(f'There is no chapter "{chapter}". The chapters: {", ".join(written)}.', fg="red", err=True)
        raise typer.Exit(2)
    if not (WEB / "index.html").is_file():
        typer.secho(
            "The dashboard is not built, so the tutorial's page will not show: run `npm run build` in dashboard/ "
            "first (an installed Latet has it built).",
            fg="yellow",
            err=True,
        )
    temp = Path(tempfile.gettempdir())
    _sweep(temp)
    root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=temp)).resolve()
    sock: socket.socket | None = None
    ended = refused = False
    try:
        (root / PID_FILE).write_text(str(os.getpid()), "utf-8")
        sock = _hold_a_port()
        port = sock.getsockname()[1]
        with sandbox_env(root, port):
            run_sandbox(
                root,
                port,
                requested=chapter,
                progress=progress,
                announce=_announcer(port, print_url),
                sock=sock,
                reader=reader,
            )
        ended = True
    except KeyboardInterrupt:
        ended = True  # Ctrl+C: the server shut down first, then raised it again; the runtime is closed
    except SandboxError as exc:
        refused = True
        typer.secho(f"The sandbox could not be made: {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    finally:
        if sock is not None:
            sock.close()
        if ended:
            typer.echo("Tutorial ended")
        # Stopped on an error (uvicorn exits when the server cannot start; an exception): what its log says.
        tail = [] if ended or refused else _tail(root / LOG_FILE)
        if tail:
            typer.secho("The tutorial stopped on an error. The last lines of its log:", fg="red", err=True)
            for line in tail:
                typer.echo(f"  {line}", err=True)
        if keep:
            (root / PID_FILE).unlink(missing_ok=True)  # kept on purpose: not a leftover for the next run to delete
            typer.echo(f"The sandbox is kept in {root}. Delete the folder when you are done with it.")
        elif tail:  # kept for its log; the next run deletes it, as its sandbox.pid names a command gone by then
            typer.echo(f"The whole log is in {root / LOG_FILE}, until you run `latet tutorial` again.", err=True)
        elif not _delete(root):
            typer.secho(f"Could not delete {root}: delete the folder yourself.", fg="yellow", err=True)
