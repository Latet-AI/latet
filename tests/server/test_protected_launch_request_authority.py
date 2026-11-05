"""Finite protected authority consumer rows, with actual PostgreSQL and Valkey."""

from __future__ import annotations

import pytest

from latet.server import plugin_bindings
from latet.server.launch_binding import RemoteResult, binding_key
from latet.server.launch_requests import ServerLaunchRequests
from latet.server.plugin_bindings import PluginBindings

from .test_launch_authority_core import _BINDING, _ISSUER, _KEY, _live
from .test_launch_authority_core import (
    launch_bootstrap_db as launch_bootstrap_db,
)
from .test_launch_authority_core import (
    owned_launch_namespaces as owned_launch_namespaces,
)
from .test_launch_authority_core import (
    protected_core as protected_core,
)

pytestmark = pytest.mark.server
_SESSION = "cc:protected-legacy-fixture"


def _requests(core):
    db, _bus, authority, codec, _master = core
    return ServerLaunchRequests(authority, codec, PluginBindings(db, _ISSUER, b"r" * 32))


@pytest.mark.parametrize("ended", [False, True], ids=["live", "late-end"])
def test_protected_request_receipt_after_decision(protected_core, ended):
    _db, _bus, authority, codec, _master = protected_core
    opened = _live(authority, codec)
    adapter = _requests(protected_core)
    selected = adapter.select(adapter.derive(_KEY), _SESSION, claimed_folder=_BINDING.folder, legacy_eligible=False)
    assert selected.status == "verified" and selected.binding == _BINDING and selected.freshness is not None
    assert opened.receipt is not None
    if ended:
        address = opened.receipt.address
        assert authority.terminal(address.issuer, address.locator, address.generation)
    assert selected.freshness.current() is not ended


def test_absent_protected_row_is_claimed_with_stale_authenticated_blob(protected_core):
    _db, bus, authority, codec, _master = protected_core
    opened = _live(authority, codec)
    assert opened.receipt is not None
    address = opened.receipt.address
    key = binding_key(bus, address.epoch, address.locator)
    stale = bus.client.get(key)
    assert type(stale) is bytes
    assert authority.terminal(address.issuer, address.locator, address.generation, exit_confirmed=True)
    cleanup = authority.reserve_cleanup(address.issuer, address.locator)
    assert cleanup is not None and authority.submit(cleanup).wire.remote == RemoteResult.DELETE
    assert authority.retire(address.issuer, address.locator, address.generation)
    assert bus.client.set(key, stale)
    adapter = _requests(protected_core)
    assert adapter.plugins.refresh(_SESSION, _BINDING.project, _BINDING.root, required=False)
    selected = adapter.select(adapter.derive(_KEY), _SESSION, claimed_folder=None, legacy_eligible=True)
    assert selected.status == "claimed" and selected.binding is None and selected.freshness is None


def test_protected_legacy_refresh_or_expiry_and_recreation(protected_core):
    db, _bus, _authority, _codec, _master = protected_core
    adapter = _requests(protected_core)
    store = adapter.plugins
    assert adapter.select(None, _SESSION, claimed_folder=None, legacy_eligible=True).status == "claimed"
    assert store.refresh(_SESSION, _BINDING.project, _BINDING.root, required=False)
    legacy = adapter.select(None, _SESSION, claimed_folder=None, legacy_eligible=True)
    assert legacy.status == "legacy" and legacy.freshness is not None and legacy.freshness.current()
    assert adapter.matches_legacy(legacy, _BINDING.project, _BINDING.root)
    assert not adapter.matches_legacy(legacy, _BINDING.project, "/other")
    assert store.refresh(_SESSION, _BINDING.project, _BINDING.root, required=False)
    assert legacy.freshness.current()
    assert store.refresh(_SESSION, _BINDING.project, _BINDING.root, required=True)
    assert store.refresh(_SESSION, _BINDING.project, _BINDING.root, required=False)
    marked = store.read(_SESSION)
    assert marked is not None and marked.required and not legacy.freshness.current()
    assert adapter.select(None, _SESSION, claimed_folder=None, legacy_eligible=True).status == "claimed"
    with db.transaction() as conn:
        conn.execute("UPDATE legacy_plugin_binding SET expires_us=0 WHERE issuer=%s", (_ISSUER,))
    assert store.read(_SESSION) is None
    assert store.refresh(_SESSION, _BINDING.project, _BINDING.root, required=False)
    recreated = store.read(_SESSION)
    assert recreated is not None and not recreated.required and recreated.generation > marked.generation


def test_protected_plugin_quota_refuses_extra_slot_and_reclaims_expired(protected_core, monkeypatch):
    db, _bus, _authority, _codec, _master = protected_core
    store = _requests(protected_core).plugins
    monkeypatch.setattr(plugin_bindings, "_CAP", 2)
    assert store.refresh("cc:first", _BINDING.project, _BINDING.root, required=True)
    assert store.refresh("cc:second", _BINDING.project, _BINDING.root, required=False)
    assert not store.refresh("cc:third", _BINDING.project, _BINDING.root, required=False)
    assert store.read("cc:first") is not None and store.read("cc:third") is None
    with db.transaction() as conn:
        conn.execute(
            "UPDATE legacy_plugin_binding SET expires_us=0 WHERE issuer=%s AND session_digest=%s",
            (_ISSUER, store.session_digest("cc:second")),
        )
    assert store.refresh("cc:third", _BINDING.project, _BINDING.root, required=False)
    with db.connection() as conn:
        assert conn.execute("SELECT entries FROM legacy_plugin_identity_quota").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM legacy_plugin_binding").fetchone() == (2,)
    assert store.read("cc:second") is None and store.read("cc:third") is not None
