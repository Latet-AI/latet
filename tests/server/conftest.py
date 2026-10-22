"""What the tests of a Latet server's real backends share: PostgreSQL and Valkey on this computer
(`uv run python scripts/server-services.py up`, then the two variables `env` prints), a database schema and a Valkey key
prefix of each test's own, so tests can run side by side."""

import asyncio
import contextlib
import functools
import os
import re
import secrets
import socket
from urllib.parse import urlsplit

import psycopg
import pytest
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from latet.server import db as db_module
from latet.server.bus import Bus, redis_url
from latet.server.db import Database, migrate
from latet.server.master import MasterKey
from latet.server.settings import ServerSettings

from ..unit.state.contracts import Clock
from .support import DATABASE, URL, VALKEY, close_test_server, open_test_server, role_catalog

# On Windows psycopg waits for ever for a port nothing listens on, unless libpq is given a time limit: this one bounds
# every connection of this process, the pools' of the later tasks too.
os.environ.setdefault("PGCONNECT_TIMEOUT", "5")

_HERE = {"127.0.0.1", "::1"}
_UP = "`uv run python scripts/server-services.py up`"
_A_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*|[0-9A-Fa-f:.]+")  # a name or an address, nothing else


def _shown(host: str) -> str:
    """A host as a skip's reason may name it: as it is when it looks like a name or an address; otherwise not, since it
    can be the tail of a password libpq read as the host (a raw @ in it ends the user's part at the first @)."""
    return host if _A_HOST.fullmatch(host) else "a host that cannot be read (an @ in a password is written %40)"


def _elsewhere(variable: str, host: str) -> str:
    return (
        f"{variable} names {host}: the server's tests reach 127.0.0.1 or ::1 only, as numbers (a name such as "
        "localhost is tried as ::1 first, and the services listen on IPv4)"
    )


def _refusal(database: str, valkey: str) -> str:
    """Why the server's tests are not run with these two addresses, or an empty text when both are set and name this
    computer alone. pytest-socket watches Python's sockets and not libpq's, so PostgreSQL's hosts are read here, every
    one: the URL's, those after a comma, and the ``host`` and ``hostaddr`` of its query, which replace or add to them."""
    unset = [
        name for name, value in (("LATET_TEST_DATABASE_URL", database), ("LATET_TEST_VALKEY_URL", valkey)) if not value
    ]
    if unset:
        return (
            f"{' and '.join(unset)} not set: {_UP} starts PostgreSQL and Valkey for these tests, and `env` prints the "
            "variables"
        )
    try:
        found = conninfo_to_dict(database)
    except psycopg.Error:
        return "LATET_TEST_DATABASE_URL is not an address PostgreSQL's driver reads"
    if "service" in found:
        return "LATET_TEST_DATABASE_URL reads a service file, which can name another computer"
    # The two variables that Database refuses (even an empty PGSERVICE, which libpq takes for a service of that name):
    # a computer that has one would meet an error in every test, and meets a skip that says why.
    if "PGSERVICE" in os.environ:
        return "PGSERVICE is set: it makes libpq read a service file, which can name another computer"
    if "PGREQUIREAUTH" in os.environ and "require_auth" not in found:
        return (
            "PGREQUIREAUTH is set: it decides which authentication methods PostgreSQL may use, which a server's "
            "connection refuses to take from the computer. Unset it, or write require_auth in LATET_TEST_DATABASE_URL"
        )
    named = (found.get("host"), found.get("hostaddr") or os.environ.get("PGHOSTADDR"))
    hosts = [host for part in named if part for host in part.split(",")]
    if not hosts:
        return _elsewhere("LATET_TEST_DATABASE_URL", "no host")
    for host in hosts:
        if host not in _HERE:
            return _elsewhere("LATET_TEST_DATABASE_URL", _shown(host) if host else "the default host")
    try:
        address = urlsplit(valkey)
        host, _ = address.hostname, address.port
    except ValueError:
        return "LATET_TEST_VALKEY_URL is not an address Valkey's client reads"
    if host not in _HERE:
        return _elsewhere("LATET_TEST_VALKEY_URL", _shown(host) if host else "no host")
    return ""


def _hostport(host: str, port: object) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _places(database: str, valkey: str) -> tuple[tuple[str, int], tuple[str, int]]:
    """Where each service listens, as these two addresses say: PostgreSQL's first place, and Valkey's."""
    found = conninfo_to_dict(database)
    host = str(found.get("host") or found.get("hostaddr") or "").split(",")[0]
    port = int(str(found.get("port") or 5432).split(",")[0])
    address = urlsplit(valkey)
    return (host, port), (address.hostname or "", address.port or 6379)


def _answers(host: str, port: int) -> str:
    """An empty text when something takes a TCP connection at ``host`` and ``port`` within a few seconds; otherwise the
    type of what went wrong."""
    try:
        with socket.create_connection((host, port), timeout=3):
            return ""
    except OSError as exc:
        return type(exc).__name__


def _silent(database: str, valkey: str) -> str:
    """The services among these two addresses that nothing answers for on TCP, each named with its address, and the
    command that starts them; an empty text when both answer. A service that answers and refuses the tests' connection
    is not silent (``_refused``): skipping would hide a password that is not the services'. PostgreSQL's address may name
    several hosts: it is silent only when none of them answers."""
    found = conninfo_to_dict(database)
    hosts = str(found.get("hostaddr") or found.get("host") or "").split(",")
    ports = str(found.get("port") or 5432).split(",")
    places = [(host, int(ports[i] if len(ports) > 1 else ports[0])) for i, host in enumerate(hosts)]
    _, (valkey_host, valkey_port) = _places(database, valkey)
    silent = []
    kinds = [_answers(host, port) for host, port in places]
    if all(kinds):
        where = ", ".join(_hostport(host, port) for host, port in places)
        silent.append(f"PostgreSQL does not answer at {where} ({kinds[0]})")
    if kind := _answers(valkey_host, valkey_port):
        silent.append(f"Valkey does not answer at {_hostport(valkey_host, valkey_port)} ({kind})")
    return f"{'; '.join(silent)}: {_UP} starts PostgreSQL and Valkey for these tests" if silent else ""


def _refused(database: str, valkey: str) -> str:
    """The services among these two addresses that answer and refuse the tests' connection (a password that is not
    theirs, a service still starting), each named with its address and the type of the driver's error, never its words;
    an empty text when both take it. Each is asked once."""
    (host, port), (valkey_host, valkey_port) = _places(database, valkey)
    refused = []
    try:
        with psycopg.connect(database) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        refused.append(
            f"PostgreSQL answers at {_hostport(host, port)} and refuses the tests' connection ({type(exc).__name__})"
        )
    try:
        with _valkey_client(valkey) as client:
            client.ping()
    except (redis.RedisError, OSError, ValueError) as exc:
        where = _hostport(valkey_host, valkey_port)
        refused.append(f"Valkey answers at {where} and refuses the tests' connection ({type(exc).__name__})")
    if not refused:
        return ""
    return (
        f"{'; '.join(refused)}: LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL are the ones "
        "`uv run python scripts/server-services.py env` prints"
    )


def unmet(reason: str) -> None:
    """A server's test that cannot run here, for ``reason``: skipped, saying why; failed where the services are required
    (CI's job sets LATET_TEST_REQUIRE_SERVICES=1), so that a job is never green with the server's tests not run."""
    if os.environ.get("LATET_TEST_REQUIRE_SERVICES") == "1":
        pytest.fail(f"the server's tests must run here (LATET_TEST_REQUIRE_SERVICES): {reason}", pytrace=False)
    pytest.skip(reason)


def required(report: pytest.TestReport) -> None:
    """Where the services are required (LATET_TEST_REQUIRE_SERVICES=1, CI's job), a server's test skipped by any other
    way than ``unmet`` (its own ``pytest.skip``, a skip mark) is failed, saying why, so that no skip leaves the job green
    with a test not run; an expected failure (xfail) stays what it is."""
    if not report.skipped or os.environ.get("LATET_TEST_REQUIRE_SERVICES") != "1" or hasattr(report, "wasxfail"):
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    report.outcome = "failed"
    report.longrepr = f"a server's test skipped where the services are required (LATET_TEST_REQUIRE_SERVICES): {reason}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Every report of a server's test, through ``required``."""
    report = yield
    required(report)
    return report


@functools.cache
def _unavailable() -> tuple[str, str]:
    """Why the server's tests cannot run here: why they are skipped (an address elsewhere, a service nothing answers
    for), and why they fail (a service that answers and refuses them); each an empty text when it does not apply. The
    two services are asked once for the whole session: the first test waits for one that does not answer, the others
    are told at once."""
    skipped = _refusal(DATABASE, VALKEY) or _silent(DATABASE, VALKEY)
    return (skipped, "") if skipped else ("", _refused(DATABASE, VALKEY))


@pytest.fixture(autouse=True)
def _services():
    """Without PostgreSQL and Valkey on this computer, answering, the server's tests are skipped, saying why (failed,
    where the services are required: ``unmet``); with one that answers and refuses them, they fail, saying why."""
    skipped, failed = _unavailable()
    if skipped:
        unmet(skipped)
    if failed:
        pytest.fail(failed, pytrace=False)


def _valkey_client(url: str = ""):
    return redis.Redis.from_url(redis_url(url or VALKEY), socket_connect_timeout=5, socket_timeout=5)


@pytest.fixture
def schema():
    """A schema of this test's own, dropped after it."""
    name = "latet_t_" + secrets.token_hex(5)
    yield name
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def prefix():
    """A Valkey prefix of this test's own, its keys removed after it."""
    value = "latet_t_" + secrets.token_hex(5) + ":"
    yield value
    client = _valkey_client()
    keys = list(client.scan_iter(match=value + "*", count=1000))
    if keys:
        client.delete(*keys)
    client.close()


@pytest.fixture
def settings(tmp_path):
    key = tmp_path / "master.key"
    key.write_text(MasterKey.generate(), "utf-8")
    return ServerSettings(url=URL, database_url=DATABASE, valkey_url=VALKEY, master_key_file=key)


@pytest.fixture
def server(schema, prefix):
    """An instance of a Latet server, in the test's own schema and prefix."""
    found = open_test_server(schema, prefix)
    yield found
    close_test_server(found)


@pytest.fixture
def instances(schema, prefix):
    """Two instances of one server: one schema and one prefix, each instance with its own memory."""
    found = (open_test_server(schema, prefix), open_test_server(schema, prefix))
    yield found
    for one in found:
        close_test_server(one)


@pytest.fixture
def db(settings, schema):
    """The test's schema, migrated."""
    database = Database(settings.database_url, schema=schema, max_size=4)
    migrate(database)
    yield database
    database.close()


@pytest.fixture
def role_url():
    """Makes a login role of the test's own whose sessions default to the settings it is given (``ALTER ROLE ... SET``,
    what a role or a database may set of a session) and returns the address that reaches PostgreSQL as that role:
    ``role_url(default_transaction_isolation="repeatable read")``. The roles, with what they made, are dropped after the
    test, which closes its connections before."""
    made: list[str] = []

    def make(**defaults: str) -> str:
        role, password = "latet_r_" + secrets.token_hex(4), "r0le-" + secrets.token_hex(4)
        found = conninfo_to_dict(DATABASE)
        with role_catalog() as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
            )
            admin.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(found["dbname"]), sql.Identifier(role)
                )
            )
            for name, value in defaults.items():
                admin.execute(
                    sql.SQL("ALTER ROLE {} SET {} = {}").format(
                        sql.Identifier(role), sql.Identifier(name), sql.Literal(value)
                    )
                )
        made.append(role)
        return f"postgresql://{role}:{password}@{found['host']}:{found['port']}/{found['dbname']}"

    yield make
    with role_catalog() as admin:
        for role in made:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


# The start-up options that pin the sessions' defaults, as libpq reads them (a backslash keeps a space inside a value).
PINS = {
    "extra_float_digits": "-c extra_float_digits=3",
    "default_transaction_isolation": "-c default_transaction_isolation=read" + chr(0x5C) + " committed",
    "client_encoding": "-c client_encoding=UTF8",
    "statement_timeout": "-c statement_timeout=60000",
}


def lose_pins(monkeypatch, schema: str, *lost: str) -> None:
    """The connections made from now on start with the search path and with the pins but those named in ``lost``: as
    through a pooler that drops some of the start-up options."""
    options = " ".join([f"-c search_path={schema}", *(option for name, option in PINS.items() if name not in lost)])
    real = db_module._parameters
    monkeypatch.setattr(db_module, "_parameters", lambda *args: {**real(*args), "options": options})


@pytest.fixture
def clock():
    """The clock a test gives the parts of a server that read the time, and moves."""
    return Clock()


BUS_KEY = b"\x07" * 32  # the bus's key: on a server derived from the master key (open_server)
CREDENTIALS_KEY = b"\x0c" * 32  # the credentials' key: on a server derived from the master key as well (open_server)
AUDIT_KEY = b"\x0e" * 32  # the administration log's key: derived from the master key too (open_server)


@pytest.fixture
def bus(settings, prefix):
    found = Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY)
    yield found
    found.close()


@pytest.fixture
def buses(settings, prefix):
    """Two instances' connections to one Valkey, with the server's one key."""
    found = (Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY), Bus(settings.valkey_url, prefix=prefix, key=BUS_KEY))
    yield found
    for one in found:
        one.close()


@contextlib.asynccontextmanager
async def deliver(bus, channel, *hearers):
    """While it lasts, each message of ``channel`` is handed to the hearers' ``heard``: an instance's ear, in small."""
    ready = asyncio.Event()

    async def ear():
        async with contextlib.aclosing(bus.listen(channel, ready=ready)) as messages:
            async for _, message in messages:
                for hearer in hearers:
                    hearer.heard(message)

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def drop_listeners(bus) -> int:
    """Valkey closes the connections this test's listeners hear on, as a restart of Valkey would: how many."""
    found = [c["id"] for c in bus.client.client_list() if c.get("name") == f"{bus.prefix}listen"]
    for client_id in found:
        bus.client.client_kill_filter(_id=client_id)
    return len(found)


def identity_in(db, identity_id="u_emilycarte", name="Emily Carter", kind="person"):
    """An identity's row, for the tests of what it keeps (Identities makes them otherwise)."""
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO identities (id, kind, name, created) VALUES (%s, %s, %s, 0) ON CONFLICT DO NOTHING",
            (identity_id, kind, name),
        )
    return identity_id


def dump(db) -> bytes:
    """Every value of every table of the test's schema, as bytes: what someone reading the database sees."""
    out = bytearray()
    with db.connection() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (db.schema,)
        )]  # fmt: skip
        for table in tables:
            for row in conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))):
                for value in row:
                    out += value if isinstance(value, bytes | bytearray | memoryview) else str(value).encode()
                    out += b"\n"
    return bytes(out)


def valkey_dump(bus, prefix) -> bytes:
    """Every key of the test's prefix and what it holds, as bytes: what someone reading Valkey sees."""
    out = bytearray()
    for key in bus.client.scan_iter(match=prefix + "*", count=1000):
        out += key + b"\n"
        kind = bus.client.type(key)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if kind == "string":
            values = [bus.client.get(key)]
        elif kind == "hash":
            values = [part for pair in bus.client.hgetall(key).items() for part in pair]
        elif kind == "set":
            values = list(bus.client.smembers(key))
        elif kind == "zset":
            values = list(bus.client.zrange(key, 0, -1))
        else:
            values = []
        for value in values:
            out += (value or b"") + b"\n"
    return bytes(out)
