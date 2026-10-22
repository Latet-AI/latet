import socket
import time
import tomllib
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from latet.service import daemon, ports


@pytest.fixture
def listener():
    """Another program listening on a local port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture
def listener_everywhere():
    """Another program listening on every address of this computer (0.0.0.0), as many servers and Docker's published
    ports do."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture(autouse=True)
def no_windows_ranges(monkeypatch):
    monkeypatch.setattr(ports, "excluded_ranges", lambda: ())


def test_free_busy_and_reserved_ports(listener, monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    busy = ports.probe(listener)
    assert not busy.usable and busy.reason == "in use by another program"
    monkeypatch.setattr(ports, "excluded_ranges", lambda: ((listener - 1, listener + 1),))
    reserved = ports.probe(listener)
    assert not reserved.usable and reserved.reason.startswith("reserved by Windows")


def test_a_probe_that_meets_itself_is_no_program(monkeypatch):
    """Where the port is among the system's own for outgoing connections (on Windows from 1024 on some computers), the
    probe's connection may be given that very port and meet itself (TCP's simultaneous open): no program listens."""

    class Itself:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, seconds):
            pass

        def connect_ex(self, address):
            return 0

        def getsockname(self):
            return ("127.0.0.1", 50001)

        def getpeername(self):
            return ("127.0.0.1", 50001)

    monkeypatch.setattr(ports.socket, "socket", Itself)
    assert ports._listened(50001) is False


def test_a_program_listening_on_every_address_holds_the_port_too(listener_everywhere, monkeypatch):
    """On macOS a bind to 127.0.0.1 with SO_REUSEADDR succeeds beside a program that listens on every address: the
    daemon would start there and take that program's connections to 127.0.0.1. The port is in use all the same."""
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    busy = ports.probe(listener_everywhere)
    assert not busy.usable and busy.reason == "in use by another program"


@pytest.mark.parametrize("system", ["posix", "nt"])
def test_the_port_is_bound_as_the_daemon_binds_it(monkeypatch, system):
    """Right after `latet stop` the port's closed connections wait out (TIME_WAIT) for some seconds: on Linux and
    macOS the daemon binds with SO_REUSEADDR, as uvicorn does, and starts all the same, so the check binds so too (a
    program that listens there still makes it fail: test_free_busy_and_reserved_ports). On Windows that option would
    let it share a port another program holds: the check asks for the port alone there, as before."""
    calls = []

    class Recorder:
        def __init__(self, *args):
            pass

        def setsockopt(self, level, name, value):
            calls.append(name)

        def bind(self, address):
            calls.append(address)

        def close(self):
            pass

    monkeypatch.setattr(ports.socket, "socket", Recorder)
    monkeypatch.setattr(ports.os, "name", system)
    assert ports._bind_error(5283) is None
    assert (socket.SO_REUSEADDR in calls) == (system == "posix")
    assert calls[-1] == ("127.0.0.1", 5283)


def test_a_port_latet_already_listens_on_is_usable(listener, monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=True))
    assert ports.probe(listener) == ports.PortState(listener, True, "latet")


def test_choose_skips_unusable_ports(listener, monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    free = ports.choose(listener, fallback=[listener, listener + 1, listener + 2])
    assert free.usable and free.port != listener
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, False, "in use by another program"))
    with pytest.raises(ports.NoFreePortError):
        ports.choose(5283, fallback=[5284])


def test_start_answers_at_once_when_the_port_is_taken(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=5283))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, False, "in use by another program"))
    started = time.monotonic()
    current = daemon.start()
    assert time.monotonic() - started < 2
    assert not current.running and current.reason == "in use by another program"


def test_the_daemon_runs_in_its_own_folder_not_the_one_it_was_started_from(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("LATET_DATA_DIR", "data")  # relative to the project: made absolute for the daemon
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=5283))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    launched = {}
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda argv, **kwargs: launched.update(kwargs))
    daemon.start(wait=0)
    data = (project / "data").resolve()
    assert launched["cwd"] == data / "logs"
    assert launched["env"]["LATET_DATA_DIR"] == str(data)


def test_global_install_moves_to_a_free_port_and_repoints_agents(tmp_path, monkeypatch):
    import latet.cli.admin  # noqa: F401 - registers global-install
    from latet import wrappers
    from latet.cli.main import app
    from latet.config import load_config
    from latet.integrations.agents import Codex
    from latet.service import autostart

    for name, sub in (("LATET_DATA_DIR", "data"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex"),
                      ("XDG_CONFIG_HOME", "xdg")):  # fmt: skip
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(autostart, "install", lambda: None)
    monkeypatch.setattr(daemon, "start", lambda: daemon.Status(False, port=5284))
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 161, 0))  # a Codex that reads config.toml
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=port or 5284))
    Codex().apply(chatgpt=True)  # set up earlier, on 5283
    taken = {5283}
    monkeypatch.setattr(
        ports,
        "probe",
        lambda port: ports.PortState(port, port not in taken, "in use by another program" if port in taken else "free"),
    )
    result = CliRunner().invoke(
        app, ["global-install", "--yes", "--no-identity", "--agents", "none", "--no-ner", "--no-autostart"]
    )
    assert result.exit_code == 0, result.output
    assert "5284: 5283 is in use by another program, and your agents move with it" in result.output
    assert load_config(tmp_path / "config.yaml").port == 5284
    codex = tomllib.loads((tmp_path / "codex" / "config.toml").read_text("utf-8"))
    assert codex["openai_base_url"].startswith("http://127.0.0.1:5284/")
    bad = CliRunner().invoke(app, ["global-install", "--yes", "--port", "5283", "--agents", "none", "--no-ner"])
    assert bad.exit_code == 1 and "5283 is in use" in bad.output


def test_global_install_makes_the_daemon_that_stays_take_its_settings_first(tmp_path, monkeypatch):
    """A daemon that keeps its port takes the saved settings before the agents are routed; one stopped for a move
    to another port is not asked, nor is one that is not running."""
    import latet.cli.admin  # noqa: F401 - registers global-install
    from latet.cli import admin
    from latet.cli.main import app
    from latet.config import load_config
    from latet.service import autostart

    for name, sub in (("LATET_DATA_DIR", "data"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex"),
                      ("XDG_CONFIG_HOME", "xdg")):  # fmt: skip
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setattr("shutil.which", lambda name: None)  # no agent is installed
    monkeypatch.setattr(autostart, "install", lambda: None)
    listening, asked = {"on": 5283}, []

    def status(port=None):
        port = port or load_config().port
        return daemon.Status(listening["on"] == port, port=port)

    monkeypatch.setattr(daemon, "status", status)
    monkeypatch.setattr(daemon, "stop", lambda: listening.update(on=0) or True)
    monkeypatch.setattr(daemon, "start", lambda: daemon.Status(False, port=load_config().port))  # no last reload
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    monkeypatch.setattr(admin, "_admin", lambda method, path, body=None, timeout=60: asked.append(path) or {"ok": True})
    args = ["global-install", "--yes", "--no-identity", "--agents", "none", "--no-ner", "--no-autostart"]
    assert CliRunner().invoke(app, args).exit_code == 0
    assert asked == ["/_latet/admin/reload"]  # it keeps its port
    asked.clear()
    assert CliRunner().invoke(app, [*args, "--port", "6650"]).exit_code == 0
    assert listening["on"] == 0 and load_config().port == 6650 and asked == []  # stopped: it moves
    assert CliRunner().invoke(app, args).exit_code == 0
    assert asked == []  # not running


class _Process:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def _launch(monkeypatch, tmp_path, process, answers_after):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    calls = []

    def status(port=None):
        calls.append(1)
        return daemon.Status(len(calls) > answers_after, version="x", port=5283)

    monkeypatch.setattr(daemon, "status", status)


def test_a_slow_first_start_is_waited_for_and_said(tmp_path, monkeypatch):
    clock = iter(range(0, 1000))
    _launch(monkeypatch, tmp_path, _Process(), answers_after=30)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: float(next(clock)))
    told = []
    current = daemon.start(wait=15, patience=90, slow=lambda: told.append(True))
    assert current.running and told == [True]


def test_a_daemon_that_stops_while_starting_is_told_at_once(tmp_path, monkeypatch):
    _launch(monkeypatch, tmp_path, _Process(code=1), answers_after=10_000)
    started = time.monotonic()
    current = daemon.start(wait=15)
    assert time.monotonic() - started < 2
    assert not current.running and current.exited == 1
