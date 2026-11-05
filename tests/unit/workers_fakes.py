"""Workers that stand in for a Latet server's instance in the supervisor's tests (``tests/unit/test_workers.py``): each
is started in a process of its own (``spawn``), so it lives at a module's top level and imports nothing of the tests.
Each writes what it was given to a file of the test's folder (its process, its worker's number, the port of the socket
it got, its metrics port, its generation: how many of its worker's ran before it), then behaves as its ``mode`` says,
beating as an instance's beat does: a frame of JSON on its pipe, a few times a second here."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

BEAT = 0.05  # seconds between two beats of a fake worker


def _said(worker: int, ready: bool = True) -> bytes:
    said = {
        "instance": f"{os.getpid():012x}"[-12:], "worker": worker, "version": "0.0.0", "ready": ready,
        "memory": 1_048_576, "spaces": 0, "index": 0,
    }  # fmt: skip
    return json.dumps(said).encode()


def _stopped(pipe: Connection) -> bool:
    """Whether the supervisor said stop, or is gone (its end of the pipe closed)."""
    try:
        if pipe.poll(0):
            pipe.recv_bytes(64)
            return True
    except (EOFError, OSError):
        return True
    return False


def no_server_app(settings: Any, worker: int, own: list[socket.socket], pipe: Connection, *, canary: str) -> None:
    """A real worker (``latet.server.workers.worker_main``) in a process whose server application cannot be imported,
    as where numpy is missing or broken: its import fails in words that name a folder holding ``canary``."""
    import importlib.abc

    class Broken(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "latet.server.app":
                raise ImportError(f"numpy cannot be loaded from /home/{canary}/site-packages")
            return None

    sys.meta_path.insert(0, Broken())
    from latet.server.workers import worker_main

    worker_main(settings, worker, own, pipe)


def fake(
    settings: Any, worker: int, own: list[socket.socket] | None, pipe: Connection, *, folder: str, mode: str
) -> None:
    """A worker as ``mode`` says: ``beat`` beats until it is stopped; ``serve`` beats and answers every connection to
    its socket with its worker's number; ``die`` ends at once (exit 3), every time; ``die-once``, ``silent-once`` and
    ``garbage-once`` beat once, then (their first generation alone) end (exit 3), go silent, or send what is no beat;
    ``stubborn`` beats and never stops when it is told to; ``refuse`` ends at once with exit 2, every time, as an
    instance that cannot start does; ``beat-then-2`` beats once, then ends with exit 2, every time; ``mute`` never
    beats; ``unready`` beats that it is not ready; ``fail`` ends at once with exit 1, every time, as a worker that
    failed once running does. Given no socket (``own`` None: Linux), it binds its own on the settings' host and port
    with ``SO_REUSEADDR`` and ``SO_REUSEPORT``, as a real worker's instance does (here with nothing more to import, so
    that it starts as fast as the others); one that serves listens before it writes its record."""
    place = Path(folder)
    generation = len(list(place.glob(f"{worker}-*.json")))
    if own is None:
        mine = socket.socket()
        mine.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        mine.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        mine.bind((settings.host, settings.port))
        own = [mine]
    if mode == "serve":
        own[0].listen(16)
        own[0].settimeout(BEAT)
    record = {
        "pid": os.getpid(), "worker": worker, "port": own[0].getsockname()[1], "metrics_port": settings.metrics_port,
        "generation": generation, "time": time.time(),
    }  # fmt: skip
    (place / f"{worker}-{os.getpid()}.json").write_text(json.dumps(record), "utf-8")
    if mode == "die":
        sys.exit(3)
    if mode == "refuse":
        sys.exit(2)
    if mode == "fail":
        sys.exit(1)
    if mode == "mute":
        time.sleep(600)  # ended by the supervisor long before
    first = generation == 0
    sent = 0
    while True:
        pipe.send_bytes(_said(worker, ready=mode != "unready"))
        sent += 1
        if mode == "beat-then-2":
            sys.exit(2)
        if first and sent == 1:
            if mode == "die-once":
                sys.exit(3)
            if mode == "silent-once":
                time.sleep(600)  # ended by the supervisor long before
            if mode == "garbage-once":
                pipe.send_bytes(b"not a beat: mario.rossi@acme.it")
                time.sleep(600)
        if mode == "serve":
            with contextlib.suppress(TimeoutError, OSError):
                conn, _ = own[0].accept()
                with conn:
                    conn.sendall(f"{worker}\n".encode())
        else:
            time.sleep(BEAT)
        if _stopped(pipe) and mode != "stubborn":
            (place / f"{worker}-{os.getpid()}.stopped").write_text("stopped", "utf-8")
            return
