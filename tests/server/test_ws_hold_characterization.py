"""WebSocket holds on a real ``StoredVault``, a person's space and an API key's (P1-4's first step, RC-REC-11 (c); the
review's I-1): every way a turn ends, the hold its masking took is released once, at the turn's end and only after its
last event was restored, or as a refusal is answered, or as the socket ends, and never the hold of a newer turn; what
the turn's answer restores is restored, from the durable store once its values left memory; nothing is sent twice.

Each case drives ``ws.relay`` over a scripted provider (a websockets server on 127.0.0.1 that answers each turn with its
input as a delta, then waits at each ``GATE`` for the test, then sends the turn's end events) and counts what the vault
holds (``_held``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.config import Config, IdentityConfig, LiveReview
from latet.engine import stored
from latet.engine.build import build
from latet.engine.stored import HOLD_SECONDS, StoredVault
from latet.gateway import ws
from latet.gateway.approvals import Decision
from latet.gateway.capture import Capture
from latet.gateway.mediate import Mediator, WSResponseRewriter
from latet.gateway.routes import RouteTable, Upstream

from ..conftest import ROOT_KEY
from ..unit.test_request_outcomes import EMAIL, _Client, _until
from ..unit.test_stored_vault import _many, _Store
from .support import listening

pytestmark = pytest.mark.server  # a server's test: CI's job with the services runs it (test_services_guard)

GATE = "gate"
COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
FAILED = {"type": "response.failed", "response": {"status": "failed"}}
ERROR = {"type": "error", "error": {"type": "server_error", "message": "overloaded, retrying"}}
PEOPLE = pytest.mark.parametrize("person", [True, False], ids=["a person's space", "an API key's"])


def _mediator(person: bool, working: int = 10) -> tuple[Mediator, StoredVault]:
    vault = StoredVault(ROOT_KEY, _Store(), working=working, glued_all=person)  # pyright: ignore[reportArgumentType]
    built = build(Config(identity=IdentityConfig(enabled=False)), vault)
    return Mediator(built.engine, built.verifier), vault


def _held(vault: StoredVault) -> int:
    return sum(hold.count for hold in list(vault._sessions.values()))


class _Provider:
    """Each turn answered by its script (default: ``GATE``, then ``COMPLETED``), after a delta that says the turn's
    input (``echo`` "first": the first turn's, a fake of an earlier turn's value)."""

    def __init__(self, scripts: list[list[Any]] | None = None, echo: str = "own") -> None:
        self.scripts, self.echo = scripts or [], echo
        self.got: list[str] = []
        self.gates = [threading.Event() for _ in range(8)]
        self.waiting = 0
        self.server = serve(self._answer, "127.0.0.1", 0, open_timeout=2, close_timeout=1)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self) -> None:
        with contextlib.suppress(OSError):
            self.server.serve_forever()

    def _answer(self, connection: Any) -> None:
        gate = 0
        with contextlib.suppress(ConnectionClosed, TimeoutError):
            for n, message in enumerate(connection):
                self.got.append(message)
                said = json.loads(self.got[0 if self.echo == "first" else n])["input"]
                delta = {"type": "response.output_text.delta", "item_id": f"t{n}", "content_index": 0, "delta": said}
                connection.send(json.dumps(delta))
                for step in self.scripts[n] if n < len(self.scripts) else [GATE, COMPLETED]:
                    if step == GATE:
                        self.waiting = gate + 1
                        if not self.gates[gate].wait(10):
                            return
                        gate += 1
                    else:
                        connection.send(json.dumps(step))

    def open(self, gate: int) -> None:
        self.gates[gate].set()

    def close(self) -> None:
        for gate in self.gates:
            gate.set()
        self.server.shutdown()
        self.thread.join(timeout=5)


class _Socket:
    """One client's WebSocket through ``ws.relay`` (its task) to the provider."""

    def __init__(self, mediator: Mediator, provider: _Provider) -> None:
        self.client = _Client()
        routes = RouteTable([Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}")])
        self.task = asyncio.create_task(ws.relay(self.client.socket, routes, Capture(None), mediator))

    def turn(self, text: str) -> None:
        frame = {"type": "response.create", "model": "gpt-5.1-codex", "input": text}  # Codex's model, by grammar
        self.client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def seen(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in map(json.loads, self.client.texts()) if event.get("type") == kind]

    def restored(self) -> str:
        return "".join(event["delta"] for event in self.seen("response.output_text.delta"))

    async def end(self) -> None:
        if not self.task.done():
            self.client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.task, 10)


async def _drained(mediator: Mediator) -> None:
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)


@PEOPLE
@pytest.mark.parametrize("working", [10, 0])
async def test_a_turn_holds_until_its_last_event_is_restored_and_then_lets_go(person, working, monkeypatch):
    mediator, vault = _mediator(person, working)
    at_end: list[int] = []
    restored = WSResponseRewriter.restored

    async def watched(self: Any, data: str) -> Any:
        if '"response.completed"' in data:
            at_end.append(_held(vault))  # the hold, as the end event is restored
        return await restored(self, data)

    monkeypatch.setattr(WSResponseRewriter, "restored", watched)
    provider = _Provider()
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        assert _held(vault) == 1  # held while its answer is awaited
        provider.open(0)
        await _until(lambda: bool(socket.seen("response.completed")))
        await _until(lambda: _held(vault) == 0)
        assert at_end == [1] and not socket.task.done()  # let go after its end was restored, the socket still open
        assert EMAIL in socket.restored()
    finally:
        await socket.end()
        provider.close()
    assert len(provider.got) == 1 and EMAIL not in provider.got[0] and _held(vault) == 0


@PEOPLE
async def test_a_superseded_turn_whose_hold_expired_lets_go_of_its_own_never_the_newer(person, monkeypatch):
    """A turn sent while the previous answer still streams, the previous turn's hold expired meanwhile (a newer hold
    taken by the new turn): the old turn's end takes nothing of the newer hold; oldest turn first, each released once."""
    clock = [1_000.0]
    monkeypatch.setattr(stored, "time", type("Clock", (), {"monotonic": lambda: clock[0], "time": time.time}))
    mediator, vault = _mediator(person)
    provider = _Provider()
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        first = vault._sessions[next(iter(vault._sessions))]
        clock[0] += HOLD_SECONDS + 1  # its hold expires
        socket.turn(f"and again to {EMAIL}")
        await _until(lambda: any(hold is not first for hold in vault._sessions.values()))
        assert _held(vault) == 1  # the newer turn's own hold
        provider.open(0)  # the first turn's end
        await _until(lambda: len(socket.seen("response.completed")) == 1)
        await asyncio.sleep(0.05)
        assert _held(vault) == 1  # the newer hold kept while its answer is awaited
        await _until(lambda: provider.waiting == 2)
        provider.open(1)
        await _until(lambda: len(socket.seen("response.completed")) == 2)
        await _until(lambda: _held(vault) == 0)
        assert EMAIL in socket.restored()
    finally:
        await socket.end()
        provider.close()
    assert len(provider.got) == 2 and _held(vault) == 0


@PEOPLE
async def test_a_blocked_turn_lets_go_of_its_hold_as_its_refusal_is_answered(person, monkeypatch):
    mediator, vault = _mediator(person)

    async def refused(batch: Any, gone: Any, held: Any = None) -> Decision:
        return Decision(False, reason="you refused it")

    monkeypatch.setattr(mediator, "live_review", LiveReview(enabled=True))
    monkeypatch.setattr(mediator, "await_decision", refused)
    provider = _Provider()
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: bool(socket.seen("error")))
        await _until(lambda: _held(vault) == 0, 2)
        assert not socket.task.done() and provider.got == []  # refused: the socket open, nothing sent
    finally:
        await socket.end()
        provider.close()


@PEOPLE
async def test_a_turn_given_up_while_it_masks_lets_go_once_the_masking_ends(person, monkeypatch):
    """The relay cancelled (its space closing) while the engine's thread masks a turn: what that masking holds is
    released as it ends; nothing is sent."""
    mediator, vault = _mediator(person)
    entered, gate = threading.Event(), threading.Event()
    mask = mediator.mask_ws_frame

    def masking(*args: Any) -> Any:
        found = mask(*args)
        entered.set()
        gate.wait(10)
        return found

    monkeypatch.setattr(mediator, "mask_ws_frame", masking)
    provider = _Provider()
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        assert await asyncio.to_thread(entered.wait, 10)
        socket.task.cancel()  # its space closes (``Space.end_long``)
        await socket.end()
        assert _held(vault) == 1  # still masking
        gate.set()
        await _drained(mediator)
        assert _held(vault) == 0 and provider.got == []
    finally:
        gate.set()
        provider.close()


@PEOPLE
@pytest.mark.parametrize("how", ["its space closes", "its client goes"])
async def test_a_turn_cut_short_mid_answer_lets_go_as_the_socket_ends(person, how):
    mediator, vault = _mediator(person)
    provider = _Provider()
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        assert _held(vault) == 1
        if how == "its space closes":
            socket.task.cancel()
        await socket.end()
        assert _held(vault) == 0 and len(provider.got) == 1
    finally:
        provider.close()


@PEOPLE
async def test_a_failed_answer_lets_go_at_its_end_and_an_error_mid_answer_does_not(person):
    """An upstream ``error`` event while the answer goes on keeps the turn's hold (more of the answer is still to be
    restored, the review's M-4); ``response.failed`` ends the answer and lets go."""
    mediator, vault = _mediator(person)
    provider = _Provider([[GATE, ERROR, GATE, FAILED]])
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        provider.open(0)
        await _until(lambda: bool(socket.seen("error")))
        await asyncio.sleep(0.05)
        assert _held(vault) == 1  # an error mid-answer: still held
        provider.open(1)
        await _until(lambda: bool(socket.seen("response.failed")))
        await _until(lambda: _held(vault) == 0)
    finally:
        await socket.end()
        provider.close()
    assert len(provider.got) == 1


@PEOPLE
async def test_a_later_turn_restores_from_the_durable_store_what_left_memory_after_a_release(person):
    """A turn ended and released, its values pushed out of memory by others (an API key's vault keeps a working set): a
    later turn's answer that names the earlier fake is restored, the values brought back from the store."""
    mediator, vault = _mediator(person)
    provider = _Provider(echo="first")
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        provider.open(0)
        await _until(lambda: _held(vault) == 0)
        entity = vault.by_original(EMAIL)
        assert entity is not None
        _many(vault, 500)
        if not person:
            assert vault.in_memory(entity.id) is None  # out of memory once released
        socket.turn("and once more, please")
        await _until(lambda: provider.waiting == 2)
        provider.open(1)
        await _until(lambda: len(socket.seen("response.completed")) == 2)
        deltas = [event["delta"] for event in socket.seen("response.output_text.delta")]
        assert len(deltas) == 2 and EMAIL in deltas[1]  # the earlier fake, restored in the later answer
        await _until(lambda: _held(vault) == 0)
    finally:
        await socket.end()
        provider.close()


@PEOPLE
@pytest.mark.parametrize("worker", [0, 1])
async def test_instances_turns_release_after_restore_and_reload_evicted_values(instances, person, worker, monkeypatch):
    """G1's real backends cell: each turn holds until its last restore, then releases once. After the first
    turn's value leaves memory, the next answer restores its fake from PostgreSQL, on either instance."""
    a, b = instances
    # Exercise the working-set path of a person's space without making 35,000 unrelated values.
    monkeypatch.setattr(stored, "WHOLE_VALUES", 10)
    for instance in instances:
        instance.settings.key_working_values = 10
    identity, _ = a.identities.add_person("WS person") if person else a.identities.add_key("WS key")
    async with listening(a, b):
        runtimes = [await instance.spaces.runtime(identity) for instance in instances]
        runtime = runtimes[worker]
        mediator, vault = runtime.mediator, runtime.built.vault
        assert isinstance(vault, StoredVault)
        releases: list[object] = []
        at_end: list[int] = []
        release = mediator.engine.release
        restored = WSResponseRewriter.restored

        def counted(session: Any, held: object) -> None:
            releases.append(held)
            release(session, held)

        async def watched(self: Any, data: str) -> Any:
            if '"response.completed"' in data:
                at_end.append(_held(vault))
            return await restored(self, data)

        with monkeypatch.context() as patch:
            patch.setattr(mediator.engine, "release", counted)
            patch.setattr(WSResponseRewriter, "restored", watched)
            provider = _Provider(echo="first")
            socket = _Socket(mediator, provider)
            try:
                socket.turn(f"write to {EMAIL}")
                await _until(lambda: provider.waiting == 1)
                assert _held(vault) == 1 and releases == []
                provider.open(0)
                await _until(lambda: len(socket.seen("response.completed")) == 1 and _held(vault) == 0)
                entity = await asyncio.to_thread(vault.by_original, EMAIL)
                assert entity is not None
                assert len(releases) == 1 and at_end == [1] and EMAIL in socket.restored()
                await asyncio.to_thread(_many, vault, 40)
                assert vault.in_memory(entity.id) is None
                assert await asyncio.to_thread(runtime.store.load_entity, entity.id) == entity
                retrieved: list[str] = []
                read = runtime.store._read

                def reading(*args: Any, **kwargs: Any) -> Any:
                    found = read(*args, **kwargs)
                    retrieved.extend(entity.id for entity in found)
                    return found

                patch.setattr(runtime.store, "_read", reading)
                socket.turn("once more, please")
                await _until(lambda: provider.waiting == 2)
                assert _held(vault) == 1 and len(releases) == 1
                provider.open(1)
                await _until(lambda: len(socket.seen("response.completed")) == 2 and _held(vault) == 0)
                assert [event["delta"] for event in socket.seen("response.output_text.delta")] == [
                    f"write to {EMAIL}",
                    f"write to {EMAIL}",
                ]
                assert entity.id in retrieved  # the earlier value came back through a real PostgreSQL query
                assert at_end == [1, 1] and len(releases) == 2
                assert releases[0] is not releases[1]
                assert len(provider.got) == 2 and all(EMAIL not in sent for sent in provider.got)
            finally:
                await socket.end()
                provider.close()
            assert len(releases) == 2 and _held(vault) == 0


@PEOPLE
async def test_repeated_terminal_errors_keep_one_connection_owner_and_release_each_superseded_hold(person, monkeypatch):
    """An error can be mid-answer: retries consolidate ownership without dropping restoration or refusing traffic."""
    mediator, vault = _mediator(person, working=0)
    releases: list[object] = []
    release = mediator.engine.release

    def released(session: Any, handle: object) -> None:
        releases.append(handle)
        release(session, handle)

    monkeypatch.setattr(mediator.engine, "release", released)
    provider = _Provider([[ERROR] for _ in range(12)], echo="first")
    socket = _Socket(mediator, provider)
    try:
        for n in range(12):
            socket.turn(f"write to {EMAIL}" if n == 0 else "retry please")
            await _until(lambda n=n: len(socket.seen("error")) == n + 1)
            assert _held(vault) == 1  # independent of retry count, still protects a possible continuation
            assert len(releases) == n  # successful replacement owns before old generation releases
            assert [event["delta"] for event in socket.seen("response.output_text.delta")] == [f"write to {EMAIL}"] * (
                n + 1
            )
            assert not socket.task.done() and len(provider.got) == n + 1
    finally:
        await socket.end()
        provider.close()
    assert _held(vault) == 0 and len(releases) == 12


@PEOPLE
async def test_supersede_preserves_late_old_and_new_restoration_under_one_current_owner(person, monkeypatch):
    """Old completion consumes its own scalar generation, retaining the newer owner through its last restore."""
    mediator, vault = _mediator(person, working=0)
    late = {"type": "response.completed", "response": {"status": "completed"}}
    provider = _Provider([[GATE, late], [GATE, COMPLETED]], echo="first")
    socket = _Socket(mediator, provider)
    left: list[Any] = []
    original_left = mediator.left

    def sent(arena: Any) -> None:
        left.append(arena)
        original_left(arena)

    monkeypatch.setattr(mediator, "left", sent)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        fake_text = json.loads(provider.got[0])["input"]
        late["response"]["output"] = [{"type": "message", "content": [{"type": "output_text", "text": fake_text}]}]
        socket.turn("please continue")
        await _until(lambda: len(left) == 2)
        assert _held(vault) == 1  # replacement covers the conversation before superseded owner releases
        provider.open(0)
        await _until(lambda: len(socket.seen("response.completed")) == 1 and provider.waiting == 2)
        first = socket.seen("response.completed")[0]
        assert first["response"]["output"][0]["content"][0]["text"] == f"write to {EMAIL}"
        assert _held(vault) == 1  # old completion cannot release the newer generation
        provider.open(1)
        await _until(lambda: len(socket.seen("response.completed")) == 2 and _held(vault) == 0)
        assert [event["delta"] for event in socket.seen("response.output_text.delta")] == [f"write to {EMAIL}"] * 2
    finally:
        await socket.end()
        provider.close()
    assert _held(vault) == 0 and len(provider.got) == 2


@PEOPLE
async def test_a_completion_during_send_releases_pending_without_publishing_an_owner(person, monkeypatch):
    """A provider may finish before send_frame resumes; that pending generation still ends exactly once."""
    mediator, vault = _mediator(person, working=0)
    provider = _Provider([[COMPLETED]])
    socket = _Socket(mediator, provider)
    send = ws.request_transport.send_frame
    releases: list[object] = []
    release = mediator.engine.release

    def released(session: Any, handle: object) -> None:
        releases.append(handle)
        release(session, handle)

    async def completed_before_return(*args: Any) -> Any:
        result = await send(*args)
        await _until(lambda: bool(socket.seen("response.completed")))
        return result

    monkeypatch.setattr(mediator.engine, "release", released)
    monkeypatch.setattr(ws.request_transport, "send_frame", completed_before_return)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: bool(socket.seen("response.completed")) and _held(vault) == 0)
        assert EMAIL in socket.restored() and len(releases) == 1
    finally:
        await socket.end()
        provider.close()
    assert len(releases) == 1 and _held(vault) == 0


@PEOPLE
async def test_a_refused_replacement_releases_an_old_owner_completed_during_its_send(person, monkeypatch):
    """Rolling back a reserved generation also ends an old owner whose completion arrived during the await."""
    mediator, vault = _mediator(person, working=0)
    provider = _Provider([[GATE, COMPLETED]])
    socket = _Socket(mediator, provider)
    send = ws.request_transport.send_frame
    attempted = 0
    releases: list[object] = []
    release = mediator.engine.release

    def released(session: Any, handle: object) -> None:
        releases.append(handle)
        release(session, handle)

    async def refused_after_old_end(*args: Any) -> Any:
        nonlocal attempted
        attempted += 1
        if attempted == 1:
            return await send(*args)
        provider.open(0)
        await _until(lambda: bool(socket.seen("response.completed")))
        return ws.SendRefusal("client")

    monkeypatch.setattr(mediator.engine, "release", released)
    monkeypatch.setattr(ws.request_transport, "send_frame", refused_after_old_end)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        socket.turn("replacement")
        await _until(lambda: bool(socket.seen("error")))
        assert _held(vault) == 0 and len(releases) == 2
        assert EMAIL in socket.restored() and len(provider.got) == 1 and not socket.task.done()
    finally:
        await socket.end()
        provider.close()
    assert _held(vault) == 0 and len(releases) == 2
