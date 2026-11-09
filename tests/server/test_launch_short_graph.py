"""Real Server OPEN checkpoints for the short-control Python ownership slice."""

import asyncio
import json
import sys
import threading
from array import array
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import _WorkItem
from pathlib import PurePosixPath, PureWindowsPath
from types import CodeType, FrameType, GeneratorType
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.exceptions import HTTPException, WebSocketException
from starlette.routing import Match, Route

from latet.bridge import api as bridge_api
from latet.bridge.api import Bridge
from latet.config import Config, ProjectEntry
from latet.engine import wellformed
from latet.server import app as server_app
from latet.server import launch_binding, launch_mutations, launch_open, sealing
from latet.server import machines as server_machines
from latet.server import spaces as server_spaces
from latet.server.app import create_server_app, listen, open_server
from latet.server.db import Database, migrate
from latet.server.grants import Grant
from latet.server.identities import Identity
from latet.server.launch_binding import Binding, BindingAddress
from latet.server.launch_lifecycle import ProtectedLaunchControls
from latet.server.launch_mutations import LAUNCH_MUTATION, MUTATION_BYTES, MutationAdmission, OpenResultCell
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .. import thread_pools
from ..unit.test_launch_keys import _LaunchGraph
from .support import DATABASE, URL, scanned_once
from .test_launch_server_adapter import (
    _DRAIN_SECONDS,
    _WORK_LIMIT,
    _clean_owned_namespaces,
    _wait_tasks,
)
from .test_launch_server_adapter import _prove_owned_launch_namespaces as _prove_owned_launch_namespaces

pytestmark = pytest.mark.server

_SHARED_MODULES = (
    "latet.bridge.api",
    "latet.engine.wellformed",
    "latet.gateway.app",
    "latet.gateway.security",
    "latet.server.app",
    "latet.server.grants",
    "latet.server.identities",
    "latet.server.launch_binding",
    "latet.server.launch_mutations",
    "latet.server.launch_open",
    "latet.server.launches",
    "latet.server.logs",
    "latet.server.sealing",
)
_PRIVATE_TYPES = (
    Binding,
    BindingAddress,
    Grant,
    Identity,
    Match,
    MutationAdmission,
    OpenResultCell,
    PurePosixPath,
    array,
    enumerate,
    range,
    wellformed.Read,
    launch_open._OpenHandoff,
    launch_open._OpenPayload,
)
_LABELS = (
    "preauth",
    "raw",
    "decoded",
    "selection-last",
    "selection-final",
    "binding-construction",
    "seal-aad",
    "seal-output",
)


class _UnknownPrivate:
    pass


def _active_frame_control(graph: _LaunchGraph) -> None:
    payload = bytearray(4096)
    measured, _ = graph.measure([sys._getframe()])
    assert measured["components"]["builtins.bytearray"] >= sys.getsizeof(payload)
    assert measured["private_frames"] == {"_active_frame_control": 1}


def _app_boundaries(shared, app, reason):
    """Inventory only the warmed ASGI dispatch chain and declared Route objects."""
    shared[id(app)] = reason
    layer = app.middleware_stack
    depth = 0
    while layer is not None:
        if depth >= 16:
            raise AssertionError("warmed middleware chain exceeds inventory")
        shared[id(layer)] = reason
        if layer is app.router:
            break
        layer = getattr(layer, "app", None)
        depth += 1
    shared[id(app.router)] = reason
    assert len(app.routes) <= 128
    for route in app.routes:
        shared[id(route)] = reason
        if type(route) is Route:
            shared[id(route.app)] = reason
            owner = getattr(route.endpoint, "__self__", None)
            if isinstance(owner, Bridge):
                shared[id(owner)] = "pre-existing Bridge"


def _registration():
    root = "/😀" + "a" * 3993
    folder = root + "/nested/" + "b" * 47
    registration = {"folder": folder, "root": root, "flavor": "posix"}
    body = json.dumps(registration, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > 8192:
        raise AssertionError("registration exceeds body profile")
    # Make the body exactly the strict ingress bound without expanding either path.
    body += b" " * (8192 - len(body))
    assert len(root.encode()) == 3998 and len(folder.encode()) == 4053
    paths = [ProjectEntry(path=f"/unrelated/{index:03}") for index in range(199)]
    paths.append(ProjectEntry(path=root + "/nested"))
    return body, paths


def _windows_registration(*, unc: bool, duplicate: bool):
    anchor, folder_anchor, width = ("\\\\Srv\\Share\\", "\\\\srv\\share\\", 3981) if unc else ("C:\\", "C:\\", 3990)
    root = anchor + "😀" + "A" * width
    folder = folder_anchor + "😀" + "a" * width + "\\Child"
    body = json.dumps(
        {"folder": folder, "root": root, "flavor": "windows"}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert (len(root.encode()), len(folder.encode()), len(body)) == (3997, 4003, 8051 if unc else 8045)
    body += b" " * (8192 - len(body))
    paths = [ProjectEntry(path=f"{anchor}unrelated\\{index:03}") for index in range(198 if duplicate else 199)]
    paths.append(ProjectEntry(path=root + "\\CHILD"))
    if duplicate:
        assert paths[-1].path != folder and paths[-1].path.lower() == folder.lower()
        paths.append(ProjectEntry(path=folder))
    return body, paths


def _segmented_unc_registration():
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    separator = chr(92)
    anchor = separator * 2 + "Srv" + separator + "Share" + separator
    segments = [alphabet[index // 36] + alphabet[index % 36] for index in range(1009)]
    segments.append("😀İ")
    assert len(segments) == len(set(segments)) == 1010
    tail = separator.join(segments)
    root = anchor + tail
    folder = anchor.lower() + tail + separator + "Child"
    body = json.dumps(
        {"folder": folder, "root": root, "flavor": "windows"}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert (len(root.encode("utf-8")), len(folder.encode("utf-8")), len(body)) == (3045, 3051, 8165)
    body += b" " * 27
    assert len(body) == 8192
    projects = [ProjectEntry(path=f"{anchor}unrelated{separator}{index:03}") for index in range(199)]
    projects.append(ProjectEntry(path=root + separator + "CHILD"))
    assert len(projects) == 200 and projects[-1].path != folder and projects[-1].path.lower() == folder.lower()
    return body, projects


async def test_real_server_open_short_graph_windows_segmented_unc(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    await _exercise_short_graph(
        owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, "segmented-unc"
    )


async def test_real_server_open_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys):
    await _exercise_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, "posix")


async def test_real_server_open_short_graph_windows_drive(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    await _exercise_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, "drive")


async def test_real_server_open_short_graph_windows_unc(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    await _exercise_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, "unc")


async def test_real_server_open_short_graph_windows_late_duplicate(
    owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys
):
    await _exercise_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, "duplicate")


async def _exercise_short_graph(owned_launch_namespaces, settings, tmp_path, request, monkeypatch, capsys, variant):
    windows = variant != "posix"
    duplicate = variant == "duplicate"
    segmented = variant == "segmented-unc"
    labels: tuple[str, ...] = _LABELS
    private_types: tuple[type, ...] = _PRIVATE_TYPES
    if windows:
        labels = (
            (*_LABELS[:4], "collision-previous") if duplicate else (*_LABELS[:4], "selection-compare", *_LABELS[4:])
        )
        private_types = (*_PRIVATE_TYPES, PureWindowsPath)
    segment_iterator_type = None
    if segmented:
        labels = (*labels[:3], "text-segments-last", *labels[3:-2])
        segment_iterator_type = type(iter("😀İ"))
        private_types = (*private_types, GeneratorType, segment_iterator_type)
    schema, prefix = owned_launch_namespaces
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    server = None
    listener = None
    work: list[Future[object]] = []
    workers = set()
    closing = set()
    stores = 0
    pool_counts: Counter[str] = Counter()
    submissions = threading.Lock()
    original_submit = ThreadPoolExecutor.submit

    def submit(executor, fn, /, *args, **kwargs):
        nonlocal stores
        with submissions:
            if len(work) >= _WORK_LIMIT:
                raise RuntimeError("Server work inventory exceeded")
            actual = original_submit(executor, fn, *args, **kwargs)
            work.append(actual)
            pool_counts[executor._thread_name_prefix] += 1
            stores += executor is server_machines._STORE
            return actual

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
            identity, secret = server.identities.add_key("P26a graph fixture")
            key = root_key(server.db, server.master, identity.id, clock=server.clock)
            identity_settings = PgSettings(server.db, server.shared, identity.id, key, kind="key")
            if windows:
                body, projects = (
                    _segmented_unc_registration()
                    if segmented
                    else _windows_registration(unc=variant != "drive", duplicate=duplicate)
                )
                last_paths = projects[-2].path, projects[-1].path
            else:
                body, projects = _registration()
                last_paths = None
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
            if windows and len(config.projects) != 200:
                raise AssertionError("Windows fixture did not preserve configured root count")
            assert (config.ner, config.ocr, config.history, len(config.projects)) == ("off", "off", "off", 200)
            if last_paths is not None:
                actual_pair = config.projects[-2].path, config.projects[-1].path
                if actual_pair != last_paths:
                    raise AssertionError("Windows fixture did not preserve final configured root order")
                if duplicate:
                    assert actual_pair[0] != actual_pair[1] and actual_pair[0].lower() == actual_pair[1].lower()
            control = runtime.gateway.launch_controls
            assert type(control) is ProtectedLaunchControls
            codec = control._codec
            mutation_baseline = server.launch_mutations.retained_bytes

            path = base + "/launches"
            host = urlsplit(URL).netloc.encode("ascii")
            headers = [(b"host", host), (b"x-latet-token", secret.encode("ascii"))]
            headers.extend((f"x-test-{index}".encode("ascii"), b"x") for index in range(29))
            remaining = 4096 - sum(len(name) + len(value) for name, value in headers) - len(b"x-test-pad")
            assert remaining > 0
            headers.append((b"x-test-pad", b"x" * remaining))
            assert len(headers) == 32 and sum(len(n) + len(v) for n, v in headers) == 4096
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
            no_store: list[bool] = []
            response_checks: list[bool] = []

            async def receive():
                nonlocal receives
                receives += 1
                if receives != 1:
                    raise AssertionError("unexpected OPEN receive count")
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])
                    if windows:
                        no_store.append((b"cache-control", b"no-store") in message["headers"])
                elif windows and message["type"] == "http.response.body":
                    payload = json.loads(message["body"])
                    if duplicate:
                        response_checks.append(payload == {"error": "launch-invalid"})
                    else:
                        assert last_paths is not None
                        binding = payload.get("binding", {})
                        response_checks.append(
                            binding.get("source") == "server"
                            and binding.get("flavor") == "windows"
                            and binding.get("root") == last_paths[1]
                        )

            counted_codes = tuple(
                code
                for code in server_spaces.Spaces.app.__code__.co_consts
                if isinstance(code, CodeType) and code.co_name == "counted"
            )
            assert len(counted_codes) == 1
            shared = {
                id(server): "pre-existing Server",
                id(server.spaces): "pre-existing Spaces",
                id(vars(server_spaces)): "shared Spaces module globals",
                id(HTTPException): "shared Starlette HTTPException class definition",
                id(WebSocketException): "shared Starlette WebSocketException class definition",
                id(server.launch_mutations): "pre-existing mutation directory; ticket counted",
                id(server.launch_authority): "pre-existing authority; no borrowed connection in this slice",
                id(runtime): "pre-existing Runtime",
                id(space): "pre-existing Space",
                id(config): "pre-existing Runtime config",
                id(config.projects): "pre-existing configured project list",
                id(control): "pre-existing OPEN adapter",
                id(codec): "pre-existing binding codec",
                id(codec._sealer): "pre-existing binding sealer",
                id(receive): "injected transport callable",
                id(send): "injected transport callable",
            }
            shared.update({id(entry): "pre-existing configured ProjectEntry" for entry in config.projects})
            _app_boundaries(shared, app, "warmed outer ASGI dispatch")
            _app_boundaries(shared, space.app, "warmed inner ASGI dispatch")

            original_auth = server_app.secret_grant
            # Observe the exact call sites while preserving their parser and crypto results.
            original_flat = bridge_api.flat_launch_json
            original_read = wellformed.read
            original_text = launch_open._text
            original_depth = launch_open._canonical_depth
            original_component = launch_binding._component
            original_any = any
            validation_code = None
            if segmented:
                validation_codes = tuple(
                    code
                    for code in original_component.__code__.co_consts
                    if isinstance(code, CodeType) and code.co_name == "<genexpr>"
                )
                assert len(validation_codes) == 1
                validation_code = validation_codes[0]
            original_project = launch_open.project_id
            original_relative = launch_binding._relative
            original_selection_relative = launch_open._windows_relative
            original_windows_relative = launch_binding._windows_relative
            original_bound = sealing._bound
            original_frame = sealing.frame
            observations = {}
            rejected = {}
            observer_codes: tuple[CodeType, ...]

            def walk(label: str, frame: FrameType, *extra: object) -> None:
                assert label in labels and label not in observations and label not in rejected
                if segmented:
                    assert stores == 0
                boundary = dict(shared)
                if label.startswith("seal-"):
                    # The STORE queue/WorkItem/actual callback qualification is a later slice.
                    ancestor = frame.f_back
                    for _ in range(8):
                        if ancestor is None or ancestor.f_code is launch_open._run_open.__code__:
                            break
                        ancestor = ancestor.f_back
                    assert ancestor is not None and ancestor.f_code is launch_open._run_open.__code__
                    invocation_frame = ancestor.f_back
                    assert invocation_frame is not None and invocation_frame.f_code is launch_mutations._invoke.__code__
                    assert "invocation" not in thread_pools.frame_locals(invocation_frame)
                    assert (
                        thread_pools.frame_locals(invocation_frame)["argument"]
                        is thread_pools.frame_locals(ancestor)["handoff"]
                    )
                    # The pool's first frame above the call: the work item's own, or on 3.14 the frame its worker's
                    # context runs the call in (thread_pools.RUN_CODES).
                    worker_frame = invocation_frame.f_back
                    assert worker_frame is not None
                    assert thread_pools.item_frame(worker_frame).f_code is _WorkItem.run.__code__
                    boundary[id(worker_frame)] = "exact selected STORE WorkItem.run frame; later tail slice"
                roots = [frame, *extra]
                if not label.startswith("seal-"):
                    current = asyncio.current_task()
                    assert current is not None
                    roots.append(current.get_coro())
                graph = _LaunchGraph(
                    boundary,
                    driver_codes=(auth.__code__, receive.__code__, send.__code__),
                    observer_codes=observer_codes,
                    private_types=private_types,
                    private_frame_codes=counted_codes,
                    shared_modules=_SHARED_MODULES,
                )
                try:
                    measured, _ = graph.measure(roots)
                except AssertionError as error:
                    rejected[label] = str(error)
                else:
                    observations[label] = measured

            async def auth(*args, **kwargs):
                caller = sys._getframe(1)
                if caller.f_code is server_app.ServerGate._authenticated_address.__code__:
                    # No launch-control slot is held while the token is checked (the review's Minor 4).
                    assert thread_pools.frame_locals(caller)["scope"].get(LAUNCH_MUTATION) is None
                    walk("preauth", caller)
                del caller
                return await original_auth(*args, **kwargs)

            def flat(raw):
                result = original_flat(raw)
                caller = sys._getframe(1)
                if caller.f_code is Bridge._launch_json.__code__:
                    assert len(raw) == 8192
                    walk("raw", caller)
                return result

            def read(text):
                result = original_read(text)
                caller = sys._getframe(1)
                if caller.f_code is Bridge._launch_json.__code__:
                    assert "raw" not in thread_pools.frame_locals(caller) and not result.mended and not result.repeated
                    walk("decoded", caller, result)
                return result

            def text(path, flavor):
                result = original_text(path, flavor)
                caller = sys._getframe(1)
                if (
                    duplicate
                    and caller.f_code is launch_open._binding.__code__
                    and thread_pools.frame_locals(caller).get("index") == 199
                    and thread_pools.frame_locals(caller).get("previous") == 198
                ):
                    assert path is thread_pools.frame_locals(caller)["other"] and type(result) is PureWindowsPath
                    walk("collision-previous", caller, result)
                return result

            def depth(path, flavor):
                result = original_depth(path, flavor)
                caller = sys._getframe(1)
                if (
                    caller.f_code is launch_open._binding.__code__
                    and thread_pools.frame_locals(caller).get("index") == 199
                ):
                    assert path is thread_pools.frame_locals(caller)["path"] and type(result) is int
                    if segmented:
                        assert result == 1012
                    walk("selection-last", caller, result)
                return result

            def validation_any(values):
                caller = sys._getframe(1)
                try:
                    if (
                        caller.f_code is original_component.__code__
                        and type(values) is GeneratorType
                        and values.gi_code is validation_code
                        and thread_pools.frame_locals(caller)["segment"] == "😀İ"
                    ):
                        validator = caller.f_back
                        assert validator is not None and validator.f_code is original_depth.__code__
                        parent = validator.f_back
                        if parent is not None and parent.f_code is depth.__code__:
                            parent = parent.f_back
                        if (
                            parent is not None
                            and parent.f_code is launch_open._binding.__code__
                            and thread_pools.frame_locals(parent).get("index") == 199
                            and thread_pools.frame_locals(validator)["path"]
                            is thread_pools.frame_locals(parent).get("path")
                        ):
                            local = thread_pools.frame_locals(validator)
                            assert local["flavor"] == "windows" and local["windows"] is True and "encoded" not in local
                            assert local["depth"] == 1010 and local["end"] == len(local["path"]) - 6
                            assert local["start"] == local["end"] - 2
                            assert local["path"].startswith(local["separator"] + "CHILD", local["end"])
                            assert values.gi_frame is not None and not values.gi_running and values.gi_yieldfrom is None
                            assert values.gi_frame.f_code is validation_code
                            assert type(thread_pools.frame_locals(values.gi_frame)[".0"]) is segment_iterator_type
                            assert caller.f_trace is None and validator.f_trace is None
                            walk("text-segments-last", caller, validator, parent, values, values.gi_frame)
                    return original_any(values)
                finally:
                    del caller

            def selection_relative(path, parent):
                result = original_selection_relative(path, parent)
                caller = sys._getframe(1)
                if (
                    caller.f_code is launch_open._binding.__code__
                    and thread_pools.frame_locals(caller).get("index") == 199
                ):
                    assert (
                        path is thread_pools.frame_locals(caller)["child"]
                        and parent is thread_pools.frame_locals(caller)["normalized"]
                    )
                    assert type(path) is bytes and type(parent) is bytes and result is True
                    walk("selection-compare", caller, path, parent, result)
                return result

            def project(path):
                result = original_project(path)
                caller = sys._getframe(1)
                if caller.f_code is launch_open._binding.__code__:
                    assert len(thread_pools.frame_locals(caller)["seen"]) == 200
                    walk("selection-final", caller, result)
                return result

            def relative(path, parent, separator):
                result = original_relative(path, parent, separator)
                caller = sys._getframe(1)
                if caller.f_code is Binding.__init__.__code__:
                    if thread_pools.frame_locals(caller)["flavor"] == "posix":
                        assert (
                            path is thread_pools.frame_locals(caller)["folder"]
                            and parent is thread_pools.frame_locals(caller)["root"]
                        )
                    walk("binding-construction", caller, path, parent, separator, result)
                return result

            def windows_relative(path, parent):
                result = original_windows_relative(path, parent)
                caller = sys._getframe(1)
                if caller.f_code is Binding.__init__.__code__:
                    assert type(path) is bytes and type(parent) is bytes
                    walk("binding-construction", caller, path, parent, result)
                return result

            def bound(version, aad):
                result = original_bound(version, aad)
                caller = sys._getframe(1)
                if (
                    caller.f_code is sealing.Sealer.seal.__code__
                    and thread_pools.frame_locals(caller)["self"] is codec._sealer
                ):
                    walk("seal-aad", caller, result)
                return result

            def frame(version, nonce, ciphertext):
                result = original_frame(version, nonce, ciphertext)
                caller = sys._getframe(1)
                if (
                    caller.f_code is sealing.Sealer.seal.__code__
                    and thread_pools.frame_locals(caller)["self"] is codec._sealer
                ):
                    walk("seal-output", caller, result)
                return result

            observer_codes = tuple(
                fn.__code__
                for fn in (
                    _LaunchGraph.measure,
                    test_real_server_open_short_graph,
                    _exercise_short_graph,
                    walk,
                    auth,
                    flat,
                    read,
                    text,
                    depth,
                    project,
                    relative,
                    bound,
                    frame,
                )
            )
            if windows and not duplicate:
                observer_codes = (*observer_codes, selection_relative.__code__, windows_relative.__code__)
            if segmented:
                observer_codes = (
                    *observer_codes,
                    validation_any.__code__,
                    test_real_server_open_short_graph_windows_segmented_unc.__code__,
                )
            control_graph = _LaunchGraph(
                {}, observer_codes=observer_codes, private_frame_codes=(_active_frame_control.__code__,)
            )
            control_graph.shared[id(control_graph)] = "negative control observer"
            _active_frame_control(control_graph)
            with pytest.raises(AssertionError, match="unknown private edge"):
                control_graph.measure([_UnknownPrivate()])

            with monkeypatch.context() as hooks:
                hooks.setattr(server_app, "secret_grant", auth)
                hooks.setattr(bridge_api, "flat_launch_json", flat)
                hooks.setattr(wellformed, "read", read)
                hooks.setattr(launch_open, "_text", text)
                hooks.setattr(launch_open, "_canonical_depth", depth)
                if segmented:
                    hooks.setattr(launch_binding, "any", validation_any, raising=False)
                hooks.setattr(launch_open, "project_id", project)
                if windows and not duplicate:
                    hooks.setattr(launch_open, "_windows_relative", selection_relative)
                    hooks.setattr(launch_binding, "_windows_relative", windows_relative)
                hooks.setattr(launch_binding, "_relative", relative)
                if not segmented:
                    hooks.setattr(sealing, "_bound", bound)
                    hooks.setattr(sealing, "frame", frame)
                await asyncio.create_task(app(scope, receive, send), name="p26a-short-open")

            with capsys.disabled():
                for label in labels:
                    print(
                        "P26A_SHORT_GRAPH",
                        json.dumps(
                            {
                                "checkpoint": label,
                                "measurement": observations.get(label),
                                "rejected": rejected.get(label),
                            },
                            sort_keys=True,
                        ),
                    )
            if windows:
                assert statuses == ([400] if duplicate else [200]) and receives == 1
                assert no_store == [True] and response_checks == [True]
                expected_bindings = 0 if duplicate else 1
                assert stores == expected_bindings
                assert server.launch_mutations.retained_bytes == mutation_baseline
                assert server.launch_mutations.issuer_bytes(identity.id) == 0
                with server.db.connection() as connection:
                    counts = connection.execute(
                        "SELECT entries, bindings FROM launch_registry WHERE singleton = 1"
                    ).fetchone()
                assert counts == (expected_bindings, expected_bindings)
                keys = server.bus.client.scan_iter(match=server.bus.key("launch:*"), count=100)
                assert len(scanned_once(keys, 2)) == expected_bindings
            else:
                assert statuses == [200] and receives == 1
                assert stores >= 1 and stores <= _WORK_LIMIT
            assert set(observations) == set(labels) and not rejected
            if segmented:
                assert validation_code is not None
                interior = observations["text-segments-last"]
                assert interior["objects"]["builtins.generator"] >= 1
                assert interior["private_frames"][original_component.__code__.co_qualname] == 1
                assert interior["private_frames"][original_depth.__code__.co_qualname] == 1
                assert interior["private_frames"][validation_code.co_qualname] == 1
            if not segmented and not duplicate:
                for label in ("seal-aad", "seal-output"):
                    assert observations[label]["private_frames"][launch_mutations._invoke.__code__.co_qualname] == 1
            assert all(row["bytes"] <= MUTATION_BYTES for row in observations.values())
            assert not server.launch_holds.count
            server.launch_mutations.require_drained()
    finally:
        if server is not None:
            async with asyncio.timeout(_DRAIN_SECONDS):
                server.launch_mutations.stop()
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
            print("P26A_SHORT_WORK", json.dumps({"executors": dict(pool_counts), "store": stores}, sort_keys=True))
