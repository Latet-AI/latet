"""One actual protected HOLD replacement and detached drain ownership witness."""

from __future__ import annotations

import asyncio
import decimal
import json
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import _base as futures_base
from concurrent.futures import thread as futures_thread
from functools import partial
from types import CodeType, CoroutineType, FunctionType
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.exceptions import HTTPException, WebSocketException

from latet.bridge import api as bridge_api
from latet.config import Config
from latet.gateway.launch_keys import HOLD_BYTES
from latet.server import app as server_app
from latet.server import launch_lifecycle, launch_mutations
from latet.server import machines as server_machines
from latet.server import spaces as server_spaces
from latet.server.app import create_server_app, listen, open_server
from latet.server.db import Database, migrate
from latet.server.launch_binding import RemoteResult, WireResult
from latet.server.launch_controls import LaunchHoldTicket
from latet.server.launch_lifecycle import ProtectedLaunchControls
from latet.server.launch_mutations import MUTATION_BYTES, MutationAdmission, TerminalIntent
from latet.server.launch_records import Operation, State
from latet.server.launches import Completion, LaunchAuthority
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from ..unit.test_launch_keys import _LaunchGraph
from .support import DATABASE, URL
from .test_launch_server_adapter import (
    _WORK_LIMIT,
    _assert_pending_launch,
    _clean_owned_namespaces,
    _launch_state,
    _wait_tasks,
)
from .test_launch_server_adapter import _prove_owned_launch_namespaces as _prove_owned_launch_namespaces
from .test_launch_short_graph import _SHARED_MODULES, _app_boundaries
from .test_launch_store_tail import _STORE_TYPES, _WORK_CODES, _Gate, _worker_boundary

pytestmark = pytest.mark.server
_RESERVE = 8192
_TERMINAL_CODES = (
    launch_lifecycle._terminal.__code__,
    launch_lifecycle._control.__code__,
    launch_lifecycle._run_control.__code__,
    launch_mutations._invoke.__code__,
)
_PRIVATE = (
    *_STORE_TYPES,
    launch_lifecycle._ProtectedHold,
    launch_lifecycle._Control,
    launch_lifecycle._ControlHandoff,
    LaunchHoldTicket,
    server_spaces._Sent,
    TerminalIntent,
    Completion,
    WireResult,
    RemoteResult,
    asyncio.Timeout,
    vars(asyncio.timeouts)["_State"],
)


def _frame(task, code):
    current = task.get_coro()
    for _ in range(64):
        if type(current) is not CoroutineType:
            break
        if current.cr_code is code:
            assert current.cr_frame is not None
            return current.cr_frame
        current = current.cr_await
    raise AssertionError("actual hold coroutine owner missing")


def _start_hold(app, path, token, generation):
    parsed = urlsplit(URL)
    headers = [(b"host", parsed.netloc.encode("ascii")), (b"x-latet-token", token.encode("ascii"))]
    headers.extend((f"x-hold-pad-{index:02}".encode("ascii"), b"p") for index in range(29))
    last = b"x-hold-pad-last"
    remaining = 4096 - sum(len(name) + len(value) for name, value in headers) - len(last)
    assert remaining > 0
    headers.append((last, b"p" * remaining))
    query = f"generation={generation:020d}".encode("ascii") + b"&" * 33
    assert len(headers) == 32 and sum(len(name) + len(value) for name, value in headers) == 4096
    assert len(query) == 64 and len(path.encode("ascii")) <= 256
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": parsed.scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": (parsed.hostname, parsed.port or 443),
    }
    acknowledged, receiving, disconnect = (asyncio.Event() for _ in range(3))
    initial = True
    status = 0
    ack = False

    async def receive():
        nonlocal initial
        if initial:
            initial = False
            return {"type": "http.request", "body": b"", "more_body": False}
        receiving.set()
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status, ack
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body" and not acknowledged.is_set():
            body = message.get("body", b"")
            assert len(body) <= 128
            ack = bool(message.get("more_body")) and json.loads(body) == {"held": True, "generation": generation + 1}
            acknowledged.set()

    async def ready():
        await asyncio.wait_for(acknowledged.wait(), 3)
        assert status == 200 and ack
        await asyncio.wait_for(receiving.wait(), 3)

    task = asyncio.ensure_future(app(scope, receive, send))
    return task, disconnect, ready, (receive, send)


def _physical_owner(task, *, drained=False):
    if drained:
        frame = _frame(task, bridge_api._launch_hold_drained.__code__)
        lease = frame.f_locals["lease"]
        assert type(lease) is launch_lifecycle._ProtectedHold
        assert not frame.f_locals["tasks"] and "settled" not in frame.f_locals
        waiter = getattr(task, "_fut_waiter", None)
        assert waiter is not None and any(waiter is found for found in lease._ticket._ready._waiters)
        callbacks = getattr(waiter, "_callbacks", ())
        assert any(getattr(callback, "__self__", None) is task for callback, _context in callbacks)
        return lease, (task, lease._physical)
    address = _frame(task, server_app.ServerGate._address.__code__)
    authenticated = _frame(task, server_app.ServerGate._authenticated_address.__code__)
    bridged = _frame(task, server_app.ServerGate._bridge_call.__code__)  # its slot taken once authenticated (Minor 4)
    served = _frame(task, server_app.ServerGate._serve.__code__)
    assert (
        address.f_locals["scope"]
        is authenticated.f_locals["scope"]
        is bridged.f_locals["scope"]
        is served.f_locals["scope"]
    )
    outer = _frame(task, bridge_api._LaunchHold.__call__.__code__)
    response = outer.f_locals["self"]
    assert type(response) is bridge_api._LaunchHold
    lease = response.lease
    assert type(lease) is launch_lifecycle._ProtectedHold
    assert served.f_locals["ticket"] is lease._physical
    assert bridged.f_locals["mutation"] is lease._ticket
    helpers = tuple(outer.f_locals["tasks"])
    assert len(helpers) == 2 and all(type(helper) is asyncio.Task and not helper.done() for helper in helpers)
    assert response.drain_task is None
    return lease, (task, response, lease._physical, *helpers)


def _paired(owners, space, shared, driver_codes, observer_code, counted_code, gate, retired):
    boundary = dict(shared)
    physical = []
    charges = []
    live_tasks = set()
    for task, detached in owners:
        lease, roots = _physical_owner(task, drained=detached)
        if not detached:
            counted_frame = _frame(task, counted_code)
            served = _frame(task, server_app.ServerGate._serve.__code__)
            held_frame = _frame(task, server_spaces._held.__code__)
            assert counted_frame.f_locals["scope"] is served.f_locals["inner"] is held_frame.f_locals["scope"]
            assert held_frame.f_locals["space"] is space and space.long_scopes[task] is served.f_locals["inner"]
            live_tasks.add(task)
        assert type(lease) is launch_lifecycle._ProtectedHold
        ticket = lease._ticket
        assert type(ticket) is MutationAdmission and ticket.driver is lease
        assert ticket.owner._slots[ticket.slot] is ticket
        assert type(lease._physical) is LaunchHoldTicket and lease._physical._owner is not None
        assert lease._physical._adopted
        charges.append(ticket)
        physical.extend(roots)
    assert len({ticket.slot for ticket in charges}) == len(charges)
    assert space.long == live_tasks and set(space.long_scopes) == live_tasks
    physical.extend((space.long, space.long_scopes))
    crosses = {id(root): root for root in physical}
    gate_roots = ()
    if retired is not None:
        caller = gate.frame
        assert caller is not None and gate.thread_id is not None and gate.thread_id != threading.get_ident()
        assert caller.f_code is launch_lifecycle._terminal.__code__
        assert caller.f_locals["intent"] is gate.roots[0]
        worker = caller
        for _ in _TERMINAL_CODES:
            assert worker is not None
            worker = worker.f_back
        assert worker is not None
        item = worker.f_locals["self"]
        assert type(item) is futures_thread._WorkItem
        _worker_boundary(caller, _TERMINAL_CODES, item, gate.thread_id, boundary)
        actual = item.future
        assert retired is charges[0] and retired.actual is actual and actual.running()
        assert retired.pending and not retired.ingress
        (invocation,) = item.args
        assert item.fn is launch_mutations._invoke and type(invocation) is launch_mutations._Invocation
        assert invocation.fn is invocation.argument is invocation.result is None
        marker = retired._tail
        assert marker is not None and marker is invocation.marker and marker() is invocation
        assert marker.__callback__ is None
        notifier = invocation.notify
        assert type(notifier) is partial
        notify_partial = cast(partial, notifier)
        assert getattr(notify_partial.func, "__self__", None) is retired
        assert getattr(notify_partial.func, "__func__", None) is MutationAdmission._tail_done
        (callback,) = vars(actual)["_done_callbacks"]
        assert type(callback) is partial and getattr(callback.func, "__self__", None) is retired
        assert getattr(callback.func, "__func__", None) is MutationAdmission._completed
        gate_roots = (caller, item, invocation)

    signal_dict = type(decimal.DefaultContext.flags)
    assert type(decimal.DefaultContext.traps) is signal_dict
    assert (signal_dict.__module__, signal_dict.__qualname__) == ("abc", "SignalDict")
    sending = tuple(
        code for code in server_spaces._held.__code__.co_consts if type(code) is CodeType and code.co_name == "sending"
    )
    assert len(sending) == 1
    graph_args = {
        "driver_codes": driver_codes,
        "private_frame_codes": (
            counted_code,
            server_spaces._held.__code__,
            *sending,
            *_TERMINAL_CODES,
            *_WORK_CODES,
        ),
        "observer_codes": (
            _LaunchGraph.measure.__code__,
            _paired.__code__,
            observer_code,
            test_protected_hold_partitions_replacement_and_actual_drain.__code__,
        ),
        "private_types": (*_PRIVATE, signal_dict),
        "shared_modules": (
            *_SHARED_MODULES,
            "latet.server.launch_controls",
            "latet.server.launch_lifecycle",
            "asyncio.timeouts",
        ),
        # A coroutine on a held task's own await chain may name its module-level latet.* function (spaces._held):
        # recorded, never descended into. Bound methods and library functions are not covered by this rule.
        "await_chain_functions": True,
    }
    census = {}
    physical_census = {}
    mutations = []
    cross_labels = {root_id: f"physical cross {index}" for index, root_id in enumerate(crosses)}
    try:
        for index, ticket in enumerate(charges):
            cuts = boundary | cross_labels
            cuts.update({key: "already counted mutation alias" for key in census})
            roots = (ticket, *gate_roots) if index == 0 else (ticket,)
            measured, _ = _LaunchGraph(cuts, **graph_args).measure(roots, census=census)
            assert id(ticket) in census and id(ticket.driver) in census
            mutations.append(measured)
        cuts = boundary | {key: "counted mutation owner" for key in census}
        measured, _ = _LaunchGraph(cuts, **graph_args).measure(physical, census=physical_census)
        assert all(root_id in physical_census for root_id in crosses)
        assert not (census.keys() & physical_census.keys())
        assert len(census) + len(physical_census) <= 4096
        assert measured["bytes"] + sum(item["bytes"] for item in mutations) <= 262144
        return {"physical": measured, "mutations": mutations}
    finally:
        physical_census.clear()
        census.clear()


async def test_protected_hold_partitions_replacement_and_actual_drain(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        migrate(database, master=MasterKey.load(settings))
    finally:
        database.close()
    server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    gate = _Gate(loop.call_soon_threadsafe)
    original_submit = ThreadPoolExecutor.submit
    original_close = server.spaces._close_later
    original_terminal = LaunchAuthority.terminal_control
    work = []
    total_submissions = 0
    old_future_ref = None
    old_future_gone = asyncio.Event()
    workers = set()
    closing = set()
    holds = []
    transport = []
    observed = {}
    rejected = {}
    listener = None
    drain_ref = None
    launch_id = None
    ledger = server.launch_mutations
    baseline = ledger.retained_bytes

    def submit(executor, fn, /, *args, **kwargs):
        nonlocal total_submissions, old_future_ref
        if threading.get_ident() != loop_thread:
            return original_submit(executor, fn, *args, **kwargs)
        if total_submissions >= _WORK_LIMIT:
            raise RuntimeError("unexpected HOLD work inventory")
        total_submissions += 1
        if executor is not server_machines._STORE or fn is not launch_mutations._invoke:
            actual = original_submit(executor, fn, *args, **kwargs)
            work.append(actual)
            return actual
        (invocation,) = args
        handoff = invocation.argument
        control_work = handoff.work if type(handoff) is launch_lifecycle._ControlHandoff else None
        intent = control_work.intent if control_work is not None else None
        is_old_terminal = intent is not None and intent.launch_id == launch_id and intent.generation == 1
        actual = original_submit(executor, fn, *args, **kwargs)
        if is_old_terminal:
            assert old_future_ref is None
            old_future_ref = weakref.ref(actual, lambda _ref: loop.call_soon_threadsafe(old_future_gone.set))
        return actual

    def close_later(spaces, words=None):
        workers.update(space.worker for space in spaces if space.worker is not None)
        original_close(spaces, words)
        closing.update(server.spaces._closing)

    def terminal(authority, issuer, target, **options):
        if authority is server.launch_authority and target == launch_id and options.get("generation") == 1:
            caller = sys._getframe(1)
            assert caller.f_code is launch_lifecycle._terminal.__code__
            gate.pause(caller, caller.f_locals["intent"])
        return original_terminal(authority, issuer, target, **options)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    monkeypatch.setattr(server.spaces, "_close_later", close_later)
    monkeypatch.setattr(LaunchAuthority, "terminal_control", terminal)
    try:
        server.shared.set(Config(ocr="off", history="off"), actor="server")
        identity, token = server.identities.add_key("P26a HOLD graph fixture")
        root = root_key(server.db, server.master, identity.id, clock=server.clock)
        storage = PgSettings(server.db, server.shared, identity.id, root, kind="key")
        configured = storage.prepare(storage.load().model_copy(update={"ner": "off"}))
        storage.save(configured)
        storage.save_reviewed(configured)
        del root
        app = create_server_app(server)
        listener = asyncio.create_task(listen(server))
        async with asyncio.timeout(30):
            await server.listening.wait()
            route = f"/u/{route_key(token)}/_latet/engine/v1"
            headers = {"x-latet-token": token}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False
            ) as client:
                ping = await client.get(route + "/ping", headers=headers)
                assert ping.status_code == 200 and ping.json()["ok"] is True
                await _wait_tasks(set(server.spaces._index_tasks))
                space = server.spaces._spaces[identity.id]
                runtime = space.runtime
                control = runtime.gateway.launch_controls
                assert type(control) is ProtectedLaunchControls
                prepared = await client.get(route + "/launches/prepare", headers=headers)
                assert prepared.status_code == 200
                fence = prepared.json()
                registration = {"folder": "/client/work/child", "root": "/client/work", "flavor": "posix"}
                opened = await client.post(route + "/launches", headers=headers, json=registration)
                stable, _, _ = _assert_pending_launch(server, opened, identity.id, bytes.fromhex(fence["epoch"]))
                launch = opened.json()
                launch_id = bytes.fromhex(launch["launch_id"])
                item_path = route + "/launches/" + launch["launch_id"]

                counted = tuple(
                    code
                    for code in server_spaces.Spaces.app.__code__.co_consts
                    if type(code) is CodeType and code.co_name == "counted"
                )
                assert len(counted) == 1
                store_submit = server_machines.submit_store
                assert type(store_submit) is FunctionType and store_submit.__closure__ is None
                assert store_submit.__globals__ is vars(server_machines)
                assert vars(server_machines)[store_submit.__name__] is store_submit
                # A library method, which the witness's await-chain rule never covers: its own exact boundary stays.
                assert asyncio.Timeout is asyncio.timeouts.Timeout
                timeout_exit = vars(asyncio.Timeout)["__aexit__"]
                assert type(timeout_exit) is FunctionType and timeout_exit is asyncio.Timeout.__aexit__
                assert timeout_exit.__globals__ is vars(asyncio.timeouts) and timeout_exit.__closure__ is None
                assert timeout_exit.__module__ == "asyncio.timeouts"
                assert timeout_exit.__qualname__ == "Timeout.__aexit__"
                shared = {
                    id(timeout_exit): "pre-existing Timeout.__aexit__ class method definition",
                    id(loop): "exact running loop",
                    id(server): "pre-existing Server",
                    id(server.spaces): "pre-existing Spaces",
                    id(vars(server_spaces)): "pre-existing Spaces globals",
                    id(space): "pre-existing Space",
                    id(runtime): "pre-existing Runtime",
                    id(runtime.config): "pre-existing Runtime config",
                    id(control): "pre-existing control adapter",
                    id(control._codec): "pre-existing identity codec",
                    id(server.launch_authority): "pre-existing authority",
                    id(ledger): "pre-existing mutation directory; slot counted",
                    id(server.launch_holds): "pre-existing physical directory; ticket counted",
                    id(HTTPException): "pre-existing Starlette exception definition",
                    id(WebSocketException): "pre-existing Starlette exception definition",
                    id(vars(futures_base)): "pre-existing Future globals",
                    id(vars(futures_thread)): "pre-existing executor globals",
                    id(store_submit): "pre-existing STORE function",
                }
                _app_boundaries(shared, app, "warmed outer application")
                _app_boundaries(shared, space.app, "warmed inner application")

                def snapshot(label, owners, expected, retired_slot=None):
                    assert server.launch_holds.count == expected
                    assert ledger.retained_bytes == baseline + expected * MUTATION_BYTES
                    assert ledger.issuer_bytes(identity.id) == expected * MUTATION_BYTES
                    exact_transport = {id(fn): "injected transport function" for fn in transport}
                    retired = ledger._slots[retired_slot] if retired_slot is not None else None
                    if retired_slot is not None:
                        assert retired is not None
                    try:
                        observed[label] = _paired(
                            owners,
                            space,
                            shared | exact_transport,
                            tuple(fn.__code__ for fn in transport),
                            snapshot.__code__,
                            counted[0],
                            gate,
                            retired,
                        )
                    except AssertionError as error:
                        rejected[label] = str(error)

                old, old_disconnect, old_ready, old_io = _start_hold(app, item_path + "/hold", token, 0)
                holds.append((old, old_disconnect))
                transport.extend(old_io)
                await old_ready()
                first = _launch_state(server, launch_id)
                assert first is not None and (first.state, first.generation, first.operation) == (
                    State.LIVE,
                    1,
                    Operation.NONE,
                )
                old_slot = _physical_owner(old)[0]._ticket.slot
                snapshot("acknowledged", ((old, False),), 1)

                recovered = await client.post(
                    route + "/launches/recover",
                    headers=headers,
                    json={**registration, **fence, "key": launch["key"], "launch_id": launch["launch_id"]},
                )
                assert recovered.status_code == 200 and recovered.json()["generation"] == 2
                replacement, new_disconnect, new_ready, new_io = _start_hold(app, item_path + "/hold", token, 2)
                holds.append((replacement, new_disconnect))
                transport.extend(new_io)
                await new_ready()
                latest = _launch_state(server, launch_id)
                assert latest is not None and (latest.state, latest.generation, latest.operation) == (
                    State.LIVE,
                    3,
                    Operation.NONE,
                )
                assert not old.done() and not replacement.done()
                snapshot("replacement-overlap", ((old, False), (replacement, False)), 2)

                old_disconnect.set()
                await asyncio.wait_for(gate.entered.wait(), 3)
                old_response_frame = _frame(old, bridge_api._LaunchHold.__call__.__code__)
                detached_task = old_response_frame.f_locals["self"].drain_task
                assert type(detached_task) is asyncio.Task and not detached_task.done()
                drain_ref = weakref.ref(detached_task)
                del detached_task, old_response_frame
                old.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(old, 3)
                assert old.cancelled()
                holds.pop(0)
                del old
                detached_task = drain_ref()
                assert detached_task is not None and not detached_task.done()
                snapshot("detached-old-drain", ((detached_task, True), (replacement, False)), 2, old_slot)
                del detached_task
                gate.release.set()
                detached_task = drain_ref()
                if detached_task is not None:
                    await _wait_tasks({detached_task})
                del detached_task
                await asyncio.wait_for(old_future_gone.wait(), 3)
                assert old_future_ref is not None and old_future_ref() is None
                assert not gate.expired and server.launch_holds.count == 1 and ledger._slots[old_slot] is None
                assert not replacement.done() and _launch_state(server, launch_id) == latest

                new_disconnect.set()
                await _wait_tasks({replacement})
                assert server.launch_holds.count == 0
                ended = await client.post(item_path + "/end", headers=headers, json={})
                assert ended.status_code == 200 and ended.json() == {"ended": True}
                exited = await client.post(item_path + "/exit", headers=headers, json={"epoch": fence["epoch"]})
                assert exited.status_code == 200 and exited.json() == {"retired": True, **fence}
                assert _launch_state(server, launch_id) is None and server.bus.client.strlen(stable) == 0
                assert server.launch_holds.count == 0 and ledger.retained_bytes == baseline
                assert ledger.issuer_bytes(identity.id) == 0
    finally:
        gate.release.set()
        for _task, disconnect in holds:
            disconnect.set()
        await _wait_tasks({task for task, _ in holds}, allow_cancelled=True)
        if drain_ref is not None and (detached := drain_ref()) is not None:
            await _wait_tasks({detached})
        if listener is not None:
            listener.cancel()
            await _wait_tasks({listener}, allow_cancelled=True)
        await _wait_tasks(set(server.spaces._building.values()))
        await _wait_tasks(set(server.spaces._index_tasks))
        await server.spaces.close(wait=False)
        await _wait_tasks(closing | server.spaces._closing)
        await _wait_tasks(workers, allow_cancelled=True)
        await _wait_tasks({asyncio.wrap_future(actual) for actual in work})
        await ledger.drain()
        if server.spaces._spaces or server.spaces._building or server.spaces._closing or server._ear is not None:
            raise RuntimeError("protected HOLD fixture still owns live Server work")
        server.close()
        _clean_owned_namespaces(schema, prefix)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))
        with capsys.disabled():
            print("P26A_PROTECTED_HOLD_GRAPH", json.dumps({"graphs": observed, "rejected": rejected}, sort_keys=True))

    assert not rejected and tuple(observed) == ("acknowledged", "replacement-overlap", "detached-old-drain")
    for label, results in observed.items():
        count = 1 if label == "acknowledged" else 2
        assert len(results["mutations"]) == count
        assert results["physical"]["bytes"] + _RESERVE <= count * HOLD_BYTES
        assert all(item["bytes"] <= MUTATION_BYTES for item in results["mutations"])
