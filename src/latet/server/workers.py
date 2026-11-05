"""`latet server run --workers N` (the server performance plan's Task 10): N instances of a Latet server on one machine,
each a process of its own on the one port, sharing PostgreSQL and Valkey, as instances on N machines would. One instance
runs in its event loop's one thread, so a machine's cores serve the server only through several.

The supervisor (``Supervisor``) checks and holds the instance's port, then starts N workers in processes of their own
(``multiprocessing``'s ``spawn``, whatever the system: a worker builds its server, its pools, its keys, its certificate
and its app in its own process, and nothing the supervisor holds is copied into it but its settings and, where the
workers share them, its sockets). It opens no server, no pool, no master key and no certificate itself.

Where the workers listen. On Linux (``reuse_port_here``) each worker binds the port itself with a socket of its own
(``SO_REUSEPORT``; every address, as an instance binds them), which uvicorn makes listen once the worker's application
has started, and the kernel spreads new connections across the workers' listening sockets by a hash of each
connection's addresses and ports: many connections spread evenly, a few long-lived ones (a load balancer's) can still
land unevenly, and more spread better (on the one socket eight workers shared, two of them sat idle: the measurement of
Tasks 9 and 10). The supervisor checks the port first with sockets bound without ``SO_REUSEPORT``, which fail where any
program listens (another server's workers too), and holds it for the whole run with sockets bound with
``SO_REUSEPORT`` alone, without ``SO_REUSEADDR``, that never listen (``Supervisor._held``): no other program, of any
user, with any option, nor a second server's check, binds the port while no worker listens (a holder with
``SO_REUSEADDR`` would let in any socket that sets it too). Linux lets only sockets of the same effective user join a
port's group: any program running as Latet's user could take a share of its connections, or all of them with a filter
of its own attached to the group, so the port's user runs Latet alone (the service's ``latet`` user, the container's).
A worker that ends drops the connections still waiting in its socket's queue (``net.ipv4.tcp_migrate_req``, an opt-in
on Linux 5.14 and later, hands them to the others), and one that is held keeps its share waiting until it is ended.
Elsewhere (macOS hands every connection to one of a port's sockets, Windows gives a socket to one process) the
supervisor binds the instance's own sockets once (``instance._listening``) and makes them listen, and every worker
serves them, as Task 10's S7 has it.

Each worker (``worker_main``) is an instance (``instance.make_instance``) on the port, with a metrics port of its own
(``metrics_port`` for worker 0, the next port for each other: the settings check them all) and a pipe to the
supervisor, on which its beat (``app.beat_said``) comes every ``BEAT_SECONDS``: its id, its number, its version,
whether it is ready, its memory, its spaces and its vaults' indexes, numbers and booleans alone (``beat_checked``). The
supervisor starts again a worker that ends, or that sends no beat for ``WORKER_SILENT_BEATS`` beats (no first one within
``WORKER_START_SECONDS`` of its start), ending it first; a worker's place is started at most once in
``WORKER_RESTART_SECONDS``, so that one that fails is tried again, not in a loop (one that cannot start: below). A
worker that is not ready stays: readiness is the load balancer's to judge, and a store away is no reason to start
anyone again. Once a minute the supervisor says each worker's readiness and memory.

A stop (SIGTERM, SIGINT, SIGBREAK on Windows; ``Supervisor.stop``) goes to every worker on its pipe, each stops as an
instance stops (``GRACEFUL_SECONDS`` at most for what is under way), and those still there when that time is up, one
time for them all, are ended (terminated, then killed); a second stop ends them at once. A worker whose supervisor is
gone (its pipe closed) stops as if told to, and ends itself once ``GRACEFUL_SECONDS`` are past.

A worker that ends with exit 2 could not start: its uvicorn never served (its settings, its database, its certificate
or its application's start refused it), whatever it beat meanwhile. ``WORKER_REFUSALS`` of those in a row stop every
worker and the run (``CannotStart``), the port let go, as one instance that cannot start stops, so that its service
manager decides.

One worker (the default) runs in the supervisor's own process, as one instance always has. On Windows, `latet server
run` refuses more than one (``refused_here``): there a socket serves one process's completion port, and select(), the
loop that shares one, ends a worker past 512 sockets. The Windows service runs one instance whatever the settings say.

What a worker says on its pipe is checked before anything is read of it: a frame of ``BEAT_BYTES`` at most, a JSON
object whose every field is what it should be, or the worker is ended and started again; the log names what it did by
fixed words and numbers, never what it sent. Nothing here logs a password, a key, a value or an error's own words; the
start line names the host and the ports, as an instance's does."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import multiprocessing
import os
import re
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.logline import described
from latet.server.instance import _OWN, GRACEFUL_SECONDS, _bound, _in_use, _listening, _why_not
from latet.server.settings import WORKERS_MOST, with_workers
from latet.server.start_errors import (
    CannotListen,
    CertificateUnusable,
    DatabaseError,
    MasterKeyError,
    SchemaMismatch,
    SettingRefused,
    ValkeyUnavailable,
)

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess

    from uvicorn import Server as Uvicorn

    from latet.server.instance import Instance
    from latet.server.settings import ServerSettings

log = logging.getLogger(__name__)

# ``latet.server.app.BEAT_SECONDS``, named here as well so that the supervisor imports nothing of a server (a test pins
# them equal): a worker's beat comes this often.
BEAT_SECONDS = 10.0
WORKER_SILENT_BEATS = 3  # beats a worker may miss before it is ended and started again
WORKER_RESTART_SECONDS = 10.0  # a worker's place is started at most once in this time
WORKER_REFUSALS = 3  # starts in a row that could not start (exit 2): the server stops
# How long a worker has from its start to its first beat (imports, its server opened, its first look at the stores):
# longer than a beat, so that a machine starting many at once ends none of them for starting slowly.
WORKER_START_SECONDS = 120.0
BEAT_BYTES = 1024  # a beat's frame on the pipe, at most
# How long the supervisor waits, on Linux, for the connections closed on its port in the last minute to let it hold
# the port (TIME_WAIT, about a minute on Linux: one instance, or Latet 0.10, stopped just before), and between tries.
HOLD_SECONDS = 60.0
_HOLD_PAUSE = 1.0
# Why the port could not be held once that time is up: no program listens there (the check passed), but
# connections closed on it outlast the wait (a peer that is gone), or a socket holds it without listening.
_STILL_TAKEN = (
    "connections closed on its port are still there, or another program holds it without listening: start "
    "again in a minute"
)
# The connections the one socket the workers share holds for them until one takes them (uvicorn's own backlog): a
# worker started again finds those that came meanwhile. On Linux each worker's socket has uvicorn's own, as one
# instance's has.
BACKLOG = 2048
_TICK = 0.25  # seconds between two looks of the supervisor at its workers, at most
_SAY_EVERY = 60.0  # each worker's readiness and memory, said this often
_END_SECONDS = 5.0  # how long a worker ended is waited for, after its termination and after its kill
_FRAMES_AT_ONCE = 16  # frames read from one worker's pipe in one look, at most
_STOP = b"stop"
_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name))
_PLATFORM = sys.platform  # read where several workers are refused (``refused_here``); a test changes it
# Why several workers are refused on Windows (the fix round's I2, amending T10-S1): Windows ties a socket to one
# process's I/O completion port, and the only event loop that shares one across processes there, select(), holds 512
# sockets at most and ends the worker past them, which ordinary load reaches.
ONE_WORKER_ON_WINDOWS = (
    "several workers need Linux or the container image: on Windows one socket serves one process. Run one instance "
    "here (`--workers 1`, or `workers: 1` in server.yaml) on each Windows machine behind a load balancer, or the "
    "Windows service; or the container image for several"
)


def refused_here(workers: int) -> str | None:
    """Why ``workers`` workers cannot run on this system, in fixed words, or None: more than one on Windows."""
    return ONE_WORKER_ON_WINDOWS if workers > 1 and _PLATFORM == "win32" else None


def reuse_port_here() -> bool:
    """Whether each worker listens on the port with a socket of its own (``SO_REUSEPORT``), which the system deals new
    connections among: Linux alone, whose kernel spreads them across a port's listening sockets by a hash of each
    connection's addresses and ports. macOS and the other BSDs let several sockets bind a port that way and hand
    every connection to one of them (the last bound, on macOS), and Windows gives a socket to one process: there the
    workers share the supervisor's one socket, as Task 10's S7 has it."""
    return _PLATFORM.startswith("linux")


# What a beat says (``app.beat_said``), each field checked (``beat_checked``): an instance's id is the 12 hexadecimal
# digits each instance draws for itself (``Server.instance``), a version is a version's characters.
TELEMETRY = ("instance", "worker", "version", "ready", "memory", "spaces", "index")
INSTANCE_ID = re.compile(r"[0-9a-f]{12}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_COUNT_MOST = 1 << 62


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < _COUNT_MOST else None


def beat_checked(raw: object, instance: str | None = None) -> dict[str, Any] | None:
    """What a beat says (a worker's frame on its pipe, an instance's key in Valkey), checked before anything is read of
    it: a JSON object of ``BEAT_BYTES`` at most whose ``instance`` is an instance's id (``instance``'s, when given),
    with the fields of ``TELEMETRY`` alone, each None unless it is what it should be (a worker's number, a version, a
    boolean, a count of bytes or of spaces). None for what is no beat: it is never repeated anywhere."""
    if not isinstance(raw, bytes) or len(raw) > BEAT_BYTES:
        return None
    try:
        said = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(said, dict):
        return None
    found = said.get("instance")
    if not isinstance(found, str) or not INSTANCE_ID.fullmatch(found) or instance not in (None, found):
        return None
    worker, version, ready = said.get("worker"), said.get("version"), said.get("ready")
    return {
        "instance": found,
        "worker": worker if type(worker) is int and 0 <= worker < WORKERS_MOST else None,
        "version": version if isinstance(version, str) and _VERSION.fullmatch(version) else None,
        "ready": ready if type(ready) is bool else None,
        "memory": _count(said.get("memory")),
        "spaces": _count(said.get("spaces")),
        "index": _count(said.get("index")),
    }


def share_processors(workers: int) -> None:
    """This worker is one of ``workers`` on the machine: its text recognitions (how many readers, how many threads
    each) are sized by its share of the processors, one at least, not by all of them (``vision.pool.share``). Nothing
    where the ``ocr`` extra is not installed: no image is read there."""
    from latet.vision import models

    if not models.runtime_available():
        return
    from latet.vision import pool

    pool.share(workers)


def worker_settings(settings: ServerSettings, worker: int) -> ServerSettings:
    """The settings of worker ``worker``: the supervisor's, with its own metrics port (``metrics_port`` and ``worker``
    after it). A copy that is not checked again: the settings checked every worker's port already, and checked again
    from this one they would weigh N more."""
    return settings.model_copy(update={"metrics_port": settings.metrics_port + worker})


# --- the worker -------------------------------------------------------------------------------------------------------


class _Link:
    """A worker's end of its pipe, in a thread of its own: the newest beat its instance made handed to the supervisor
    (``report``, which never waits: the beat's thread calls it), and the supervisor's stop, or its end (the pipe closed:
    the supervisor is gone), which stops the instance as a stop signal would (``serving``). Gone, the process ends
    itself once ``GRACEFUL_SECONDS`` are past, however busy its event loop."""

    def __init__(self, pipe: Connection, worker: int) -> None:
        self._pipe, self._worker = pipe, worker
        self._lock = threading.Lock()
        self._said: bytes | None = None
        self._uvicorn: Uvicorn | None = None
        self.stopping = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="latet-worker-link", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def report(self, said: dict[str, Any]) -> None:
        text = json.dumps(said, separators=(",", ":")).encode()
        with self._lock:
            self._said = text

    def serving(self, uvicorn: Uvicorn) -> None:
        """The instance's uvicorn server, which a stop tells to stop; told at once when one came already."""
        with self._lock:
            self._uvicorn = uvicorn
        if self.stopping.is_set():
            uvicorn.should_exit = True

    def _stop(self) -> None:
        self.stopping.set()
        with self._lock:
            uvicorn = self._uvicorn
        if uvicorn is not None:
            uvicorn.should_exit = True

    def _run(self) -> None:
        try:
            while not self._closed.is_set():
                if self._pipe.poll(_TICK):
                    self._pipe.recv_bytes(64)  # what the supervisor sends is its stop
                    self._stop()
                with self._lock:
                    said, self._said = self._said, None
                if said is not None and not self.stopping.is_set():
                    self._pipe.send_bytes(said)
        except (EOFError, OSError):  # the supervisor is gone, or its pipe closed under it
            if self._closed.is_set():
                return
            log.warning("worker %d's supervisor is gone: it stops", self._worker)
            self._stop()
            time.sleep(GRACEFUL_SECONDS + _END_SECONDS)
            os._exit(1)

    def close(self) -> None:
        self._closed.set()
        with contextlib.suppress(OSError):
            self._pipe.close()


def worker_main(
    settings: ServerSettings,
    worker: int,
    own: list[socket.socket] | None,
    pipe: Connection,
    *,
    schema: str = "latet",
    prefix: str = "latet:",
    data: Path | None = None,
) -> None:
    """A worker's process (``spawn``): its log set up as the settings say before anything else, then an instance
    (``instance.make_instance``), worker ``worker`` with the worker's own settings (``worker_settings``), on ``own``,
    the supervisor's sockets, or, given None (Linux, ``reuse_port_here``), on a socket of its own that it binds on the
    port with ``SO_REUSEPORT``; its beat handed to ``pipe``, which brings the supervisor's stop. A worker whose uvicorn
    never served (``started``), and that was not told to stop, could not start: what stopped it is said in its fixed
    words (a server's own start errors), or by its type (any other), or (its application's start failed inside the
    run, which uvicorn ends with SystemExit) as that, and it ends with exit 2, which the supervisor counts
    (``WORKER_REFUSALS`` in a row stop the server), whether or not it beat meanwhile. What stops it once it served is
    said "stopped" (exit 1: started again). Never other words, and before multiprocessing could print them. ``schema``,
    ``prefix`` and ``data`` are ``open_server``'s, which only a test or a benchmark names (never the environment)."""
    from latet.server import instance
    from latet.server.logs import configure_logging

    code = 0
    made: Instance | None = None
    link = _Link(pipe, worker)

    def served() -> bool:
        return made is not None and bool(getattr(made.uvicorn, "started", False))

    def said() -> str:
        return "stopped" if served() else "did not start"

    try:
        configure_logging(settings.log_format, settings.log_level)
        share_processors(settings.workers)
        link.start()
        made = instance.make_instance(
            settings, own=own, worker=worker, reuse_port=own is None, schema=schema, prefix=prefix, data=data
        )
        made.server.reporting = link.report
        link.serving(made.uvicorn)
        ended = 0
        try:
            made.run()  # it closes its server and its sockets, whatever ends it
        except SystemExit as exc:  # uvicorn ends a run whose application could not start with sys.exit(3)
            ended = exc.code if isinstance(exc.code, int) else 1
        if not served() and not link.stopping.is_set():
            log.error("worker %d did not start: its application's start failed (the lines above say why)", worker)
            code = 2
        elif ended:
            code = 1
    except KeyboardInterrupt:  # Ctrl-C reaches every process of a terminal: the supervisor stops them all
        code = 0
    except (
        SchemaMismatch,
        DatabaseError,
        MasterKeyError,
        ValkeyUnavailable,
        CannotListen,
        CertificateUnusable,
        SettingRefused,
    ) as exc:
        # A server's own start errors (``latet.server.start_errors``, which imports nothing that can fail), in fixed
        # words that name no value, as `latet server run` prints one instance's: their words as text, since the log
        # renders an error object by its type. Another error's words can quote what was being read: its type, below,
        # an import that fails among them (numpy, psycopg or redis missing or broken).
        log.error("worker %d %s: %s", worker, said(), str(exc))
        code = 1 if served() else 2
    except Exception as exc:  # anything else (a ValueError, an OSError: a folder or a file refused), by its type alone
        log.error("worker %d %s (%s)", worker, said(), described(exc))
        code = 1 if served() else 2
    finally:
        for sock in own or []:  # the supervisor's: on Linux none (the instance's run closes the socket it bound)
            sock.close()
        link.close()
        instance._written_out()  # what the log holds, written out: the process ends next
    if code:
        sys.exit(code)


# --- the supervisor ---------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Place:
    """A worker's place: its process now (None while it waits to start again), its end of the pipe, when it started and
    when it last beat (``time.monotonic``), and what its last beat said."""

    worker: int
    process: BaseProcess | None = None
    pipe: Any = None  # the supervisor's end of its pipe (a Connection; a PipeConnection on Windows)
    started: float = -math.inf
    heard: float | None = None
    said: dict[str, Any] | None = None
    # A process ended that has not gone yet (the system holds it): none starts in its place, with its metrics port,
    # before it has.
    ending: BaseProcess | None = None
    refused: int = 0  # its starts in a row that could not start (exit 2: its uvicorn never served)


class CannotStart(RuntimeError):
    """A worker could not start ``WORKER_REFUSALS`` times in a row: the workers stopped, the port let go."""


class Supervisor:
    """`latet server run`'s instances on this machine (the module's words): ``workers`` of them, ``settings``' workers
    when it is the same count, else ``settings`` with that count, checked (``settings.with_workers``: a count whose
    metrics ports go past the last port, or reach the instance's own, raises ValueError before anything). ``target``
    is each worker's process (``worker_main``; a test's own, at a module's top level, which ``spawn`` imports)."""

    def __init__(self, settings: ServerSettings, workers: int, target: Callable[..., None] = worker_main) -> None:
        self.settings = settings if workers == settings.workers else with_workers(settings, workers)
        self.workers = workers
        self.target = target
        self.starts: list[tuple[int, float]] = []  # each worker started, and when (``time.monotonic``)
        self._places = [_Place(worker) for worker in range(workers)]
        # The port's sockets: those the workers share, listening; on Linux those that hold the port, never listening.
        self._sockets: list[socket.socket] = []
        self._spread = False  # whether each worker binds the port itself (``reuse_port_here``)
        self._context = multiprocessing.get_context("spawn")
        self._stopping = threading.Event()
        self._hurry = threading.Event()
        self._said_at = time.monotonic()
        self._one: Instance | None = None  # one worker: the instance this process runs
        self._started = False
        self._cannot: int | None = None  # the worker that could not start, which stopped the server

    # what a caller and the tests ask

    def stop(self) -> None:
        """Every worker told to stop, and the run ended once they did; asked again while they stop, they are ended."""
        if self._stopping.is_set():
            self._hurry.set()
        self._stopping.set()

    def process(self, worker: int) -> BaseProcess | None:
        """Worker ``worker``'s process now: None while it waits to start again."""
        return self._places[worker].process

    def said(self, worker: int) -> dict[str, Any] | None:
        """What worker ``worker``'s process said in its last beat (``beat_checked``): None before its first."""
        return self._places[worker].said

    # the run

    def start(self) -> None:
        """What `latet server run` says "Not started" of: one worker made in this process (``instance.make_instance``,
        what stops it raised in its fixed words), or the port checked and held before any worker starts (CannotListen,
        in fixed words). On Linux (``reuse_port_here``) it is checked as one instance's start checks it, with sockets
        bound without ``SO_REUSEPORT``, which fail where any program listens on it (another server's workers among
        them, which the workers' own sockets would join), let go at once; then held with sockets bound with
        ``SO_REUSEPORT`` alone that never listen (``_held``: Linux deals connections to listening sockets alone), so
        that no other program, nor a second server, binds the port while no worker listens, and the workers bind it
        beside them. Elsewhere the instance's sockets are bound and listening, and every worker serves them. Once."""
        from latet.server import instance

        if self._started:
            return
        if self.workers == 1:
            self._one = instance.make_instance(self.settings)
            self._started = True
            return
        if reuse_port_here():
            self._sockets = self._held()
            self._spread = self._started = True
            return
        sockets = _listening(self.settings.host, self.settings.port, _OWN)
        problem = ""
        try:
            for sock in sockets:  # listening before any worker: two that made it listen at once could fail
                sock.listen(BACKLOG)
        except OSError as exc:  # its words could name the address: said by its type
            problem = described(exc)
        if problem:
            for sock in sockets:
                sock.close()
            what, where = _OWN
            raise CannotListen(f"{what} cannot listen on {where}: {problem}")
        self._sockets, self._started = sockets, True

    def _held(self) -> list[socket.socket]:
        """On Linux: the port checked (``_listening`` without ``SO_REUSEPORT``: CannotListen where any program listens,
        another server's workers among them), that socket let go at once, then held (``_bound(hold=True)``:
        ``SO_REUSEPORT`` alone, never listening). A holder cannot bind over the connections a plain listener closed on
        the port in the last minute (TIME_WAIT): then the port is checked and tried again once every ``_HOLD_PAUSE``,
        for ``HOLD_SECONDS`` at most, said once in fixed words, and refused after that (CannotListen, in fixed words
        of its own: ``_STILL_TAKEN``);
        any other error refuses it at once."""
        host, port = self.settings.host, self.settings.port
        deadline = time.monotonic() + HOLD_SECONDS
        said = False
        while True:
            for sock in _listening(host, port, _OWN):
                sock.close()
            try:
                return _bound(host, port, hold=True)
            except (OSError, UnicodeError) as exc:  # its words name the address: said in words of its own, below
                waits, problem = _in_use(exc), _why_not(exc)
            if not waits or time.monotonic() >= deadline:
                break
            if not said:
                said = True
                log.warning(
                    "the instance's port is still taken by connections closed on it in the last minute (a server "
                    "that stopped just before): the workers start once they are gone, in %.0f seconds at most",
                    HOLD_SECONDS,
                )
            time.sleep(_HOLD_PAUSE)
        what, where = _OWN
        if waits:  # the time is up
            raise CannotListen(f"{what} cannot be held on {where}: {_STILL_TAKEN}")
        raise CannotListen(f"{what} cannot be held on {where}: {problem}")

    def run(self) -> None:
        """Until stopped (``start`` first, when it was not called): one worker in this process, as `latet server run`
        has always run one, or the workers started, watched and started again, then all stopped. A worker that cannot
        start ``WORKER_REFUSALS`` times in a row stops them all, and the run raises CannotStart in fixed words once the
        port is let go. The signals that stop it are heard in the main thread alone."""
        self.start()
        if self._one is not None:
            self._one.run()
            return
        before = self._hearing_signals()
        try:
            self._said_start()
            for place in self._places:
                self._start(place)
            while not self._stopping.is_set():
                self._look()
        finally:
            try:
                self._stop_all()
            finally:
                for sock in self._sockets:
                    sock.close()
                for sig, handler in before.items():
                    signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        if self._cannot is not None:
            raise CannotStart(
                f"worker {self._cannot} cannot start: it could not start {WORKER_REFUSALS} times in a row, and its "
                "lines above say why; the workers stopped"
            )
        log.info("the %d workers stopped", self.workers)

    def _hearing_signals(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}

        def heard(signum: int, frame: Any) -> None:
            self.stop()

        return {sig: signal.signal(sig, heard) for sig in _SIGNALS}

    def _said_start(self) -> None:
        settings = self.settings
        if settings.metrics_token_file is not None:
            log.info(
                "%d workers start on %s port %d, their metrics on %s ports %d to %d",
                self.workers, settings.host, settings.port, settings.metrics_host, settings.metrics_port,
                settings.metrics_port + self.workers - 1,
            )  # fmt: skip
        else:
            log.info(
                "%d workers start on %s port %d; no metrics socket (metrics_token_file is not set)",
                self.workers, settings.host, settings.port,
            )  # fmt: skip

    def _start(self, place: _Place) -> None:
        """Its worker started in a process of its own, given its settings, the sockets it serves (none on Linux: it
        binds its own) and its end of a new pipe."""
        mine, theirs = self._context.Pipe(duplex=True)
        own = None if self._spread else self._sockets
        process = self._context.Process(
            target=self.target,
            args=(worker_settings(self.settings, place.worker), place.worker, own, theirs),
            name=f"latet-worker-{place.worker}",
            daemon=True,  # ended with the supervisor, should it end without stopping them
        )
        try:
            process.start()
        except BaseException:
            mine.close()
            raise
        finally:
            theirs.close()  # the worker's own now: its end closes with it
        place.process, place.pipe, place.said, place.heard = process, mine, None, None
        place.started = time.monotonic()
        self.starts.append((place.worker, place.started))
        log.info("worker %d starts (process %d)", place.worker, process.pid)

    def _look(self) -> None:
        """One look at the workers: the beats that came read, those that ended or went silent ended and let go, the
        places whose time has come started again, and once a minute each worker's readiness and memory said."""
        waited: list[Any] = []
        running: list[_Place] = []
        for place in self._places:
            if place.process is not None and place.pipe is not None:
                running.append(place)
                waited += [place.pipe, place.process.sentinel]
        if waited:
            ready = wait(waited, timeout=_TICK)
        else:  # every place waits to start again
            self._stopping.wait(_TICK)
            ready = []
        for place in running:
            if place.pipe in ready:
                self._read(place)
        now = time.monotonic()
        for place in running:
            process = place.process
            if process is None:  # ended while its frames were read
                continue
            if not process.is_alive():
                self._exited(place, process)
            elif place.heard is None and now - place.started > WORKER_START_SECONDS:
                self._ended(place, f"worker {place.worker} sent no beat within {WORKER_START_SECONDS:.0f} seconds")
            elif place.heard is not None and now - place.heard > WORKER_SILENT_BEATS * BEAT_SECONDS:
                self._ended(place, f"worker {place.worker} sent no beat for {now - place.heard:.1f} seconds")
        if self._stopping.is_set():
            return
        for place in self._places:
            if place.ending is not None and not place.ending.is_alive():
                place.ending = None
            if place.process is None and place.ending is None and now - place.started >= WORKER_RESTART_SECONDS:
                self._start(place)
        if now - self._said_at >= _SAY_EVERY:
            self._said_at = now
            for place in self._places:
                self._say(place)

    def _read(self, place: _Place) -> None:
        """The frames its pipe holds, a few at most: each beat checked; what is no beat ends the worker."""
        pipe = place.pipe
        try:
            for _ in range(_FRAMES_AT_ONCE):
                if pipe is None or not pipe.poll(0):
                    return
                said = beat_checked(pipe.recv_bytes(BEAT_BYTES))
                if said is None:
                    self._ended(place, f"worker {place.worker} sent what is no beat")
                    return
                place.said, place.heard = said, time.monotonic()
        except (EOFError, ConnectionError):  # its end closed (a broken pipe, on Windows): the worker ends, or ended
            process = place.process
            if process is not None:
                process.join(_END_SECONDS)
                if process.is_alive():
                    self._ended(place, f"worker {place.worker} closed its pipe")
                else:
                    self._exited(place, process)
        except OSError:  # a frame past BEAT_BYTES, or a pipe that broke
            self._ended(place, f"worker {place.worker} sent what is no beat")

    def _exited(self, place: _Place, process: BaseProcess) -> None:
        """Its worker ended by itself. One that ended with exit 2 could not start (``worker_main``: its uvicorn never
        served, whatever it beat meanwhile; its settings, its database, its certificate or its application's start
        refused it), and ``WORKER_REFUSALS`` of those in a row stop the server, as one instance that cannot start
        stops: its service manager decides then. Any other end resets the count and is started again."""
        code = process.exitcode
        place.refused = place.refused + 1 if code == 2 else 0
        if place.refused >= WORKER_REFUSALS and not self._stopping.is_set():
            self._cannot = place.worker
            log.error(
                "worker %d cannot start: it could not start %d times in a row, and its lines above say why; the "
                "workers stop",
                place.worker,
                WORKER_REFUSALS,
            )
            self.stop()
        self._ended(place, f"worker {place.worker} ended (exit code {code})", killed=False)

    def _ended(self, place: _Place, why: str, *, killed: bool = True) -> None:
        """The worker ended (``killed``: a worker that went silent may hear no signal), waited for, and let go; its
        place started again once ``WORKER_RESTART_SECONDS`` after its last start are past, and the process is gone."""
        process = place.process
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(_END_SECONDS)
            if process.is_alive():
                place.ending = process
        if place.pipe is not None:
            with contextlib.suppress(OSError):
                place.pipe.close()
        place.process, place.pipe = None, None
        if killed:  # ended here: no start that failed by itself
            place.refused = 0
        done = ": it is ended" if killed else ""
        if self._stopping.is_set():
            log.warning("%s%s", why, done)
            return
        wait_for = max(0.0, place.started + WORKER_RESTART_SECONDS - time.monotonic())
        later = f" in {wait_for:.0f} seconds" if wait_for >= 0.5 else ""
        log.warning("%s%s, and starts again%s", why, done, later)

    def _say(self, place: _Place) -> None:
        said = place.said
        if place.process is None:
            log.info("worker %d waits to start again", place.worker)
        elif said is None:
            log.info("worker %d is starting", place.worker)
        else:
            memory = said["memory"]
            log.info(
                "worker %d: instance %s, %s, %s, %s spaces",
                place.worker, said["instance"], "ready" if said["ready"] else "not ready",
                "memory unknown" if memory is None else f"{memory / 1048576:.0f} MiB in memory",
                said["spaces"] if said["spaces"] is not None else "?",
            )  # fmt: skip

    def _stop_all(self) -> None:
        """Every worker told to stop, one deadline of ``GRACEFUL_SECONDS`` for them all (a second stop ends it at once),
        then those still there terminated, then killed, each waited for, every pipe closed."""
        alive = [place for place in self._places if place.process is not None]
        if not alive:
            return
        log.info("the workers stop: they have %.0f seconds to finish what is under way", GRACEFUL_SECONDS)
        for place in alive:
            if place.pipe is not None:
                with contextlib.suppress(OSError):
                    place.pipe.send_bytes(_STOP)
        deadline = time.monotonic() + GRACEFUL_SECONDS
        while not self._hurry.is_set() and (left := deadline - time.monotonic()) > 0:
            running = [p.process for p in alive if p.process is not None and p.process.is_alive()]
            if not running:
                break
            wait([process.sentinel for process in running], timeout=min(_TICK, left))
        for place in alive:
            process = place.process
            if process is not None and process.is_alive():
                log.warning("worker %d did not stop in time: it is ended", place.worker)
                process.terminate()
        for place in alive:
            process = place.process
            if process is None:
                continue
            process.join(_END_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(_END_SECONDS)
            if place.pipe is not None:
                with contextlib.suppress(OSError):
                    place.pipe.close()
            place.pipe = None
