"""The repositories a computer remembers, read within deadlines: by one helper process at a time (``repository_child``).

A remembered repository's latet.yaml is read again at every start and reload (``projects.SeenRepositories``), and one
on a share that no longer answers, or a drive that is gone without the system saying so, can make a read wait for as
long as the system waits: minutes, sometimes for ever. Neither a thread nor a timeout ends such a read. So the reads run
in a helper process the daemon starts for one check and ends at its deadlines: ready within ``READY_SECONDS``, each
folder within ``ITEM_SECONDS`` of the one before, the whole check within ``TOTAL_SECONDS``, and ``CLEANUP_SECONDS`` more
to end the helper and the thread reading its answers. What it answered past a deadline counts for nothing: the check is
incomplete, which never counts as nothing to weigh (``SeenRepositories.weighed``).

One helper at a time in the whole process, with one thread reading its answers, and nobody waiting in line: a check
asked while another runs is answered incomplete at once (``busy``). A helper the system cannot end (a read stuck in the
kernel) keeps its place, and every later check is answered incomplete at once, until the system lets it go: no other
helper is started meanwhile. Nothing here reads a repository itself.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, NamedTuple, Protocol

from pydantic import ValidationError

from latet.config import ProjectSettings
from latet.project_file import REMEMBERED_KINDS, Remembered
from latet.repository_child import (
    LIMIT_FAILED,
    PROTOCOL,
    WHY_LONGEST,
    ProtocolError,
    manifest,
    read_frame,
    read_sized,
)

READY_SECONDS = 5.0  # the helper started and ready: counted in the whole check
ITEM_SECONDS = 2.0  # each folder's answer, from the one before (or from ready): never a new whole budget
TOTAL_SECONDS = 10.0  # the whole check, from its admission
CLEANUP_SECONDS = 1.0  # ending the helper and its reading thread, past the check's own deadline
RESULTS_MOST = 16 * 1024 * 1024  # what a check's answers weigh at most, all frames counted
_TICK = 0.05  # how often a thread waiting to hand over a frame looks whether the check is over


class Child(Protocol):
    """The helper process as the check uses it (``subprocess.Popen``'s; a test's own)."""

    stdin: IO[bytes] | None
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class ScanOutcome(NamedTuple):
    """A check of remembered repositories: each folder's answer in the order given (None: not answered: the check
    stopped before it, or the answer came past a deadline), whether every one was answered by a helper that ended
    well (``complete``), and why not, in fixed words (``failure``)."""

    results: tuple[Remembered | None, ...]
    complete: bool
    failure: str | None = None
    # Whether the helper said its memory was capped (macOS: False, deadline-only, R23); None: no helper said anything.
    capped: bool | None = None


# A helper's exit code as its failure is said (M5): fixed words, never what it wrote.
_EXITS = {LIMIT_FAILED: "memory cap failed", 2: "helper refused (its own Latet or the check)"}


def _child_environment() -> dict[str, str]:
    """What the helper runs with: what the interpreter needs to start, nothing of the user's (no variable of Python's,
    of a proxy, a credential, Latet's own)."""
    if os.name != "nt":
        return {}
    windows = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    return {"SystemRoot": windows, "WINDIR": windows}


def _launch() -> Child:
    """The helper started: this Latet's interpreter (``sys.executable``), isolated (``-I``, no bytecode written), the
    helper's file from where this Latet is installed, in its folder, with no window. Never a program a path, a variable
    or a request names."""
    if not sys.executable:
        raise OSError("no interpreter to start the helper with")
    helper = Path(__file__).with_name("repository_child.py")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(  # noqa: S603 - a fixed command: this interpreter and this helper
        [sys.executable, "-I", "-B", str(helper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(helper.parent),
        env=_child_environment(),
        close_fds=True,
        creationflags=flags,
    )


# How a helper is started: ``_launch``. Tests put a helper of their own here; nothing a person or a request sends does.
launch: Callable[[], Child] = _launch

_EOF = object()
_BROKEN = object()


@dataclass(eq=False)
class _Running:
    """One helper and the thread that talks to it: the only one, while it holds the place."""

    child: Child
    frames: queue.Queue[Any] = field(default_factory=lambda: queue.Queue(maxsize=1))
    over: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def ended(self) -> bool:
        """Whether the helper and its thread are both gone, as proved: the process reaped, the thread finished."""
        try:
            gone = self.child.poll() is not None
        except OSError:
            gone = False
        return gone and (self.thread is None or not self.thread.is_alive())

    def hand(self, frame: Any) -> bool:
        """``frame`` handed to the check, one at a time; False once the check is over (nobody takes it)."""
        while not self.over.is_set():
            try:
                self.frames.put(frame, timeout=_TICK)
                return True
            except queue.Full:
                continue
        return False

    def talk(self, command: bytes, budget: int) -> None:
        """The thread's work, and nothing else: wait for ready, give the check, hand each frame over, within
        ``budget`` bytes in all. It reads no repository: only the helper's pipes."""
        stdout, stdin = self.child.stdout, self.child.stdin
        if stdout is None or stdin is None:
            self.hand(_BROKEN)
            return
        try:
            try:
                ready = read_frame(stdout)
                if not self.hand(ready if ready is not None else _EOF) or ready is None:
                    return
                stdin.write(command)
                stdin.flush()
            finally:  # its only writer: the helper reads no more than the one command, or nothing
                with contextlib.suppress(OSError, ValueError):
                    stdin.close()
            used = 0
            while True:
                frame, size = read_sized(stdout)
                if frame is None:
                    self.hand(_EOF)
                    return
                used += size
                if used > budget:
                    self.hand(_BROKEN)
                    return
                if not self.hand(frame):
                    return
        except (OSError, ValueError, ProtocolError):
            self.hand(_BROKEN)

    def next(self, deadline: float, clock: Callable[[], float]) -> Any:
        """The next frame, waited for until ``deadline`` at most; None past it."""
        left = deadline - clock()
        if left <= 0:
            return None
        try:
            return self.frames.get(timeout=left)
        except queue.Empty:
            return None

    def end(self, kill: bool, seconds: float, clock: Callable[[], float]) -> bool:
        """The helper ended (killed when ``kill``, or once it failed to end by itself) and its thread with it, within
        ``seconds``: whether both are proved gone. Never waits longer."""
        self.over.set()
        deadline = clock() + seconds
        if kill:
            with contextlib.suppress(OSError):
                self.child.kill()
        try:
            self.child.wait(timeout=max(0.0, deadline - clock()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                self.child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                self.child.wait(timeout=max(0.0, deadline - clock()))
        except OSError:
            pass
        if self.thread is not None:
            self.thread.join(max(0.0, deadline - clock()))
        if self.ended():
            for pipe in (self.child.stdin, self.child.stdout):
                with contextlib.suppress(OSError, ValueError):
                    if pipe is not None:
                        pipe.close()
            return True
        return False


def _remembered(frame: dict[str, Any], index: int) -> Remembered:
    """A folder's answer, as the check takes it: its place the next one, its shape the check's, what it sets valid
    settings (``ProtocolError`` otherwise: the check is incomplete)."""
    if not set(frame) <= {"i", "kind", "settings", "why"} or frame.get("i") != index:
        raise ProtocolError("an answer out of order")
    kind, why = frame.get("kind"), frame.get("why", "")
    if kind not in REMEMBERED_KINDS or not isinstance(why, str) or len(why) > WHY_LONGEST:
        raise ProtocolError("an answer of another shape")
    if kind != "settings":
        if "settings" in frame:
            raise ProtocolError("an answer of another shape")
        return Remembered(kind, why=why)
    data = frame.get("settings")
    if not isinstance(data, dict):
        raise ProtocolError("an answer of another shape")
    try:
        return Remembered("settings", ProjectSettings.model_validate(data))
    except ValidationError:
        raise ProtocolError("settings that are not valid") from None


class RepositoryReader:
    """The place of the one helper of the process (``launch``). ``scan`` checks folders within the deadlines."""

    def __init__(
        self,
        *,
        ready: float = READY_SECONDS,
        item: float = ITEM_SECONDS,
        total: float = TOTAL_SECONDS,
        cleanup: float = CLEANUP_SECONDS,
        results_most: int = RESULTS_MOST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ready, self._item, self._total, self._cleanup = ready, item, total, cleanup
        self._results_most = results_most
        self._clock = clock
        self._place = threading.Lock()
        self._held: _Running | None = None  # a helper not proved gone: it keeps the place until it is
        self._running: _Running | None = None  # the helper of the check under way

    def stop(self) -> None:
        """The daemon closing: a helper still at work, or one kept for not being gone, killed, waited for no longer
        than ``CLEANUP_SECONDS`` (ER.4). Its place stays as it is: a check after this is answered as before."""
        for running in (self._running, self._held):
            if running is None:
                continue
            running.over.set()
            with contextlib.suppress(OSError):
                running.child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                running.child.wait(timeout=self._cleanup)

    def scan(self, folders: Sequence[str]) -> ScanOutcome:
        """Each folder's latet.yaml, read by the helper: incomplete at once when the place is taken (another check, or
        a helper that could not be ended yet), and whatever deadline passes."""
        if not folders:
            return ScanOutcome((), True)
        started = self._clock()
        try:
            command = manifest(list(folders))
        except ProtocolError:
            return ScanOutcome((None,) * len(folders), False, "not a list of folders")
        if not self._place.acquire(blocking=False):
            return ScanOutcome((None,) * len(folders), False, "busy")
        try:
            if self._held is not None:
                if not self._held.ended():
                    return ScanOutcome((None,) * len(folders), False, "busy")
                self._held = None
            try:
                running = _Running(launch())
            except (OSError, ValueError):
                return ScanOutcome((None,) * len(folders), False, "helper")
            running.thread = threading.Thread(
                target=running.talk, args=(command, self._results_most), name="latet-repositories", daemon=True
            )
            self._running = running
            running.thread.start()
            results, failure, capped = self._answers(running, len(folders), started)
            # Past a deadline it is killed at once; one that stopped answering by itself is given the cleanup's time to
            # end (and say why by its exit code) before it is killed.
            if not running.end(failure == "timeout", self._cleanup, self._clock):
                self._held = running  # the system has not let it go: nothing else starts until it does
                failure = failure or "helper"
            else:
                # Said by the helper's own exit code (never one a kill gives: 1 on Windows, a signal elsewhere).
                code = running.child.poll()
                if failure is None and code != 0:
                    failure = _EXITS.get(code, "helper failed") if isinstance(code, int) else "helper failed"
                elif failure == "helper" and code in _EXITS:
                    failure = _EXITS[code]
            self._running = None
            padded = (*results, *(None,) * (len(folders) - len(results)))
            if failure is not None:
                return ScanOutcome(padded, False, failure, capped)
            return ScanOutcome(padded, True, None, capped)
        finally:
            self._running = None
            self._place.release()

    def _answers(
        self, running: _Running, count: int, started: float
    ) -> tuple[list[Remembered], str | None, bool | None]:
        """The answers in order, each in its time, until the end; why they stopped short, if they did; and whether the
        helper said it was capped."""
        end = started + self._total
        ready = running.next(min(started + self._ready, end), self._clock)
        if ready is None:
            return [], "timeout", None
        if (
            not isinstance(ready, dict)
            or set(ready) != {"ready", "cap"}
            or type(ready["ready"]) is not int
            or ready["ready"] != PROTOCOL
            or not isinstance(ready["cap"], bool)
        ):
            return [], "helper", None
        capped: bool = ready["cap"]
        results, failure = self._items(running, count, end)
        return results, failure, capped

    def _items(self, running: _Running, count: int, end: float) -> tuple[list[Remembered], str | None]:
        """Each folder's answer in order, within its own deadline and the check's (``end``), then the end."""
        results: list[Remembered] = []
        last = self._clock()
        while True:
            frame = running.next(min(last + self._item, end), self._clock)
            if frame is None:
                return results, "timeout"
            if frame is _EOF or frame is _BROKEN or not isinstance(frame, dict):
                return results, "helper"
            if "end" in frame:
                if frame == {"end": count} and len(results) == count:
                    return results, None
                return results, "helper"
            try:
                results.append(_remembered(frame, len(results)))
            except ProtocolError:
                return results, "helper"
            if len(results) > count:
                return results, "helper"
            last = self._clock()


READER = RepositoryReader()


def scan_folders(folders: Sequence[str]) -> ScanOutcome:
    """Each folder's latet.yaml read by the process's one helper, within its deadlines (``RepositoryReader.scan``)."""
    return READER.scan(folders)
