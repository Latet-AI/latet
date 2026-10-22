"""Helpers that read remembered repositories for the tests, in this process (``repository_io.launch``).

The daemon's own helper is a process of its own (``repository_child``), which a test's monkeypatches of ``os``,
``own_file`` or ``_read_remembered`` would never reach. These run its very protocol (``repository_child.serve``) in a
thread of the test's process instead, through ``latet.projects._read_remembered``; the place, the deadlines and the
frames are ``repository_io``'s own. A thread is never killed: ``kill`` stops its answers, and it keeps its place until
it ends, as a helper the system cannot end does.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from collections.abc import Callable
from typing import IO, Any


class _Stopped(IO[bytes]):
    """The helper's standard output: refused once it is killed (``BrokenPipeError``), as a killed process writes no
    more."""

    def __init__(self, out: IO[bytes], child: InProcessChild) -> None:
        self._out, self._child = out, child

    def write(self, data: Any) -> int:
        if self._child.killed:
            raise BrokenPipeError("killed")
        return self._out.write(data)

    def flush(self) -> None:
        if self._child.killed:
            raise BrokenPipeError("killed")
        self._out.flush()


class InProcessChild:
    """A helper in a thread of this process (``read_one``: none, ``latet.projects._read_remembered``)."""

    def __init__(self, read_one: Callable[[str], Any] | None = None) -> None:
        from latet import projects
        from latet.repository_child import serve

        child_in, parent_out = os.pipe()
        parent_in, child_out = os.pipe()
        self.stdin: IO[bytes] | None = os.fdopen(parent_out, "wb")
        self.stdout: IO[bytes] | None = os.fdopen(parent_in, "rb")
        self._in = os.fdopen(child_in, "rb")
        self._out = os.fdopen(child_out, "wb")
        self.killed = False
        self.returncode: int | None = None
        self.started = threading.Event()
        reader = read_one if read_one is not None else (lambda folder: projects._read_remembered(folder))

        def run() -> None:
            code = 1
            try:
                self.started.set()
                code = serve(self._in, _Stopped(self._out, self), reader)
            except Exception:
                code = 1
            finally:
                for pipe in (self._out, self._in):
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
                self.returncode = -9 if self.killed else code

        self._thread = threading.Thread(target=run, name="latet-repositories-test-child", daemon=True)
        self._thread.start()

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._thread.join(timeout)
        if self._thread.is_alive() or self.returncode is None:
            raise subprocess.TimeoutExpired("in-process repository helper", timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
