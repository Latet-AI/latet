"""P2-4 HTTP/CLI boundary RED and controls through real gateway/transport, fake providers only."""

from __future__ import annotations

import dataclasses
import os
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from latet.cli import main as cli
from latet.engine.pipeline import Scope
from latet.gateway import forward, outcome, request_policy, request_profiles, request_transport
from latet.gateway.coverage import EnvelopeProof, inventory
from latet.gateway.outcome import Attempted, Block, Forward, NotAttempted, Pass
from latet.gateway.request_transport import AuthorizedRequest, BuiltRequest, SendRefusal
from latet.gateway.sigv4 import AwsCredentials

from ..request_recorder import RecordingClient, RecordingTransport
from ..unit.test_request_outcomes import CHAT, EMAIL, HEADERS, OPENAI, _chat, _envelope, _mediator, _rule_pass
from ..unit.test_request_transport import HELLO, _Aws, _bedrock, _gateway, _invoke, _Provider, _route

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def contained_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("AWS_") or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def turns(monkeypatch):
    seen: list[Any] = []
    real = forward._Turn

    def made():
        turn = real()
        seen.append(turn)
        return turn

    monkeypatch.setattr(forward, "_Turn", made)
    return seen


def prepared_decision(*, rule=False, proof=True, falsely_off=False):
    mediator, body = _rule_pass(f"write to {EMAIL}") if rule else (_mediator(), _chat(f"write to {EMAIL}"))
    envelope = _envelope(body)
    admission = request_profiles.admit(inventory(envelope), OPENAI)
    arena = mediator.prepare_http(
        OPENAI, "POST", CHAT, HEADERS, body, admission.checked, len(admission.inventory.entries), ""
    )
    assert not arena.block_reason and arena.body_proof is not None
    if falsely_off:
        arena.off = True
        arena.body_ledger = None
        arena.body_proof = None
        arena.freeze_inputs()
    envelope_proof = EnvelopeProof(
        envelope,
        len(admission.inventory.entries),
        admission.profile.id,
        admission.treatments,
        admission.headers,
        OPENAI.target(CHAT, ""),
        admission.checked,
        admission.removed,
    )
    decision = outcome.finalize(
        arena,
        envelope,
        request_policy.current(),
        outcome.response_plan(arena),
        envelope_proof=envelope_proof if proof else None,
    )
    return mediator, arena, envelope, admission, decision


def release(arena):
    arena.close_receipt()
    if arena.lease is not None:
        arena.lease.release()


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
def test_complete_proofs_keep_masking_and_response_restoration(rule, turns):
    mediator, body = _rule_pass(f"write to {EMAIL}") if rule else (_mediator(), _chat(f"write to {EMAIL}"))
    gateway = _gateway(mediator)
    with gateway.client as client:
        response = client.post(CHAT, content=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == f"write to {EMAIL}"
    assert len(gateway.transport.sent) == 1 and EMAIL.encode() not in gateway.transport.sent[0].content
    assert type(turns[-1].decision) is (Pass if rule else Forward)
    assert turns[-1].decision.envelope_proof is not None


def test_no_unmediated_http_send(turns):
    gateway = _gateway(None)
    with gateway.client as client:
        response = client.post(CHAT, content=_chat(f"write to {EMAIL}"), headers=HEADERS)
    assert gateway.counts() == (0, 0), "absent mediator sent the raw request"
    assert response.status_code >= 400 and isinstance(turns[-1].execution, NotAttempted)


@pytest.mark.parametrize("defect", ["missing-envelope", "falsely-off", "no-arena"])
def test_transport_requires_actual_strict_proofs(defect):
    arena = None
    try:
        if defect == "no-arena":
            envelope = _envelope(_chat("hello"))
            decision = outcome.decide(None, envelope)
        else:
            _, arena, envelope, _, decision = prepared_decision(
                proof=defect != "missing-envelope", falsely_off=defect == "falsely-off"
            )
        authorization = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
        assert isinstance(authorization, SendRefusal), f"transport authorized {defect}"
    finally:
        if arena is not None:
            release(arena)


@pytest.mark.parametrize("defect", ["unminted", "foreign-envelope"])
def test_strict_proof_cannot_be_copied_or_used_for_another_request(defect):
    _, arena, envelope, _, decision = prepared_decision()
    try:
        if defect == "unminted":
            decision = dataclasses.replace(decision)
        else:
            envelope = _envelope(_chat("another request"))
        assert isinstance(
            request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1), SendRefusal
        )
    finally:
        release(arena)


def test_bodyless_profile_has_explicit_empty_body_authority(turns):
    provider = _Provider()
    gateway = _gateway(_mediator(), lambda request: provider._answer(200, {}, {"data": []}))
    with gateway.client as client:
        response = client.get("/openai/v1/models", headers={"authorization": "Bearer sk-test"})
    assert response.status_code == 200 and gateway.transport.sent[0].content == b""
    assert isinstance(turns[-1].decision, Forward), "empty body still uses blanket legacy instead of bounded proof"


@pytest.mark.parametrize("change", ["policy", "payload", "header", "target"])
async def test_actual_http_send_rechecks_freshness_and_built_bytes(change, monkeypatch):
    _, arena, envelope, admission, decision = prepared_decision()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    async with RecordingClient(transport) as client:
        try:
            authorized = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
            assert isinstance(authorized, AuthorizedRequest)
            built = request_transport.build(
                authorized, client, "POST", OPENAI.target(CHAT, ""), list(admission.headers)
            )
            assert isinstance(built, BuiltRequest)
            if change == "policy":
                monkeypatch.setattr(request_policy, "BUILT_IN_POLICY", request_policy.PolicySnapshot(()))
            elif change == "payload":
                built.request.stream = httpx.ByteStream(b"changed")
            elif change == "header":
                built.request.headers["x-p24-added"] = "unproven"
            else:
                built.request.url = httpx.URL("http://127.0.0.1:1/v1/other")
            response = await request_transport.send(client, built)
            assert isinstance(response, SendRefusal) and transport.sent == [], "stale/changed request was invoked"
        finally:
            release(arena)


@pytest.mark.parametrize("change", ["cookie", "default-header", "normalized-target", "duplicate-header"])
async def test_http_final_envelope_controls(change):
    _, arena, envelope, admission, decision = prepared_decision()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    async with RecordingClient(transport) as client:
        try:
            authorized = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
            assert isinstance(authorized, AuthorizedRequest)
            headers, url = list(admission.headers), OPENAI.target(CHAT, "")
            if change == "cookie":
                client.cookies.set("ambient", "p24-canary")
            elif change == "default-header":
                client.headers["x-p24-default"] = "p24-canary"
            elif change == "normalized-target":
                url += "/../unproven"
            else:
                headers.append(headers[-1])
            built = request_transport.build(authorized, client, "POST", url, headers)
            assert isinstance(built, SendRefusal) and transport.sent == []
        finally:
            release(arena)


async def test_final_boundary_uses_authorized_frozen_payload_without_rewalking_body(monkeypatch):
    from latet.gateway.body_coverage import BodyProof

    mediator, arena, envelope, admission, decision = prepared_decision()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    async with RecordingClient(transport) as client:
        try:
            authorized = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
            assert isinstance(authorized, AuthorizedRequest)
            built = request_transport.build(
                authorized, client, "POST", OPENAI.target(CHAT, ""), list(admission.headers)
            )
            assert isinstance(built, BuiltRequest)

            def traversal(*args, **kwargs):
                raise AssertionError("final boundary repeated a body traversal")

            monkeypatch.setattr(outcome, "_unchanged", traversal)
            monkeypatch.setattr(BodyProof, "proves", traversal)
            response = await request_transport.send(client, built)
            assert isinstance(response, httpx.Response)
            assert client.sends == 1 and transport.sent[0].content == decision.payload
            assert EMAIL.encode() not in transport.sent[0].content
        finally:
            release(arena)
            mediator.close()


def test_attempt_is_recorded_before_transport_invocation(turns, monkeypatch):
    seen: list[Any] = []
    real_send = RecordingClient.send

    async def observed(client, request, **kwargs):
        seen.append(turns[-1].execution)
        return await real_send(client, request, **kwargs)

    monkeypatch.setattr(RecordingClient, "send", observed)
    gateway = _gateway(_mediator())
    with gateway.client as client:
        assert client.post(CHAT, content=_chat("hello"), headers=HEADERS).status_code == 200
    assert seen == [Attempted(1, None)], "transport was invoked before attempted fact existed"
    assert turns[-1].execution == Attempted(1, 200)


@pytest.mark.parametrize("change", ["credential-failure", "policy-revocation", "local-rules"])
def test_aws_retry_reuses_payload_but_revalidates_policy_and_signature(change, turns, monkeypatch):
    mediator = _mediator()

    def read(number):
        if number == 2 and change == "policy-revocation":
            monkeypatch.setattr(request_policy, "BUILT_IN_POLICY", request_policy.PolicySnapshot(()))
        elif number == 2 and change == "local-rules":
            mediator.engine.rules_changed()

    aws = _Aws(on_read=read, fails=2 if change == "credential-failure" else None)
    gateway = _bedrock(mediator, aws)
    with gateway.client as client:
        response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert len(gateway.transport.sent) == 1, "retry sent under changed local authority"
    assert gateway.provider.answers[0].closed
    assert turns[-1].execution == Attempted(1, 403)
    words = response.text.lower()
    assert "nothing was sent" not in words, "retry refusal denied the already observed first attempt"
    assert "no further" in words and ("uncertain" in words or "may have" in words)


def test_successful_aws_retry_keeps_payload_and_fresh_signature(turns):
    gateway = _bedrock(_mediator(), _Aws())
    with gateway.client as client:
        response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert response.status_code == 200 and len(gateway.transport.sent) == 2
    first, second = gateway.transport.sent
    assert first.content == second.content == _invoke(HELLO)
    headers = [dict((k.lower(), v) for k, v in request.headers) for request in (first, second)]
    assert headers[0][b"authorization"] != headers[1][b"authorization"]
    assert gateway.provider.answers[0].closed and turns[-1].execution == Attempted(2, 200)


def test_local_policy_change_during_credentials_blocks_first_attempt(turns):
    mediator = _mediator()
    gateway = _bedrock(mediator, _Aws(on_read=lambda number: mediator.engine.rules_changed()))
    with gateway.client as client:
        response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert gateway.transport.sent == [], "first attempt used pre-await local policy"
    assert response.status_code >= 400 and isinstance(turns[-1].execution, NotAttempted)


def test_expired_signing_credentials_send_nothing(turns):
    class Expired(_Aws):
        async def profile(self, name):
            return AwsCredentials("AKIARCDUMMY", "rc-dummy-secret", "expired-p24", expires=1.0)

    gateway = _bedrock(_mediator(), Expired())
    with gateway.client as client:
        response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert gateway.transport.sent == [] and response.status_code >= 400
    assert isinstance(turns[-1].execution, NotAttempted)


@pytest.mark.parametrize("entry", ["cli", "direct"])
def test_passthrough_is_gone_and_its_hidden_flag_exits_with_a_clear_message(entry, monkeypatch):
    from latet import config

    startup: list[str] = []

    def forbidden():
        startup.append("data-dir")
        raise AssertionError("passthrough reached startup")

    monkeypatch.setattr(config, "data_dir", forbidden)
    if entry == "cli":
        result = CliRunner().invoke(cli.app, ["serve", "--passthrough"])
        assert startup == [] and result.exit_code == 2
        assert "passthrough" in result.output.lower() and "mask" in result.output.lower()
        assert "--passthrough" not in CliRunner().invoke(cli.app, ["serve", "--help"]).output
    else:
        with pytest.raises(SystemExit) as ended:
            cli.run_server(passthrough=True)
        assert startup == [] and ended.value.code == 2


def test_an_actual_off_scope_refuses_and_sends_nothing(turns, monkeypatch):
    """D5 (P2-6): a project where Latet is off refuses its requests; 0.11 sends none of them unread."""
    mediator = _mediator()
    scope = Scope("p24-off", "p24-off", mediator.engine.masker, enabled=False)
    monkeypatch.setattr(mediator.engine, "scope", lambda session: scope)
    gateway = _gateway(mediator)
    body = _chat(f"write to {EMAIL}")
    with gateway.client as client:
        refused = client.post(CHAT, content=body, headers=HEADERS)
        blocked = client.post(CHAT + "?uncovered=p24-off-canary", content=body, headers=HEADERS)
    assert refused.status_code == 400 and "Latet is off" in refused.text and EMAIL not in refused.text
    assert gateway.transport.sent == [] and blocked.status_code == 400
    assert all(isinstance(turn.decision, Block) for turn in turns)


def test_a_conversation_whose_project_was_turned_off_is_refused_from_then_on(turns, monkeypatch):
    mediator = _mediator()
    gateway = _gateway(mediator, upstreams=[OPENAI])
    headers = {**HEADERS, "session-id": "019a4c2e-7d1b-7c3e-9f2a-0000000000f0"}  # within its grammar, no row
    with gateway.client as client:
        original = client.post(CHAT, content=_chat(f"write to {EMAIL}"), headers=headers)
        assert original.status_code == 200 and EMAIL.encode() not in gateway.transport.sent[0].content
        old_masked = gateway.transport.sent[0].content
        scope = Scope("p24-now-off", "p24-now-off", mediator.engine.masker, enabled=False)
        monkeypatch.setattr(mediator.engine, "scope", lambda session: scope)
        refused = client.post(CHAT, content=old_masked, headers=headers)
    assert refused.status_code == 400 and "Latet is off" in refused.text and EMAIL not in refused.text
    assert len(gateway.transport.sent) == 1  # nothing more left once Latet was off there (D5)
    assert isinstance(turns[-1].decision, Block)


def test_tutorial_supported_post_uses_strict_protection(turns):
    from starlette.testclient import TestClient

    from latet.gateway.app import GatewayConfig, create_app
    from latet.tutorial.chapters import Turn
    from latet.tutorial.model import MODEL_ID, PretendModel, PretendUpstream

    model = PretendModel()
    model.expect([Turn.model_validate({"id": "p24", "text": "done"})], {})
    config = GatewayConfig(port=5283, upstreams=[PretendUpstream()], mediator=_mediator(), client=model.client)
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
        response = client.post(
            "/anthropic/v1/messages",
            json={"model": MODEL_ID, "max_tokens": 64, "messages": [{"role": "user", "content": f"write to {EMAIL}"}]},
            headers={"x-api-key": "p24-dummy", "anthropic-version": "2023-06-01"},
        )
    assert response.status_code == 200 and len(model.received) == 1
    assert EMAIL.encode() not in model.received[0].body
    assert isinstance(turns[-1].decision, Forward) and turns[-1].decision.envelope_proof is not None
