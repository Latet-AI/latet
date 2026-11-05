"""G1 durable restoration across workers: dispose the first worker's runtime and every cached restore before a
previously unused worker restores the earlier fake from PostgreSQL plus the conversation persisted in Valkey."""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import Any

import pytest

from latet.engine.stored import StoredVault
from latet.server.live import ValkeyConversations

from ..unit.test_request_outcomes import EMAIL, _until
from .support import listening
from .test_ws_hold_characterization import _drained, _held, _Provider, _Socket

pytestmark = pytest.mark.server


async def _learn_and_end(server: Any, identity: Any, provider: _Provider) -> tuple[weakref.ReferenceType[Any], ...]:
    """Leave only weak references to the first worker's original-bearing objects, plus the provider's masked input."""
    runtime = await server.spaces.runtime(identity)
    mediator, vault = runtime.mediator, runtime.built.vault
    assert isinstance(vault, StoredVault)
    socket = _Socket(mediator, provider)
    try:
        socket.turn(f"write to {EMAIL}")
        await _until(lambda: provider.waiting == 1)
        assert _held(vault) == 1
        provider.open(0)
        await _until(lambda: bool(socket.seen("response.completed")) and _held(vault) == 0)
        assert socket.restored() == f"write to {EMAIL}"
        assert len(provider.got) == 1 and EMAIL not in provider.got[0]
    finally:
        await socket.end()
    # Response provenance finishes before its writer is flushed; space closure flushes it again as part of disposal.
    await _drained(mediator)
    assert runtime.backends is not None
    conversations = runtime.backends.conversations
    assert isinstance(conversations, ValkeyConversations)
    assert await asyncio.to_thread(conversations.flush)
    return tuple(weakref.ref(value) for value in (runtime, mediator, mediator.engine, vault, conversations))


def _disposed(references: tuple[weakref.ReferenceType[Any], ...]) -> bool:
    gc.collect()
    return all(reference() is None for reference in references)


@pytest.mark.parametrize("person", [True, False], ids=["person", "key"])
async def test_fresh_worker_restores_previous_fake_after_first_runtime_is_disposed(instances, person, monkeypatch):
    """A's complete turn is restored and released, then A's runtime is collected. B has never built this identity;
    its new socket names the same durable conversation and sends no original, yet restores A's earlier fake."""
    a, b = instances
    identity, _ = a.identities.add_person("Restart person") if person else a.identities.add_key("Restart key")
    assert a.spaces.count() == b.spaces.count() == 0
    # A fresh key space starts with no recent values. Its conversation's old entity must be loaded from PostgreSQL.
    b.settings.key_working_values = 10 if person else 0
    provider = _Provider(echo="first")
    try:
        async with listening(a):
            old = await _learn_and_end(a, identity, provider)
            assert b.spaces.count() == 0 and not b.listening.is_set()
            await a.spaces.close()
        assert a.spaces.count() == b.spaces.count() == 0
        await _until(lambda: _disposed(old))
        # B's first build occurs only after every original-bearing A runtime object above has been collected.
        async with listening(b):
            runtime = await b.spaces.runtime(identity)
            mediator, vault = runtime.mediator, runtime.built.vault
            assert isinstance(vault, StoredVault)
            holds: list[object] = []
            releases: list[object] = []
            counts: list[int] = []
            hold, release = mediator.engine.hold, mediator.engine.release

            def taking(session: Any) -> object:
                handle = hold(session)
                holds.append(handle)
                return handle

            def letting_go(session: Any, handle: object) -> None:
                counts.append(_held(vault))
                releases.append(handle)
                release(session, handle)

            with monkeypatch.context() as patch:
                patch.setattr(mediator.engine, "hold", taking)
                patch.setattr(mediator.engine, "release", letting_go)
                socket = _Socket(mediator, provider)
                try:
                    socket.turn("once more, please")
                    # A's gate zero is already open on this new provider connection: wait for this socket's end event.
                    await _until(lambda: bool(socket.seen("response.completed")) and _held(vault) == 0)
                    assert socket.restored() == f"write to {EMAIL}"
                    assert len(holds) == 1 and releases == holds and counts == [1]
                    assert len(provider.got) == 2 and all(EMAIL not in frame for frame in provider.got)
                    assert "once more, please" in provider.got[1]
                finally:
                    await socket.end()
                assert len(releases) == 1 and _held(vault) == 0
    finally:
        provider.close()
