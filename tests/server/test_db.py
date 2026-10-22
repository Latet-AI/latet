"""PostgreSQL for a Latet server: its schema made by the migrations, one instance at a time, and an instance that
refuses a schema older or newer than itself."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from latet.server.db import Database, SchemaMismatch, check_schema, migrate

pytestmark = pytest.mark.server

TABLES = {
    "identities", "tokens", "codes", "admin_tokens", "root_keys", "settings", "vault_entities", "vault_strings",
    "reviews", "image_decisions", "exchanges", "blocks", "images", "review_records", "changes", "audit",
    "audit_anchor", "schema_migrations",
}  # fmt: skip


def test_migrating_makes_the_schema_once_and_says_what_it_applied(settings, schema):
    db = Database(settings.database_url, schema=schema)
    try:
        with pytest.raises(SchemaMismatch, match="latet server migrate"):
            check_schema(db)
        applied = migrate(db)  # the core's first; a later part's after it (E1 appends "latet.ee.migrations")
        assert applied[0] == "latet.server.migrations:001_core" and len(applied) == len(set(applied))
        assert migrate(db) == []
        check_schema(db)
        with db.connection() as conn:
            found = {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
            )}  # fmt: skip
        assert found >= TABLES
    finally:
        db.close()


def test_two_instances_migrating_at_once_apply_each_migration_once(settings, schema):
    dbs = [Database(settings.database_url, schema=schema) for _ in range(2)]
    try:
        with ThreadPoolExecutor(2) as pool:
            applied = list(pool.map(migrate, dbs))
        none, every = sorted(applied, key=len)
        assert none == [] and every[0] == "latet.server.migrations:001_core"  # each migration applied once
    finally:
        for db in dbs:
            db.close()


def test_an_instance_refuses_a_schema_newer_than_itself(db):
    with db.connection() as conn:
        conn.execute("INSERT INTO schema_migrations (name, applied) VALUES ('latet.server.migrations:999_later', 0)")
    with pytest.raises(SchemaMismatch, match="newer than this Latet"):
        check_schema(db)


def _stopped_halfway(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO admin_tokens (id, hash, label, created, tag) VALUES ('a_1', 'h1', 'first', 0, 't1')")
        raise RuntimeError("stopped halfway")


def test_a_transaction_commits_whole_or_not_at_all(db):
    with pytest.raises(RuntimeError, match="halfway"):
        _stopped_halfway(db)
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_tokens").fetchone() == (0,)
    assert db.ping()


def test_a_wait_that_psycopg_bounds_itself_is_left_as_psycopg_has_it(db):
    """A connection bounds its waits for an answer (``ANSWER_SECONDS``) where psycopg gives none: one it bounds itself
    (the notifications of LISTEN) is its own, ends as psycopg ends it, and leaves the connection open."""
    with db.connection() as conn:
        assert list(conn.notifies(timeout=0.1)) == []
        assert not conn.closed and conn.execute("SELECT 1").fetchone() == (1,)
