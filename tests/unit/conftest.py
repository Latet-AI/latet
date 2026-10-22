"""Fixtures of the unit tests that meet a Latet server as a computer does (tests/unit/fake_latet_server.py)."""

import os
from pathlib import Path
from types import SimpleNamespace

import keyring.core
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from latet.cli.agent import MARKERS, PREFIXES
from latet.config import IdentityConfig

from .fake_latet_server import FakeLatetServer

LAPTOP = IdentityConfig(
    usernames=["emily"], hostname="emily-laptop", full_name="Emily Carter", emails=["emily.carter@acme.com"]
)
_AGENT_VARIABLES = {marker for markers in MARKERS.values() for marker in markers} | {"AGENT"}


@pytest.fixture(autouse=True)
def isolated_machine_defaults(isolated_homes):
    """Machine discovery must never inspect a real installed service from a unit test."""
    from latet.server import service

    root = Path.home() / "machine"
    # Keep each test's shared monkeypatch teardown order independent of this autouse isolation.
    with pytest.MonkeyPatch.context() as isolation:
        isolation.setattr(service, "windows_folder", lambda: root / "windows" / "Latet")
        isolation.setattr(service, "LINUX_CONFIG", root / "linux" / "server.yaml")
        yield


class MemoryKeyring(KeyringBackend):
    """A keyring in memory: what a test keeps there is the test's alone, and the system's is never asked."""

    priority = -10  # never the one keyring would pick by itself

    def __init__(self) -> None:
        super().__init__()
        self.kept: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.kept.get((service, username))

    def set_password(self, service, username, password):
        self.kept[(service, username)] = password

    def delete_password(self, service, username):
        if self.kept.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture
def computer(tmp_path, monkeypatch):
    """This computer's Latet in folders of its own: its keyring off (LATET_KEYRING) and, for what asks it all the same,
    one in memory; no connection, no server named by a variable. Its home and its agents' settings are the test's too
    (`latet disconnect` puts the agents back): an agent whose own variable a fixture removes (`quiet` removes every
    CODEX_*) finds its settings in the test's home, never in yours."""
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.setenv("LATET_KEYRING", "off")
    monkeypatch.setattr(keyring.core, "_keyring_backend", MemoryKeyring())
    for name in ("LATET_CONNECTION", "LATET_SERVER_URL", "LATET_SERVER_FINGERPRINT", "LATET_SERVER_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name, folder in (("CLAUDE_CONFIG_DIR", ".claude"), ("CODEX_HOME", ".codex"), ("XDG_CONFIG_HOME", ".config")):
        monkeypatch.setenv(name, str(tmp_path / "home" / folder))
    return tmp_path


@pytest.fixture
def keyring_on(computer, monkeypatch):
    """The computer's keyring on: the one in memory, which a test may replace (``keyring.core._keyring_backend``)."""
    monkeypatch.setenv("LATET_KEYRING", "on")
    return keyring.core.get_keyring()


@pytest.fixture
def latet_server(computer):
    """A Latet server as a computer meets it, over HTTPS on 127.0.0.1 with a self-signed certificate."""
    server = FakeLatetServer(computer / "tls").start()
    yield server
    server.stop()


@pytest.fixture
def quiet(monkeypatch):
    """No agent of this computer applied again, no daemon running here, and this computer's identity known; and no
    agent around (the one these tests may run in): `latet connect` and `latet disconnect` refuse to run in one."""
    monkeypatch.setattr("latet.cli.admin.refresh_applied", lambda skip=None: [])
    monkeypatch.setattr("latet.config.detect_identity", lambda: LAPTOP)
    monkeypatch.setattr("latet.service.daemon.status", lambda port=None: SimpleNamespace(running=False))
    for name in list(os.environ):
        if name in _AGENT_VARIABLES or name.startswith(tuple(PREFIXES)):
            monkeypatch.delenv(name)


def run_cli(*args, input=None):
    from typer.testing import CliRunner

    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def connect_to(server):
    """`latet connect` to a FakeLatetServer, as a script of yours runs it where no terminal is there: with its
    fingerprint, its code on standard input (never on the command line), and --yes."""
    return run_cli(
        "connect", server.url, "--fingerprint", server.fingerprint, "--code", "-", "--yes", input=f"{server.code}\n"
    )
