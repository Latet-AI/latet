import socket

import pytest
from pydantic import BaseModel, ConfigDict

from latet.ee import license as license_module
from latet.ee import settings as settings_module

from .licenses import Clock, Signer, at
from .servers import MASTER_KEY, Instances


@pytest.fixture(scope="session")
def signer() -> Signer:
    return Signer()


@pytest.fixture
def licensed(signer, monkeypatch):
    """Latet carries the tests' public key, as it carries the licensor's: a Licensing made without keys of its own
    (the edition's, the server's) checks with it."""
    monkeypatch.setitem(license_module.PUBLIC_KEYS, signer.key_id, signer.public)
    return signer


@pytest.fixture
def offline(monkeypatch):
    """Every way out to the network fails the test: a socket made, a connection, a name looked up."""

    def refuse(*args, **kwargs):
        raise AssertionError("a license check reached for the network")

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse)


class Demo(BaseModel):
    """A section as a later part adds one (E3's sign-in, E4's SIEM outputs): E1 has none, so the tests add this."""

    model_config = ConfigDict(extra="forbid")
    level: int = 1
    note: str | None = None


@pytest.fixture
def demo_section(monkeypatch):
    """The Enterprise settings with a section `demo`, as if a later part had added it to EnterpriseSettings. A plain
    default (not a factory), so that a section at its defaults is left out of what is written."""

    class WithDemo(settings_module.EnterpriseSettings):
        demo: Demo = Demo()

    monkeypatch.setattr(settings_module, "EnterpriseSettings", WithDemo)
    return WithDemo


@pytest.fixture
def clock():
    """The servers' clock in a test: a day of the license's, which the test moves."""
    return Clock(at("2027-03-01"))


@pytest.fixture
def instances(clock, monkeypatch):
    """A server on the test services: `instances.open()` starts one more instance of it."""
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    opened = Instances(clock)
    yield opened
    opened.close()


@pytest.fixture
def server(instances):
    """One instance of a server."""
    return instances.open()
