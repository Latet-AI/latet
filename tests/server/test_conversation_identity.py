"""Identical logical chat openings through real identity-scoped PostgreSQL/Valkey spaces."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, ExitStack, aclosing
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.engine import stored
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.routes import RouteTable

from ..conversation_identity import FIRST, OPENAI, SECOND, answer, fake, name_response, prepare, release, request
from .support import listening

pytestmark = pytest.mark.server


@pytest.mark.parametrize("kind", ["person", "key"])
@pytest.mark.parametrize("explicit", [False, True], ids=["same-fallback", "same-explicit-id"])
async def test_identical_ids_never_restore_another_server_spaces_original(server, kind, explicit):
    add = server.identities.add_person if kind == "person" else server.identities.add_key
    owner_a, _ = add("Scope A")
    owner_b, _ = add("Scope B")
    async with listening(server):
        a, b = await server.spaces.runtime(owner_a), await server.spaces.runtime(owner_b)

        def probe():
            headers = {"session-id": "same-client-id"} if explicit else {}
            pa = prepare(a.mediator, request(FIRST), headers)
            pb = prepare(b.mediator, request(SECOND), headers)
            try:
                fa, fb = fake(a.mediator, FIRST, pa), fake(b.mediator, SECOND, pb)
                assert pa.session is not None and pb.session is not None and pa.session.key == pb.session.key
                # Separate vaults need not draw globally unique fakes. Even if these happen to match, each reply
                # must use only its own vault's association; the dedicated case below forces that coincidence.
                in_b, in_a = (SECOND, FIRST) if fa == fb else (fa, fb)
                assert answer(b.mediator, pb, f"{fa} | {fb}") == f"{in_b} | {SECOND}"
                assert answer(a.mediator, pa, f"{fa} | {fb}") == f"{FIRST} | {in_a}"
                reflected = prepare(b.mediator, request(f"{SECOND} | {fa}"), headers)
                try:
                    # Even actively submitting the foreign fake learns only its literal bytes in B's vault.
                    echo = fake(b.mediator, fa, reflected)
                    assert answer(b.mediator, reflected, echo) == fa
                    assert FIRST not in answer(b.mediator, reflected, f"{fa} | {echo}")
                    assert b.built.vault.by_original(FIRST) is None
                finally:
                    release(b.mediator, reflected)
            finally:
                release(a.mediator, pa)
                release(b.mediator, pb)

        await asyncio.to_thread(probe)


@pytest.mark.parametrize("kind", ["person", "key"])
async def test_the_same_fake_in_two_vaults_restores_only_the_receiving_spaces_original(server, kind):
    add = server.identities.add_person if kind == "person" else server.identities.add_key
    owner_a, _ = add("Collision A")
    owner_b, _ = add("Collision B")
    async with listening(server):
        a, b = await server.spaces.runtime(owner_a), await server.spaces.runtime(owner_b)

        def probe():
            shared_fake = "same-placeholder-741902"
            # Control only candidate generation. Real intern, collision checks, persistence, request masking,
            # entity association and response restoration all remain active in both independently keyed vaults.
            for runtime, original in ((a, FIRST), (b, SECOND)):
                runtime.built.vault.intern(
                    "BOUNDARY", EntityClass.CUSTOM, original, generate=lambda _value, _rng: shared_fake
                )
            with ExitStack() as cleanup:
                pa = prepare(a.mediator, request(FIRST))
                cleanup.callback(release, a.mediator, pa)
                pb = prepare(b.mediator, request(SECOND))
                cleanup.callback(release, b.mediator, pb)
                assert fake(a.mediator, FIRST, pa) == fake(b.mediator, SECOND, pb) == shared_fake
                assert answer(a.mediator, pa, shared_fake) == FIRST
                assert answer(b.mediator, pb, shared_fake) == SECOND
                # Identical fake bytes need not be globally unique; neither vault acquires the other's original.
                assert a.built.vault.by_original(SECOND) is None
                assert b.built.vault.by_original(FIRST) is None

        await asyncio.to_thread(probe)


@pytest.mark.parametrize("kind", ["person", "key"])
async def test_response_names_are_local_to_the_server_space_that_received_them(server, kind):
    add = server.identities.add_person if kind == "person" else server.identities.add_key
    owner_a, _ = add("Responses A")
    owner_b, _ = add("Responses B")
    async with listening(server):
        a, b = await server.spaces.runtime(owner_a), await server.spaces.runtime(owner_b)

        def probe():
            with ExitStack() as cleanup:
                pa = prepare(a.mediator, request(FIRST, responses=True, prompt_cache_key="a"), responses=True)
                cleanup.callback(release, a.mediator, pa)
                pb = prepare(b.mediator, request(SECOND, responses=True, prompt_cache_key="b"), responses=True)
                cleanup.callback(release, b.mediator, pb)
                fa, fb = fake(a.mediator, FIRST, pa), fake(b.mediator, SECOND, pb)
                in_b, in_a = (SECOND, FIRST) if fa == fb else (fa, fb)
                name_response(a.mediator, pa, "resp_same_name")
                assert a.built.engine.conversations.flush(5)
                continuation = request("Continue.", responses=True, previous_response_id="resp_same_name")
                own = prepare(a.mediator, continuation, responses=True)
                cleanup.callback(release, a.mediator, own)
                foreign = prepare(b.mediator, continuation, responses=True)
                cleanup.callback(release, b.mediator, foreign)
                assert answer(a.mediator, own, f"{fa} | {fb}") == f"{FIRST} | {in_a}"
                assert answer(b.mediator, foreign, f"{fa} | {fb}") == f"{fa} | {fb}"
                # B can receive exactly the same provider ID for its own response. Its association is B's;
                # A's already named response keeps its own meaning.
                name_response(b.mediator, pb, "resp_same_name")
                assert b.built.engine.conversations.flush(5)
                own_b = prepare(b.mediator, continuation, responses=True)
                cleanup.callback(release, b.mediator, own_b)
                own_a = prepare(a.mediator, continuation, responses=True)
                cleanup.callback(release, a.mediator, own_a)
                assert answer(b.mediator, own_b, f"{fa} | {fb}") == f"{in_b} | {SECOND}"
                assert answer(a.mediator, own_a, f"{fa} | {fb}") == f"{FIRST} | {in_a}"

        await asyncio.to_thread(probe)


def _http_request(raw: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/openai/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:5283"), (b"content-type", b"application/json")],
            "server": ("127.0.0.1", 5283),
        },
        receive,
    )


def _stream_text(chunks) -> str:
    raw = b"".join(chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks)
    return "".join(
        choice.get("delta", {}).get("content", "")
        for line in raw.splitlines()
        if line.startswith(b"data: ") and line != b"data: [DONE]"
        for choice in json.loads(line[6:]).get("choices", [])
    )


@pytest.mark.parametrize("end_old", ["complete", "cancel"])
async def test_fallback_streams_release_only_their_own_hold_generation(server, monkeypatch, end_old):
    owner, _ = server.identities.add_key("Stream lifetime")
    now = [100.0]
    # Replace only StoredVault's clock reference; server timeouts, Valkey TTLs and asyncio retain real clocks.
    monkeypatch.setattr(stored, "time", SimpleNamespace(monotonic=lambda: now[0]))
    async with listening(server), asyncio.timeout(30):
        runtime = await server.spaces.runtime(owner)
        mediator, vault = runtime.mediator, runtime.built.vault
        assert isinstance(vault, StoredVault)
        sent = []

        def provider(outgoing: httpx.Request) -> httpx.Response:
            sent.append(json.loads(outgoing.content)["messages"][-1]["content"])
            assert FIRST not in outgoing.content.decode() and SECOND not in outgoing.content.decode()
            # A reply echoes both logical chats once B has arrived, split across actual SSE delta events.
            text = " | ".join(dict.fromkeys(sent))
            middle = len(text) // 2
            events = [
                {"choices": [{"index": 0, "delta": {"content": piece}}]} for piece in (text[:middle], text[middle:])
            ]
            body = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
            body += b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(body))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client, AsyncExitStack() as cleanup:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator=mediator)

            async def start(value):
                response = await forwarder.handle(_http_request(request(value, stream=True)))
                assert isinstance(response, StreamingResponse) and response.status_code == 200
                iterator = response.body_iterator
                assert isinstance(iterator, AsyncGenerator)
                await cleanup.enter_async_context(aclosing(iterator))
                # Start the relay so cancellation/cleanup executes its real finally block and closes its response.
                first = await anext(iterator)
                return iterator, [first]

            async def drain(stream):
                iterator, chunks = stream
                chunks.extend([chunk async for chunk in iterator])
                return _stream_text(chunks)

            first = await start(FIRST)
            assert len(vault._sessions) == 1
            key, old_hold = next(iter(vault._sessions.items()))
            assert key.startswith("conv:") and old_hold.count == 1
            second = await start(SECOND)
            assert vault._sessions[key] is old_hold and old_hold.count == 2
            # Both streams remain open. A further stateless request after the hold's lifetime starts a new
            # generation under precisely the same fallback, retaining the durable conversation entities.
            now[0] += stored.HOLD_SECONDS + 1
            newest = await start(FIRST)
            new_hold = vault._sessions[key]
            assert new_hold is not old_hold and new_hold.count == 1
            assert await drain(first) == FIRST
            assert vault._sessions[key] is new_hold and new_hold.count == 1
            if end_old == "complete":
                assert await drain(second) == f"{FIRST} | {SECOND}"
            else:
                with pytest.raises(asyncio.CancelledError):
                    await second[0].athrow(asyncio.CancelledError())
            assert vault._sessions[key] is new_hold and new_hold.count == 1
            assert await drain(newest) == f"{FIRST} | {SECOND}"
            assert key not in vault._sessions
            # Releasing the final stream never deletes either protected value from durable storage.
            continuation = await mediator.run(prepare, mediator, request(FIRST))
            try:
                fa, fb = sent[:2]
                assert await mediator.run(answer, mediator, continuation, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
            finally:
                release(mediator, continuation)


async def test_same_space_fallback_shares_restoration_and_counts_overlapping_holds(server):
    owner, _ = server.identities.add_key("Scope holds")
    async with listening(server):
        runtime = await server.spaces.runtime(owner)

        def probe():
            mediator, vault = runtime.mediator, runtime.built.vault
            assert isinstance(vault, StoredVault)
            a = prepare(mediator, request(FIRST))
            b = prepare(mediator, request(SECOND))
            try:
                fa, fb = fake(mediator, FIRST, a), fake(mediator, SECOND, b)
                assert a.session is not None and b.session is not None
                assert a.hold is b.hold and vault._sessions[a.session.key].count == 2
                assert answer(mediator, b, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
                release(mediator, a)
                assert vault._sessions[b.session.key].count == 1
                assert answer(mediator, b, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
                release(mediator, b)
                assert b.session.key not in vault._sessions
                # Release drops a memory hold, not durable values or stateless continuation.
                continued = prepare(mediator, request(FIRST))
                try:
                    assert answer(mediator, continued, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
                finally:
                    release(mediator, continued)
            finally:
                release(mediator, a, b)

        await asyncio.to_thread(probe)
