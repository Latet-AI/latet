"""The finite F3/F5 durable-state witnesses, through actual PostgreSQL stores and HTTP handlers."""

import asyncio
import ipaddress

import httpx
import pytest

from latet.config import Config, Destination
from latet.runtime import digest
from latet.server import egress, spaces
from latet.server.app import create_server_app
from latet.server.master import root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .support import URL

pytestmark = pytest.mark.server
API = "/_latet/api/v1"
CANARY = "es13-private@acme.com"
TERM = "Mercury Labs"
ADDED_URL = "https://added.acme.com"
USERNAME = "es-eop-user"


def _fresh_settings(server, person):
    key = root_key(server.db, server.master, person.id, clock=server.clock)
    return PgSettings(server.db, server.shared, person.id, key)


def _durable(server, person):
    """Read the actual rows, independently of the runtime's settings/snapshot caches."""
    with server.db.connection() as conn:
        settings = conn.execute(
            "SELECT kind, data, version FROM settings WHERE owner = %s ORDER BY kind", (person.id,)
        ).fetchall()
        changes = conn.execute(
            "SELECT id, data, state FROM changes WHERE identity_id = %s ORDER BY id", (person.id,)
        ).fetchall()
    return settings, changes


def _has_edit(config, surface):
    if surface == "destinations":
        return any(d.url == ADDED_URL for d in config.upstreams)
    if surface == "identity":
        return USERNAME in config.identity.usernames
    return TERM in [t.value for t in config.terms]


async def _save(client, runtime, token, session, surface, key=None):
    browser = {"cookie": f"latet_session={session}", "x-latet-ui": "1"}
    if surface == "config":
        found = await client.get(f"{API}/config", headers=browser)
        assert found.status_code == 200
        terms = [*runtime.config.model_dump(mode="json")["terms"], {"value": TERM}]
        return await client.put(f"{API}/config", json={"etag": found.json()["etag"], "terms": terms}, headers=browser)
    if surface == "import":
        return await client.post(
            f"{API}/rules/import", json={"file": f"terms:\n  - {{value: {TERM}}}\n", "apply": True}, headers=browser
        )
    headers = {"x-latet-token": token}
    if surface == "destinations":
        return await client.post(
            f"/u/{route_key(token)}/_latet/admin/destinations",
            json={"destinations": [{"url": ADDED_URL}]},
            headers=headers,
        )
    keyed = {**headers, "x-latet-computer-key": key or ""}  # the computer's key, which the identity asks for (R16)
    return await client.put("/_latet/server/v1/identity", json={"usernames": [USERNAME]}, headers=keyed)


async def test_a_real_shared_destination_move_refuses_before_writing_and_approval_allows_the_separate_edit(server):
    """F3: a fresh, deterministic PgSettings reproduction; this does not claim to recover the historical probe."""
    server.listening.set()
    old = Config(upstreams=[Destination(name="gw", url="https://old.acme.com")])
    moved = Config(upstreams=[Destination(name="gw", url="https://new.acme.com")])
    server.shared.set(old, actor="admin:a_eop0000000")
    person, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    session = server.sign_ins.open(person.id)
    try:
        await server.spaces.runtime(person)  # establishes the actual approved old shared snapshot
        server.shared.set(moved, actor="admin:a_eop0000000")
        await server.spaces.drop(person.id)
        runtime = await server.spaces.runtime(person)
        assert isinstance(runtime.settings, PgSettings)
        assert runtime.config.upstreams == old.upstreams and runtime.settings.composition is not None
        [waiting] = list(runtime.mediator.approvals.waiting_changes())
        before, applied = _durable(server, person), runtime.config
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            answer = await _save(client, runtime, token, session, "config")
            assert answer.status_code == 409 and answer.json()["reason"] == "settings_change_waits"
            assert "retry-after" not in answer.headers and "saved" not in answer.json()
            assert _durable(server, person) == before and runtime.config == applied
            decision = await client.post(
                f"{API}/changes/{waiting.id}",
                json={"action": "approve"},
                headers={"cookie": f"latet_session={session}", "x-latet-ui": "1"},
            )
            assert decision.status_code == 200
            await server.spaces.drop(person.id)  # refresh/rebuild from the durable decision
            runtime = await server.spaces.runtime(person)
            assert runtime.config.upstreams == moved.upstreams
            answer = await _save(client, runtime, token, session, "config")
            assert answer.status_code == 200
        fresh = _fresh_settings(server, person)
        assert _has_edit(fresh.load(), "config") and fresh.reviewed() == runtime.config
        assert not list(runtime.mediator.approvals.waiting_changes())
    finally:
        await server.spaces.close()


@pytest.mark.parametrize("surface", ["config", "import", "destinations", "identity"])
@pytest.mark.parametrize("stage", ["before-save", "waiting-read", "proposal-open", "obsolete-settle"])
async def test_real_saved_outcomes_keep_durable_state_and_recover_without_replay(
    server, monkeypatch, caplog, surface, stage
):
    """F5: failures surround actual writes; the existing reapply is gated until the HTTP result is inspected.

    The obsolete-settle case explicitly makes propose report no lowering to enter that branch; its settlement then
    fails. It proves durable partial-outcome propagation there, not a naturally occurring obsolete proposal.
    """
    server.listening.set()
    person, code = server.identities.add_person("Emily Carter")
    _, token, key = server.identities.enrol(code, "laptop")
    session = server.sign_ins.open(person.id)
    release = asyncio.Event()
    try:
        await server.spaces.runtime(person)
        settings = _fresh_settings(server, person)
        settings.save(settings.load().model_copy(update={"allow_values": [CANARY]}))  # kept, not approved
        await server.spaces.drop(person.id)
        runtime = await server.spaces.runtime(person)
        settings, approvals = runtime.settings, runtime.mediator.approvals
        assert isinstance(settings, PgSettings) and settings.composition is not None
        assert CANARY not in runtime.config.allow_values
        [waiting] = list(approvals.waiting_changes())
        before, applied = _durable(server, person), runtime.config
        keep, stale, asked = settings._keep_own, settings._stale, runtime._asked_again
        read, opened, settled, propose = (
            approvals.waiting_changes,
            approvals.open_change,
            approvals.settle_change,
            runtime.propose,
        )
        run = server.spaces._run
        committed, active = [False], [True]
        writes, refreshes, signals, faults, forced = [], [], [], [], []
        recovered = asyncio.Event()

        def kept(own):
            writes.append(dict(own))
            if stage == "before-save":
                faults.append(stage)
                raise OSError("es13-private-store-fault")
            keep(own)
            committed[0] = True

        def told():
            signals.append(True)
            stale()  # preserve the existing real reapply signal, rather than adding a replacement hook

        def refreshed():
            refreshes.append(True)
            return asked()

        def failed_at(place):
            if active[0] and committed[0] and stage == place:
                faults.append(place)
                raise RuntimeError("es13-private-refresh-fault")

        def read_waiting():
            failed_at("waiting-read")
            return read()

        def open_change(change):
            failed_at("proposal-open")
            return opened(change)

        def settle_change(change_id, state):
            failed_at("obsolete-settle")
            return settled(change_id, state)

        def proposed(config, source, agent=None):
            if active[0] and committed[0] and stage == "obsolete-settle":
                forced.append(True)
                return None
            return propose(config, source, agent)

        async def held_reapply(work, *args, **kwargs):
            if work is spaces._reapply and args[0].identity.id == person.id:
                await release.wait()
                result = await run(work, *args, **kwargs)
                recovered.set()
                return result
            return await run(work, *args, **kwargs)

        monkeypatch.setattr(settings, "_keep_own", kept)
        monkeypatch.setattr(settings, "_stale", told)
        monkeypatch.setattr(runtime, "_asked_again", refreshed)
        monkeypatch.setattr(approvals, "waiting_changes", read_waiting)
        monkeypatch.setattr(approvals, "open_change", open_change)
        monkeypatch.setattr(approvals, "settle_change", settle_change)
        monkeypatch.setattr(runtime, "propose", proposed)
        monkeypatch.setattr(server.spaces, "_run", held_reapply)
        monkeypatch.setattr(egress, "resolve", lambda host, port: [ipaddress.ip_address("1.2.3.4")])
        transport = httpx.ASGITransport(app=create_server_app(server), client=("10.0.3.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
            answer = await _save(client, runtime, token, session, surface, key)
        active[0] = False
        assert len(writes) == 1 and faults == [stage]
        assert "es13-private-" not in answer.text + caplog.text
        if stage == "before-save":
            assert answer.status_code == 503 and "saved" not in answer.json()
            assert _durable(server, person) == before and runtime.config == applied
            assert refreshes == signals == []
            return
        assert answer.status_code == 409
        assert answer.json()["reason"] == "settings_saved_change_pending" and answer.json()["saved"] is True
        assert "retry-after" not in answer.headers
        assert refreshes == signals == [True] and forced == ([True] if stage == "obsolete-settle" else [])
        fresh = _fresh_settings(server, person)
        durable, approved = fresh.load(), fresh.reviewed()
        assert _has_edit(durable, surface) and _has_edit(runtime.config, surface)
        assert approved is not None and approved == runtime.config and CANARY not in approved.allow_values
        assert CANARY in durable.allow_values and CANARY not in runtime.config.allow_values
        own_rows, waiting_rows = _durable(server, person)
        assert waiting_rows == before[1] and [c.id for c in read()] == [waiting.id]
        if surface == "destinations":
            assert next(d.name for d in runtime.config.upstreams if d.url == ADDED_URL) in runtime.wrapped
        release.set()  # the one existing signal now recovers through the real space worker
        await asyncio.wait_for(recovered.wait(), 20)
        [current] = list(read())
        assert current.base == digest(runtime.config) and current.config == _fresh_settings(server, person).load()
        assert len(writes) == 1 and signals == [True] and CANARY not in runtime.config.allow_values
        assert _durable(server, person)[0] == own_rows
        await server.spaces.drop(person.id)
        rebuilt = await server.spaces.runtime(person)
        assert _has_edit(rebuilt.config, surface) and CANARY not in rebuilt.config.allow_values
        [after_restart] = list(rebuilt.mediator.approvals.waiting_changes())
        assert after_restart.id == current.id and after_restart.base == digest(rebuilt.config)
        assert _durable(server, person)[0] == own_rows
    finally:
        release.set()
        await server.spaces.close()
