"""P2-3 authority witnesses through the real preparation and provider-send boundary."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, get_ident

import httpx
import pytest
from starlette.testclient import TestClient

from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import JSONTransform, SSETransform, WSResponseRewriter
from latet.gateway.provenance import (
    ECHO_CONVERSATION_BYTES,
    ECHO_ENTRY_BYTES,
    ECHO_RULE,
    ENCRYPTED_RULE,
    EchoCheck,
    EchoScope,
    LocalEchoes,
    OpaqueCapture,
)
from latet.gateway.request_profiles import opaque_scope
from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream

from ..request_coverage import RecordingClient, RecordingTransport
from ..thread_pools import frame_locals
from .test_request_coverage import counts, gateway
from .test_request_outcomes import _mediator
from .test_request_transport import _Answer

OPAQUE = "b2JzZXJ2ZWQtb25seS1ieS10aGUtcHJvdmlkZXI="
# Conversation ids within their agents' grammars (request_profiles.ID_HEADERS), no configured row.
SESSION_A = "5e55a000-0000-4000-8000-00000000000a"
SESSION_B = "5e55b000-0000-4000-8000-00000000000b"
SAME_CONVERSATION = "5a3ec000-0000-4000-8000-00000000000c"
OPENAI = next(route for route in DEFAULT_UPSTREAMS if route.name == "openai")
ANTHROPIC = next(route for route in DEFAULT_UPSTREAMS if route.name == "anthropic")


@pytest.mark.parametrize("value", [OPAQUE, OPAQUE[:-4] + "YWJj", "forged-provider-item-tag"])
@pytest.mark.parametrize("company", [False, True], ids=["fixed-provider", "company"])
def test_opaque_echo_needs_exact_same_provider_conversation_evidence(value, company):
    """A provider item tag must not grant an unobserved image result permission to leave."""
    route = Upstream("company", "/company", "https://company.example.invalid") if company else OPENAI
    body = {
        "model": "gpt-4o",
        "input": [{"type": "image_generation_call", "id": "ig_1", "result": value}],
    }
    with gateway(upstream=route) as (client, transport, made, _):
        response = client.post(route.prefix + "/v1/responses", json=body)
        assert response.status_code == 400, "unobserved opaque image data reached the provider"
        assert counts(made) == (0, 0)
        assert transport.sent == []
        assert value not in response.text


@pytest.mark.parametrize(
    "signature",
    ["", "one two", "A", "YQ=", "YQ===", "=YQ=", "+_AA", "YQ==\n", "ordinary.thought", "\ud800"],
    ids=[
        "empty",
        "whitespace",
        "impossible-length",
        "short-padding",
        "extra-padding",
        "leading-padding",
        "mixed-alphabets",
        "newline",
        "non-base64",
        "unpaired-surrogate",
    ],
)
def test_provider_encrypted_fallback_rejects_malformed_value(signature):
    """The encrypted-field rule cannot act as an arbitrary-string carrier."""
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "ordinary thought", "signature": signature},
                ],
            }
        ],
    }
    with gateway(upstream=ANTHROPIC) as (client, transport, made, _):
        response = client.post(
            "/anthropic/v1/messages", content=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )
        assert response.status_code == 400, "malformed unobserved signature used encrypted fallback"
        assert counts(made) == (0, 0)
        assert not transport.sent


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_provider_encrypted_fallback_never_authorizes_a_nonmodel_role(role):
    """Moving an eligible encrypted field to another role must remove its fallback authority."""
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {
                "role": role,
                "content": [
                    {"type": "thinking", "thinking": "ordinary thought", "signature": OPAQUE},
                ],
            }
        ],
    }
    with gateway(upstream=ANTHROPIC) as (client, transport, made, _):
        response = client.post("/anthropic/v1/messages", json=body)
        assert response.status_code == 400, "a nonmodel role inherited encrypted fallback"
        assert counts(made) == (0, 0)
        assert not transport.sent


@pytest.mark.parametrize("origin", ["https://company.example.invalid", "https://api.anthropic.com"])
def test_provider_encrypted_fallback_is_not_granted_to_a_configured_destination(origin):
    """A company route, including one aimed at the real provider, is not a built-in trust rule."""
    route = Upstream("company", "/company", origin)
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "ordinary thought", "signature": OPAQUE},
                ],
            }
        ],
    }
    with gateway(upstream=route) as (client, transport, made, _):
        response = client.post("/company/v1/messages", json=body)
        assert response.status_code == 400, "configured destination borrowed fixed-provider fallback"
        assert counts(made) == (0, 0)
        assert not transport.sent


def test_anthropic_compaction_encrypted_content_requires_observation():
    """Responses compaction approval must not widen the different Anthropic compaction field."""
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "compaction", "content": "ordinary summary", "encrypted_content": OPAQUE},
                ],
            }
        ],
    }
    with gateway(upstream=ANTHROPIC) as (client, transport, made, _):
        response = client.post("/anthropic/v1/messages", json=body)
        assert response.status_code == 400, "an unapproved encrypted field inherited fallback"
        assert counts(made) == (0, 0)
        assert not transport.sent


@contextmanager
def observed_gateway(route, answer, content_type="application/json"):
    mediator = _mediator()
    transport = RecordingTransport(
        lambda request: httpx.Response(200, headers={"content-type": content_type}, stream=_Answer(answer))
    )
    config = GatewayConfig(port=5283, upstreams=[route], mediator=mediator, client=lambda: RecordingClient(transport))
    try:
        with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
            yield client, transport, mediator
    finally:
        mediator.close()


def event(value):
    return b"data: " + json.dumps(value).encode() + b"\n\n"


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_observed_image_result_passes_only_its_exact_company_conversation(stream):
    route = Upstream("company", "/company", "https://company.example.invalid")
    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}
    answer = {"id": "resp_a", "output": [item]}
    raw = event({"type": "response.completed", "response": answer}) if stream else json.dumps(answer).encode()
    headers = {"session-id": SESSION_A}
    with observed_gateway(route, raw, "text/event-stream" if stream else "application/json") as (
        client,
        sent,
        mediator,
    ):
        first = client.post(
            "/company/v1/responses", headers=headers, json={"model": "gpt-4o", "input": "hello", "stream": stream}
        )
        assert first.status_code == 200 and first.content == raw
        assert mediator.opaque_provenance_bytes > 0
        replay = {"model": "gpt-4o", "input": [item]}
        assert client.post("/company/v1/responses", headers=headers, json=replay).status_code == 200
        assert client.post("/company/v1/responses", headers={"session-id": SESSION_B}, json=replay).status_code == 400
        changed = copy.deepcopy(replay)
        changed["input"][0]["result"] = OPAQUE[:-4] + "YWJj"
        assert client.post("/company/v1/responses", headers=headers, json=changed).status_code == 400
        assert len(sent.sent) == 2


@pytest.mark.parametrize("verify", [False, True], ids=["rewrite-no-entities", "verify-only"])
def test_an_echo_assembled_from_deltas_matches_its_reserialized_request(verify):
    route = Upstream("company", "/company", "https://company.example.invalid")
    pieces = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "ab/c"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "d+ef="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    raw = b"".join(event(part) for part in pieces).replace(b"ab/c", b"ab\\/c")
    headers = {"x-claude-code-session-id" if verify else "session-id": SAME_CONVERSATION}
    initial = {"model": "claude-sonnet-4-6", "max_tokens": 20, "messages": [{"role": "user", "content": "hello"}]}
    with observed_gateway(route, raw, "text/event-stream") as (client, _, mediator):
        assert client.post("/company/v1/messages", headers=headers, json=initial).content == raw
        replay = copy.deepcopy(initial)
        replay["messages"].append(
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "ordinary", "signature": "ab/cd+ef="}]}
        )
        assert client.post("/company/v1/messages", headers=headers, json=replay).status_code == 200
        replay["messages"][-1]["content"].insert(0, {"type": "text", "text": "a different sibling position"})
        assert client.post("/company/v1/messages", headers=headers, json=replay).status_code == 400
        mediator.echoes.close()
        replay["messages"][-1]["content"].pop(0)
        assert client.post("/company/v1/messages", headers=headers, json=replay).status_code == 400


@pytest.mark.parametrize("value", ["YQ==", "YQ", "YWJj", "ab/c", "ab_c", "ab-c", "ab+c"])
def test_exact_fixed_provider_signature_fallback_is_sendable(value):
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 20,
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "ordinary", "signature": value}]}
        ],
    }
    with gateway(upstream=ANTHROPIC) as (client, transport, _, _):
        assert client.post("/anthropic/v1/messages", json=body).status_code == 200
        assert len(transport.sent) == 1


def test_opaque_metadata_limits_and_reservation_rollback_are_exact():
    entry, conversation = ECHO_ENTRY_BYTES, ECHO_CONVERSATION_BYTES
    store = LocalEchoes(limit=conversation + 2 * entry, entries=2, conversation_limit=conversation + 2 * entry)
    owner = b"c" * 32
    first, second, third = (hashlib.sha256(bytes([i])).digest() for i in range(3))
    assert store.keep(owner, (first, second))
    assert store.retained_bytes == conversation + 2 * entry
    assert not store.keep(owner, (first, second, third))
    assert store.read((first, second, third))[0] == {first, second}
    assert store.keep(owner, (third,))
    assert len(store.read((first, second, third))[0]) == 2 and first not in store.read((first,))[0]
    store.close()
    assert store.retained_bytes == 0
    store.close()
    assert store.retained_bytes == 0
    assert not store.reserve(1)
    pending = LocalEchoes(limit=entry)
    assert pending.reserve(entry)
    assert not pending.reserve(1)
    pending.close()
    assert pending.retained_bytes == entry
    pending.release(entry)
    assert pending.retained_bytes == 0


def test_owned_echo_container_overhead_is_covered_at_capacity_and_after_eviction():
    store = LocalEchoes(entries=64, conversation_limit=128 * ECHO_ENTRY_BYTES)
    sizes = []
    for index in range(129):
        member = hashlib.sha256(str(index).encode()).digest()
        assert store.keep(b"c" * 32, (member,))
        # Independently walk actual owned dynamic containers and their fixed-size entries, counting duplicate
        # references conservatively. No payload or implementation accounting helper derives the expectation.
        actual = sys.getsizeof(store._items) + sys.getsizeof(store._conversations)
        actual += sum(
            sys.getsizeof(key) + sys.getsizeof(pair) + sum(sys.getsizeof(part) for part in pair)
            for key, pair in store._items.items()
        )
        actual += sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in store._conversations.items())
        sizes.append(actual)
        assert actual <= store.retained_bytes
    print(
        json.dumps(
            {
                "peak_owned": max(sizes),
                "charged": store.retained_bytes,
                "entry_constant": ECHO_ENTRY_BYTES,
                "conversation_constant": ECHO_CONVERSATION_BYTES,
            }
        )
    )
    store.close()
    assert store.retained_bytes == 0


def test_exact_digest_scope_has_no_cross_identity_destination_profile_role_or_position_authority():
    body = {"input": [{"type": "image_generation_call", "result": OPAQUE}]}
    scope = EchoScope(("destination", "profile-1"), "responses")
    store = LocalEchoes()
    observed = EchoCheck(b"a" * 32, store, scope, "conversation", body)
    capture = OpaqueCapture(observed, store)
    capture.body({"output": body["input"]})
    assert capture.commit()
    known = EchoCheck(b"a" * 32, store, scope, "conversation", body)
    assert set(known.rules(body).values()) == {ECHO_RULE}
    for key, scoped, session in [
        (b"b" * 32, scope, "conversation"),
        (b"a" * 32, EchoScope(("other-destination", "profile-1"), "responses"), "conversation"),
        (b"a" * 32, EchoScope(("destination", "profile-2"), "responses"), "conversation"),
        (b"a" * 32, scope, "other-conversation"),
    ]:
        assert EchoCheck(key, store, scoped, session, body).rules(body) == {}
    changed = {"input": [{"type": "image_generation_call", "result": OPAQUE, "role": "user"}]}
    assert known.rules(changed) == {}
    store.close()
    assert EchoCheck(b"a" * 32, store, scope, "conversation", body).rules(body) == {}


def matrix_rows():
    for kind, key, fallback in [
        ("thinking", "signature", True),
        ("redacted_thinking", "data", True),
        ("compaction", "encrypted_content", False),
    ]:
        yield "anthropic", {"type": kind, key: OPAQUE}, fallback
    for kind in (
        "char_location",
        "page_location",
        "content_block_location",
        "web_search_result_location",
        "url_citation",
    ):
        yield (
            "anthropic",
            {"type": "text", "citations": [{"type": kind, "encrypted_index": OPAQUE}]},
            kind == "web_search_result_location",
        )
    for wrapper in (
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "tool_search_tool_result",
        "advisor_tool_result",
    ):
        for kind in (
            "web_search_result",
            "web_fetch_result",
            "code_execution_result",
            "bash_code_execution_result",
            "text_editor_code_execution_view_result",
            "text_editor_code_execution_create_result",
            "text_editor_code_execution_str_replace_result",
            "tool_search_tool_search_result",
            "advisor_result",
            "web_search_tool_result_error",
            "web_fetch_tool_result_error",
            "code_execution_tool_result_error",
        ):
            yield (
                "anthropic",
                {"type": wrapper, "content": [{"type": kind, "encrypted_content": OPAQUE}]},
                wrapper == "web_search_tool_result" and kind == "web_search_result",
            )
    for kind in ("reasoning", "compaction", "image_generation_call"):
        yield (
            "responses",
            {"type": kind, "result" if kind == "image_generation_call" else "encrypted_content": OPAQUE},
            kind != "image_generation_call",
        )
    for key in ("thoughtSignature", "thought_signature"):
        yield "gemini", {key: OPAQUE}, True


def all_protocol_matrix_rows():
    for protocol, item, fallback in matrix_rows():
        yield protocol, item, fallback
        if protocol == "anthropic":
            yield "chat", item, False
            yield "responses", {"type": "message", "role": "assistant", "content": [item]}, False


@pytest.mark.parametrize(("protocol", "item", "fallback"), list(all_protocol_matrix_rows()))
def test_every_declared_opaque_leaf_needs_observation_or_its_exact_m2_rule(protocol, item, fallback):
    if protocol in {"anthropic", "chat"}:
        body = {"messages": [{"role": "assistant", "content": [item]}]}
        answer = body["messages"][0]
        if protocol == "chat":
            answer = {"choices": [{"message": answer}]}
    elif protocol == "responses":
        body, answer = {"input": [item]}, {"output": [item]}
    else:
        body = {"contents": [{"role": "model", "parts": [item]}]}
        answer = {"candidates": [{"content": body["contents"][0]}]}
    store = LocalEchoes()
    scope = EchoScope(("selected-destination", "profile-generation"), protocol, True)
    unknown = EchoCheck(b"a" * 32, store, scope, "s", body)
    assert list(unknown.rules(body).values()) == ([ENCRYPTED_RULE] if fallback else [])
    company = replace(scope, fallback=False)
    check = EchoCheck(b"a" * 32, store, company, "s", body)
    assert check.rules(body) == {}
    capture = OpaqueCapture(check, store)
    capture.body(answer)
    assert capture.commit()
    assert list(EchoCheck(b"a" * 32, store, company, "s", body).rules(body).values()) == [ECHO_RULE]
    assert EchoCheck(b"a" * 32, store, company, "other", body).rules(body) == {}
    store.close()


def test_citation_delta_assembly_preserves_its_sibling_position():
    store = LocalEchoes()
    scope = EchoScope(("company",), "anthropic")
    check = EchoCheck(b"k" * 32, store, scope, "s", {})
    capture = OpaqueCapture(check, store)
    citation = {"type": "web_search_result_location", "encrypted_index": "decoded/opaque+value"}
    capture.event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    capture.event(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "citations_delta", "citation": citation}}
    )
    capture.event({"type": "content_block_stop", "index": 0})
    capture.event({"type": "message_stop"})
    assert capture.commit()
    body = {"messages": [{"role": "assistant", "content": [{"type": "text", "citations": [citation]}]}]}
    assert list(EchoCheck(b"k" * 32, store, scope, "s", body).rules(body).values()) == [ECHO_RULE]
    body["messages"][0]["content"][0]["citations"].insert(0, {"type": "char_location"})
    assert EchoCheck(b"k" * 32, store, scope, "s", body).rules(body) == {}
    store.close()


def test_signature_completion_transfers_its_charge_and_citation_refusal_releases_once():
    store = LocalEchoes(limit=2 * ECHO_ENTRY_BYTES)
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "anthropic"), "s", {})
    capture = OpaqueCapture(check, store)
    capture.event(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "signature": "abc"}}
    )
    capture.event({"type": "content_block_stop", "index": 0})
    assert not capture.closed and store.retained_bytes == 2 * ECHO_ENTRY_BYTES
    capture.event({"type": "message_stop"})
    assert not capture.commit()  # metadata plus conversation cannot fit; no partial insertion
    assert store.retained_bytes == 0
    capture.close()
    capture = OpaqueCapture(check, store)
    capture.event({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    capture.event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "citations_delta",
                "citation": {"type": "web_search_result_location", "encrypted_index": "abc"},
            },
        }
    )
    assert capture.closed and store.retained_bytes == 0
    capture.close()
    assert store.retained_bytes == 0


class BlockingEchoes(LocalEchoes):
    def __init__(self):
        super().__init__()
        self.entered, self.go = Event(), Event()
        self.worker = None

    def keep(self, conversation, members, **kwargs):
        self.worker = get_ident()
        self.entered.set()
        assert self.go.wait(5), "test did not release commit worker"
        return super().keep(conversation, members, **kwargs)


async def entered(event):
    async with asyncio.timeout(4):
        while not event.is_set():  # noqa: ASYNC110 - a threading.Event on the engine worker
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("kind", ["json", "sse", "ws", "written-sse", "written-json"])
async def test_terminal_publication_waits_for_the_existing_worker_commit(kind):
    mediator = _mediator()
    store = BlockingEchoes()
    mediator.echoes = store
    session = mediator.engine.session("opaque-barrier")
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "responses"), session.key, {})
    capture = OpaqueCapture(check, store)
    item = {"type": "image_generation_call", "result": OPAQUE}
    answer = {"output": [item]}
    completed = {"type": "response.completed", "response": answer}
    transform = None
    if kind == "json":
        transform = JSONTransform(
            "responses", mediator.restore_context(session), None, mediator=mediator, opaque=capture
        )
        transform.feed(json.dumps(answer).encode() + b"  ")
        operation = transform.restored_end()
    elif kind == "sse":
        transform = SSETransform(
            "responses", mediator.restore_context(session), None, lambda _: None, mediator=mediator, opaque=capture
        )
        raw = event(completed)
        assert [part async for part in transform.restored(raw[:-1])] == [b""]

        async def finish():
            return [part async for part in transform.restored(raw[-1:])]

        operation = finish()
    elif kind == "ws":
        transform = WSResponseRewriter(mediator, session)
        transform.opaque = capture
        operation = transform.restored(json.dumps(completed))
    else:
        from latet.gateway.written import Written

        whole = kind == "written-json"
        written = Written("application/json" if whole else "text/event-stream", None, opaque=capture, record_text=False)
        if whole:
            await mediator.read_written(written, session, json.dumps(answer).encode())
            await mediator.read_written(written, session, b"   ")
            assert not store.entered.is_set()
        operation = mediator.read_written(written, session, None if whole else event(completed))
    task = asyncio.create_task(operation)
    try:
        await entered(store.entered)
        assert not task.done() and store.worker != get_ident()
        assert store.retained_bytes >= ECHO_ENTRY_BYTES
        store.go.set()
        await task
        body = {"input": [item]}
        assert list(EchoCheck(b"k" * 32, store, check.scope, session.key, body).rules(body).values()) == [ECHO_RULE]
    finally:
        store.go.set()
        if transform is not None:
            transform.close()
        mediator.close()


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
async def test_cancelled_barrier_releases_once_at_actual_worker_completion(running):
    mediator = _mediator()
    store = BlockingEchoes()
    mediator.echoes = store
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "responses"), "s", {})
    capture = OpaqueCapture(check, store)
    capture.body({"output": [{"type": "image_generation_call", "result": OPAQUE}]})
    busy, release = Event(), Event()
    if not running:

        def occupying():
            busy.set()
            assert release.wait(5)

        worker = mediator.executor.submit(occupying)
        await entered(busy)
    task = asyncio.create_task(mediator.commit_opaque(capture, 512))
    try:
        if running:
            await entered(store.entered)
        else:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        capture.close()
        assert store.retained_bytes == (ECHO_ENTRY_BYTES if running else 0)
        if running:
            store.go.set()
        else:
            release.set()
            await asyncio.wrap_future(worker)
        await mediator.run(lambda: None)  # same worker: prior commit and release have actually ended
        assert not capture.charge
        assert bool(store._items) is running
        store.close()
        assert store.retained_bytes == 0
    finally:
        release.set()
        store.go.set()
        mediator.close()


def test_native_cloud_fallback_uses_constructor_and_selected_logical_profile():
    from latet.gateway.cloud import BedrockUpstream, VertexUpstream

    expected = frozenset({("project", "project-a"), ("model", "claude-sonnet-4-6"), ("model", "gemini-2.5-flash")})
    for route, path, protocol in [
        (BedrockUpstream(envelope_expected=expected), "/bedrock/us-east-1/model/claude-sonnet-4-6/invoke", "anthropic"),
        (
            VertexUpstream(envelope_expected=expected),
            "/vertex/v1/projects/project-a/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict",
            "anthropic",
        ),
        (
            VertexUpstream(envelope_expected=expected),
            "/vertex/v1/projects/project-a/locations/us-east5/publishers/google/models/gemini-2.5-flash:generateContent",
            "gemini",
        ),
    ]:
        scope = opaque_scope(route, "POST", path, protocol)
        assert scope is not None and scope.fallback
        wrong = opaque_scope(route, "POST", path, "gemini" if protocol == "anthropic" else "anthropic")
        assert wrong is not None and not wrong.fallback
        configured = Upstream(route.name, route.prefix, "https://same-host.example.invalid", envelope_expected=expected)
        other = opaque_scope(configured, "POST", path, protocol)
        assert other is None or not other.fallback
    for name in ("openai", "chatgpt", "gemini", "anthropic"):
        route = next(route for route in DEFAULT_UPSTREAMS if route.name == name)
        configured = Upstream(route.name, route.prefix, route.base_url)
        path = route.prefix + ("/v1/messages" if name == "anthropic" else "/v1/responses")
        scope = opaque_scope(configured, "POST", path, "anthropic" if name == "anthropic" else "responses")
        assert scope is None or not scope.fallback


async def test_capture_precedes_restoration_and_final_rules_reject_changed_bytes(monkeypatch):
    from latet.gateway import mediate

    mediator = _mediator()
    session = mediator.engine.session("capture-order")
    scope = EchoScope(("company",), "responses")
    check = EchoCheck(b"k" * 32, mediator.echoes, scope, session.key, {})
    capture = OpaqueCapture(check, mediator.echoes)

    def changing_restore(ctx, body):
        body["output"][0]["result"] = "restored-form-is-not-observed"
        return body

    monkeypatch.setattr(mediate, "restore_response_body", changing_restore)
    transform = JSONTransform("responses", mediator.restore_context(session), None, mediator=mediator, opaque=capture)
    try:
        transform.feed(json.dumps({"output": [{"type": "image_generation_call", "result": OPAQUE}]}).encode())
        assert b"restored-form-is-not-observed" in await transform.restored_end()
        body = {"input": [{"type": "image_generation_call", "result": OPAQUE}]}
        prepared = EchoCheck(b"k" * 32, mediator.echoes, scope, session.key, body)
        assert list(prepared.rules(body).values()) == [ECHO_RULE]
        body["input"][0]["result"] = "restored-form-is-not-observed"
        assert prepared.rules(body) == {}
        assert EchoCheck(b"k" * 32, mediator.echoes, scope, session.key, body).rules(body) == {}
    finally:
        transform.close()
        mediator.close()


@pytest.mark.parametrize("failure", ["read", "write"])
def test_gateway_backend_failures_lose_evidence_without_turning_into_503(failure, monkeypatch):
    route = Upstream("company", "/company", "https://company.example.invalid")
    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}
    with observed_gateway(route, json.dumps({"output": [item]}).encode()) as (client, sent, mediator):

        def unavailable(*args, **kwargs):
            raise ConnectionError("synthetic unavailable evidence")

        monkeypatch.setattr(mediator.echoes, "read" if failure == "read" else "keep", unavailable)
        headers = {"session-id": SESSION_A}
        assert (
            client.post(
                "/company/v1/responses", headers=headers, json={"model": "gpt-4o", "input": "hello"}
            ).status_code
            == 200
        )
        assert (
            client.post("/company/v1/responses", headers=headers, json={"model": "gpt-4o", "input": [item]}).status_code
            == 400
        )
        assert len(sent.sent) == 1


def test_actual_capture_containers_and_digest_states_fit_reservations():
    store = LocalEchoes()
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "anthropic"), "s", {})
    capture = OpaqueCapture(check, store)
    for index in range(64):
        capture.event(
            {"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "signature": "abc"}}
        )
    actual = sum(
        sys.getsizeof(container)
        for container in (capture.signatures, capture.pending, capture.citations, capture.blocks)
    )
    actual += sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in capture.blocks.items())
    actual += sum(
        sys.getsizeof(k) + sys.getsizeof(v) + sum(sys.getsizeof(p) for p in v) + 256
        for k, v in capture.signatures.items()
    )  # conservatively include native HMAC context storage
    assert actual < capture.charge == store.retained_bytes
    for index in range(64):
        capture.event({"type": "content_block_stop", "index": index})
    pending = sys.getsizeof(capture.pending) + sum(sys.getsizeof(k) for k in capture.pending)
    assert pending < capture.charge == store.retained_bytes
    print(json.dumps({"capture_states": actual, "capture_digests": pending, "capture_charged": capture.charge}))
    capture.close()
    assert store.retained_bytes == 0


@pytest.mark.parametrize("bad", ["unfinished", "duplicate-start", "duplicate-stop", "error"])
def test_inconsistent_or_failed_anthropic_stream_never_commits(bad):
    store = LocalEchoes()
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "anthropic"), "s", {})
    capture = OpaqueCapture(check, store)
    start = {"type": "content_block_start", "index": 0, "content_block": {"type": "redacted_thinking", "data": OPAQUE}}
    capture.event(start)
    if bad == "duplicate-start":
        capture.event(start)
    elif bad == "duplicate-stop":
        capture.event({"type": "content_block_stop", "index": 0})
        capture.event({"type": "content_block_stop", "index": 0})
    elif bad == "error":
        capture.event({"type": "error"})
    capture.event({"type": "message_stop"})
    assert not capture.commit()
    assert store.retained_bytes == 0


def test_one_authoritative_batch_is_reused_but_never_for_changed_bytes(monkeypatch):
    store = LocalEchoes()
    body = {"input": [{"type": "image_generation_call", "result": OPAQUE}]}
    scope = EchoScope(("company",), "responses")
    initial = EchoCheck(b"k" * 32, store, scope, "s", {})
    capture = OpaqueCapture(initial, store)
    capture.body({"output": body["input"]})
    assert capture.commit()
    calls = []
    read = store.read

    def counted(values):
        calls.append(values)
        return read(values)

    monkeypatch.setattr(store, "read", counted)
    check = EchoCheck(b"k" * 32, store, scope, "s", body)
    for _ in range(3):
        assert list(check.rules(body).values()) == [ECHO_RULE]
    body["input"][0]["result"] += "changed"
    assert check.rules(body) == {} and len(calls) == 1
    EchoCheck(b"k" * 32, store, scope, "s", {})
    assert len(calls) == 1


def test_scope_omission_mutant_is_detected_by_cross_conversation_control(monkeypatch):
    # Named scratch mutant: bypass every scope salt. The negative witness below must detect the resulting authority.
    monkeypatch.setattr(
        EchoCheck, "member", lambda self, field: hashlib.sha256(field.holder[field.key].encode()).digest()
    )
    store = LocalEchoes()
    scope = EchoScope(("company",), "responses")
    body = {"input": [{"type": "image_generation_call", "result": OPAQUE}]}
    capture = OpaqueCapture(EchoCheck(b"k" * 32, store, scope, "first", {}), store)
    capture.body({"output": body["input"]})
    assert capture.commit()
    with pytest.raises(AssertionError):
        assert EchoCheck(b"other-key", store, scope, "second", body).rules(body) == {}


async def test_no_opaque_observation_never_queues_a_commit_behind_engine_work(monkeypatch):
    mediator = _mediator()
    check = EchoCheck(b"k" * 32, mediator.echoes, EchoScope(("company",), "responses"), "s", {})
    capture = OpaqueCapture(check, mediator.echoes)
    capture.body({"output": [{"type": "message", "role": "assistant", "content": []}]})

    async def unnecessary(*args):
        raise AssertionError("empty opaque observation queued an engine operation")

    monkeypatch.setattr(mediator, "run", unnecessary)
    try:
        await mediator.commit_opaque(capture, 512)
        assert capture.closed and mediator.opaque_provenance_bytes == 0
    finally:
        mediator.close()


def test_opaque_value_character_bound_and_eviction_lose_permission():
    from latet.gateway.provenance import ECHO_VALUE_CHARS, encrypted_value

    assert encrypted_value("A" * ECHO_VALUE_CHARS)
    assert not encrypted_value("A" * (ECHO_VALUE_CHARS + 1))
    store = LocalEchoes(entries=1, conversation_limit=2 * ECHO_ENTRY_BYTES)
    scope = EchoScope(("company",), "responses")
    first = {"input": [{"type": "image_generation_call", "result": "first"}]}
    for value in ("first", "second"):
        check = EchoCheck(b"k" * 32, store, scope, "s", {})
        capture = OpaqueCapture(check, store)
        capture.body({"output": [{"type": "image_generation_call", "result": value}]})
        assert capture.commit()
    assert EchoCheck(b"k" * 32, store, scope, "s", first).rules(first) == {}
    assert store.retained_bytes == ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES
    store.close()


@pytest.mark.parametrize("status", [[], {}, ["completed"]])
def test_malformed_completion_metadata_never_raises_or_grants_evidence(status):
    store = LocalEchoes()
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "responses"), "s", {})
    capture = OpaqueCapture(check, store)
    capture.body({"status": status, "output": [{"type": "image_generation_call", "result": OPAQUE}]})
    assert not capture.commit()
    assert capture.closed and store.retained_bytes == 0


@pytest.mark.parametrize("refused", [False, True], ids=["committed-before-bytes", "bounded-refusal"])
async def test_raw_json_transport_holds_trailing_chunks_until_commit_or_discards_evidence(refused):
    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.routes import RouteTable

    from .test_request_leaks import _request
    from .test_request_outcomes import OPENAI as FIXED_ROUTE
    from .test_request_outcomes import _chat

    mediator = _mediator()
    store = BlockingEchoes()
    if refused:
        store.limit = ECHO_ENTRY_BYTES
    mediator.echoes = store
    raw = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": [{"type": "thinking", "signature": OPAQUE}]}}]}
    ).encode() + b" " * (130 * 1024)
    transport = RecordingTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(raw))
    )
    client = RecordingClient(transport)
    forwarder = Forwarder(RouteTable([FIXED_ROUTE]), Capture(None), client, mediator)
    received = []
    response = await forwarder.handle(_request(_chat("hello")))
    assert response.status_code == 200

    async def drain():
        async for chunk in response.body_iterator:
            received.append(chunk)

    task = asyncio.create_task(drain())
    try:
        if not refused:
            await entered(store.entered)
            assert not task.done() and not received
            assert store.retained_bytes >= len(raw) + ECHO_ENTRY_BYTES
            store.go.set()
        await task
        assert b"".join(received) == raw
        assert bool(store.retained_bytes) is (not refused)
        assert store._reserved == 0
        if refused:
            assert not store.entered.is_set()
    finally:
        store.go.set()
        await client.aclose()
        mediator.close()


async def test_bedrock_binary_frames_capture_decoded_signature_before_terminal_return():
    import base64

    from latet.gateway.written import Written
    from latet.protocols.eventstream import encode

    mediator = _mediator()
    store = BlockingEchoes()
    mediator.echoes = store
    session = mediator.engine.session("bedrock-decoded")
    scope = EchoScope(("bedrock", "model-resource"), "anthropic")
    check = EchoCheck(b"k" * 32, store, scope, session.key, {})
    capture = OpaqueCapture(check, store)
    written = Written("application/vnd.amazon.eventstream", None, opaque=capture, record_text=False)

    def frame(value):
        payload = json.dumps({"bytes": base64.b64encode(json.dumps(value).encode()).decode()}).encode()
        return encode({":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"}, payload)

    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": OPAQUE[:11]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": OPAQUE[11:]}},
        {"type": "content_block_stop", "index": 0},
    ]
    for value in events:
        raw = frame(value)
        for offset in range(0, len(raw), 7):
            await mediator.read_written(written, session, raw[offset : offset + 7])
    task = asyncio.create_task(mediator.read_written(written, session, frame({"type": "message_stop"})))
    try:
        await entered(store.entered)
        assert not task.done()
        store.go.set()
        await task
        body = {"messages": [{"role": "assistant", "content": [{"type": "thinking", "signature": OPAQUE}]}]}
        assert list(EchoCheck(b"k" * 32, store, scope, session.key, body).rules(body).values()) == [ECHO_RULE]
    finally:
        store.go.set()
        mediator.close()


def test_prepared_receipt_is_charged_until_explicit_close():
    store = LocalEchoes()
    scope = EchoScope(("company",), "responses")
    body = {"input": [{"type": "image_generation_call", "result": OPAQUE}]}
    capture = OpaqueCapture(EchoCheck(b"k" * 32, store, scope, "s", {}), store)
    capture.body({"output": body["input"]})
    assert capture.commit()
    baseline = store.retained_bytes
    check = EchoCheck(b"k" * 32, store, scope, "s", body)
    assert store.retained_bytes >= baseline + 2 * ECHO_ENTRY_BYTES, "the Prepared receipt is outside its owner budget"
    assert list(check.rules(body).values()) == [ECHO_RULE]
    check.close()
    check.close()
    assert store.retained_bytes == baseline and check.rules(body) == {}
    store.close()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("count", [1, 64, 1023])
def test_receipt_reservation_boundary_expiry_and_overhead(fallback, count, monkeypatch):
    store = LocalEchoes()
    scope = EchoScope(("selected",), "anthropic", fallback)
    body = {
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "signature": OPAQUE} for _ in range(count)]}
        ]
    }
    capture = OpaqueCapture(EchoCheck(b"k" * 32, store, scope, "s", {}), store)
    capture.body(body["messages"][0])
    assert capture.commit()
    baseline = store.retained_bytes
    store.limit = baseline + (count + 1) * ECHO_ENTRY_BYTES
    check = EchoCheck(b"k" * 32, store, scope, "s", body)
    assert check.found and store.retained_bytes == store.limit
    measured = (
        sys.getsizeof(check)
        + sys.getsizeof(check.__dict__)
        + sys.getsizeof(check._lock)
        + sys.getsizeof(check.found)
        + sum(map(sys.getsizeof, check.found))
    )
    assert measured <= check._charge
    print(f"receipt entries={count} actual={measured} charged={check._charge}")
    check.expires = 0
    assert list(check.rules(body).values()) == ([ENCRYPTED_RULE] * count if fallback else [])
    assert store.retained_bytes == baseline
    store.limit -= 1
    calls = []
    monkeypatch.setattr(store, "read", lambda members: calls.append(members))
    refused = EchoCheck(b"k" * 32, store, scope, "s", body)
    assert not calls and store.retained_bytes == baseline
    assert list(refused.rules(body).values()) == ([ENCRYPTED_RULE] * count if fallback else [])
    refused.close()
    empty = EchoCheck(b"k" * 32, store, scope, "s", {})
    empty.close()
    assert store.retained_bytes == baseline
    store.close()


def receipt_preparation(mediator):
    headers = {"session-id": "receipt-s"}
    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}
    first = mediator.prepare(
        OPENAI, "POST", "/openai/v1/responses", headers, json.dumps({"model": "gpt-4o", "input": "hello"}).encode()
    )
    capture = mediator.opaque_capture(first)
    assert capture is not None
    capture.body({"output": [item]})
    assert capture.commit()
    first.close_receipt()
    if first.lease is not None:
        first.lease.release()
    raw = json.dumps({"model": "gpt-4o", "input": [item]}).encode()
    return lambda: mediator.prepare(OPENAI, "POST", "/openai/v1/responses", headers, raw)


def test_failed_preparation_explicitly_releases_receipt(monkeypatch):
    mediator = _mediator()
    prepare = receipt_preparation(mediator)
    baseline = mediator.opaque_provenance_bytes

    def failed(*args):
        raise RuntimeError("synthetic post-lookup preparation failure")

    monkeypatch.setattr(mediator, "_attach_project", failed)
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            prepare()
        assert mediator.opaque_provenance_bytes == baseline and mediator.echoes._reserved == 0
    finally:
        mediator.close()


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
@pytest.mark.parametrize("closed", [False, True], ids=["live-owner", "closed-owner"])
async def test_cancelled_preparation_keeps_receipt_charge_until_worker_ends(running, closed, monkeypatch):
    mediator = _mediator()
    prepare = receipt_preparation(mediator)
    store = mediator.echoes
    baseline = store.retained_bytes
    busy, go = Event(), Event()
    read = store.read

    def blocked(members=None):
        busy.set()
        assert go.wait(5)
        return read(members) if members is not None else None

    if running:
        monkeypatch.setattr(store, "read", blocked)
    else:
        worker = mediator.executor.submit(blocked)
        await entered(busy)
    task = asyncio.create_task(mediator.run_held(prepare))
    try:
        if running:
            await entered(busy)
        else:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if closed:
            store.close()
            baseline = 0
        assert store.retained_bytes == baseline + (2 * ECHO_ENTRY_BYTES if running else 0)
        go.set()
        if not running:
            await asyncio.wrap_future(worker)
        await mediator.run(lambda: None)
        assert store.retained_bytes == baseline and store._reserved == 0
    finally:
        go.set()
        mediator.close()


async def test_real_websocket_replay_releases_its_receipt_after_final_frame_validation(monkeypatch):
    from websockets.exceptions import ConnectionClosed

    from latet.gateway import mediate, request_transport, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable

    from .test_request_outcomes import _Client, _until, _Upstream

    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}

    class Provider(_Upstream):
        def _answer(self, conn):
            try:
                for message in conn:
                    self.got.append(message)
                    conn.send(json.dumps({"type": "response.completed", "response": {"output": [item]}}))
            except ConnectionClosed:
                pass

    mediator = _mediator()
    upstream, client = Provider(), _Client()
    route = Upstream(
        "relay",
        "/relay",
        f"http://127.0.0.1:{upstream.port}",
        envelope_expected=frozenset(),
    )
    checked, receipts, rewriters = [], [], []
    validate = request_transport.validate_frame

    def rewriter(*args, **kwargs):
        result = WSResponseRewriter(*args, **kwargs)
        rewriters.append(result)
        return result

    def validated(decision, prepared, *args, **kwargs):
        if prepared is not None and prepared.opaque_check is not None:
            checked.append(prepared.opaque_check._charge)
            receipts.append(prepared.opaque_check)
        return validate(decision, prepared, *args, **kwargs)

    monkeypatch.setattr(ws, "WSResponseRewriter", rewriter)
    monkeypatch.setattr(request_transport, "validate_frame", validated)
    task = asyncio.create_task(ws.relay(client.socket, RouteTable([route]), Capture(None), mediator))
    try:
        for count, contents in enumerate(("hello", [item]), 1):
            client.incoming.put_nowait(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "response.create", "model": "gpt-4o", "input": contents}),
                }
            )
            await _until(lambda count=count: len(client.texts()) == count)
            assert len(rewriters) == 1 and rewriters[0].mediator is mediator
            state = rewriters[0]._target
            assert isinstance(state, mediate._ResponseState) and state.phase == "terminal" and not state.closed
            assert state.digest is None and state.token is None
            assert mediator.echoes._reserved == ECHO_ENTRY_BYTES
            assert len(receipts) == count and all(receipt._charge == 0 for receipt in receipts)
        assert checked == [0, 2 * ECHO_ENTRY_BYTES] and len(upstream.got) == 2
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 10)
        assert mediator.echoes._reserved == 0
        assert state.closed and state.digest is None and state.token is None and rewriters[0]._target is False
    finally:
        task.cancel()
        upstream.close()
        mediator.close()


@pytest.mark.parametrize("mutant", ["capture-after-restore", "raw-delta-hash", "terminal-without-commit"])
async def test_named_capture_guards_detect_actual_mutations(mutant, monkeypatch):
    from latet.gateway.mediate import Mediator

    if mutant == "capture-after-restore":
        end = JSONTransform.end

        def after_restore(self):
            capture, self.opaque = self.opaque, None
            try:
                result = end(self)
            finally:
                self.opaque = capture
            capture.body(json.loads(result))
            return result

        monkeypatch.setattr(JSONTransform, "end", after_restore)
        with pytest.raises(AssertionError):
            await test_capture_precedes_restoration_and_final_rules_reject_changed_bytes(monkeypatch)
    elif mutant == "raw-delta-hash":
        observe = OpaqueCapture._event

        def raw_delta(self, value):
            if value.get("type") == "content_block_delta" and value.get("delta", {}).get("type") == "signature_delta":
                value = copy.deepcopy(value)
                value["delta"]["signature"] = json.dumps(value["delta"]["signature"])
            return observe(self, value)

        monkeypatch.setattr(OpaqueCapture, "_event", raw_delta)
        with pytest.raises(AssertionError):
            test_an_echo_assembled_from_deltas_matches_its_reserialized_request(False)
    else:

        async def bypass(self, capture, held=0):
            return None

        monkeypatch.setattr(Mediator, "commit_opaque", bypass)
        with pytest.raises(TimeoutError):
            await test_terminal_publication_waits_for_the_existing_worker_commit("json")
    print(f"P23_SOURCE_MUTANT {mutant}: detected")


@pytest.mark.parametrize("boundary", ["review", "route"])
@pytest.mark.parametrize("expired", [False, True], ids=["fresh", "expired"])
def test_http_send_rechecks_receipt_deadline_after_await(boundary, expired, monkeypatch):
    from latet.gateway.forward import Forwarder

    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}
    route = Upstream("company", "/company", "https://company.example.invalid")
    with observed_gateway(route, json.dumps({"output": [item]}).encode()) as (client, sent, mediator):
        latest = []
        live, routed = mediator.live_prepared, Forwarder._route

        async def reviewed(prepared, *args):
            result = await live(prepared, *args)
            latest[:] = [result]
            if boundary == "review" and expired and result.opaque_check.found:
                result.opaque_check.expires = 0
            return result

        async def delayed(self, *args, **kwargs):
            result = await routed(self, *args, **kwargs)
            await asyncio.sleep(0)
            if boundary == "route" and expired and latest[0].opaque_check.found:
                latest[0].opaque_check.expires = 0
            return result

        monkeypatch.setattr(mediator, "live_prepared", reviewed)
        monkeypatch.setattr(Forwarder, "_route", delayed)
        headers = {"session-id": SESSION_A}
        url = "/company/v1/responses"
        assert client.post(url, headers=headers, json={"model": "gpt-4o", "input": "hello"}).status_code == 200
        response = client.post(url, headers=headers, json={"model": "gpt-4o", "input": [item]})
        assert response.status_code == (400 if expired else 200), "stale opaque receipt reached HTTP provider"
        assert len(sent.sent) == (1 if expired else 2)
        assert mediator.echoes._reserved == 0


@pytest.mark.parametrize("boundary", ["review", "sealed"])
@pytest.mark.parametrize("expired", [False, True], ids=["fresh", "expired"])
async def test_ws_send_rechecks_receipt_deadline_at_final_frame_gate(boundary, expired, monkeypatch):
    from websockets.exceptions import ConnectionClosed

    from latet.gateway import mediate, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable

    from .test_request_outcomes import _Client, _until, _Upstream

    item = {"type": "image_generation_call", "id": "ig_1", "result": OPAQUE}

    class Provider(_Upstream):
        def _answer(self, conn):
            try:
                for message in conn:
                    self.got.append(message)
                    conn.send(json.dumps({"type": "response.completed", "response": {"output": [item]}}))
            except ConnectionClosed:
                pass

    mediator = _mediator()
    upstream, client = Provider(), _Client()
    route = Upstream(
        "relay",
        "/relay",
        f"http://127.0.0.1:{upstream.port}",
        envelope_expected=frozenset(),
    )
    live, seal = mediator.live_prepared, ws.decide
    receipts, rewriters = [], []

    def rewriter(*args, **kwargs):
        result = WSResponseRewriter(*args, **kwargs)
        rewriters.append(result)
        return result

    async def reviewed(prepared, *args):
        result = await live(prepared, *args)
        receipts.append(result.opaque_check)
        if boundary == "review" and expired and result.opaque_check.found:
            result.opaque_check.expires = 0
        return result

    def sealed(prepared, *args, **kwargs):
        result = seal(prepared, *args, **kwargs)
        if boundary == "sealed" and expired and prepared is not None and prepared.opaque_check.found:
            prepared.opaque_check.expires = 0
        return result

    monkeypatch.setattr(mediator, "live_prepared", reviewed)
    monkeypatch.setattr(ws, "decide", sealed)
    monkeypatch.setattr(ws, "WSResponseRewriter", rewriter)
    task = asyncio.create_task(ws.relay(client.socket, RouteTable([route]), Capture(None), mediator))
    try:
        for count, contents in enumerate(("hello", [item]), 1):
            client.incoming.put_nowait(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "response.create", "model": "gpt-4o", "input": contents}),
                }
            )
            await _until(lambda count=count: len(client.texts()) == count)
        last = json.loads(client.texts()[-1])
        assert last["type"] == ("error" if expired else "response.completed"), "stale receipt reached WS provider"
        if expired:
            assert last["error"]["type"] == "latet_blocked"
        assert len(upstream.got) == (1 if expired else 2)
        assert len(rewriters) == 1 and rewriters[0].mediator is mediator
        state = rewriters[0]._target
        assert isinstance(state, mediate._ResponseState) and state.phase == "terminal" and not state.closed
        assert state.digest is None and state.token is None
        assert mediator.echoes._reserved == ECHO_ENTRY_BYTES
        assert len(receipts) == 2 and all(receipt._charge == 0 for receipt in receipts)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 10)
        assert mediator.echoes._reserved == 0
        assert state.closed and state.digest is None and state.token is None and rewriters[0]._target is False
    finally:
        task.cancel()
        upstream.close()
        mediator.close()


@pytest.mark.parametrize("expired", [False, True], ids=["fresh-retry", "expired-retry"])
def test_expiry_refusal_preserves_the_actual_first_aws_attempt(expired, monkeypatch):
    from latet.gateway import forward
    from latet.gateway.mediate import Prepared
    from latet.gateway.outcome import Attempted

    from .test_request_transport import _Aws, _bedrock, _invoke, _route

    mediator = _mediator()
    turns = []
    real_turn = forward._Turn
    current = Prepared.opaque_current
    aws = _Aws()

    def kept():
        turns.append(real_turn())
        return turns[-1]

    # The deadline decision itself has real-receipt HTTP/WS controls above. Inject only its refusal here to isolate
    # retry bookkeeping after an actual signed request received an actual ExpiredToken response.
    def receipt_current(self):
        return current(self) and not (expired and aws.reads == 2)

    monkeypatch.setattr(forward, "_Turn", kept)
    monkeypatch.setattr(Prepared, "opaque_current", receipt_current)
    gateway = _bedrock(mediator, aws)
    try:
        with gateway.client as client:
            response = client.post(_route(), content=_invoke("hello"), headers={"content-type": "application/json"})
        assert response.status_code == (400 if expired else 200)
        assert gateway.counts() == ((1, 1) if expired else (2, 2))
        assert turns[-1].execution == (Attempted(1, 403) if expired else Attempted(2, 200))
        if expired:
            assert "No further attempt was sent" in response.text and "Nothing was sent" not in response.text
        assert gateway.provider.answers[0].closed and aws.reads == 2 and aws.invalidated == ["work"]
        assert mediator.echoes._reserved == 0
    finally:
        mediator.close()


@pytest.mark.parametrize("refused", [False, True], ids=["committed", "cap-refused"])
@pytest.mark.parametrize("ending", ["drain", "close", "cancel"])
async def test_fix1_raw_pending_bytes_stay_charged_while_consumer_is_suspended(refused, ending):
    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.routes import RouteTable

    from .test_request_leaks import _request
    from .test_request_outcomes import OPENAI as FIXED_ROUTE
    from .test_request_outcomes import _chat

    mediator = _mediator()
    store = mediator.echoes
    # One pending chunk fits, the next cannot. The original streaming chunk is the transport's pre-existing buffer.
    if refused:
        store.limit = 66 * 1024
    raw = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": [{"type": "thinking", "signature": OPAQUE}]}}]}
    ).encode() + b" " * (130 * 1024)
    answer = _Answer(raw)
    transport = RecordingTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)
    )
    client = RecordingClient(transport)
    response = await Forwarder(RouteTable([FIXED_ROUTE]), Capture(None), client, mediator).handle(
        _request(_chat("hello"))
    )
    iterator = response.body_iterator
    try:
        first = await anext(iterator)
        pending = frame_locals(iterator.ag_frame)["pending"]
        measured = sys.getsizeof(pending) + sum(sys.getsizeof(part) for part in {id(x): x for x in pending}.values())
        assert pending and store._reserved >= measured, "live raw pending chunks became uncharged at yield"
        assert first in pending and (not store._items if refused else bool(store._items))
        charged = store._reserved
        print(f"FIX1_RAW refused={refused} ending={ending} actual={measured} charged={charged}")
        ceiling = store.retained_bytes
        store.ceiling = lambda extra: "retained-full" if store.retained_bytes + extra > ceiling else None
        assert not store.reserve(1), "pending bytes became available through the real retained ceiling"
        await asyncio.sleep(0.02)
        assert store._reserved == charged
        del pending  # the test must not extend the product's ownership lifetime
        if ending == "drain":
            received = [first]
            async for part in iterator:
                received.append(part)
            assert b"".join(received) == raw
        elif ending == "close":
            await iterator.aclose()
        else:
            waiting = asyncio.Event()

            async def consumer():
                try:
                    waiting.set()
                    await asyncio.Event().wait()
                finally:
                    await iterator.aclose()

            task = asyncio.create_task(consumer())
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert store._reserved == 0 and answer.closed
        await iterator.aclose()
        assert store._reserved == 0
        if refused:
            assert not store._items
    finally:
        await iterator.aclose()
        await client.aclose()
        mediator.close()


async def test_fix1_large_noentity_ws_observation_uses_worker_and_replays(monkeypatch):
    from websockets.exceptions import ConnectionClosed

    from latet.gateway import mediate, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable

    from .test_request_outcomes import _Client, _until, _Upstream
    from .test_restore_off_the_loop import HELD, _ticking

    item = {"type": "image_generation_call", "id": "ig_1", "result": "a" * (512 * 1024)}
    wire = json.dumps(
        {"type": "response.completed", "response": {"output": [item], "padding": "x" * (4 * 1024 * 1024)}}
    )
    short = json.dumps({"type": "response.completed", "response": {"status": "completed"}})

    class Provider(_Upstream):
        def _answer(self, conn):
            try:
                for message in conn:
                    self.got.append(message)
                    conn.send(wire if len(self.got) == 1 else short)
            except ConnectionClosed:
                pass

    mediator = _mediator()
    upstream, client = Provider(), _Client()
    route = Upstream(
        "relay",
        "/relay",
        f"http://127.0.0.1:{upstream.port}",
        envelope_expected=frozenset(),
    )
    loop_thread = get_ident()
    parses, hashes, observations, holds, sizes = [], [], [], [], []
    size_of = mediate._utf8_size
    live = mediator.live_prepared
    receipts, rewriters = [], []

    def rewriter(*args, **kwargs):
        result = WSResponseRewriter(*args, **kwargs)
        rewriters.append(result)
        return result

    async def reviewed(prepared, *args):
        result = await live(prepared, *args)
        receipts.append(result.opaque_check)
        return result

    def sized(text):
        if len(text) == len(wire):
            sizes.append(get_ident())
        return size_of(text)

    loads, member, on_frame, hold = (
        mediate.wellformed.loads,
        EchoCheck.member,
        WSResponseRewriter.on_frame,
        mediator.engine.hold,
    )

    def parsed(text, *args, **kwargs):
        if isinstance(text, str) and len(text) == len(wire):
            parses.append(get_ident())
        return loads(text, *args, **kwargs)

    def hashed(self, field):
        if len(field.holder[field.key]) == len(item["result"]):
            hashes.append(get_ident())
        return member(self, field)

    def observed(self, text):
        if len(text) == len(wire):
            observations.append((not self.session.entity_ids, len(holds)))
        return on_frame(self, text)

    def held(*args):
        holds.append(None)
        return hold(*args)

    monkeypatch.setattr(mediate.wellformed, "loads", parsed)
    monkeypatch.setattr(mediate, "_utf8_size", sized)
    monkeypatch.setattr(EchoCheck, "member", hashed)
    monkeypatch.setattr(WSResponseRewriter, "on_frame", observed)
    monkeypatch.setattr(mediator.engine, "hold", held)
    monkeypatch.setattr(mediator, "live_prepared", reviewed)
    monkeypatch.setattr(ws, "WSResponseRewriter", rewriter)
    task = asyncio.create_task(ws.relay(client.socket, RouteTable([route]), Capture(None), mediator))
    try:
        async with _ticking() as gaps:
            client.incoming.put_nowait(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "response.create", "model": "gpt-4o", "input": "hello"}),
                }
            )
            await _until(lambda: len(client.texts()) == 1)
            assert client.texts()[0] == wire and observations == [(True, len(holds))]
            assert parses and hashes and sizes and all(thread != loop_thread for thread in parses + hashes + sizes), (
                "large clean WS observation ran on event loop"
            )
        assert max(gaps) < HELD
        print(f"FIX1_WS bytes={len(wire)} parse_hash_off_loop=True max_gap={max(gaps):.6f}")
        client.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "response.create", "model": "gpt-4o", "input": [item]}),
            }
        )
        await _until(lambda: len(client.texts()) == 2)
        assert client.texts()[1] == short and len(upstream.got) == 2
        assert len(rewriters) == 1 and rewriters[0].mediator is mediator
        state = rewriters[0]._target
        assert isinstance(state, mediate._ResponseState) and state.phase == "terminal" and not state.closed
        assert state.digest is None and state.token is None
        assert mediator.echoes._reserved == ECHO_ENTRY_BYTES
        assert len(receipts) == 2 and all(receipt._charge == 0 for receipt in receipts)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 10)
        assert mediator.echoes._reserved == 0
        assert state.closed and state.digest is None and state.token is None and rewriters[0]._target is False
    finally:
        task.cancel()
        upstream.close()
        mediator.close()


@pytest.mark.parametrize("kind", ["json", "sse", "ws"])
@pytest.mark.parametrize("cancel", [False, True], ids=["completed-control", "cancelled-hash"])
async def test_fix1_close_never_waits_for_active_capture_hash(kind, cancel, monkeypatch):
    import time

    from .test_restore_off_the_loop import HELD, _ticking

    mediator = _mediator()
    prepared = mediator.prepare(
        OPENAI,
        "POST",
        "/openai/v1/chat/completions",
        {},
        json.dumps(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "write to emily.carter@acme.com"}]}
        ).encode(),
    )
    session = prepared.session
    assert session is not None and session.entity_ids
    store = mediator.echoes
    check = EchoCheck(b"k" * 32, store, EchoScope(("company",), "responses"), session.key, {})
    capture = OpaqueCapture(check, store)
    second = OPAQUE + "A"
    answer = {
        "output": [{"type": "image_generation_call", "result": value} for value in (OPAQUE, second)],
        "padding": "x" * (128 * 1024),
    }
    completed = {"type": "response.completed", "response": answer}
    busy, go, watchdog = Event(), Event(), Event()
    member = check.member
    threads = []

    def hashing(field):
        if field.holder[field.key] == second:
            threads.append(get_ident())
            busy.set()
            if not go.wait(1):  # independent of the event loop; causal RED cannot leave the job hung
                watchdog.set()
        return member(field)

    monkeypatch.setattr(check, "member", hashing)
    if kind == "json":
        transform = JSONTransform(
            "responses", mediator.restore_context(session), None, mediator=mediator, opaque=capture
        )
        transform.feed(json.dumps(answer).encode())
        operation = transform.restored_end()
    elif kind == "sse":
        transform = SSETransform(
            "responses", mediator.restore_context(session), None, lambda _: None, mediator=mediator, opaque=capture
        )

        async def finish():
            return [part async for part in transform.restored(event(completed))]

        operation = finish()
    else:
        transform = WSResponseRewriter(mediator, session)
        transform.opaque = capture
        operation = transform.restored(json.dumps(completed))
    task = asyncio.create_task(operation)
    try:
        async with _ticking() as gaps:
            await entered(busy)
            assert threads == [threads[0]] and threads[0] != get_ident()
            assert store._reserved >= ECHO_ENTRY_BYTES
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                started = time.perf_counter()
                transform.close()
                elapsed = time.perf_counter() - started
                assert elapsed < HELD and not watchdog.is_set(), "capture close blocked event loop on hashing worker"
                print(f"FIX1_CANCEL kind={kind} close_seconds={elapsed:.6f} held={store._reserved}")
                assert store._reserved >= ECHO_ENTRY_BYTES and capture.closed and not store._items
                await asyncio.sleep(0.03)
                assert not watchdog.is_set() and store._reserved >= ECHO_ENTRY_BYTES
                transform.close()
            go.set()
            if not cancel:
                await task
            await mediator.run(lambda: None)
        assert max(gaps) < HELD
        assert store._reserved == 0 and bool(store._items) is not cancel
        transform.close()
        transform.close()
        assert store._reserved == 0
    finally:
        go.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await mediator.run(lambda: None)
        transform.close()
        if prepared.lease is not None:
            prepared.lease.release()
        prepared.close_receipt()
        mediator.close()
