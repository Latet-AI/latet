"""Causal G1 regressions: worker-owned representations and exact WS producer bindings."""

from __future__ import annotations

import dataclasses
import json
import threading
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from latet.engine.types import SlotKind
from latet.gateway import outcome, request_policy, request_profiles, request_transport
from latet.gateway.approvals import Decision, ImageDecision
from latet.gateway.coverage import Envelope, EnvelopeProof, envelope_of, frame_envelope, inventory
from latet.gateway.mediate import LiveItem, Prepared
from latet.gateway.recorder import Outcome, Recorder
from latet.gateway.routes import Upstream
from latet.protocols.base import ImageRef, JsonRef

from ..fake_ocr import FakeOcr
from .test_request_outcomes import EMAIL, OPENAI, _chat, _envelope, _mediator, _prepared, _strict, _until
from .test_request_ws_guard import _Quick

# An HTTP request's decision is finalized as the forwarder finalizes it, with its envelope's proof (``_strict``): the
# compatibility shape ``outcome.decide`` is a WebSocket frame's, and refuses an HTTP request or a handshake it is given.


async def test_json_text_is_frozen_in_the_worker_and_shared_at_both_consumers(monkeypatch):
    mediator = _mediator()
    calls: list[tuple[str, str]] = []
    get = JsonRef.get

    def read(ref: JsonRef) -> str:
        text = get(ref)
        calls.append((threading.current_thread().name, text))
        return text

    monkeypatch.setattr(JsonRef, "get", read)
    body = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string", "description": "x" * 8192}},
                        },
                    },
                }
            ],
        }
    ).encode()
    arena = await mediator.run_held(_prepared, mediator, body)
    index = next(i for i, (slot, _, _) in enumerate(arena.pending) if isinstance(slot, JsonRef))
    after_worker = len(calls)
    await mediator.run(arena.refresh_recording)
    assert len(calls) == after_worker  # unchanged worker refresh reuses the serialized JsonRef
    envelope = _envelope(body)
    decision = _strict(arena, envelope)
    assert isinstance(decision, outcome.Forward)
    assert outcome.sendable(decision, arena, envelope) is decision
    snap = Recorder(None).snapshot(Outcome({}, "openai", "/v1/chat/completions", "http", 0, arena))
    assert snap is not None
    again = Recorder(None).snapshot(Outcome({}, "openai", "/v1/chat/completions", "http", 0, arena))
    assert len(calls) == after_worker  # neither finalization, send guard nor snapshot serializes JsonRef
    assert again is not None and again.blocks is snap.blocks  # prebuilt metadata is shared too
    assert calls and all(name.startswith("latet-engine") for name, _ in calls)
    assert decision.texts[index] is snap.blocks[index].text
    assert any(decision.texts[index] is text for _, text in calls)
    slot = arena.pending[index][0]
    nested = slot.container[slot.key]["properties"]["value"]
    nested["description"] = "changed in the same nested object"
    with pytest.raises(outcome.NotSendable, match="stale"):
        outcome.sendable(decision, arena, envelope)
    assert len(calls) == after_worker  # nested mutation checked without serialization too


@pytest.mark.parametrize("form", ["data_url", "base64url"])
async def test_real_image_representations_are_frozen_once_and_shared_with_snapshot(form, monkeypatch):
    reader = FakeOcr()
    png = reader.screenshot(["same"])
    mediator = _mediator(reader)
    calls: list[tuple[str, str]] = []
    data = ImageRef.data

    def read(ref: ImageRef) -> str:
        text = data(ref)
        calls.append((threading.current_thread().name, text))
        return text

    monkeypatch.setattr(ImageRef, "data", read)
    if form == "data_url":
        body, path = _chat("hello", image=png), "/v1/chat/completions"
    else:
        body = json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": png.replace("+", "-").replace("/", "_").rstrip("="),
                                }
                            }
                        ],
                    }
                ]
            }
        ).encode()
        path = "/v1beta/models/m:generateContent"
    arena = await mediator.run_held(mediator.prepare, OPENAI, "POST", path, {}, body)
    assert arena.images[0].ref.form == form
    after_worker = len(calls)
    frozen = arena.record_images[0].data  # worker-owned current image after M3 clean reencoding
    assert arena.images[0].current_original is None
    await mediator.run(arena.refresh_recording)
    assert len(calls) == after_worker  # normalization is reused on an unchanged worker refresh
    envelope = _envelope(body)
    decision = _strict(arena, envelope)
    assert outcome.sendable(decision, arena, envelope) is decision
    for history in ("full", "metadata"):
        snap = Recorder(None, history=history).snapshot(Outcome({}, "openai", path, "http", 0, arena))
        assert snap is not None and snap.images[0].data is frozen
    assert len(calls) == after_worker  # metadata history must not extract an image again either
    assert calls and all(name.startswith("latet-engine") for name, _ in calls)


@pytest.mark.parametrize("image_only", [False, True])
async def test_ws_finalizer_binds_the_exact_worker_frame_even_without_text_masks(image_only):
    reader = FakeOcr()
    mediator = _mediator(reader)
    frame: dict[str, Any] = {"type": "response.create", "model": "m", "input": f"write to {EMAIL}"}
    if image_only:
        frame["input"] = [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64," + reader.screenshot(["same"])},
                ],
            }
        ]
    raw = json.dumps(frame)
    session = await mediator.run(mediator.engine.session, "g1-final-frame")
    sent, arena = await mediator.run_held(mediator.mask_ws_frame, raw, session, OPENAI)
    assert arena is not None and not arena.block_reason
    if image_only:
        assert arena.masked_count == 0
        item = LiveItem(0, "same", SlotKind.IMAGE_TEXT, None, None, False, "same", image=arena.images[0])
        await mediator.run(
            mediator._apply_live_prepared, arena, [item], Decision(True, images=[ImageDecision(0, withhold=True)])
        )
        assert arena.amended and arena.masked_count == 0
        sent = arena.frame_payload
        assert isinstance(sent, str)
    else:
        assert EMAIL not in sent
    handshake = envelope_of({"type": "websocket", "path": "/v1/responses"}, "ws-handshake", "openai", connection=31)
    envelope = frame_envelope(handshake, raw)
    try:
        for invalid in (raw, raw + " ", sent + " "):
            with pytest.raises(ValueError, match="frame leaves"):
                outcome.decide(arena, envelope, payload=invalid)
            assert arena.sealed is None
        decision = outcome.decide(arena, envelope, payload=sent)
        assert outcome.sendable(decision, arena, envelope).payload is sent
    finally:
        if arena.lease is not None:
            arena.lease.release()


@pytest.mark.parametrize(
    ("field", "value"), [("connection", 42), ("identity", "other-person"), ("destination", "other-provider")]
)
async def test_a_foreign_socket_is_refused_before_spending_the_frame(field, value):
    server = _Quick()
    handshake = envelope_of(
        {"type": "websocket", "path": "/v1/responses", "latet.identity": "person"},
        "ws-handshake",
        "openai",
        connection=41,
    )
    other = dataclasses.replace(handshake, **{field: value})
    policy = request_policy.current()
    url = f"ws://127.0.0.1:{server.port}/v1/responses"
    connections = []
    try:
        for context in (handshake, other):
            arena, shook = _decided_handshake(context, url)
            authorized = request_transport.validate_connect(shook, context, policy, [], arena)
            assert isinstance(authorized, request_transport.AuthorizedConnect)
            socket = await request_transport.connect_ws(authorized, url, None)
            assert not isinstance(socket, request_transport.SendRefusal)
            connections.append(socket)
        envelope, arena, decision = _decided_frame(handshake, _FRAME)
        assert request_transport.validate_frame(
            decision, arena, envelope, policy, connections[1]
        ) == request_transport.SendRefusal("foreign")
        authorized = request_transport.validate_frame(decision, arena, envelope, policy, connections[0])
        assert isinstance(authorized, request_transport.AuthorizedFrame)  # foreign refusal spent nothing
        assert await request_transport.send_frame(connections[1], authorized) == request_transport.SendRefusal("client")
        envelope, arena, again = _decided_frame(handshake, _FRAME)
        authorized = request_transport.validate_frame(again, arena, envelope, policy, connections[0])
        assert isinstance(authorized, request_transport.AuthorizedFrame)
        assert await request_transport.send_frame(connections[0], authorized) is None
        await _until(lambda: server.got == [_FRAME])
    finally:
        for socket in connections:
            socket.transport.abort()
        server.close()
    assert server.got == [_FRAME]


_FRAME = '{"type":"response.cancel"}'


def _decided_handshake(context: Envelope, url: str) -> tuple[Prepared, outcome.SendDecision]:
    """The handshake ``context`` decided as the relay decides it: its arena prepared by the mediator, the decision
    finalized with its envelope's proof for ``url``."""
    parts = urlsplit(url)
    upstream = Upstream(context.destination, "", f"http://{parts.netloc}")
    mediator = _mediator()
    session = mediator.engine.session("g1-foreign-socket")
    admission = request_profiles.admit(inventory(context), upstream)
    arena = mediator.prepare_handshake(upstream, parts.path, {}, admission.checked, session)
    proof = EnvelopeProof(
        context,
        len(admission.inventory.entries),
        admission.profile.id,
        admission.treatments,
        admission.headers,
        url,
        admission.checked,
        admission.removed,
    )
    decision = outcome.finalize(
        arena,
        context,
        request_policy.current(),
        outcome.response_plan(arena),
        envelope_proof=proof,
    )
    assert type(decision) is outcome.Forward
    return arena, decision


def _decided_frame(handshake: Envelope, payload: str) -> tuple[Envelope, Prepared, outcome.SendDecision]:
    """A frame of the socket ``handshake`` opened, read by the mediator and decided as it leaves."""
    mediator = _mediator()
    session = mediator.engine.session("g1-foreign-socket")
    upstream = Upstream(handshake.destination, "", "http://127.0.0.1:1")
    sent, arena = mediator.mask_ws_frame(payload, session, upstream, "/v1/responses")
    assert arena is not None
    envelope = frame_envelope(handshake, payload)
    decision = outcome.decide(arena, envelope, payload=sent)
    assert type(decision) is outcome.Forward
    return envelope, arena, decision


async def test_ws_count_only_review_keeps_the_baseline_compact_wire_representation(monkeypatch):
    """The existing relay writes compact JSON on a review count change, even if no slot text changes."""
    import asyncio

    from latet.gateway import ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, Upstream

    from .test_request_outcomes import _Client, _Upstream

    mediator = _mediator()
    mask = mediator.mask_ws_frame

    def reviewed_mask(*args: Any) -> Any:
        payload, arena = mask(*args)
        assert arena is not None
        arena.reviews = [
            cast(Any, object())
        ]  # only selects the real relay's review branch; no content-bearing review double
        return payload, arena

    def count_only(arena: Any, *args: Any) -> None:
        arena.masked_count += 1  # a repeated reviewer finding changes accounting while text is already fit to leave

    async def reviewed(arena: Any) -> Any:
        await mediator.run(mediator._apply_review, arena, None, {})
        return arena

    monkeypatch.setattr(mediator, "mask_ws_frame", reviewed_mask)
    monkeypatch.setattr(mediator, "_apply_review_changes", count_only)
    monkeypatch.setattr(mediator, "review_prepared", reviewed)
    upstream, client = _Upstream(), _Client()
    routes = RouteTable(
        [Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}", envelope_expected=OPENAI.envelope_expected)]
    )
    raw = '{\n  "type": "response.create", "model": "m", "input": "hello"\n}'
    relay = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), mediator))
    try:
        client.incoming.put_nowait({"type": "websocket.receive", "text": raw})
        await _until(lambda: bool(upstream.got))
        assert upstream.got == ['{"type":"response.create","model":"m","input":"hello"}']
    finally:
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relay, 10)
        upstream.close()


async def test_a_late_worker_adding_a_slot_refuses_stale_instead_of_raising():
    from latet.protocols.base import SlotRef

    mediator = _mediator()
    body = _chat("hello")
    arena = await mediator.run_held(_prepared, mediator, body)
    envelope = _envelope(body)
    decision = _strict(arena, envelope)

    def late_edit() -> None:
        message = {"role": "user", "content": "later"}
        arena.document["messages"].append(message)
        arena.pending.append((SlotRef(message, "content", SlotKind.USER_TEXT), [], True))
        arena.refresh_recording()

    await mediator.run(late_edit)
    with pytest.raises(outcome.NotSendable, match="stale"):
        outcome.sendable(decision, arena, envelope)


async def test_a_refreshed_worker_cannot_replace_selected_refs_with_the_same_text():
    from latet.protocols.base import SlotRef

    mediator = _mediator()
    body = _chat("hello")
    arena = await mediator.run_held(_prepared, mediator, body)
    envelope = _envelope(body)
    decision = _strict(arena, envelope)
    snap = Recorder(None).snapshot(Outcome({}, "openai", "/v1/chat/completions", "http", 0, arena))
    assert snap is not None and outcome.sendable(decision, arena, envelope) is decision

    def late_edit() -> None:
        message = {"role": "user", "content": arena.pending[0][0].get()}
        arena.document["messages"][0] = message
        arena.pending[0] = (SlotRef(message, "content", SlotKind.USER_TEXT), [], True)
        arena.refresh_recording()

    await mediator.run(late_edit)
    with pytest.raises(outcome.NotSendable, match="stale"):
        outcome.sendable(decision, arena, envelope)
