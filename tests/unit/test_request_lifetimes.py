"""Exact hold ownership over a real ``StoredVault`` (P1-4's ownership matrix, RC-REC-11; a person's space and an API
key's, P1-0 D-2): however an exchange ends, its conversation's vault holds nothing more for it afterwards, each hold
released once, and an exchange that ends never takes a hold another exchange of the conversation still needs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from latet.config import Config, IdentityConfig, LiveReview
from latet.engine.build import build
from latet.engine.stored import StoredVault
from latet.gateway.approvals import Decision
from latet.gateway.aws import AwsCredentialStore, profile_tag
from latet.gateway.capture import Capture
from latet.gateway.cloud import Signing
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Resolved, RouteTable, Upstream
from latet.gateway.sigv4 import AwsCredentials

from ..conftest import ROOT_KEY
from .test_request_leaks import _forwarder, _Provider, _renewers, _request
from .test_request_outcomes import EMAIL, OPENAI, _chat
from .test_stored_vault import _Store


def _mediator(person: bool) -> tuple[Mediator, StoredVault]:
    vault = StoredVault(ROOT_KEY, _Store(), working=10, glued_all=person)  # pyright: ignore[reportArgumentType]
    built = build(Config(identity=IdentityConfig(enabled=False)), vault)
    return Mediator(built.engine, built.verifier), vault


def _held(vault: StoredVault) -> int:
    return sum(hold.count for hold in list(vault._sessions.values()))


def _streaming(text: str) -> bytes:
    return json.dumps({"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": text}]}).encode()


async def _gone() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _closed(message: dict[str, Any]) -> None:
    raise OSError("the client is gone")


async def _drained(response: Any) -> None:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}, _gone, send)


PEOPLE = pytest.mark.parametrize("person", [True, False], ids=["a person's space", "an API key's"])


@PEOPLE
async def test_every_way_an_http_exchange_ends_leaves_nothing_held(person):
    """Answered whole, streamed to its end, a stream never started, an upstream error: each holds while its answer is
    awaited and nothing afterwards."""
    mediator, vault = _mediator(person)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)
    whole = await forwarder.handle(_request(_chat(f"write to {EMAIL}")))
    assert whole.status_code == 200 and _held(vault) == 0
    streamed = await forwarder.handle(_request(_streaming(f"write to {EMAIL}")))
    assert isinstance(streamed, StreamingResponse) and _held(vault) == 1  # owned by the response until it ends
    await _drained(streamed)
    assert _held(vault) == 0
    unstarted = await forwarder.handle(_request(_streaming(f"and to {EMAIL}")))
    assert _held(vault) == 1
    with pytest.raises(ClientDisconnect):
        await unstarted({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}, _gone, cast(Any, _closed))
    assert _held(vault) == 0 and all(answer.closed for answer in provider.answers[1:])

    def failing(*args: Any) -> Any:
        raise RuntimeError("the transform could not be made")

    mediator.response_transform = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await forwarder.handle(_request(_streaming(f"once more to {EMAIL}")))
    assert _held(vault) == 0  # handle's finally released it: the response that would own it was never made
    await forwarder.client.aclose()


@PEOPLE
async def test_cancellation_at_each_step_leaves_nothing_held_and_sends_nothing(person):
    """Before the masking starts (nothing held), while it masks, and while the Live Reviewer's decision applies: once
    the executor ends, nothing is held, and nothing was sent."""
    mediator, vault = _mediator(person)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)
    busy, entered, gate = threading.Event(), threading.Event(), threading.Event()
    mediator.executor.submit(lambda: (busy.set(), gate.wait(10)))  # the engine's thread busy: masking not started
    assert await asyncio.to_thread(busy.wait, 10)
    task = asyncio.create_task(forwarder.handle(_request(_chat(f"write to {EMAIL}"))))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
    assert _held(vault) == 0
    prepare = mediator.prepare
    gate.clear()

    def masking(*args: Any, **kwargs: Any) -> Any:
        found = prepare(*args, **kwargs)
        entered.set()
        gate.wait(10)
        return found

    mediator.prepare = masking  # type: ignore[method-assign]
    task = asyncio.create_task(forwarder.handle(_request(_chat(f"write to {EMAIL}"))))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert _held(vault) == 1  # still masking
    gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
    assert _held(vault) == 0
    mediator.prepare = prepare  # type: ignore[method-assign]
    engine = mediator.engine
    reacquire = engine.reacquire
    entered.clear()
    gate.clear()

    def applying(*args: Any) -> Any:
        entered.set()
        gate.wait(10)
        return reacquire(*args)

    async def decided(batch: Any, gone: Any, held: Any = None) -> Decision:
        return Decision(True)

    engine.reacquire = applying  # type: ignore[method-assign]
    mediator.live_review = LiveReview(enabled=True)
    mediator.await_decision = decided  # type: ignore[method-assign]
    task = asyncio.create_task(forwarder.handle(_request(_chat(f"call {EMAIL} again"))))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
    assert _held(vault) == 0 and provider.sent == []
    await forwarder.client.aclose()


@PEOPLE
async def test_an_exchange_that_ends_never_takes_the_hold_another_of_its_conversation_needs(person):
    """Two requests of one conversation: the first's stream awaited while the second is cancelled as it masks; the
    second's release, when its executor ends, takes its own count alone, the first still held until its answer ends."""
    mediator, vault = _mediator(person)
    provider = _Provider()
    forwarder = _forwarder(mediator, provider)
    first = await forwarder.handle(_request(_streaming(f"write to {EMAIL}")))
    assert _held(vault) == 1
    entered, gate = threading.Event(), threading.Event()
    prepare = mediator.prepare

    def masking(*args: Any, **kwargs: Any) -> Any:
        found = prepare(*args, **kwargs)
        entered.set()
        gate.wait(10)
        return found

    mediator.prepare = masking  # type: ignore[method-assign]
    task = asyncio.create_task(forwarder.handle(_request(_streaming(f"write to {EMAIL}"))))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
    assert _held(vault) == 1  # the first's, kept
    await _drained(first)
    assert _held(vault) == 0
    await forwarder.client.aclose()


@PEOPLE
@pytest.mark.parametrize("waiting_at", ["credentials", "transport"])
async def test_http_cancellation_during_credentials_or_send_releases_once_and_ends_its_renewer(
    person, waiting_at, monkeypatch
):
    """G1's carried HTTP row: cancellation while credentials or the transport waits propagates unchanged, releases
    the actual hold once, and ends its renewer. It publishes no late answer or second send; the next call still works."""
    mediator, vault = _mediator(person)
    holds: list[object] = []
    releases: list[object] = []
    hold, release = mediator.engine.hold, mediator.engine.release

    def taking(session: Any) -> object:
        handle = hold(session)
        holds.append(handle)
        return handle

    def letting_go(session: Any, handle: object) -> None:
        releases.append(handle)
        release(session, handle)

    monkeypatch.setattr(mediator.engine, "hold", taking)
    monkeypatch.setattr(mediator.engine, "release", letting_go)
    entered, gate = asyncio.Event(), asyncio.Event()
    credentials = AwsCredentials("AKIDEXAMPLE", "test-secret", "test-token")
    aws = AwsCredentialStore()

    async def profile(name: str) -> AwsCredentials:
        assert name == "g1"
        if waiting_at == "credentials" and not gate.is_set():
            entered.set()
            await gate.wait()
        return credentials

    # Only the external credential fetch and provider's network wait are controlled; acquisition and release are real.
    monkeypatch.setattr(aws, "profile", profile)
    provider = _Provider()

    async def send(request: httpx.Request) -> httpx.Response:
        if waiting_at == "transport" and not gate.is_set():
            provider.sent.append(request)
            entered.set()
            await gate.wait()
        return provider(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    token = "g1-local-token"

    class SignedUpstream(Upstream):
        def resolve(self, method: str, path: str, query: str) -> Resolved:
            resolved = super().resolve(method, path, query)
            assert resolved is not None
            return replace(resolved, signing=Signing("us-east-1", profile="g1", key=profile_tag(token, "g1")))

    # Real Bedrock routes only verify and take no hold. Exercise credentials with an owned hold by injecting the
    # signing descriptor at the route boundary; normal preparation, signing, send and finalization remain real.
    upstream = SignedUpstream(OPENAI.name, OPENAI.prefix, OPENAI.base_url, envelope_expected=OPENAI.envelope_expected)
    forwarder = Forwarder(RouteTable([upstream]), Capture(None), client, mediator, aws=aws, token=token)

    def request() -> Any:
        return _request(_chat(f"write to {EMAIL}"))

    before = set(_renewers())
    task = asyncio.create_task(forwarder.handle(request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        own_renewers = set(_renewers()) - before
        assert len(own_renewers) == 1 and _held(vault) == 1 and len(holds) == 1 and releases == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(4):
            await asyncio.sleep(0)
        assert all(renewer.done() for renewer in own_renewers)
        assert releases == holds and _held(vault) == 0
        cancelled_sends = 1 if waiting_at == "transport" else 0
        assert len(provider.sent) == cancelled_sends and provider.answers == []
        gate.set()
        await asyncio.sleep(0)
        assert len(provider.sent) == cancelled_sends and provider.answers == []
        # The following exchange answers as before, with a new hold and exactly one send.
        answer = await forwarder.handle(request())
        assert answer.status_code == 200 and json.loads(bytes(answer.body)) == {
            "choices": [{"message": {"content": "noted"}}]
        }
        assert len(provider.sent) == cancelled_sends + 1
        assert all(EMAIL.encode() not in sent.content for sent in provider.sent)
        assert len(holds) == 2 and releases == holds and holds[0] is not holds[1] and _held(vault) == 0
    finally:
        gate.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for renewer in set(_renewers()) - before:
            renewer.cancel()
        await client.aclose()
