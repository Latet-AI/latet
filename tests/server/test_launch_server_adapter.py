"""First protected launch control through the real authenticated Server and Space."""

import asyncio
import hashlib
import json
import secrets
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import replace
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
import redis
from psycopg import sql
from starlette.types import ASGIApp, Message, Scope

from latet.config import Config
from latet.gateway.launch_keys import BINDING_BYTES
from latet.projects import project_id
from latet.server import app as server_app
from latet.server import db as db_module
from latet.server import launches as server_launches
from latet.server import machines as server_machines
from latet.server.app import Server, create_server_app, listen, open_server
from latet.server.bus import redis_url
from latet.server.db import Database, migrate
from latet.server.launch_binding import Binding, BindingCodec, RemoteResult, binding_key, canonical_credential
from latet.server.launch_mutations import MUTATION_BYTES, MutationAdmission, TerminalIntent
from latet.server.launch_records import LaunchUnavailable, Operation, Reason, State
from latet.server.launches import Completion
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .conftest import identity_in
from .support import DATABASE, URL, VALKEY, scanned_once

pytestmark = pytest.mark.server
_KEY_LIMIT = 32
_WORK_LIMIT = 32
_DRAIN_SECONDS = 15
_FAULT_REGISTRATION = {"folder": "/client/work/child", "root": "/client/work", "flavor": "posix"}


@pytest.fixture(name="owned_launch_namespaces")
def _prove_owned_launch_namespaces(request):
    schema = "latet_t_" + secrets.token_hex(5)
    prefix = "latet_t_" + secrets.token_hex(5) + ":"
    with psycopg.connect(DATABASE, connect_timeout=5) as connection:
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None:
            raise RuntimeError("schema must be absent before ownership")
    with redis.Redis.from_url(redis_url(VALKEY), socket_timeout=5, socket_connect_timeout=5) as client:
        if next(client.scan_iter(match=prefix + "*", count=100), None) is not None:
            raise RuntimeError("prefix must be absent before ownership")
    request.node.user_properties.append(("p26a.before_absent", (schema, prefix)))
    return schema, prefix


def _clean_owned_namespaces(schema, prefix, *, key_limit=_KEY_LIMIT):
    with redis.Redis.from_url(redis_url(VALKEY), socket_timeout=5, socket_connect_timeout=5) as client:
        keys = scanned_once(client.scan_iter(match=prefix + "*", count=100), key_limit + 1)
        if len(keys) > key_limit or any(not key.startswith(prefix.encode("ascii")) for key in keys):
            raise RuntimeError("unexpected owned Server prefix inventory")
        if keys:
            client.delete(*keys)
    with psycopg.connect(DATABASE, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


async def _wait_tasks(tasks, *, allow_cancelled=False):
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=_DRAIN_SECONDS)
        if pending or any(
            (task.cancelled() and not allow_cancelled) or (not task.cancelled() and task.exception() is not None)
            for task in done
        ):
            raise RuntimeError("owned Server task drain was not proved")


def _canonical_key(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        canonical_credential(value.encode("ascii"))
    except (UnicodeEncodeError, LaunchUnavailable):
        return False
    return True


def _hex_id(value: object) -> bytes | None:
    if type(value) is str and len(value) == 32:
        try:
            decoded = bytes.fromhex(value)
        except ValueError:
            return None
        if decoded.hex() == value:
            return decoded
    return None


def _assert_pending_launch(
    server: Server, response: httpx.Response, issuer: str, epoch: bytes
) -> tuple[bytes, tuple[bytes, bytes, bytes], int]:
    unavailable = (
        response.status_code == 503
        and response.json() == {"error": "launch-unavailable"}
        and response.headers.get("cache-control") == "no-store"
    )
    assert response.status_code == 200, "launch-unavailable" if unavailable else "unexpected open response"
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    exact_fields = type(payload) is dict and set(payload) == {"key", "launch_id", "generation", "binding"}
    assert exact_fields
    credential_valid = _canonical_key(payload["key"])
    launch_id = _hex_id(payload["launch_id"])
    assert credential_valid and launch_id is not None
    initial_generation = type(payload["generation"]) is int and payload["generation"] == 0
    assert initial_generation
    binding_matches = payload["binding"] == {
        "folder": "/client/work/child",
        "root": "/client/work",
        "flavor": "posix",
        "source": "client",
        "project": project_id("/client/work"),
    }
    assert binding_matches

    with server.db.connection() as connection:
        entries = connection.execute(
            "SELECT locator, tag, issuer, launch_id, generation, state, op_kind, disclosure_possible, binding_reserved "
            "FROM launch_entry LIMIT 2"
        ).fetchall()
        issuer_counts = connection.execute(
            "SELECT tag, issuer, entries, bindings FROM launch_identity LIMIT 2"
        ).fetchall()
        shared_counts = connection.execute(
            "SELECT tag, entries, bindings FROM launch_registry WHERE singleton = 1"
        ).fetchone()
    assert len(entries) == 1
    locator, entry_tag, *entry = entries[0]
    assert entry == [issuer, launch_id, 0, State.PENDING, Operation.NONE, True, True]
    assert len(issuer_counts) == 1 and issuer_counts[0][1:] == (issuer, 1, 1)
    assert shared_counts is not None and shared_counts[1:] == (1, 1)
    key = binding_key(server.bus, epoch, locator)
    length = server.bus.client.strlen(key)
    assert type(length) is int and 30 <= length <= BINDING_BYTES
    return key, (entry_tag, issuer_counts[0][0], shared_counts[0]), length


def _sealed_fingerprint(server: Server, key: bytes) -> bytes:
    sealed = server.bus.client.getrange(key, 0, BINDING_BYTES)
    if type(sealed) is not bytes or not 30 <= len(sealed) <= BINDING_BYTES:
        raise RuntimeError("expected one bounded sealed binding")
    return hashlib.sha256(sealed).digest()


class _LaunchState(NamedTuple):
    state: int
    generation: int
    deadline: int
    operation: int
    sequence: int
    binding_reserved: bool
    exit_confirmed: bool
    publication: bytes | None
    tag: bytes
    operation_id: bytes | None


def _launch_state(server: Server, launch_id: bytes) -> _LaunchState | None:
    with server.db.connection() as connection:
        row = connection.execute(
            "SELECT state, generation, deadline_us, op_kind, op_seq, binding_reserved, exit_confirmed, "
            "published_token, tag, op_id FROM launch_entry WHERE launch_id = %s",
            (launch_id,),
        ).fetchone()
    return _LaunchState(*row) if row is not None else None


@asynccontextmanager
async def _start_held_control(app: ASGIApp, path: str, secret: str, base_generation: int, *, allow_cancelled=False):
    address = urlsplit(URL)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": address.scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": f"generation={base_generation}".encode("ascii"),
        "headers": [(b"host", address.netloc.encode("ascii")), (b"x-latet-token", secret.encode("ascii"))],
        "client": ("127.0.0.1", 123),
        "server": (address.hostname, address.port or 443),
    }
    body_ready, disconnect = asyncio.Event(), asyncio.Event()
    status, no_store, more_body = 0, False, False
    first_body = b""
    calls = 0
    initial = True

    async def receive() -> Message:
        nonlocal initial
        if initial:
            initial = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status, no_store, first_body, more_body, calls
        calls += 1
        if calls > 3:
            raise RuntimeError("unexpected held response inventory")
        if message["type"] == "http.response.start":
            status = message["status"]
            no_store = (b"cache-control", b"no-store") in message.get("headers", ())
        if message["type"] == "http.response.body" and not body_ready.is_set():
            first_body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(first_body) > 512:
                raise RuntimeError("unexpected held response size")
            body_ready.set()

    async def acknowledge() -> None:
        await asyncio.wait_for(body_ready.wait(), 5)
        unavailable = status == 503 and no_store and json.loads(first_body) == {"error": "launch-unavailable"}
        assert status == 200, "launch-unavailable" if unavailable else "unexpected HOLD response"
        acknowledged = json.loads(first_body) == {"held": True, "generation": base_generation + 1}
        assert acknowledged and more_body and no_store

    held = asyncio.ensure_future(app(scope, receive, send))
    try:
        yield _HeldCall(held, disconnect, body_ready, acknowledge)
    finally:
        disconnect.set()
        await _wait_tasks({held}, allow_cancelled=allow_cancelled)


class _HeldCall(NamedTuple):
    task: asyncio.Future[None]
    disconnect: asyncio.Event
    body_ready: asyncio.Event
    acknowledge: Callable[[], Awaitable[None]]


@asynccontextmanager
async def _held_control(app: ASGIApp, path: str, secret: str, base_generation: int):
    async with _start_held_control(app, path, secret, base_generation) as held:
        await held.acknowledge()
        yield held.task, held.disconnect


async def _exercise_lifecycle(server: Server, app: ASGIApp, client: httpx.AsyncClient, base, headers, issuer, prepared):
    registration = {"folder": "/client/work/child", "root": "/client/work", "flavor": "posix"}
    opened = await client.post(base + "/launches", headers=headers, json=registration)
    epoch = bytes.fromhex(prepared["epoch"])
    key, _, _ = _assert_pending_launch(server, opened, issuer, epoch)
    opened_data = opened.json()
    launch_id = bytes.fromhex(opened_data["launch_id"])
    item = base + "/launches/" + launch_id.hex()
    secret = headers["x-latet-token"]
    async with AsyncExitStack() as stack:
        old, disconnect = await stack.enter_async_context(_held_control(app, item + "/hold", secret, 0))
        initial = _launch_state(server, launch_id)
        assert initial is not None
        assert (initial.state, initial.generation, initial.operation) == (State.LIVE, 1, Operation.NONE)
        assert server.launch_holds.count == 1
        assert server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES

        await asyncio.sleep(19)
        for _ in range(25):
            refreshed = _launch_state(server, launch_id)
            if (
                refreshed is not None
                and refreshed.operation == Operation.NONE
                and refreshed.deadline > initial.deadline
            ):
                break
            await asyncio.sleep(0.25)
        else:
            pytest.fail("one committed held refresh was not observed", pytrace=False)
        assert refreshed is not None
        assert (refreshed.state, refreshed.generation, refreshed.sequence) == (State.LIVE, 1, initial.sequence + 1)
        assert refreshed.publication == initial.publication and refreshed.binding_reserved
        assert refreshed.tag != initial.tag

        loop = asyncio.get_running_loop()
        replacement_started = loop.time()
        recovered = await client.post(
            base + "/launches/recover",
            headers=headers,
            json={**registration, **prepared, "key": opened_data["key"], "launch_id": launch_id.hex()},
        )
        expected = {"launch_id": launch_id.hex(), "generation": 2, "binding": opened_data["binding"]}
        recovered_as_expected = recovered.json() == expected
        assert recovered.status_code == 200 and recovered_as_expected
        assert recovered.headers["cache-control"] == "no-store"
        replacement, _ = await stack.enter_async_context(_held_control(app, item + "/hold", secret, 2))
        assert not old.done() and server.launch_holds.count == 2
        assert server.launch_mutations.issuer_bytes(issuer) == 2 * MUTATION_BYTES
        current = _launch_state(server, launch_id)
        assert current is not None
        assert (current.state, current.generation, current.operation) == (State.LIVE, 3, Operation.NONE)
        assert loop.time() - replacement_started < 10
        disconnect.set()
        await _wait_tasks({old})
        assert not replacement.done() and server.launch_holds.count == 1
        assert _launch_state(server, launch_id) == current

        ended = await client.post(item + "/end", headers=headers, json={})
        ended_as_expected = ended.json() == {"ended": True}
        assert ended.status_code == 200 and ended_as_expected and ended.headers["cache-control"] == "no-store"
        await _wait_tasks({replacement})
        assert server.launch_holds.count == 0
        terminal = _launch_state(server, launch_id)
        assert terminal is not None
        assert (terminal.state, terminal.generation, terminal.operation) == (State.TERMINAL, 3, Operation.NONE)
        assert not terminal.binding_reserved and not terminal.exit_confirmed
        assert server.bus.client.strlen(key) == 0

        exited = await client.post(item + "/exit", headers=headers, json={"epoch": prepared["epoch"]})
        retired_as_expected = exited.json() == {"retired": True, **prepared}
        assert exited.status_code == 200 and retired_as_expected and exited.headers["cache-control"] == "no-store"
        assert _launch_state(server, launch_id) is None
        with server.db.connection() as connection:
            counts = connection.execute("SELECT entries, bindings FROM launch_registry WHERE singleton = 1").fetchone()
        assert counts == (0, 0)
        assert server.bus.client.strlen(key) == 0


@contextmanager
def _pause_after_reservation(monkeypatch, authority, method):
    owner = type(authority)
    original = getattr(owner, method)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def reserve(instance, *args, **kwargs):
        publication = original(instance, *args, **kwargs)
        if instance is authority:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise RuntimeError("test did not release the bounded reservation barrier")
        return publication

    with monkeypatch.context() as observe:
        observe.setattr(owner, method, reserve)
        try:
            yield entered
        finally:
            release.set()


def _mutation_tickets(server, issuer, launch_id):
    return [
        ticket
        for ticket in server.launch_mutations._slots
        if ticket is not None and ticket.issuer == issuer and ticket.target == launch_id
    ]


async def _open_for_fault(server, client, base, headers, issuer, prepared):
    opened = await client.post(base + "/launches", headers=headers, json=_FAULT_REGISTRATION)
    key, _, _ = _assert_pending_launch(server, opened, issuer, bytes.fromhex(prepared["epoch"]))
    return key, opened.json()


async def _cancel_turn(task):
    assert task.cancel()
    loop = asyncio.get_running_loop()
    checkpoint = loop.create_future()
    loop.call_soon(checkpoint.set_result, None)
    await checkpoint


async def _exercise_pre_ack_cancel(server, app, client, base, headers, issuer, prepared, stores, monkeypatch):
    stable, opened = await _open_for_fault(server, client, base, headers, issuer, prepared)
    launch_id = bytes.fromhex(opened["launch_id"])
    item = base + "/launches/" + launch_id.hex()
    baseline = server.launch_mutations.retained_bytes
    submitted = len(stores)
    authority = server.launch_authority
    assert authority is not None
    ticket = actual = None
    async with _start_held_control(app, item + "/hold", headers["x-latet-token"], 0, allow_cancelled=True) as held:
        with _pause_after_reservation(monkeypatch, authority, "reserve_hold") as entered:
            await asyncio.wait_for(entered.wait(), 5)
            (ticket,) = _mutation_tickets(server, issuer, launch_id)
            actual = ticket.actual
            assert actual is not None and actual is stores[-1] and ticket.claimed
            state = _launch_state(server, launch_id)
            assert state is not None
            assert (state.state, state.generation, state.operation) == (State.PENDING, 0, Operation.HOLD)
            await _cancel_turn(held.task)
            assert not held.task.done() and not held.body_ready.is_set()
            assert actual.running() and not actual.cancelled()
            assert server.launch_holds.count == 1 and server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES
            assert len(stores) == submitted + 1
            server.launch_mutations.stop()
            with pytest.raises(RuntimeError, match="launch controls have not drained"):
                server.launch_mutations.require_drained()
            await _cancel_turn(held.task)
            assert not held.task.done() and not held.body_ready.is_set()
            assert ticket.actual is actual and actual.running() and not actual.cancelled()
            assert server.launch_holds.count == 1 and server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES
            assert len(stores) == submitted + 1
    assert held.task.cancelled() and not held.body_ready.is_set()
    assert ticket is not None and actual is not None
    assert actual.done() and not actual.cancelled()
    assert len(stores) == submitted + 2
    assert ticket.actual is None and ticket.driver is None
    assert server.launch_holds.count == 0
    terminal = _launch_state(server, launch_id)
    assert terminal is not None
    assert (terminal.state, terminal.generation, terminal.operation) == (State.TERMINAL, 1, Operation.NONE)
    assert not terminal.binding_reserved and not terminal.exit_confirmed
    assert server.bus.client.strlen(stable) == 0
    assert server.launch_mutations.retained_bytes == baseline
    server.launch_mutations.require_drained()


async def _exercise_recovery_contention(server, app, client, base, headers, issuer, prepared, stores, monkeypatch):
    _, opened = await _open_for_fault(server, client, base, headers, issuer, prepared)
    launch_id = bytes.fromhex(opened["launch_id"])
    item = base + "/launches/" + launch_id.hex()
    authority = server.launch_authority
    assert authority is not None
    async with _held_control(app, item + "/hold", headers["x-latet-token"], 0) as (held, disconnect):
        (old,) = _mutation_tickets(server, issuer, launch_id)
        assert not old.claimed and old.driver is not None
        submitted = len(stores)
        recovering = None
        intent = None
        try:
            with _pause_after_reservation(monkeypatch, authority, "recover_current") as entered:
                recovering = asyncio.create_task(
                    client.post(
                        base + "/launches/recover",
                        headers=headers,
                        json={
                            **_FAULT_REGISTRATION,
                            **prepared,
                            "key": opened["key"],
                            "launch_id": launch_id.hex(),
                        },
                    )
                )
                await asyncio.wait_for(entered.wait(), 5)
                (active,) = [ticket for ticket in _mutation_tickets(server, issuer, launch_id) if ticket.claimed]
                actual = active.actual
                assert actual is not None and actual is stores[-1] and actual.running()
                pending = _launch_state(server, launch_id)
                assert pending is not None
                assert (pending.state, pending.generation, pending.operation) == (State.LIVE, 1, Operation.RECOVER)
                disconnect.set()
                await _wait_tasks({held})
                assert server.launch_holds.count == 0 and old.driver is None and not old.claimed
                assert old.outcome is not None and isinstance(old.outcome.retained, TerminalIntent)
                intent = old.outcome.retained
                assert intent.launch_id == launch_id and intent.generation == 1 and intent.operation is None
                assert intent.prior is None and not intent.exit_confirmed
                assert len(stores) == submitted + 1 and active.actual is actual and actual.running()
                assert _launch_state(server, launch_id) == pending
                for action, body in (("end", {}), ("exit", {"epoch": prepared["epoch"]})):
                    refused = await client.post(item + "/" + action, headers=headers, json=body)
                    unavailable = refused.json() == {"error": "launch-unavailable"}
                    assert refused.status_code == 503 and unavailable and refused.headers["cache-control"] == "no-store"
                    assert _launch_state(server, launch_id) == pending
                    assert len(stores) == submitted + 1
                    assert server.launch_mutations.issuer_bytes(issuer) == 2 * MUTATION_BYTES
        finally:
            if recovering is not None:
                await _wait_tasks({recovering})
        assert recovering is not None and intent is not None
        recovered = recovering.result()
        same_binding = recovered.json() == {
            "launch_id": launch_id.hex(),
            "generation": 2,
            "binding": opened["binding"],
        }
        assert recovered.status_code == 200 and same_binding and recovered.headers["cache-control"] == "no-store"
        current = _launch_state(server, launch_id)
        assert current is not None
        assert (current.state, current.generation, current.operation) == (State.LIVE, 2, Operation.NONE)
        assert old.outcome is not None and old.outcome.retained is intent and not old.claimed
        assert server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES
        assert server.launch_holds.count == 0
        server.launch_mutations.require_drained()


@contextmanager
def _observe_binding_wire(monkeypatch, server, key, credential, *, lose=None, confirm_delete=False):
    connection_type = server.bus.client.connection_pool.connection_class
    original_send, original_read = connection_type.send_packed_command, connection_type.read_response
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    counts = {"SET": 0, "DEL": 0, "lost": 0}
    armed = None
    markers = {
        command: b"*%d\r\n$3\r\n%b\r\n$%d\r\n%b\r\n" % (arity, command.encode("ascii"), len(key), key)
        for command, arity in (("SET", 5), ("DEL", 2))
    }
    prefix_size = max(map(len, markers.values()))
    canaries = (credential.encode("ascii"), b"/client/work/child", b"/client/work")

    def send(connection, chunks, *args, **kwargs):
        nonlocal armed
        # The pinned stock serializer keeps this short command/key prefix in its first chunk.
        if type(chunks) is bytes:
            head = chunks[:prefix_size]
        elif isinstance(chunks, (list, tuple)) and chunks:
            head = bytes(chunks[0][:prefix_size])
        else:
            head = b""
        if type(connection) is connection_type:
            for command, marker in markers.items():
                if head.startswith(marker):
                    counts[command] += 1
                    packed = chunks if isinstance(chunks, bytes) else b"".join(chunks)
                    clean = all(canary not in packed for canary in canaries)
                    assert clean
                    if (command == lose and counts["lost"] == 0) or (confirm_delete and command == "DEL"):
                        assert armed is None
                        armed = connection
                    break
        return original_send(connection, chunks, *args, **kwargs)

    def read(connection, *args, **kwargs):
        nonlocal armed
        reply = original_read(connection, *args, **kwargs)
        if connection is armed:
            armed = None
            expected = type(reply) is bytes and reply == b"OK" if lose == "SET" else type(reply) is int and reply == 1
            assert expected
            if confirm_delete:
                loop.call_soon_threadsafe(event.set)
                return reply
            counts["lost"] += 1
            loop.call_soon_threadsafe(event.set)
            raise redis.ConnectionError("synthetic lost launch mutation reply")
        return reply

    with monkeypatch.context() as observe:
        observe.setattr(connection_type, "send_packed_command", send)
        observe.setattr(connection_type, "read_response", read)
        yield counts, event


@contextmanager
def _lose_retirement_commit(monkeypatch, authority):
    authority_type = type(authority)
    original_retire, original_execute = authority_type.retire, db_module._Connection._exec_command
    active_thread = None
    counts = {"retire": 0, "lost": 0}

    def retire(instance, *args, **kwargs):
        nonlocal active_thread
        if instance is not authority:
            return original_retire(instance, *args, **kwargs)
        counts["retire"] += 1
        active_thread = threading.get_ident()
        try:
            return original_retire(instance, *args, **kwargs)
        finally:
            active_thread = None

    def execute(connection, command, *args, **kwargs):
        answer = yield from original_execute(connection, command, *args, **kwargs)
        if command == b"COMMIT" and active_thread == threading.get_ident() and counts["lost"] == 0:
            counts["lost"] += 1
            raise psycopg.OperationalError("synthetic lost launch retirement commit acknowledgment")
        return answer

    with monkeypatch.context() as fault:
        fault.setattr(authority_type, "retire", retire)
        fault.setattr(db_module._Connection, "_exec_command", execute)
        yield counts


def _unavailable(response):
    fixed = response.json() == {"error": "launch-unavailable"}
    assert response.status_code == 503 and fixed and response.headers["cache-control"] == "no-store"


def _unknown_operation(ticket, state, kind):
    assert ticket.claimed and ticket.actual is None and ticket.driver is None
    assert ticket.outcome is not None and isinstance(ticket.outcome.retained, TerminalIntent)
    intent = ticket.outcome.retained
    assert isinstance(intent.prior, Completion)
    completion = intent.prior
    assert completion.wire.remote == RemoteResult.UNKNOWN and not completion.settled
    assert state is not None and state.state == State.TERMINAL and state.binding_reserved
    assert state.operation == completion.ticket.kind == kind and state.sequence == completion.ticket.sequence
    assert state.operation_id == completion.ticket.operation
    return intent


def _assert_detached_driver(old, tickets, issuer_bytes):
    assert old.actual is None and old.driver is None
    assert len(tickets) in (1, 2) and issuer_bytes == len(tickets) * MUTATION_BYTES
    if old in tickets:
        assert not old.claimed and old.outcome is not None and isinstance(old.outcome.retained, TerminalIntent)
        assert old.outcome.retained.generation == 1 and old.outcome.retained.operation is None


async def _exercise_refresh_unknown(
    server, held, disconnect, client, item, headers, issuer, launch_id, prepared, stores, event
):
    await asyncio.wait_for(event.wait(), 30)
    disconnect.set()
    await _wait_tasks({held})
    state = _launch_state(server, launch_id)
    (resident,) = _mutation_tickets(server, issuer, launch_id)
    intent = _unknown_operation(resident, state, Operation.REFRESH)
    assert isinstance(intent.prior, Completion) and intent.operation is intent.prior.ticket
    assert server.launch_holds.count == 0 and server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES
    submitted = len(stores)
    for action, body in (("end", {}), ("exit", {"epoch": prepared["epoch"]})):
        _unavailable(await client.post(item + "/" + action, headers=headers, json=body))
        assert len(stores) == submitted and _launch_state(server, launch_id) == state
        assert server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES


async def _exercise_terminal_unknown(
    server, held, client, item, headers, issuer, launch_id, prepared, stable, stores, monkeypatch, mode, wire
):
    (old,) = _mutation_tickets(server, issuer, launch_id)
    if mode == "end_delete_unknown":
        ended = await client.post(item + "/end", headers=headers, json={})
        confirmed = ended.json() == {"ended": True}
        assert ended.status_code == 200 and confirmed and ended.headers["cache-control"] == "no-store"
    else:
        authority = server.launch_authority
        assert authority is not None
        with _lose_retirement_commit(monkeypatch, authority) as commits:
            exited = await client.post(item + "/exit", headers=headers, json={"epoch": prepared["epoch"]})
        _unavailable(exited)
        assert commits == {"retire": 1, "lost": 1}
    await _wait_tasks({held})
    assert server.launch_holds.count == 0 and server.bus.client.strlen(stable) == 0
    tickets = _mutation_tickets(server, issuer, launch_id)
    _assert_detached_driver(old, tickets, server.launch_mutations.issuer_bytes(issuer))
    (explicit,) = [ticket for ticket in tickets if ticket is not old]
    state = _launch_state(server, launch_id)
    if mode == "end_delete_unknown":
        _unknown_operation(explicit, state, Operation.DELETE)
        submitted = len(stores)
        _unavailable(await client.post(item + "/exit", headers=headers, json={"epoch": prepared["epoch"]}))
        assert len(stores) == submitted and _launch_state(server, launch_id) == state
        assert server.launch_mutations.issuer_bytes(issuer) == len(tickets) * MUTATION_BYTES
    else:
        assert state is None and explicit.claimed and explicit.actual is None
        assert explicit.outcome is not None and explicit.outcome.uncertain
        intent = explicit.outcome.retained
        assert isinstance(intent, TerminalIntent) and intent.exit_confirmed and intent.launch_id == launch_id
        with server.db.connection() as connection:
            counts = connection.execute("SELECT entries, bindings FROM launch_registry WHERE singleton = 1").fetchone()
        assert counts == (0, 0)
        assert wire == {"SET": 0, "DEL": 1, "lost": 0}
        prior = intent.prior
        assert isinstance(prior, Completion)
        assert prior.settled and prior.wire.remote == RemoteResult.DELETE and prior.wire.cleanup_pending is None
        assert prior.ticket.kind == Operation.DELETE and prior.ticket.launch_id == launch_id
        assert binding_key(server.bus, prior.ticket.epoch, prior.ticket.locator) == stable


async def _exercise_rejected_recoveries(
    server, client, base, headers, issuer, launch_id, prepared, opened, stable, stores
):
    state = _launch_state(server, launch_id)
    assert state is not None and state.state == State.LIVE and state.operation == Operation.NONE
    fingerprint = _sealed_fingerprint(server, stable)
    (resident,) = _mutation_tickets(server, issuer, launch_id)
    original = {**_FAULT_REGISTRATION, **prepared, "key": opened["key"], "launch_id": launch_id.hex()}
    alternate_key = "latet_lk_" + ("A" * 43 if original["key"] != "latet_lk_" + "A" * 43 else "E" * 43)
    alternate_id = "0" * 32 if original["launch_id"] != "0" * 32 else "1" * 32
    alternate_epoch = "0" * 32 if original["epoch"] != "0" * 32 else "1" * 32
    assert _canonical_key(alternate_key)
    for field, value in (
        ("key", alternate_key),
        ("launch_id", alternate_id),
        ("epoch", alternate_epoch),
        ("retirement", True),
        ("folder", "/client/work/other"),
        ("root", "/client"),
    ):
        different = original[field] != value
        assert different
        submitted = len(stores)
        response = await client.post(base + "/launches/recover", headers=headers, json={**original, field: value})
        if field == "retirement":
            invalid = response.json() == {"error": "launch-invalid"}
            assert response.status_code == 400 and invalid and response.headers["cache-control"] == "no-store"
            assert len(stores) == submitted
        else:
            _unavailable(response)
            assert len(stores) == submitted + 1
        assert _launch_state(server, launch_id) == state
        unchanged = _sealed_fingerprint(server, stable) == fingerprint
        assert unchanged
        assert _mutation_tickets(server, issuer, launch_id) == [resident]
        assert not resident.claimed and resident.actual is None
        assert server.launch_holds.count == 1 and server.launch_mutations.issuer_bytes(issuer) == MUTATION_BYTES


async def _exercise_fault(server, app, client, base, headers, issuer, prepared, stores, monkeypatch, mode):
    stable, opened = await _open_for_fault(server, client, base, headers, issuer, prepared)
    launch_id = bytes.fromhex(opened["launch_id"])
    item = base + "/launches/" + launch_id.hex()
    async with _held_control(app, item + "/hold", headers["x-latet-token"], 0) as (held, disconnect):
        lose = {"refresh_unknown": "SET", "end_delete_unknown": "DEL"}.get(mode)
        with _observe_binding_wire(monkeypatch, server, stable, opened["key"], lose=lose) as (wire, event):
            if mode == "refresh_unknown":
                await _exercise_refresh_unknown(
                    server, held, disconnect, client, item, headers, issuer, launch_id, prepared, stores, event
                )
                assert wire == {"SET": 1, "DEL": 0, "lost": 1}
            elif mode == "recovery_rejected":
                await _exercise_rejected_recoveries(
                    server, client, base, headers, issuer, launch_id, prepared, opened, stable, stores
                )
                assert wire == {"SET": 0, "DEL": 0, "lost": 0}
            else:
                await _exercise_terminal_unknown(
                    server,
                    held,
                    client,
                    item,
                    headers,
                    issuer,
                    launch_id,
                    prepared,
                    stable,
                    stores,
                    monkeypatch,
                    mode,
                    wire,
                )
                assert wire == {"SET": 0, "DEL": 1, "lost": int(lose is not None)}


@pytest.mark.parametrize(
    "mode",
    [
        "prepare",
        "open",
        "ingress_guard",
        "capacity",
        "shared_capacity",
        "lifecycle",
        "pre_ack_cancel",
        "recovery_claim",
        "refresh_unknown",
        "end_delete_unknown",
        "exit_retire_commit_unknown",
        "recovery_rejected",
    ],
)
async def test_real_server_launch_control(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, mode):
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        migrate(database, master=MasterKey.load(settings))
    finally:
        database.close()
    server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")

    # Observe actual executor futures for this loop, including default and index work.
    loop_thread = threading.get_ident()
    work = []
    stores = []
    original_submit = ThreadPoolExecutor.submit

    def submit(executor, fn, /, *args, **kwargs):
        if threading.get_ident() != loop_thread:
            return original_submit(executor, fn, *args, **kwargs)
        if len(work) == _WORK_LIMIT:
            raise RuntimeError("unexpected Server work inventory")
        actual = original_submit(executor, fn, *args, **kwargs)
        work.append(actual)
        if executor is server_machines._STORE:
            stores.append(actual)
        return actual

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    workers = set()
    closing = set()
    original_close_later = server.spaces._close_later

    def close_later(spaces, words=None):
        workers.update(space.worker for space in spaces if space.worker is not None)
        original_close_later(spaces, words)
        closing.update(server.spaces._closing)

    monkeypatch.setattr(server.spaces, "_close_later", close_later)
    listener = None
    try:
        server.shared.set(Config(ocr="off", history="off"), actor="server")
        identity, secret = server.identities.add_key("P26a launch fixture")
        key = root_key(server.db, server.master, identity.id, clock=server.clock)
        identity_settings = PgSettings(server.db, server.shared, identity.id, key, kind="key")
        configured = identity_settings.prepare(identity_settings.load().model_copy(update={"ner": "off"}))
        identity_settings.save(configured)
        identity_settings.save_reviewed(configured)
        app = create_server_app(server)
        response_charges = []

        async def observed_app(scope, receive, send):
            if scope["method"] != "POST" or not scope["path"].endswith("/launches"):
                await app(scope, receive, send)
                return

            async def observed_send(message):
                if message["type"] == "http.response.body":
                    response_charges.append(server.launch_mutations.issuer_bytes(identity.id))
                await send(message)
                if message["type"] == "http.response.body":
                    response_charges.append(server.launch_mutations.issuer_bytes(identity.id))

            await app(scope, receive, observed_send)

        listener = asyncio.create_task(listen(server))
        async with asyncio.timeout(45 if mode in ("lifecycle", "refresh_unknown") else _DRAIN_SECONDS):
            await server.listening.wait()
            base = f"/u/{route_key(secret)}/_latet/engine/v1"
            headers = {"x-latet-token": secret}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=observed_app), base_url=URL, trust_env=False
            ) as client:
                ping = await client.get(base + "/ping", headers=headers)
                assert ping.status_code == 200 and ping.json()["ok"] is True
                space = server.spaces._spaces[identity.id]
                config = space.runtime.config
                assert (config.ner, config.ocr, config.history) == ("off", "off", "off")
                prepared = await client.get(base + "/launches/prepare", headers=headers)
                unavailable = (
                    prepared.status_code == 503
                    and prepared.json() == {"error": "launch-unavailable"}
                    and prepared.headers.get("cache-control") == "no-store"
                )
                assert prepared.status_code == 200, (
                    "launch-unavailable" if unavailable else "unexpected prepare response"
                )
                assert prepared.headers["cache-control"] == "no-store"
                data = prepared.json()
                assert set(data) == {"epoch", "retirement"} and type(data["retirement"]) is int
                assert data["retirement"] == 0
                assert type(data["epoch"]) is str and len(data["epoch"]) == 32
                assert bytes.fromhex(data["epoch"]).hex() == data["epoch"]
                with server.db.connection() as connection:
                    registered = connection.execute("SELECT epoch FROM launch_registry WHERE singleton = 1").fetchone()
                assert registered is not None and registered[0] == bytes.fromhex(data["epoch"])
                if mode == "lifecycle":
                    await _exercise_lifecycle(server, observed_app, client, base, headers, identity.id, data)
                if mode == "pre_ack_cancel":
                    await _exercise_pre_ack_cancel(
                        server, observed_app, client, base, headers, identity.id, data, stores, monkeypatch
                    )
                if mode == "recovery_claim":
                    await _exercise_recovery_contention(
                        server, observed_app, client, base, headers, identity.id, data, stores, monkeypatch
                    )
                if mode in ("refresh_unknown", "end_delete_unknown", "exit_retire_commit_unknown", "recovery_rejected"):
                    await _exercise_fault(
                        server, observed_app, client, base, headers, identity.id, data, stores, monkeypatch, mode
                    )
                if mode in ("open", "capacity", "shared_capacity"):
                    assert not config.projects
                    registration = {"folder": "/client/work/child", "root": "/client/work", "flavor": "posix"}
                    if mode in ("open", "shared_capacity"):
                        opened = await client.post(base + "/launches", headers=headers, json=registration)
                        before = _assert_pending_launch(server, opened, identity.id, registered[0])
                        assert response_charges == [MUTATION_BYTES, MUTATION_BYTES]
                        assert server.launch_mutations.issuer_bytes(identity.id) == 0
                        if mode == "shared_capacity":
                            fingerprint = _sealed_fingerprint(server, before[0])
                            with monkeypatch.context() as cap:
                                cap.setattr(server_launches, "IDENTITY_LIVE", 1)
                                limited = await client.post(base + "/launches", headers=headers, json=registration)
                            capacity = limited.json() == {"key": None, "launch_id": None, "reason": "launch-capacity"}
                            assert limited.status_code == 200 and capacity
                            assert limited.headers["cache-control"] == "no-store"
                            unchanged = _assert_pending_launch(server, opened, identity.id, registered[0]) == before
                            assert unchanged
                            sealed_unchanged = _sealed_fingerprint(server, before[0]) == fingerprint
                            assert sealed_unchanged
                            keys = scanned_once(server.bus.client.scan_iter(match=server.bus.key("launch:*")), 2)
                            one_stable_key = keys == {before[0]}
                            assert one_stable_key
                            assert response_charges == [MUTATION_BYTES] * 4
                    else:
                        await _wait_tasks(set(server.spaces._index_tasks))
                        baseline = server.spaces._retained_total()
                        budget = server.spaces.budget
                        auth_calls = 0
                        original = server_app.secret_grant

                        async def auth(*args, **kwargs):
                            nonlocal auth_calls
                            auth_calls += 1
                            return await original(*args, **kwargs)

                        try:
                            server.spaces.budget = baseline + MUTATION_BYTES - 1
                            with monkeypatch.context() as observe:
                                observe.setattr(server_app, "secret_grant", auth)
                                limited = await client.post(base + "/launches", headers=headers, json=registration)
                        finally:
                            server.spaces.budget = budget
                        refused = limited.json() == {"error": "launch-capacity"}
                        # The token is checked before a slot is taken (the review's Minor 4): then the full slot.
                        assert (limited.status_code, refused, auth_calls) == (503, True, 1)
                        assert limited.headers["cache-control"] == "no-store"
                        assert response_charges == [0, 0] and server.spaces._retained_total() == baseline
                        with server.db.connection() as connection:
                            row = connection.execute(
                                "SELECT entries, bindings FROM launch_registry WHERE singleton = 1"
                            )
                            assert row.fetchone() == (0, 0)
                    assert server.launch_mutations.issuer_bytes(identity.id) == 0
                if mode == "ingress_guard":
                    auth_calls = space_calls = 0
                    original_auth = server_app.secret_grant
                    original_space = server.spaces._space

                    async def auth(*args, **kwargs):
                        nonlocal auth_calls
                        auth_calls += 1
                        return await original_auth(*args, **kwargs)

                    async def lookup(*args, **kwargs):
                        nonlocal space_calls
                        space_calls += 1
                        return await original_space(*args, **kwargs)

                    with monkeypatch.context() as observe:
                        observe.setattr(server_app, "secret_grant", auth)
                        observe.setattr(server.spaces, "_space", lookup)
                        oversized = client.build_request("GET", base + "/launches/prepare", headers=headers)
                        oversized.headers = httpx.Headers(
                            [
                                ("host", oversized.headers["host"]),
                                ("x-latet-token", secret),
                                *((f"x-test-{index}", "1") for index in range(31)),
                            ]
                        )
                        assert len(oversized.headers.raw) == 33
                        refused = await client.send(oversized)
                    invalid = refused.json() == {"error": "launch-invalid"}
                    no_store = refused.headers.get("cache-control") == "no-store"
                    assert (refused.status_code, invalid, no_store, auth_calls, space_calls) == (400, True, True, 0, 0)
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
            raise RuntimeError("Server still owns live work after drain")
        server.close()
        _clean_owned_namespaces(schema, prefix)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))


async def test_real_beat_cleans_expired_launch_binding(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch
):
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        migrate(database, master=MasterKey.load(settings))
    finally:
        database.close()
    server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")
    loop_thread = threading.get_ident()
    work: list[Future[object]] = []
    store_slots: list[tuple[MutationAdmission, ...]] = []
    beat_calls = []
    original_submit = ThreadPoolExecutor.submit
    mutations = server.launch_mutations
    baseline = mutations.retained_bytes

    def slots():
        return tuple(ticket for ticket in mutations._slots if ticket is not None)

    def submit(executor, fn, /, *args, **kwargs):
        if threading.get_ident() != loop_thread:
            return original_submit(executor, fn, *args, **kwargs)
        if len(work) >= 7:
            raise RuntimeError("unexpected sweep work inventory")
        if executor is server_machines._STORE:
            store_slots.append(slots())
        if executor is server_app._BEATING:
            beat_calls.append(fn)
        actual = original_submit(executor, fn, *args, **kwargs)
        work.append(actual)
        return actual

    third = asyncio.Event()
    blocked = asyncio.Event()
    rounds = 0
    handoff: tuple[MutationAdmission, ...] = ()
    handoff_bytes = baseline

    async def ready():
        nonlocal rounds, handoff, handoff_bytes
        rounds += 1
        if rounds == 2:
            handoff, handoff_bytes = slots(), mutations.retained_bytes
        if rounds >= 3:
            third.set()
            await blocked.wait()
        return server.listening.is_set()

    async def drain_beat(task):
        mutations.stop()
        task.cancel()
        await _wait_tasks({task}, allow_cancelled=True)
        await _wait_tasks({asyncio.wrap_future(actual) for actual in work})

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    listener = beating = None
    beat_drained = False
    try:
        authority = server.launch_authority
        assert authority is not None
        issuer = identity_in(server.db)
        codec = BindingCodec(root_key(server.db, server.master, issuer, clock=server.clock))
        credential = b"latet_lk_" + b"A" * 43
        launch_id = b"s" * 16
        locator = authority.locator(credential)
        binding = Binding("/client/work/child", "/client/work", project_id("/client/work"), "client", "posix")
        published = authority.reserve_open(issuer, locator, launch_id, binding, codec.verifier(credential), codec)
        completion = authority.submit(published)
        assert completion.settled and completion.wire.remote == RemoteResult.SET
        assert completion.wire.cleanup_pending is None and completion.receipt is not None
        stable = binding_key(server.bus, completion.ticket.epoch, locator)
        initial, _ = authority._snapshot(locator)
        assert initial is not None and initial.state == State.PENDING and initial.op_kind == Operation.NONE
        assert initial.disclosure_possible and initial.binding_reserved

        def expire(connection, _registry, _retry):
            _, row = authority._locked(connection, locator, issuer)
            assert row == initial and row is not None
            authority._save(connection, replace(row, deadline_us=0))

        authority._transaction(expire)
        expired, now = authority._snapshot(locator)
        assert expired == replace(initial, deadline_us=0, revision=initial.revision + 1)
        assert expired is not None and expired.deadline_us == 0 <= now
        listener = asyncio.create_task(listen(server))
        with _observe_binding_wire(monkeypatch, server, stable, credential.decode("ascii"), confirm_delete=True) as (
            wire,
            delete_reply,
        ):
            try:
                async with asyncio.timeout(35):
                    await server.listening.wait()
                    assert 30 <= server.bus.client.strlen(stable) <= BINDING_BYTES
                    beating = asyncio.create_task(server_app.beat(server, ready=ready))
                    await third.wait()
            finally:
                if beating is not None:
                    await drain_beat(beating)
                    beat_drained = True
            assert rounds == 3 and beat_calls == [server_app._alive, server_app._alive]
            assert len(work) == 4 + len(store_slots)
            key_count = len(scanned_once(server.bus.client.scan_iter(match=prefix + "*", count=100), 6))
            assert key_count <= 5
            assert not server.spaces._spaces and not server.spaces._building and not server.spaces._closing
            assert not server.spaces._index_tasks and not server._cleanups and server._cleaning is None
            final, _ = authority._snapshot(locator)
            swept = (
                final is not None
                and final.state == State.TERMINAL
                and final.terminal_reason == Reason.EXPIRED
                and final.op_kind == Operation.NONE
                and not final.binding_reserved
            )
            assert swept, "signed expired launch remains reserved instead of swept"
            assert final is not None
            assert (final.launch_id, final.locator, final.issuer, final.generation) == (launch_id, locator, issuer, 0)
            assert final.binding_digest == expired.binding_digest
            assert final.disclosure_possible and not final.exit_confirmed
            assert final.published_token is None and final.published_blob_digest is None

            def protected_counts(connection, registry, _retry):
                counter, row = authority._locked(connection, locator, issuer)
                assert row == final and counter is not None
                return registry.entries, registry.bindings, counter.entries, counter.bindings

            assert authority._transaction(protected_counts) == (1, 0, 1, 0)
            assert server.bus.client.strlen(stable) == 0 and delete_reply.is_set()
            assert wire == {"SET": 0, "DEL": 1, "lost": 0}
            assert len(handoff) == 1 and handoff_bytes == baseline + MUTATION_BYTES
            assert store_slots == [handoff, handoff]
            assert not slots() and mutations.retained_bytes == baseline
    finally:
        mutations.stop()
        if beating is not None:
            if not beat_drained:
                raise RuntimeError("actual sweep work did not drain; retaining owned namespaces")
            gone = asyncio.get_running_loop().run_in_executor(server_app._BEATING, server_app._gone, server)
            await _wait_tasks({gone})
        if listener is not None:
            listener.cancel()
            await _wait_tasks({listener}, allow_cancelled=True)
        await _wait_tasks({asyncio.wrap_future(actual) for actual in work})
        await mutations.drain()
        await server.spaces.close()
        if server._ear is not None or server.spaces._spaces or server.spaces._closing or server.spaces._building:
            raise RuntimeError("sweep fixture still owns live Server work")
        server.close()
        _clean_owned_namespaces(schema, prefix, key_limit=5)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))
