"""Typed outcome boundary: immutable Forward/Pass/Block decisions minted after final review, exact masked payload
and restoration, envelope proof, stale-state refusal and per-exchange lease ownership.
Phase 2 uses genuine rule-bearing Pass fixtures alongside ordinary Forward requests."""

from __future__ import annotations

import ast
import asyncio
import copy
import dataclasses
import gzip
import inspect
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from starlette.testclient import TestClient
from starlette.websockets import WebSocket
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.config import Config, IdentityConfig, LiveReview
from latet.engine.build import build
from latet.engine.pipeline import Engine
from latet.engine.stored import StoredVault
from latet.engine.vault import MaskingError, Vault
from latet.gateway import forward, outcome, request_policy, request_profiles, ws
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.approvals import Decision, HumanMask
from latet.gateway.capture import Capture
from latet.gateway.coverage import (
    CoverageProof,
    Envelope,
    EnvelopeProof,
    FinalEnvelope,
    envelope_of,
    frame_envelope,
    inventory,
    occurrences,
)
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import REWRITE, Mediator, Prepared
from latet.gateway.outcome import Block, Forward, NotSendable, Observation, Pass, ResponsePlan
from latet.gateway.routes import RouteTable, Upstream
from latet.project_file import REPO_KEYS, repo_settings
from latet.vision.masking import WITHHELD, ImageMasking

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr
from .test_stored_vault import _engine, _people

EMAIL = "emily.carter@acme.com"
CARD = 4111111111111111  # a card number written as a JSON number: the final check stops it
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1", envelope_expected=frozenset({("model", "m")}))
CHAT = "/openai/v1/chat/completions"
HEADERS = {"authorization": "Bearer sk-test", "content-type": "application/json"}
SRC = Path(__file__).resolve().parents[2] / "src" / "latet"


def _chat(text: str, *, tool: str | None = None, image: str | None = None) -> bytes:
    content: Any = text
    if image is not None:
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    if tool is not None:
        call = {"id": "call_c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_c1", "content": tool},
        ]
    return json.dumps({"model": "m", "messages": messages}).encode()


def _mediator(vision: FakeOcr | None = None) -> Mediator:
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    if vision is not None:
        mediator.vision = ImageMasking(vision)
    return mediator


def _prepared(mediator: Mediator, body: bytes) -> Prepared:
    return mediator.prepare(OPENAI, "POST", CHAT, HEADERS, body)


def _envelope(body: bytes) -> Envelope:
    scope = {
        "type": "http", "method": "POST", "path": CHAT, "raw_path": CHAT.encode(), "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in HEADERS.items()],
    }  # fmt: skip
    return envelope_of(scope, "http", "openai", body)


def _strict(arena: Prepared, envelope: Envelope, upstream: Upstream = OPENAI) -> outcome.SendDecision:
    admission = request_profiles.admit(inventory(envelope), upstream)
    proof = EnvelopeProof(
        envelope,
        len(admission.inventory.entries),
        admission.profile.id,
        admission.treatments,
        admission.headers,
        upstream.target(envelope.raw_path.decode(), envelope.query.decode()),
        admission.checked,
        admission.removed,
    )
    return outcome.finalize(
        arena,
        envelope,
        request_policy.current(),
        outcome.response_plan(arena),
        envelope_proof=proof,
    )


def _send(decision: Any, arena: Prepared | None, envelope: Envelope, sent: list[bytes | str]) -> None:
    """The consumer's seam: a payload leaves only through ``outcome.sendable``, as P1-3's transport takes it, for the
    exchange's own envelope."""
    sent.append(outcome.sendable(decision, arena, envelope).payload)


def _refused(decision: Any, arena: Prepared | None, envelope: Envelope, sent: list[bytes | str]) -> str:
    with pytest.raises(NotSendable) as refused:
        _send(decision, arena, envelope, sent)
    return refused.value.code


def _proof(envelope: Envelope, inbound: int | None = None, final: int = 1, missing: int | None = None) -> CoverageProof:
    """A proof over ``envelope``: complete unless ``inbound`` says another count or ``missing`` leaves one out."""
    count = occurrences(envelope) if inbound is None else inbound
    covered = frozenset(n for n in range(count) if n != missing)
    return CoverageProof(envelope, count, final, covered, frozenset(range(final)))


class _Provider:
    """A provider that answers each chat request with its own first text, and keeps every body it was sent."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.headers: list[list[tuple[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.content)
        self.headers.append([(k.lower(), v) for k, v in request.headers.multi_items()])
        content = json.loads(request.content)["messages"][0]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        answer = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(answer))


def _gateway(provider: _Provider, mediator: Mediator | None) -> TestClient:
    config = GatewayConfig(
        port=5283,
        upstreams=[OPENAI],
        mediator=mediator,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1:5283")


@pytest.fixture
def turns(monkeypatch) -> list[Any]:
    """Each request's ``_Turn``, as ``Forwarder.handle`` made it: its decision kept for P1-3's guard."""
    made: list[Any] = []
    real = forward._Turn

    def kept() -> Any:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(forward, "_Turn", kept)
    return made


# --- the three kinds ---------------------------------------------------------------------------------------------------


def test_block_has_no_transport_payload(turns):
    provider = _Provider()
    body = _chat("list the cards", tool=json.dumps({"card": CARD}))
    with _gateway(provider, _mediator()) as client:
        answered = client.post(CHAT, content=body, headers=HEADERS)
    assert answered.status_code == 400 and provider.sent == []  # nothing sent: 0.10's refusal, as it was
    [turn] = turns
    block = turn.decision
    assert type(block) is Block and block.status == 400 and block.code == "refused"
    assert block.reason == turn.prepared.block_reason and block.reason in answered.json()["error"]["message"]
    # Nothing of the request in the value: no payload, envelope, header, target, text or arena to send from.
    assert {f.name for f in dataclasses.fields(Block)} == {"transport", "destination", "code", "reason", "status"}
    for name in ("payload", "envelope", "body", "headers", "target", "texts", "document", "prepared", "frame"):
        assert not hasattr(block, name)
    assert str(CARD) not in repr(block)
    sent: list[bytes | str] = []
    assert _refused(block, turn.prepared, _envelope(body), sent) == "block" and sent == []


def _rule_pass(text: str) -> tuple[Mediator, bytes]:
    reader = FakeOcr()
    return _mediator(reader), _chat(text, image=reader.screenshot(["ordinary image"]))


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
def test_frozen_decision_detaches_slots_and_lists(rule):
    mediator, body = _rule_pass(f"write to {EMAIL}") if rule else (_mediator(), _chat(f"write to {EMAIL}"))
    prepared = _prepared(mediator, body)
    decision = _strict(prepared, _envelope(body))
    assert type(decision) is (Pass if rule else Forward) and prepared.masked_count == 1
    proof = decision.body_proof if rule else decision.proof
    before = (decision.texts, proof.receipts, proof.checked, proof.rules, proof.final_receipts)
    assert all(type(part) is tuple for part in before)
    text = decision.texts[0]
    assert EMAIL not in text and text == prepared.pending[0][0].get()
    assert decision.payload is prepared.body
    prepared.uninspected.append("image")
    prepared.degraded.append("llm-review")
    prepared.outbound.append("web_search")
    prepared.leaks.extend(prepared.leaks or [])
    prepared.images.clear()
    prepared.pending.clear()
    assert (decision.texts, proof.receipts, proof.checked, proof.rules, proof.final_receipts) == before
    assert proof.rules == (("image remainder after text recognition",) if rule else ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.payload = b"{}"
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.input_count = 0


def test_rule_pass_keeps_masked_payload_and_restore_plan(turns):
    provider = _Provider()
    mediator, body = _rule_pass(f"write to {EMAIL}")
    with _gateway(provider, mediator) as client:
        answered = client.post(CHAT, content=body, headers=HEADERS)
    assert answered.status_code == 200
    [turn] = turns
    decision = turn.decision
    assert type(decision) is Pass
    [sent] = provider.sent  # one send, of exactly the frozen payload: masked
    assert sent == decision.payload and EMAIL.encode() not in sent
    assert decision.evidence.rules == ("image remainder after text recognition",)
    assert decision.body_proof is not None and decision.envelope_proof is not None
    assert decision.policy is request_policy.current()
    assert decision.response == ResponsePlan(
        protocol="chat", stream=False, restore=True, session=turn.prepared.session.key
    )
    assert answered.json()["choices"][0]["message"]["content"] == f"write to {EMAIL}"  # restored: Pass keeps it on
    # Absent protection refuses before any provider request is built or sent.
    provider = _Provider()
    with _gateway(provider, None) as client:
        client.post(CHAT, content=body, headers=HEADERS)
    bare = turns[-1].decision
    assert type(bare) is Block and isinstance(turns[-1].execution, outcome.NotAttempted)
    assert provider.sent == []


def test_observation_cannot_authorize_send():
    mediator = _mediator()
    body = _chat(f"write to {EMAIL}")
    prepared = _prepared(mediator, body)
    observed = outcome.observe("final-check", prepared)
    assert type(observed) is Observation and observed.uninspected == tuple(prepared.uninspected)
    for name in ("payload", "envelope", "body", "texts", "policy", "response"):
        assert not hasattr(observed, name)
    sent: list[bytes | str] = []
    envelope = _envelope(body)
    assert _refused(observed, prepared, envelope, sent) == "not-a-decision"
    assert _refused(None, prepared, envelope, sent) == "not-a-decision"
    # Its arena given a Pass made by hand from what it observed: never minted, never sent.
    forged = Pass(
        envelope, prepared.body, (), outcome.PolicyEvidence(("checked only",), "chat.body.1"),
        request_policy.current(), ResponsePlan(),
    )  # fmt: skip
    assert _refused(forged, prepared, envelope, sent) == "unminted"
    assert sent == [] and prepared.sealed is None  # observing finalizes nothing


def test_forward_requires_a_complete_proof():
    body = _chat("hello")
    policy, plan = request_policy.current(), ResponsePlan()
    sent: list[bytes | str] = []
    envelope = _envelope(body)
    assert occurrences(envelope) == 2 + 2 + 1  # method and path, two headers, the body
    for partial in (
        CoverageProof(envelope, 0, 0, frozenset(), frozenset()),  # the proof of nothing
        CoverageProof(envelope, 0, 1, frozenset(), frozenset({0})),  # nothing of the request as it came
        _proof(envelope, final=0),  # nothing of it as it leaves
        _proof(envelope, missing=2),  # a header left out
        _proof(envelope, inbound=occurrences(envelope) - 1),  # a count that is not the request's
        CoverageProof(envelope, 5, 1, frozenset({0, 1, 2, 3, 4, 7}), frozenset({0})),  # one it does not have
    ):
        decided = outcome.finalize(None, envelope, policy, plan)  # no arena: never sent, whatever its proof
        assert not partial.complete and not partial.proves(envelope)
        assert type(decided) is Block and decided.code == "proof-incomplete" and decided.status == 400
        assert _refused(decided, None, envelope, sent) == "block"
    elsewhere = outcome.finalize(None, envelope, policy, plan)  # a proof (of another request or not) sends nothing
    assert type(elsewhere) is Block and elsewhere.code == "proof-incomplete"
    assert sent == []
    complete = _proof(envelope)
    decided = outcome.finalize(None, envelope, policy, plan)
    assert type(decided) is Block and complete.proves(envelope)
    assert _refused(decided, None, envelope, sent) == "block" and sent == []
    arena = _prepared(_mediator(), body)
    decided = _strict(arena, envelope)
    assert type(decided) is Forward and decided.proof.proves(envelope)
    other = _envelope(_chat("another request"))
    assert _refused(decided, arena, other, sent) == "foreign"  # presented for another request: never sent there
    _send(decided, arena, envelope, sent)
    assert sent == [body]


def test_hand_built_forward_is_refused(monkeypatch):
    body = _chat("hello")
    policy, plan = request_policy.current(), ResponsePlan()
    envelope = _envelope(body)
    complete = _proof(envelope)
    arena = _prepared(_mediator(), body)
    minted = _strict(arena, envelope)
    assert type(minted) is Forward

    class Sneaky(Pass):
        pass

    sent: list[bytes | str] = []
    hand_built = Forward(envelope, body, (), complete, policy, plan)  # a complete proof of its very request
    assert _refused(hand_built, None, envelope, sent) == "unminted"
    assert _refused(copy.copy(minted), None, envelope, sent) == "unminted"
    assert _refused(dataclasses.replace(minted, payload=b"{}"), None, envelope, sent) == "unminted"
    sneaky = Sneaky(envelope, body, (), outcome.PolicyEvidence(("checked only",), "chat.body.1"), policy, plan)
    assert _refused(sneaky, None, envelope, sent) == "unknown"
    assert sent == []
    _send(minted, arena, envelope, sent)
    assert sent == [body]
    object.__setattr__(minted, "payload", b'{"model": "m", "messages": []}')  # a minted value written over
    assert _refused(minted, None, envelope, sent) == "tampered" and sent == [body]
    # Written over one level down: in its plan, its envelope, its proof, its policy.
    for value, name, written in (
        (lambda d: d.response, "restore", False),
        (lambda d: d.envelope, "destination", "elsewhere"),
        (lambda d: d.proof, "final_count", 2),
        (lambda d: d.policy, "rules", ()),
    ):
        again = _envelope(body)
        # A private current snapshot for this test only, never mutate the process's original policy object.
        with monkeypatch.context() as local:
            local.setattr(request_policy, "BUILT_IN_POLICY", dataclasses.replace(request_policy.current()))
            fresh = _prepared(_mediator(), body)
            decided = _strict(fresh, again)
            assert outcome.sendable(decided, fresh, again) is decided
            object.__setattr__(value(decided), name, written)
            assert _refused(decided, fresh, again, sent) == "tampered", name
    # A payload that is not its arena's, minted all the same (only outcome.py can): the consumer still refuses it.
    mediator = _mediator()
    masked = _chat(f"write to {EMAIL}")
    arena, arrived = _prepared(mediator, masked), _envelope(masked)
    legit = _strict(arena, arrived)
    assert type(legit) is Forward
    rogue = outcome._mint(dataclasses.replace(legit, payload=masked), outcome._MINTED[legit].seal)
    assert _refused(rogue, arena, arrived, sent) == "payload" and sent == [body]


def test_a_pass_carries_what_its_arena_leaves_with_and_restores_it():
    """The review's probe B: a masked request whose ``Pass`` would carry the original, or switch restoration off, is
    refused before anything is finalized; the arena then finalizes as it should, and leaves masked, once."""
    mediator, body = _rule_pass(f"write to {EMAIL}")
    arena, envelope = _prepared(mediator, body), _envelope(body)
    assert arena.mode == REWRITE and arena.masked_count == 1 and EMAIL.encode() not in arena.body
    policy, plan = request_policy.current(), outcome.response_plan(arena)
    with pytest.raises(TypeError, match="no payload is given"):
        outcome.finalize(arena, envelope, policy, plan, payload=body)  # the original, given as its payload
    with pytest.raises(ValueError, match="restored where it was rewritten"):
        outcome.finalize(arena, envelope, policy, dataclasses.replace(plan, restore=False))
    with pytest.raises(ValueError, match="restored where it was rewritten"):
        outcome.finalize(arena, envelope, policy, dataclasses.replace(plan, session="conv:another"))
    with pytest.raises(ValueError, match="restored where it was rewritten"):
        outcome.finalize(None, envelope, policy, plan)  # none to restore
    assert arena.sealed is None  # nothing finalized by a refused call
    sent: list[bytes | str] = []
    decision = _strict(arena, envelope)
    assert type(decision) is Pass
    _send(decision, arena, envelope, sent)
    assert sent == [arena.body] and decision.payload is arena.body and decision.response.restore
    # A WebSocket turn: its payload is given, and required; a masked turn never leaves as it came.
    text = json.dumps({"type": "response.create", "model": "m", "input": f"write to {EMAIL}"})
    handshake = envelope_of(
        {"type": "websocket", "path": "/openai/v1/responses", "raw_path": b"/openai/v1/responses", "headers": []},
        "ws-handshake",
        "openai",
        connection=1,
    )
    session = mediator.engine.session("ws:frame")
    written, turn = mediator.mask_ws_frame(text, session, OPENAI)
    assert turn is not None and turn.masked_count == 1 and EMAIL not in written
    frame = frame_envelope(handshake, text)
    with pytest.raises(TypeError, match="the frame as it leaves"):
        outcome.decide(turn, frame)  # no payload: never the frame as it came by default
    with pytest.raises(ValueError, match="never as it came"):
        outcome.decide(turn, frame, payload=text)
    with pytest.raises(ValueError, match="leaves as it came"):
        outcome.decide(None, frame, payload=written)  # a frame Latet read nothing of is not rewritten
    assert turn.sealed is None
    decided = outcome.decide(turn, frame, payload=written)
    _send(decided, turn, frame, sent)
    assert sent[-1] == written


# --- the arena: finalized once, after its last change -----------------------------------------------------------------


def _withhold_image(prepared: Prepared) -> None:
    prepared.images[0].withhold("a late writer")


def _write_slot(prepared: Prepared) -> None:
    slot = prepared.pending[0][0]
    slot.put(slot.get() + " and more", [])


def _write_in_place(prepared: Prepared) -> None:
    prepared.document["messages"][0]["content"][0]["text"] = "changed in place"


def _write_body(prepared: Prepared) -> None:
    prepared.body = prepared.body + b" "


def _write_document(prepared: Prepared) -> None:
    prepared.document = copy.deepcopy(prepared.document)


def _write_reason(prepared: Prepared) -> None:
    prepared.block_reason = "stopped late"


def _write_session(prepared: Prepared) -> None:
    assert prepared.session is not None
    prepared.session = copy.copy(prepared.session)  # another conversation's: its answer restored under it


def _write_stream(prepared: Prepared) -> None:
    prepared.stream = not prepared.stream  # its answer read as another kind


def _write_protocol(prepared: Prepared) -> None:
    prepared.protocol = "responses"


CHANGES: dict[str, Callable[[Prepared], None]] = {
    "a reviewer writes a slot": _write_slot,
    "a late writer changes the document in place": _write_in_place,
    "a late writer replaces the body": _write_body,
    "a late writer replaces the document": _write_document,
    "an image is withheld": _withhold_image,
    "a refusal written late": _write_reason,
    "its conversation replaced": _write_session,
    "its answer read as a stream": _write_stream,
    "its protocol replaced": _write_protocol,
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_a_change_to_the_arena_after_the_decision_invalidates_it(change):
    reader = FakeOcr()
    image = reader.screenshot(["Customer since 2019, VIP"])  # read, nothing in it masked
    body = _chat(f"write to {EMAIL}", image=image)
    mediator = _mediator(reader)
    sent: list[bytes | str] = []
    untouched, kept = _prepared(mediator, body), _envelope(body)
    decision = _strict(untouched, kept)
    _send(decision, untouched, kept, sent)
    assert sent == [untouched.body] and EMAIL.encode() not in untouched.body  # one send, the frozen bytes
    sent.clear()
    prepared, envelope = _prepared(mediator, body), _envelope(body)
    assert len(prepared.images) == 1 and prepared.masked_count == 1
    decision = _strict(prepared, envelope)
    assert type(decision) is Pass
    payload, texts = decision.payload, decision.texts
    CHANGES[change](prepared)
    if change == "an image is withheld":
        assert prepared.images[0].state == WITHHELD
    assert _refused(decision, prepared, envelope, sent) == "stale" and sent == []  # zero sends
    assert decision.payload is payload and decision.texts == texts  # the value itself never changed
    other, theirs = _prepared(mediator, body), _envelope(body)  # another arena's decision is not this one's
    assert _refused(_strict(other, theirs), untouched, theirs, sent) == "foreign" and sent == []


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
@pytest.mark.parametrize("late", [False, True], ids=["manual-mask", "learned-while-awaiting"])
def test_finalization_happens_once_after_live_review(turns, monkeypatch, rule, late):
    mediator = _mediator()
    body = _chat(f"write to {EMAIL}")
    prepared = _prepared(mediator, body)
    _strict(prepared, _envelope(body))
    with pytest.raises(outcome.AlreadyFinal):
        outcome.decide(prepared, _envelope(body))
    # Through the gateway, the Live Reviewer on: you mask "Falcon" by hand; the request is finalized once, after.
    mediator, reviewed_body = (
        _rule_pass(f"write to {EMAIL} about Falcon") if rule else (_mediator(), _chat(f"write to {EMAIL} about Falcon"))
    )
    applied: list[str] = []
    finalized: list[tuple[list[str], bytes]] = []

    async def decided(batch: Any, gone: Any, held: Any = None) -> Decision:
        if late:
            from latet.engine.types import EntityClass

            await mediator.run(mediator.engine.vault.intern, "PERSON", EntityClass.LLM, "Falcon")
            return Decision(True)
        return Decision(True, masks=[HumanMask(0, "Falcon", "person")])

    apply = mediator._apply_live_prepared

    def applying(*args: Any) -> None:
        apply(*args)
        applied.append("live")

    real = outcome.finalize

    def finalize(arena: Any, *args: Any, **kwargs: Any) -> Any:
        finalized.append((list(applied), arena.body))
        return real(arena, *args, **kwargs)

    monkeypatch.setattr(mediator, "live_review", LiveReview(enabled=True))
    monkeypatch.setattr(mediator, "await_decision", decided)
    monkeypatch.setattr(mediator, "_apply_live_prepared", applying)
    monkeypatch.setattr(forward, "finalize", finalize)
    provider = _Provider()
    with _gateway(provider, mediator) as client:
        answered = client.post(CHAT, content=reviewed_body, headers=HEADERS)
    assert answered.status_code == 200
    assert len(finalized) == 1 and finalized[0][0] == ["live"]  # once, after your decision was applied
    decision = turns[-1].decision
    [sent] = provider.sent
    assert type(decision) is (Pass if rule else Forward) and sent == decision.payload == finalized[0][1]
    assert b"Falcon" not in sent and EMAIL.encode() not in sent  # your mask is in what the value carries
    assert turns[-1].prepared.masked_count == 2
    with pytest.raises(outcome.AlreadyFinal):
        real(
            turns[-1].prepared,
            _envelope(body),
            request_policy.current(),
            outcome.response_plan(turns[-1].prepared),
        )


# --- the built-in rules: internal, never configured --------------------------------------------------------------


def _names(command: Any) -> Iterator[str]:
    for param in getattr(command, "params", []):
        yield param.name or ""
        yield from param.opts
    for sub in getattr(command, "commands", {}).values():
        yield from _names(sub)


def test_the_built_in_rules_cannot_be_configured(monkeypatch, turns):
    from latet.cli.main import app

    assert inspect.signature(request_policy.current).parameters == {}
    assert (
        request_policy.current() is request_policy.BUILT_IN_POLICY
        and set(request_policy.BUILT_IN_POLICY.rules) == request_policy.BUILT_IN
    )
    with pytest.raises(ValueError, match="built-in rules alone"):
        request_policy.PolicySnapshot(rules=("forward-everything",))
    # Config: no setting names it, and one written in config.yaml is not a setting.
    assert not [name for name in Config.model_fields if "legacy" in name or "request_polic" in name]
    config = Config.model_validate({"request_policy": "off", "legacy": "forward"})
    assert not hasattr(config, "request_policy") and not hasattr(config, "legacy")
    # The environment: neither module reads it, and the snapshot is the same under any variable.
    monkeypatch.setenv("LATET_REQUEST_POLICY", "forward")
    monkeypatch.setenv("LATET_LEGACY", "off")
    assert request_policy.current() is request_policy.BUILT_IN_POLICY
    for module in ("gateway/outcome.py", "gateway/request_policy.py", "gateway/coverage.py"):
        tree = ast.parse((SRC / module).read_text("utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not names & {"environ", "getenv", "argv"}, module
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"os", "sys", "dotenv"}, module
    # The CLI: no command takes it.
    found = [name for name in _names(typer.main.get_command(app)) if "legacy" in name or "request-polic" in name]
    assert found == []
    # A repository file: not a key it may set, and said so.
    assert not [key for key in REPO_KEYS if "legacy" in key or ("polic" in key and key != "policies")]
    settings, notices = repo_settings({"request_policy": "legacy-0.10", "legacy": "on"})
    assert [(n.key, n.level, n.reason) for n in notices] == [
        ("request_policy", "ignored", "repo_unknown"),
        ("legacy", "ignored", "repo_unknown"),
    ]
    assert settings == repo_settings({})[0]
    # A header: the same decision, and the provider never sees it.
    provider = _Provider()
    body = _chat(f"write to {EMAIL}")
    with _gateway(provider, _mediator()) as client:
        client.post(CHAT, content=body, headers=HEADERS)
        client.post(CHAT, content=body, headers={**HEADERS, "x-latet-policy": "forward", "x-latet-rule": "off"})
    plain, asked = turns[-2].decision, turns[-1].decision
    assert (
        type(plain) is type(asked) is Forward
        and plain.proof.rules == asked.proof.rules == ()
        and plain.policy is asked.policy
    )
    assert all(not name.startswith("x-latet-") for name, _ in provider.headers[-1])
    assert (b"x-latet-policy", b"forward") in asked.envelope.headers  # seen in the envelope, granting nothing


# --- the lease ---------------------------------------------------------------------------------------------------------


class _Engine:
    """``Engine.hold``, ``renew``, ``reacquire`` and ``release``, recorded: each hold a handle of its own."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.made = 0

    def _handle(self) -> object:
        self.made += 1
        return f"handle-{self.made}"

    def hold(self, session: Any) -> object:
        handle = self._handle()
        self.calls.append(("hold", handle))
        return handle

    def renew(self, session: Any) -> bool:
        self.calls.append(("renew", session))
        return True

    def reacquire(self, session: Any, held: object) -> object:
        handle = self._handle()
        self.calls.append(("reacquire", (held, handle)))
        return handle

    def release(self, session: Any, held: object) -> None:
        self.calls.append(("release", held))


async def test_lease_releases_exactly_its_own_hold_once():
    engine = _Engine()
    session = Engine.__new__(Engine)  # any session object: the lease hands it on as it is
    lease = ExchangeLease(engine, session)  # type: ignore[arg-type]
    other = ExchangeLease(engine, session)  # type: ignore[arg-type]
    lease.acquire()
    other.acquire()
    assert lease.renew() and lease.held
    lease.reacquire()  # the Live Reviewer's decision applied: the handle reacquire returns is the lease's now
    renewer = asyncio.get_running_loop().create_future()
    lease.own_renewer(renewer)
    lease.release()
    lease.release()
    assert not lease.renew() and not lease.held and lease.released  # its renewer's next turn renews nothing: it ends
    lease.cancel_renewer()
    assert renewer.cancelled()
    assert [c for c in engine.calls if c[0] == "release"] == [("release", "handle-3")]  # its own, never handle-1/2
    other.release()
    assert [c for c in engine.calls if c[0] == "release"] == [("release", "handle-3"), ("release", "handle-2")]
    assert [c[0] for c in engine.calls].count("renew") == 1  # none after its release
    # The executor ends after the request was given up: what it takes then is released at once, once.
    gone = ExchangeLease(engine, session)  # type: ignore[arg-type]
    gone.release()  # cancelled while masking: nothing held yet
    gone.adopt("masked-late")  # the masking's hold, taken as the executor ends
    gone.reacquire()  # nothing held: nothing taken
    gone.release()
    assert engine.calls[-1:] == [("release", "masked-late")]

    class Racing(_Engine):
        def reacquire(self, session: Any, held: object) -> object:
            given_up.release()  # cancelled while the Live Reviewer's decision applies
            return super().reacquire(session, held)

    racing = Racing()
    given_up = ExchangeLease(racing, session)  # type: ignore[arg-type]
    given_up.adopt("masked")
    given_up.reacquire()
    given_up.release()
    assert racing.calls == [("release", "masked"), ("reacquire", ("masked", "handle-1")), ("release", "handle-1")]

    class Taking(_Engine):
        def hold(self, session: Any) -> object:
            with pytest.raises(RuntimeError, match="holds once"):
                busy.adopt("adopted-meanwhile")  # a second taker while the hold is asked: refused, its hold released
            return super().hold(session)

    taking = Taking()
    busy = ExchangeLease(taking, session)  # type: ignore[arg-type]
    busy.acquire()
    with pytest.raises(RuntimeError, match="holds once"):
        busy.acquire()
    busy.release()
    assert taking.calls == [("release", "adopted-meanwhile"), ("hold", "handle-1"), ("release", "handle-1")]
    never = ExchangeLease(engine, session)  # type: ignore[arg-type]
    calls = len(engine.calls)
    never.release()
    assert len(engine.calls) == calls  # nothing held, nothing released
    # Handed to the response's owner: the old one releases nothing; the new one, once.
    moved = ExchangeLease(engine, session)  # type: ignore[arg-type]
    moved.acquire()
    owner = moved.transfer()
    moved.release()
    owner.release()
    owner.release()
    assert engine.calls[-2:] == [("hold", "handle-4"), ("release", "handle-4")]
    # On a real vault: two requests of one conversation, each lease takes back its own count, once.
    store, values = _people()
    vault = StoredVault(ROOT_KEY, store, working=10, glued_all=True)  # pyright: ignore[reportArgumentType]
    real = _engine(vault)
    conversation = real.session("conversation")
    conversation.entity_ids.update(e.id for e in values[:4])
    first, second = ExchangeLease(real, conversation), ExchangeLease(real, conversation)
    await asyncio.to_thread(first.acquire)  # in the thread that masks, as ``_mask_slots`` holds
    await asyncio.to_thread(second.acquire)
    assert vault._sessions["conversation"].count == 2
    first.release()
    first.release()
    assert vault._sessions["conversation"].count == 1  # the second request's hold kept
    second.release()
    assert "conversation" not in vault._sessions


# --- refusals before an arena, printing, the mint's privacy --------------------------------------------------------


def test_a_request_refused_before_it_has_an_arena_is_a_block_that_sends_nothing(turns, monkeypatch):
    """``_prepared``'s two refusals: masking that failed (400) and a body that would decode too large (413)."""
    mediator = _mediator()

    def failing(*args: Any, **kwargs: Any) -> Prepared:
        raise MaskingError("the vault is full")

    monkeypatch.setattr(mediator, "prepare", failing)
    provider = _Provider()
    body = _chat(f"write to {EMAIL}")
    with _gateway(provider, mediator) as client:
        answered = client.post(CHAT, content=body, headers=HEADERS)
    assert answered.status_code == 400 and provider.sent == []
    block = turns[-1].decision
    assert type(block) is Block and (block.code, block.status) == ("masking-failed", 400)
    assert block.reason == "masking failed: the vault is full" and block.reason in answered.json()["error"]["message"]
    sent: list[bytes | str] = []
    assert _refused(block, None, _envelope(body), sent) == "block"
    mediator = _mediator()
    mediator.body_limit = 1024  # a server's max_request_mb, small
    inflated = gzip.compress(_chat("x" * 20_000))
    with _gateway(provider, mediator) as client:
        answered = client.post(CHAT, content=inflated, headers={**HEADERS, "content-encoding": "gzip"})
    assert answered.status_code == 413 and provider.sent == []
    block = turns[-1].decision
    assert type(block) is Block and (block.code, block.status) == ("too-large", 413)
    assert block.reason in answered.json()["error"]["message"]
    assert _refused(block, None, _envelope(inflated), sent) == "block" and sent == []


def test_no_value_prints_what_the_request_carried():
    mediator = _mediator()
    body = _chat(f"write to {EMAIL}")
    session_id, tool = "rc-session-4f6c1a52", "crm_lookup_internal"  # a session header's value, a tool that ran outside
    arena = mediator.prepare(OPENAI, "POST", CHAT, {**HEADERS, "x-claude-code-session-id": session_id}, body)
    assert arena.session is not None and session_id in arena.session.key
    arena.outbound.append(tool)  # as ``Mediator._mark_outbound`` records a provider's own tool call
    envelope = _envelope(body)
    decision = _strict(arena, envelope)
    assert type(decision) is Forward
    assert decision.response.session == arena.session.key and tuple(arena.outbound) == (tool,)
    [fake] = [t for t in decision.texts]
    final = FinalEnvelope("POST", b"https://api.openai.com/v1/chat/completions", envelope.headers, arena.body)
    forward_ = Forward(envelope, body, (), _proof(envelope), request_policy.current(), ResponsePlan())
    printed = [
        repr(envelope),
        repr(decision),
        repr(forward_),
        repr(final),
        repr(outcome.observe("final-check", arena)),
        repr(outcome._MINTED[decision]),
        repr(arena.sealed),
    ]
    for text in printed:
        assert EMAIL not in text and fake not in text and "sk-test" not in text and "Bearer" not in text, text
        assert "chat/completions" not in text and "messages" not in text, text
        assert session_id not in text and tool not in text, text
    assert "openai" in repr(envelope) and "Forward" in repr(decision)  # what it is, still said


def test_nothing_outside_outcome_mints():
    """RC-REC-8: the factory and its registry are named in ``gateway/outcome.py`` alone."""
    found = []
    for path in SRC.rglob("*.py"):
        if path == SRC / "gateway" / "outcome.py":
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in ("_mint", "_MINTED", "_Binding", "_Seal"):
                found.append(f"{path.relative_to(SRC)}:{getattr(node, 'lineno', 0)}")
            if isinstance(node, ast.ImportFrom) and node.module == "latet.gateway.outcome":
                found += [f"{path.relative_to(SRC)}:{node.lineno}" for a in node.names if a.name.startswith("_")]
    assert found == []


# --- a WebSocket's frames ---------------------------------------------------------------------------------------------


class _Upstream:
    """A WebSocket upstream on 127.0.0.1 that keeps every frame it is sent and answers each with a turn's end."""

    def __init__(self) -> None:
        self.got: list[str | bytes] = []
        self.server = serve(self._answer, "127.0.0.1", 0, open_timeout=1, close_timeout=1)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        print(f"P24_PORT_OPEN {self.port}")

    def _answer(self, conn: Any) -> None:
        done = json.dumps({"type": "response.completed", "response": {"status": "completed"}})
        try:
            for message in conn:
                self.got.append(message)
                conn.send(done)
        except ConnectionClosed:
            pass

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        print(f"P24_PORT_CLOSED {self.port}")


class _Client:
    """The client's side of a WebSocket the relay serves: what it sends, queued; what it is sent, kept."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.sent: list[dict[str, Any]] = []
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1",
            "server": ("127.0.0.1", 5283), "client": ("127.0.0.1", 50000), "root_path": "",
            "path": "/relay/v1/responses", "raw_path": b"/relay/v1/responses", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:5283"), (b"session-id", b"5f4a3e00-0000-4000-8000-00000000f4a3")], "subprotocols": [],
        }  # fmt: skip
        self.socket = WebSocket(scope, self._receive, self._send)

    async def _receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def _send(self, message: Any) -> None:
        self.sent.append(message)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent if m["type"] == "websocket.send" and "text" in m]


async def _until(predicate: Callable[[], bool], seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not in time")


@pytest.mark.parametrize("on_server", [False, True], ids=["a computer", "a server"])
async def test_a_websocket_frame_is_finalized_once_after_it_is_written_anew(on_server, monkeypatch):
    """Each frame's decision, once, as the frame leaves: a masked turn the Live Reviewer amended, finalized after it
    was written anew (its payload is what the upstream received, the original and your mask's value absent); a binary
    frame refused on a server and passed as it came on a computer; a contentless frame passed as it came."""
    mediator = _mediator()
    mediator.on_server = on_server
    decided: list[tuple[Prepared | None, Envelope, Any]] = []
    real = ws.decide
    real_finalize = ws.finalize

    def kept(arena: Prepared | None, envelope: Envelope, **kwargs: Any) -> Any:
        decision = real(arena, envelope, **kwargs)
        decided.append((arena, envelope, decision))
        return decision

    async def approved(batch: Any, gone: Any, held: Any = None) -> Decision:
        return Decision(True, masks=[HumanMask(0, "Falcon", "person")])

    monkeypatch.setattr(ws, "decide", kept)

    def finalized(arena: Prepared, envelope: Envelope, *args: Any, **kwargs: Any) -> Any:
        decision = real_finalize(arena, envelope, *args, **kwargs)
        decided.append((arena, envelope, decision))
        return decision

    monkeypatch.setattr(ws, "finalize", finalized)
    monkeypatch.setattr(mediator, "live_review", LiveReview(enabled=True))
    monkeypatch.setattr(mediator, "await_decision", approved)
    upstream, client = _Upstream(), _Client()
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                f"http://127.0.0.1:{upstream.port}",
                envelope_expected=frozenset({("model", "m")}),
            )
        ]
    )
    turn = {
        "type": "response.create",
        "model": "m",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": f"write to {EMAIL} about Falcon"}]}],
    }
    came = json.dumps(turn)
    cancel = json.dumps({"type": "response.cancel"})
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), mediator))
        client.incoming.put_nowait({"type": "websocket.receive", "text": came})
        await _until(lambda: len(client.texts()) == 1)
        client.incoming.put_nowait({"type": "websocket.receive", "bytes": b"\x00binary"})
        await _until(lambda: len(client.texts()) == 2)
        client.incoming.put_nowait({"type": "websocket.receive", "text": cancel})
        await _until(lambda: len(client.texts()) == 3)
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relaying, 10)
    finally:
        upstream.close()
    [(no_arena, handshake, shook)] = [found for found in decided if found[1].transport == "ws-handshake"]
    assert type(shook) is Forward and no_arena is not None and shook.envelope_proof is not None
    decided = [found for found in decided if found[1].transport == "ws-frame"]
    assert len(decided) == 3  # one decision per frame
    (arena, envelope, masked), (_, _, binary), (none, _, contentless) = decided
    assert type(masked) is Forward and arena is not None and arena.sealed is not None
    assert envelope.transport == "ws-frame" and envelope.body == came and envelope.destination == "relay"
    assert masked.payload == upstream.got[0]  # what left is the decision's, written anew after your decision
    assert isinstance(masked.payload, str) and EMAIL not in masked.payload and "Falcon" not in masked.payload
    assert arena.masked_count == 2 and masked.response.restore
    assert outcome.sendable(masked, arena, envelope) is masked
    assert type(binary) is Block and binary.code == "refused" and binary.transport == "ws-frame"
    assert b"\x00binary" not in upstream.got and json.loads(client.texts()[1])["type"] == "error"
    assert upstream.got[1:] == [cancel]
    assert type(contentless) is Forward and none is not None and contentless.payload == cancel
    connections = {envelope.connection for _, envelope, _ in decided} | {handshake.connection}
    assert len(connections) == 1 and None not in connections  # the socket's own number, on each of its frames
