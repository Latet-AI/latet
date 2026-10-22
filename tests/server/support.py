"""What tests of a Latet server use, in E0b and in later parts: PostgreSQL and Valkey on this computer, an instance of
a server in a test's own schema and Valkey prefix (two sharing them, when a test asks for two), each instance's ear on
Valkey, a wait for what one instance publishes to reach another, and what the server logs meanwhile."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import io
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import psycopg

from latet.server.app import Server, listen, open_server
from latet.server.db import Database, migrate
from latet.server.logs import configure_logging, saved
from latet.server.master import MasterKey
from latet.server.settings import ServerSettings

DATABASE = os.environ.get("LATET_TEST_DATABASE_URL", "")
VALKEY = os.environ.get("LATET_TEST_VALKEY_URL", "")
URL = "https://latet.acme.lan"


@contextlib.contextmanager
def role_catalog() -> Iterator[psycopg.Connection]:
    """Serialize shared database ACL updates while each test keeps its own role and schema."""
    with psycopg.connect(DATABASE, autocommit=True, connect_timeout=5) as admin, admin.transaction():
        admin.execute("SET LOCAL lock_timeout = '5s'")
        admin.execute("SELECT pg_advisory_xact_lock(1818326117, 1)")
        yield admin


@functools.cache
def master_key_file() -> Path:
    """The master key every test server of this run shares, in a file of its own, in a folder removed when the run ends
    (a test key, but a key file all the same), whatever its tests did."""
    folder = Path(tempfile.mkdtemp(prefix="latet-server-tests-"))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    path = folder / "master.key"
    path.write_text(MasterKey.generate(), "utf-8")
    return path


def open_test_server(
    schema: str, prefix: str, *, clock: Callable[[], float] = time.time, url: str = URL, data: Path | None = None
) -> Server:
    """An instance of a Latet server in ``schema`` and ``prefix`` (the fixtures ``schema`` and ``prefix`` give a test
    its own, and remove them after it), the schema migrated first. Called again with the same two, it is another
    instance of the same server, with its own memory."""
    settings = ServerSettings(url=url, database_url=DATABASE, valkey_url=VALKEY, master_key_file=master_key_file())
    db = Database(DATABASE, schema=schema, max_size=1)
    try:
        migrate(db)
    finally:
        db.close()
    return open_server(settings, clock=clock, schema=schema, prefix=prefix, data=data)


def close_test_server(server: Server) -> None:
    """Its connections closed. Its app's lifespan closes its spaces; a test that built some without one awaits
    ``server.spaces.close()`` first (``listening`` does)."""
    server.close()


@contextlib.asynccontextmanager
async def listening(*servers: Server) -> AsyncIterator[None]:
    """Each instance's ear on Valkey while it lasts (an app's lifespan starts it otherwise); their spaces closed after."""
    tasks = []
    for server in servers:
        server.listening = asyncio.Event()
        tasks.append(asyncio.create_task(listen(server)))
    try:
        for server in servers:
            await asyncio.wait_for(server.listening.wait(), 5)
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for server in servers:
            await server.spaces.close()


@contextlib.contextmanager
def captured_logs(format: str = "json", level: str = "info") -> Iterator[io.StringIO]:
    """What the server logs while it lasts, as configure_logging writes it at ``level``; after it, the loggers, how
    warnings are shown and the hooks as they were (``latet.server.logs.saved``: the tests share one process)."""
    stream = io.StringIO()
    with saved():
        configure_logging("json" if format == "json" else "text", level, stream=stream)
        yield stream


async def until(check: Callable[[], object], seconds: float = 5.0) -> None:
    """Wait for ``check()`` to hold: what one instance publishes takes a moment to reach the others."""
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not within the time")
