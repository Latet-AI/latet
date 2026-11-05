"""The first launch migration must have its explicit master before schema mutation."""

import secrets
import sys

import psycopg
import pytest
import redis
from psycopg import sql

from latet.server.bus import redis_url
from latet.server.db import Database, DatabaseError, migrate

from .support import DATABASE, VALKEY

pytestmark = pytest.mark.server


def _schema_exists(connection, schema):
    return connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None


@pytest.fixture
def owned_launch_namespaces(request):
    schema = "latet_t_" + secrets.token_hex(5)
    prefix = "latet_t_" + secrets.token_hex(5) + ":"
    with psycopg.connect(DATABASE, connect_timeout=5) as conn:
        if _schema_exists(conn, schema):
            raise RuntimeError("schema must be absent before ownership")
    with redis.Redis.from_url(redis_url(VALKEY), socket_timeout=5, socket_connect_timeout=5) as client:
        if next(client.scan_iter(match=prefix + "*", count=100), None) is not None:
            raise RuntimeError("prefix must be absent before ownership")
    # The optional contained-run plugin records this proof before a dependent fixture can migrate.
    request.node.user_properties.append(("p26a.before_absent", (schema, prefix)))
    try:
        yield schema, prefix
    finally:
        # The dependent core fixture closes its Bus and cleans its prefix before this finalizer.
        # The dependent database fixture has also closed its pool before we drop the owned schema.
        with psycopg.connect(DATABASE, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))


@pytest.fixture
def launch_bootstrap_db(owned_launch_namespaces):
    schema, _ = owned_launch_namespaces
    db = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        yield db
    finally:
        db.close()


def test_core_launch_migration_requires_explicit_master_before_schema_mutation(launch_bootstrap_db):
    db = launch_bootstrap_db
    master_required = False
    try:
        migrate(db)
    except DatabaseError as exc:
        master_required = str(exc) == "launch migration requires an explicit master key"
    with db.connection() as conn:
        schema_created = _schema_exists(conn, db.schema)
    assert (master_required, schema_created) == (True, False)


def test_custom_migration_package_without_launch_bootstrap_still_applies(launch_bootstrap_db, tmp_path, monkeypatch):
    db = launch_bootstrap_db
    package = tmp_path / f"{db.schema}_control"
    package.mkdir()
    (package / "__init__.py").write_text("", "utf-8")
    (package / "001_control.sql").write_text(
        "CREATE TABLE control (value INTEGER NOT NULL); INSERT INTO control VALUES (1);", "utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert migrate(db, packages=[package.name]) == [f"{package.name}:001_control"]
    finally:
        sys.modules.pop(package.name, None)
    with db.connection() as conn:
        assert conn.execute("SELECT value FROM control").fetchone() == (1,)
