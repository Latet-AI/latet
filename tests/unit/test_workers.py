"""`latet server run --workers N` (the server performance plan's Task 10): the supervisor checks and holds the
instance's port, starts N worker processes (``spawn``) on it (on Linux each binds a socket of its own there with
SO_REUSEPORT, elsewhere they share the supervisor's one), each with a metrics port of its own, hears each one's beat
on its pipe, starts again a worker that ends or goes silent (at most once in ``WORKER_RESTART_SECONDS`` for each), and
passes a stop to every worker, ending those still there once ``GRACEFUL_SECONDS`` have gone by. One worker runs in the
supervisor's own process, as before. The workers here are stand-ins (``tests/unit/workers_fakes.py``): no PostgreSQL,
no Valkey; the real ones are ``tests/server/test_workers.py``'s."""

from __future__ import annotations

import contextlib
import errno
import functools
import itertools
import json
import logging
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from latet.server import instance
from latet.server import workers as workers_module
from latet.server.settings import ServerSettings, load_server_settings, with_workers
from latet.server.workers import WORKER_RESTART_SECONDS, WORKER_SILENT_BEATS, Supervisor, beat_checked

from . import workers_fakes

CANARY = "s3cr3t-Canary-Pw"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _settings(**changes) -> ServerSettings:
    port = changes.pop("port", None) or _free_port()
    return ServerSettings(
        url=f"http://127.0.0.1:{port}",
        database_url=f"postgresql://latet:{CANARY}@127.0.0.1:1/latet",
        valkey_url=f"redis://:{CANARY}@127.0.0.1:1/0",
        host="127.0.0.1",
        port=port,
        **changes,
    )


def _fake(folder: Path, mode: str):
    return functools.partial(workers_fakes.fake, folder=str(folder), mode=mode)


def _records(folder: Path, worker: int | None = None) -> list[dict]:
    """What each worker started wrote of itself (``workers_fakes.fake``), by worker and generation: ``worker``'s alone."""
    found = []
    for path in folder.glob("*.json" if worker is None else f"{worker}-*.json"):
        with contextlib.suppress(ValueError, OSError):  # a file being written
            found.append(json.loads(path.read_text("utf-8")))
    return sorted(found, key=lambda record: (record["worker"], record["generation"]))


def _until(check, seconds: float = 60.0, what: str = "the workers") -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not within {seconds} s")
        time.sleep(0.05)


@contextlib.contextmanager
def _supervising(settings: ServerSettings, count: int, target):
    """The supervisor running in a thread of its own, as `latet server run --workers` runs it in its main thread;
    stopped at the end, every worker with it."""
    supervisor = Supervisor(settings, count, target=target)
    raised: list[BaseException] = []

    def run() -> None:
        try:
            supervisor.run()
        except BaseException as exc:  # what `latet server run` would say: the test reads it
            raised.append(exc)

    thread = threading.Thread(target=run, name="test-supervisor", daemon=True)
    thread.start()
    supervisor.raised, supervisor.thread = raised, thread  # type: ignore[attr-defined]
    try:
        yield supervisor
    finally:
        supervisor.stop()
        thread.join(90)
        assert not thread.is_alive(), "the supervisor did not stop"
        assert multiprocessing.active_children() == []


def _gone(pid: int) -> bool:
    return all(record != pid for record in (child.pid for child in multiprocessing.active_children()))


LINUX = sys.platform.startswith("linux")
linux_alone = pytest.mark.skipif(not LINUX, reason="SO_REUSEPORT's spread by the kernel is Linux's alone")


def _answers(port: int, count: int) -> Counter[str]:
    """``count`` connections to ``port``, one after the other: which worker answered each, counted."""
    answers: Counter[str] = Counter()
    for _ in range(count):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            answers[conn.makefile().readline().strip()] += 1
    return answers


def test_n_workers_answer_on_one_port(tmp_path):
    """Every worker answers on the one port. On Linux each listens there with a socket of its own (SO_REUSEPORT) and
    the kernel spreads new connections across them by a hash of their addresses and ports: 90 connections give each of
    three workers at least ten (thirty each on average; fewer than ten is about one chance in a hundred thousand).
    Elsewhere they share the supervisor's one socket, which deals them as it may."""
    settings = _settings()
    with _supervising(settings, 3, _fake(tmp_path, "serve")) as supervisor:
        _until(lambda: len(_records(tmp_path)) == 3)
        records = _records(tmp_path)
        assert [r["worker"] for r in records] == [0, 1, 2] and len({r["pid"] for r in records}) == 3
        assert {r["port"] for r in records} == {settings.port}  # one port, in every worker
        assert all(r["pid"] != os.getpid() for r in records)
        answers = _answers(settings.port, 90)
        assert set(answers) <= {"0", "1", "2"} and answers
        if LINUX:
            assert set(answers) == {"0", "1", "2"} and min(answers.values()) >= 10, answers
        assert supervisor.workers == 3


def test_a_worker_started_again_answers_on_the_port_again(tmp_path, monkeypatch):
    """A worker killed is started again and answers on the port as before: on Linux its new process binds the port
    again beside the others (the supervisor holds it for them), elsewhere it takes the one socket again."""
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.5)
    settings = _settings()
    with _supervising(settings, 2, _fake(tmp_path, "serve")) as supervisor:
        _until(lambda: len(_records(tmp_path)) == 2)
        killed = supervisor.process(0)
        killed.kill()
        _until(lambda: len(_records(tmp_path, 0)) == 2, what="worker 0 started again")
        answered: Counter[str] = Counter()
        deadline = time.monotonic() + 60
        while answered["0"] == 0 and time.monotonic() < deadline:
            answered += _answers(settings.port, 10)
        assert answered["0"] > 0 and supervisor.process(0).pid != killed.pid, answered


# --- where the workers listen: a socket of each one's own on Linux, the supervisor's one elsewhere ---------------------


class _FakeSocket:
    """A socket as ``_listening`` or ``_bound`` makes one, standing in: whether it was bound with SO_REUSEPORT, whether
    it holds the port (SO_REUSEPORT alone), its backlog once it listens, whether it was closed."""

    def __init__(self, port: int, reuse_port: bool, hold: bool = False) -> None:
        self.port, self.reuse_port, self.hold = port, reuse_port, hold
        self.backlog: int | None = None
        self.closed = False

    def listen(self, backlog: int) -> None:
        self.backlog = backlog

    def close(self) -> None:
        self.closed = True

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)


def _fake_binds(monkeypatch, *, in_use: bool = False, holds_refused: Sequence[int] = ()) -> list[_FakeSocket]:
    """``_listening`` and ``_bound`` in the supervisor, standing in: each socket they make, in order; ``in_use``: a
    bind without SO_REUSEPORT finds the port in use (another program, or another server's workers, listen there);
    ``holds_refused``: the errors (errno) the holders' binds meet first, one each, in turn."""
    from latet.server.start_errors import CannotListen

    made: list[_FakeSocket] = []
    refusals = list(holds_refused)

    def listening(host, port, which, *, reuse_port=False):
        if in_use and not reuse_port:
            raise CannotListen(f"{which[0]} cannot be bound on {which[1]}: its address and port are in use")
        made.append(_FakeSocket(port, reuse_port))
        return [made[-1]]

    def bound(host, port, *, reuse_port=False, hold=False):
        if hold and refusals:
            code = refusals.pop(0)
            raise OSError(code, f"bind refused on {host}:{port}")
        made.append(_FakeSocket(port, reuse_port, hold))
        return [made[-1]]

    monkeypatch.setattr(workers_module, "_listening", listening)
    monkeypatch.setattr(workers_module, "_bound", bound)
    return made


def _given(supervisor: Supervisor) -> list[dict]:
    """What each worker the supervisor starts is given (its process's arguments), its process standing in."""
    given: list[dict] = []
    real = supervisor._context

    class _Process:
        pid = 4242

        def __init__(self, **kwargs):
            given.append(kwargs)

        def start(self):
            pass

    class Context:
        Process = _Process

        def Pipe(self, duplex=True):
            return real.Pipe(duplex=duplex)

    supervisor._context = Context()  # type: ignore[assignment]
    return given


def test_on_linux_the_supervisor_checks_and_holds_the_port_and_each_worker_binds_its_own(tmp_path, monkeypatch):
    """Linux: the supervisor checks the port as one instance's start does (a bind without SO_REUSEPORT, which fails
    where anything listens, another server's workers included), lets that socket go, and holds the port with one bound
    with SO_REUSEPORT alone, without SO_REUSEADDR, that never listens (the system deals connections to listening
    sockets alone), so that no program of another user, nor a second server of the same user, binds the port while
    no worker listens. Each worker is given no socket: it binds its own beside the others."""
    monkeypatch.setattr(workers_module, "_PLATFORM", "linux")
    made = _fake_binds(monkeypatch)
    supervisor = Supervisor(_settings(), 3, target=_fake(tmp_path, "beat"))
    given = _given(supervisor)
    supervisor.start()
    checked, held = made
    assert not checked.reuse_port and not checked.hold and checked.closed and checked.backlog is None
    assert held.hold and not held.closed and held.backlog is None
    supervisor._start(supervisor._places[0])
    supervisor._places[0].pipe.close()
    [worker] = given
    settings, number, own, _ = worker["args"]
    assert number == 0 and own is None and settings.port == supervisor.settings.port


def test_on_linux_a_port_in_use_is_refused_before_the_port_is_held_or_any_worker_starts(tmp_path, monkeypatch):
    from latet.server.start_errors import CannotListen

    monkeypatch.setattr(workers_module, "_PLATFORM", "linux")
    made = _fake_binds(monkeypatch, in_use=True)
    supervisor = Supervisor(_settings(), 2, target=_fake(tmp_path, "beat"))
    given = _given(supervisor)
    with pytest.raises(CannotListen, match="in use"):
        supervisor.run()
    assert made == [] and given == [] and supervisor.starts == []


def test_on_linux_a_port_still_held_by_connections_just_closed_is_waited_for_then_held(tmp_path, monkeypatch, caplog):
    """A holder, bound without SO_REUSEADDR, cannot bind over the connections a plain listener closed in the last
    minute (TIME_WAIT: one instance, or Latet 0.10, stopped just before). The supervisor checks the port again and
    tries again, once a second, saying so once in fixed words, and holds the port once they are gone."""
    monkeypatch.setattr(workers_module, "_PLATFORM", "linux")
    monkeypatch.setattr(workers_module, "_HOLD_PAUSE", 0.01)
    made = _fake_binds(monkeypatch, holds_refused=[errno.EADDRINUSE, errno.EADDRINUSE])
    supervisor = Supervisor(_settings(), 2, target=_fake(tmp_path, "beat"))
    with caplog.at_level(logging.INFO, logger="latet.server.workers"):
        supervisor.start()
    assert [sock.hold for sock in made] == [False, False, False, True]  # checked three times, held at the third
    assert made[-1] is supervisor._sockets[0] and all(sock.closed for sock in made[:3])
    said = [r.getMessage() for r in caplog.records if "closed on it in the last minute" in r.getMessage()]
    assert len(said) == 1 and "127.0.0.1" not in said[0] and str(supervisor.settings.port) not in said[0]


def test_on_linux_a_port_that_stays_in_use_past_the_wait_is_refused(tmp_path, monkeypatch, caplog):
    from latet.server.start_errors import CannotListen

    monkeypatch.setattr(workers_module, "_PLATFORM", "linux")
    monkeypatch.setattr(workers_module, "HOLD_SECONDS", 0.2)
    monkeypatch.setattr(workers_module, "_HOLD_PAUSE", 0.01)
    made = _fake_binds(monkeypatch, holds_refused=[errno.EADDRINUSE] * 10_000)
    supervisor = Supervisor(_settings(), 2, target=_fake(tmp_path, "beat"))
    given = _given(supervisor)
    with caplog.at_level(logging.INFO, logger="latet.server.workers"), pytest.raises(CannotListen) as refused:
        supervisor.run()
    assert "cannot be held" in str(refused.value)
    assert "connections closed on its port are still there" in str(refused.value)
    assert "another program listens there" not in str(refused.value)
    assert "127.0.0.1" not in str(refused.value) and given == [] and supervisor.starts == []
    assert all(sock.closed for sock in made) and not any(sock.hold for sock in made)
    assert sum("closed on it in the last minute" in r.getMessage() for r in caplog.records) == 1


def test_on_linux_a_hold_refused_for_another_reason_is_refused_at_once(tmp_path, monkeypatch, caplog):
    from latet.server.start_errors import CannotListen

    monkeypatch.setattr(workers_module, "_PLATFORM", "linux")
    made = _fake_binds(monkeypatch, holds_refused=[errno.EACCES])
    supervisor = Supervisor(_settings(), 2, target=_fake(tmp_path, "beat"))
    with caplog.at_level(logging.INFO, logger="latet.server.workers"), pytest.raises(CannotListen, match="may not"):
        supervisor.start()
    assert len(made) == 1 and "closed on it in the last minute" not in caplog.text


@pytest.mark.parametrize("system", ["darwin", "freebsd14", "win32"])
def test_elsewhere_the_workers_share_the_supervisors_one_socket(tmp_path, monkeypatch, system):
    """macOS lets several sockets bind a port with SO_REUSEPORT and hands every connection to the last one, the other
    BSDs too, and Windows gives a socket to one process: there the supervisor binds the port once, makes it listen,
    and hands that socket to every worker, as Task 10 has it (several workers are refused on Windows by `latet server
    run`; the tests and measurements drive the supervisor there)."""
    monkeypatch.setattr(workers_module, "_PLATFORM", system)
    made = _fake_binds(monkeypatch)
    supervisor = Supervisor(_settings(), 2, target=_fake(tmp_path, "beat"))
    given = _given(supervisor)
    supervisor.start()
    [shared] = made
    assert not shared.reuse_port and shared.backlog == workers_module.BACKLOG and not shared.closed
    supervisor._start(supervisor._places[1])
    supervisor._places[1].pipe.close()
    assert given[0]["args"][2] == [shared]


@pytest.mark.parametrize(("own", "reuse_port"), [(None, True), ([], False)])
def test_a_worker_given_no_socket_has_its_instance_bind_the_port_with_so_reuseport(
    monkeypatch, capsys, own, reuse_port
):
    """A worker given no socket (Linux) makes its instance bind the port itself with SO_REUSEPORT; one given the
    supervisor's sockets serves them, as Task 10's do."""
    from latet.server.start_errors import SettingRefused

    asked: list[dict] = []

    def made(settings, **kwargs):
        asked.append(kwargs)
        raise SettingRefused("database_url names no database")

    code, said = _worker_main_here(monkeypatch, capsys, made, own=own)
    assert code == 2 and "worker 1 did not start: database_url names no database" in said
    assert asked[0]["own"] == own and asked[0]["reuse_port"] is reuse_port


def test_an_instance_made_to_share_the_port_binds_it_with_so_reuseport(monkeypatch):
    """``make_instance(reuse_port=True)``, as a worker on Linux makes its instance: its own socket bound with
    SO_REUSEPORT, last, by the instance itself; the metrics' socket, a port of the worker's own, without. Its
    certificate is loaded in the worker, as every instance's is."""
    from types import SimpleNamespace

    from latet.cli import main as cli_main
    from latet.server import app as app_module
    from latet.server import operations

    bound: list[tuple[int, bool]] = []
    loaded: list[str] = []
    closed: list[str] = []

    def listening(host, port, which, *, reuse_port=False):
        bound.append((port, reuse_port))
        return [_FakeSocket(port, reuse_port)]

    server = SimpleNamespace(close=lambda: closed.append("server"), worker=None)
    made = SimpleNamespace(config=SimpleNamespace(load=lambda: loaded.append("certificate and key")))
    monkeypatch.setattr(app_module, "open_server", lambda settings, **kwargs: server)
    monkeypatch.setattr(operations, "server_app", lambda server: "app")
    monkeypatch.setattr(cli_main, "uvicorn_server", lambda *args, **kwargs: made)
    monkeypatch.setattr(instance, "_listening", listening)
    from latet.server import logs

    monkeypatch.setattr(logs, "configure_logging", lambda *args, **kwargs: None)
    # Any file as the metrics token's: an instance makes its metrics socket when one is named.
    settings = _settings(workers=2, metrics_port=17283).model_copy(update={"metrics_token_file": Path(__file__)})
    made_instance = instance.make_instance(settings, worker=1, reuse_port=True)
    assert bound == [(settings.port, True), (17283, False)]
    assert loaded == ["certificate and key"] and closed == []
    assert made_instance.worker == 1 and [s.port for s in made_instance.own or []] == [settings.port]


@linux_alone
def test_sockets_bound_with_so_reuseport_share_a_port_and_a_plain_bind_finds_it_in_use():
    """What the Linux path rests on: sockets of this user bound with SO_REUSEPORT all listen on one port, and a socket
    bound without it there is refused, so that the supervisor's check finds another server's workers."""
    from latet.server.start_errors import CannotListen

    port = _free_port()
    first = instance._listening("127.0.0.1", port, instance._OWN, reuse_port=True)
    second = instance._listening("127.0.0.1", port, instance._OWN, reuse_port=True)
    try:
        for sock in [*first, *second]:
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 1
            sock.listen(8)
        with pytest.raises(CannotListen, match="in use"):
            instance._listening("127.0.0.1", port, instance._OWN)
    finally:
        for sock in [*first, *second]:
            sock.close()


def _socket_with(*, reuse_address: bool, reuse_port: bool) -> socket.socket:
    sock = socket.socket()
    if reuse_address:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuse_port:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    return sock


def _bind_refused(sock: socket.socket, port: int) -> bool:
    """Whether ``sock`` is refused the port (EADDRINUSE); closed either way."""
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(8)
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    finally:
        sock.close()
    return False


@linux_alone
def test_on_linux_the_holders_keep_the_port_while_no_worker_listens(tmp_path):
    """I1 and M1 of the fix round: the supervisor's holders are bound with SO_REUSEPORT alone. While no worker
    listens, a socket with SO_REUSEADDR (as most servers bind theirs) or with none is refused the port, and so is a
    second server's check (a bind without SO_REUSEPORT): a holder with SO_REUSEADDR would let them both in. A worker
    (SO_REUSEADDR and SO_REUSEPORT, this user) binds and listens beside them, and once it is gone the port is still
    held."""
    settings = _settings()
    supervisor = Supervisor(settings, 2, target=_fake(tmp_path, "beat"))
    supervisor.start()
    try:
        assert supervisor._sockets
        for held in supervisor._sockets:
            assert held.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
            assert held.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 1
        from latet.server.start_errors import CannotListen

        with pytest.raises(CannotListen, match="in use"):  # a second server's check
            instance._listening("127.0.0.1", settings.port, instance._OWN)
        assert _bind_refused(_socket_with(reuse_address=True, reuse_port=False), settings.port)
        assert _bind_refused(_socket_with(reuse_address=False, reuse_port=False), settings.port)
        worker = instance._listening("127.0.0.1", settings.port, instance._OWN, reuse_port=True)
        for sock in worker:
            sock.listen(8)
        with socket.create_connection(("127.0.0.1", settings.port), timeout=10):
            pass
        for sock in worker:
            sock.close()
        assert _bind_refused(_socket_with(reuse_address=True, reuse_port=False), settings.port)
    finally:
        for held in supervisor._sockets:
            held.close()


@linux_alone
@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="sockets of another user are made as root alone")
def test_on_linux_the_holders_keep_another_users_bind_off_the_port_whatever_it_sets(tmp_path):
    """I1 of the fix round: while no worker listens, a program of another user is refused the port whatever it sets
    (SO_REUSEADDR, SO_REUSEPORT, both, neither), and still once a worker listened and went. Linux compares each
    socket's owner, so the test makes the other user's sockets with that user's effective id."""
    settings = _settings()
    supervisor = Supervisor(settings, 2, target=_fake(tmp_path, "beat"))
    supervisor.start()

    def theirs(reuse_address: bool, reuse_port: bool) -> socket.socket:
        os.seteuid(65534)  # nobody
        try:
            return _socket_with(reuse_address=reuse_address, reuse_port=reuse_port)
        finally:
            os.seteuid(0)

    options = [(True, False), (True, True), (False, True), (False, False)]
    try:
        for reuse_address, reuse_port in options:
            assert _bind_refused(theirs(reuse_address, reuse_port), settings.port), (reuse_address, reuse_port)
        worker = instance._listening("127.0.0.1", settings.port, instance._OWN, reuse_port=True)
        for sock in worker:
            sock.listen(8)
            sock.close()
        for reuse_address, reuse_port in options:
            assert _bind_refused(theirs(reuse_address, reuse_port), settings.port), (reuse_address, reuse_port)
    finally:
        for held in supervisor._sockets:
            held.close()


@linux_alone
def test_on_linux_a_port_held_by_connections_just_closed_is_refused_once_the_wait_is_over(
    tmp_path, monkeypatch, caplog
):
    """A plain listener's connections closed just before (TIME_WAIT, about a minute on Linux) keep a holder bound
    without SO_REUSEADDR off the port: the supervisor waits for them (``HOLD_SECONDS``, shortened here), saying so
    once, then refuses the port in fixed words, holding nothing and starting no worker."""
    from latet.server.start_errors import CannotListen

    monkeypatch.setattr(workers_module, "HOLD_SECONDS", 1.0)
    monkeypatch.setattr(workers_module, "_HOLD_PAUSE", 0.2)
    settings = _settings()
    plain = _socket_with(reuse_address=True, reuse_port=False)
    plain.bind(("127.0.0.1", settings.port))
    plain.listen(8)
    client = socket.create_connection(("127.0.0.1", settings.port), timeout=10)
    accepted, _ = plain.accept()
    accepted.close()  # the server's side closes first: its end waits (TIME_WAIT) on the port
    time.sleep(0.2)
    client.close()
    plain.close()
    time.sleep(0.2)
    supervisor = Supervisor(settings, 2, target=_fake(tmp_path, "beat"))
    with caplog.at_level(logging.INFO, logger="latet.server.workers"), pytest.raises(CannotListen) as refused:
        supervisor.run()
    assert "cannot be held" in str(refused.value) and supervisor.starts == [] and supervisor._sockets == []
    assert "connections closed on its port are still there" in str(refused.value)
    assert "another program listens there" not in str(refused.value)
    assert sum("closed on it in the last minute" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own option")
def test_on_windows_a_socket_asked_to_share_or_hold_a_port_stays_exclusive():
    """M3 of the fix round: Windows never reaches the Linux path (``reuse_port_here``), and its sockets keep
    SO_EXCLUSIVEADDRUSE, nothing else, whatever ``_bound`` is asked."""
    port = _free_port()
    for asked in ({"reuse_port": True}, {"hold": True}):
        sockets = instance._bound("127.0.0.1", port, **asked)
        try:
            for sock in sockets:
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        finally:
            for sock in sockets:
                sock.close()


@linux_alone
def test_on_linux_the_supervisor_refuses_a_port_another_servers_workers_listen_on(tmp_path):
    """Another server's workers (a socket of this user bound with SO_REUSEPORT, listening) hold the port: the supervisor
    refuses it, in fixed words, before it holds anything or starts any worker, rather than join their group."""
    from latet.server.start_errors import CannotListen

    settings = _settings()
    other = socket.socket()
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    other.bind(("127.0.0.1", settings.port))
    other.listen(8)
    try:
        supervisor = Supervisor(settings, 2, target=_fake(tmp_path, "beat"))
        with pytest.raises(CannotListen, match="in use"):
            supervisor.run()
        assert supervisor.starts == [] and multiprocessing.active_children() == []
    finally:
        other.close()


def test_a_dead_worker_is_started_again(tmp_path, monkeypatch, caplog):
    """Review Focus 5: a worker that ends is started again, in a process of its own, and beats; the others go on."""
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.5)
    with (
        caplog.at_level(logging.INFO, logger="latet.server.workers"),
        _supervising(_settings(), 2, _fake(tmp_path, "die-once")) as supervisor,
    ):
        _until(lambda: [r["generation"] for r in _records(tmp_path)] == [0, 1, 0, 1])
        records = _records(tmp_path)
        for worker in (0, 1):
            first, again = (r for r in records if r["worker"] == worker)
            assert first["pid"] != again["pid"] and _gone(first["pid"])
            _until(
                lambda w=worker, pid=again["pid"]: (
                    supervisor.process(w) is not None and supervisor.process(w).pid == pid
                )
            )
            _until(lambda w=worker: (supervisor.said(w) or {}).get("ready") is True, what="its beat")
    said = caplog.text
    assert "worker 0 ended (exit code 3)" in said and "worker 1 ended (exit code 3)" in said


def test_a_worker_dying_in_a_loop_is_started_again_once_every_ten_seconds(tmp_path, monkeypatch):
    assert WORKER_RESTART_SECONDS == 10.0
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 1.0)  # the same rule, ten times faster
    with _supervising(_settings(), 2, _fake(tmp_path, "die")) as supervisor:  # one would run in this process
        time.sleep(5.5)
        starts = [at for worker, at in supervisor.starts if worker == 0]
    assert 2 <= len(starts) <= 6, starts
    assert all(later - before >= 1.0 for before, later in itertools.pairwise(starts)), starts


def test_a_silent_worker_is_ended_and_started_again(tmp_path, monkeypatch, caplog):
    from latet.server import app

    assert workers_module.BEAT_SECONDS == app.BEAT_SECONDS and WORKER_SILENT_BEATS == 3
    monkeypatch.setattr(workers_module, "BEAT_SECONDS", 0.3)  # silent past 0.9 s
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.5)
    with (
        caplog.at_level(logging.INFO, logger="latet.server.workers"),
        _supervising(_settings(), 2, _fake(tmp_path, "silent-once")) as supervisor,
    ):
        _until(lambda: len(_records(tmp_path, 0)) == 2)
        first, again = _records(tmp_path, 0)
        _until(lambda: _gone(first["pid"]), what="the silent worker's end")
        _until(lambda: (supervisor.said(0) or {}).get("instance") == f"{again['pid']:012x}"[-12:], what="its beat")
    assert not (tmp_path / f"0-{first['pid']}.stopped").exists()  # ended, not stopped
    assert "worker 0 sent no beat for" in caplog.text


def test_what_is_no_beat_ends_the_worker_and_is_never_said(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.5)
    with (
        caplog.at_level(logging.INFO, logger="latet.server.workers"),
        _supervising(_settings(), 2, _fake(tmp_path, "garbage-once")),
    ):
        _until(lambda: len(_records(tmp_path, 0)) == 2)
        first, _ = _records(tmp_path, 0)
        _until(lambda: _gone(first["pid"]), what="its end")
    assert "worker 0 sent what is no beat" in caplog.text and "mario.rossi" not in caplog.text


def test_a_stop_reaches_every_worker(tmp_path):
    with _supervising(_settings(), 3, _fake(tmp_path, "beat")) as supervisor:
        _until(lambda: all(supervisor.said(worker) is not None for worker in range(3)), what="their beats")
        processes = [supervisor.process(worker) for worker in range(3)]
        started = time.monotonic()
    assert time.monotonic() - started < instance.GRACEFUL_SECONDS
    records = _records(tmp_path)
    assert len(records) == 3 and all((tmp_path / f"{r['worker']}-{r['pid']}.stopped").exists() for r in records)
    assert [process.exitcode for process in processes] == [0, 0, 0]  # each stopped by itself, none ended


def test_a_worker_that_does_not_stop_in_time_is_ended_once_the_shared_time_is_up(tmp_path, monkeypatch):
    monkeypatch.setattr(workers_module, "GRACEFUL_SECONDS", 1.0)
    with _supervising(_settings(), 2, _fake(tmp_path, "stubborn")) as supervisor:
        _until(lambda: all(supervisor.said(worker) is not None for worker in range(2)), what="their beats")
        processes = [supervisor.process(worker) for worker in range(2)]
        started = time.monotonic()
    assert time.monotonic() - started < 10  # one deadline for them all, not one each after the other
    assert all(process.exitcode not in (0, None) for process in processes)


def test_each_worker_has_a_metrics_port_of_its_own(tmp_path):
    settings = _settings(metrics_port=17283)  # a stand-in binds none of them
    with _supervising(settings, 3, _fake(tmp_path, "beat")):
        _until(lambda: len(_records(tmp_path)) == 3)
    assert [r["metrics_port"] for r in _records(tmp_path)] == [settings.metrics_port + k for k in range(3)]


def test_workers_past_the_ports_are_refused(monkeypatch):
    assert _settings(metrics_port=65529, workers=7).workers == 7  # 65529 to 65535
    for refused in (
        lambda: _settings(metrics_port=65529, workers=8),  # one past 65535
        lambda: _settings(port=7285, metrics_port=7283, workers=3),  # the instance's own port among them
        lambda: _settings(workers=0),
        lambda: _settings(workers=65),
        lambda: with_workers(_settings(metrics_port=65529), 8),  # `--workers` is checked as the settings are
        lambda: Supervisor(_settings(metrics_port=65529), 8),
    ):
        with pytest.raises(ValueError, match=r"workers|metrics_port|less than|greater than") as error:
            refused()
        assert CANARY not in str(error.value)
    assert _settings(port=7285, metrics_port=7283, workers=2).workers == 2
    with pytest.raises(ValueError, match="metrics_port"):
        _settings(metrics_port=65529, workers=8)
    for name in ("LATET_SERVER_CONFIG", "LATET_SERVER_PORT", "LATET_SERVER_METRICS_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", "http://127.0.0.1:5283")
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@127.0.0.1:1/latet")
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("LATET_SERVER_WORKERS", "4")
    assert load_server_settings().workers == 4
    assert with_workers(load_server_settings(), 2).workers == 2


def test_one_worker_runs_in_the_process(monkeypatch):
    ran = []

    class Made:
        def run(self):
            ran.append(("run", os.getpid()))

    def made(settings):
        ran.append(("made", os.getpid()))
        return Made()

    monkeypatch.setattr(instance, "make_instance", made)
    Supervisor(_settings(), 1, target=lambda *args, **kwargs: pytest.fail("a worker was started")).run()
    assert ran == [("made", os.getpid()), ("run", os.getpid())] and multiprocessing.active_children() == []


def test_a_workers_link_hands_its_beats_over_and_stops_it_when_told_or_when_its_supervisor_is_gone(monkeypatch):
    """The worker's end of its pipe (``_Link``): the newest beat handed to the supervisor, a stop passed to its
    uvicorn server; and a supervisor gone (its end closed) stops the worker too, which ends itself once the time to
    finish is past, however busy it is."""
    from types import SimpleNamespace

    ended: list[int] = []
    monkeypatch.setattr(workers_module, "GRACEFUL_SECONDS", 0.2)
    monkeypatch.setattr(workers_module, "_END_SECONDS", 0.1)
    monkeypatch.setattr(workers_module, "os", SimpleNamespace(_exit=ended.append))
    context = multiprocessing.get_context("spawn")
    for gone in (False, True):
        supervisor_end, worker_end = context.Pipe(duplex=True)
        link = workers_module._Link(worker_end, 3)
        uvicorn = SimpleNamespace(should_exit=False)
        link.start()
        link.serving(uvicorn)  # type: ignore[arg-type]
        link.report({"instance": "3fa9c2e1b07d", "worker": 3})
        assert supervisor_end.poll(10) and json.loads(supervisor_end.recv_bytes()) == {
            "instance": "3fa9c2e1b07d",
            "worker": 3,
        }
        if gone:
            supervisor_end.close()
        else:
            supervisor_end.send_bytes(b"stop")
        _until(lambda u=uvicorn: u.should_exit, seconds=10, what="the stop")
        if gone:
            _until(lambda: ended == [1], seconds=10, what="its own end")
        else:
            time.sleep(0.5)
            assert ended == []  # stopped: its own run ends it
            link.close()
            supervisor_end.close()


def test_a_beat_holds_numbers_booleans_and_the_version_alone():
    good = {"instance": "3fa9c2e1b07d", "worker": 2, "version": "0.11.0", "ready": True, "memory": 7, "spaces": 1,
            "index": 0}  # fmt: skip
    assert beat_checked(json.dumps(good).encode()) == good
    assert beat_checked(json.dumps({**good, "worker": None, "memory": None}).encode()) == {
        **good, "worker": None, "memory": None,
    }  # fmt: skip
    for bad in (b"1", b"not json", b"[1]", json.dumps({**good, "instance": "Mario Rossi"}).encode(), b"{" * 5000):
        assert beat_checked(bad) is None
    loose = {**good, "worker": True, "memory": -1, "spaces": "2", "ready": 1, "version": "x" * 200, "words": CANARY}
    assert beat_checked(json.dumps(loose).encode()) == {
        "instance": "3fa9c2e1b07d", "worker": None, "version": None, "ready": None, "memory": None, "spaces": None,
        "index": 0,
    }  # fmt: skip


def test_the_process_memory_is_read_where_it_can_be():
    memory = instance.process_memory()
    if os.name == "nt" or Path("/proc/self/statm").exists():
        assert isinstance(memory, int) and memory > 1_000_000
    else:
        assert memory is None


# --- fix round 1: a start that fails every time, a slow start, a worker not ready, a start that fails partway -----


def _starts(supervisor: Supervisor, worker: int) -> list[float]:
    return [at for which, at in supervisor.starts if which == worker]


def test_a_worker_that_cannot_start_three_times_in_a_row_stops_the_server(tmp_path, monkeypatch, caplog):
    """I1: a worker that ends with exit 2 before its first beat (its settings, its database, its certificate refuse
    it) three times in a row stops every worker and the run, in fixed words, and lets the port go: `latet server run`
    then exits 2, as one instance that cannot start does, and its service manager decides."""
    assert workers_module.WORKER_REFUSALS == 3
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    settings = _settings()
    with (
        caplog.at_level(logging.INFO, logger="latet.server.workers"),
        _supervising(settings, 2, _fake(tmp_path, "refuse")) as supervisor,
    ):
        supervisor.thread.join(60)
        assert not supervisor.thread.is_alive(), "the run did not end"
    [raised] = supervisor.raised
    assert isinstance(raised, RuntimeError) and "cannot start" in str(raised) and CANARY not in str(raised)
    assert max(len(_starts(supervisor, 0)), len(_starts(supervisor, 1))) == 3  # the third refusal ends it
    assert all(len(_starts(supervisor, worker)) <= 3 for worker in (0, 1))
    assert "cannot start" in caplog.text
    with socket.socket() as again:  # the port is let go
        again.bind(("127.0.0.1", settings.port))


def test_a_worker_that_beats_and_then_cannot_start_stops_the_server_at_its_third_start(tmp_path, monkeypatch):
    """P1: a start can fail after the worker's first beat (its application's start, after the beat began: an audit
    write a read-only database refuses), and the worker then says exit 2, which counts whatever the beats said: the
    third in a row stops the server, as for a worker that never beat."""
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    with _supervising(_settings(), 2, _fake(tmp_path, "beat-then-2")) as supervisor:
        supervisor.thread.join(60)
        assert not supervisor.thread.is_alive(), "the run did not end"
    [raised] = supervisor.raised
    assert isinstance(raised, workers_module.CannotStart) and "could not start 3 times in a row" in str(raised)
    assert max(len(_starts(supervisor, worker)) for worker in (0, 1)) == 3


def test_a_worker_with_no_first_beat_in_its_time_is_ended_and_started_again(tmp_path, monkeypatch, caplog):
    """T10-S7: a start has ``WORKER_START_SECONDS`` for its first beat; past it the worker is ended (not counted as a
    start that failed) and started again. Five seconds here: a process spawned on a busy machine can take more than
    one to write its record, and was then ended before it, every time."""
    assert workers_module.WORKER_START_SECONDS == 120.0
    monkeypatch.setattr(workers_module, "WORKER_START_SECONDS", 5.0)
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    with (
        caplog.at_level(logging.INFO, logger="latet.server.workers"),
        _supervising(_settings(), 2, _fake(tmp_path, "mute")) as supervisor,
    ):
        _until(lambda: len(_records(tmp_path, 0)) >= 2, what="a second start")
        first = _records(tmp_path, 0)[0]
        _until(lambda: _gone(first["pid"]), what="the mute worker's end")
        assert supervisor.raised == []
    assert "worker 0 sent no beat within 5 seconds" in caplog.text


def test_a_worker_that_is_not_ready_is_never_started_again(tmp_path, monkeypatch):
    """T10-S7: a worker that beats and says it is not ready (PostgreSQL or Valkey away, a warm-up) stays: readiness is
    the load balancer's to judge."""
    monkeypatch.setattr(workers_module, "BEAT_SECONDS", 0.3)  # silent past 0.9 s: it beats far more often
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    with _supervising(_settings(), 2, _fake(tmp_path, "unready")) as supervisor:
        _until(lambda: all(supervisor.said(worker) is not None for worker in range(2)), what="their beats")
        time.sleep(3.0)  # ten restart intervals
        assert [len(_starts(supervisor, worker)) for worker in range(2)] == [1, 1]
        assert supervisor.said(0)["ready"] is False


def test_a_start_that_fails_partway_ends_the_workers_started_and_lets_the_port_go(tmp_path):
    """T10-S4/S6: the second worker's process cannot be started (no handle left, say): the first is stopped and
    reaped, the sockets closed, and the error raised."""
    settings = _settings()
    supervisor = Supervisor(settings, 2, target=_fake(tmp_path, "beat"))
    real = supervisor._context

    class Context:
        made = 0

        def Pipe(self, duplex=True):
            return real.Pipe(duplex=duplex)

        def Process(self, **kwargs):
            Context.made += 1
            if Context.made == 2:
                raise OSError(24, "Too many open files")
            return real.Process(**kwargs)

    supervisor._context = Context()  # type: ignore[assignment]
    with pytest.raises(OSError, match="Too many open files"):
        supervisor.run()
    first = supervisor.process(0)
    assert first is not None and not first.is_alive() and multiprocessing.active_children() == []
    with socket.socket() as again:
        again.bind(("127.0.0.1", settings.port))


def test_a_second_stop_ends_the_workers_at_once(tmp_path):
    """T10-S6: told twice to stop (a second Ctrl-C), the supervisor does not wait out the time to finish."""
    with _supervising(_settings(), 2, _fake(tmp_path, "stubborn")) as supervisor:
        _until(lambda: all(supervisor.said(worker) is not None for worker in range(2)), what="their beats")
        processes = [supervisor.process(worker) for worker in range(2)]
        started = time.monotonic()
        supervisor.stop()
        time.sleep(0.5)
        supervisor.stop()
        supervisor.thread.join(60)
    assert time.monotonic() - started < 15 < instance.GRACEFUL_SECONDS
    assert all(process.exitcode not in (0, None) for process in processes)


def test_the_process_memory_is_none_when_it_cannot_be_read(monkeypatch, tmp_path):
    """M6: a statm that is no statm, none at all, Windows saying no, or a reader that raises: None, never a number."""
    garbage = tmp_path / "statm"
    garbage.write_text("not numbers", "ascii")
    monkeypatch.setattr(instance, "Path", lambda _: garbage)
    assert instance._linux_memory() is None
    monkeypatch.setattr(instance, "Path", lambda _: tmp_path / "missing")
    assert instance._linux_memory() is None
    pytest.importorskip("ctypes.wintypes")

    class Function:
        def __init__(self, answer):
            self.answer = answer

        def __call__(self, *args):
            return self.answer

    class Kernel32:
        GetCurrentProcess = Function(-1)
        K32GetProcessMemoryInfo = Function(0)  # Windows' "no"

    monkeypatch.setattr(instance.ctypes, "WinDLL", lambda *args, **kwargs: Kernel32(), raising=False)
    assert instance._windows_memory() is None

    def raising():
        raise OSError("no such library")

    monkeypatch.setattr(instance, "_windows_memory", raising)
    monkeypatch.setattr(instance, "_linux_memory", raising)
    assert instance.process_memory() is None


def test_a_beat_says_none_for_what_it_cannot_read_and_still_beats(monkeypatch):
    """M10: a part of the telemetry that fails is None; the beat goes on, so telemetry never ends a worker."""
    from types import SimpleNamespace

    from latet.server import app

    def broken():
        raise RuntimeError("a space closing")

    monkeypatch.setattr(app, "process_memory", broken)
    server = SimpleNamespace(
        instance="3fa9c2e1b07d", worker=1, ready=True, spaces=SimpleNamespace(count=broken, index_bytes=broken)
    )
    said = app.beat_said(server)  # type: ignore[arg-type]
    assert said == {
        "instance": "3fa9c2e1b07d", "worker": 1, "version": app.__version__, "ready": True, "memory": None,
        "spaces": None, "index": None,
    }  # fmt: skip


def test_a_real_worker_that_cannot_start_says_so_in_fixed_words_and_nothing_else(tmp_path, monkeypatch, capfd):
    """T10-S10, across the process boundary: real workers (``worker_main``, spawned) whose database refuses them (an
    address of nothing, a password that is a canary) and whose master key lies under a canary's name say why in fixed
    words on their output, never the password, the path, the address or a traceback; the third refusal stops the
    run (I1)."""
    from latet.server.master import MasterKey

    folder = tmp_path / f"keys-{CANARY}"
    folder.mkdir()
    key = folder / "master.key"
    key.write_text(MasterKey.generate(), "utf-8")
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    settings = _settings(master_key_file=key, database_connect_timeout=2)
    with _supervising(settings, 2, workers_module.worker_main) as supervisor:
        supervisor.thread.join(180)
        assert not supervisor.thread.is_alive(), "the run did not end"
    out, err = capfd.readouterr()
    said = out + err
    assert "did not start" in said and "cannot start" in str(supervisor.raised[0])
    assert CANARY not in said and "Traceback" not in said and str(key) not in said and "127.0.0.1:1" not in said


def test_the_vaults_index_bytes_are_counted_once_a_minute_whoever_asks(monkeypatch):
    """M7: the beat (every ten seconds) and every scrape ask the vaults' index bytes, and a print cache's count walks
    its entries: they are counted once in ``INDEX_BYTES_SECONDS``, and said again meanwhile."""
    from types import SimpleNamespace

    from latet.server import spaces as spaces_module

    reads = []

    class Store:
        @property
        def index_bytes(self):
            reads.append(1)
            return 4096

    spaces = object.__new__(spaces_module.Spaces)
    spaces._spaces = {"k_a": SimpleNamespace(runtime=SimpleNamespace(store=Store()))}
    spaces._index_bytes = None
    assert spaces_module.INDEX_BYTES_SECONDS == 60.0
    assert [spaces.index_bytes() for _ in range(5)] == [4096] * 5 and len(reads) == 1
    monkeypatch.setattr(spaces_module, "INDEX_BYTES_SECONDS", 0.0)
    assert spaces.index_bytes() == 4096 and len(reads) == 2


# --- fix round 2: the measurement's lines, a start's OSError, a failure once running, the processors' share --------


def _bench_server(monkeypatch):
    """scripts/bench_apps_server.py, imported as the measurement runs it (its folder first on the path)."""
    import importlib.util
    import sys

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("bench_apps_server", scripts / "bench_apps_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bench_apps_server", module)
    spec.loader.exec_module(module)
    return module


def test_the_workers_measurement_offers_every_count_the_same_distinct_lines(monkeypatch):
    """I2 of the fix round: every count is offered the same pool of distinct lines, in calls of one size, and its
    clients take the calls from one shared count, so that no line is sent twice in a phase (a text masked before is
    answered from the pipeline's cache, which would count as throughput) and no client waits while lines are left. At
    least four connections a worker, and no more lines than the vault knows (each line's customer kept already)."""
    bench = _bench_server(monkeypatch)
    lines = bench.bench_apps.lines(10_000, 1, 0.5)
    assert len(set(lines)) == len(lines)
    calls = bench.calls_of(lines, 62)
    assert [line for call in calls for line in call] == lines and {len(call) for call in calls[:-1]} == {62}
    assert bench.workers_clients(8, 4, len(lines), 200_000) == 32 and bench.workers_clients(1, 4, 10, 10) == 4
    with pytest.raises(SystemExit, match="four connections"):
        bench.workers_clients(8, 3, len(lines), 200_000)
    with pytest.raises(SystemExit, match="values"):
        bench.workers_clients(8, 4, 300_000, 200_000)


def test_a_workers_client_sends_until_its_time_is_up_or_the_lines_run_out(monkeypatch):
    """Each client sends one call after the other on its connection, the next call of the shared count each time, until
    the phase's time is up or no call is left (when it says so): two clients at once take every call once."""

    bench = _bench_server(monkeypatch)
    calls = [[f"line {k}"] for k in range(50)]
    lock, taken = threading.Lock(), itertools.count()

    def claim() -> int:
        with lock:
            return next(taken)

    def post(texts):
        time.sleep(0.001)
        return [text.upper() for text in texts]

    found: list[dict] = []
    clients = [
        threading.Thread(target=lambda: found.append(bench.client_loop(post, calls, claim, time.time() + 60)))
        for _ in range(2)
    ]
    for client in clients:
        client.start()
    for client in clients:
        client.join(30)
    done = sorted(index for result in found for index, _ in result["done"])
    assert done == list(range(50)) and all(result["ran_out_at"] is not None for result in found)
    assert all(result["answers"][index] == [calls[index][0].upper()] for result in found for index in result["answers"])
    timed = bench.client_loop(post, calls, itertools.count().__next__, time.time() - 1)  # its time is up already
    assert timed["done"] == [] and timed["ran_out_at"] is None


def test_the_workers_measurement_counts_the_lines_answered_within_its_fixed_time(monkeypatch):
    """I2 of the fix round: a phase's throughput is the lines of the calls answered within its fixed time (from the
    go every client waited for), divided by that time, so that no wave ends with its slowest client; when the calls ran
    out first, the time ends where they did (every client was sending until then), and the measurement says so."""

    bench = _bench_server(monkeypatch)
    calls = [["a"] * 10, ["b"] * 10, ["c"] * 10, ["d"] * 10]
    results = [
        {"done": [[0, 101.0], [2, 109.5]], "ran_out_at": None},
        {"done": [[1, 104.0], [3, 110.5]], "ran_out_at": None},  # answered after the time: not counted
    ]
    assert bench.measured_window(results, calls, 100.0, 10.0) == (10.0, 30, False)
    results[1]["ran_out_at"] = 108.0
    assert bench.measured_window(results, calls, 100.0, 10.0) == (8.0, 20, True)


def test_a_workers_client_keeps_its_one_connection_however_long_it_waits(monkeypatch):
    """I3 of the re-review: a client waits at the go for the slowest of 32 to start, and httpx drops a connection idle
    for five seconds by default, then opens another, which the kernel may give to another worker than the one counted.
    A client of the measurement keeps its one connection however long it waits (its local port the same)."""
    import http.server

    bench = _bench_server(monkeypatch)
    accepted: list[tuple[str, int]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            accepted.append(self.client_address)

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    served = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=served.serve_forever, daemon=True).start()
    try:
        with bench.phase_http(f"http://127.0.0.1:{served.server_address[1]}", "latet_k_x") as http_client:
            first = bench.local_port(http_client.get("/_latet/ready"))
            time.sleep(5.5)  # past httpx's own keep-alive time
            again = bench.local_port(http_client.get("/_latet/ready"))
        assert len(accepted) == 1, accepted
        assert first is not None and first == again == accepted[0][1]
    finally:
        served.shutdown()
        served.server_close()


def test_the_workers_measurement_trusts_the_connections_it_counts_only_when_they_add_up(monkeypatch):
    """I3 of the re-review: the workers' connections are read while the phase runs, every client sending; their sum is
    printed beside the clients', and a sum that differs, or a client that connected again during the phase, gives no
    number from the round. Read after the lines ran out (clients closing), they are not trusted, and the line says so;
    unknown (not Linux), they are said as such."""

    bench = _bench_server(monkeypatch)
    steady = [{"ran_out_at": None, "ports": [50001, 50001]}, {"ran_out_at": None, "ports": [50002, 50002]}]
    assert bench.connections_read([4, 4], 8, 105.0, steady) == "[4, 4] (8 of 8 clients)"
    with pytest.raises(SystemExit, match="5 of 8"):
        bench.connections_read([1, 4], 8, 105.0, steady)
    moved = [*steady, {"ran_out_at": None, "ports": [50003, 50009]}]
    with pytest.raises(SystemExit, match="connected again"):
        bench.connections_read([4, 4], 8, 105.0, moved)
    early = [{"ran_out_at": 103.0, "ports": [50001, 50001]}, *steady]
    assert "not read while every client sent" in bench.connections_read([1, 4], 8, 105.0, early)
    assert bench.connections_read([None, None], 8, 105.0, steady) == "[?, ?] (unknown here)"


def test_the_workers_measurement_checks_every_line_back(monkeypatch):
    """M7 of the re-review: every line of the pool is unmasked (the lines no client reached in the timed phase too,
    untimed) and compared; a call missing counts as changed. The pool is the vault's values unless ``--lines`` says
    less."""
    bench = _bench_server(monkeypatch)
    calls = [["a", "b"], ["c"], ["d"]]
    assert bench.lines_changed(calls, {0: ["a", "b"], 1: ["c"], 2: ["d"]}) == 0
    assert bench.lines_changed(calls, {0: ["a", "x"], 2: ["d"]}) == 2
    assert bench.parser().parse_args(["workers"]).lines is None


def test_the_workers_measurement_reads_each_workers_connections_and_processor_time(monkeypatch):
    """I2 of the fix round: on Linux the measurement records how many of the clients' connections each worker holds
    (its sockets that are established on the port, ``/proc/net/tcp``) and the processor time it spent in each phase
    (``/proc/<pid>/stat``), so that a miss can be laid at the spread or at the workers."""

    bench = _bench_server(monkeypatch)
    table = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:14A3 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 111 1\n"
        "   1: 0100007F:14A3 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 222 1\n"
        "   2: 0100007F:C350 0100007F:14A3 01 00000000:00000000 00:00000000 00000000  1000        0 333 1\n"
        "   3: 0100007F:14A4 0100007F:C351 01 00000000:00000000 00:00000000 00000000  1000        0 444 1\n"
    )
    assert bench.established_on(table, 0x14A3) == {222}
    stat = "4242 (python3 (latet)) S 1 4242 4242 0 -1 4194560 100 0 0 0 250 50 0 0 20 0 9 0 1 2 3"
    assert bench.processor_seconds(stat, 100) == 3.0


def test_the_workers_measurement_warms_every_worker_with_2n_calls_at_once(monkeypatch):
    """The measurement's warm-up gives N workers 2N calls at once, each on a connection of its own, as its waves' 2N
    clients do, until every worker's beat says it holds the key's space: calls one at a time went to the same few
    workers on Linux, and eight were never all warmed (notes/server-performance-task9-10-results.md)."""
    from types import SimpleNamespace

    bench = _bench_server(monkeypatch)
    count = 4
    together = threading.Barrier(2 * count, timeout=5)  # broken unless 2N calls are under way at once
    clients: list[dict] = []
    calls: list[tuple[str, dict]] = []

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json):
            calls.append((path, json))
            together.wait()
            return SimpleNamespace(raise_for_status=lambda: None)

    looks: list[int] = []

    class Server:
        def instances(self):
            looks.append(1)
            return [{"instance": f"{k:012x}", "spaces": int(len(looks) > 1)} for k in range(count)]

    monkeypatch.setattr(bench.httpx, "Client", Client)
    alive = {f"{k:012x}" for k in range(count)}
    bench._warmed(Server(), "http://127.0.0.1:1", "latet_k_x", count, alive, seconds=60)
    assert len(calls) == len(clients) == 2 * count and len(looks) == 2
    assert all(path == "/_latet/apps/v1/mask" for path, _ in calls)


def test_the_measurements_script_imports_without_pytest():
    """scripts/bench_apps_server.py runs where no development tool is installed (the container image, for the workers'
    measurement on Linux): it imports nothing that needs pytest."""
    root = Path(__file__).resolve().parents[2]
    code = "import sys; sys.modules['pytest'] = None; sys.path.insert(0, 'scripts'); import bench_apps_server"
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]


def _worker_main_here(monkeypatch, capsys, made, own: Sequence[socket.socket] | None = ()):
    """``worker_main`` run in this process with ``made`` as its instance (a stand-in), its log restored after: its exit
    code and what it said. ``own``: the sockets it is given (None: none, as on Linux). The processors' share it sets
    (m2) is put back after the test."""
    from latet.server.logs import saved
    from latet.vision import models

    if models.runtime_available():
        from latet.vision import pool

        monkeypatch.setattr(pool, "_SHARE", 1)
    monkeypatch.setattr(instance, "make_instance", made)
    supervisor_end, worker_end = multiprocessing.get_context("spawn").Pipe(duplex=True)
    code = 0
    try:
        with saved():
            try:
                workers_module.worker_main(_settings(workers=3), 1, None if own is None else list(own), worker_end)
            except SystemExit as exc:
                code = exc.code
    finally:
        supervisor_end.close()
    return code, capsys.readouterr().out


def test_a_worker_whose_start_meets_an_os_error_refuses_as_one_instance_does(monkeypatch, capsys):
    """m1: an OSError before the run (a folder refused, say) is a start refused, exit 2, as `latet server run` says of
    one instance; said by its type alone. And the worker reads images with its share of the processors (m2)."""

    from latet.vision import models

    sized = models.runtime_available()

    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied", f"C:/{CANARY}/data")

    code, said = _worker_main_here(monkeypatch, capsys, refused)
    assert code == 2 and "worker 1 did not start (PermissionError" in said and CANARY not in said
    if sized:
        from latet.vision import pool

        assert pool._SHARE == 3  # one of the settings' three workers


@pytest.mark.parametrize("kind", [ValueError, RuntimeError])
def test_a_workers_start_refused_by_another_error_is_said_by_its_type_alone(monkeypatch, capsys, kind):
    """A ValueError or a RuntimeError of no kind of Latet's own that stops a worker's start is said by its type alone,
    as any other error is: its words can quote what was being read (an address and its password, a value, a folder),
    and no line of a server's log holds an error's words (tests/unit/test_logging_calls.py). Exit 2, counted."""

    def refused(*args, **kwargs):
        raise kind(f"a value read: {CANARY}, in /home/someone/{CANARY}")  # nothing the log's own redaction hides

    code, said = _worker_main_here(monkeypatch, capsys, refused)
    assert code == 2 and f"worker 1 did not start ({kind.__name__})" in said and CANARY not in said


def _metrics_token_refused(tmp_path):
    from types import SimpleNamespace

    from latet.server.operations import _metrics_token

    empty = tmp_path / "metrics.token"
    empty.write_text("\n", "utf-8")
    _metrics_token(SimpleNamespace(settings=SimpleNamespace(metrics_token_file=empty)))  # type: ignore[arg-type]


def _secret_file_refused(tmp_path):
    from latet.server.settings import read_secret_file

    read_secret_file(tmp_path / "missing.key", "reviewer_key_file")


def _database_address_refused(tmp_path):
    from latet.server.db import _parameters

    _parameters("postgresql://latet@db.acme.lan/latet?sslmode=verify-full", "latet", 10)


@pytest.mark.parametrize(
    ("refuse", "words"),
    [
        (_metrics_token_refused, "metrics_token_file names a file that holds no token"),
        (_secret_file_refused, "reviewer_key_file names a file that cannot be read"),
        (_database_address_refused, "the database address asks for a verified connection (sslmode) and names no "
         "sslrootcert"),
    ],
)  # fmt: skip
def test_a_workers_start_refused_by_a_setting_is_said_in_the_refusals_fixed_words(
    monkeypatch, capsys, tmp_path, refuse, words
):
    """I3 of the suite fix's review: a setting the server refuses as it starts (the database's address, a secret's file,
    the metrics token's) stops a worker in the refusal's fixed words, which name the setting and never what it holds,
    as `latet server run` prints one instance's, so that the line that stops the server after three ("its lines above
    say why") is true. The refusal is a SettingRefused, a ValueError still."""
    from latet.server.start_errors import SettingRefused

    with pytest.raises(SettingRefused) as refused:
        refuse(tmp_path)
    assert isinstance(refused.value, ValueError)

    code, said = _worker_main_here(monkeypatch, capsys, lambda *args, **kwargs: refuse(tmp_path))
    assert code == 2 and f"worker 1 did not start: {words}" in said


def test_a_worker_whose_server_cannot_be_imported_refuses_by_the_errors_type(monkeypatch, capsys):
    """I1 of the suite fix's review: a worker whose server application cannot be imported (numpy missing or broken, an
    upgrade that replaced files under a running supervisor) refuses its start by the error's type, exit 2, which the
    supervisor counts: the import's words, which name folders, are never said, and nothing escapes the worker's own
    handlers for multiprocessing to print."""
    import importlib
    import importlib.abc
    import sys

    class Broken(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "latet.server.app":
                raise ImportError(f"numpy cannot be loaded from /home/{CANARY}/site-packages")
            return None

    monkeypatch.delitem(sys.modules, "latet.server.app", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])

    def made(*args, **kwargs):
        return importlib.import_module("latet.server.app")  # as make_instance imports it

    code, said = _worker_main_here(monkeypatch, capsys, made)
    assert code == 2 and "worker 1 did not start (ImportError)" in said and CANARY not in said


def test_workers_whose_server_cannot_be_imported_stop_the_server_at_the_third_start(tmp_path, monkeypatch, capfd):
    """I1 of the suite fix's review, end to end: real workers (``worker_main``, ``spawn``) in processes where the server
    application cannot be imported each refuse with exit 2, said by the error's type, and the third refusal in a row
    stops the server, as for any start that fails; no traceback and no word of the import is written."""
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    target = functools.partial(workers_fakes.no_server_app, canary=CANARY)
    with _supervising(_settings(), 2, target) as supervisor:
        supervisor.thread.join(150)
        assert not supervisor.thread.is_alive(), "the run did not end"
    [raised] = supervisor.raised
    assert isinstance(raised, workers_module.CannotStart) and "could not start 3 times in a row" in str(raised)
    assert max(len(_starts(supervisor, worker)) for worker in (0, 1)) == 3
    written = capfd.readouterr()
    said = written.out + written.err
    assert "did not start (ImportError)" in said and CANARY not in said and "Traceback" not in said


@pytest.mark.parametrize(
    ("module", "name", "words"),
    [
        ("latet.server.db", "SchemaMismatch", "the database's schema latet is older than this Latet (2 to apply): run "
         "`latet server migrate`"),
        ("latet.server.db", "DatabaseError", "the connection to PostgreSQL failed (OperationalError)"),
        ("latet.server.master", "MasterKeyError", "the master key is 32 bytes, in base64"),
        ("latet.server.app", "ValkeyUnavailable", "Valkey (valkey_url) refused this instance or did not answer in time "
         "(ConnectionError): check its address and its password (valkey_password_file)"),
        ("latet.server.instance", "CannotListen", "the metrics socket cannot be bound on metrics_host and metrics_port: "
         "its address and port are in use: another program listens there"),
    ],
)  # fmt: skip
def test_a_workers_start_refused_by_latets_own_start_error_is_said_in_its_fixed_words(
    monkeypatch, capsys, module, name, words
):
    """Latet's own start errors, whose words are fixed and name no value (the schema, the database, the master key,
    Valkey, a socket), are said in those words, as `latet server run` prints one instance's. Exit 2, counted."""
    import importlib

    error = getattr(importlib.import_module(module), name)

    def refused(*args, **kwargs):
        raise error(words)

    code, said = _worker_main_here(monkeypatch, capsys, refused)
    assert code == 2 and f"worker 1 did not start: {words}" in said


def test_a_worker_whose_certificate_cannot_be_used_says_so_in_fixed_words(monkeypatch, capsys):
    """The instance's certificate and key, loaded at its start (``instance._loaded``), that cannot be used stop a
    worker's start in the start error's fixed words, as they stop one instance's: the settings and the error's type,
    never what the file holds or the library's words. Exit 2, counted."""
    import ssl
    from types import SimpleNamespace

    def load():
        raise ssl.SSLError(f"[SSL] PEM lib: {CANARY}")

    def refused(*args, **kwargs):
        instance._loaded(SimpleNamespace(config=SimpleNamespace(load=load)))

    code, said = _worker_main_here(monkeypatch, capsys, refused)
    assert code == 2 and CANARY not in said
    assert (
        "worker 1 did not start: the instance's certificate and key (certificate, key) cannot be used (SSLError)"
        in said
    )


def test_a_worker_that_fails_once_running_says_it_stopped_and_is_started_again(monkeypatch, capsys, tmp_path):
    """m6: a worker whose run fails once it began says "stopped" and ends with exit 1, which no count of refusals
    weighs: started again every time. A RuntimeError of no kind of Latet's own is said by its type alone."""
    from types import SimpleNamespace

    def failing():
        raise RuntimeError(f"words that quote what was read: {CANARY}")

    serving = SimpleNamespace(should_exit=False, started=True)  # uvicorn served before the run failed
    made = SimpleNamespace(server=SimpleNamespace(), uvicorn=serving, run=failing)
    code, said = _worker_main_here(monkeypatch, capsys, lambda *args, **kwargs: made)
    assert code == 1 and "worker 1 stopped (RuntimeError)" in said and "did not start" not in said
    assert CANARY not in said
    monkeypatch.setattr(workers_module, "WORKER_RESTART_SECONDS", 0.3)
    with _supervising(_settings(), 2, _fake(tmp_path, "fail")) as supervisor:
        _until(lambda: len(_starts(supervisor, 0)) >= 5, what="five starts")
        assert supervisor.thread.is_alive() and supervisor.raised == []


def test_each_worker_reads_images_with_its_share_of_the_processors(monkeypatch):
    """m2: N workers on one machine each size their text recognitions (how many readers, how many threads each) by
    their share of the processors, one at least, not every one of them each."""
    from latet.vision import models

    if not models.runtime_available():
        pytest.skip("the ocr extra is not installed: no text recognition to size")
    from latet.vision import pool

    monkeypatch.setattr(pool, "_SHARE", 1)
    twenty = {"affinity": lambda: 20, "cpu_max": lambda: None}
    assert pool.usable_cpus(**twenty) == 20
    workers_module.share_processors(8)
    assert pool.usable_cpus(**twenty) == 2
    workers_module.share_processors(64)
    assert pool.usable_cpus(**twenty) == 1


def test_a_worker_whose_application_cannot_start_inside_its_run_refuses_with_exit_2(monkeypatch, capsys):
    """P1: uvicorn ends a run whose application could not start (its lifespan failed) with SystemExit(3), before it
    ever served: the worker says it did not start, in fixed words, and ends with exit 2, which the supervisor counts."""
    from types import SimpleNamespace

    def lifespan_failed():
        raise SystemExit(3)

    made = SimpleNamespace(
        server=SimpleNamespace(), uvicorn=SimpleNamespace(should_exit=False, started=False), run=lifespan_failed
    )
    code, said = _worker_main_here(monkeypatch, capsys, lambda *args, **kwargs: made)
    assert code == 2 and "worker 1 did not start: its application's start failed" in said
