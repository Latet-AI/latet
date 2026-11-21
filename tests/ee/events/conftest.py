"""What the tests of E4 share: no proxy between them and their receivers on 127.0.0.1, a fake model, and, for the tests
of the server's backends (marker `server`), PostgreSQL and Valkey as E0b's tests have them: a schema and a Valkey prefix
of the test's own (E0b's fixtures `schema` and `prefix`), skipped, saying why, without the services (failed instead
where the services are required, LATET_TEST_REQUIRE_SERVICES=1, as E0b's own tests are); E3's clock and its cluster of
two instances of one licensed server, whose events E4 now keeps; and no name looked up beyond this computer."""

import os
import socket

import pytest

from ...e2e.fake_upstream import FakeUpstream
from ...server import conftest as server_conftest
from ..people.server_support import Clock, start, stop

_LOCAL = frozenset({"127.0.0.1", "::1", "localhost"})

# E0b's fixtures (tests/server/conftest.py): a PostgreSQL schema and a Valkey key prefix of the test's own, removed
# after it. Bound here, so that the tests of this folder have them too.
schema = server_conftest.schema
prefix = server_conftest.prefix


def needs(name: str) -> str:
    """A variable a test of the real backends needs: skipped, saying why, when it is not set (failed where the services
    are required: E0b's ``unmet``)."""
    value = os.environ.get(name)
    if not value:
        server_conftest.unmet(
            f"{name} is not set: `uv run python scripts/server-services.py up` starts PostgreSQL and Valkey"
        )
    return value or ""


@pytest.fixture(autouse=True)
def _services(request):
    """A test of the server's real backends (marker `server`) is skipped, saying why, without PostgreSQL and Valkey,
    before the schema and the prefix it would make (as E0b's own tests are); and, as E0b's are, never run against
    services that are not on this computer, or that answer and refuse it."""
    if request.node.get_closest_marker("server") is not None:
        needs("LATET_TEST_DATABASE_URL")
        needs("LATET_TEST_VALKEY_URL")
        skipped, failed = server_conftest._unavailable()
        if skipped:
            server_conftest.unmet(skipped)
        if failed:
            pytest.fail(failed, pytrace=False)


@pytest.fixture(autouse=True)
def _no_lookups(monkeypatch):
    """No name is looked up beyond this computer (the tests reach no network beyond 127.0.0.1): an instance's SIEM
    delivery runs in the background and connects to the outputs a test sets (``siem0.acme.com``), whose names would
    otherwise go to the network's resolver. Such a name does not resolve here: its output fails, and is tried again."""
    real = socket.getaddrinfo

    def local(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in _LOCAL:
            raise socket.gaierror("no name is looked up beyond this computer in the tests")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def database(schema):
    """The test's schema, migrated as `latet server migrate` migrates it (E0b's and E1's packages, E4's tables among
    E1's); E0b's `schema` drops it after the test."""
    from latet.server.db import Database, migrate

    from ..servers import master

    db = Database(needs("LATET_TEST_DATABASE_URL"), schema=schema)
    migrate(db, master=master())  # Latet 0.11's launch registry is sealed with the master key
    yield db
    db.close()


@pytest.fixture
def bus(prefix):
    """Valkey, under the test's own prefix (E0b's `prefix`, whose keys go after the test): its locks are no other
    test's. The bus's key is any 32 bytes here (a server derives its own from its master key)."""
    from latet.server.bus import Bus

    found = Bus(needs("LATET_TEST_VALKEY_URL"), prefix=prefix, key=bytes([7]) * 32)
    yield found
    found.close()


@pytest.fixture
def clock():
    """E3's clock of the tests (2027-03-01, within the tests' license), which these tests also move by seconds."""
    return Clock()


@pytest.fixture
def cluster(instances, licensed, clock):
    """E3's two instances of one server, a license of every feature installed (E1's `instances` and `licensed`)."""
    started = start(instances, licensed, clock)
    yield started
    stop(started)
