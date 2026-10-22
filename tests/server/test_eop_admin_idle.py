"""ES.2 real role/session evidence, prepared only; no runtime result is claimed."""

import asyncio
import threading
from contextlib import contextmanager

import httpx
import pytest

from latet.server import admin as admin_module
from latet.server.app import create_server_app, open_server
from latet.server.db import Database, migrate

from .support import URL

pytestmark = pytest.mark.server
ADMIN = "/_latet/server/v1/admin"


@pytest.fixture
def idle_server(settings, schema, prefix, role_url, tmp_path):
    configured = settings.model_copy(update={"database_url": role_url(idle_in_transaction_session_timeout="1s")})
    db = Database(configured.database_url, schema=schema, max_size=1)
    try:
        migrate(db)
    finally:
        db.close()
    server = open_server(configured, schema=schema, prefix=prefix, data=tmp_path / "models")
    yield server
    server.close()


async def test_api_key_save_keeps_its_token_lock_across_both_commits_past_the_roles_idle_limit(
    idle_server, monkeypatch
):
    server = idle_server
    server.listening.set()
    saved = revoking = None
    releases = [threading.Event(), threading.Event()]
    entered = [threading.Event(), threading.Event()]
    revocation_started = threading.Event()
    try:
        key, _ = server.identities.add_key("Billing CI")
        rt = await server.spaces.runtime(key)
        saver = server.admin.issue("saver", days=None)
        other = server.admin.issue("revoker", days=None)
        saver_id = server.admin.check(saver)
        held, observed, held_pids, restored = server.admin.held, [], set(), []

        @contextmanager
        def traced_transaction():
            # The same Database.transaction contexts, with the same leased connection checked after transaction end.
            with server.db.pool.connection() as conn:
                with conn.transaction():
                    yield conn
                if conn.info.backend_pid in held_pids:
                    restored.append(conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0])

        def checked(token_id, *, conn, update=False):
            if token_id == saver_id and update:
                revocation_started.set()
            if token_id == saver_id and not update:
                held_pids.add(conn.info.backend_pid)
                observed.append(conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0])
            return held(token_id, conn=conn, update=update)

        keep, approve = rt.settings._keep_own, rt.settings.save_reviewed

        def paused_keep(own):
            entered[0].set()
            assert releases[0].wait(15), "settings-save barrier was not released"
            return keep(own)

        def paused_approval(config):
            entered[1].set()
            assert releases[1].wait(15), "approval barrier was not released"
            return approve(config)

        monkeypatch.setattr(server.admin, "held", checked)
        monkeypatch.setattr(server.db, "transaction", traced_transaction)
        monkeypatch.setattr(rt.settings, "_keep_own", paused_keep)
        monkeypatch.setattr(rt.settings, "save_reviewed", paused_approval)
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            saved = asyncio.create_task(
                client.put(
                    f"{ADMIN}/identities/{key.id}/settings",
                    json={**rt.config.model_dump(mode="json"), "profile": "max"},
                    headers={"x-latet-admin": saver},
                )
            )
            assert await asyncio.to_thread(entered[0].wait, 10)
            assert observed and observed[-1] == "10min"  # before taking the held token row, not a global pool setting
            revoking = asyncio.create_task(
                client.delete(f"{ADMIN}/admin-tokens/{saver_id}", headers={"x-latet-admin": other})
            )
            assert await asyncio.to_thread(revocation_started.wait, 10)
            # Deliberately beyond the one-second role setting, with wide scheduling margin. No sub-ms race assertion.
            await asyncio.sleep(1.5)
            assert not revoking.done()
            releases[0].set()
            assert await asyncio.to_thread(entered[1].wait, 10)
            await asyncio.sleep(1.5)
            assert not revoking.done()
            releases[1].set()
            answer = await asyncio.wait_for(saved, 15)
            assert answer.status_code == 200, answer.text
            assert (await asyncio.wait_for(revoking, 15)).json() == {"revoked": True}
        assert rt.settings.reviewed() == rt.config
        assert restored and all(timeout == "1s" for timeout in restored)
        actions = [entry.action for entry in reversed(server.audit.query())]
        assert actions.index("settings.changed") < actions.index("admin-token.revoked")
        with server.db.connection() as conn:
            assert conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0] == "1s"
    finally:
        for release in releases:
            release.set()
        outstanding = [task for task in (saved, revoking) if task is not None]
        if outstanding:
            await asyncio.wait_for(asyncio.gather(*outstanding, return_exceptions=True), 20)
        await server.spaces.close()


async def test_a_save_that_hangs_holds_a_revocation_of_its_token_no_longer_than_the_held_idle_timeout(
    idle_server, monkeypatch
):
    """The held transaction's idle timeout is generous, never none: a save that hangs past it has that transaction
    ended by PostgreSQL, and a revocation of its token, waiting on the row it held, goes on (it waited for good)."""
    server = idle_server
    server.listening.set()
    monkeypatch.setattr(admin_module, "HELD_IDLE_SECONDS", 1)
    saved = revoking = None
    release, entered = threading.Event(), threading.Event()
    try:
        key, _ = server.identities.add_key("Billing CI")
        rt = await server.spaces.runtime(key)
        saver = server.admin.issue("saver", days=None)
        other = server.admin.issue("revoker", days=None)
        saver_id = server.admin.check(saver)
        keep = rt.settings._keep_own
        approved = [row for row in _settings_rows(server, key.id) if row[0] == "reviewed"]

        def hung(own):
            entered.set()
            release.wait(20)
            return keep(own)

        monkeypatch.setattr(rt.settings, "_keep_own", hung)
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            saved = asyncio.create_task(
                client.put(
                    f"{ADMIN}/identities/{key.id}/settings",
                    json={**rt.config.model_dump(mode="json"), "profile": "max"},
                    headers={"x-latet-admin": saver},
                )
            )
            assert await asyncio.to_thread(entered.wait, 10)
            revoking = asyncio.create_task(
                client.delete(f"{ADMIN}/admin-tokens/{saver_id}", headers={"x-latet-admin": other})
            )
            answer = await asyncio.wait_for(revoking, 10)  # the save still hangs
            assert answer.json() == {"revoked": True} and not saved.done()
            release.set()
            outcome = await asyncio.wait_for(saved, 20)
        # The save lost its token's row with that transaction: its approval is not kept (it was kept after the token's
        # revocation, and the call failed on the dead transaction's commit), and the log says so.
        assert outcome.status_code == 503 and "could not be kept" in outcome.text
        assert [row for row in _settings_rows(server, key.id) if row[0] == "reviewed"] == approved
        assert server.audit.query(action="settings.changed") == []
        [refused] = [e for e in server.audit.query() if e.details.get("approved") is False]
        assert refused.details["status"] == 503 and refused.details["keys"] == ["profile"]
    finally:
        release.set()
        outstanding = [task for task in (saved, revoking) if task is not None]
        if outstanding:
            await asyncio.wait_for(asyncio.gather(*outstanding, return_exceptions=True), 20)
        await server.spaces.close()


def _settings_rows(server, identity_id):
    with server.db.connection() as conn:
        return conn.execute(
            "SELECT kind, data, version FROM settings WHERE owner = %s ORDER BY kind", (identity_id,)
        ).fetchall()


async def test_idle_pin_failure_aborts_before_holding_or_applying_settings(idle_server, monkeypatch):
    """Inject refusal of exactly SET LOCAL at the real held transaction's execute seam, before driver execution."""
    server = idle_server
    server.listening.set()
    try:
        key, _ = server.identities.add_key("Billing CI")
        rt = await server.spaces.runtime(key)
        saver = server.admin.issue("saver", days=None)
        saver_id = server.admin.check(saver)
        before = _settings_rows(server, key.id), rt.config
        attempts, checked, applied = [], [], []
        transaction, held, apply = server.db.transaction, server.admin.held, rt.apply_config

        class RefusedPin:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, query, *args, **kwargs):
                if query == admin_module.HELD_IDLE:
                    attempts.append(self.conn.info.backend_pid)
                    raise OSError("es2-private-pin-failure")
                return self.conn.execute(query, *args, **kwargs)

        @contextmanager
        def refused_pin():
            with transaction() as conn:
                yield RefusedPin(conn)

        def holding(token_id, *, conn, update=False):
            if token_id == saver_id:
                checked.append(update)
            return held(token_id, conn=conn, update=update)

        def applying(*args, **kwargs):
            applied.append(True)
            return apply(*args, **kwargs)

        monkeypatch.setattr(server.db, "transaction", refused_pin)
        monkeypatch.setattr(server.admin, "held", holding)
        monkeypatch.setattr(rt, "apply_config", applying)
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            answer = await client.put(
                f"{ADMIN}/identities/{key.id}/settings",
                json={**rt.config.model_dump(mode="json"), "profile": "max"},
                headers={"x-latet-admin": saver},
            )
        assert answer.status_code == 503 and "es2-private-pin-failure" not in answer.text
        assert len(attempts) == 1 and checked == [] and applied == []
        assert (_settings_rows(server, key.id), rt.config) == before
        with server.db.connection() as conn:
            assert conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0] == "1s"
    finally:
        await server.spaces.close()


@pytest.mark.parametrize("fault", [OSError, asyncio.CancelledError], ids=["failed-save", "cancelled-save-worker"])
async def test_held_transaction_restores_and_releases_after_a_save_fault(idle_server, monkeypatch, fault):
    """The actual held context rolls back on an error or worker cancellation before the own-part write.

    Cancellation is injected into that worker; this does not claim an HTTP disconnect cancels a running thread.
    """
    server = idle_server
    server.listening.set()
    try:
        key, _ = server.identities.add_key("Billing CI")
        rt = await server.spaces.runtime(key)
        saver = server.admin.issue("saver", days=None)
        saver_id = server.admin.check(saver)
        before = _settings_rows(server, key.id)
        held, held_pids, observed, restored, failures = server.admin.held, set(), [], [], []

        @contextmanager
        def traced_transaction():
            with server.db.pool.connection() as conn:
                try:
                    with conn.transaction():
                        yield conn
                finally:
                    if conn.info.backend_pid in held_pids:
                        restored.append(
                            (
                                conn.info.backend_pid,
                                conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0],
                            )
                        )

        def checked(token_id, *, conn, update=False):
            if token_id == saver_id and not update:
                held_pids.add(conn.info.backend_pid)
                observed.append(conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()[0])
            return held(token_id, conn=conn, update=update)

        def failed_keep(own):
            failures.append(True)
            raise fault("es2-private-save-failure")

        monkeypatch.setattr(server.db, "transaction", traced_transaction)
        monkeypatch.setattr(server.admin, "held", checked)
        monkeypatch.setattr(rt.settings, "_keep_own", failed_keep)
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            call = client.put(
                f"{ADMIN}/identities/{key.id}/settings",
                json={**rt.config.model_dump(mode="json"), "profile": "max"},
                headers={"x-latet-admin": saver},
            )
            if fault is asyncio.CancelledError:
                with pytest.raises(asyncio.CancelledError):
                    await call
            else:
                answer = await call
                assert answer.status_code == 503 and "es2-private-save-failure" not in answer.text
        assert failures == [True] and observed == ["10min"]
        assert restored and all(pid in held_pids and timeout == "1s" for pid, timeout in restored)
        assert _settings_rows(server, key.id) == before
        # NOWAIT proves the held row was released, without a timing-based assertion or an unbounded lock wait.
        with server.db.transaction() as conn:
            assert conn.execute("SELECT id FROM admin_tokens WHERE id = %s FOR UPDATE NOWAIT", (saver_id,)).fetchone()
    finally:
        await server.spaces.close()
