"""`latet server` on the server's machine: init makes the master key and the first administration token once, migrate
and run say what the schema needs, ready asks this machine's instance with the server's own name, and certificate
makes or shows a certificate's fingerprint. Without the `server` extra, they say which one to install."""

import base64
import contextlib
import datetime
import gc
import inspect
import io
import ipaddress
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from latet import __version__
from latet.engine.reviewer import certificate as certificates
from latet.server import instance
from latet.server.certificate import make_certificate
from latet.server.db import DatabaseError
from latet.server.logs import saved
from latet.server.master import MasterKey
from latet.storage import folders
from latet.storage.folders import ADMINISTRATORS, OWNER_RIGHTS, SYSTEM, Grant

from .conftest import run_cli
from .fake_latet_server import FakeLatetServer

URL = "https://latet.acme.lan"
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
ME, OTHER, SERVICE, ALL, READ = (
    "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", "S-1-5-80-1-2-3-4-5", 0x1F01FF, 0x1200A9,
)  # fmt: skip


@pytest.fixture(autouse=True)
def _logging_given_back(monkeypatch):
    """The commands set the process's logging up (``configure_logging``): given back as it was after each test, which
    shares the process with the others. And no master key, nor asyncio's debug mode, of the computer's; nor the agent
    these tests may run in (`latet server init` refuses one)."""
    from latet.cli.agent import MARKERS

    for name in ("LATET_MASTER_KEY", "PYTHONASYNCIODEBUG", "LATET_SERVER_CONFIG", "AGENT"):
        monkeypatch.delenv(name, raising=False)
    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)
    with saved():
        yield


class Tokens:
    """E0b's ``AdminTokens`` as `latet server init` uses it: ``issued`` (its triple), ``list`` and ``check``."""

    def __init__(self, days=90):
        self.rows = {}
        self.days = days

    def issued(self, label, *, days=0, never=False, by=None, not_after=None, conn=None):
        n = len(self.rows)
        token_id, secret = "a_" + chr(ord("a") + n) * 10, f"latet_adm_{n}"
        expires = None if never else time.time() + (days or self.days) * 86400
        self.rows[token_id] = SimpleNamespace(
            secret=secret, expires=expires, issued_by=by, not_after=not_after, conn=conn, label=label
        )
        return token_id, secret, expires

    def list(self, *, conn=None):
        self.listed_in = conn
        return [SimpleNamespace(id=token_id, expires=row.expires) for token_id, row in self.rows.items()]

    def check(self, secret):
        return next((token_id for token_id, row in self.rows.items() if row.secret == secret), None)


class Audit:
    def __init__(self):
        self.entries = []

    def record(self, actor, action, target=None, details=None, address=None, *, conn=None):
        self.entries.append(SimpleNamespace(actor=actor, action=action, target=target, details=details, conn=conn))


class Connection:
    """A transaction's connection: what was executed on it."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))


class Transactions:
    """A database's transactions, as ``first_admin_token`` uses them: each yields a connection of its own."""

    schema = "latet"

    def __init__(self):
        self.made = []

    @contextlib.contextmanager
    def transaction(self):
        self.made.append(Connection())
        yield self.made[-1]


def _server():
    closed = []
    return SimpleNamespace(
        settings=SimpleNamespace(url=URL, admin_token_days=90), admin=Tokens(), audit=Audit(), db=Transactions(),
        closed=closed, close=lambda: closed.append(1),
    )  # fmt: skip


def _settings(tmp_path, **more):
    found = {
        "url": URL, "master_key_file": None, "certificate": None, "key": None, "host": "0.0.0.0", "port": 5283,
        "forwarded_allow_ips": "127.0.0.1", "admin_token_days": 90, "log_format": "text", "log_level": "info",
        "max_request_mb": 32, "database_private_network": False, "valkey_private_network": False,
        "valkey_unauthenticated": False, "database_url": "postgresql://emily@127.0.0.1:9/latet",
        "database_connect_timeout": 5, "metrics_token_file": None, "metrics_host": "127.0.0.1", "metrics_port": 7283,
    }  # fmt: skip
    return SimpleNamespace(**{**found, **more})


def _on_the_servers_machine(monkeypatch, settings):
    monkeypatch.setattr(instance, "require_extra", lambda: None)
    monkeypatch.setattr(instance, "settings", lambda config=None: settings)


class Kept:
    """A database as ``ensure_master_key`` asks it whether an instance started on it: whether it has the table of the
    master key's check value, and whether that holds its row."""

    def __init__(self, table=False, row=False):
        self.table, self.row = table, row

    @contextlib.contextmanager
    def connection(self):
        def execute(query, *args):
            found = ("master_check",) if self.table else (None,)
            if "FROM master_check" in query:
                found = (1,) if self.row else None
            return SimpleNamespace(fetchone=lambda: found)

        yield SimpleNamespace(execute=execute)


# --- the master key ---------------------------------------------------------------------------------------------------


def test_a_master_key_file_is_written_once_and_a_missing_one_is_printed_to_keep(tmp_path, monkeypatch):
    named = _settings(tmp_path, master_key_file=tmp_path / "master.key")
    assert instance.ensure_master_key(named, Kept()) == tmp_path / "master.key"
    assert len(base64.b64decode(named.master_key_file.read_text().strip())) == 32
    assert instance.ensure_master_key(named, Kept()) is None  # there now
    printed = instance.ensure_master_key(_settings(tmp_path), Kept())
    assert isinstance(printed, str) and len(base64.b64decode(printed)) == 32
    monkeypatch.setenv("LATET_MASTER_KEY", printed)
    assert instance.ensure_master_key(_settings(tmp_path), Kept()) is None


@pytest.mark.parametrize("named", [True, False], ids=["a file named", "nothing named"])
def test_no_master_key_is_made_for_a_database_an_instance_started_on(tmp_path, named):
    """A key made for a database started with another opens nothing it seals: none is written, none is printed."""
    settings = _settings(tmp_path, master_key_file=tmp_path / "master.key" if named else None)
    with pytest.raises(RuntimeError, match="started with another master key") as refused:
        instance.ensure_master_key(settings, Kept(table=True, row=True))
    assert "LATET_MASTER_KEY_FILE" in str(refused.value) and "or in LATET_MASTER_KEY" in str(refused.value)
    assert list(tmp_path.iterdir()) == []
    # Migrated but never started (the table, no row): the database was sealed with no key yet, and one is made.
    assert instance.ensure_master_key(settings, Kept(table=True, row=False)) is not None


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX", "OPENCODE"])
def test_init_refuses_an_agent_before_anything_prints_a_secret(tmp_path, monkeypatch, marker):
    """`latet server init` prints a new master key when nothing names one, and an administration token: an agent is
    refused before the settings are read, as it is for a service's installation."""
    monkeypatch.setattr(instance, "require_extra", lambda: pytest.fail("the settings were read"))
    monkeypatch.setattr(instance, "settings", lambda config=None: pytest.fail("the settings were read"))
    monkeypatch.setenv(marker, "1")
    for asked in (("server", "init"), ("server", "init", "--new-admin-token", "--never")):
        done = run_cli(*asked)
        assert done.exit_code == 2 and "An agent cannot initialize a server" in done.output, done.output
        assert "latet_adm_" not in done.output


def test_init_asks_the_database_before_it_makes_a_master_key(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path, master_key_file=tmp_path / "master.key"))
    monkeypatch.setattr(instance, "_database", lambda settings, database: (Kept(table=True, row=True), False))
    monkeypatch.setattr(instance, "migrate_schema", lambda *a, **k: pytest.fail("migrated without its master key"))
    done = run_cli("server", "init")
    assert done.exit_code == 2 and "started with another master key" in done.output, done.output
    assert not (tmp_path / "master.key").exists() and "A new master key" not in done.output


def test_a_master_key_given_twice_is_said_before_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_MASTER_KEY", MasterKey.generate())
    named = _settings(tmp_path, master_key_file=tmp_path / "master.key")
    with pytest.raises(RuntimeError, match="given twice"):
        instance.ensure_master_key(named)
    assert not named.master_key_file.exists()


def test_the_master_keys_folder_is_made_the_persons_alone(tmp_path):
    named = _settings(tmp_path, master_key_file=tmp_path / "secrets" / "master.key")
    assert instance.ensure_master_key(named, Kept()) == named.master_key_file
    assert folders.private_folder_problem(named.master_key_file.parent) is None
    if os.name != "nt":  # made 0700 whatever the umask, and the key 0600
        assert stat.S_IMODE(named.master_key_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(named.master_key_file.stat().st_mode) == 0o600


def test_the_master_key_is_never_written_in_a_folder_others_may_open(tmp_path, monkeypatch):
    said = f"{tmp_path} is open to other users of this computer: make it yours alone"
    monkeypatch.setattr(folders, "private_folder_problem", lambda folder: said)
    named = _settings(tmp_path, master_key_file=tmp_path / "master.key")
    generated = []
    real = MasterKey.generate
    monkeypatch.setattr(MasterKey, "generate", staticmethod(lambda: generated.append(real()) or generated[-1]))
    with pytest.raises(PermissionError, match="open to other users") as refused:
        instance.ensure_master_key(named, Kept())
    assert not named.master_key_file.exists() and list(tmp_path.iterdir()) == []
    assert all(key not in str(refused.value) for key in generated)


@POSIX
def test_on_posix_the_master_key_is_never_written_in_a_folder_others_may_write(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    folder.chmod(0o777)
    named = _settings(tmp_path, master_key_file=folder / "master.key")
    with pytest.raises(PermissionError, match="chmod 700"):
        instance.ensure_master_key(named, Kept())
    assert list(folder.iterdir()) == []


@WINDOWS
def test_on_windows_the_master_key_is_never_written_in_a_folder_another_account_may_open(tmp_path, monkeypatch):
    me = folders.my_sid()
    grants = [Grant(me, ALL, False), Grant(SYSTEM, ALL, False), Grant("S-1-5-32-545", READ, False)]  # and Users
    monkeypatch.setattr(folders, "folder_grants", lambda folder: (me, grants))
    named = _settings(tmp_path, master_key_file=tmp_path / "master.key")
    with pytest.raises(PermissionError, match="icacls"):
        instance.ensure_master_key(named, Kept())
    assert list(tmp_path.iterdir()) == []


@WINDOWS
def test_on_windows_the_master_key_is_readable_by_the_person_system_and_the_administrators_alone(tmp_path):
    folder = tmp_path / "secrets"
    named = _settings(tmp_path, master_key_file=folder / "master.key")
    instance.ensure_master_key(named, Kept())
    owner, grants = folders.folder_grants(named.master_key_file)
    assert owner in (folders.my_sid(), SYSTEM, ADMINISTRATORS)
    assert grants and {grant.sid for grant in grants} <= {folders.my_sid(), SYSTEM, ADMINISTRATORS, OWNER_RIGHTS}
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)R"], check=True, capture_output=True)
    named.master_key_file.unlink()
    with pytest.raises(PermissionError, match="icacls"):
        instance.ensure_master_key(named, Kept())  # Users may read the folder now
    assert not named.master_key_file.exists()


@WINDOWS
def test_on_windows_the_master_keys_new_folder_is_private_inside_one_that_is_not(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["icacls", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)R"], check=True, capture_output=True)
    named = _settings(tmp_path, master_key_file=shared / "secrets" / "master.key")
    assert instance.ensure_master_key(named, Kept()) == named.master_key_file  # made private, not left to inherit
    assert "S-1-5-32-545" not in {grant.sid for grant in folders.folder_grants(named.master_key_file)[1]}


def test_no_folder_check_lets_in_an_account_its_caller_names():
    """No hook lets another account into the folder of a secret (T12.29): beside the person, SYSTEM and the
    Administrators, nobody; the Windows service's own account comes with its own task and pre-read (Task 14)."""
    checks = (
        folders.private_folder_problem,
        folders._windows_folder_problem,
        make_certificate,
        instance.ensure_master_key,
    )
    assert [check.__name__ for check in checks if "also" in inspect.signature(check).parameters] == []


# --- the first administration token -----------------------------------------------------------------------------------


def test_the_first_administration_token_is_issued_once_and_recorded():
    server = _server()
    token_id, secret = instance.first_admin_token(server)
    assert server.admin.check(secret) == token_id
    assert [(e.actor, e.action, e.target) for e in server.audit.entries] == [
        ("server", "server.initialized", None), ("server", "admin-token.issued", token_id),
    ]  # fmt: skip
    assert server.audit.entries[1].details == {}  # never the label
    row = server.admin.rows[token_id]
    assert row.issued_by == "server" and row.not_after is None  # the root origin: bounded by no token
    assert {entry.conn for entry in server.audit.entries} == {row.conn} and row.conn is server.db.made[0]
    # The list read in the issuing transaction, under the lock of the first token taken first.
    assert server.admin.listed_in is row.conn and "pg_advisory_xact_lock" in row.conn.executed[0][0]
    assert instance.first_admin_token(server) is None
    again = instance.first_admin_token(server, again=True)
    assert again is not None and again[1] != secret
    assert (server.audit.entries[-1].action, server.audit.entries[-1].details) == (
        "admin-token.issued",
        {"again": True},
    )
    assert [e.action for e in server.audit.entries].count("server.initialized") == 1


def test_a_token_listed_is_one_issued_before_whatever_the_log_still_holds():
    server = _server()
    server.admin.issued("an administrator")  # issued outside init, or its entries pruned since: the log holds none
    assert server.audit.entries == []
    assert instance.first_admin_token(server) is None
    assert len(server.admin.rows) == 1 and server.audit.entries == []


def test_a_first_token_lasts_for_good_or_the_days_asked():
    server = _server()
    token_id, _ = instance.first_admin_token(server, never=True)
    assert server.admin.rows[token_id].expires is None
    token_id, _ = instance.first_admin_token(server, again=True, days=30)
    assert server.admin.rows[token_id].expires == pytest.approx(time.time() + 30 * 86400, abs=60)


def test_init_prints_the_token_once_and_says_what_follows(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "ensure_master_key", lambda settings: tmp_path / "master.key")
    monkeypatch.setattr(instance, "migrate_schema", lambda settings, database=None: ["001_identities"])
    server = _server()
    monkeypatch.setattr("latet.server.app.open_server", lambda settings: server)
    done = run_cli("server", "init")
    assert done.exit_code == 0, done.output
    assert "latet_adm_0" in done.output and "master.key" in done.output and "latet server run" in done.output
    assert (
        "90 days" in done.output and "--never" in done.output
    )  # when the token expires, and how to make one that does not
    again = run_cli("server", "init")
    assert again.exit_code == 0 and "latet_adm_" not in again.output and "--new-admin-token" in again.output
    assert server.closed == [1, 1]


def test_init_makes_a_first_token_for_good_or_for_the_days_asked(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "ensure_master_key", lambda settings: None)
    monkeypatch.setattr(instance, "migrate_schema", lambda settings, database=None: [])
    monkeypatch.setattr("latet.server.app.open_server", lambda settings: _server())
    asked = []

    def first(server, *, again=False, days=0, never=False):
        asked.append((again, days, never))
        return "a_aaaaaaaaaa", "latet_adm_x"

    monkeypatch.setattr(instance, "first_admin_token", first)
    done = run_cli("server", "init", "--never")
    assert done.exit_code == 0, done.output
    assert asked == [(False, 0, True)] and "does not expire" in done.output
    done = run_cli("server", "init", "--new-admin-token", "--days", "30")
    assert done.exit_code == 0 and asked[-1] == (True, 30, False) and "30 days" in done.output
    assert run_cli("server", "init", "--days", "30", "--never").exit_code == 2
    for days in ("0", "3651"):
        assert run_cli("server", "init", "--days", days).exit_code == 2
    assert len(asked) == 2


def test_init_refuses_days_or_never_where_they_would_make_no_token(tmp_path, monkeypatch):
    """A token was issued before: init issues none, so a --days or a --never would be dropped without a word. They are
    refused, saying they apply with --new-admin-token, which then makes the token asked for."""
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "ensure_master_key", lambda settings: None)
    monkeypatch.setattr(instance, "migrate_schema", lambda settings, database=None: [])
    server = _server()
    server.admin.issued("an administrator")
    monkeypatch.setattr("latet.server.app.open_server", lambda settings: server)
    for flags in (["--never"], ["--days", "30"]):
        done = run_cli("server", "init", *flags)
        assert done.exit_code == 2 and "apply with --new-admin-token" in done.output, done.output
        assert "latet_adm_" not in done.output and "makes another with it" not in done.output
    assert len(server.admin.rows) == 1
    again = run_cli("server", "init")  # with neither, as before: issued before, said, exit 0
    assert again.exit_code == 0 and "issued before" in again.output
    made = run_cli("server", "init", "--new-admin-token", "--never")
    assert made.exit_code == 0 and "does not expire" in made.output and len(server.admin.rows) == 2


def test_migrate_says_a_schema_newer_than_this_latet_and_never_that_it_is_up_to_date(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "migrate_schema", lambda settings, database=None: [])  # names it does not know
    newer = (
        "the database's schema latet is newer than this Latet (latet.server.migrations:999_next): upgrade Latet here"
    )
    monkeypatch.setattr(instance, "schema_state", lambda settings, database=None: newer)
    done = run_cli("server", "migrate")
    assert done.exit_code == 1 and newer in done.output and "up to date" not in done.output, done.output
    monkeypatch.setattr(instance, "schema_state", lambda settings, database=None: None)
    done = run_cli("server", "migrate")
    assert done.exit_code == 0 and "up to date" in done.output


def test_init_without_a_master_key_prints_one_and_stops(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "ensure_master_key", lambda settings: "a-new-key-in-base64")
    monkeypatch.setattr(instance, "migrate_schema", lambda *a, **k: pytest.fail("migrated without a master key"))
    done = run_cli("server", "init")
    assert done.exit_code == 2 and "a-new-key-in-base64" in done.output and "LATET_MASTER_KEY_FILE" in done.output


def test_init_says_a_master_key_it_may_not_write_and_stops(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path, master_key_file=tmp_path / "master.key"))
    monkeypatch.setattr(instance, "_database", lambda settings, database: (Kept(), False))  # never started
    monkeypatch.setattr(folders, "private_folder_problem", lambda folder: f"{folder} is open to others")
    monkeypatch.setattr(instance, "migrate_schema", lambda *a, **k: pytest.fail("migrated without a master key"))
    done = run_cli("server", "init")
    assert done.exit_code == 2 and "is open to others" in done.output and "Traceback" not in done.output
    assert not (tmp_path / "master.key").exists()


def test_init_and_run_say_what_stops_them_in_fixed_words(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "ensure_master_key", lambda settings: None)

    def unreachable(settings, database=None):
        raise DatabaseError("the connection to PostgreSQL failed (OperationalError)")

    monkeypatch.setattr(instance, "migrate_schema", unreachable)
    done = run_cli("server", "init")
    assert done.exit_code == 2 and "the connection to PostgreSQL failed" in done.output, done.output
    monkeypatch.setattr(instance, "migrate_schema", lambda settings, database=None: [])

    def refused(settings):
        raise RuntimeError("Valkey (valkey_url) refused this instance or did not answer in time (ConnectionError)")

    monkeypatch.setattr("latet.server.app.open_server", refused)
    done = run_cli("server", "init")
    assert done.exit_code == 2 and "Valkey (valkey_url) refused" in done.output and "Traceback" not in done.output


def _handlers():
    """The root logger's handlers, their filters and their formatter, by their types' names."""
    return [
        (type(h).__name__, [type(f).__name__ for f in h.filters], type(h.formatter).__name__)
        for h in logging.getLogger().handlers
    ]


@pytest.mark.parametrize("command", ["init", "migrate"])
def test_init_and_migrate_set_logging_up_before_they_open_the_database(tmp_path, monkeypatch, command):
    """A library's record while the pool opens (psycopg_pool's, naming the database's host and user) meets the server's
    gate and formatter, never an unconfigured standard error; and so does anything said while the settings are read."""
    key = tmp_path / "master.key"
    key.write_text(MasterKey.generate(), "utf-8")
    settings = _settings(tmp_path, master_key_file=key, log_format="json")
    monkeypatch.setattr(instance, "require_extra", lambda: None)
    seen, opened = [], []

    def read(config=None):
        seen.append(_handlers())
        return settings

    monkeypatch.setattr(instance, "settings", read)

    class Opened:
        def __init__(self, *args, **kwargs):
            opened.append((args, kwargs))
            seen.append(_handlers())
            logging.getLogger("psycopg_pool").warning("error connecting to %s", "postgresql://emily@db.acme.lan/latet")
            raise DatabaseError("the connection to PostgreSQL failed (OperationalError)")

    monkeypatch.setattr("latet.server.db.Database", Opened)
    done = run_cli("server", command)
    assert done.exit_code == 2 and "the connection to PostgreSQL failed" in done.output, done.output
    # Text from the first line on, then as the settings say (JSON here) by the time the database is opened.
    assert [handlers for handlers in seen if ("_Writer", ["_Gate"], "TextFormatter") in handlers] == seen[:1]
    assert ("_Writer", ["_Gate"], "JsonFormatter") in seen[1]
    assert "emily@" not in done.output
    # The server's own schema, and the time its settings give a new connection.
    assert opened == [((settings.database_url,), {"schema": "latet", "connect_timeout": 5})]


def test_run_sets_logging_up_before_it_reads_the_settings(tmp_path, monkeypatch):
    seen = []

    def read(config=None):
        seen.append(_handlers())
        return _settings(tmp_path)

    def stopped(settings):
        raise ValueError("stopped here")

    monkeypatch.setattr(instance, "require_extra", lambda: None)
    monkeypatch.setattr(instance, "settings", read)
    monkeypatch.setattr(instance, "make_instance", stopped)
    done = run_cli("server", "run")
    assert done.exit_code == 2 and "stopped here" in done.output
    assert len(seen) == 1 and ("_Writer", ["_Gate"], "TextFormatter") in seen[0]


def test_what_the_databases_driver_says_is_said_by_its_type_alone(tmp_path, monkeypatch):
    """libpq's words name the database's host and user: what init and migrate raise holds the error's type, and never
    the words, not even as the context a traceback would show."""
    import psycopg

    said = 'connection to server at "db.acme.lan", port 5432 failed: password authentication failed for user "emily"'

    class Refusing:
        def list(self, *, conn=None):
            raise psycopg.OperationalError(said)

    server = _server()
    server.admin = Refusing()
    with pytest.raises(DatabaseError) as issued:
        instance.first_admin_token(server)
    key = tmp_path / "master.key"
    key.write_text(MasterKey.generate(), "utf-8")

    class Unreachable:
        schema = "latet"

        @contextlib.contextmanager
        def connection(self):
            raise psycopg.OperationalError(said)
            yield

    with pytest.raises(DatabaseError) as asked:
        instance.migrate_schema(_settings(tmp_path, master_key_file=key), Unreachable())

    class Unrecorded(Unreachable):  # it keeps a check value, migrates, and refuses the entry
        @contextlib.contextmanager
        def connection(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: ("master_check",)))

        @contextlib.contextmanager
        def transaction(self):
            raise psycopg.OperationalError(said)
            yield

    monkeypatch.setattr("latet.server.master.check_master_key", lambda db, master: None)
    monkeypatch.setattr("latet.server.db.migrate", lambda db: ["latet.server.migrations:003_next"])
    with pytest.raises(DatabaseError) as recorded:
        instance.migrate_schema(_settings(tmp_path, master_key_file=key), Unrecorded())
    for raised in (issued.value, asked.value, recorded.value):
        assert "OperationalError" in str(raised) and "emily" not in str(raised) and "db.acme.lan" not in str(raised)
        assert raised.__context__ is None and raised.__cause__ is None


# --- the settings -----------------------------------------------------------------------------------------------------


def test_the_administration_logs_retention_is_a_start_up_setting(monkeypatch):
    from latet.server.settings import ServerSettings, load_server_settings

    # This machine's addresses: E0b refuses an off-machine PostgreSQL without TLS and a Valkey without a password.
    base = {
        "url": URL,
        "database_url": "postgresql://latet@127.0.0.1:5432/latet",
        "valkey_url": "redis://127.0.0.1:6379/0",
    }
    assert ServerSettings(**base).audit_retention_days == 365
    for days in (7, 29, 3651):
        with pytest.raises(ValueError, match="audit_retention_days"):
            ServerSettings(**base, audit_retention_days=days)
    for key, value in {"URL": URL, "DATABASE_URL": base["database_url"], "VALKEY_URL": base["valkey_url"]}.items():
        monkeypatch.setenv(f"LATET_SERVER_{key}", value)
    monkeypatch.setenv("LATET_SERVER_AUDIT_RETENTION_DAYS", "730")
    assert load_server_settings().audit_retention_days == 730


def test_every_setting_of_a_server_says_what_it_is_and_has_its_variable():
    from latet.server.settings import _VARIABLES, ServerSettings

    assert [name for name, field in ServerSettings.model_fields.items() if not field.description] == []
    assert set(_VARIABLES) == set(ServerSettings.model_fields)


# --- run --------------------------------------------------------------------------------------------------------------


def test_run_refuses_a_schema_that_is_not_the_codes_and_says_which_command(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))

    def mismatch(settings):  # as E0b's open_server refuses an older schema (its SchemaMismatch, or a RuntimeError)
        raise RuntimeError("the database's tables are older than this Latet: run `latet server migrate`")

    monkeypatch.setattr(instance, "make_instance", mismatch)
    done = run_cli("server", "run")
    assert done.exit_code == 2 and "latet server migrate" in done.output


def test_run_refuses_asyncios_debug_mode_before_it_opens_anything(tmp_path, monkeypatch):
    _on_the_servers_machine(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(instance, "make_instance", lambda *a, **k: pytest.fail("started in asyncio's debug mode"))
    monkeypatch.setenv("PYTHONASYNCIODEBUG", "1")
    done = run_cli("server", "run")
    assert done.exit_code == 2 and "PYTHONASYNCIODEBUG" in done.output and "-X dev" in done.output


def test_run_refuses_pythons_development_mode():
    code = textwrap.dedent(
        """
        import sys
        from typer.testing import CliRunner
        import latet.cli.admin
        from latet.cli.main import app
        from latet.server import instance
        assert sys.flags.dev_mode
        def started(*args, **kwargs):
            raise AssertionError("started in development mode")
        instance.make_instance = started
        result = CliRunner().invoke(app, ["server", "run"])
        assert result.exit_code == 2 and "-X dev" in result.output, result.output
        """
    )
    clean = {key: value for key, value in os.environ.items() if key != "PYTHONASYNCIODEBUG"}
    done = subprocess.run(
        [sys.executable, "-X", "dev", "-c", code], capture_output=True, text=True, timeout=120, check=False, env=clean
    )
    assert done.returncode == 0, done.stderr + done.stdout


class _Stub:
    """uvicorn's server as an instance runs it: ``run`` returns, or raises what it is given; and how many objects were
    frozen out of the collections while it ran."""

    def __init__(self, raised=None):
        self.raised, self.ran, self.frozen, self.sockets = raised, 0, 0, "not run"

    def run(self, sockets=None):
        self.ran += 1
        self.frozen = gc.get_freeze_count()
        self.sockets = sockets
        if self.raised is not None:
            raise self.raised


def _opened(settings, closed):
    return SimpleNamespace(settings=settings, instance="0123456789ab", close=lambda: closed.append(1))


@pytest.mark.parametrize("raised", [None, KeyboardInterrupt(), SystemExit(1)], ids=["returns", "interrupted", "exits"])
def test_an_instance_closes_its_server_once_whatever_ends_its_run(tmp_path, raised):
    closed = []
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    found = instance.Instance(_opened(_settings(tmp_path), closed), _Stub(raised))
    with contextlib.nullcontext() if raised is None else pytest.raises(type(raised)):
        found.run()
    assert closed == [1] and found.uvicorn.ran == 1
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers  # the process's own, given back
    # What was open when it began, kept out of the full collections while it ran (a vault's reading holds the event loop
    # half as long so), and given back to them after.
    assert found.uvicorn.frozen > 0 and gc.get_freeze_count() == 0


@pytest.mark.parametrize("tls", [True, False], ids=["https", "http"])
@pytest.mark.parametrize("metrics", [True, False], ids=["metrics", "no metrics"])
def test_an_instance_says_who_it_is_where_it_listens_and_whether_tls_is_its_own(tmp_path, caplog, tls, metrics):
    pair = {"certificate": tmp_path / "server.crt", "key": tmp_path / "server.key"} if tls else {}
    settings = _settings(tmp_path, port=8443, metrics_host="10.0.5.10", metrics_port=7283, **pair)
    own, theirs = socket.socket(), [socket.socket()] if metrics else []
    found = instance.Instance(_opened(settings, []), _Stub(), own=[own], metrics=theirs)
    with caplog.at_level(logging.INFO, logger="latet.server.instance"):
        found.run()
    over = "over https (TLS at the instance)" if tls else "over http (TLS before it)"
    said = [record.getMessage() for record in caplog.records if record.name == "latet.server.instance"]
    where = "its metrics on 10.0.5.10 port 7283" if metrics else "no metrics socket (metrics_token_file is not set)"
    assert said == [f"instance 0123456789ab of Latet {__version__} starts on 0.0.0.0 port 8443, {over}; {where}"]
    assert found.uvicorn.sockets == [own, *theirs]  # one uvicorn server serves them all
    assert all(sock.fileno() == -1 for sock in [own, *theirs])  # and they are let go once its run ended


def test_a_stop_signal_ends_the_run_gracefully_and_reaches_the_servers_close():
    """SIGTERM, as Docker, systemd and Kubernetes stop a server: uvicorn shuts down gracefully, then raises the signal
    again, which would end the process before the server is closed; the instance's run takes it and closes it."""
    code = textwrap.dedent(
        """
        import signal, socket, threading, time
        from types import SimpleNamespace
        from starlette.applications import Starlette
        from latet.cli.main import uvicorn_server
        from latet.server.instance import Instance

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        served = uvicorn_server(Starlette(), "127.0.0.1", port, "warning", log_config=None)
        settings = SimpleNamespace(host="127.0.0.1", port=port, certificate=None)
        server = SimpleNamespace(settings=settings, instance="probe", close=lambda: print("closed", flush=True))

        def stop():
            deadline = time.monotonic() + 30
            while not served.started and time.monotonic() < deadline:
                time.sleep(0.05)
            print("serving", flush=True)
            signal.raise_signal(signal.SIGTERM)

        threading.Thread(target=stop, daemon=True).start()
        Instance(server, served).run()
        print("returned", flush=True)
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert done.stdout.split() == ["serving", "closed", "returned"], done.stdout + done.stderr
    assert done.returncode == 0


def test_an_instance_listens_with_the_settings_certificate_and_logs_where_it_is_told(tmp_path, monkeypatch):
    made, logged, loaded, closed, sink = [], [], [], [], io.StringIO()
    port = _free_port()
    settings = _settings(
        tmp_path, log_format="json", log_level="info", forwarded_allow_ips="10.0.5.2", max_request_mb=8,
        host="127.0.0.1", port=port,
    )  # fmt: skip
    settings.certificate, settings.key = tmp_path / "server.crt", tmp_path / "server.key"
    monkeypatch.setattr(
        "latet.server.logs.configure_logging", lambda fmt, level, *, stream=None: logged.append((fmt, level, stream))
    )
    opened = _opened(settings, closed)
    monkeypatch.setattr("latet.server.app.open_server", lambda s: opened)
    monkeypatch.setattr("latet.server.operations.server_app", lambda server: "the app")

    def served(app, host, port, log_level="info", log_config="default", **more):
        made.append((app, host, port, log_level, log_config, more))
        return SimpleNamespace(config=SimpleNamespace(load=lambda: loaded.append(app)))

    monkeypatch.setattr("latet.cli.main.uvicorn_server", served)
    found = instance.make_instance(settings, stream=sink)  # the Windows service's log file (Task 14); stdout otherwise
    for sock in found.own or []:
        sock.close()
    tls = (settings.certificate, settings.key)
    assert made == [
        ("the app", "127.0.0.1", port, None, None, {
            "tls": tls, "forwarded_allow_ips": "10.0.5.2", "ws_max_size": 8 * 1024 * 1024,
            "timeout_graceful_shutdown": 30, "access_log": False,
        }),
    ]  # fmt: skip
    assert logged == [("json", "info", sink)]
    assert loaded == ["the app"]  # the certificate and the key loaded before the run, where a refusal is caught
    assert found.server is opened and closed == []


def test_an_instance_says_at_its_start_which_opt_outs_it_trusts(tmp_path, monkeypatch, caplog):
    settings = _settings(
        tmp_path, database_private_network=True, valkey_unauthenticated=True, host="127.0.0.1", port=_free_port()
    )
    monkeypatch.setattr("latet.server.logs.configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("latet.server.app.open_server", lambda s: _opened(s, []))
    monkeypatch.setattr("latet.server.operations.server_app", lambda server: "the app")
    monkeypatch.setattr(
        "latet.cli.main.uvicorn_server", lambda *a, **k: SimpleNamespace(config=SimpleNamespace(load=lambda: None))
    )
    with caplog.at_level(logging.WARNING, logger="latet"):
        found = instance.make_instance(settings)
    for sock in found.own or []:
        sock.close()
    said = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("database_private_network" in line for line in said)
    assert any("valkey_unauthenticated" in line for line in said)
    assert not any("valkey_private_network" in line for line in said)


def test_an_instance_is_never_made_in_asyncios_debug_mode(tmp_path, monkeypatch):
    """Whoever makes it: `latet server run` refuses first, and so does make_instance, which the Windows service's host
    calls itself (Task 14)."""
    monkeypatch.setattr("latet.server.logs.configure_logging", lambda *a, **k: pytest.fail("set up in debug mode"))
    monkeypatch.setattr("latet.server.app.open_server", lambda settings: pytest.fail("opened in debug mode"))
    monkeypatch.setenv("PYTHONASYNCIODEBUG", "1")
    with pytest.raises(RuntimeError, match="PYTHONASYNCIODEBUG"):
        instance.make_instance(_settings(tmp_path))


def test_a_certificate_or_a_key_the_instance_cannot_load_stops_it_in_fixed_words(tmp_path, monkeypatch):
    settings = _settings(tmp_path, certificate=tmp_path / "server.crt", key=tmp_path / "server.key")
    settings.certificate.write_text("not a certificate", "utf-8")
    settings.key.write_text("-----BEGIN PRIVATE KEY-----\nTHEKEYSOWNBYTES\n-----END PRIVATE KEY-----\n", "utf-8")
    _on_the_servers_machine(monkeypatch, settings)
    closed = []
    monkeypatch.setattr("latet.server.app.open_server", lambda s: _opened(s, closed))
    monkeypatch.setattr("latet.server.operations.server_app", lambda server: Starlette())
    done = run_cli("server", "run")
    assert done.exit_code == 2, done.output
    assert "certificate" in done.output and "SSLError" in done.output
    assert "THEKEYSOWNBYTES" not in done.output and "Traceback" not in done.output
    assert closed == [1]  # the server it opened, closed


# --- the instance's two sockets (T10.12, T10.23) ----------------------------------------------------------------------

METRICS_TOKEN = "metrics-" + "z" * 32


def _served_settings(tmp_path, *, metrics=True):
    """An instance's settings on this machine's loopback and free ports, its metrics on or off."""
    token = tmp_path / "metrics.token"
    token.write_text(METRICS_TOKEN, "utf-8")
    return _settings(
        tmp_path, host="127.0.0.1", port=_free_port(), metrics_host="127.0.0.1", metrics_port=_free_port(),
        metrics_token_file=token if metrics else None,
    )  # fmt: skip


def _operated(monkeypatch, settings, closed):
    """make_instance's server and app as stand-ins (its logging left as it is): an app answering "the app" to anything,
    behind the real Operations, which tells the two sockets apart."""
    from latet.server.machines import Limits
    from latet.server.metrics import Metrics
    from latet.server.operations import Operations

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"the app"})

    app.state = SimpleNamespace()
    server = SimpleNamespace(
        settings=settings, instance="0123456789ab", close=lambda: closed.append(1), master=MasterKey([b"\x05" * 32]),
        metrics=Metrics(), limits=Limits(), spaces=SimpleNamespace(count=lambda: 0),
        identities=SimpleNamespace(enabled_count=lambda: 1), db=SimpleNamespace(ping=lambda: True),
        bus=SimpleNamespace(ping=lambda: True, rejected=0), chain=SimpleNamespace(read=lambda: "unknown"),
        edition=SimpleNamespace(metrics=lambda metrics: None),
    )  # fmt: skip
    monkeypatch.setattr("latet.server.logs.configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("latet.server.app.open_server", lambda s: server)
    monkeypatch.setattr("latet.server.operations.server_app", lambda s: Operations(app, s))
    return server


def test_an_instance_serves_its_own_socket_and_the_metrics_socket_each_its_own(tmp_path, monkeypatch):
    import httpx

    closed = []
    settings = _served_settings(tmp_path)
    server = _operated(monkeypatch, settings, closed)
    found = instance.make_instance(settings)
    assert [sock.getsockname()[:2] for sock in found.own or []] == [("127.0.0.1", settings.port)]
    assert [sock.getsockname()[:2] for sock in found.metrics] == [("127.0.0.1", settings.metrics_port)]
    running = threading.Thread(target=found.run, daemon=True)  # its own thread: it hears no signal there
    running.start()
    try:
        deadline = time.monotonic() + 30
        while not found.uvicorn.started and time.monotonic() < deadline:
            time.sleep(0.02)
        mine = {"authorization": f"Bearer {METRICS_TOKEN}"}
        with httpx.Client(trust_env=False, timeout=10) as client:
            own = client.get(f"http://127.0.0.1:{settings.port}/_latet/metrics", headers=mine)
            metrics = client.get(f"http://127.0.0.1:{settings.metrics_port}/_latet/metrics", headers=mine)
            app_there = client.get(f"http://127.0.0.1:{settings.metrics_port}/", headers=mine)
    finally:
        found.uvicorn.should_exit = True
        running.join(timeout=30)
        server.metrics.close()
    assert own.text == "the app"  # the instance's own port never answers the metrics: it is the app's, as any path
    assert metrics.status_code == 200 and "latet_build_info" in metrics.text
    assert (app_there.status_code, app_there.json()) == (404, {"error": "there is no such route"})  # never the app's
    assert not running.is_alive() and closed == [1]
    assert all(sock.fileno() == -1 for sock in [*(found.own or []), *found.metrics])  # let go once the run ended


def test_with_metrics_off_no_metrics_socket_is_opened(tmp_path, monkeypatch):
    closed = []
    settings = _served_settings(tmp_path, metrics=False)
    server = _operated(monkeypatch, settings, closed)
    found = instance.make_instance(settings)
    try:
        assert found.metrics == [] and [sock.getsockname()[1] for sock in found.own or []] == [settings.port]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", settings.metrics_port))  # nothing holds the metrics' port
    finally:
        for sock in found.own or []:
            sock.close()
        server.metrics.close()


@pytest.mark.parametrize("taken", ["port", "metrics_port"])
def test_an_address_in_use_stops_the_start_in_fixed_words_with_the_server_closed(tmp_path, monkeypatch, taken):
    closed = []
    settings = _served_settings(tmp_path)
    server = _operated(monkeypatch, settings, closed)
    with socket.socket() as holder:  # another program, listening there
        holder.bind(("127.0.0.1", getattr(settings, taken)))
        holder.listen()
        with pytest.raises(instance.CannotListen) as refused:
            instance.make_instance(settings)
    server.metrics.close()
    words = str(refused.value)
    if taken == "port":
        assert "the instance's socket" in words and "host and port" in words and "metrics_host" not in words
    else:
        assert "the metrics socket" in words and "metrics_host and metrics_port" in words
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", settings.port))  # the instance's own, bound first, let go
    assert "in use" in words and "127.0.0.1" not in words and str(settings.port) not in words
    assert closed == [1]  # the server it opened, closed


@pytest.mark.parametrize("host", ["prometheus.lan", "a..b"], ids=["not resolved", "no name"])
def test_a_metrics_host_this_machine_does_not_know_stops_the_start_in_fixed_words(tmp_path, monkeypatch, host):
    real = socket.getaddrinfo

    def resolved(name, *args, **kwargs):  # no resolver asked: prometheus.lan is a name it does not know
        if name == "prometheus.lan":
            raise socket.gaierror(socket.EAI_NONAME, "the name is not known")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolved)
    closed = []
    settings = _served_settings(tmp_path)
    settings.metrics_host = host
    server = _operated(monkeypatch, settings, closed)
    with pytest.raises(instance.CannotListen) as refused:
        instance.make_instance(settings)
    server.metrics.close()
    words = str(refused.value)
    assert "metrics_host and metrics_port" in words and "no name or address this machine knows" in words
    assert host not in words and refused.value.__context__ is None and closed == [1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", settings.port))  # the instance's own, bound first, let go


def test_latet_server_run_says_an_address_in_use_and_stops(tmp_path, monkeypatch):
    def ran(self):
        raise AssertionError("an instance whose socket could not be bound ran")

    closed = []
    settings = _served_settings(tmp_path)
    server = _operated(monkeypatch, settings, closed)
    _on_the_servers_machine(monkeypatch, settings)
    monkeypatch.setattr(instance.Instance, "run", ran)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", settings.metrics_port))
        holder.listen()
        done = run_cli("server", "run")
    server.metrics.close()
    assert done.exit_code == 2 and "Not started: the metrics socket cannot be bound" in done.output, done.output
    assert "Traceback" not in done.output and closed == [1]


def test_a_socket_the_instance_bound_is_its_own_alone(tmp_path, monkeypatch):
    settings = _served_settings(tmp_path)
    server = _operated(monkeypatch, settings, [])
    found = instance.make_instance(settings)
    try:
        for sock in [*(found.own or []), *found.metrics]:
            if sys.platform == "win32":  # where SO_REUSEADDR would let another socket bind the same address and port
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE)
                with socket.socket() as thief:
                    thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with pytest.raises(OSError, match=r"WinError 100(13|48)"):  # access refused, or in use
                        thief.bind(sock.getsockname()[:2])
            else:  # as asyncio binds a server's
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    finally:
        for sock in [*(found.own or []), *found.metrics]:
            sock.close()
        server.metrics.close()


# --- ready ------------------------------------------------------------------------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ready_asks_this_machines_instance_with_the_servers_own_name(tmp_path):
    async def ready(request: Request) -> JSONResponse:
        known = request.headers.get("host") == "latet.acme.lan"
        return JSONResponse({"ready": known}, status_code=200 if known else 403)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/_latet/ready", ready)]), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    settings = _settings(tmp_path, port=port)
    try:
        assert instance.ready(settings) == (True, "ready")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    answered, why = instance.ready(settings)
    assert not answered and "nothing answers" in why


@contextlib.contextmanager
def _serving(app, tls=None):
    """``app`` on 127.0.0.1, over HTTPS with ``tls`` (a certificate and its key): its port, while it lasts."""
    port = _free_port()
    more = {} if tls is None else {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])}
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **more))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_ready_asks_an_instance_over_https_with_its_own_certificate_and_no_other(tmp_path):
    async def ready(request: Request) -> JSONResponse:
        return JSONResponse({"ready": True})

    crt, key, _ = make_certificate("https://127.0.0.1", tmp_path / "tls")
    other, _, _ = make_certificate("https://127.0.0.1", tmp_path / "other")
    with _serving(Starlette(routes=[Route("/_latet/ready", ready)]), tls=(crt, key)) as port:
        assert instance.ready(_settings(tmp_path, port=port, certificate=crt, key=key)) == (True, "ready")
        answered, why = instance.ready(_settings(tmp_path, port=port, certificate=other, key=key))
    assert not answered and "certificate" in why
    (tmp_path / "unreadable.crt").write_bytes(b"\xff\xfe not a certificate")
    answered, why = instance.ready(_settings(tmp_path, port=port, certificate=tmp_path / "unreadable.crt"))
    assert not answered and "cannot be read" in why


def _issued(folder, authority, authority_key):
    """A certificate for 127.0.0.1 that ``authority`` signed (a company's authority), CA:FALSE, and its key."""
    issuer = x509.load_pem_x509_certificate(authority.read_bytes())
    signer = serialization.load_pem_private_key(authority_key.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, folder.name)]))
        .issuer_name(issuer.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(signer, hashes.SHA256())
    )  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "server.key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return folder / "server.crt", folder / "server.key"


def test_ready_accepts_the_instances_own_certificate_and_nothing_else_its_chain_would_sign(tmp_path):
    async def ready(request: Request) -> JSONResponse:
        return JSONResponse({"ready": True})

    app = Starlette(routes=[Route("/_latet/ready", ready)])
    authority, authority_key = _authority(tmp_path / "acme")
    own, own_key = _issued(tmp_path / "own", authority, authority_key)
    chain = tmp_path / "chain.crt"  # the instance's certificate, then its authority's, as a company's are given
    chain.write_text(own.read_text() + authority.read_text(), "utf-8")
    with _serving(app, tls=(chain, own_key)) as port:
        assert instance.ready(_settings(tmp_path, port=port, certificate=chain)) == (True, "ready")
    other, other_key = _issued(tmp_path / "other", authority, authority_key)  # the same authority's, not the instance's
    with _serving(app, tls=(other, other_key)) as port:
        answered, why = instance.ready(_settings(tmp_path, port=port, certificate=chain))
    assert not answered and "certificate" in why


def _ready_app():
    async def ready(request: Request) -> JSONResponse:
        return JSONResponse({"ready": True})

    return Starlette(routes=[Route("/_latet/ready", ready)])


def test_ready_refuses_an_authority_named_as_the_instances_certificate(tmp_path):
    """T3.7 where ready pins: an authority named by `certificate` would have it take whatever the authority's key
    signs, here a certificate that is not the instance's."""
    authority, authority_key = _authority(tmp_path / "acme")
    signed, signed_key = _issued(tmp_path / "signed", authority, authority_key)
    with _serving(_ready_app(), tls=(signed, signed_key)) as port:
        answered, why = instance.ready(_settings(tmp_path, port=port, certificate=authority))
    assert not answered and "is an authority" in why, why


def test_ready_takes_the_presented_certificate_only_when_it_is_the_settings_own_byte_for_byte(tmp_path, monkeypatch):
    """Were an authority taken all the same (its check gone), what it signs is still not the instance's certificate:
    the certificate presented is compared with the one the settings name, byte for byte."""
    monkeypatch.setattr(certificates, "is_authority", lambda pem: False)
    authority, authority_key = _authority(tmp_path / "acme")
    signed, signed_key = _issued(tmp_path / "signed", authority, authority_key)
    with _serving(_ready_app(), tls=(signed, signed_key)) as port:
        answered, why = instance.ready(_settings(tmp_path, port=port, certificate=authority))
        assert (answered, "the certificate the settings name" in why) == (False, True), why
        assert instance.ready(_settings(tmp_path, port=port, certificate=signed)) == (True, "ready")


def test_ready_asks_an_instance_listening_on_every_address_on_this_machines_own(tmp_path, monkeypatch):
    asked = []

    class Connection:
        def __init__(self, host, port, timeout=None):
            asked.append(host)

        def request(self, *args, **kwargs):
            raise ConnectionRefusedError

        def close(self):
            pass

    monkeypatch.setattr(instance.http.client, "HTTPConnection", Connection)
    for listening in ("0.0.0.0", "::", "10.0.5.7"):
        assert instance.ready(_settings(tmp_path, host=listening))[0] is False
    assert asked == ["127.0.0.1", "::1", "10.0.5.7"]  # asyncio listens on IPv6 alone for ::


# --- certificate ------------------------------------------------------------------------------------------------------


def _authority(folder):
    """A self-signed certificate that is an authority (CA:TRUE, as `openssl req -x509` makes by default), and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Acme Root")])
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("127.0.0.1")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "ca.key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return folder / "ca.crt", folder / "ca.key"


def test_certificate_makes_one_and_shows_a_file_or_what_a_server_presents(computer, latet_server, tmp_path):
    made = run_cli("server", "certificate", "--make", "--url", URL, "--out", str(tmp_path / "certs"))
    assert made.exit_code == 0, made.output
    fingerprint = certificates.fingerprint_of((tmp_path / "certs" / "server.crt").read_text())
    # The one-time code is asked at a prompt, never written in a command (T4.5): other users read a command line.
    assert f"latet connect {URL} --fingerprint {fingerprint}" in made.output and "--code" not in made.output
    # Every agent takes it (the spike): the variables a connected computer gives each, and no agent that does not.
    assert "NODE_EXTRA_CA_CERTS" in made.output and "CODEX_CA_CERTIFICATE" in made.output
    assert fingerprint in run_cli("server", "certificate", str(tmp_path / "certs" / "server.crt")).output
    assert latet_server.fingerprint in run_cli("server", "certificate", "--url", latet_server.url).output
    nothing = run_cli("server", "certificate")
    assert nothing.exit_code == 2 and "--make" in nothing.output


def test_certificate_of_an_address_says_it_is_what_this_computer_sees_and_where_the_pin_is_read(
    computer, latet_server, tmp_path
):
    """What a server presents is what this computer sees at its address: a machine in between can put another in its
    place, so the fingerprint to pin is read from the certificate's file on the server's machine (T3.12). A file or a
    certificate just made says nothing of it."""
    shown = run_cli("server", "certificate", "--url", latet_server.url)
    assert shown.exit_code == 0 and latet_server.fingerprint in shown.output, shown.output
    words = " ".join(shown.output.split())
    assert "what this computer sees" in words and "a machine in between" in words
    assert "the certificate's file on the server's machine" in words and "latet server certificate <file>" in words
    made = run_cli("server", "certificate", "--make", "--url", URL, "--out", str(tmp_path / "certs"))
    read = run_cli("server", "certificate", str(tmp_path / "certs" / "server.crt"))
    for other in (made, read):
        assert other.exit_code == 0 and "a machine in between" not in " ".join(other.output.split())


def test_certificate_says_an_authority_is_no_certificate_to_pin(computer, tmp_path):
    authority, key = _authority(tmp_path / "ca")
    first = make_certificate(URL, tmp_path / "first")[0].read_text()
    second = make_certificate(URL, tmp_path / "second")[0].read_text()
    (tmp_path / "two.crt").write_text(first + second, "utf-8")
    for file in (authority, tmp_path / "two.crt"):
        done = run_cli("server", "certificate", str(file))
        assert done.exit_code == 2 and "is an authority" in done.output, done.output
        assert "sha256:" not in done.output and "--fingerprint" not in done.output
    (tmp_path / "none.crt").write_text("-----BEGIN CERTIFICATE-----\nnot one\n-----END CERTIFICATE-----\n", "utf-8")
    unreadable = run_cli("server", "certificate", str(tmp_path / "none.crt"))
    assert unreadable.exit_code == 2 and "not a certificate this can read" in unreadable.output
    server = FakeLatetServer(tmp_path / "tls", certificate=(authority, key)).start()
    try:
        presented = run_cli("server", "certificate", "--url", server.url)
    finally:
        server.stop()
    assert presented.exit_code == 2 and "is an authority" in presented.output, presented.output
    assert "sha256:" not in presented.output and "--fingerprint" not in presented.output


@pytest.mark.parametrize("shape", ["byte-order mark", "bag attributes", "certificate then key", "key then certificate"])
def test_certificate_shows_the_fingerprint_of_a_file_holding_more_than_the_certificate(computer, tmp_path, shape):
    """What a certificate file holds as tools write it, around the certificate: a byte-order mark, openssl pkcs12's
    Bag Attributes, its key beside it. The fingerprint is the certificate's, and nothing of the key is shown."""
    crt, key, fingerprint = make_certificate(URL, tmp_path / "tls")
    cert, private = crt.read_text("utf-8"), key.read_text("utf-8")
    text = {
        "byte-order mark": chr(0xFEFF) + cert,
        "bag attributes": "Bag Attributes\n    localKeyID: 01 00 00 00\nsubject=CN=latet.acme.lan\nissuer=CN=latet.acme.lan\n"
        + cert,
        "certificate then key": cert + private,
        "key then certificate": private + cert,
    }[shape]
    (tmp_path / "server.pem").write_text(text, "utf-8")
    done = run_cli("server", "certificate", str(tmp_path / "server.pem"))
    assert done.exit_code == 0 and fingerprint in done.output, done.output
    assert "PRIVATE KEY" not in done.output and private.splitlines()[1] not in done.output


def test_the_server_keys_folder_is_checked_on_every_system(tmp_path, monkeypatch):
    """The key's folder is checked before the key is written, on POSIX as on Windows: another account that may write
    there could swap the key and the certificate before the administrator reads the fingerprint computers pin."""
    from latet.server import certificate as certificate_module

    checked = []

    def refused(folder):
        checked.append(folder)
        return f"{folder} can be written by its group or by other users: `chmod 700 {folder}`"

    monkeypatch.setattr(certificate_module, "private_folder_problem", refused)
    with pytest.raises(PermissionError, match="chmod 700"):
        make_certificate(URL, tmp_path / "tls")
    assert checked == [tmp_path / "tls"] and list((tmp_path / "tls").iterdir()) == []


@POSIX
def test_on_posix_the_server_keys_folder_is_made_private_and_one_others_may_write_is_refused(tmp_path):
    old = os.umask(0o002)  # a user-private-group umask, as Fedora's bash sets
    try:
        _, key, _ = make_certificate(URL, tmp_path / "tls")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "tls").stat().st_mode) == 0o700 and stat.S_IMODE(key.stat().st_mode) == 0o600
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o775)
    with pytest.raises(PermissionError, match="chmod 700"):
        make_certificate(URL, shared)
    assert list(shared.iterdir()) == []


def test_certificate_says_a_folder_it_may_not_write_the_key_in(computer, tmp_path, monkeypatch):
    def refused(url, folder, days=825, now=None):
        raise PermissionError(f"the server's key is not written: {folder} is open to others")

    monkeypatch.setattr("latet.server.certificate.make_certificate", refused)
    done = run_cli("server", "certificate", "--make", "--url", URL, "--out", str(tmp_path / "certs"))
    assert done.exit_code == 2 and "is open to others" in done.output and "Traceback" not in done.output


# --- without the extra ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", [("psycopg", "psycopg_pool", "redis"), ("psycopg_pool",)], ids=["the extra", "the pool alone"]
)
def test_the_server_machines_commands_say_which_extra_they_need(missing):
    code = (
        "import sys\n"
        f"for name in {missing!r}: sys.modules[name] = None\n"
        "import latet.cli.admin\n"
        "from typer.testing import CliRunner\n"
        "from latet.cli.main import app\n"
        "for command in ('init', 'migrate', 'run', 'ready'):\n"
        "    result = CliRunner().invoke(app, ['server', command])\n"
        "    assert result.exit_code == 2 and 'server` extra' in result.output, (command, result.output)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr + done.stdout
