"""P2-4 S3/S4 causal checks: real relay and retained loopback sockets, no provider services.

Removing the final handshake comparison, per-frame proof, ID binding or local generation
check must expose the corresponding canary/send below. Existing refusals are controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed

from latet.gateway import request_policy, request_transport, ws
from latet.gateway.capture import Capture
from latet.gateway.routes import RouteTable, Upstream

from .test_request_outcomes import EMAIL, _Client, _mediator, _until


@pytest.fixture(autouse=True)
def contained_environment(monkeypatch):
    """Remove inherited credential/proxy selectors before any fixture opens a socket."""
    for name in tuple(os.environ):
        if name.startswith("AWS_") or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)


@dataclass
class SocketCase:
    mediator: Any
    client: Any
    routes: RouteTable
    task: asyncio.Task[Any]
    handshakes: list[Any]
    sent: list[str | bytes]
    port: int
    closed: list[int | None] = field(default_factory=list)

    async def frame(self, value: str | bytes) -> dict[str, Any]:
        before = len(self.client.sent)
        self.client.incoming.put_nowait(
            {"type": "websocket.receive", "text" if isinstance(value, str) else "bytes": value}
        )
        await _until(lambda: len(self.client.sent) > before or self.task.done(), seconds=3)
        if self.task.done():
            self.task.result()
        fresh = self.client.sent[before:]
        assert fresh, "relay ended without a client-facing result"
        message = fresh[0]
        return json.loads(message["text"]) if message["type"] == "websocket.send" else message


def create(text: str = "hello", *, nested: bool = False) -> str:
    body = {"model": "m", "input": text}
    return json.dumps({"type": "response.create", **({"response": body} if nested else body)})


@asynccontextmanager
async def socket_case(
    *,
    mediator: Any = True,
    query: bytes = b"",
    extra: tuple[tuple[bytes, bytes], ...] = (),
    terminal: bool = False,
    own: bool = True,
    pin: Any = None,
) -> AsyncIterator[SocketCase]:
    """One server owns port0 until context close; relay and all handler tasks are awaited."""
    handshakes: list[Any] = []
    sent: list[str | bytes] = []
    closed: list[int | None] = []
    turns = 0

    def handshake(connection, request):
        handshakes.append((request.path, tuple(request.headers.raw_items())))

    async def answer(connection):
        nonlocal turns
        try:
            async for data in connection:
                sent.append(data)
                body = json.loads(data) if isinstance(data, str) else {}
                if body.get("type") == "response.create":
                    turns += 1
                    kind = "response.completed" if terminal else "response.created"
                    event = {
                        "type": kind,
                        "response": {"id": f"resp_p24_{turns}", "status": "completed" if terminal else "in_progress"},
                    }
                else:
                    event = {"type": "ack"}
                await connection.send(json.dumps(event))
        except ConnectionClosed:
            pass
        finally:
            closed.append(connection.close_code)

    async with serve(answer, "127.0.0.1", 0, process_request=handshake, open_timeout=2, close_timeout=1) as server:
        port = server.sockets[0].getsockname()[1]
        print(f"P24_LOOPBACK_OPEN {port}")
        med = _mediator() if mediator is True else mediator
        client = _Client()
        client.socket.scope["extensions"] = {"websocket.http.response": {}}
        client.socket.scope["query_string"] = query
        client.socket.scope["headers"] += list(extra)
        route = Upstream(
            "relay",
            "/relay",
            f"http://127.0.0.1:{port}",
            own=own,
            envelope_expected=frozenset({("model", "m")}),
        )
        routes = RouteTable([route])
        if own and pin is None:

            async def pin(url):
                return url, "127.0.0.1", {"sni_hostname": "127.0.0.1"}

        task = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), med, pin=pin))
        case = SocketCase(med, client, routes, task, handshakes, sent, port, closed)
        try:
            await _until(lambda: bool(client.sent) or task.done(), seconds=4)
            if task.done():
                task.result()
            yield case
        finally:
            try:
                client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
                if not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), 4)
                    except TimeoutError:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        raise AssertionError("owned relay did not stop within four seconds") from None
                if not task.cancelled():
                    task.result()
            finally:
                server.close()
                await asyncio.wait_for(server.wait_closed(), 4)
                print(f"P24_LOOPBACK_CLOSED {port}")


@pytest.mark.parametrize("nested", [False, True])
async def test_top_and_nested_create_have_masked_payloads(nested):
    async with socket_case() as case:
        event = await case.frame(create(f"write to {EMAIL}", nested=nested))
        assert event["type"] == "response.created"
        assert len(case.sent) == 1 and EMAIL not in case.sent[0]
        assert len(case.handshakes) == 1


@pytest.mark.parametrize("carrier", ["query", "header"])
async def test_handshake_canary_blocks_before_connect(carrier):
    query = b"uncovered=p24-handshake-canary" if carrier == "query" else b""
    extra = ((b"x-uncovered-p24", b"p24-handshake-canary"),) if carrier == "header" else ()
    pins: list[str] = []

    async def pin(url):
        pins.append(url)
        return url, "127.0.0.1", {"sni_hostname": "127.0.0.1"}

    async with socket_case(query=query, extra=extra, own=True, pin=pin) as case:
        assert pins == [], "uncovered handshake reached destination pin"
        assert case.handshakes == [], "uncovered handshake reached provider"
        assert case.client.sent[-1]["code"] == 1008


async def test_a_websocket_handshake_takes_no_request_exception(monkeypatch):
    """P2-6: request exceptions are taken over HTTP alone in this revision; a handshake that only an exception would
    admit is refused before anything connects, as it would be without one (never loosened)."""
    from types import SimpleNamespace

    from latet.gateway import request_profiles

    grant = SimpleNamespace(id="x0123456789", project=None)
    monkeypatch.setattr(
        request_profiles,
        "_excepted",
        lambda upstream, profile, kind, name: (grant, "checked") if name == "x-team" else None,
    )
    async with socket_case(extra=((b"x-team", b"data-science"),)) as case:
        assert case.handshakes == [], "a handshake admitted by an exception reached the provider"
        assert case.client.sent[-1]["code"] == 1008


async def test_no_mediator_cannot_open_a_provider_socket():
    async with socket_case(mediator=None) as case:
        assert case.handshakes == [], "unmediated handshake reached provider"
        assert case.client.sent[-1]["code"] == 1008


@pytest.mark.parametrize("change", ["path", "host", "duplicate-host", "version", "extension", "key"])
async def test_the_actual_library_handshake_is_checked_before_writing(change, monkeypatch):
    check = request_transport._handshake_refusal

    def changed(request, authorization, *expected):
        if change == "path":
            request.path += "?uncovered=p24-final-target"
        else:
            raw = list(request.headers.raw_items())
            if change == "duplicate-host":
                raw.insert(1, ("Host", "collector.invalid"))
            else:
                name, value = {
                    "host": ("Host", "collector.invalid"),
                    "version": ("Sec-WebSocket-Version", "12"),
                    "extension": ("Sec-WebSocket-Extensions", "permessage-deflate; uncovered=p24"),
                    "key": ("Sec-WebSocket-Key", "AAAAAAAAAAAAAAAAAAAAAA=="),
                }[change]
                raw = [(key, value if key == name else old) for key, old in raw]
            # Keep generated-prefix and caller-suffix ordering intact: value/cardinality is the only change.
            request.headers = Headers(raw)
        return check(request, authorization, *expected)

    monkeypatch.setattr(request_transport, "_handshake_refusal", changed)
    async with socket_case() as case:
        assert case.handshakes == [], "changed library handshake bytes reached provider"


async def test_a_library_normalized_handshake_target_blocks(monkeypatch):
    connecting = request_transport.connect_ws

    async def changed(authorization, url, pinned, *bounds):
        return await connecting(authorization, url + "?uncovered=caf\u00e9", pinned, *bounds)

    monkeypatch.setattr(request_transport, "connect_ws", changed)
    async with socket_case() as case:
        assert case.handshakes == [], "library-normalized unproven query was transmitted"


@pytest.mark.parametrize("kind", ["binary", "unknown", "duplicate-cancel"])
async def test_binary_unknown_and_duplicate_frames_cannot_fall_through(kind):
    bad = {
        "binary": b"\x00p24-binary",
        "unknown": '{"type":"uncovered.p24","payload":"p24-canary"}',
        "duplicate-cancel": '{"type":"response.cancel","response_id":"resp_one","response_id":"resp_two"}',
    }[kind]
    async with socket_case() as case:
        event = await case.frame(bad)
        assert event["type"] == "error" and case.sent == []
        following = await case.frame(create())
        assert following["type"] == "response.created" and len(case.sent) == 1


async def test_a_turn_that_gives_a_key_twice_leaves_written_anew_with_the_copy_latet_read():
    """One code path with HTTP: the shadow copy never leaves; what was read, masked, is what is sent."""
    async with socket_case() as case:
        event = await case.frame('{"type":"response.create","model":"m","input":"p24-shadow","input":"hello"}')
        assert event["type"] == "response.created" and len(case.sent) == 1
        assert "p24-shadow" not in str(case.sent[0]) and "hello" in str(case.sent[0])


async def test_each_frame_requires_its_own_current_payload_proof(monkeypatch):
    mediator = _mediator()
    mask = mediator.mask_ws_frame

    def without_proof(*args, **kwargs):
        data, prepared = mask(*args, **kwargs)
        assert prepared is not None and prepared.body_proof is not None
        prepared.body_ledger = None
        prepared.body_proof = None
        return data, prepared

    monkeypatch.setattr(mediator, "mask_ws_frame", without_proof)
    async with socket_case(mediator=mediator) as case:
        event = await case.frame(create())
        assert event["type"] == "error" and case.sent == [], "missing own body proof fell through legacy"


async def test_frame_authorization_is_rechecked_at_send(monkeypatch):
    sending = request_transport.send_frame

    async def changed(connection, authorized):
        monkeypatch.setattr(request_policy, "BUILT_IN_POLICY", request_policy.PolicySnapshot(()))
        return await sending(connection, authorized)

    monkeypatch.setattr(request_transport, "send_frame", changed)
    async with socket_case() as case:
        event = await case.frame(create())
        assert case.sent == [], "frame token survived a policy change before actual send"
        assert event.get("code") == 1008 or event["type"] == "error"


@pytest.mark.parametrize("target", ["known", "unknown", "after-terminal", "superseded"])
async def test_cancel_ids_are_bound_not_arbitrary_strings(target):
    async with socket_case(terminal=target == "after-terminal") as case:
        first = await case.frame(create())
        assert first["type"] in {"response.created", "response.completed"}
        if target == "superseded":
            assert (await case.frame(create("next")))["type"] == "response.created"
        before = len(case.sent)
        identifier = "resp_unobserved" if target == "unknown" else "resp_p24_1"
        cancel = json.dumps({"type": "response.cancel", "response_id": identifier})
        event = await case.frame(cancel)
        if target == "known":
            assert event["type"] == "ack" and case.sent[-1] == cancel
        else:
            assert event["type"] == "error" and len(case.sent) == before


@pytest.mark.parametrize("event_id", [None, "new-cancel-correlation", "z" * 512, "\U0001f680" * 512])
async def test_cancel_fresh_checked_event_id_and_exact_bound_are_allowed(event_id):
    async with socket_case() as case:
        body = {"type": "response.cancel"}
        if event_id is not None:
            body["event_id"] = event_id
        cancel = json.dumps(body)
        event = await case.frame(cancel)
        assert event["type"] == "ack" and case.sent == [cancel]


@pytest.mark.parametrize("event_id", [EMAIL, "z" * 513, "", "\ud800"], ids=["secret", "one-over", "empty", "surrogate"])
async def test_cancel_correlation_is_checked_and_bounded(event_id):
    async with socket_case() as case:
        # Ordinary detector-visible email is unsafe even before it was learned by another turn.
        event = await case.frame(json.dumps({"type": "response.cancel", "event_id": event_id}))
        assert event["type"] == "error" and case.sent == []


@pytest.mark.parametrize(
    ("enabled", "project_stricter"),
    [(True, False), (False, False), (False, True)],
    ids=["enabled-control", "disabled-project", "disabled-stricter-project"],
)
async def test_cancel_fields_stay_strict_when_project_is_disabled(enabled, project_stricter, monkeypatch):
    from latet.engine.masker import Masker
    from latet.engine.pipeline import Scope
    from latet.engine.types import Policy

    mediator = _mediator()
    masker = mediator.engine.masker
    if project_stricter:
        masker = Masker(mediator.engine.vault, masker.detectors)
        mediator.verifier.masker.policies["EMAIL"] = Policy.ALLOW
    scope = Scope("p24-cancel-scope", "p24-cancel-scope", masker, enabled=enabled)
    monkeypatch.setattr(mediator.engine, "scope", lambda session: scope)
    try:
        if not enabled:  # D5: a socket of a project where Latet is off is refused before it connects (P2-6)
            async with socket_case(mediator=mediator) as case:
                await _until(case.task.done, seconds=3)
                closes = [m for m in case.client.sent if m["type"] == "websocket.close"]
                assert case.handshakes == [] and case.sent == [] and [m["code"] for m in closes] == [1008]
                assert "Latet is off" in closes[0]["reason"]
            return
        async with socket_case(mediator=mediator) as case:
            event = await case.frame(json.dumps({"type": "response.cancel", "event_id": EMAIL}))
            assert event["type"] == "error" and case.sent == [], "disabled scope bypassed control inspection"
            for body in ({"type": "response.cancel"}, {"type": "response.cancel", "event_id": "fresh-safe-id"}):
                cancel = json.dumps(body)
                assert (await case.frame(cancel))["type"] == "ack"
                assert case.sent[-1] == cancel
            assert len(case.sent) == 2
            assert scope.enabled is enabled and scope.masker is masker
    finally:
        mediator.close()


@pytest.mark.parametrize("prior_checked", [False, True], ids=["fresh-control", "genuine-prior-check"])
async def test_cancel_correlation_cannot_borrow_checked_conversation_history(prior_checked, monkeypatch):
    from latet.engine.types import EntityClass, SlotKind
    from latet.engine.vault import Entity
    from latet.gateway.outcome import Block

    mediator = _mediator()
    decisions = []
    finalize = ws.decide
    text = "CrimsonHarbor"

    def observed(prepared, *args, **kwargs):
        result = finalize(prepared, *args, **kwargs)
        decisions.append((result, prepared))
        return result

    monkeypatch.setattr(ws, "decide", observed)
    try:
        async with socket_case(mediator=mediator) as case:
            session = await mediator.run(mediator.engine.session, "session-id:5f4a3e00-0000-4000-8000-00000000f4a3")
            if prior_checked:
                result = await mediator.run(mediator.verifier.verify, [(text, SlotKind.OTHER, False)], session.key)
                assert not result.blocking and not result.rules and not result.authority
            assert mediator.verifier.sent(session.key, text) is prior_checked
            await mediator.run(
                mediator.engine.vault.learn,
                Entity("p24-cancel-history", "TERM", EntityClass.CUSTOM, text, "HiddenLabel", 1, 1.0),
            )
            event = await case.frame(json.dumps({"type": "response.cancel", "event_id": text}))
            assert event["type"] == "error" and case.sent == [], "history silently authorized a sensitive control"
            decision, prepared = decisions[-1]
            assert type(decision) is Block and prepared.session is session and not prepared.off
            assert prepared.body_proof is None
            cancel = '{"type":"response.cancel"}'
            assert (await case.frame(cancel))["type"] == "ack" and case.sent == [cancel]
    finally:
        mediator.close()


async def test_cancel_configured_allow_retains_authority_on_fresh_and_cached_checks(monkeypatch):
    from latet.engine.types import Policy
    from latet.gateway.outcome import Forward

    mediator = _mediator()
    mediator.verifier.masker.policies["EMAIL"] = Policy.ALLOW
    decisions = []
    finalize = ws.decide

    def observed(prepared, *args, **kwargs):
        result = finalize(prepared, *args, **kwargs)
        decisions.append((result, prepared))
        return result

    monkeypatch.setattr(ws, "decide", observed)
    cancel = json.dumps({"type": "response.cancel", "event_id": EMAIL})
    try:
        async with socket_case(mediator=mediator) as case:
            for count in (1, 2):
                assert (await case.frame(cancel))["type"] == "ack" and case.sent == [cancel] * count
                decision, prepared = decisions[-1]
                assert type(decision) is Forward and not prepared.off
                assert prepared.body_authority == decision.proof.authority == ("configured policy",)
                assert not prepared.body_rules and not decision.proof.rules
    finally:
        mediator.close()


async def test_cancel_response_id_observed_on_another_socket_does_not_authorize():
    async with socket_case() as first, socket_case() as second:
        assert (await first.frame(create()))["type"] == "response.created"
        event = await second.frame('{"type":"response.cancel","response_id":"resp_p24_1"}')
        assert event["type"] == "error" and second.sent == []


@pytest.mark.parametrize("change", ["rules", "route"])
async def test_revoked_socket_sends_no_next_frame(change):
    async with socket_case() as case:
        assert (await case.frame(create()))["type"] == "response.created"
        if change == "rules":
            case.mediator.engine.rules_changed()
        else:
            case.routes.use([])
        event = await case.frame(create("second"))
        assert len(case.sent) == 1, "revoked connection sent another frame"
        assert event["type"] == "websocket.close" and event["code"] == 1008


def test_current_response_metadata_fits_existing_echo_entry():
    import sys
    from dataclasses import fields

    from latet.gateway.mediate import WSResponseRewriter
    from latet.gateway.provenance import ECHO_ENTRY_BYTES

    mediator = _mediator()
    rewriter = WSResponseRewriter(mediator, mediator.engine.session("p24-measure"))
    rewriter.begin()
    rewriter.on_frame('{"type":"response.created","response":{"id":"resp_measure"}}')
    state = rewriter._target
    state.generation = 2**63 - 1
    # Actual slotted state, every field (including phase, lock and digest/token), and containing/owner references.
    metadata = (
        sys.getsizeof(state)
        + sum(sys.getsizeof(getattr(state, f.name)) for f in fields(state))
        + sys.getsizeof({"state": state, "owner": mediator.echoes})
        + sys.getsizeof(mediator.echoes)
    )
    print(f"P24_RESPONSE_ACTUAL_METADATA {metadata} limit={ECHO_ENTRY_BYTES}")
    assert metadata <= ECHO_ENTRY_BYTES
    with pytest.raises(ValueError, match="generation limit"):
        rewriter.begin()
    assert state.generation == 2**63 - 1
    rewriter.close()
    assert mediator.echoes.retained_bytes == 0
    mediator.close()


@pytest.mark.parametrize("end", ["terminal", "supersede", "close"])
def test_response_target_uses_same_owner_and_clears(end, monkeypatch, tmp_path):
    from latet.config import Config
    from latet.gateway.app import GatewayConfig
    from latet.gateway.mediate import WSResponseRewriter, _ResponseState
    from latet.gateway.provenance import ECHO_ENTRY_BYTES
    from latet.runtime import Runtime

    mediator = _mediator()
    runtime = Runtime(Config(), None, None, GatewayConfig(mediator=mediator), tmp_path)
    rewriter = WSResponseRewriter(mediator, mediator.engine.session("p24-response-metadata"))
    owner = mediator.echoes
    before = owner.retained_bytes
    reserve = owner.reserve
    charges = []
    allocations = []
    initialize = _ResponseState.__init__

    def charged(size):
        assert rewriter._target is None  # precharge, before state allocation/publication
        charges.append(size)
        return reserve(size)

    monkeypatch.setattr(owner, "reserve", charged)

    def allocated(self, *args, **kwargs):
        assert owner.retained_bytes == before + ECHO_ENTRY_BYTES
        allocations.append(True)
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(_ResponseState, "__init__", allocated)
    global_before = runtime.request_retained_bytes
    rewriter.begin()
    rewriter.on_frame('{"type":"response.created","response":{"id":"resp_p24_metadata"}}')
    record = rewriter._target
    assert allocations == [True] and charges == [ECHO_ENTRY_BYTES]
    assert owner.retained_bytes == before + ECHO_ENTRY_BYTES
    assert runtime.request_retained_bytes == global_before + ECHO_ENTRY_BYTES
    assert "resp_p24_metadata" not in repr(record)
    check = rewriter.target_check("resp_p24_metadata")
    assert check()
    if end == "terminal":
        rewriter.on_frame('{"type":"response.completed","response":{"id":"resp_p24_metadata"}}')
    elif end == "supersede":
        rewriter.begin()
    else:
        rewriter.close()
    assert not check()
    assert record.digest is None and record.token is None
    assert owner.retained_bytes == before + (0 if end == "close" else ECHO_ENTRY_BYTES)
    rewriter.close()
    assert owner.retained_bytes == before
    assert runtime.request_retained_bytes == global_before
    mediator.close()


def test_response_target_reservation_refusal_and_generation_bound():
    from latet.gateway.mediate import WSResponseRewriter

    mediator = _mediator()
    mediator.echoes.ceiling = lambda size: "retained-limit"
    rewriter = WSResponseRewriter(mediator, mediator.engine.session("p24-response-refused"))
    rewriter.begin()
    rewriter.on_frame('{"type":"response.created","response":{"id":"resp_p24_refused"}}')
    assert not rewriter.target_check("resp_p24_refused")() and mediator.echoes.retained_bytes == 0
    assert rewriter._target is False
    mediator.echoes.ceiling = None
    rewriter.begin()
    rewriter.on_frame('{"type":"response.created","response":{"id":"resp_later"}}')
    assert rewriter._target is False and not rewriter.target_check("resp_later")()
    assert mediator.echoes.retained_bytes == 0
    rewriter.close()
    mediator.close()


@pytest.mark.parametrize("sequence", ["terminal-before-created", "overlap", "clean-sequential", "duplicate-created"])
def test_response_target_requires_unambiguous_live_phase(sequence):
    from latet.gateway.mediate import WSResponseRewriter

    mediator = _mediator()
    rewriter = WSResponseRewriter(mediator, mediator.engine.session("p24-phase"))

    def created(name):
        return rewriter.on_frame(json.dumps({"type": "response.created", "response": {"id": name}}))

    def terminal():
        return rewriter.on_frame('{"type":"response.completed","response":{}}')

    try:
        rewriter.begin()
        if sequence == "terminal-before-created":
            terminal()
            created("resp_late")
            assert not rewriter.target_check("resp_late")()
        else:
            created("resp_first")
            assert rewriter.target_check("resp_first")()
            if sequence == "duplicate-created":
                created("resp_repeat")
                assert not rewriter.target_check("resp_repeat")()
                assert not rewriter.target_check("resp_first")()
            else:
                if sequence == "clean-sequential":
                    terminal()
                rewriter.begin()
                created("resp_second")
                assert rewriter.target_check("resp_second")() is (sequence == "clean-sequential")
                assert not rewriter.target_check("resp_first")()
                if sequence == "overlap":
                    terminal()
                    rewriter.begin()
                    created("resp_third")
                    assert not rewriter.target_check("resp_third")()
    finally:
        rewriter.close()
        mediator.close()


@pytest.mark.parametrize("reason", ["overlap", "reservation"])
async def test_disabled_target_keeps_fixed_cancel_usable_and_releases_on_close(reason):
    mediator = _mediator()
    if reason == "reservation":
        mediator.echoes.ceiling = lambda added: "retained-limit"
    async with socket_case(mediator=mediator) as case:
        assert (await case.frame(create()))["type"] == "response.created"
        target = "resp_p24_1"
        if reason == "overlap":
            assert (await case.frame(create("second")))["type"] == "response.created"
            target = "resp_p24_2"
        refused = await case.frame(json.dumps({"type": "response.cancel", "response_id": target}))
        assert refused["type"] == "error"
        cancel = '{"type":"response.cancel"}'
        assert (await case.frame(cancel))["type"] == "ack" and case.sent[-1] == cancel
    assert mediator.echoes._reserved == 0
    mediator.close()


@pytest.mark.parametrize("ending", ["cancel", "exception"])
async def test_response_state_released_when_real_relay_ends_early(ending, monkeypatch):
    mediator = _mediator()
    try:
        async with socket_case(mediator=mediator) as case:
            assert (await case.frame(create()))["type"] == "response.created"
            assert mediator.echoes._reserved >= 1024
            if ending == "cancel":
                case.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await case.task
            else:

                async def failed(*args, **kwargs):
                    raise RuntimeError("owned transport failed")

                monkeypatch.setattr(request_transport, "send_frame", failed)
                ended = await case.frame('{"type":"response.cancel"}')
                assert ended["type"] == "websocket.close" and ended["code"] == 1011
        assert mediator.echoes._reserved == 0
    finally:
        mediator.close()


def test_response_state_allocation_error_refunds_precharge(monkeypatch):
    from latet.gateway.mediate import WSResponseRewriter, _ResponseState

    mediator = _mediator()
    rewriter = WSResponseRewriter(mediator, mediator.engine.session("p24-allocation"))

    def failed(self):
        assert mediator.echoes._reserved == 1024
        raise MemoryError("test allocation")

    monkeypatch.setattr(_ResponseState, "__init__", failed)
    with pytest.raises(MemoryError, match="test allocation"):
        rewriter.begin()
    assert mediator.echoes._reserved == 0 and rewriter._target is None
    rewriter.close()
    mediator.close()
