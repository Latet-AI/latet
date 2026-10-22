"""What a Latet server's start asks of PostgreSQL: the lock of the migrations, a migration that fails, the address as the
only word on where the server connects and as whom, and what is said when the database refuses it. The tests of the
connection that need no PostgreSQL are in tests/unit/server/test_database.py."""

import logging
import secrets
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from urllib.parse import quote, urlsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from latet.server import db as db_module
from latet.server.db import Database, DatabaseError, SchemaMismatch, _lock_key, check_schema, migrate

from .conftest import DATABASE, PINS
from .libpq_environment import hostile
from .support import role_catalog

pytestmark = pytest.mark.server


def _address_parts() -> list[str]:
    """The test address' host, port, user, password and database; empty ones when it is unset or not readable (the tests
    are skipped then, saying why)."""
    try:
        found = conninfo_to_dict(DATABASE)
    except psycopg.Error:
        found = {}
    return [found.get(key, "") for key in ("host", "port", "user", "password", "dbname")]


HOST, PORT, USER, PASSWORD, DBNAME = _address_parts()


def _shown(error: BaseException) -> str:
    """All an error shows of itself: its words, its repr, and its chain as a traceback prints it (no source lines)."""
    return f"{error}\n{error!r}\n{''.join(traceback.format_exception(error, limit=0))}"


def _raised(call) -> Exception:
    try:
        call()
    except Exception as error:
        return error
    pytest.fail("nothing was raised")


def test_a_migration_waits_for_the_instance_that_holds_the_lock(settings, schema):
    db = Database(settings.database_url, schema=schema)
    holder = psycopg.connect(DATABASE, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (_lock_key(schema),))
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(migrate, db)
            time.sleep(1)
            assert not running.done()  # another instance is migrating: it waits
            holder.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(schema),))
            assert running.result(timeout=30)[0] == "latet.server.migrations:001_core"
    finally:
        holder.close()
        db.close()


def test_a_migration_waits_for_the_lock_a_bounded_time_and_says_why_it_stopped(settings, schema, monkeypatch):
    """An instance that stopped while it held the lock leaves it to its connection, which may linger: the next waits
    ``MIGRATION_WAIT`` at most, and says what holds it up."""
    monkeypatch.setattr(db_module, "MIGRATION_WAIT", 1)
    db = Database(settings.database_url, schema=schema)
    holder = psycopg.connect(DATABASE, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (_lock_key(schema),))
        with ThreadPoolExecutor(1) as pool:
            started = time.monotonic()
            running = pool.submit(lambda: _raised(lambda: migrate(db)))
            try:
                refused = running.result(timeout=15)
            finally:
                holder.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(schema),))  # a migration that waits ends
        assert 0.9 < time.monotonic() - started < 10
        assert (
            type(refused) is DatabaseError and "lock of the migrations" in str(refused) and "1 seconds" in str(refused)
        )
        assert PASSWORD not in _shown(refused)
        assert _tables_of(schema) == 0  # nothing was made meanwhile
        assert migrate(db)[0] == "latet.server.migrations:001_core"  # nothing of the wait is left: it is free
    finally:
        holder.close()
        db.close()


def test_a_statement_runs_a_bounded_time_and_a_migration_as_long_as_it_takes(settings, schema, monkeypatch, tmp_path):
    """A statement a live PostgreSQL holds (a lock that is not let go, a query that does not end) gives its connection
    back after ``STATEMENT_SECONDS``; a migration's own statements have no bound, neither PostgreSQL's nor that of the
    wait for its answer (``ANSWER_SECONDS``)."""
    with closing(Database(settings.database_url, schema=schema)) as db:
        migrate(db)
        check_schema(db)
        with db.connection() as conn:
            assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
    monkeypatch.setitem(db_module._PINNED, "statement_timeout", "500")  # half a second, for the test
    monkeypatch.setattr(db_module, "ANSWER_SECONDS", 1.5)  # past PostgreSQL's own, as the server's is
    with closing(Database(settings.database_url, schema=schema)) as db:
        check_schema(db)
        with pytest.raises(psycopg.errors.QueryCanceled), db.connection() as conn:
            conn.execute("SELECT pg_sleep(3)")
        package = tmp_path / "latet_t_slow_migrations"  # a migration of two seconds: past both bounds
        package.mkdir()
        (package / "__init__.py").write_bytes(b"")
        (package / "001_slow.sql").write_bytes(b"CREATE TABLE slow AS SELECT 1 AS one FROM pg_sleep(2);\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        assert migrate(db, packages=["latet_t_slow_migrations"]) == ["latet_t_slow_migrations:001_slow"]


class _Relay:
    """A relay between the connections of a test and PostgreSQL that can stop passing anything on, either way, every
    connection left open: a PostgreSQL whose host acknowledges what is sent and answers nothing (its process stopped, a
    relay that lost its way), which neither TCP's keepalives nor the user timeout see."""

    def __init__(self) -> None:
        self.hole = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._open: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                peer, _ = self._listener.accept()
                upstream = socket.create_connection((HOST, int(PORT)), timeout=5)
            except OSError:
                return
            upstream.settimeout(None)
            self._open += [peer, upstream]
            for one, other in ((peer, upstream), (upstream, peer)):
                threading.Thread(target=self._pass, args=(one, other), daemon=True).start()

    def _pass(self, one: socket.socket, other: socket.socket) -> None:
        while True:
            try:
                data = one.recv(65536)
                while data and self.hole.is_set():  # read, and so acknowledged, but passed on to nobody for now
                    time.sleep(0.02)
                if not data:
                    return
                other.sendall(data)
            except OSError:
                return

    def close(self) -> None:
        self.hole.clear()
        self._listener.close()
        for one in self._open:
            with suppress(OSError):
                one.close()


def _asked(db: Database) -> None:
    with db.transaction() as conn:  # as the stores ask: its commit, or its rollback, waits for an answer too
        conn.execute("SELECT 1")


def _unanswered(db: Database) -> tuple[BaseException | None, bool]:
    """What a statement that is never answered raises, and whether its connection is closed after it."""
    with db.connection() as conn:
        try:
            conn.execute("SELECT 1")
        except psycopg.Error as exc:
            return exc, conn.closed
    return None, conn.closed


def test_a_postgresql_that_answers_nothing_gives_the_connection_back_after_a_bound(schema, monkeypatch):
    """Its host acknowledges what is sent, and nothing comes back: TCP's keepalives and the user timeout see nothing
    wrong, and PostgreSQL's own statement bound never comes (Task 16 measured requests still waiting at 40 s). A call
    fails after ``ANSWER_SECONDS``, its connection closed, and the next call has another."""
    monkeypatch.setattr(db_module, "ANSWER_SECONDS", 1.0)
    relay = _Relay()
    try:
        address = f"postgresql://{quote(USER)}:{quote(PASSWORD, safe='')}@127.0.0.1:{relay.port}/{DBNAME}"
        with closing(Database(address, schema=schema, min_size=2, max_size=2)) as db:
            db.pool.wait(10)  # both connections open before PostgreSQL stops answering
            _asked(db)
            relay.hole.set()
            with ThreadPoolExecutor(2) as pool:
                started, call, held = time.monotonic(), pool.submit(_unanswered, db), pool.submit(_asked, db)
                try:
                    silent, closed = call.result(timeout=15)  # without a bound it waits for ever: the test says so
                    with pytest.raises(psycopg.OperationalError, match="did not answer"):
                        held.result(timeout=15)  # a transaction's wait too
                finally:
                    relay.hole.clear()
            assert isinstance(silent, psycopg.OperationalError) and time.monotonic() - started < 10, silent
            assert "did not answer within 1 seconds" in str(silent) and PASSWORD not in str(silent)
            assert closed  # what it asked may still be under way: the connection is not used again
            _asked(db)  # through a connection of its own: the pool made another
    finally:
        relay.close()


def test_a_role_without_create_on_the_database_migrates_the_schema_made_for_it(schema):
    """An operator who gives the server's role its schema and nothing more on the database (least privilege): CREATE
    SCHEMA, even IF NOT EXISTS, would ask for CREATE on the database before it looks."""
    role, password = "latet_r_" + secrets.token_hex(4), "r0le-" + secrets.token_hex(4)
    with role_catalog() as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        admin.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(schema), sql.Identifier(role)))
    try:
        address = f"postgresql://{role}:{password}@{HOST}:{PORT}/{DBNAME}"
        with psycopg.connect(address, autocommit=True) as plain, pytest.raises(psycopg.errors.InsufficientPrivilege):
            plain.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))  # why
        with closing(Database(address, schema=schema)) as db:
            assert migrate(db)[0] == "latet.server.migrations:001_core"
            check_schema(db)
    finally:
        with role_catalog() as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_the_lock_of_one_schema_does_not_hold_the_migrations_of_another(settings, schema):
    db = Database(settings.database_url, schema=schema)
    holder = psycopg.connect(DATABASE, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (_lock_key(schema + "_other"),))
        assert _lock_key(schema + "_other") != _lock_key(schema)
        assert migrate(db)[0] == "latet.server.migrations:001_core"
    finally:
        holder.close()
        db.close()


def test_a_migration_that_fails_is_named_and_leaves_nothing_of_itself(db, tmp_path, monkeypatch):
    package = tmp_path / "latet_t_failing_migrations"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"")
    (package / "001_half.sql").write_bytes(
        b"CREATE TABLE half (x INTEGER);\nSELECT * FROM a_table_that_is_not_there;\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    failed = _raised(lambda: migrate(db, packages=["latet_t_failing_migrations"]))
    assert type(failed) is DatabaseError and "latet_t_failing_migrations:001_half" in str(failed)
    assert "UndefinedTable" in str(failed)  # the type of the driver's error is said...
    assert "a_table_that_is_not_there" not in _shown(failed)  # ...and not its words
    with db.connection() as conn:
        assert conn.execute("SELECT to_regclass('half')").fetchone() == (None,)  # its statements ended with it
        applied = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name LIKE 'latet_t_failing%'").fetchone()
        assert applied == (0,)
    (package / "001_half.sql").write_bytes(b"CREATE TABLE half (x INTEGER);\n")
    again = migrate(db, packages=["latet_t_failing_migrations"])  # no lock was left: it is not stuck
    assert again == ["latet_t_failing_migrations:001_half"]


def test_a_connection_of_the_pool_works_in_the_schema_alone(db):
    with db.connection() as conn:
        assert conn.execute("SHOW search_path").fetchone() == (db.schema,)
        assert conn.execute("SELECT current_schema()").fetchone() == (db.schema,)
        assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() == (None,)


def test_a_complete_address_is_used_whatever_the_environment_holds(settings, schema, tmp_path, monkeypatch):
    """Every variable libpq reads for an option is set wrong (the two that are refused apart), and the server connects
    as its address says."""
    for variable, value in hostile(tmp_path).items():
        if variable not in ("PGSERVICE", "PGREQUIREAUTH"):
            monkeypatch.setenv(variable, value)
    db = Database(settings.database_url, schema=schema)
    try:
        assert migrate(db)[0] == "latet.server.migrations:001_core"
        with db.connection() as conn:
            assert (conn.info.host, str(conn.info.port), conn.info.user, conn.info.dbname) == (HOST, PORT, USER, DBNAME)
            assert conn.execute("SHOW search_path").fetchone() == (schema,)  # not PGOPTIONS'
            assert conn.execute("SHOW application_name").fetchone() != ("env-app",)
            assert conn.info.encoding != "latin1"  # nor PGCLIENTENCODING's
    finally:
        db.close()


def _lost_search_path(monkeypatch, instead: str) -> None:
    """Connections that start with the search path ``instead`` of the schema's, as through a pooler that drops the
    start-up options (PgBouncer, with ``options`` among the parameters it ignores)."""
    real = db_module._parameters
    monkeypatch.setattr(db_module, "_parameters", lambda *args: {**real(*args), "options": instead})


@pytest.fixture
def decoy(schema):
    """Another schema of the test's own, where a search path that is not the server's leads."""
    name = schema + "_decoy"
    with psycopg.connect(DATABASE, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    yield name
    with psycopg.connect(DATABASE, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def _tables_of(schema: str) -> int:
    with psycopg.connect(DATABASE) as conn:
        found = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (schema,))
        return found.fetchone()[0]


def test_the_migrations_are_refused_through_connections_that_lost_the_search_path(settings, schema, decoy, monkeypatch):
    _lost_search_path(monkeypatch, f"-c search_path={decoy}")
    db = Database(settings.database_url, schema=schema)
    try:
        failed = _raised(lambda: migrate(db))
    finally:
        db.close()
    assert type(failed) is DatabaseError and "search path" in str(failed) and schema in str(failed)
    assert decoy not in _shown(failed)  # the other schema's name is not said
    assert _tables_of(decoy) == 0  # no table was made where the connections stood, and none in the server's schema
    assert _tables_of(schema) == 0


@pytest.mark.parametrize(
    "columns",
    [
        "name TEXT PRIMARY KEY, applied DOUBLE PRECISION NOT NULL",  # another role's table that lists every migration
        "version INTEGER",  # a table of that name that is not ours at all: it is not read, so it is not what fails
    ],
)
def test_a_table_of_another_schema_at_the_head_of_the_search_path_is_not_the_servers(
    settings, schema, decoy, monkeypatch, columns
):
    with psycopg.connect(DATABASE, autocommit=True) as admin:  # what a table made beforehand by another role would be
        admin.execute(sql.SQL("CREATE TABLE {}.schema_migrations ({})").format(sql.Identifier(decoy), sql.SQL(columns)))
        if "name" in columns:
            for migration, _ in db_module._migrations(db_module.MIGRATIONS):
                insert = sql.SQL("INSERT INTO {}.schema_migrations VALUES (%s, 0)").format(sql.Identifier(decoy))
                admin.execute(insert, (migration,))
    _lost_search_path(monkeypatch, f"-c search_path={decoy}")
    db = Database(settings.database_url, schema=schema)  # its own schema is not migrated
    try:
        refused = _raised(lambda: check_schema(db))
    finally:
        db.close()
    assert type(refused) is DatabaseError and "search path" in str(refused) and schema in str(refused)
    assert decoy not in _shown(refused)


@pytest.mark.parametrize("migrated", [True, False])
@pytest.mark.parametrize("behind", ["decoy", "public"])
def test_a_search_path_that_starts_in_the_schema_and_goes_on_is_not_the_schema_alone(
    settings, schema, decoy, monkeypatch, behind, migrated
):
    """The schema is the first of the path and another stands behind it: whatever the schema does not hold is looked
    for there (on PostgreSQL 14 and older an operator or a function of another role's, in public, with a closer match
    to its arguments is chosen over the schema's own)."""
    if migrated:
        db = Database(settings.database_url, schema=schema)
        try:
            migrate(db)
            check_schema(db)  # the schema alone: nothing to say
        finally:
            db.close()
    _lost_search_path(monkeypatch, f"-c search_path={schema},{decoy if behind == 'decoy' else 'public'}")
    db = Database(settings.database_url, schema=schema)
    try:
        for call in (lambda: check_schema(db), lambda: migrate(db)):
            refused = _raised(call)
            assert type(refused) is DatabaseError and "search path" in str(refused) and schema in str(refused)
            assert decoy not in _shown(refused)
    finally:
        db.close()
    if not migrated:
        assert _tables_of(schema) == 0  # nothing was made where the path goes on, nor in the schema itself


def test_the_names_are_not_read_through_a_search_path_that_goes_on_after_the_schema(
    settings, schema, decoy, monkeypatch
):
    """The schema is there and holds no table yet, and the schema behind it holds a table of that name that is not ours:
    read through the path it would fail with the driver's error, where the sentence about the path is wanted."""
    with psycopg.connect(DATABASE, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin.execute(sql.SQL("CREATE TABLE {}.schema_migrations (version INTEGER)").format(sql.Identifier(decoy)))
    _lost_search_path(monkeypatch, f"-c search_path={schema},{decoy}")
    db = Database(settings.database_url, schema=schema)
    try:
        refused = _raised(lambda: check_schema(db))
    finally:
        db.close()
    assert type(refused) is DatabaseError and "search path" in str(refused) and schema in str(refused)
    assert "UndefinedColumn" not in str(refused) and decoy not in _shown(refused)


def test_a_pooler_that_drops_the_options_of_a_role_named_like_its_schema_is_refused(schema, monkeypatch):
    """The search path of a role is ``"$user", public``: with the role named like the schema, the schema is the first of
    it and public stands behind. A pooler that drops the start-up options leaves it so."""
    role, password = schema, "r0le-" + secrets.token_hex(4)
    with role_catalog() as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        admin.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(DBNAME), sql.Identifier(role)))
        admin.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(schema), sql.Identifier(role)))
    try:
        with psycopg.connect(f"postgresql://{role}:{password}@{HOST}:{PORT}/{DBNAME}") as alone:  # the options dropped
            assert alone.execute("SELECT current_schemas(false)").fetchone() == ([schema, "public"],)
            assert alone.execute("SELECT current_schema()").fetchone() == (schema,)  # the first: all that was asked
        _lost_search_path(monkeypatch, "")
        db = Database(f"postgresql://{role}:{password}@{HOST}:{PORT}/{DBNAME}", schema=schema)
        try:
            for call in (lambda: check_schema(db), lambda: migrate(db)):
                refused = _raised(call)
                assert type(refused) is DatabaseError and "search path" in str(refused) and schema in str(refused)
                assert password not in _shown(refused)
        finally:
            db.close()
    finally:
        with role_catalog() as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_a_schema_that_is_not_there_yet_is_still_the_older_schema_and_not_a_lost_search_path(settings, schema):
    db = Database(settings.database_url, schema=schema)
    try:
        with pytest.raises(SchemaMismatch, match="latet server migrate"):
            check_schema(db)
    finally:
        db.close()


def test_a_password_with_every_mark_of_an_address_is_the_password_all_the_same(settings, schema):
    """The address is read once, by libpq, and the connection is made of what it read: a password that holds a space, a
    quote, a backslash and the marks of an address reaches the server as it was written."""
    password = "p@ss w:rd/#?%&='" + chr(34) + chr(92) + "+"
    role = "latet_t_" + secrets.token_hex(4)
    with role_catalog() as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    try:
        db = Database(f"postgresql://{role}:{quote(password, safe='')}@{HOST}:{PORT}/{DBNAME}", schema=schema)
        try:
            with db.connection() as conn:
                assert conn.execute("SELECT current_user").fetchone() == (role,)
        finally:
            db.close()
    finally:
        with role_catalog() as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_the_user_password_and_database_of_the_environment_do_not_complete_an_address(schema, monkeypatch):
    monkeypatch.setenv("PGUSER", USER)
    monkeypatch.setenv("PGPASSWORD", PASSWORD)
    monkeypatch.setenv("PGDATABASE", DBNAME)
    bare = f"postgresql://{HOST}:{PORT}/"
    with psycopg.connect(bare, connect_timeout=5) as conn:  # libpq alone: the environment is all it needs
        assert conn.info.user == USER
    db = Database(bare, schema=schema, connect_timeout=2)
    try:
        refused = _raised(lambda: migrate(db))  # as the system's user, with no password, the server refuses
    finally:
        db.close()
    assert type(refused) is DatabaseError and "OperationalError" in str(refused)


def test_a_password_file_does_not_give_the_password_an_address_leaves_out(tmp_path, schema, monkeypatch):
    passfile = tmp_path / "pgpass"
    passfile.write_bytes(f"{HOST}:{PORT}:{DBNAME}:{USER}:{PASSWORD}\n".encode())
    passfile.chmod(0o600)  # libpq ignores a password file others can read
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    address = f"postgresql://{USER}@{HOST}:{PORT}/{DBNAME}"
    with psycopg.connect(address, connect_timeout=5):  # libpq alone: the file gives the password
        pass
    db = Database(address, schema=schema, connect_timeout=2)
    try:
        refused = _raised(lambda: migrate(db))
    finally:
        db.close()
    assert type(refused) is DatabaseError and "OperationalError" in str(refused)


def test_a_service_file_does_not_complete_an_address(tmp_path, schema, monkeypatch):
    services = tmp_path / "pg_service.conf"
    services.write_bytes(
        f"[latet-test]\nhost={HOST}\nport={PORT}\nuser={USER}\npassword={PASSWORD}\ndbname={DBNAME}\n".encode()
    )
    monkeypatch.setenv("PGSERVICEFILE", str(services))
    monkeypatch.setenv("PGSERVICE", "latet-test")
    with psycopg.connect("", connect_timeout=5):  # libpq alone: the service is all it needs
        pass
    refused = _raised(lambda: Database(DATABASE, schema=schema))
    assert type(refused) is ValueError and "PGSERVICE" in str(refused) and PASSWORD not in _shown(refused)


def test_a_refusal_by_the_server_is_neither_shown_nor_logged_in_its_words(settings, schema, caplog):
    wrong = DATABASE.replace(PASSWORD, "wrong-pw-5f3a")
    assert wrong != DATABASE
    caplog.set_level(logging.DEBUG)
    db = Database(wrong, schema=schema, connect_timeout=2)
    try:
        refused = _raised(lambda: migrate(db))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not any("error connecting" in r.getMessage() for r in caplog.records):
            time.sleep(0.05)
    finally:
        db.close()
    assert type(refused) is DatabaseError and "OperationalError" in str(refused)
    assert "authentication" not in _shown(refused) and "wrong-pw-5f3a" not in _shown(refused)
    pool = [r for r in caplog.records if r.name == "psycopg.pool" and r.levelno >= logging.WARNING]
    assert any("OperationalError" in r.getMessage() for r in pool)  # what the pool says: the type of the error
    assert not any("authentication" in r.getMessage() or HOST in r.getMessage() for r in pool)
    assert "wrong-pw-5f3a" not in repr([r.__dict__ for r in caplog.records])


# --- the settings of a session that the server rests on ------------------------------------------------------------

A_TIME = 1_760_431_234.123456  # a time with microseconds: 16 digits, 15 is what a short float gives back


def test_every_connection_has_the_servers_session_settings_whatever_its_role_defaults_to(role_url, schema):
    """A role may default to a short float and to REPEATABLE READ; a pooled connection of the server does not."""
    url = role_url(extra_float_digits="0", default_transaction_isolation="repeatable read")
    with psycopg.connect(url) as plain:  # the role as it is: what a connection would have without the pins
        assert plain.execute("SHOW extra_float_digits").fetchone() == ("0",)
        assert plain.execute("SHOW default_transaction_isolation").fetchone() == ("repeatable read",)
        assert plain.execute("SELECT %s::float8", (A_TIME,)).fetchone() != (A_TIME,)
    with closing(Database(url, schema=schema, max_size=3)) as db:
        migrate(db)
        check_schema(db)  # the start accepts connections that have them
        with db.connection() as a, db.connection() as b, db.connection() as c:  # three connections of the pool
            for conn in (a, b, c):
                assert conn.execute("SHOW extra_float_digits").fetchone() == ("3",)
                assert conn.execute("SHOW default_transaction_isolation").fetchone() == ("read committed",)
                assert conn.execute("SHOW transaction_isolation").fetchone() == ("read committed",)
                assert conn.execute("SELECT %s::float8", (A_TIME,)).fetchone() == (A_TIME,)  # what was written


TEXT_OF_EVERY_SCRIPT = "Zo" + chr(0xEB) + " " + chr(0x5C71) + chr(0x7530) + " " + chr(0x1F469)  # what LATIN1 lacks


def test_every_connection_speaks_utf8_whatever_its_role_or_its_address_says(role_url, schema):
    """A role may default to another client encoding, and an address may name one: a name, a label or an entry's text of
    another script would be refused on its way to PostgreSQL, or read back otherwise. The server's connections are
    UTF8 whatever either says."""
    url = role_url(client_encoding="LATIN1")
    with psycopg.connect(url) as plain:  # the role as it is: what a connection would have without the pin
        assert plain.execute("SHOW client_encoding").fetchone() == ("LATIN1",)
    for address in (url, url + "?client_encoding=LATIN1"):
        with closing(Database(address, schema=schema, max_size=2)) as db:
            migrate(db)
            check_schema(db)  # the start accepts connections that have it
            with db.connection() as conn:
                assert conn.execute("SHOW client_encoding").fetchone() == ("UTF8",)
                assert conn.execute("SELECT %s::text", (TEXT_OF_EVERY_SCRIPT,)).fetchone() == (TEXT_OF_EVERY_SCRIPT,)


def test_the_start_refuses_connections_whose_client_encoding_is_not_utf8(settings, schema, monkeypatch):
    """As a pooler or a proxy that sets another encoding leaves them: the words name the setting."""
    real = db_module._parameters
    monkeypatch.setattr(db_module, "_parameters", lambda *args: {**real(*args), "client_encoding": "LATIN1"})
    with closing(Database(settings.database_url, schema=schema)) as db:
        for call in (lambda: check_schema(db), lambda: migrate(db)):
            refused = _raised(call)
            assert type(refused) is DatabaseError and "client_encoding UTF8" in str(refused)
            assert PASSWORD not in _shown(refused)
    assert _tables_of(schema) == 0


FLOAT, ISOLATION = "extra_float_digits", "default_transaction_isolation"


@pytest.mark.parametrize(
    ("options", "named"),
    [
        ("{path} {isolation}", (FLOAT,)),
        ("{path} {digits}", (ISOLATION,)),
        ("{path}", (FLOAT, ISOLATION)),
        ("{path} -c extra_float_digits=1 -c default_transaction_isolation=serializable", (FLOAT, ISOLATION)),
    ],
    ids=["no float pin", "no isolation pin", "neither", "other values"],
)
def test_the_start_refuses_connections_that_lost_a_setting_the_server_rests_on(
    role_url, schema, monkeypatch, options, named
):
    """As a pooler that drops the start-up options (or a proxy that sets others) leaves them: the words name the setting
    that is not what the server sets and not the search path, which is."""
    url = role_url(extra_float_digits="0", default_transaction_isolation="repeatable read")
    options = options.format(path=f"-c search_path={schema}", digits=PINS[FLOAT], isolation=PINS[ISOLATION])
    real = db_module._parameters
    monkeypatch.setattr(db_module, "_parameters", lambda *args: {**real(*args), "options": options})
    with closing(Database(url, schema=schema)) as db:
        for call in (lambda: check_schema(db), lambda: migrate(db)):
            refused = _raised(call)
            assert type(refused) is DatabaseError and "search path" not in str(refused)
            for name in (FLOAT, ISOLATION):
                assert (name in str(refused)) == (name in named), name
            assert urlsplit(url).password not in _shown(refused)
    assert _tables_of(schema) == 0  # nothing was made where the connections stood


# --- the database's encoding ------------------------------------------------------------------------------------


@pytest.fixture
def in_encoding():
    """Makes a database of the test's own in an encoding, and returns the address that reaches it."""
    made: list[str] = []

    def make(encoding: str) -> str:
        name = "latet_t_" + secrets.token_hex(5)
        with psycopg.connect(DATABASE, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} ENCODING {} LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0").format(
                    sql.Identifier(name), sql.Literal(encoding)
                )
            )
        made.append(name)
        return make_conninfo(DATABASE, dbname=name)

    yield make
    with psycopg.connect(DATABASE, autocommit=True) as admin:
        for name in made:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("encoding", ["LATIN1", "SQL_ASCII"])
def test_the_start_refuses_a_database_that_is_not_utf8(in_encoding, schema, encoding):
    """A name, a label or an entry's text from any script is kept only by a UTF8 database: in another, PostgreSQL
    refuses the characters its encoding lacks and the action that holds one would fail to be kept."""
    url = in_encoding(encoding)
    with closing(Database(url, schema=schema)) as db:
        for call in (lambda: check_schema(db), lambda: migrate(db)):
            refused = _raised(call)
            assert type(refused) is DatabaseError and "server_encoding" in str(refused)
            assert "UTF8" in str(refused) and encoding in str(refused) and PASSWORD not in _shown(refused)
    with psycopg.connect(url) as conn:  # not even the schema was made there
        assert conn.execute("SELECT COUNT(*) FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() == (0,)


def test_a_utf8_database_is_not_refused_for_its_encoding(db):
    with db.connection() as conn:
        assert conn.execute("SHOW server_encoding").fetchone() == ("UTF8",)
    check_schema(db)
