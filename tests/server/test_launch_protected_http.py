"""Real protected HTTP authority across the final send wait."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from latet.config import Config, ProjectEntry
from latet.gateway import request_transport
from latet.server.app import create_server_app, listen, open_server
from latet.server.db import Database, migrate
from latet.server.launch_binding import binding_key
from latet.server.launch_records import Operation, State
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .support import DATABASE, URL
from .test_launch_server_adapter import (
    _WORK_LIMIT,
    _clean_owned_namespaces,
    _held_control,
    _launch_state,
    _wait_tasks,
)
from .test_launch_server_adapter import _prove_owned_launch_namespaces as _prove_owned_launch_namespaces

pytestmark = pytest.mark.server
_ROOT = "/client/protected-http"
_SESSION = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"  # Claude Code session ids are UUIDs (ID_HEADERS)
_CARRIER = b"x-latet-launch-key"
# The folder each mode's request claims beside the launch's key (the launch's own root otherwise).
_CLAIMS = {"claim-nested": _ROOT + "/sub", "claim-subfolder": _ROOT + "/src"}


async def _legacy_http(
    server, identity, client, route, base, control_headers, monkeypatch, provider_calls, marked, off
):
    session = _SESSION
    bridge = URL + route
    facts = {
        "bridge": bridge,
        "session": session,
        "cwd": _ROOT,
        "env": {"ANTHROPIC_BASE_URL": bridge + "/anthropic"},
        "sources": {},
        "entrypoint": "cli",
        "surface": "terminal",
    }
    if marked:
        # The launch ID only tightens. No matching launch or presented key is required to commit this marker.
        facts["launch_id"] = "1" * 32
    registered = await client.post(base + "/route-check", headers=control_headers, json=facts)
    assert registered.status_code == 200 and registered.json()["ok"] is True
    requests = server.spaces._spaces[identity.id].runtime.gateway.protected_launch
    assert requests is not None
    row = requests.plugins.read(f"cc:{session}")
    assert row is not None and row.required is marked

    original_send = request_transport.send
    prepared_trusted: bool | None = None

    async def around_send(
        upstream: httpx.AsyncClient, built: request_transport.BuiltRequest
    ) -> httpx.Response | request_transport.SendRefusal:
        nonlocal prepared_trusted
        assert prepared_trusted is None
        arena = built.authorized.arena
        prepared_trusted = not arena.off and arena.protected_current is not None
        return await original_send(upstream, built)

    with monkeypatch.context() as hook:
        hook.setattr(request_transport, "send", around_send)
        result = await client.post(
            route + "/anthropic/v1/messages",
            headers={
                "x-api-key": "sk-fixture",
                "x-latet-project": _ROOT,
                "x-claude-code-session-id": session,
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    if marked:  # a claim of a project where Latet is off: refused (R-83)
        assert result.status_code == 400 and "request claimed a project where Latet is off" in result.text
        assert prepared_trusted is None and provider_calls == []
    elif off:  # D5: trusted through the plugin's binding, a project where Latet is off still sends nothing
        assert result.status_code == 400 and "Latet is off in this project" in result.text
        assert prepared_trusted is None and provider_calls == []
    else:
        assert result.status_code == 200 and prepared_trusted is True and provider_calls == [(True, True)]


@pytest.mark.parametrize(
    "mode", ["live", "late-end", "legacy-unmarked", "legacy-required", "trusted-off", "claim-nested", "claim-subfolder"]
)
async def test_protected_http_send(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, mode):
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        migrate(database, master=MasterKey.load(settings))
    finally:
        database.close()
    server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")
    loop_thread = threading.get_ident()
    work = []
    workers = set()
    closing = set()
    original_submit = ThreadPoolExecutor.submit
    original_close_later = server.spaces._close_later

    def submit(executor, fn, /, *args, **kwargs):
        if threading.get_ident() != loop_thread:
            return original_submit(executor, fn, *args, **kwargs)
        if len(work) == _WORK_LIMIT:
            raise RuntimeError("unexpected protected HTTP work inventory")
        actual = original_submit(executor, fn, *args, **kwargs)
        work.append(actual)
        return actual

    def close_later(spaces, words=None):
        workers.update(space.worker for space in spaces if space.worker is not None)
        original_close_later(spaces, words)
        closing.update(server.spaces._closing)

    provider_calls = []

    def provider(incoming: httpx.Request) -> httpx.Response:
        if provider_calls:
            raise RuntimeError("unexpected second provider attempt")
        provider_calls.append(
            (
                incoming.method == "POST" and incoming.url.path == "/v1/messages",
                all(name.lower() != _CARRIER for name, _value in incoming.headers.raw),
            )
        )
        body = b'{"id":"msg_test","type":"message","role":"assistant","model":"claude","content":[]}'
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": str(len(body))},
            stream=httpx.ByteStream(body),
        )

    monkeypatch.setattr(
        "latet.gateway.app.make_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
    )
    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    monkeypatch.setattr(server.spaces, "_close_later", close_later)
    listener = None
    try:
        server.shared.set(Config(ocr="off", history="off"), actor="server")
        identity, token = server.identities.add_key("P26a protected HTTP fixture")
        identity_root = root_key(server.db, server.master, identity.id, clock=server.clock)
        storage = PgSettings(server.db, server.shared, identity.id, identity_root, kind="key")
        loaded = storage.load()  # a Claude Code session id needs only its grammar, no configured row
        config = storage.prepare(
            loaded.model_copy(
                update={
                    "ner": "off",
                    "projects": [
                        ProjectEntry(path=_ROOT, enabled=mode not in ("legacy-required", "trusted-off")),
                        # A project of its own inside the launch's (R-105, D7: projects are compared, not folders).
                        ProjectEntry(path=_ROOT + "/sub", terms=[{"value": "Project Heron", "type": "PROJECT"}]),
                    ],
                }
            )
        )
        storage.save(config)
        storage.save_reviewed(config)
        app = create_server_app(server)
        listener = asyncio.create_task(listen(server))
        async with asyncio.timeout(30):
            await server.listening.wait()
            route = f"/u/{route_key(token)}"
            base = f"{route}/_latet/engine/v1"
            control_headers = {"x-latet-token": token}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False
            ) as client:
                ready = await client.get(base + "/ping", headers=control_headers)
                assert ready.status_code == 200 and ready.json()["ok"] is True
                if mode in ("legacy-unmarked", "legacy-required", "trusted-off"):
                    await _legacy_http(
                        server,
                        identity,
                        client,
                        route,
                        base,
                        control_headers,
                        monkeypatch,
                        provider_calls,
                        marked=mode == "legacy-required",
                        off=mode == "trusted-off",
                    )
                    return
                late_end = mode == "late-end"
                prepare = await client.get(base + "/launches/prepare", headers=control_headers)
                assert prepare.status_code == 200
                epoch = prepare.json()["epoch"]
                opened = await client.post(
                    base + "/launches",
                    headers=control_headers,
                    json={"folder": _ROOT, "root": _ROOT, "flavor": "posix"},
                )
                assert opened.status_code == 200 and opened.headers["cache-control"] == "no-store"
                launch = opened.json()
                assert launch["generation"] == 0 and launch["binding"]["root"] == _ROOT
                launch_id = bytes.fromhex(launch["launch_id"])
                pending = _launch_state(server, launch_id)
                assert pending is not None and pending.state == State.PENDING
                authority = server.launch_authority
                assert authority is not None
                locator = authority.locator(launch["key"].encode("ascii"))
                stable = binding_key(server.bus, bytes.fromhex(epoch), locator)
                assert server.bus.client.strlen(stable) > 0
                item = base + "/launches/" + launch["launch_id"]
                original_send = request_transport.send
                send_ready: tuple[bool, bool, bool] | None = None
                refused_stale = False

                async def end() -> None:
                    response = await client.post(item + "/end", headers=control_headers, json={})
                    assert response.status_code == 200 and response.json() == {"ended": True}
                    row = _launch_state(server, launch_id)
                    assert row is not None
                    assert (row.state, row.generation, row.operation) == (State.TERMINAL, 1, Operation.NONE)

                async def around_send(
                    upstream: httpx.AsyncClient, built: request_transport.BuiltRequest
                ) -> httpx.Response | request_transport.SendRefusal:
                    nonlocal send_ready, refused_stale
                    if send_ready is not None:
                        raise RuntimeError("unexpected second authorized HTTP send")
                    arena = built.authorized.arena
                    send_ready = (not arena.off, arena.protected_current is not None, not provider_calls)
                    if late_end:
                        # At the one-use adapter, after prepare/finalize and before its real freshness check.
                        await end()
                    result = await original_send(upstream, built)
                    refused_stale = isinstance(result, request_transport.SendRefusal) and result.code == "stale"
                    return result

                async with _held_control(app, item + "/hold", token, 0) as (held, _disconnect):
                    live = _launch_state(server, launch_id)
                    assert live is not None
                    assert (live.state, live.generation, live.operation) == (State.LIVE, 1, Operation.NONE)
                    with monkeypatch.context() as hook:
                        hook.setattr(request_transport, "send", around_send)
                        result = await client.post(
                            route + "/anthropic/v1/messages",
                            headers={
                                "x-api-key": "sk-fixture",
                                "x-latet-project": _CLAIMS.get(mode, _ROOT),
                                "x-latet-launch-key": launch["key"],
                            },
                            json={
                                "model": "claude-sonnet-4-5",
                                "max_tokens": 8,
                                "messages": [{"role": "user", "content": "hello"}],
                            },
                        )
                    if mode == "claim-nested":
                        # The key's project is /repo; the claim resolves to /repo/sub, another project: refused, as on
                        # a computer, never the key's loosenings with the other project's added rules skipped.
                        assert result.status_code == 400 and "launch authority refused" in result.text
                        assert send_ready is None and provider_calls == []
                        await end()
                        await _wait_tasks({held})
                        return
                    assert send_ready == (True, True, True) and refused_stale is late_end
                    assert result.status_code == (500 if late_end else 200)
                    assert provider_calls == ([] if late_end else [(True, True)])
                    if not late_end:
                        await end()
                    await _wait_tasks({held})

                exited = await client.post(item + "/exit", headers=control_headers, json={"epoch": epoch})
                assert exited.status_code == 200 and exited.json() == {"retired": True, **prepare.json()}
                assert _launch_state(server, launch_id) is None and server.bus.client.strlen(stable) == 0
                assert server.launch_holds.count == 0
    finally:
        if listener is not None:
            listener.cancel()
            await _wait_tasks({listener}, allow_cancelled=True)
        await _wait_tasks(set(server.spaces._building.values()))
        await _wait_tasks(set(server.spaces._index_tasks))
        await server.spaces.close(wait=False)
        await _wait_tasks(closing | server.spaces._closing)
        await _wait_tasks(workers, allow_cancelled=True)
        await _wait_tasks({asyncio.wrap_future(actual) for actual in work})
        await server.launch_mutations.drain()
        if server.spaces._spaces or server.spaces._building or server.spaces._closing or server._ear is not None:
            raise RuntimeError("protected HTTP fixture still owns live Server work")
        server.close()
        _clean_owned_namespaces(schema, prefix)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))
