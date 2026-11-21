"""Servers for the Enterprise tests, on the PostgreSQL and Valkey that E0b's `scripts/server-services.py up` starts.
The instances of one test share a schema and a key prefix of their own, so tests run side by side, and the schema is
dropped and the keys removed at the end. Skipped, saying why, when LATET_TEST_DATABASE_URL or LATET_TEST_VALKEY_URL is
not set; failed instead where the services are required (LATET_TEST_REQUIRE_SERVICES=1, CI's job), as the server's own
tests are."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from typing import Any

import pytest

from ..owned import owned

DATABASE = os.environ.get("LATET_TEST_DATABASE_URL", "")
VALKEY = os.environ.get("LATET_TEST_VALKEY_URL", "")
URL = "https://latet.acme.lan"
MASTER_KEY = base64.b64encode(b"\x09" * 32).decode()  # the server's master key, as LATET_MASTER_KEY gives it

services = pytest.mark.skipif(
    not (DATABASE and VALKEY) and os.environ.get("LATET_TEST_REQUIRE_SERVICES") != "1",
    reason="LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL are not set: `uv run python scripts/server-services.py up`",
)


def master() -> Any:
    """The server's master key (``MASTER_KEY``), as an instance loads it: what the migrations seal Latet 0.11's launch
    registry with (``db.migrate``'s ``master``), so that the instances started with it open that registry."""
    from latet.server.master import MasterKey

    return MasterKey([base64.b64decode(MASTER_KEY)])


class Instances:
    """The instances of one server in a test: each `open()` is one more, sharing this test's schema and prefix in
    PostgreSQL and Valkey and keeping its own memory, as instances behind a load balancer do."""

    def __init__(self, clock: Any) -> None:
        from latet.server.db import Database, migrate
        from latet.server.settings import ServerSettings

        self.clock = clock
        # Named in the run's journal before the migration below makes the schema (tests/owned.py).
        self.schema = owned(f"t_{uuid.uuid4().hex[:12]}", "schema", DATABASE)
        self.prefix = owned(f"t{uuid.uuid4().hex[:8]}:", "prefix", VALKEY)
        self.settings = ServerSettings(url=URL, database_url=DATABASE, valkey_url=VALKEY)
        db = Database(DATABASE, schema=self.schema)
        try:
            migrate(db, master=master())  # the Community tables and the Enterprise ones (MIGRATIONS)
        finally:
            db.close()
        self.opened: list[Any] = []

    def open(self) -> Any:
        from latet.server.app import open_server

        server = open_server(self.settings, clock=self.clock, schema=self.schema, prefix=self.prefix)
        self.opened.append(server)
        return server

    def close(self) -> None:
        import psycopg
        from psycopg import sql

        failed: BaseException | None = None
        try:
            for server in reversed(self.opened):  # each closed whatever another did
                try:
                    asyncio.run(server.spaces.close())
                except BaseException as exc:
                    failed = failed or exc
                try:
                    server.close()  # E0b's: its edition (from Task 10, its listener) first, then its Bus and its pool
                except BaseException as exc:
                    failed = failed or exc
        finally:
            try:
                # Once every instance has stopped (what an edition writes as it closes too: E4's events, and the roles
                # they read), with a connection of the purge's own (E3's release gates, fix round 2).
                if self.opened:
                    purge(self.prefix)
            finally:
                with psycopg.connect(DATABASE, autocommit=True) as conn:
                    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))
        if failed is not None:
            raise failed


def left(prefix: str) -> list[bytes]:
    """The keys under ``prefix`` in the test Valkey, read with a connection of its own."""
    import redis

    from latet.server.bus import redis_url

    client = redis.Redis.from_url(redis_url(VALKEY), socket_connect_timeout=5, socket_timeout=5)
    try:
        return list(client.scan_iter(match=prefix + "*", count=1000))
    finally:
        client.close()


def purge(prefix: str) -> None:
    """Every key under ``prefix``, which is this test's alone, as E0b's ``prefix`` fixture removes them, with a
    connection of its own (this prefix holds no character a pattern reads)."""
    import redis

    from latet.server.bus import redis_url

    client = redis.Redis.from_url(redis_url(VALKEY), socket_connect_timeout=5, socket_timeout=5)
    try:
        keys = list(client.scan_iter(match=prefix + "*", count=1000))
        if keys:
            client.delete(*keys)
    finally:
        client.close()
