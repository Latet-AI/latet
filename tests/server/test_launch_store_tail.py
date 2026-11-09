"""One real protected OPEN across actual STORE completion and worker release."""

import asyncio
import decimal
import json
import sys
import threading
import weakref
from _thread import LockType, RLock
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import _base as futures_base
from concurrent.futures import thread as futures_thread
from contextlib import suppress
from functools import partial
from types import CodeType, CoroutineType, FrameType, FunctionType
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.exceptions import HTTPException, WebSocketException

from latet.config import Config
from latet.server import launch_mutations, launch_open
from latet.server import machines as server_machines
from latet.server import spaces as server_spaces
from latet.server.app import create_server_app, listen, open_server
from latet.server.db import Database, migrate
from latet.server.launch_lifecycle import ProtectedLaunchControls
from latet.server.launch_mutations import MUTATION_BYTES, MutationAdmission, OpenOutcome, OpenResultCell
from latet.server.launch_records import Operation
from latet.server.launches import LaunchAuthority, Publication, Receipt, Ticket
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .. import thread_pools
from ..unit.test_launch_keys import _LaunchGraph
from .support import DATABASE, URL, scanned_once
from .test_launch_server_adapter import _DRAIN_SECONDS, _WORK_LIMIT, _clean_owned_namespaces, _wait_tasks
from .test_launch_server_adapter import _prove_owned_launch_namespaces as _prove_owned_launch_namespaces
from .test_launch_short_graph import _PRIVATE_TYPES, _SHARED_MODULES, _app_boundaries, _registration

pytestmark = pytest.mark.server

_WorkItem = futures_thread._WorkItem
_worker = futures_thread._worker
_STORE_TYPES = (
    *_PRIVATE_TYPES,
    Future,
    _WorkItem,
    launch_mutations._Invocation,
    weakref.ReferenceType,
    partial,
    decimal.Context,
    threading.Condition,
    LockType,
    RLock,
    Publication,
    Receipt,
    Ticket,
    Operation,
    OpenOutcome,
    suppress,
)
_RUNNING_CODES = (
    launch_open._publish_open.__code__,
    launch_open._run_open.__code__,
    launch_mutations._invoke.__code__,
)
_CALLBACK_CODES = (
    MutationAdmission._completed.__code__,
    vars(Future)["_invoke_callbacks"].__code__,
    Future.set_result.__code__,
)
_WORK_CODES = (*thread_pools.RUN_CODES, _WorkItem.run.__code__, _worker.__code__)
_CHECKPOINTS = ("running", "finished-before-consume", "consumed-tail")


class _Gate:
    def __init__(self, signal):
        self.signal = signal
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.frame: FrameType | None = None
        self.roots: tuple[object, ...] = ()
        self.thread_id: int | None = None
        self.expired = False

    def pause(self, frame: FrameType, *roots: object) -> None:
        self.frame, self.roots = frame, roots
        self.thread_id = threading.get_ident()
        try:
            self.signal(self.entered.set)
            self.expired = not self.release.wait(3)
        finally:
            self.frame, self.roots = None, ()


def _worker_boundary(frame, codes, item, thread_id, boundary):
    """Walk the concrete owner chain to its Thread.run infrastructure parent."""
    for code in codes:
        assert frame is not None and frame.f_code is code
        frame = frame.f_back
    # The call itself runs in the pool's own frames on 3.14 (thread_pools.RUN_CODES); its future's callbacks do not.
    frame = thread_pools.item_frame(frame, item, called=codes[-1] is not Future.set_result.__code__)
    frame = frame.f_back
    assert frame is not None and frame.f_code is _worker.__code__
    held = thread_pools.frame_locals(frame)  # the worker's frame outlives this read: nothing may stay on it
    assert held["work_item"] is item
    queue, executor_ref = held["work_queue"], held["executor_reference"]
    del held
    assert queue is vars(server_machines._STORE)["_work_queue"] and executor_ref() is server_machines._STORE
    frame = frame.f_back
    assert frame is not None and frame.f_code is threading.Thread.run.__code__
    thread = thread_pools.frame_locals(frame)["self"]
    assert type(thread) is threading.Thread and thread.ident == thread_id
    assert vars(thread)["_target"] is _worker and not vars(thread)["_kwargs"]
    context = thread_pools.worker_context(vars(thread)["_args"], executor_ref, queue)
    boundary[id(frame)] = "exact selected Thread.run bootstrap parent"
    boundary[id(queue)] = "pre-existing STORE queue"
    boundary[id(executor_ref)] = "exact STORE worker bootstrap executor weakref"
    if context is not None:
        boundary[id(context)] = "exact STORE worker context, made with the worker"


async def test_real_server_open_store_running_and_callback_tail(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    signal_dict_type = type(decimal.DefaultContext.flags)
    assert type(decimal.DefaultContext.traps) is signal_dict_type
    assert (signal_dict_type.__module__, signal_dict_type.__qualname__) == ("abc", "SignalDict")
    loop = asyncio.get_running_loop()
    schedule = loop.call_soon_threadsafe
    running, finished, consumed = (_Gate(schedule) for _ in range(3))
    item_gone, future_gone = asyncio.Event(), asyncio.Event()
    consume_turn = asyncio.Event()
    handoff_id = None
    item_ref = None
    future_ref = None
    ticket_id = ticket_slot = None
    tail_paid = False
    server = listener = request_task = None
    work: list[Future[object]] = []
    total_submissions = store_submissions = selected_schedules = 0
    submission_lock = threading.Lock()
    original_submit = ThreadPoolExecutor.submit
    original_item = _WorkItem.__init__
    original_wire = LaunchAuthority.submit
    closing = set()
    workers = set()

    def submit(executor, fn, /, *args, **kwargs):
        nonlocal total_submissions, store_submissions
        with submission_lock:
            if total_submissions >= _WORK_LIMIT:
                raise RuntimeError("Server work inventory exceeded")
            actual = original_submit(executor, fn, *args, **kwargs)
            total_submissions += 1
            store_submissions += executor is server_machines._STORE
            # Only the work item and product own the selected Future across detach.
            if executor is not server_machines._STORE or fn is not launch_mutations._invoke:
                work.append(actual)
            return actual

    def seen_item(item, future, fn, args, kwargs):
        nonlocal handoff_id, item_ref, future_ref
        if fn is launch_mutations._invoke:
            assert item_ref is None and future_ref is None and type(future) is Future
            assert len(args) == 1 and type(args[0]) is launch_mutations._Invocation and not kwargs
            assert args[0].fn is launch_open._run_open and type(args[0].argument) is launch_open._OpenHandoff
            assert args[0].result is args[0].argument.result
            handoff_id = id(args[0].argument)
            item_ref = weakref.ref(item, lambda _ref: schedule(item_gone.set))
            future_ref = weakref.ref(future, lambda _ref: schedule(future_gone.set))

    def publication_submit(authority, publication):
        caller = sys._getframe(1)
        if caller.f_code is launch_open._publish_open.__code__:
            running.pause(caller, publication)
        return original_wire(authority, publication)

    def observe_consume(callback, actual):
        callback(actual)
        # A ready request was queued by the callback before this FIFO sentinel.
        loop.call_soon(consume_turn.set)

    def call_soon_threadsafe(callback, *args, context=None):
        nonlocal selected_schedules
        if (
            future_ref is not None
            and len(args) == 1
            and args[0] is future_ref()
            and getattr(callback, "__func__", None) is MutationAdmission._consume
        ):
            selected_schedules += 1
            caller = sys._getframe(1)
            finished.pause(caller, callback, *args, context)
            handle = schedule(observe_consume, callback, *args, context=context)
            consumed.pause(caller, callback, *args, context)
            return handle
        return schedule(callback, *args, context=context)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    try:
        async with asyncio.timeout(30):
            database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
            try:
                migrate(database, master=MasterKey.load(settings))
            finally:
                database.close()
            server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")
            original_close_later = server.spaces._close_later

            def close_later(spaces, words=None):
                workers.update(space.worker for space in spaces if space.worker is not None)
                original_close_later(spaces, words)
                closing.update(server.spaces._closing)

            monkeypatch.setattr(server.spaces, "_close_later", close_later)
            server.shared.set(Config(ocr="off", history="off"), actor="server")
            identity, secret = server.identities.add_key("P26a STORE fixture")
            key = root_key(server.db, server.master, identity.id, clock=server.clock)
            identity_settings = PgSettings(server.db, server.shared, identity.id, key, kind="key")
            body, projects = _registration()
            configuration = identity_settings.prepare(
                identity_settings.load().model_copy(update={"ner": "off", "projects": projects})
            )
            identity_settings.save(configuration)
            identity_settings.save_reviewed(configuration)
            del key, configuration, projects

            app = create_server_app(server)
            listener = asyncio.create_task(listen(server))
            await server.listening.wait()
            base = f"/u/{route_key(secret)}/_latet/engine/v1"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client:
                ping = await client.get(base + "/ping", headers={"x-latet-token": secret})
                assert ping.status_code == 200 and ping.json()["ok"] is True
            await _wait_tasks(set(server.spaces._index_tasks))
            space = server.spaces._spaces[identity.id]
            runtime = space.runtime
            config = runtime.config
            assert (config.ner, config.ocr, config.history, len(config.projects)) == ("off", "off", "off", 200)
            control = runtime.gateway.launch_controls
            assert type(control) is ProtectedLaunchControls
            codec = control._codec
            ledger = server.launch_mutations
            baseline = ledger.retained_bytes

            path = base + "/launches"
            host = urlsplit(URL).netloc.encode("ascii")
            headers = [(b"host", host), (b"x-latet-token", secret.encode("ascii"))]
            headers.extend((f"x-test-{index}".encode("ascii"), b"x") for index in range(29))
            remaining = 4096 - sum(len(name) + len(value) for name, value in headers) - len(b"x-test-pad")
            assert remaining > 0
            headers.append((b"x-test-pad", b"x" * remaining))
            assert len(headers) == 32 and sum(len(n) + len(v) for n, v in headers) == 4096
            assert len(body) == 8192
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": headers,
                "client": ("127.0.0.1", 43210),
                "server": (urlsplit(URL).hostname, 80),
                "state": {},
                "extensions": {},
            }
            receives = 0
            statuses: list[int] = []

            async def receive():
                nonlocal receives
                receives += 1
                if receives != 1:
                    raise AssertionError("unexpected OPEN receive count")
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])

            store_submit = server_machines.submit_store
            assert type(store_submit) is FunctionType
            assert store_submit.__globals__ is vars(server_machines) and store_submit.__closure__ is None
            assert store_submit.__module__ == server_machines.__name__ and store_submit.__name__ == "submit_store"
            assert vars(server_machines)[store_submit.__name__] is store_submit
            shared = {
                id(store_submit): "pre-existing machines.submit_store function definition",
                id(server): "pre-existing Server",
                id(server.spaces): "pre-existing Spaces",
                id(vars(server_spaces)): "shared Spaces module globals",
                id(vars(futures_base)): "shared Future module globals",
                id(vars(futures_thread)): "shared executor module globals",
                id(loop): "exact current loop",
                id(ledger): "pre-existing mutation directory; ticket counted",
                id(server.launch_authority): "pre-existing authority outside borrowed connection slice",
                id(runtime): "pre-existing Runtime",
                id(space): "pre-existing Space",
                id(config): "pre-existing Runtime config",
                id(config.projects): "pre-existing configured project list",
                id(control): "pre-existing OPEN adapter",
                id(codec): "pre-existing binding codec",
                id(HTTPException): "shared Starlette HTTPException class definition",
                id(WebSocketException): "shared Starlette WebSocketException class definition",
                id(suppress.__exit__): "pre-existing contextlib.suppress method definition",
                id(receive): "injected transport callable",
                id(send): "injected transport callable",
            }
            shared.update({id(entry): "pre-existing configured ProjectEntry" for entry in config.projects})
            _app_boundaries(shared, app, "warmed outer ASGI dispatch")
            _app_boundaries(shared, space.app, "warmed inner ASGI dispatch")
            counted_codes = tuple(
                code
                for code in server_spaces.Spaces.app.__code__.co_consts
                if isinstance(code, CodeType) and code.co_name == "counted"
            )
            assert len(counted_codes) == 1
            observations = {}
            rejected = {}

            def snapshot(label, gate):
                nonlocal tail_paid, ticket_id, ticket_slot
                assert item_ref is not None and future_ref is not None
                item, actual = item_ref(), future_ref()
                assert item is not None and actual is not None and item.future is actual
                fn, (invocation,), _ = thread_pools.call(item)
                assert fn is launch_mutations._invoke
                fn = None
                assert type(invocation) is launch_mutations._Invocation
                assert invocation.fn is invocation.argument is invocation.result is None
                assert request_task is not None and not request_task.done()
                coroutine = request_task.get_coro()
                open_code = launch_open.ProtectedLaunchOpen.open.__code__
                for _ in range(64):
                    assert type(coroutine) is CoroutineType
                    if coroutine.cr_code is open_code:
                        break
                    coroutine = coroutine.cr_await
                else:
                    raise AssertionError("pending request has no protected OPEN coroutine")
                open_frame = coroutine.cr_frame
                assert open_frame is not None
                opening = thread_pools.frame_locals(open_frame)
                handoff = opening["handoff"]
                assert type(handoff) is launch_open._OpenHandoff and id(handoff) == handoff_id
                assert handoff.payload is None
                result = handoff.result
                assert type(result) is OpenResultCell and result.started and opening["result"] is result
                (callback,) = vars(actual)["_done_callbacks"]
                assert type(callback) is partial and len(callback.args) == 1 and callback.args[0] is loop
                assert getattr(callback.func, "__func__", None) is MutationAdmission._completed
                ticket = getattr(callback.func, "__self__", None)
                assert type(ticket) is MutationAdmission
                marker = ticket._tail
                assert type(marker) is weakref.ReferenceType and marker() is invocation
                assert invocation.marker is marker and marker.__callback__ is None
                notifier = invocation.notify
                assert isinstance(notifier, partial)
                assert type(notifier) is partial and notifier.args == (loop,)
                assert getattr(notifier.func, "__self__", None) is ticket
                assert getattr(notifier.func, "__func__", None) is MutationAdmission._tail_done
                assert opening["ticket"] is ticket and ticket.pending
                del opening
                if ticket_id is None:
                    ticket_id = id(ticket)
                    ticket_slot = ticket.slot
                assert id(ticket) == ticket_id
                assert gate.frame is not None and gate.thread_id == running.thread_id
                assert gate.thread_id != threading.get_ident()
                boundary = dict(shared)
                codes = _RUNNING_CODES if gate is running else _CALLBACK_CODES
                _worker_boundary(gate.frame, codes, item, gate.thread_id, boundary)
                if gate is running:
                    (publication,) = gate.roots
                    assert type(publication) is Publication
                    assert thread_pools.frame_locals(gate.frame)["publication"] is publication
                    assert publication._confirmed and not publication.attempted
                    assert actual.running() and result.outcome is None
                    run_frame = gate.frame.f_back
                    assert run_frame is not None and thread_pools.frame_locals(run_frame)["handoff"] is handoff
                    invoke_frame = run_frame.f_back
                    assert invoke_frame is not None
                    invoking = thread_pools.frame_locals(invoke_frame)
                    assert invoking["argument"] is handoff
                    assert invoking["result"] is result and "invocation" not in invoking
                    del invoking
                else:
                    consume_callback, scheduled, context = gate.roots
                    assert consume_callback.__self__ is ticket
                    assert consume_callback.__func__ is MutationAdmission._consume and scheduled is actual
                    assert not tuple(context.items()) and actual.done()
                waiter = getattr(request_task, "_fut_waiter", None)
                assert waiter is not None and any(waiter is current for current in ticket._ready._waiters)
                assert ticket.actual is actual and ticket._result is result and ticket.ingress
                assert not statuses and not ticket._ready.is_set()
                if gate is not running:
                    assert type(result.outcome) is OpenOutcome and result.outcome.status == "opened"
                paid = (
                    ledger._slots[ticket.slot] is ticket
                    and ledger.retained_bytes == baseline + MUTATION_BYTES
                    and ledger.issuer_bytes(identity.id) == MUTATION_BYTES
                )
                if gate is consumed:
                    assert consume_turn.is_set()
                    tail_paid = paid
                else:
                    assert paid
                roots = [
                    request_task,
                    gate.frame,
                    item,
                    actual,
                    invocation,
                    marker,
                    handoff,
                    result,
                    ticket,
                    *gate.roots,
                ]
                graph = _LaunchGraph(
                    boundary,
                    driver_codes=(receive.__code__, send.__code__),
                    private_frame_codes=(*counted_codes, *_RUNNING_CODES, *_CALLBACK_CODES, *_WORK_CODES),
                    observer_codes=(
                        _LaunchGraph.measure.__code__,
                        snapshot.__code__,
                        test_real_server_open_store_running_and_callback_tail.__code__,
                    ),
                    private_types=(*_STORE_TYPES, signal_dict_type),
                    shared_modules=_SHARED_MODULES,
                )
                try:
                    measured, _ = graph.measure(roots)
                except AssertionError as error:
                    rejected[label] = str(error)
                else:
                    observations[label] = measured
                with capsys.disabled():
                    print(
                        "P26A_STORE_GRAPH",
                        json.dumps(
                            {
                                "checkpoint": label,
                                "measurement": observations.get(label),
                                "rejected": rejected.get(label),
                            },
                            sort_keys=True,
                        ),
                    )

            with monkeypatch.context() as hooks:
                hooks.setattr(_WorkItem, "__init__", thread_pools.item_init(original_item, seen_item))
                hooks.setattr(LaunchAuthority, "submit", publication_submit)
                hooks.setattr(loop, "call_soon_threadsafe", call_soon_threadsafe)
                request_task = asyncio.create_task(app(scope, receive, send), name="p26a-store-open")
                await running.entered.wait()
                snapshot("running", running)
                running.release.set()
                await finished.entered.wait()
                snapshot("finished-before-consume", finished)
                finished.release.set()
                await consumed.entered.wait()
                await consume_turn.wait()
                snapshot("consumed-tail", consumed)
                consumed.release.set()
                await request_task
                assert statuses == [200] and receives == 1
                request_task = None
                del scope
                await item_gone.wait()
                await future_gone.wait()
            assert item_ref is not None and item_ref() is None
            assert future_ref is not None and future_ref() is None
            with server.db.connection() as connection:
                counts = connection.execute(
                    "SELECT entries, bindings FROM launch_registry WHERE singleton = 1"
                ).fetchone()
            assert counts == (1, 1)
            keys = server.bus.client.scan_iter(match=server.bus.key("launch:*"), count=100)
            assert len(scanned_once(keys, 2)) == 1
            assert selected_schedules == store_submissions == 1
            assert not any(gate.expired for gate in (running, finished, consumed))
            with capsys.disabled():
                print("P26A_STORE_DETACH", '{"future": true, "item": true}')
            assert set(observations) == set(_CHECKPOINTS) and not rejected
            assert all(row["bytes"] <= MUTATION_BYTES for row in observations.values())
            assert not server.launch_holds.count
            ledger.require_drained()
            assert ticket_slot is not None and ledger._slots[ticket_slot] is None
            assert ledger.retained_bytes == baseline and ledger.issuer_bytes(identity.id) == 0
    finally:
        if server is not None:
            server.launch_mutations.stop()
        for gate in (running, finished, consumed):
            gate.release.set()
        if server is not None:
            async with asyncio.timeout(_DRAIN_SECONDS):
                if request_task is not None:
                    await _wait_tasks({request_task})
                    request_task = None
                if item_ref is not None:
                    await item_gone.wait()
                if future_ref is not None:
                    await future_gone.wait()
                if listener is not None:
                    listener.cancel()
                    await _wait_tasks({listener}, allow_cancelled=True)
                await _wait_tasks(set(server.spaces._building.values()))
                await _wait_tasks(set(server.spaces._index_tasks))
                await _wait_tasks({asyncio.wrap_future(actual) for actual in work})
                await server.launch_mutations.drain()
                await server.spaces.close(wait=False)
                await _wait_tasks(closing | server.spaces._closing)
                await _wait_tasks(workers, allow_cancelled=True)
                await _wait_tasks({asyncio.wrap_future(actual) for actual in work})
                assert not server.spaces._spaces and not server.spaces._building and not server.spaces._closing
                assert server._ear is None
            server.close()
        _clean_owned_namespaces(schema, prefix)
        request.node.user_properties.append(("p26a.drained", (schema, prefix)))
        with capsys.disabled():
            print(
                "P26A_STORE_WORK",
                json.dumps(
                    {"submissions": total_submissions, "store": store_submissions, "schedules": selected_schedules},
                    sort_keys=True,
                ),
            )

    with capsys.disabled():
        print("P26A_STORE_TAIL_CHARGE", json.dumps({"retained_until_detach": tail_paid}))
    assert tail_paid, "selected STORE tail outlived its mutation charge"
