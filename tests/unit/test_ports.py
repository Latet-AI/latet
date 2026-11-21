import socket
import subprocess
import sys
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


# The real readers of Windows' tables, before the fixture below stands in for them.
_EXCLUDED = ports.excluded_ranges
_DYNAMIC = getattr(ports, "dynamic_range", None)


@pytest.fixture(autouse=True)
def no_windows_ranges(monkeypatch):
    monkeypatch.setattr(ports, "excluded_ranges", lambda: ())
    monkeypatch.setattr(ports, "dynamic_range", lambda: None, raising=False)
    # Windows' system folder, as Windows would give it, on any system the tests run on
    monkeypatch.setattr(ports, "system_folder", lambda: "C:\\Windows\\System32", raising=False)


# `netsh int ipv4 show excludedportrange protocol=tcp` on the owner's Windows, in Italian (2026-11-15).
OWNER_EXCLUDED = """
Intervallo di esclusione porte protocollo tcp

Porta iniziale    Porta finale
----------    --------
      5241        5340
     50000       50059     *

* - Esclusioni porte amministrate.
"""


def _netsh(monkeypatch, outputs: dict[str, str]) -> list[list[str]]:
    """netsh as Windows answers it: the output of the first key all of whose words are in the command line."""
    asked: list[list[str]] = []

    def run(argv, **kwargs):
        asked.append(list(argv))
        text = next((out for key, out in outputs.items() if all(word in argv for word in key.split())), "")
        return SimpleNamespace(stdout=text, stderr="", returncode=0)

    monkeypatch.setattr(ports.subprocess, "run", run)
    monkeypatch.setattr(ports.os, "name", "nt")
    return asked


def test_the_excluded_ranges_tell_an_administrators_from_windows_own(monkeypatch):
    asked = _netsh(monkeypatch, {"excludedportrange": OWNER_EXCLUDED})
    monkeypatch.setattr(ports, "excluded_ranges", _EXCLUDED)
    _EXCLUDED.cache_clear()
    try:
        assert ports.excluded_ranges() == (
            ports.ExcludedRange(5241, 5340, False),
            ports.ExcludedRange(50000, 50059, True),
        )
    finally:
        _EXCLUDED.cache_clear()
    assert asked and all("ipv4" in argv and "ipv6" not in argv for argv in asked)


def test_netsh_output_in_the_consoles_code_page_never_fails_to_decode(monkeypatch):
    """German netsh writes cp850 ("Verwaltete Portausschlüsse": ü is 0x81), which a Windows locale of cp1252 cannot
    decode: the fake decodes the bytes as subprocess.run would with the kwargs it is given."""
    german = "Verwaltete Portausschlüsse\n\n      5241        5340\n     50000       50059     *\n"

    def run(argv, **kwargs):
        encoding = kwargs.get("encoding") or "cp1252"
        errors = kwargs.get("errors") or "strict"
        assert kwargs.get("text") or kwargs.get("encoding")
        return SimpleNamespace(stdout=german.encode("cp850").decode(encoding, errors), stderr="", returncode=0)

    monkeypatch.setattr(ports.subprocess, "run", run)
    monkeypatch.setattr(ports.os, "name", "nt")
    monkeypatch.setattr(ports, "excluded_ranges", _EXCLUDED)
    _EXCLUDED.cache_clear()
    try:
        assert ports.excluded_ranges() == (
            ports.ExcludedRange(5241, 5340, False),
            ports.ExcludedRange(50000, 50059, True),
        )
    finally:
        _EXCLUDED.cache_clear()


# The same table with no range in it, in English.
EMPTY_EXCLUDED = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------

* - Administered port exclusions.
"""


def test_the_table_read_fresh_parses_the_owners_italian_output_each_time(monkeypatch):
    """For a decision that changes something: read again at each call, never from the cache."""
    reader = getattr(ports, "read_excluded_ranges", None)
    assert reader is not None, "ports.read_excluded_ranges does not exist"
    asked = _netsh(monkeypatch, {"excludedportrange": OWNER_EXCLUDED})
    expected = (ports.ExcludedRange(5241, 5340, False), ports.ExcludedRange(50000, 50059, True))
    assert reader() == expected
    assert reader() == expected
    assert len(asked) == 2


def test_a_table_with_no_range_is_read_as_empty(monkeypatch):
    reader = getattr(ports, "read_excluded_ranges", None)
    assert reader is not None, "ports.read_excluded_ranges does not exist"
    _netsh(monkeypatch, {"excludedportrange": EMPTY_EXCLUDED})
    assert reader() == ()


@pytest.mark.parametrize(
    "answer",
    [
        FileNotFoundError(2, "No such file or directory"),
        subprocess.TimeoutExpired(["netsh"], 10),
        SimpleNamespace(stdout="", stderr="", returncode=0),
        SimpleNamespace(stdout="The following command was not found: int ipv4 show.\n", stderr="", returncode=1),
        SimpleNamespace(stdout="Ok.\n", stderr="", returncode=0),
        SimpleNamespace(stdout=OWNER_EXCLUDED, stderr="", returncode=1),
    ],
    ids=["not there", "timed out", "nothing printed", "an error", "no table", "a failure that printed a table"],
)
def test_a_table_that_cannot_be_read_fresh_is_an_error_never_an_empty_one(monkeypatch, answer):
    """An empty tuple would say that no range holds any port: the ports Latet reserved would look gone."""
    reader = getattr(ports, "read_excluded_ranges", None)
    assert reader is not None, "ports.read_excluded_ranges does not exist"

    def run(argv, **kwargs):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(ports.subprocess, "run", run)
    monkeypatch.setattr(ports.os, "name", "nt")
    unreadable = getattr(ports, "TableUnreadable", None)
    assert unreadable is not None and issubclass(unreadable, OSError)
    with pytest.raises(unreadable):
        reader()


def test_netsh_is_windows_own_and_opens_no_window(monkeypatch):
    """Never a bare "netsh", which Windows looks for in the current folder before System32; and no console window
    from the windowless daemon, whose Port check reads these tables."""
    seen = []

    def run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return SimpleNamespace(stdout=OWNER_EXCLUDED, stderr="", returncode=0)

    monkeypatch.setattr(ports.subprocess, "run", run)
    monkeypatch.setattr(ports.os, "name", "nt")
    ports.read_excluded_ranges()
    monkeypatch.setattr(ports, "excluded_ranges", _EXCLUDED)
    _EXCLUDED.cache_clear()
    try:
        ports.excluded_ranges()
    finally:
        _EXCLUDED.cache_clear()
    assert len(seen) == 2
    for argv, kwargs in seen:
        assert argv[0] == "C:\\Windows\\System32\\netsh.exe"
        assert kwargs.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' flag")
def test_netsh_is_started_with_no_console_window_on_windows():
    assert ports._NO_WINDOW == 0x08000000


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal off Windows")
def test_the_table_is_never_read_fresh_off_windows():
    with pytest.raises(ports.TableUnreadable):
        ports.read_excluded_ranges()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            """Intervallo porte dinamiche tcp protocollo
---
Porta iniziale      : 1024
Numero di porte     : 64511
""",
            (1024, 65534),
        ),
        (
            """Protocol tcp Dynamic Port Range
---
Start Port      : 49152
Number of Ports : 16384
""",
            (49152, 65535),
        ),
    ],
    ids=["italian", "english"],
)
def test_the_dynamic_range_is_read_in_any_language(monkeypatch, text, expected):
    assert _DYNAMIC is not None, "ports.dynamic_range does not exist"
    _netsh(monkeypatch, {"dynamicport": text})
    _DYNAMIC.cache_clear()
    try:
        assert _DYNAMIC() == expected
    finally:
        _DYNAMIC.cache_clear()


def test_a_port_has_one_of_four_findings(monkeypatch):
    monkeypatch.setattr(ports.os, "name", "nt")
    monkeypatch.setattr(
        ports,
        "excluded_ranges",
        lambda: (ports.ExcludedRange(5241, 5340, False), ports.ExcludedRange(50000, 50059, True)),
    )
    monkeypatch.setattr(ports, "dynamic_range", lambda: (1024, 65534))
    assert [ports.reservation(port) for port in (5283, 50001, 6655)] == ["held", "reserved", "at-risk"]
    monkeypatch.setattr(ports, "dynamic_range", lambda: (49152, 65535))
    assert ports.reservation(6655) == "safe"
    monkeypatch.setattr(ports.os, "name", "posix")
    assert ports.reservation(5283) == "safe"


def test_an_administered_range_never_makes_a_port_unusable(monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(ports, "excluded_ranges", lambda: (ports.ExcludedRange(port - 1, port + 1, True),))
    assert ports.probe(port) == ports.PortState(port, True, "free")


def test_choose_finds_a_free_port_past_windows_block(monkeypatch):
    monkeypatch.setattr(ports, "excluded_ranges", lambda: (ports.ExcludedRange(5241, 5340, False),))
    probed: list[int] = []

    def probe(port):
        probed.append(port)
        held = 5241 <= port <= 5340
        return ports.PortState(port, not held, "reserved by Windows (5241-5340)" if held else "free")

    monkeypatch.setattr(ports, "probe", probe)
    assert ports.choose(5283).port == 5341
    assert [port for port in probed if 5284 <= port <= 5340] == []


def test_a_move_skips_the_ports_an_administrator_reserved_unless_latet_did(monkeypatch):
    """Latet can listen in an administered range, but one Latet did not make is someone's: never Latet's new port,
    which would then be recorded as Latet's own. A port on Latet's record (``ours``) is Latet's to move back to."""
    monkeypatch.setattr(
        ports,
        "excluded_ranges",
        lambda: (
            ports.ExcludedRange(5241, 5340, False),
            ports.ExcludedRange(5341, 5400, True),
            ports.ExcludedRange(5402, 5402, True),
        ),
    )
    taken = {5283, 5401}
    monkeypatch.setattr(
        ports,
        "probe",
        lambda port: ports.PortState(port, port not in taken, "in use by another program" if port in taken else "free"),
    )
    assert ports.free_port(5283).port == 5403
    assert ports.free_port(5283, ours=[5402]).port == 5402
    assert ports.free_port(5283, ours=[5350]).port == 5350
    assert ports.choose(5283).port == 5403
    assert ports.choose(5283, ours=[5341]).port == 5341


def test_global_install_moves_past_ranges_latet_did_not_reserve(monkeypatch):
    from latet.cli import admin
    from latet.service import reservation

    monkeypatch.setattr(
        ports,
        "excluded_ranges",
        lambda: (ports.ExcludedRange(5241, 5340, False), ports.ExcludedRange(5341, 5400, True)),
    )
    monkeypatch.setattr(
        ports,
        "probe",
        lambda port: ports.PortState(port, port != 5283, "in use by another program" if port == 5283 else "free"),
    )
    monkeypatch.setattr(reservation, "recorded", lambda: [])
    assert admin._install_port(5283, None)[0] == 5401
    monkeypatch.setattr(reservation, "recorded", lambda: [5341])
    assert admin._install_port(5283, None)[0] == 5341


def test_free_busy_and_reserved_ports(listener, monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda port=None: SimpleNamespace(running=False))
    busy = ports.probe(listener)
    assert not busy.usable and busy.reason == "in use by another program"
    monkeypatch.setattr(ports, "excluded_ranges", lambda: (ports.ExcludedRange(listener - 1, listener + 1, False),))
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
    free = ports.free_port(listener, below=listener + 50)
    assert free.usable and free.port != listener
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, False, "in use by another program"))
    with pytest.raises(ports.NoFreePortError):
        ports.free_port(49000)


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


def test_a_port_windows_holds_points_at_reserve_port(capsys, monkeypatch):
    from latet.cli.main import _cannot_start

    monkeypatch.delenv("LATET_PORT", raising=False)
    _cannot_start(SimpleNamespace(port=5283, reason="reserved by Windows (5241-5340)"))
    held = capsys.readouterr().err
    assert "reserved by Windows (5241-5340)" in held
    assert "latet reserve-port" in held and "latet global-install" not in held

    _cannot_start(SimpleNamespace(port=5283, reason="in use by another program"))
    taken = capsys.readouterr().err
    assert "latet global-install" in taken and "latet reserve-port" not in taken


def test_a_port_windows_holds_that_latet_port_sets_points_at_latet_port(capsys, monkeypatch):
    """`latet reserve-port` never moves a port that LATET_PORT sets: the advice is to choose another one there."""
    from latet.cli.main import _cannot_start

    monkeypatch.setenv("LATET_PORT", "5283")
    _cannot_start(SimpleNamespace(port=5283, reason="reserved by Windows (5241-5340)"))
    held = capsys.readouterr().err
    assert "reserved by Windows (5241-5340)" in held
    assert "LATET_PORT" in held
    assert "latet reserve-port" not in held and "latet global-install" not in held


def test_global_install_moves_past_windows_block(monkeypatch):
    from latet.cli import admin

    monkeypatch.setattr(ports, "excluded_ranges", lambda: (ports.ExcludedRange(5241, 5340, False),))
    probed: list[int] = []

    def probe(port):
        probed.append(port)
        held = 5241 <= port <= 5340
        return ports.PortState(port, not held, "reserved by Windows (5241-5340)" if held else "free")

    monkeypatch.setattr(ports, "probe", probe)
    new, moved = admin._install_port(5283, None)
    assert new == 5341 and moved is not None and "5283 is reserved by Windows (5241-5340)" in moved
    assert [port for port in probed if 5284 <= port <= 5340] == []


def test_global_install_with_no_free_port_says_what_is_true_now(monkeypatch, capsys):
    import typer

    from latet.cli import admin

    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, False, "reserved by Windows (5241-5340)"))

    def none_free(preferred, ours=()):
        raise ports.NoFreePortError("none")

    monkeypatch.setattr(ports, "choose", none_free)
    with pytest.raises(typer.Exit):
        admin._install_port(5283, None)
    said = capsys.readouterr()
    assert "5335" not in said.err and "49151" in said.err
    assert "latet reserve-port" in said.out + said.err


@pytest.mark.parametrize(("finding", "named"), [("at-risk", True), ("reserved", False), ("safe", False)])
def test_global_install_says_the_port_is_at_risk(tmp_path, monkeypatch, finding, named):
    import latet.cli.admin  # noqa: F401 - registers global-install
    from latet.cli.main import app

    for name, sub in (("LATET_DATA_DIR", "data"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex"),
                      ("XDG_CONFIG_HOME", "xdg")):  # fmt: skip
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("LATET_PORT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)  # no agent is installed
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=port or 5283))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    monkeypatch.setattr(ports, "reservation", lambda port: finding)
    result = CliRunner().invoke(app, ["global-install", "--agents", "none", "--no-ner"])  # shown, nothing changed
    assert result.exit_code == 2, result.output
    lines = [line for line in result.output.splitlines() if "latet reserve-port" in line]
    assert len(lines) == (1 if named else 0), result.output


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
