"""Finite relay witnesses with the real authorization and response owners."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Response as HandshakeResponse

from latet.gateway import request_transport, response_finalizer, ws
from latet.gateway.capture import Capture, Exchange
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import Prepared, WSResponseRewriter
from latet.gateway.outcome import Forward, Pass
from latet.gateway.recorder import ResponseCapture
from latet.gateway.response_finalizer import ResponseFinalizer
from latet.gateway.routes import RouteTable, Upstream

from .test_request_outcomes import _Client
from .test_response_finalizer_lifecycle import _Gate
from .test_response_sink_failures import _TIMEOUT, _sinks
from .test_response_sinks import _records
from .test_response_ws_one_shot import _json

_WIRE = 16_384
_MARKERS = {"a": "describe a tree", "b": "describe a rock", "c": "describe a leaf"}


@dataclass(frozen=True)
class _Profile:
    client_frames: int
    provider_frames: int
    asgi_sends: int
    owners: int
    engine_jobs: int


_CANCEL = _Profile(2, 3, 5, 1, 10)
_ORDER = _Profile(2, 7, 9, 2, 17)
_REFUSAL = _Profile(3, 6, 9, 3, 19)
_RULES = _Profile(2, 3, 5, 1, 10)
_TARGET = _Profile(2, 3, 6, 1, 10)
_GONE = _Profile(1, 3, 5, 1, 10)


def _require(condition: bool, category: str) -> None:
    if not condition:
        pytest.fail(category, pytrace=False)


class _Marks:
    def __init__(self) -> None:
        self.events: dict[str, asyncio.Event] = {}
        self.order: list[str] = []

    def _event(self, key: str) -> asyncio.Event:
        if key not in self.events:
            _require(len(self.events) < 64, "fixture mark bound")
            self.events[key] = asyncio.Event()
        return self.events[key]

    def mark(self, key: str) -> None:
        _require(len(self.order) < 64, "fixture mark order bound")
        self.order.append(key)
        self._event(key).set()

    async def wait(self, key: str) -> None:
        try:
            await asyncio.wait_for(self._event(key).wait(), _TIMEOUT)
        except TimeoutError:
            pytest.fail(f"missing fixture milestone {key}", pytrace=False)


def _created(tag: str) -> str:
    return _json({"type": "response.created", "response": {"id": f"resp_{tag}"}})


def _delta(tag: str, text: str) -> str:
    return _json({"type": "response.output_text.delta", "item_id": f"item_{tag}", "content_index": 0, "delta": text})


def _terminal(tag: str) -> str:
    return _json({"type": "response.completed", "response": {"id": f"resp_{tag}", "status": "completed"}})


def _answer(tag: str) -> tuple[str, ...]:
    return _created(tag), _delta(tag, f"ordinary answer {tag}"), _terminal(tag)


class _Downstream(_Client):
    def __init__(self, marks: _Marks, profile: _Profile) -> None:
        super().__init__()
        first = self.incoming.get_nowait()
        self.incoming = asyncio.Queue(maxsize=3)
        self.incoming.put_nowait(first)
        self.marks, self.profile = marks, profile
        self.output_bytes = 0
        self.block_terminal = False
        self.terminal_release = asyncio.Event()

    async def _send(self, message: Any) -> None:
        _require(len(self.sent) < self.profile.asgi_sends, "fixture ASGI send bound")
        kind = message["type"]
        if kind == "websocket.accept":
            self.marks.mark("accept")
        elif kind == "websocket.send":
            text = message.get("text", "")
            self.output_bytes += len(text.encode()) + len(message.get("bytes", b""))
            _require(self.output_bytes <= _WIRE, "fixture ASGI byte bound")
            if text == _terminal("a") and self.block_terminal:
                self.marks.mark("terminal-blocked")
                await asyncio.wait_for(self.terminal_release.wait(), _TIMEOUT)
        await super()._send(message)
        if kind == "websocket.send":
            text = message.get("text", "")
            if text == _created("a"):
                self.marks.mark("created-a")
            elif text == _terminal("a"):
                self.marks.mark("terminal-a")
            elif text and json.loads(text).get("type") == "error":
                self.marks.mark("local-error")

    def close_codes(self) -> list[int]:
        return [message["code"] for message in self.sent if message["type"] == "websocket.close"]


class _Wire:
    def __init__(self, marks: _Marks, profile: _Profile) -> None:
        self.marks, self.profile = marks, profile
        self.response = HandshakeResponse(101, "Switching Protocols", Headers())
        self.incoming: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self.transport = self
        self.close_code: int | None = None
        self.opens = self.closes = self.aborts = self.emitted = self.reads = 0
        self.input_bytes = self.output_bytes = 0
        self.sent_types: list[str] = []
        self.block_send = False
        self.send_release = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        frame = await self.incoming.get()
        self.reads += 1
        return frame

    async def send(self, frame: str | bytes) -> None:
        _require(isinstance(frame, str), "fixture provider text expected")
        assert isinstance(frame, str)
        self.input_bytes += len(frame.encode())
        _require(self.input_bytes <= _WIRE and len(self.sent_types) < self.profile.client_frames, "provider send bound")
        self.sent_types.append(json.loads(frame)["type"])
        self.marks.mark(f"send-{len(self.sent_types)}")
        if self.block_send and len(self.sent_types) == 1:
            await asyncio.wait_for(self.send_release.wait(), _TIMEOUT)

    def emit(self, *frames: str) -> None:
        self.emitted += len(frames)
        self.output_bytes += sum(len(frame.encode()) for frame in frames)
        _require(self.emitted <= self.profile.provider_frames and self.output_bytes <= _WIRE, "provider output bound")
        _require(self.incoming.qsize() + len(frames) <= self.incoming.maxsize, "provider queue bound")
        for frame in frames:
            self.incoming.put_nowait(frame)

    async def close(self) -> None:
        self.closes += 1
        self.close_code = 1000

    def abort(self) -> None:
        self.aborts += 1


class _Fixture:
    def __init__(self, profile: _Profile) -> None:
        self.profile = profile
        self.loop = asyncio.get_running_loop()
        self.marks = _Marks()
        self.client = _Downstream(self.marks, profile)
        self.wire = _Wire(self.marks, profile)
        self.engine_jobs = self.finalizer_jobs = self.history_jobs = 0
        self.screened_here = 0  # omitted answers, screened on the event loop (``response_finalizer._here``)
        self.owners = self.seals = self.begins = self.published = 0
        self.client_frames = self.client_bytes = self.validations = self.send_calls = 0
        self.allowed: list[bool] = []
        self.control_on_send: list[bool] = []
        self.control_identities: list[bool] = []
        self.targets: list[Any] = []
        self.cancel_arena: Prepared | None = None
        self.target_at_decision: bool | None = None
        self.refusals: list[tuple[int, str]] = []
        self.stale_second = False
        self.prepared_leases: dict[int, int] = {}
        self.pending_releases: list[int] = []
        self.screen_gate: _Gate | None = None
        self.restore_gate: asyncio.Event | None = None
        self.task: asyncio.Task[None] | None = None
        self.disconnected = False
        self.executor: Any = None  # the engine's, once patched

    async def dial(self, _url, **_options):
        _require(request_transport.connecting() is not None and self.wire.opens == 0, "one guarded connect")
        self.wire.opens += 1
        return self.wire

    def create(self, tag: str) -> None:
        self._request(
            {
                "type": "response.create",
                "model": "m",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": _MARKERS[tag]}]}],
            }
        )

    def cancel(self) -> None:
        self._request({"type": "response.cancel", "response_id": "resp_a"})

    def _request(self, value: dict[str, Any]) -> None:
        message = _json(value)
        self.client_frames += 1
        self.client_bytes += len(message.encode())
        _require(self.client_frames <= self.profile.client_frames and self.client_bytes <= _WIRE, "client input bound")
        self.client.incoming.put_nowait({"type": "websocket.receive", "text": message})

    def disconnect(self) -> None:
        if not self.disconnected:
            self.disconnected = True
            self.client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def join(self, *, disconnect: bool = True) -> None:
        """The relay ended, then the settlement of its last turn answered whole, which ends alone once its relay went
        (``ResponseFinalizer.leave``), and what was left to it (its raw capture's end): the callbacks of that
        settlement's end, each run in its turn after the ones before it. A settlement that ended just as its relay did
        is done with its callbacks still queued, the capture's end not written yet; gathering it is done at once
        (``asyncio.gather`` takes a done task as done), and a check right after read the capture without its end
        (public CI on 3.14, 2026-11-09). A callback of the fixture's own, added after them, ends the wait."""
        assert self.task is not None
        if disconnect and not self.task.done():
            self.disconnect()
        await asyncio.wait_for(asyncio.shield(self.task), _TIMEOUT)
        alone = list(response_finalizer._ALONE.get(self.executor, ()) if self.executor is not None else ())
        await asyncio.wait_for(asyncio.gather(*(self._after(task) for task in alone)), _TIMEOUT)
        await asyncio.gather(*alone)  # each done: what one raised is raised here, as before

    def _after(self, task: asyncio.Task[None]) -> asyncio.Future[None]:
        """Done once ``task``'s callbacks added before this one have run."""
        after = self.loop.create_future()
        task.add_done_callback(lambda _: after.done() or after.set_result(None))
        return after

    async def cleanup(self) -> None:
        self.wire.send_release.set()
        self.client.terminal_release.set()
        if self.screen_gate is not None:
            self.screen_gate.release.set()
        if self.restore_gate is not None:
            self.restore_gate.set()
        if self.task is not None and not self.task.done():
            if not self.client.incoming.full():
                self.disconnect()
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.task), _TIMEOUT)
            if not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self.task), _TIMEOUT)

    def patch(self, monkeypatch, mediator, recorder) -> None:
        legacy, validate, send = ws.decide, request_transport.validate_frame, request_transport.send_frame
        construct, seal = ResponseFinalizer.__init__, ResponseFinalizer.seal
        begin, target_check, restored = (
            WSResponseRewriter.begin,
            WSResponseRewriter.target_check,
            WSResponseRewriter.restored,
        )
        publish, screen = ResponseCapture.publish, ResponseCapture.screen
        release, transfer = ExchangeLease.release, ExchangeLease.transfer
        self.executor = mediator.executor
        engine_submit = mediator.executor.submit
        assert recorder._executor is not None
        history_submit = recorder._executor.submit

        def engine_submit_probe(function, *args, **kwargs):
            self.engine_jobs += 1
            _require(self.engine_jobs <= self.profile.engine_jobs, "fixture engine submission bound")
            if function is response_finalizer._work:
                self.finalizer_jobs += 1
                _require(self.finalizer_jobs <= 2 * self.profile.owners, "fixture finalizer submission bound")
            return engine_submit(function, *args, **kwargs)

        def history_submit_probe(function, *args, **kwargs):
            self.history_jobs += 1
            _require(self.history_jobs <= self.profile.owners, "fixture Recorder submission bound")
            return history_submit(function, *args, **kwargs)

        def legacy_probe(arena, *args, **kwargs):
            decision = legacy(arena, *args, **kwargs)
            _require(len(self.allowed) < self.profile.client_frames, "fixture frame decision bound")
            self.allowed.append(isinstance(decision, (Forward, Pass)))
            if arena is not None and arena.control_target is not None:
                self.cancel_arena = arena
                predicate = arena.control_current
                self.target_at_decision = predicate is not None and predicate()
            elif arena is not None and arena.lease is not None:
                self.prepared_leases[id(arena.lease)] = len(self.allowed)
            self.marks.mark(f"decision-{len(self.allowed)}")
            return decision

        def validate_probe(decision, arena, *args, **kwargs):
            self.validations += 1
            _require(self.validations <= self.profile.client_frames, "fixture validation bound")
            if arena is not None and arena.control_target is not None:
                self.control_on_send.append(kwargs.get("on_send") is None)
                self.control_identities.append(len(self.targets) == 1 and arena.control_current is self.targets[0])
            self.marks.mark(f"validate-{self.validations}")
            return validate(decision, arena, *args, **kwargs)

        async def send_probe(connection, authorized):
            self.send_calls += 1
            _require(self.send_calls <= self.profile.client_frames, "fixture send guard bound")
            if self.stale_second and self.send_calls == 2:
                arena = authorized.arena
                before = arena.document
                arena.document = {}
                try:
                    refused = await send(connection, authorized)
                finally:
                    arena.document = before
                if isinstance(refused, request_transport.SendRefusal):
                    self.refusals.append((self.owners, refused.code))
                return refused
            return await send(connection, authorized)

        def construct_probe(finalizer, *args, **kwargs):
            self.owners += 1
            _require(self.owners <= self.profile.owners, "fixture owner bound")
            construct(finalizer, *args, **kwargs)
            self.marks.mark(f"owner-{self.owners}")

        def seal_probe(finalizer, *args, **kwargs):
            self.seals += 1
            _require(self.seals <= self.profile.owners, "fixture seal bound")
            return seal(finalizer, *args, **kwargs)

        def begin_probe(rewriter):
            self.begins += 1
            return begin(rewriter)

        def target_probe(rewriter, value):
            predicate = target_check(rewriter, value)
            _require(len(self.targets) < 2, "fixture control predicate bound")
            self.targets.append(predicate)
            return predicate

        async def restored_probe(rewriter, text):
            if text == _terminal("a") and self.restore_gate is not None:
                self.marks.mark("restore-blocked")
                await asyncio.wait_for(self.restore_gate.wait(), _TIMEOUT)
            return await restored(rewriter, text)

        def screen_probe(capture: ResponseCapture) -> bool:
            if asyncio._get_running_loop() is not None:
                _require(capture.broken, "an answer with a payload to read was screened on the event loop")
                self.screened_here += 1
                return screen(capture)
            if self.screen_gate is not None:
                self.screen_gate.block()
            return screen(capture)

        def publish_probe(capture: ResponseCapture, exchange: Exchange, status: int, *, terminal: bool) -> None:
            publish(capture, exchange, status, terminal=terminal)
            self.loop.call_soon_threadsafe(self._published)

        def release_probe(lease: ExchangeLease) -> None:
            release(lease)
            ordinal = self.prepared_leases.pop(id(lease), None)
            if ordinal is not None:
                self.pending_releases.append(ordinal)

        def transfer_probe(lease: ExchangeLease) -> ExchangeLease:
            heir = transfer(lease)
            self.prepared_leases.pop(id(lease), None)
            return heir

        monkeypatch.setattr(request_transport, "_NoRedirect", self.dial)
        monkeypatch.setattr(request_transport, "validate_frame", validate_probe)
        monkeypatch.setattr(request_transport, "send_frame", send_probe)
        monkeypatch.setattr(ws, "decide", legacy_probe)
        monkeypatch.setattr(ResponseFinalizer, "__init__", construct_probe)
        monkeypatch.setattr(ResponseFinalizer, "seal", seal_probe)
        monkeypatch.setattr(ResponseCapture, "screen", screen_probe)
        monkeypatch.setattr(ResponseCapture, "publish", publish_probe)
        monkeypatch.setattr(WSResponseRewriter, "begin", begin_probe)
        monkeypatch.setattr(WSResponseRewriter, "target_check", target_probe)
        monkeypatch.setattr(WSResponseRewriter, "restored", restored_probe)
        monkeypatch.setattr(ExchangeLease, "release", release_probe)
        monkeypatch.setattr(ExchangeLease, "transfer", transfer_probe)
        monkeypatch.setattr(mediator.executor, "submit", engine_submit_probe)
        monkeypatch.setattr(recorder._executor, "submit", history_submit_probe)

    def _published(self) -> None:
        self.published += 1
        self.marks.mark(f"published-{self.published}")

    def actual_cleanup(self, code: int = 1000) -> bool:
        return (
            self.client.close_codes() == [code]
            and self.wire.opens == self.wire.closes == 1
            and not self.wire.aborts
            and self.owners == self.seals == self.published == self.history_jobs
            # A screening and a raw publication each, but the screening of an omitted answer, done where it is.
            and self.finalizer_jobs + self.screened_here == 2 * self.owners
            and not self.prepared_leases
        )


@asynccontextmanager
async def _relay(tmp_path, monkeypatch, profile: _Profile):
    directory = tmp_path / "capture"
    fixture = _Fixture(profile)
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                "http://127.0.0.1:1",
                envelope_expected=frozenset({("model", "m")}),
            )
        ]
    )
    with _sinks(tmp_path) as (mediator, recorder, store), monkeypatch.context() as patch:
        fixture.patch(patch, mediator, recorder)
        fixture.task = asyncio.create_task(
            ws.relay(fixture.client.socket, routes, Capture(directory), mediator, recorder)
        )
        try:
            await fixture.marks.wait("accept")
            yield fixture, mediator, recorder, store, directory
        finally:
            fixture.cancel_arena = None
            try:
                await fixture.cleanup()
            finally:
                fixture.cancel_arena = None


def _history(recorder, store) -> dict[str, dict[str, Any]]:
    assert recorder.drain(_TIMEOUT), "WebSocket history drain"
    result = {}
    for summary in store.list()[0]:
        detail = store.detail(summary["id"])
        assert detail is not None
        matches = [
            tag
            for tag, marker in _MARKERS.items()
            if any(slot["masked"] == marker for slot in detail["request"]["slots"])
        ]
        assert len(matches) == 1, "one request marker in each turn"
        assert matches[0] not in result, "one history row per turn"
        result[matches[0]] = detail.get("response") or {}
    return result


def _raw(directory) -> tuple[tuple[str, ...], list[str], int]:
    rows = _records(directory)
    return (
        tuple(row["data"] for row in rows if row["t"] == "ws" and row["dir"] == "down"),
        [row["reason"] for row in rows if row["t"] == "omitted"],
        sum(row["t"] == "end" for row in rows),
    )


async def _start(fixture: _Fixture) -> None:
    fixture.create("a")
    await fixture.marks.wait("send-1")
    fixture.wire.emit(_created("a"))
    await fixture.marks.wait("created-a")


async def test_ws_current_explicit_cancel_preserves_bound_predicate(tmp_path, monkeypatch):
    async with _relay(tmp_path, monkeypatch, _CANCEL) as (fixture, _, recorder, store, directory):
        await _start(fixture)
        fixture.cancel()
        await fixture.marks.wait("send-2")
        same_live = len(fixture.targets) == 1 and fixture.targets[0]()
        fixture.cancel_arena = None
        fixture.wire.emit(*_answer("a")[1:])
        await fixture.marks.wait("published-1")
        await fixture.join()
        history = _history(recorder, store)
        checks = {
            "real initial explicit target and original predicate remained live": (
                fixture.target_at_decision is True and same_live and fixture.control_identities == [True]
            ),
            "actual validation and send accepted create and cancel": (
                fixture.allowed == [True, True]
                and fixture.validations == fixture.send_calls == 2
                and fixture.wire.sent_types == ["response.create", "response.cancel"]
            ),
            "cancel created no second owner and invoked no begin": (
                fixture.owners == fixture.begins == 1 and fixture.control_on_send == [True]
            ),
            "one safe turn and clean shutdown": (
                _raw(directory) == (_answer("a"), [], 1)
                and tuple(fixture.client.texts()) == _answer("a")
                and set(history) == {"a"}
                and history["a"].get("text") == "ordinary answer a"
                and fixture.actual_cleanup()
            ),
        }
        assert all(checks.values()), checks


async def test_ws_early_provisional_terminal_blocks_next_turn_then_recovers(tmp_path, monkeypatch):
    async with _relay(tmp_path, monkeypatch, _ORDER) as (fixture, _, recorder, store, directory):
        fixture.wire.block_send = fixture.client.block_terminal = True
        fixture.create("a")
        await fixture.marks.wait("send-1")
        unsafe = (
            _created("a"),
            _delta("a", "latet_"),
            _delta("a", "lk_" + "a" * 43),
            _terminal("a"),
        )
        fixture.wire.emit(*unsafe)
        await fixture.marks.wait("terminal-blocked")
        provisional = fixture.owners == fixture.seals == 1 and fixture.finalizer_jobs == 0
        fixture.wire.send_release.set()
        fixture.create("b")
        await fixture.marks.wait("decision-2")
        no_early_sinks = _raw(directory)[0] == () and not store.list()[0]
        waited = fixture.validations == fixture.send_calls == fixture.owners == fixture.begins == 1
        fixture.client.terminal_release.set()
        await fixture.marks.wait("send-2")
        await fixture.marks.wait("published-1")
        fixture.wire.emit(*_answer("b"))
        await fixture.marks.wait("published-2")
        await fixture.join()
        history = _history(recorder, store)
        down, omissions, ends = _raw(directory)
        checks = {
            "terminal was captured before provisional send returned": provisional,
            "B waited after its actual initial decision without early sink or owner": (
                fixture.allowed == [True, True] and waited and no_early_sinks
            ),
            "terminal downstream send preceded B validation and transport": (
                fixture.marks.order.index("terminal-a")
                < fixture.marks.order.index("published-1")
                < fixture.marks.order.index("validate-2")
                < fixture.marks.order.index("owner-2")
                < fixture.marks.order.index("send-2")
            ),
            "unsafe and ordinary wire preserved in order": tuple(fixture.client.texts()) == unsafe + _answer("b"),
            "A omitted and B independently retained": (
                set(history) == {"a", "b"}
                and not history["a"].get("text")
                and history["a"].get("omitted") == "response-credential"
                and history["b"].get("text") == "ordinary answer b"
                and down == _answer("b")
                and omissions == ["response-credential"]
                and ends == 1
                and fixture.actual_cleanup()
            ),
        }
        assert all(checks.values()), checks


async def test_ws_stale_provisional_refusal_releases_only_its_pending_lease(tmp_path, monkeypatch):
    async with _relay(tmp_path, monkeypatch, _REFUSAL) as (fixture, _, recorder, store, directory):
        fixture.stale_second = True
        fixture.create("a")
        await fixture.marks.wait("send-1")
        fixture.wire.emit(*_answer("a"))
        await fixture.marks.wait("published-1")
        fixture.create("b")
        await fixture.marks.wait("local-error")
        refused = (
            fixture.refusals == [(2, "stale")]
            and fixture.pending_releases.count(2) == 1
            and fixture.begins == 1
            and fixture.wire.sent_types == ["response.create"]
        )
        fixture.create("c")
        await fixture.marks.wait("send-2")
        fixture.wire.emit(*_answer("c"))
        await fixture.marks.wait("published-3")
        await fixture.join()
        history = _history(recorder, store)
        down, omissions, ends = _raw(directory)
        checks = {
            "actual stale refusal after B provisional creation": refused and fixture.allowed == [True, True, True],
            "only A and C actually sent": (
                fixture.validations == fixture.send_calls == 3
                and fixture.begins == 2
                and fixture.wire.sent_types == ["response.create", "response.create"]
            ),
            "B released exactly its own pending lease once": fixture.pending_releases.count(2) == 1,
            "safe A and C remained associated and B had no response payload": (
                set(history) == {"a", "b", "c"}
                and history["a"].get("text") == "ordinary answer a"
                and not history["b"].get("text")
                and bool(history["b"].get("omitted"))
                and history["c"].get("text") == "ordinary answer c"
                and down == _answer("a") + _answer("c")
                and len(omissions) == ends == 1
                and fixture.actual_cleanup()
            ),
        }
        assert all(checks.values()), checks


async def test_ws_rules_revocation_while_waiting_for_actual_screen_blocks_validation(tmp_path, monkeypatch):
    async with _relay(tmp_path, monkeypatch, _RULES) as (fixture, mediator, recorder, store, directory):
        fixture.client.block_terminal = True
        fixture.screen_gate = _Gate()
        fixture.create("a")
        await fixture.marks.wait("send-1")
        fixture.wire.emit(*_answer("a"))
        await fixture.marks.wait("terminal-blocked")
        fixture.create("b")
        await fixture.marks.wait("decision-2")
        waited = fixture.validations == fixture.owners == fixture.send_calls == 1
        fixture.client.terminal_release.set()
        await asyncio.wait_for(fixture.screen_gate.entered.wait(), _TIMEOUT)
        reached = fixture.finalizer_jobs == 1 and not fixture.screen_gate.expired
        mediator.engine.rules_changed()
        fixture.screen_gate.release.set()
        await fixture.join(disconnect=False)
        history = _history(recorder, store)
        checks = {
            "B reached initial decision before actual A screen gate": (
                fixture.allowed == [True, True] and waited and reached
            ),
            "B neither validated nor sent nor allocated after rules revocation": (
                fixture.validations == fixture.send_calls == fixture.owners == 1
                and fixture.wire.sent_types == ["response.create"]
            ),
            "completed A safe and policy close drained": (
                set(history) == {"a"}
                and history["a"].get("text") == "ordinary answer a"
                and _raw(directory) == (_answer("a"), [], 1)
                and fixture.actual_cleanup(1008)
            ),
        }
        assert all(checks.values()), checks


async def test_ws_turn_answered_whole_is_recorded_whole_when_its_client_goes_while_it_is_screened(
    tmp_path, monkeypatch
):
    """F2 of the P2 baseline, its order forced: the client goes right after the turn's last event reached it, while the
    engine's thread screens the turn (Codex's ``codex exec`` closes so after its last turn). The relay ends without
    waiting for that screening, and the turn is recorded whole, its raw capture ending after its publication."""
    async with _relay(tmp_path, monkeypatch, _GONE) as (fixture, mediator, recorder, store, directory):
        fixture.screen_gate = _Gate()
        fixture.create("a")
        await fixture.marks.wait("send-1")
        fixture.wire.emit(*_answer("a"))
        await fixture.marks.wait("terminal-a")
        await asyncio.wait_for(fixture.screen_gate.entered.wait(), _TIMEOUT)
        fixture.disconnect()
        assert fixture.task is not None
        await asyncio.wait({fixture.task}, timeout=1.0)
        ended_while_screened = fixture.task.done() and not fixture.screen_gate.release.is_set()
        unpublished = fixture.published == 0 and not store.list()[0]
        settling = set(response_finalizer._ALONE.get(mediator.executor, ()))
        fixture.screen_gate.release.set()
        await fixture.marks.wait("published-1")
        await fixture.join(disconnect=False)
        # Its settlement's end: what was left to it (the capture's end) runs before this wait's own callback.
        await asyncio.wait_for(asyncio.gather(*settling), _TIMEOUT)
        alone = len(settling) == 1 and mediator.executor not in response_finalizer._ALONE
        history = _history(recorder, store)
        rows = _records(directory)
        checks = {
            "the relay ended while the engine's thread screened the turn": ended_while_screened
            and unpublished
            and alone,
            "the turn recorded whole": set(history) == {"a"}
            and history["a"].get("text") == "ordinary answer a"
            and "omitted" not in history["a"],
            "its raw capture whole, its end last": _raw(directory) == (_answer("a"), [], 1) and rows[-1]["t"] == "end",
            "relayed whole to the client and closed": tuple(fixture.client.texts()) == _answer("a")
            and fixture.actual_cleanup()
            and not fixture.screen_gate.expired,
        }
        assert all(checks.values()), checks


async def test_ws_cancel_target_expires_across_terminal_barrier(tmp_path, monkeypatch):
    async with _relay(tmp_path, monkeypatch, _TARGET) as (fixture, _, recorder, store, directory):
        fixture.restore_gate = asyncio.Event()
        await _start(fixture)
        fixture.wire.emit(*_answer("a")[1:])
        await fixture.marks.wait("restore-blocked")
        fixture.cancel()
        await fixture.marks.wait("decision-2")
        predicate = fixture.targets[0] if len(fixture.targets) == 1 else None
        before = (
            fixture.cancel_arena is not None
            and predicate is not None
            and fixture.cancel_arena.control_current is predicate
            and predicate()
        )
        waited = fixture.seals == fixture.owners == fixture.validations == fixture.send_calls == 1
        fixture.restore_gate.set()
        await fixture.marks.wait("local-error")
        expired = (
            fixture.cancel_arena is not None
            and predicate is not None
            and fixture.cancel_arena.control_current is predicate
            and len(fixture.targets) == 1
            and not predicate()
        )
        fixture.cancel_arena = None
        await fixture.join()
        history = _history(recorder, store)
        checks = {
            "initial actual cancel target was live during closing barrier": (
                fixture.allowed == [True, True] and fixture.target_at_decision is True and before and waited
            ),
            "same target predicate expired without validation or send": (
                expired
                and fixture.validations == fixture.send_calls == fixture.begins == 1
                and fixture.wire.sent_types == ["response.create"]
            ),
            "one safe A and normal drained close": (
                set(history) == {"a"}
                and history["a"].get("text") == "ordinary answer a"
                and _raw(directory) == (_answer("a"), [], 1)
                and fixture.actual_cleanup()
            ),
        }
        assert all(checks.values()), checks
