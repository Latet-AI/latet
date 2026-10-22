"""A Latet server's database layer where no PostgreSQL is needed: the connection is made of its address alone, an error
of the driver is shown and logged by its type and never by its words, and a database that does not answer is given up
after the time the server was set to wait. The tests that need PostgreSQL are in tests/server/test_db_start.py."""

import logging
import os
import random
import socket
import time
import traceback
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from latet.server import db as db_module
from latet.server.db import _NO_FILE, CONNECT_TIMEOUT, Database, DatabaseError, _parameters, check_schema, migrate
from latet.server.settings import ServerSettings

from ...server.libpq_environment import PASSWORD_IN_THE_FILE, hostile, options

PASSWORD = "s3cret-pw"
HERE = "127.0.0.1"
SCHEMA = "latet_t_unit"


def _closed_port() -> int:
    with socket.socket() as one:
        one.bind((HERE, 0))
        return one.getsockname()[1]


@contextmanager
def _silent_peer():
    """A listener that takes connections into its queue and never says a word: a PostgreSQL that does not answer."""
    with socket.socket() as listener:
        listener.bind((HERE, 0))
        listener.listen(16)
        yield listener.getsockname()[1]


def _shown(error: BaseException) -> str:
    """All an error shows of itself: its words, its repr, and its chain as a traceback prints it (no source lines)."""
    return f"{error}\n{error!r}\n{''.join(traceback.format_exception(error, limit=0))}"


def _raised(call) -> Exception:
    try:
        made = call()
    except Exception as error:
        return error
    if isinstance(made, Database):
        made.close()  # the refusal did not come: the pool that was made is not left to run on while the test fails
    pytest.fail("nothing was raised")


def _until(check, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def _libpq_resolves(parameters: dict[str, str]) -> dict[str, str]:
    """What libpq makes of these parameters, with no server: it reads them and the environment, a password file and a
    service file for what they leave out, then starts to connect, and the connection is dropped at once."""
    conn = pq.PGconn.connect_start(make_conninfo("", **parameters).encode())
    try:
        return {name: getattr(conn, name).decode() for name in ("host", "hostaddr", "port", "user", "db", "password")}
    finally:
        conn.finish()


def _libpq_holds(parameters: dict[str, str]) -> dict[str, str | None]:
    """Every option as libpq holds it for these parameters, and the password it found (``password``, a key of its own:
    libpq looks in a password file for one when it has none), with no server: it reads the parameters and the
    environment, a password file and a service file for what they leave out, then starts to connect, and the connection
    is dropped at once."""
    conn = pq.PGconn.connect_start(make_conninfo("", **parameters).encode())
    try:
        held = {o.keyword.decode(): (o.val.decode() if o.val is not None else None) for o in conn.info}
        return held | {"found password": conn.password.decode()}
    finally:
        conn.finish()


def _libpq_bad(parameters: dict[str, str]) -> str | None:
    """What libpq says is wrong with these parameters before it connects anywhere (a closed port, no server), or None."""
    conn = pq.PGconn.connect_start(make_conninfo("", **parameters).encode())
    try:
        return conn.error_message.decode() if conn.status == pq.ConnStatus.BAD else None
    finally:
        conn.finish()


@pytest.fixture(autouse=True)
def _no_refused_variable(monkeypatch):
    """A PGSERVICE or a PGREQUIREAUTH of this computer is refused before the address is read, in words of their own:
    the tests that mean it set them."""
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.delenv("PGREQUIREAUTH", raising=False)


@pytest.fixture
def worked_with_psql(monkeypatch, tmp_path):
    """The environment of someone who works with psql: whatever an address leaves out, libpq finds in it, in a password
    file or in a service file. Every host is this computer and every port is closed, so that nothing is reached."""
    port = _closed_port()
    passfile, services = tmp_path / "pgpass", tmp_path / "pg_service.conf"
    passfile.write_bytes(b"*:*:*:*:pw-from-the-password-file\n")
    passfile.chmod(0o600)  # libpq ignores a password file others can read
    services.write_bytes(f"[prod]\nhost={HERE}\nport={port}\nuser=service-user\ndbname=service-db\n".encode())
    for name, value in {
        "PGHOST": HERE, "PGHOSTADDR": "127.0.0.2", "PGPORT": str(port), "PGUSER": "env-user", "PGPASSWORD": "env-pw",
        "PGDATABASE": "env-db", "PGPASSFILE": str(passfile), "PGSERVICEFILE": str(services),
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    return port


def test_libpq_takes_nothing_the_address_leaves_out_from_the_environment(worked_with_psql):
    port = worked_with_psql
    # No user, no password: not PGUSER, PGPASSWORD, a password file or a service file; the user is the system's.
    seen = _libpq_resolves(_parameters(f"postgresql://{HERE}:{port}/latet", SCHEMA, 5))
    assert (seen["host"], seen["port"], seen["db"]) == (HERE, str(port), "latet")
    assert seen["user"] not in ("env-user", "service-user") and seen["password"] == ""
    # No port, no database: not PGPORT, not PGDATABASE (the database is the user's name, as libpq has it).
    seen = _libpq_resolves(_parameters(f"postgresql://{HERE}/", SCHEMA, 5))
    assert seen["port"] == "5432" and seen["db"] == seen["user"] != "env-db"
    # A host by its name: PGHOSTADDR does not say where it is.
    seen = _libpq_resolves(_parameters(f"postgresql://localhost:{port}/latet", SCHEMA, 5))
    assert seen["host"] == "localhost" and seen["hostaddr"] != "127.0.0.2"
    # What the address says is what is used, whatever else the environment holds.
    seen = _libpq_resolves(_parameters(f"postgresql://u:{PASSWORD}@{HERE}:{port}/d", SCHEMA, 5))
    assert (seen["user"], seen["password"], seen["db"]) == ("u", PASSWORD, "d")


@pytest.mark.parametrize(
    "address",
    [
        f"postgresql://latet:{PASSWORD}@/latet",
        f"postgresql:///latet?user=latet&password={PASSWORD}",
        f"postgresql://latet:{PASSWORD}@{HERE},/latet",  # the first host is empty, and an empty one is the default
        f"postgresql://latet:{PASSWORD}@/latet?hostaddr=",
        f"dbname=latet password={PASSWORD}",
        "",
    ],
)
def test_an_address_without_a_host_is_refused_though_the_environment_has_one(worked_with_psql, address):
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "host" in str(refused) and "PGHOST" in str(refused)
    assert PASSWORD not in _shown(refused)


def test_a_socket_or_a_host_given_by_its_address_alone_is_an_address_with_a_host(worked_with_psql):
    assert _parameters("postgresql:///latet?host=/var/run/postgresql", SCHEMA, 5)["host"] == "/var/run/postgresql"
    only_hostaddr = _parameters(f"postgresql://u@/latet?hostaddr={HERE}", SCHEMA, 5)
    assert (only_hostaddr["hostaddr"], only_hostaddr["host"]) == (HERE, "")  # and a host of no variable's
    assert (
        _parameters("postgresql://u@db1.acme.lan,db2.acme.lan/latet", SCHEMA, 5)["host"] == "db1.acme.lan,db2.acme.lan"
    )


def test_a_service_file_is_never_read(worked_with_psql, monkeypatch):
    port = worked_with_psql
    address = f"postgresql://latet:{PASSWORD}@{HERE}:{port}/latet"
    assert _parameters(address, SCHEMA, 5)  # PGSERVICEFILE alone reads nothing: no service is named
    refused = _raised(lambda: Database(address + "?service=prod", schema=SCHEMA))
    assert type(refused) is ValueError and "service" in str(refused) and PASSWORD not in _shown(refused)
    monkeypatch.setenv("PGSERVICE", "prod")
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "PGSERVICE" in str(refused) and PASSWORD not in _shown(refused)


def _options(schema: str) -> str:
    """The start-up options of every connection: the search path, and the settings the server rests on, whatever a role
    or a database defaults to (a backslash keeps a space inside the value of an option)."""
    return (
        f"-c search_path={schema} -c extra_float_digits=3 -c default_transaction_isolation=read{chr(0x5C)} committed"
        " -c client_encoding=UTF8 -c statement_timeout=60000"
    )


@pytest.mark.parametrize("name", ["latet", "latet_t_0123456789", "a", "a" * 63, "s_2"])
def test_a_schema_named_like_this_is_the_connections_search_path(name):
    assert _parameters(f"postgresql://u@{HERE}/d", name, 5)["options"] == _options(name)


@pytest.mark.parametrize(
    "name",
    [
        "", "Latet", "latet x", "latet,public", "latet;drop schema public", "latet-x", "latet\n", "9latet", "_latet",
        "a" * 64, "latet.x", 'latet"x', "public", "information_schema", "pg_catalog", "pg_temp", "pg_latet",
        "latet\N{LATIN SMALL LETTER E WITH ACUTE}",
    ],
)  # fmt: skip
def test_a_schema_is_a_name_of_its_own_and_nothing_that_adds_to_the_search_path(name):
    refused = _raised(lambda: Database(f"postgresql://u:{PASSWORD}@{HERE}/d", schema=name))
    assert type(refused) is ValueError and "schema" in str(refused)


@pytest.mark.parametrize("seconds", [-1, 0, 1, 121, 3600])
def test_a_connect_timeout_is_two_to_a_hundred_and_twenty_seconds(seconds):
    refused = _raised(lambda: Database(f"postgresql://u:{PASSWORD}@{HERE}/d", schema=SCHEMA, connect_timeout=seconds))
    assert type(refused) is ValueError and "connect_timeout" in str(refused)


def test_the_connect_timeout_of_the_database_is_the_one_of_the_settings():
    default = ServerSettings.model_fields["database_connect_timeout"].default
    assert default == CONNECT_TIMEOUT
    assert _parameters(f"postgresql://u@{HERE}/d", SCHEMA, CONNECT_TIMEOUT)["connect_timeout"] == str(default)


@pytest.mark.parametrize(
    "address",
    [
        f"postgresql://latet:{PASSWORD}%zzpw@{HERE}:5/latet",  # libpq refuses it, and says it with the password
        f"postgresql://latet:{PASSWORD}%dbpw@{HERE}:5/latet",  # an escape that is no text: psycopg holds the bytes
        f"postgresql://latet:{PASSWORD}{chr(0xD800)}pw@{HERE}:5/latet",  # a lone surrogate: it holds the address
    ],
)
def test_an_address_the_driver_cannot_read_is_refused_without_a_word_of_it(address):
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "database address" in str(refused)
    assert PASSWORD not in _shown(refused)
    assert refused.__cause__ is None and refused.__context__ is None  # nothing to follow back to the address


@pytest.mark.parametrize(
    ("said", "kind"),
    [
        # libpq's words name the host, and a driver's error can hold the password in its words and in its arguments.
        (lambda: psycopg.OperationalError(f"at {HERE}: {PASSWORD} was quoted-by-libpq", PASSWORD), "OperationalError"),
        # An error that is not the driver's, from the code it calls: its repr holds what it was given.
        (lambda: UnicodeDecodeError("utf-8", PASSWORD.encode(), 0, 1, "quoted-by-libpq"), "UnicodeDecodeError"),
    ],
)
def test_what_the_driver_says_of_a_failed_connection_is_neither_shown_nor_logged(monkeypatch, caplog, said, kind):
    """The test makes the driver say it so, and looks at the error and at every record."""

    def quoting(cls, conninfo="", **kwargs):
        raise said()

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(quoting))
    caplog.set_level(logging.DEBUG)
    db = Database(f"postgresql://latet:{PASSWORD}@{HERE}:5/latet", schema=SCHEMA, connect_timeout=2)
    try:
        error = _raised(lambda: migrate(db))
        assert _until(lambda: any("error connecting" in record.getMessage() for record in caplog.records))
    finally:
        db.close()
    assert type(error) is DatabaseError and kind in str(error)
    assert error.__cause__ is None and error.__context__ is None
    assert PASSWORD not in _shown(error) and "quoted-by-libpq" not in _shown(error)
    for record in caplog.records:
        assert PASSWORD not in repr(record.__dict__) and "quoted-by-libpq" not in record.getMessage()
    assert any(kind in record.getMessage() for record in caplog.records)  # what is said: the type


def test_a_database_that_does_not_answer_is_given_up_after_the_time_it_was_set_to_wait(monkeypatch, caplog):
    """The timeout is the Database's own: psycopg waits 130 seconds for one that does not answer, and libpq's
    environment is not what bounds it here."""
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    caplog.set_level(logging.WARNING, logger="psycopg.pool")
    with _silent_peer() as port:
        db = Database(f"postgresql://latet:{PASSWORD}@{HERE}:{port}/latet", schema=SCHEMA, connect_timeout=2)
        try:
            started = time.monotonic()
            error = _raised(lambda: migrate(db))
            took = time.monotonic() - started
            # The pool's own worker gave up too, and said so (by the type): its connection has the same limit.
            assert _until(lambda: any("error connecting" in record.getMessage() for record in caplog.records))
            gave_up = time.monotonic() - started
        finally:
            db.close()
    assert type(error) is DatabaseError and "ConnectionTimeout" in str(error)
    assert 1.5 < took < 6, took
    assert gave_up < 8, gave_up
    assert any("ConnectionTimeout" in record.getMessage() for record in caplog.records)
    assert PASSWORD not in _shown(error) + repr([record.__dict__ for record in caplog.records])


def test_a_pool_that_was_closed_says_so_by_its_type():
    db = Database(f"postgresql://latet:{PASSWORD}@{HERE}:{_closed_port()}/latet", schema=SCHEMA, min_size=0, max_size=1)
    db.close()
    error = _raised(lambda: check_schema(db))
    assert type(error) is DatabaseError and "PoolClosed" in str(error)
    assert not db.ping()


# The options that fall back to a file in the home folder when they are empty: a path that is not there is their "none".
HOME_FILES = ("passfile", "sslcert", "sslkey", "sslrootcert", "sslcrl")
REFUSED = ("PGSERVICE", "PGREQUIREAUTH")  # the variables that no explicit value sets aside: the address is refused


def _unset(keyword: str, compiled: bytes | None) -> str:
    """What an option is when nothing sets it, in the form libpq takes for it."""
    if keyword in HOME_FILES:
        return _NO_FILE
    if keyword == "sslcertmode":
        return "allow"  # an empty value is an error to libpq
    if keyword in ("min_protocol_version", "max_protocol_version"):
        return "3.0"
    return compiled.decode() if compiled is not None else ""


def test_no_option_that_libpq_has_a_variable_for_is_filled_in_from_the_computer(tmp_path, monkeypatch):
    """Every variable libpq reads for an option, set wrong at once: the two that no explicit value sets aside are refused,
    and with them gone nothing of the rest reaches the connection, as libpq itself holds it."""
    port = _closed_port()
    address = f"postgresql://{HERE}:{port}"  # nothing but where: the rest is left out
    environment = hostile(tmp_path)
    assert set(REFUSED) <= set(environment) and len(environment) >= 30
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    for variable in REFUSED:
        refused = _raised(lambda: _parameters(address, SCHEMA, 5))
        assert type(refused) is ValueError and variable in str(refused)
        monkeypatch.delenv(variable)
    parameters = _parameters(address, SCHEMA, 5)
    given = {keyword for keyword in options() if keyword not in ("service", "require_auth")}
    assert given <= set(parameters), sorted(given - set(parameters))  # each one explicitly, so libpq asks no variable
    held = _libpq_holds(parameters)
    for keyword, option in options().items():
        assert held.get(keyword) != environment[option.envvar.decode()], keyword
    assert held["found password"] == ""  # nor the one in the file PGPASSFILE names
    assert PASSWORD_IN_THE_FILE not in repr(held)
    assert (held["host"], held["port"]) == (HERE, str(port))


def test_what_the_address_leaves_out_is_what_libpq_has_when_nothing_sets_it():
    parameters = _parameters(f"postgresql://u@{HERE}/d", SCHEMA, 5)  # a user, a host and a database; no port
    for keyword, option in options().items():
        if keyword in ("service", "require_auth"):
            assert keyword not in parameters  # not set: any method the server asks, no service file
        elif keyword in ("host", "user", "dbname"):
            assert parameters[keyword] == {"host": HERE, "user": "u", "dbname": "d"}[keyword]
        elif keyword in ("options", "connect_timeout", "client_encoding"):  # the server's own
            expected = {"options": _options(SCHEMA), "connect_timeout": "5", "client_encoding": "UTF8"}
            assert parameters[keyword] == expected[keyword]
        else:
            assert parameters[keyword] == _unset(keyword, option.compiled), keyword
    assert parameters["sslcrldir"] == ""  # libpq has no file in the home folder for it, and a path that is not there
    # makes OpenSSL fail every certificate it checks (seen against a PostgreSQL with TLS)


@pytest.mark.parametrize("keyword", HOME_FILES)
def test_an_empty_name_of_a_file_is_no_file_and_not_the_one_in_the_home_folder(keyword):
    for address in (f"postgresql://u@{HERE}/d?{keyword}=", f"postgresql://u@{HERE}/d"):
        parameters = _parameters(address, SCHEMA, 5)
        assert parameters[keyword] == _NO_FILE and not os.path.exists(_NO_FILE)
        assert _libpq_holds(parameters)[keyword] == _NO_FILE  # libpq looks in no other
    for named in ("/etc/ssl/named.pem", "system"):  # what the address names, it names
        assert (
            _parameters(f"postgresql://u@{HERE}/d?{keyword}={named.replace('/', '%2F')}", SCHEMA, 5)[keyword] == named
        )


def test_sslmode_and_the_root_certificate_are_the_addresss_alone(tmp_path, monkeypatch):
    for variable, value in hostile(tmp_path).items():
        if variable not in REFUSED:
            monkeypatch.setenv(variable, value)
    port = _closed_port()
    held = _libpq_holds(
        _parameters(f"postgresql://u@{HERE}:{port}/d?sslmode=verify-full&sslrootcert=system", SCHEMA, 5)
    )
    assert (held["sslmode"], held["sslrootcert"]) == ("verify-full", "system")
    held = _libpq_holds(_parameters(f"postgresql://u@{HERE}:{port}/d", SCHEMA, 5))
    assert (held["sslmode"], held["sslrootcert"]) == (
        "prefer",
        _NO_FILE,
    )  # neither PGSSLMODE nor a root.crt of the home


@pytest.mark.parametrize("query", ["sslmode=verify-full", "sslmode=verify-ca", "sslmode=verify-full&sslrootcert="])
def test_a_verified_connection_with_no_root_certificate_named_is_refused_in_words_that_say_what_to_write(query):
    """The root certificates of the home folder are not read, so such an address could never connect: it is said at
    once, in words, rather than by the type of the driver's error at the first connection."""
    refused = _raised(lambda: Database(f"postgresql://u:{PASSWORD}@{HERE}/d?{query}", schema=SCHEMA))
    assert type(refused) is ValueError and "sslrootcert=system" in str(refused) and PASSWORD not in _shown(refused)
    for named in ("sslrootcert=system", "sslrootcert=%2Fetc%2Fssl%2Fca.pem"):
        assert _parameters(f"postgresql://u@{HERE}/d?sslmode=verify-full&{named}", SCHEMA, 5)
    for unverified in ("require", "prefer", "disable"):  # nothing to check against: nothing is asked for
        assert _parameters(f"postgresql://u@{HERE}/d?sslmode={unverified}", SCHEMA, 5)["sslrootcert"] == _NO_FILE


def test_an_address_that_names_only_hostaddr_takes_its_host_from_no_variable(tmp_path, monkeypatch):
    for variable, value in hostile(tmp_path).items():
        if variable not in REFUSED:
            monkeypatch.setenv(variable, value)
    port = _closed_port()  # PGHOST is 127.0.0.3 here: a name that libpq would check a certificate against
    parameters = _parameters(f"postgresql://u:{PASSWORD}@/d?hostaddr={HERE}&port={port}", SCHEMA, 5)
    assert parameters["host"] == ""
    held = _libpq_holds(parameters)
    assert (held["host"], held["hostaddr"]) == ("", HERE)


def test_require_auth_is_the_addresss_or_it_is_not_set(monkeypatch):
    address = f"postgresql://u@{HERE}/d"
    assert "require_auth" not in _parameters(address, SCHEMA, 5)  # not set: any method the server asks
    monkeypatch.setenv("PGREQUIREAUTH", "scram-sha-256")
    refused = _raised(lambda: _parameters(address, SCHEMA, 5))
    assert type(refused) is ValueError and "PGREQUIREAUTH" in str(refused) and "require_auth" in str(refused)
    assert (
        _parameters(address + "?require_auth=md5", SCHEMA, 5)["require_auth"] == "md5"
    )  # the variable fills in nothing


@pytest.mark.parametrize(
    "address",
    [
        f"postgresql://latet:{PASSWORD}@a.example{chr(0)}@db.acme.lan/latet",  # libpq ends the address at the zero byte
        f"host=a.example{chr(0)} host=db.acme.lan user=latet password={PASSWORD}",
        f"postgresql://latet:{PASSWORD}@{HERE}/latet?application_name=a{chr(10)}b",  # a line break
        f"postgresql://latet:{PASSWORD}@{HERE}/latet{chr(9)}",  # a tab
        f"postgresql://latet:{PASSWORD}@{HERE}/latet{chr(127)}",  # DEL
    ],
)
def test_an_address_with_a_control_character_is_refused_before_it_is_parsed(address):
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "control character" in str(refused)
    assert PASSWORD not in _shown(refused) and "a.example" not in _shown(refused)


# Fix round 2. libpq writes two other parameters into sslmode, the last one written winning: requiressl (a value that
# starts with 1 is require, any other prefer) and, in a URI, ssl=true (require). A name may be written with escapes in
# a URI.
ALIASED = [
    "sslmode=verify-full&ssl=true",
    "ssl=true&sslmode=verify-full",
    "sslmode=verify-full&requiressl=0",
    "sslmode=verify-full&requiressl=1",
    "requiressl=0&sslmode=verify-full",
    "requiressl=1",
    "ssl=true",
    "sslmode=verify-full&sslrootcert=system&ssl=true",
    "sslmode=verify-full&%73sl=true",
    "sslmode=verify-full&requires%73l=0",
]


@pytest.mark.parametrize("query", ALIASED)
def test_ssl_and_requiressl_are_refused_whatever_sslmode_is_written_beside_them(query):
    """The sslmode that libpq will use is not the one written: ``sslmode=verify-full&requiressl=0`` is ``prefer``."""
    address = f"postgresql://u:{PASSWORD}@{HERE}/d?{query}"
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "requiressl" in str(refused) and "write sslmode" in str(refused)
    assert PASSWORD not in _shown(refused)


@pytest.mark.parametrize(
    "address",
    [
        f"host={HERE} user=u requiressl=0 sslmode=verify-full",  # the keyword form of an address
        f"host={HERE} user=u requiressl = 1",
        f"host={HERE} user=u requiressl='1' sslmode=verify-full",
        f"host={HERE} user=u application_name=a\\ b requiressl=0",  # a bare value, its blank written with a backslash
        f"host={HERE} application_name='a b' requiressl=0",
        f"postgresql://u:pa?ss@{HERE}/d?sslmode=verify-full&requiressl=0",  # a ? of the password, before the query
        f"postgresql://u:pa/ss@{HERE}/d?sslmode=verify-full&requiressl=0",  # a / ends what libpq takes for a user
        f"postgresql://{HERE}/d?requiressl=0&application_name=a@b",  # an @ after a /: no user information, a value
        f"postgres://u@{HERE}/d?sslmode=verify-full&requiressl=0",
    ],
)
def test_the_aliases_are_found_in_the_forms_of_an_address_that_libpq_reads(address):
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "requiressl" in str(refused), refused


@pytest.mark.parametrize(
    "address",
    [
        f"host={HERE} user=u password='x ssl=true requiressl=1'",
        f"host={HERE} user=u application_name=requiressl",
        f"postgresql://u:ssl%3Dtrue@{HERE}/d",
        f"postgresql://u:requiressl%3D1@{HERE}/ssl%3Dtrue",
        f"postgresql://u@{HERE}/d?application_name=ssl%3Dtrue",
        f"postgresql://u@{HERE}/d?application_name=requiressl&sslmode=prefer",
        f"postgresql://u@{HERE}/d?sslmode=verify-full&sslrootcert=system&sslcert=%2Fssl",
    ],
)
def test_a_text_that_is_not_the_name_of_a_parameter_is_not_taken_for_an_alias(address):
    assert _parameters(address, SCHEMA, 5)


def _uri(rng: random.Random, pairs: list[str]) -> str:
    """A URI whose user information, hosts and database hold the marks that could move the start of its query (a ``?``,
    a ``/``, an ``@``, brackets, commas, text that looks like a parameter), then the parameters ``pairs``."""
    userinfo = rng.choice(
        ["", "u@", "u:pw@", "u:p%3Fw@", "u:p?w@", "u:p/w@", "u:p&w@", "u:p=w@", "u:p[w@", "?x@", "u:ssl%3Dtrue@"]
    )
    host = rng.choice([HERE, "[::1]", f"{HERE},{HERE}", f"{HERE}:5432", "[a?b]", "[a/b]", "[a@b]", f"{HERE}:1,[::1]:2"])
    path = rng.choice(["", "/", "/d", "/d%3Fb", "/ssl%3Dtrue", "/requiressl=1", "/d@x", "/d,e", "/d&x=y"])
    return f"postgresql://{userinfo}{host}{path}" + (("?" + "&".join(pairs)) if pairs else "")


def _keywords(rng: random.Random, pairs: list[tuple[str, str]]) -> str:
    """A keyword/value string, its values bare or quoted, with or without spaces around the equal sign."""
    plain = set(" '") | {chr(92)}
    written = []
    for name, value in [("host", HERE), *pairs]:
        space = rng.choice(["", " "])
        escaped = value.replace(chr(92), chr(92) * 2).replace("'", chr(92) + "'")  # a quote, a backslash: a backslash
        if value and not set(value) & plain:
            shown = rng.choice([value, f"'{value}'"])
        elif value and rng.random() < 0.5:  # bare, with each blank written with a backslash too
            shown = escaped.replace(" ", chr(92) + " ")
        else:  # quoted
            shown = f"'{escaped}'"
        written.append(f"{name}{space}={space}{shown}")
    return " ".join(written)


def _libpq_reads(address: str) -> bool:
    try:
        conninfo_to_dict(address)
    except Exception:
        return False
    return True


def test_an_alias_is_found_whenever_libpq_would_apply_it():
    """Soundness against libpq itself: whenever renaming the alias makes libpq refuse an address that it read before,
    the alias was one of its parameters, and the address is found. Random URIs and keyword strings."""
    rng = random.Random(20261014)
    aliases = ["ssl=true", "requiressl=1", "requiressl=0", "%73sl=true", "requires%73l=1", "requiressl="]
    others = ["sslmode=verify-full", "sslmode=disable", "sslrootcert=system", "application_name=a",
              "application_name=ssl%3Dtrue", "application_name=requiressl", "target_session_attrs=any",
              "application_name=a@b", "application_name=a/b", "application_name=a?b"]  # fmt: skip
    effective_uris = 0
    for _ in range(1500):
        pairs = rng.sample(others, rng.randint(0, 3)) + rng.sample(aliases, rng.randint(0, 2))
        rng.shuffle(pairs)
        address = _uri(rng, pairs)
        renamed = address  # the same address, its aliases renamed: libpq refuses a parameter it does not know
        for name in ("ssl=", "requiressl=", "%73sl=", "requires%73l="):
            for prefix in ("?", "&"):
                renamed = renamed.replace(prefix + name, prefix + "x" + name)
        effective = _libpq_reads(address) and not _libpq_reads(renamed)
        found = db_module._has_sslmode_alias(address)
        effective_uris += effective
        assert found or not effective, address
        assert not found or any(alias in address for alias in ("ssl=", "requiressl", "%73sl", "requires%73l")), address
    assert effective_uris > 200  # the corpus holds what it is meant to hold
    effective_keywords = 0
    for _ in range(1500):
        pairs = [("sslmode", rng.choice(["verify-full", "disable"])) for _ in range(rng.randint(0, 2))]
        pairs += [("requiressl", rng.choice(["0", "1"])) for _ in range(rng.randint(0, 2))]
        pairs += [("application_name", "a b'c" + chr(92) + "d"), ("password", "ssl=true requiressl=1")]
        rng.shuffle(pairs)
        address = _keywords(rng, pairs)
        effective = _libpq_reads(address) and not _libpq_reads(address.replace("requiressl", "xrequiressl"))
        found = db_module._has_sslmode_alias(address)
        effective_keywords += effective
        assert found or not effective, address
        assert found == any(name == "requiressl" for name, _ in pairs), address
    assert effective_keywords > 200


def test_sslrootcert_system_alone_is_the_verify_full_that_libpq_makes_of_it(tmp_path, monkeypatch):
    for variable, value in hostile(tmp_path).items():
        if variable not in REFUSED:
            monkeypatch.setenv(variable, value)  # PGSSLMODE is disable here
    parameters = _parameters(f"postgresql://u@{HERE}:{_closed_port()}/d?sslrootcert=system", SCHEMA, 5)
    assert parameters["sslmode"] == "verify-full"
    assert _libpq_bad(parameters) is None  # libpq refuses a weaker mode with sslrootcert=system
    written = _parameters(f"postgresql://u@{HERE}:{_closed_port()}/d?sslrootcert=system&sslmode=verify-full", SCHEMA, 5)
    assert written["sslmode"] == "verify-full" and _libpq_bad(written) is None
    for other in ("%2Fetc%2Fssl%2Fca.pem", "System", "system%20", ""):  # only the word system makes verify-full of it
        alone = _parameters(f"postgresql://u@{HERE}:{_closed_port()}/d?sslrootcert={other}", SCHEMA, 5)
        assert alone["sslmode"] == "prefer" and _libpq_bad(alone) is None


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "require", "verify-ca"])
def test_sslrootcert_system_beside_another_sslmode_is_refused_in_words(mode):
    """libpq takes the system's certificate authorities with verify-full alone, and refuses any other mode written
    beside them, saying only ``weak sslmode``: the operator would see the driver's error by its type."""
    address = f"postgresql://u:{PASSWORD}@{HERE}:{_closed_port()}/d?sslmode={mode}&sslrootcert=system"
    assert "weak sslmode" in (_libpq_bad(conninfo_to_dict(address)) or "")  # why: libpq's own words, before it connects
    refused = _raised(lambda: Database(address, schema=SCHEMA))
    assert type(refused) is ValueError and "sslrootcert=system" in str(refused) and "verify-full" in str(refused)
    assert PASSWORD not in _shown(refused)


BOUNDS = {
    "keepalives": "1", "keepalives_idle": "10", "keepalives_interval": "5", "keepalives_count": "3",
    "tcp_user_timeout": "20000",
}  # fmt: skip


@pytest.mark.parametrize(
    "query", ["", "?keepalives=0", "?keepalives_idle=7200&keepalives_count=1000&tcp_user_timeout=0"]
)
def test_a_connection_that_went_silent_is_given_up_whatever_the_address_says(query):
    """TCP's keepalives find a peer gone silent while a connection waits, and what was sent and not acknowledged gives
    the connection up: the server's own bounds, which libpq has no variable for, and which an address does not turn
    off."""
    held = _libpq_holds(_parameters(f"postgresql://u@{HERE}:{_closed_port()}/d{query}", SCHEMA, 5))
    assert {keyword: held[keyword] for keyword in BOUNDS} == BOUNDS
    assert all(option.envvar is None for keyword, option in _all_options().items() if keyword in BOUNDS)


def _all_options() -> dict[str, pq.ConninfoOption]:
    return {o.keyword.decode(): o for o in pq.Conninfo.get_defaults()}


@pytest.mark.parametrize("minimum", ["3.2", "latest"])
def test_a_minimum_protocol_version_above_3_0_lifts_the_maximum_that_is_pinned(minimum):
    port = _closed_port()
    parameters = _parameters(f"postgresql://u@{HERE}:{port}/d?min_protocol_version={minimum}", SCHEMA, 5)
    assert parameters["max_protocol_version"] == minimum and _libpq_bad(parameters) is None
    unchanged = _parameters(f"postgresql://u@{HERE}:{port}/d?min_protocol_version=3.0", SCHEMA, 5)
    assert unchanged["max_protocol_version"] == "3.0" and _libpq_bad(unchanged) is None
    assert _parameters(f"postgresql://u@{HERE}:{port}/d", SCHEMA, 5)["max_protocol_version"] == "3.0"
    named = _parameters(
        f"postgresql://u@{HERE}:{port}/d?min_protocol_version={minimum}&max_protocol_version=latest", SCHEMA, 5
    )
    assert named["max_protocol_version"] == "latest"  # the maximum the address names is its own


@pytest.mark.parametrize(
    "query",
    [
        "hostaddr=127.0.0.1&sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fca.pem",
        "hostaddr=127.0.0.1&sslrootcert=system",  # verify-full, made of sslrootcert=system
        "host=,127.0.0.1&hostaddr=127.0.0.1,127.0.0.1&sslmode=verify-full&sslrootcert=system",  # the first place
    ],
)
def test_a_verify_full_address_with_a_hostaddr_and_no_host_is_refused_in_words(query):
    """libpq checks the certificate against the host's name, and says only ``host name must be specified for a verified
    SSL connection``: the operator would see a type. verify-ca checks no name, and connects."""
    refused = _raised(lambda: Database(f"postgresql://u:{PASSWORD}@/d?{query}", schema=SCHEMA))
    assert type(refused) is ValueError and "hostaddr" in str(refused) and "host" in str(refused)
    assert PASSWORD not in _shown(refused) and HERE not in _shown(refused)
    kept = _parameters(
        f"postgresql://u@/d?hostaddr={HERE}&sslmode=verify-ca&sslrootcert=%2Fetc%2Fssl%2Fca.pem", SCHEMA, 5
    )
    assert kept["hostaddr"] == HERE and kept["host"] == ""
    named = f"postgresql://u@db.acme.lan/d?hostaddr={HERE}&sslmode=verify-full&sslrootcert=system"
    assert _parameters(named, SCHEMA, 5)["host"] == "db.acme.lan"  # the name to check, and the address to go to


def test_the_path_that_is_not_there_holds_a_character_that_windows_refuses_in_a_file_name():
    """Else a local user could make the folder in the root of the system drive and put a password file or a root
    certificate in it. libpq reads such a path as not there, for every option (tried against a PostgreSQL with TLS)."""
    assert set(_NO_FILE) & set('<>:"|?*') and not os.path.exists(_NO_FILE)
