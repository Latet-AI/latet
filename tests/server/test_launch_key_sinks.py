"""Where a launch key, or a value Latet protects, could leave a Latet server: one real server (PostgreSQL, Valkey, the
gate, the identity's space with its History on full) and a MockTransport provider.

A launch opened on the space's bridge carries its key in the header (the only form a server takes): the request its
binding decides is masked toward the provider and restored back. The path form (`/u/<route>/_k/<key>/...`), a
malformed carrier and the key after the launch ended are refused. Neither the key nor the original value reaches the
provider, an answer (the original only in the restored reply), a log record at DEBUG (the gate's request lines among
them) or a metrics report; the key reaches neither the space's History nor its dashboard's answers (`/me`,
`/exchanges`, each exchange, `/sessions`, `/stats`). Valkey's bytes are Task 11's (`test_valkey_nothing_in_clear.py`)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
import redis

from latet import observe
from latet.config import Config, Profile, ProjectEntry
from latet.server.app import create_server_app, listen, open_server
from latet.server.bus import redis_url
from latet.server.db import Database, migrate
from latet.server.launch_records import State
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .support import DATABASE, URL, VALKEY
from .test_launch_server_adapter import _clean_owned_namespaces, _held_control, _launch_state, _wait_tasks
from .test_launch_server_adapter import _prove_owned_launch_namespaces as _prove_owned_launch_namespaces

pytestmark = pytest.mark.server
_ROOT = "/client/launch-key-sinks"
_WORK_LIMIT = 64
_ORIGINAL = "ghp_" + "R5tY8uI1oP4aS7dF0gH3jK6lZ9xC2vB5nM8q"  # a GitHub token shape: the secrets profile masks it
_ASKED = f"ordinary launched request with token {_ORIGINAL}"


class _Metrics:
    def __init__(self) -> None:
        self.heard: list[tuple[str, tuple]] = []

    def masked(self, found):
        self.heard.append(("masked", (found,)))

    def exchange(self, agent, stopped):
        self.heard.append(("exchange", (agent, stopped)))

    def waiting(self, delta):
        self.heard.append(("waiting", (delta,)))

    def apps(self, operation, texts, values):
        self.heard.append(("apps", (operation, texts, values)))


async def _answered(response) -> bytes:
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await response({"type": "http", "method": "GET", "headers": []}, receive, send)
    return b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")


def _asked(path_params=None):
    from starlette.requests import Request

    return Request(
        {"type": "http", "method": "GET", "headers": [], "query_string": b"", "path_params": path_params or {}}
    )


def _absent(schema: str, prefix: str) -> None:
    """This test's own schema and key prefix (exact names it drew) are gone: read only."""
    with psycopg.connect(DATABASE, connect_timeout=5) as connection:
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None:
            raise RuntimeError("the owned schema is still there")
    with redis.Redis.from_url(redis_url(VALKEY), socket_timeout=5, socket_connect_timeout=5) as client:
        if next(client.scan_iter(match=prefix + "*", count=100), None) is not None:
            raise RuntimeError("the owned prefix still holds keys")


def _logged(records: list[logging.LogRecord]) -> list[str]:
    return [
        text
        for record in records
        for text in (getattr(record, "message", ""), record.getMessage(), repr(record.args), record.exc_text or "")
    ]


async def test_a_server_keeps_the_launch_key_out_of_every_sink(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
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
            raise RuntimeError("unexpected launch-key sinks work inventory")
        actual = original_submit(executor, fn, *args, **kwargs)
        work.append(actual)
        return actual

    def close_later(spaces, words=None):
        workers.update(space.worker for space in spaces if space.worker is not None)
        original_close_later(spaces, words)
        closing.update(server.spaces._closing)

    sent: list[httpx.Request] = []

    def provider(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        if len(sent) > 1:
            raise RuntimeError("unexpected second provider attempt")
        asked = json.loads(incoming.content)["messages"][0]["content"]
        body = json.dumps(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "ordinary provider reply: " + asked}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()
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
    metrics = _Metrics()
    unlisten = observe.listen(metrics)
    listener = None
    answered: dict[str, httpx.Response] = {}
    key = ""
    history_text = board_text = ""
    items: list[dict] = []
    try:
        server.shared.set(Config(ocr="off", history="full"), actor="server")
        identity, token = server.identities.add_key("P26a launch-key sinks fixture")
        identity_root = root_key(server.db, server.master, identity.id, clock=server.clock)
        storage = PgSettings(server.db, server.shared, identity.id, identity_root, kind="key")
        loaded = storage.load()
        config = storage.prepare(
            loaded.model_copy(update={"ner": "off", "projects": [ProjectEntry(path=_ROOT, profile=Profile.SECRETS)]})
        )
        storage.save(config)
        storage.save_reviewed(config)
        app = create_server_app(server)
        listener = asyncio.create_task(listen(server))
        async with asyncio.timeout(60):
            await server.listening.wait()
            route = f"/u/{route_key(token)}"
            base = f"{route}/_latet/engine/v1"
            control = {"x-latet-token": token}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False
            ) as client:
                assert (await client.get(base + "/ping", headers=control)).status_code == 200
                prepare = await client.get(base + "/launches/prepare", headers=control)
                assert prepare.status_code == 200
                epoch = prepare.json()["epoch"]
                opened = await client.post(
                    base + "/launches", headers=control, json={"folder": _ROOT, "root": _ROOT, "flavor": "posix"}
                )
                assert opened.status_code == 200 and opened.headers["cache-control"] == "no-store"
                key, launch_id = opened.json()["key"], opened.json()["launch_id"]
                item = base + "/launches/" + launch_id
                body = {
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": _ASKED}],
                }
                headers = {"x-api-key": "sk-fixture", "x-latet-project": _ROOT}

                async def ask(name: str, path: str, carrier: str | None) -> None:
                    extra = {"x-latet-launch-key": carrier} if carrier is not None else {}
                    answered[name] = await client.post(path, headers={**headers, **extra}, json=body)

                async with _held_control(app, item + "/hold", token, 0) as (held, _disconnect):
                    live = _launch_state(server, bytes.fromhex(launch_id))
                    assert live is not None and (live.state, live.generation) == (State.LIVE, 1)
                    await ask("verified", route + "/anthropic/v1/messages", key)
                    await ask("path", f"{route}/_k/{key}/anthropic/v1/messages", None)
                    await ask("malformed", route + "/anthropic/v1/messages", key[:-2])
                    stale = {"folder": _ROOT, "root": _ROOT, "flavor": "posix", "epoch": "0" * 32, "retirement": 0}
                    answered["recover"] = await client.post(
                        base + "/launches/recover",
                        headers=control,
                        json={**stale, "key": key, "launch_id": launch_id},
                    )
                    answered["end"] = await client.post(item + "/end", headers=control, json={})
                    await _wait_tasks({held})
                await ask("ended", route + "/anthropic/v1/messages", key)
                answered["exit"] = await client.post(item + "/exit", headers=control, json={"epoch": epoch})
                assert _launch_state(server, bytes.fromhex(launch_id)) is None
                assert server.launch_holds.count == 0
            runtime = server.spaces._spaces[identity.id].runtime
            assert runtime.recorder is not None and runtime.recorder.drain(10)
            store = runtime.exchanges
            assert store is not None
            items = store.list()[0]
            history_text = json.dumps((items, [store.detail(row["id"]) for row in items]))
            board = runtime.gateway.dashboard
            board_text = b"".join(
                [
                    await _answered(await board.me(_asked())),
                    await _answered(await board.exchanges(_asked())),
                    await _answered(await board.sessions(_asked())),
                    await _answered(await board.stats(_asked())),
                    *[await _answered(await board.exchange(_asked({"id": row["id"]}))) for row in items],
                ]
            ).decode("utf-8")
    finally:
        unlisten()
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
            raise RuntimeError("launch-key sinks fixture still owns live Server work")
        server.close()
        _clean_owned_namespaces(schema, prefix)
        _absent(schema, prefix)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))

    told = repr(metrics.heard)
    # The agent's own HTTP client runs in this process here: its httpx lines name the server's address (with the path
    # form's key, as the agent sent it) and are the agent's, not Latet's.
    records = [record for record in caplog.records if not (record.name == "httpx" and URL + "/" in record.getMessage())]
    refused = ("path", "malformed")
    provider_request = sent[0] if len(sent) == 1 else None
    checks = {
        "the launch's binding decides the keyed request, restored back": answered["verified"].status_code == 200
        and _ORIGINAL in answered["verified"].text,
        "the path form and a malformed carrier are refused": all(
            answered[name].status_code == 400 and "request launch authority refused" in answered[name].text
            for name in refused
        ),
        "the ended key is refused": answered["ended"].status_code >= 400,
        "the controls answer, a stale recovery by its code alone": answered["end"].json() == {"ended": True}
        and answered["exit"].json().get("retired") is True
        and answered["recover"].status_code >= 400
        and set(answered["recover"].json()) == {"error"},
        "one provider send, masked, without the carrier": provider_request is not None
        and key not in str(provider_request.url)
        and "/_k/" not in str(provider_request.url)
        and all(name.lower() != "x-latet-launch-key" for name, _ in provider_request.headers.raw)
        and all(key not in value for value in provider_request.headers.values())
        and key.encode() not in provider_request.content
        and _ORIGINAL.encode() not in provider_request.content,
        "no answer holds the key, no refusal the original": all(
            key not in response.text and key not in json.dumps(dict(response.headers)) for response in answered.values()
        )
        and all(_ORIGINAL not in answered[name].text for name in (*refused, "ended", "recover", "end", "exit")),
        "History says the keyed request was decided by its launch key (root's ruling I-5)": [
            row.get("launch") for row in items if row.get("launch") is not None
        ]
        == ["launched"],
        "History holds the exchange and not the key": bool(items)
        and "ordinary launched request" in history_text
        and key not in history_text
        and "/_k/" not in history_text,
        "the dashboard's answers hold the rows and not the key": all(row["id"] in board_text for row in items)
        and key not in board_text,
        "the gate's request lines are made, and no record at DEBUG holds the key or the original": any(
            record.name == "latet.server.requests" for record in records
        )
        and all(secret not in text for text in _logged(records) for secret in (key, _ORIGINAL)),
        "metrics heard the masking, not the key or the original": any(name == "masked" for name, _ in metrics.heard)
        and key not in told
        and _ORIGINAL not in told,
    }
    assert all(checks.values()), {name: passed for name, passed in checks.items() if not passed}
