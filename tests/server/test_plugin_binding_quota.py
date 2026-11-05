"""The server's plugin bindings are accounted per identity (root's ruling I-3 on P2-6a's review): each identity has its
own quota row, locked alone, so one identity that fills its quota never refuses another's binding and never serializes
another's bridge calls; and a launched Claude Code session of an identity whose quota is full starts claimed (only
tightens), never answered 503. Migration 016 moves 015's one server-wide counter to the identities' rows."""

from __future__ import annotations

import pytest

from latet.server import plugin_bindings
from latet.server.db import Database, check_schema, migrate
from latet.server.db import _migrations as every
from latet.server.plugin_bindings import PluginBindings

from .conftest import identity_in

pytestmark = pytest.mark.server
_EMILY, _MARIO = "u_emilyaaaaa", "u_marioaaaaa"
_PROJECT, _ROOT = "a" * 12, "/workspace"
_QUOTA = "latet.server.migrations:016_plugin_identity_quota"


def _bindings(db, issuer):
    return PluginBindings(db, issuer, b"r" * 32)


def test_015s_bindings_move_to_their_identities_quotas(settings, schema, monkeypatch, migration_master):
    """A server of 015 (one counter for the whole server) migrates to a row for each identity, its count and its
    sequence carried over, and the bindings it had still bind."""
    before = [(name, text) for name, text in every(["latet.server.migrations"]) if name < _QUOTA]
    db = Database(settings.database_url, schema=schema, max_size=2)
    try:
        with monkeypatch.context() as at_015:
            at_015.setattr("latet.server.db._migrations", lambda _packages: before)
            migrate(db, master=migration_master)
            for issuer in (_EMILY, _MARIO):
                identity_in(db, issuer)
            with db.transaction() as conn:
                for n, (issuer, session) in enumerate(((_EMILY, "cc:e1"), (_EMILY, "cc:e2"), (_MARIO, "cc:m1"))):
                    digest = _bindings(db, issuer).session_digest(session)
                    binding = _bindings(db, issuer).binding_digest(_PROJECT, _ROOT)
                    conn.execute(
                        "INSERT INTO legacy_plugin_binding (issuer, session_digest, project_id, binding_digest,"
                        " required, generation, expires_us) VALUES (%s, %s, %s, %s, false, %s, %s)",
                        (issuer, digest, bytes.fromhex(_PROJECT), binding, n + 1, 1 << 60),
                    )
                conn.execute("UPDATE legacy_plugin_quota SET entries = 3, generation = 3 WHERE singleton = 1")
        assert migrate(db, master=migration_master)[0] == _QUOTA  # the first applied, those after it (017 on) next
        check_schema(db)
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT issuer, entries, generation FROM legacy_plugin_identity_quota ORDER BY issuer"
            ).fetchall()
            assert rows == [(_EMILY, 2, 3), (_MARIO, 1, 3)]
            gone = conn.execute("SELECT to_regclass('legacy_plugin_quota')").fetchone()
            assert gone == (None,), "015's server-wide counter is gone"
        assert _bindings(db, _EMILY).read("cc:e1") is not None
        assert _bindings(db, _MARIO).refresh("cc:m2", _PROJECT, _ROOT, required=False)
    finally:
        db.close()


def test_a_full_identity_quota_never_refuses_another_identity(db, monkeypatch):
    for issuer in (_EMILY, _MARIO):
        identity_in(db, issuer)
    monkeypatch.setattr(plugin_bindings, "_CAP", 2)
    emily, mario = _bindings(db, _EMILY), _bindings(db, _MARIO)
    assert emily.refresh("cc:e1", _PROJECT, _ROOT, required=False)
    assert emily.refresh("cc:e2", _PROJECT, _ROOT, required=True)
    assert not emily.refresh("cc:e3", _PROJECT, _ROOT, required=False), "her own quota is full"
    assert emily.refresh("cc:e1", _PROJECT, _ROOT, required=False), "a binding she has is refreshed"
    assert mario.refresh("cc:m1", _PROJECT, _ROOT, required=False), "another identity is not refused for it"
    with db.connection() as conn:
        rows = conn.execute("SELECT issuer, entries FROM legacy_plugin_identity_quota ORDER BY issuer").fetchall()
    assert rows == [(_EMILY, 2), (_MARIO, 1)], "each identity's count is its own"
    assert not emily.refresh("cc:e4", _PROJECT, _ROOT, required=False), "his binding gave her no slot"
    assert mario.refresh("cc:m2", _PROJECT, _ROOT, required=True)


def test_an_identity_reclaims_its_own_expired_bindings_only(db, monkeypatch):
    for issuer in (_EMILY, _MARIO):
        identity_in(db, issuer)
    monkeypatch.setattr(plugin_bindings, "_CAP", 1)
    emily, mario = _bindings(db, _EMILY), _bindings(db, _MARIO)
    assert emily.refresh("cc:e1", _PROJECT, _ROOT, required=False)
    assert mario.refresh("cc:m1", _PROJECT, _ROOT, required=False)
    with db.transaction() as conn:
        conn.execute("UPDATE legacy_plugin_binding SET expires_us = 0")
    assert emily.refresh("cc:e2", _PROJECT, _ROOT, required=False), "her expired binding gave its place back"
    with db.connection() as conn:
        left = conn.execute("SELECT issuer FROM legacy_plugin_binding ORDER BY issuer").fetchall()
    assert left == [(_EMILY,), (_MARIO,)], "his expired row is his to reclaim, not hers"
