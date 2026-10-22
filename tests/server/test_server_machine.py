"""`latet server` on the server's machine, on the test PostgreSQL: the schema is the code's after the fixture's
migration, migrating again applies nothing, a migration is recorded under the log's key with the master key the
database knows (and nothing is changed without it), the first administration token works on the administration and is
issued once, and the beat's hourly job walks the administration log's chain whatever its prune did."""

import logging
import time

import psycopg
import pytest
from starlette.testclient import TestClient

from latet.server import app as app_module
from latet.server import db as db_module
from latet.server import instance
from latet.server.audit import AuditLog
from latet.server.db import Database
from latet.server.live import ChainVerdicts
from latet.server.master import MasterKey, MasterKeyError
from latet.server.operations import server_app

from .conftest import DATABASE
from .support import captured_logs

pytestmark = pytest.mark.server


def test_the_schema_is_the_codes_and_migrating_again_applies_nothing(server):
    assert instance.schema_state(server.settings, server.db) is None
    assert instance.migrate_schema(server.settings, server.db) == []
    assert server.audit.query(action="server.migrated") == []  # nothing applied, nothing recorded


def test_the_first_administration_token_works_and_is_issued_once(server):
    token_id, secret = instance.first_admin_token(server)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        assert client.get("/_latet/server/v1/admin/status", headers={"x-latet-admin": secret}).status_code == 200
    assert instance.first_admin_token(server) is None
    assert server.admin.check(secret) == token_id
    issued = server.audit.query(action="admin-token.issued")
    assert [(e.actor, e.target, e.details) for e in issued] == [("server", token_id, {})]  # never the label
    assert [e.actor for e in server.audit.query(action="server.initialized")] == ["server"]
    assert [token.issued_by for token in server.admin.list()] == ["server"]


def test_two_inits_at_once_on_a_fresh_database_issue_one_first_token(server, monkeypatch):
    """Two containers or machines running `latet server init` at the same moment: the token list is read again under
    one lock, in the transaction that issues the first token, so that one is issued and the other finds it."""
    import threading

    listed = server.admin.list

    def slow(*args, **kwargs):  # a list read, and the issue a moment later: room for the other init to read it too
        found = listed(*args, **kwargs)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(server.admin, "list", slow)
    issued: list[object] = []
    threads = [threading.Thread(target=lambda: issued.append(instance.first_admin_token(server))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sum(found is not None for found in issued) == 1 and len(issued) == 3
    assert len(server.audit.query(action="server.initialized")) == 1 and len(listed()) == 1


def test_a_first_token_lasts_for_good_or_the_days_asked_bounded_by_no_issuer(server):
    for_good, _ = instance.first_admin_token(server, never=True)
    again, _ = instance.first_admin_token(server, again=True, days=30)
    listed = {token.id: token for token in server.admin.list()}
    assert listed[for_good].expires is None and listed[for_good].issued_by == "server"
    assert listed[again].expires == pytest.approx(time.time() + 30 * 86400, abs=60)
    issued = server.audit.query(action="admin-token.issued")  # newest first
    assert [(e.actor, e.target, e.details) for e in issued] == [
        ("server", again, {"again": True}), ("server", for_good, {}),
    ]  # fmt: skip


def test_a_token_listed_is_one_issued_before_whatever_the_log_still_holds(server):
    server.admin.issue("an administrator")  # outside init: no entry, as when the log's retention has passed
    assert server.audit.query(action="admin-token.issued") == []
    assert instance.first_admin_token(server) is None
    assert len(server.admin.list()) == 1 and server.audit.query(action="server.initialized") == []


def test_a_migration_is_recorded_under_the_logs_key(settings, schema):
    database = Database(settings.database_url, schema=schema, max_size=2)
    try:
        assert "latet server migrate" in (instance.schema_state(settings, database) or "")  # not there yet
        applied = instance.migrate_schema(settings, database)
        assert applied == [name for name, _ in db_module._migrations(db_module.MIGRATIONS)]
        assert instance.schema_state(settings, database) is None
        log = AuditLog(database, key=MasterKey.load(settings).derive(b"audit"))
        assert [(e.actor, e.details) for e in log.query(action="server.migrated")] == [
            ("server", {"migrations": applied})
        ]
        assert log.verify().ok  # under the key the instances derive: their chain goes on from it
    finally:
        database.close()


def test_a_master_key_the_database_does_not_know_migrates_nothing(settings, schema, tmp_path):
    database = Database(settings.database_url, schema=schema, max_size=2)
    try:
        instance.migrate_schema(settings, database)  # the first key: the database's from now on
        with database.connection() as conn:  # a migration that waits, as after an upgrade of Latet
            conn.execute("DELETE FROM schema_migrations WHERE name LIKE %s", ("%002_admin_token_issuer",))
            conn.execute("ALTER TABLE admin_tokens DROP COLUMN issued_by")
        other = tmp_path / "other.key"
        other.write_text(MasterKey.generate(), "utf-8")
        with pytest.raises(MasterKeyError, match="not the one this database was first started with"):
            instance.migrate_schema(settings.model_copy(update={"master_key_file": other}), database)
        assert "1 to apply" in (instance.schema_state(settings, database) or "")  # the migration waits still
        log = AuditLog(database, key=MasterKey.load(settings).derive(b"audit"))
        assert len(log.query(action="server.migrated")) == 1 and log.verify().ok  # no entry under another key
    finally:
        database.close()


def test_no_master_key_is_made_for_a_database_an_instance_started_on(server, tmp_path):
    """The database holds the check value an instance's first start sealed: a key made now would open nothing of it.
    None is written, none is printed, whether a file is named or nothing is."""
    named = server.settings.model_copy(update={"master_key_file": tmp_path / "new.key"})
    for settings in (named, named.model_copy(update={"master_key_file": None})):
        with pytest.raises(MasterKeyError, match="started with another master key"):
            instance.ensure_master_key(settings, server.db)
    assert list(tmp_path.iterdir()) == []


def test_a_schema_newer_than_this_latet_migrates_nothing_and_says_so(server):
    with server.db.connection() as conn:  # a migration of a later Latet, which this one does not know
        conn.execute(
            "INSERT INTO schema_migrations (name, applied) VALUES (%s, 0)", ("latet.server.migrations:999_next",)
        )
    assert instance.migrate_schema(server.settings, server.db) == []
    assert "newer than this Latet" in (instance.schema_state(server.settings, server.db) or "")


def test_migrating_without_a_master_key_changes_nothing(settings, schema):
    database = Database(settings.database_url, schema=schema, max_size=2)
    try:
        with pytest.raises(MasterKeyError, match="master key"):
            instance.migrate_schema(settings.model_copy(update={"master_key_file": None}), database)
    finally:
        database.close()
    with psycopg.connect(DATABASE) as conn:
        assert conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is None


def test_the_hourly_job_prunes_as_many_days_as_the_settings_say(server, monkeypatch):
    server.settings = server.settings.model_copy(update={"audit_retention_days": 730})
    asked = []
    monkeypatch.setattr(server.audit, "prune", lambda days: asked.append(days) or 0)
    app_module._audit(server)
    assert asked == [730]


def test_the_hourly_job_walks_the_chain_whatever_its_prune_did(server, monkeypatch):
    """A prune made to fail (a row held by a writer of the table, its statement ended at its bound) never stops the
    chain's verdict, which a stopped verdict would let an alert on a broken chain clear by itself."""

    def held(days):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout: Emily Carter's row")

    monkeypatch.setattr(server.audit, "prune", held)
    kept = []
    real = server.chain.keep
    monkeypatch.setattr(server.chain, "keep", lambda checked: kept.append(checked) or real(checked))
    with captured_logs("text") as logs:
        app_module._audit(server)
        logging.getLogger().handlers[0].flush()
        said = logs.getvalue()
    assert len(kept) == 1 and kept[0].ok
    assert ChainVerdicts(server.bus, server.master).read() == "ok"
    assert "QueryCanceled" in said and "Emily" not in said and "statement timeout" not in said
