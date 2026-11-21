"""A request member no request profile declares, through the gateway (notes/unknown-members-design.md §1-§3): read whole
and checked by the final check as masking reads a person's text, sent as it came when it holds nothing sensitive, on a
request Latet only checks (Claude Code's) and on one it rewrites (Codex over HTTP and in a WebSocket's
``response.create``, OpenAI's Chat Completions, Gemini), the known texts beside it masked as before. A value in it stops
the request, a value the vault learnt included, unless the very text left before in the conversation. Under
``unknown_members: refuse`` (the settings', a project's, never a request's) every such request is refused as before the
rule, and decided under the policy that says so; a member's name never reaches the history or a log line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from latet.config import Config, FieldRule, IdentityConfig, ProjectEntry
from latet.engine.build import build
from latet.engine.detectors.ner import NerDetector
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.engine.verifier import Leak
from latet.gateway import mediate, new_members, outcome, request_policy, request_transport
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.body_coverage import NEW_MEMBER_RULE, PROTECTED_LAUNCH
from latet.gateway.mediate import REWRITE, VERIFY, Mediator, Prepared
from latet.gateway.recorder import Recorder
from latet.gateway.request_audit import RequestAudit
from latet.gateway.request_policy import CHECK_POLICY, REFUSE_POLICY
from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream
from latet.storage.exchanges import ExchangeStore, safe_summary

from ..conftest import ROOT_KEY
from .test_ner import PERSON, Spans
from .test_request_leaks import _Answer
from .test_what_leaves import EMAIL, STOPPED

_TIMEOUT = 5
ANTHROPIC = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")
GEMINI = Upstream("gemini", "/gemini", "http://127.0.0.1:1", refuses_unread=True)
CHATGPT = next(route for route in DEFAULT_UPSTREAMS if route.name == "chatgpt")
SESSION = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
CLAUDE_CODE = {
    "authorization": "Bearer sk-ant-oat01-rc",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.325 (external, cli)",
    "x-claude-code-session-id": SESSION,
    "x-app": "cli",
}
CODEX = {
    "authorization": "Bearer eyJ-rc",
    "content-type": "application/json",
    "originator": "codex_cli_rs",
    "session-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f",
    "user-agent": "codex_cli_rs/0.162.0 (Windows 10.0.19045; x86_64) WindowsTerminal",
}
GEMINI_KEY = {"x-goog-api-key": "AIzaSyFAKE"}
GEMINI_CALL = "/gemini/v1beta/models/gemini-3-pro:generateContent"
_ANSWER = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'

# What a new agent version adds: a mode, a level, a flag, under a name no profile declares.
CONTROL = {"mode": "compact", "level": 2, "on": True}
NAME = "Zorbak Quillon"  # a person no detector finds alone: only the vault, once it learnt it, or the name model
PROSE = {"note": f"ask {NAME} before merging"}
LAUNCH_KEY = "latet_lk_" + "A" * 43


def _mediator(*, mode: str = "check", names: tuple[str, ...] = (), fields: tuple[FieldRule, ...] = ()) -> Mediator:
    """A computer's mediator, the name model a stand-in that finds ``names`` (none: off), new members read as ``mode``
    says (what the settings in use set: ``Runtime.apply_config``), with your field rules ``fields``."""
    extra = [NerDetector(Spans(*((name, PERSON) for name in names)))] if names else []
    config = Config(ner="off", identity=IdentityConfig(enabled=False), fields=list(fields))
    built = build(config, Vault(ROOT_KEY), extra=extra)
    mediator = Mediator(built.engine, built.verifier)
    mediator.unknown_members = mode  # type: ignore[assignment]
    return mediator


# --- the requests -----------------------------------------------------------------------------------------------------


def claude_code(member: Any = None, text: str = "write the release notes") -> bytes:
    """Claude Code 2.1.325's 21:47 shape: a member no profile declares in a message's content block."""
    block = {"type": "text", "text": text} | ({"x_render": member} if member is not None else {})
    return json.dumps(
        {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": [block]}]}
    ).encode()


def codex(member: Any = None, text: str = f"write to {EMAIL}") -> dict[str, Any]:
    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    doc: dict[str, Any] = {
        "model": "gpt-5.2-codex", "instructions": "You are Codex.", "input": [item], "stream": True, "store": False,
    }  # fmt: skip
    return doc | ({"x_render": member} if member is not None else {})


def chat(member: Any = None, text: str = f"write to {EMAIL}") -> bytes:
    message = {"role": "user", "content": text} | ({"x_render": member} if member is not None else {})
    return json.dumps({"model": "gpt-4.1-mini", "messages": [message]}).encode()


def gemini(member: Any = None, text: str = f"write to {EMAIL}") -> bytes:
    part = {"text": text} | ({"x_render": member} if member is not None else {})
    return json.dumps({"contents": [{"role": "user", "parts": [part]}]}).encode()


def frame(member: Any = None) -> str:
    doc = codex(member)
    del doc["instructions"], doc["stream"]
    return json.dumps({"type": "response.create"} | doc)


def _prepared(mediator: Mediator, agent: str, member: Any = None) -> tuple[Prepared, str]:
    """The request of ``agent`` carrying ``member``, prepared, with the text that leaves."""
    if agent == "claude-code":
        prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, claude_code(member))
    elif agent == "codex-http":
        raw = json.dumps(codex(member)).encode()
        prepared = mediator.prepare(CHATGPT, "POST", "/backend-api/codex/responses", CODEX, raw)
    elif agent == "chat":
        prepared = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", {}, chat(member))
    elif agent == "gemini":
        prepared = mediator.prepare(GEMINI, "POST", GEMINI_CALL, GEMINI_KEY, gemini(member))
    else:
        session = mediator.engine.session("session-id:ws-1")
        sent, found = mediator.mask_ws_frame(frame(member), session, CHATGPT, "/backend-api/codex/responses")
        assert found is not None
        return found, sent
    return prepared, prepared.body.decode()


AGENTS = ["claude-code", "codex-http", "codex-ws", "chat", "gemini"]
# How each is refused under `refuse`: today's words, today's code (a frame takes no exception: F-2).
STRICT = {
    "claude-code": ("request body refused (unknown member in message content block)", "body-unknown-member"),
    "codex-http": ("request body refused (unknown member in top-level)", "body-unknown-member"),
    "codex-ws": ("request body refused (unknown member in top-level)", "body-unknown-member-no-exception"),
    "chat": ("request body refused (unknown member in message)", "body-unknown-member"),
    "gemini": ("request body refused (unknown member in message content block)", "body-unknown-member"),
}


def _member(sent: str) -> Any:
    """The member as it left: at the place each request carries it."""
    doc = json.loads(sent)
    if "messages" in doc and isinstance(doc["messages"][0]["content"], list):
        return doc["messages"][0]["content"][0].get("x_render")
    if "messages" in doc:
        return doc["messages"][0].get("x_render")
    if "contents" in doc:
        return doc["contents"][0]["parts"][0].get("x_render")
    return doc.get("x_render")


# --- the policy a decision is made under ------------------------------------------------------------------------------


def test_two_snapshots_one_for_each_way_of_reading_new_members():
    assert request_policy.REVISION == "phase-2-policy-2"
    assert request_policy.PASS_CATEGORIES[NEW_MEMBER_RULE] == "new-member" and "new-member" in request_policy.PASSES
    assert NEW_MEMBER_RULE in request_policy.BUILT_IN
    assert (CHECK_POLICY.unknown_members, REFUSE_POLICY.unknown_members) == ("check", "refuse")
    assert CHECK_POLICY.allows(NEW_MEMBER_RULE) and not REFUSE_POLICY.allows(NEW_MEMBER_RULE)
    assert set(REFUSE_POLICY.rules) == request_policy.BUILT_IN - {NEW_MEMBER_RULE}
    assert request_policy.current() is CHECK_POLICY and request_policy.current("check") is CHECK_POLICY
    assert request_policy.current("refuse") is REFUSE_POLICY
    with pytest.raises(ValueError, match="not a way to read unknown members"):
        request_policy.current("off")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a way to read unknown members"):
        request_policy.PolicySnapshot(unknown_members="off")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="never names the rule that reads them"):
        request_policy.PolicySnapshot(unknown_members="refuse")
    # Only the snapshots this version has are in force, each its own mode's: never one equal to them.
    assert request_policy.is_current(CHECK_POLICY) and request_policy.is_current(REFUSE_POLICY)
    assert not request_policy.is_current(request_policy.PolicySnapshot())
    assert not request_policy.is_current(object())


# --- a control member passes, the known texts masked as before ----------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_a_control_member_passes_read_by_the_new_rule(agent):
    mediator = _mediator()
    prepared, sent = _prepared(mediator, agent, CONTROL)
    assert prepared.block_reason is None, prepared.block_reason
    assert prepared.mode == (VERIFY if agent == "claude-code" else REWRITE)
    assert NEW_MEMBER_RULE in prepared.body_rules and prepared.unknown_members == "check"
    assert _member(sent) == CONTROL  # never masked, never rewritten
    if agent != "claude-code":  # the known texts beside it masked as before
        assert EMAIL not in sent
    prepared.freeze_inputs()
    assert prepared.body_proof is not None and NEW_MEMBER_RULE in prepared.body_proof.rules


@pytest.mark.parametrize("agent", AGENTS)
def test_strict_a_control_member_is_refused_as_before_the_rule(agent):
    prepared, _ = _prepared(_mediator(mode="refuse"), agent, CONTROL)
    assert (prepared.block_reason, prepared.envelope_refusal) == STRICT[agent]
    assert prepared.unknown_members == "refuse" and NEW_MEMBER_RULE not in prepared.body_rules


@pytest.mark.parametrize("agent", AGENTS)
def test_a_request_without_a_new_member_is_the_same_in_both_modes(agent):
    found = []
    for mode in ("check", "refuse"):
        prepared, _ = _prepared(_mediator(mode=mode), agent)
        assert prepared.block_reason is None and NEW_MEMBER_RULE not in prepared.body_rules
        found.append((prepared.mode, prepared.body_rules, prepared.unknown_members))
    assert found[0][:2] == found[1][:2] and [mode for *_, mode in found] == ["check", "refuse"]


# --- a value in it stops the request ----------------------------------------------------------------------------------


def test_a_name_in_prose_in_a_claude_code_member_is_refused():
    """On a request Latet only checks, a member the new rule reads holding a person's name in prose: the final check
    reads it as a person's text, the name model included, and stops it, where the same member holding a mode passes."""
    mediator = _mediator(names=(NAME,))
    refused, _ = _prepared(mediator, "claude-code", PROSE)
    assert refused.block_reason == f"{STOPPED}PERSON[{len(NAME)}]"
    assert [leak.kind for leak in refused.leaks] == [SlotKind.NEW_MEMBER]
    passed, _ = _prepared(mediator, "claude-code", CONTROL)
    assert passed.block_reason is None


def test_strict_a_name_in_prose_in_a_claude_code_member_is_refused_as_before():
    refused, _ = _prepared(_mediator(mode="refuse", names=(NAME,)), "claude-code", PROSE)
    assert refused.block_reason == STRICT["claude-code"][0] and refused.leaks == []


@pytest.mark.parametrize("agent", ["codex-http", "codex-ws", "chat", "gemini"])
def test_a_value_the_vault_learnt_in_a_rewritten_requests_member_is_refused(agent):
    """The final check's lookup of the vault's values reads a new member's texts, which masking never looked up: a value
    learnt earlier (by the name model, here put in the vault directly), in real form in a member, stops the request."""
    mediator = _mediator()
    clean, _ = _prepared(mediator, agent, PROSE)
    assert clean.block_reason is None  # nothing finds the name alone
    mediator.engine.vault.intern("PERSON", EntityClass.PII, NAME)
    refused, _ = _prepared(mediator, agent, PROSE)
    assert refused.block_reason == f"{STOPPED}PERSON[{len(NAME)}]"


@pytest.mark.parametrize("agent", ["codex-http", "codex-ws", "chat", "gemini"])
def test_a_launch_key_in_a_rewritten_requests_new_member_is_refused_as_a_protected_launch(agent):
    prepared, sent = _prepared(_mediator(), agent, {"note": "key " + LAUNCH_KEY})
    assert prepared.block_reason == PROTECTED_LAUNCH and LAUNCH_KEY not in sent


def test_a_launch_key_split_across_a_flat_list_of_the_copy_read_is_refused():
    """A rewritten request's top-level list of strings, the copy the walk reads (no key given twice): the launch-key
    screen reads its strings one by one (D6's string case in its declarations), the new rule joined, and the joined text
    is screened too (``safe_text`` over the new members): refused as a protected launch, nothing of it sent."""
    doc = json.loads(chat(text="hello"))
    doc["x_parts"] = [LAUNCH_KEY[:29], LAUNCH_KEY[29:]]
    prepared = _mediator().prepare(OPENAI, "POST", "/openai/v1/chat/completions", {}, json.dumps(doc).encode())
    assert prepared.block_reason == PROTECTED_LAUNCH and LAUNCH_KEY.encode() not in prepared.body


# --- your field rules read it -----------------------------------------------------------------------------------------

# A field rule, and a member holding a value it names, where the member puts it: by its key, by its path from the
# member's own key, by an array's path inside it. Each value is ten characters.
FIELD_MEMBERS = [
    (FieldRule(name="tier", type="TIER"), {"tier": "platinumzx", "customer_code": "ACME-99812"}),  # the review's probe
    (FieldRule(name="x_render.tier", type="TIER"), {"tier": "platinumzx"}),
    (FieldRule(name="items[].sku", type="TIER"), {"items": [{"sku": "AB-0099812"}]}),
]


@pytest.mark.parametrize(("rule", "member"), FIELD_MEMBERS, ids=["key", "path", "array path"])
@pytest.mark.parametrize("agent", ["claude-code", "codex-http"])
def test_a_field_rule_stops_a_value_of_production_data_in_a_new_member(agent, rule, member):
    """A value your field rule names, in a member no profile declares: the final check reads the member's values as
    their fields, as masking reads a JSON document's, and stops the request, on a request Latet only checks and on one
    it rewrites; a member of control values beside the same rule passes; under `refuse`, refused as before the rule."""
    mediator = _mediator(fields=(rule,))
    refused, _ = _prepared(mediator, agent, member)
    assert refused.block_reason == f"{STOPPED}TIER[10]"
    assert refused.leaks and {leak.kind for leak in refused.leaks} == {SlotKind.NEW_MEMBER}
    passed, sent = _prepared(mediator, agent, CONTROL)
    assert passed.block_reason is None and _member(sent) == CONTROL
    strict, _ = _prepared(_mediator(mode="refuse", fields=(rule,)), agent, member)
    assert (strict.block_reason, strict.envelope_refusal) == STRICT[agent]


@pytest.mark.parametrize("agent", ["claude-code", "codex-http"])
def test_the_design_s_example_of_a_member_s_shape_stops_the_request(agent):
    """notes/unknown-members-design.md §1, step 5: a STRUCT value stops it where a field rule reads the member's shape,
    ``{"tier": "platinumzx"}``; the same request with no such rule passes."""
    rule = FieldRule(name="tier", type="TIER")
    refused, _ = _prepared(_mediator(fields=(rule,)), agent, {"tier": "platinumzx"})
    assert refused.block_reason == f"{STOPPED}TIER[10]"
    assert {(leak.etype, leak.eclass) for leak in refused.leaks} == {("TIER", EntityClass.STRUCT)}
    passed, _ = _prepared(_mediator(), agent, {"tier": "platinumzx"})
    assert passed.block_reason is None


# --- what Latet reads of one request's new members --------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["claude-code", "codex-http"])
def test_new_members_the_name_model_cannot_read_in_time_refuse_the_request_as_before(agent, monkeypatch):
    """The name model reads a request's new members in the time masking gives a document (``DOCUMENT_READ_SECONDS``):
    past it, the request is refused as before the rule (it fails closed), nothing of it sent."""
    monkeypatch.setattr(mediate, "DOCUMENT_READ_SECONDS", -1.0)
    prepared, _ = _prepared(_mediator(names=(NAME,)), agent, CONTROL)
    assert (prepared.block_reason, prepared.envelope_refusal) == STRICT[agent]
    assert prepared.body in (b"", "")


@pytest.mark.timing
def test_a_request_s_many_new_member_texts_are_read_by_the_name_model_at_once():
    """Claude Code's request, a member of a thousand distinct values beside its text: the final check, the one reader of
    the name model there, reads them in one run of it, none alone, well under a second."""
    from .test_verifier_new_members import _Counted

    model = _Counted((NAME, PERSON))
    built = build(
        Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY), extra=[NerDetector(model)]
    )
    mediator = Mediator(built.engine, built.verifier)
    member = {"values": [f"mode {index}" for index in range(1000)]}
    started = time.monotonic()
    prepared, _ = _prepared(mediator, "claude-code", member)
    assert time.monotonic() - started < 1
    assert prepared.block_reason is None and NEW_MEMBER_RULE in prepared.body_rules
    assert (model.runs, model.alone) == (1, 0)
    refused, _ = _prepared(mediator, "claude-code", {**member, "note": f"ask {NAME}"})
    assert refused.block_reason == f"{STOPPED}PERSON[{len(NAME)}]" and (model.runs, model.alone) == (2, 0)


@pytest.mark.parametrize("agent", ["claude-code", "codex-http"])
def test_new_members_past_what_latet_reads_of_a_request_refuse_it_as_before(agent):
    over = {"values": [f"v{index}" for index in range(new_members.TEXTS // 4 + 1)]}
    prepared, _ = _prepared(_mediator(), agent, over)
    assert (prepared.block_reason, prepared.envelope_refusal) == STRICT[agent]


# --- a text that left passes again in its conversation ----------------------------------------------------------------


def test_a_claude_code_member_sent_again_after_the_vault_learnt_its_value_passes_as_sent_before():
    mediator = _mediator()
    first, _ = _prepared(mediator, "claude-code", PROSE)
    assert first.block_reason is None  # recorded as sent in its conversation (``keep_checked``)
    mediator.engine.vault.intern("PERSON", EntityClass.PII, NAME)
    again, _ = _prepared(mediator, "claude-code", PROSE)
    assert again.block_reason is None and "sent before in this conversation" in again.body_rules
    other = {**CLAUDE_CODE, "x-claude-code-session-id": "9a1b2c3d-0d1e-4b8e-9a37-2c5e8d0b7a11"}
    elsewhere = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", other, claude_code(PROSE))
    assert elsewhere.block_reason == f"{STOPPED}PERSON[{len(NAME)}]"


@pytest.mark.parametrize("agent", ["codex-http", "chat", "gemini"])
def test_a_rewritten_member_sent_again_after_the_vault_learnt_its_value_passes_as_sent_before(agent):
    """What a rewritten request carried as it left is recorded in its conversation once it reached its provider
    (``left``, which ``record_left`` does in the engine's thread): its new members' texts among them."""
    mediator = _mediator()
    first, _ = _prepared(mediator, agent, PROSE)
    assert first.block_reason is None
    assert first.body_new_members and any(NAME in text for text in first.body_new_members)
    assert first.body_new_member_fields and all(NAME in text for text in first.body_new_member_fields)
    mediator.record_left(first)
    mediator.engine.vault.intern("PERSON", EntityClass.PII, NAME)
    again, _ = _prepared(mediator, agent, PROSE)
    assert again.block_reason is None and "sent before in this conversation" in again.body_rules


def test_a_rewritten_member_never_recorded_as_left_stops_once_the_vault_learnt_its_value():
    mediator = _mediator()
    first, _ = _prepared(mediator, "chat", PROSE)
    assert first.block_reason is None  # prepared, never sent: not recorded as left
    mediator.engine.vault.intern("PERSON", EntityClass.PII, NAME)
    again, _ = _prepared(mediator, "chat", PROSE)
    assert again.block_reason == f"{STOPPED}PERSON[{len(NAME)}]"


def _decided(arena: Prepared, body: bytes, policy: request_policy.PolicySnapshot) -> Any:
    """``arena``'s decision under ``policy``, its envelope proven as the forwarder proves it."""
    from latet.gateway import outcome, request_profiles
    from latet.gateway.coverage import EnvelopeProof, inventory

    from .test_request_outcomes import OPENAI as ROUTE
    from .test_request_outcomes import _envelope

    envelope = _envelope(body)
    admission = request_profiles.admit(inventory(envelope), ROUTE)
    proof = EnvelopeProof(
        envelope, len(admission.inventory.entries), admission.profile.id, admission.treatments, admission.headers,
        ROUTE.target(envelope.raw_path.decode(), envelope.query.decode()), admission.checked, admission.removed,
    )  # fmt: skip
    return outcome.finalize(arena, envelope, policy, outcome.response_plan(arena), envelope_proof=proof), envelope


def test_a_decision_is_made_under_the_snapshot_of_the_way_its_arena_read_new_members():
    """``outcome.finalize`` takes the snapshot of its arena's way of reading new members alone (``policy_for``); one
    sent under it is refused once that way moved after its decision (its seal holds it)."""
    from latet.gateway import outcome

    from .test_request_outcomes import CHAT as ROUTE_CHAT
    from .test_request_outcomes import HEADERS
    from .test_request_outcomes import OPENAI as ROUTE
    from .test_request_outcomes import _chat as outcome_chat

    mediator = _mediator(mode="refuse")
    body = outcome_chat("hello")
    arena = mediator.prepare(ROUTE, "POST", ROUTE_CHAT, HEADERS, body)
    assert arena.block_reason is None and outcome.policy_for(arena) is REFUSE_POLICY
    with pytest.raises(ValueError, match="new members"):
        _decided(arena, body, CHECK_POLICY)
    decision, envelope = _decided(arena, body, outcome.policy_for(arena))
    assert decision.policy is REFUSE_POLICY and outcome.sendable(decision, arena, envelope) is decision
    arena.unknown_members = "check"
    with pytest.raises(outcome.NotSendable) as stale:
        outcome.sendable(decision, arena, envelope)
    assert stale.value.code == "stale"


# --- the mode: the settings', a project's, never a request's ----------------------------------------------------------


def test_switching_to_refuse_invalidates_a_body_verified_before():
    """A tightening applies at once: a body checked while new members were read is read again before it can leave (the
    content epoch moved), and read again it is refused."""
    mediator = _mediator()
    prepared, _ = _prepared(mediator, "claude-code", CONTROL)
    assert prepared.body_verified and prepared.block_reason is None
    mediator.unknown_members = "check"  # the same mode again moves nothing
    prepared.freeze_inputs()
    assert prepared.body_proof is not None
    mediator.unknown_members = "refuse"
    prepared.freeze_inputs()
    assert not prepared.body_verified and prepared.body_proof is None
    assert prepared.block_reason == "request values changed after the final check; send the request again"
    again, _ = _prepared(mediator, "claude-code", CONTROL)
    assert again.block_reason == STRICT["claude-code"][0]


@pytest.mark.parametrize("header", ["x-latet-unknown-members", "x-latet-project"])
def test_the_mode_is_never_taken_from_a_request(header):
    mediator = _mediator(mode="refuse")
    headers = {**CLAUDE_CODE, header: "check"}
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", headers, claude_code(CONTROL))
    assert prepared.block_reason == STRICT["claude-code"][0] and prepared.unknown_members == "refuse"
    loose = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", {header: "check"}, chat(CONTROL))
    assert loose.block_reason == STRICT["chat"][0]


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Any]:
    from latet.runtime import make_runtime

    made = []

    def make(**settings: Any) -> Any:
        config = Config(identity=IdentityConfig(enabled=False), ner="off", **settings)
        folder = tmp_path / f"rt-{len(made)}"
        made.append(make_runtime(config, data=folder, config_file=folder / "config.yaml"))
        return made[-1]

    yield make
    for rt in made:
        rt.close()


def test_the_runtime_gives_its_mediator_the_mode_of_the_settings_in_use(runtime):
    rt = runtime()
    assert rt.mediator.unknown_members == "check" == rt.unknown_members()
    prepared, _ = _prepared(rt.mediator, "chat", CONTROL)
    assert prepared.block_reason is None
    rt.apply_config(rt.config.model_copy(update={"unknown_members": "refuse"}))  # a tightening: at once
    assert rt.mediator.unknown_members == "refuse"
    prepared.freeze_inputs()
    assert prepared.body_proof is None and not prepared.body_verified
    refused, _ = _prepared(rt.mediator, "chat", CONTROL)
    assert refused.block_reason == STRICT["chat"][0]
    assert runtime(unknown_members="refuse").mediator.unknown_members == "refuse"


def test_a_project_that_refuses_new_members_refuses_them_in_its_requests(runtime, tmp_path):
    """A project of yours set to `refuse` (a repository's latet.yaml alike): its requests refuse new members, those of
    another folder read them."""
    shop, other = tmp_path / "shop", tmp_path / "other"
    shop.mkdir()
    other.mkdir()
    rt = runtime(projects=[ProjectEntry(path=str(shop), unknown_members="refuse")])
    mediator = rt.mediator
    found = {}
    for folder in (shop, other):
        prepared = mediator.prepare(
            OPENAI, "POST", "/openai/v1/chat/completions", {"x-latet-project": str(folder)}, chat(CONTROL, f"{folder}")
        )
        found[folder.name] = (prepared.block_reason, prepared.unknown_members)
    assert found == {"shop": (STRICT["chat"][0], "refuse"), "other": (None, "check")}


def test_a_final_check_run_again_after_a_move_to_refuse_refuses_the_member():
    """The final check runs again after a reviewer read the request (the local reviewer, or you with the Live Reviewer:
    ``review_prepared``, ``live_prepared``): a move to `refuse` since it was prepared refuses its new members there,
    and its decision is then made under that mode's rules, whatever the epoch it stamps."""
    mediator = _mediator()
    prepared, _ = _prepared(mediator, "chat", CONTROL)
    assert prepared.block_reason is None and prepared.unknown_members == "check"
    mediator.unknown_members = "refuse"
    mediator._check(prepared)  # as a reviewer's end calls it, on what leaves then
    prepared.freeze_inputs()
    assert prepared.block_reason == STRICT["chat"][0] and prepared.body_proof is None
    assert prepared.unknown_members == "refuse" and outcome.policy_for(prepared) is REFUSE_POLICY


def test_a_final_check_run_again_never_reads_new_members_a_request_refused():
    """A move to `check` after a request was prepared under `refuse` changes nothing of it: only a tightening."""
    mediator = _mediator(mode="refuse")
    prepared, _ = _prepared(mediator, "chat")
    assert prepared.block_reason is None and prepared.unknown_members == "refuse"
    mediator.unknown_members = "check"
    mediator._check(prepared)
    assert prepared.unknown_members == "refuse" and prepared.block_reason is None


def test_a_final_check_run_again_after_the_project_moved_to_refuse_refuses_the_member(runtime, tmp_path, monkeypatch):
    """Its project's latet.yaml set to `refuse` after the request was prepared (no epoch moves for a project): the
    final check run again reads the project's rules of now, and refuses the member."""
    from latet import projects

    monkeypatch.setattr(projects, "_RECHECK", 0.0)  # the file looked at again at once
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "latet.yaml").write_text("", encoding="utf-8")
    rt = runtime(projects=[ProjectEntry(path=str(repo))])
    headers = {"x-latet-project": str(repo)}
    prepared = rt.mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", headers, chat(CONTROL, "repo"))
    assert prepared.block_reason is None and prepared.unknown_members == "check"
    (repo / "latet.yaml").write_text("unknown_members: refuse\n", encoding="utf-8")
    rt.mediator._check(prepared)
    prepared.freeze_inputs()
    assert prepared.block_reason == STRICT["chat"][0] and prepared.body_proof is None
    assert prepared.unknown_members == "refuse"


def test_a_repository_file_that_refuses_new_members_refuses_them(runtime, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "latet.yaml").write_text("unknown_members: refuse\n", encoding="utf-8")
    rt = runtime(projects=[ProjectEntry(path=str(repo))])
    headers = {"x-latet-project": str(repo)}
    prepared = rt.mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", headers, chat(CONTROL, "repo"))
    assert (prepared.block_reason, prepared.unknown_members) == (STRICT["chat"][0], "refuse")


# --- through the production gateway: the decision, the history, the logs ---------------------------------------------


@contextlib.contextmanager
def _sinks(tmp_path: Path, mode: str) -> Iterator[tuple[Mediator, Recorder, ExchangeStore]]:
    mediator = _mediator(mode=mode)
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    recorder = Recorder(store, engine=mediator.engine, history="full")
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(mediator.bridge_executor.shutdown, wait=True)
        cleanup.callback(mediator.executor.shutdown, wait=True)
        cleanup.callback(mediator.close)
        cleanup.callback(store.close)
        cleanup.callback(recorder.close)
        yield mediator, recorder, store


@dataclass
class _Through:
    """What one request through the gateway gave: the answer, what reached the provider, the history's row and detail
    of it, the policies its decision was authorized under, and the request audit's entries of it."""

    response: httpx.Response
    sent: list[httpx.Request]
    row: dict[str, Any]
    detail: dict[str, Any]
    policies: list[request_policy.PolicySnapshot]
    audited: list[str]
    lost: int  # decisions the request audit could not record (``RequestAudit.overflow``)


async def _through(tmp_path: Path, monkeypatch, agent: str, body: bytes, mode: str) -> _Through:
    """One request of ``agent`` through the real local gateway, recorder, store and request audit."""
    upstream, path, headers, _ = CASES[agent]
    sent: list[httpx.Request] = []
    policies: list[request_policy.PolicySnapshot] = []
    validate = request_transport.validate_send

    def authorized(decision, arena, envelope, policy, attempt, **kwargs):
        policies.append(policy)
        return validate(decision, arena, envelope, policy, attempt, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", authorized)

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(_ANSWER))

    audit = RequestAudit()
    with _sinks(tmp_path, mode) as (mediator, recorder, store):
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token="new-members-bridge-token",
                fixed_routes=[upstream],
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
                request_audit=audit,
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            response = await asyncio.wait_for(client.post(path, headers=headers, content=body), _TIMEOUT)
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]
        detail = store.detail(row["id"])
    audited = [
        entry.payload.decode() if isinstance(entry.payload, bytes) else str(entry.payload)
        for entry in audit._events.values()
    ]
    lost = audit.overflow
    audit.close()
    assert detail is not None
    return _Through(response, sent, row, detail, policies, audited, lost)


CASES = {
    "claude-code": (ANTHROPIC, "/anthropic/v1/messages?beta=true", CLAUDE_CODE, claude_code),
    "chat": (OPENAI, "/openai/v1/chat/completions", {"content-type": "application/json"}, chat),
}


@pytest.mark.parametrize("agent", sorted(CASES))
async def test_a_control_member_is_forwarded_and_the_history_says_its_pass(tmp_path, monkeypatch, agent):
    made = CASES[agent][3]
    found = await _through(tmp_path, monkeypatch, agent, made(CONTROL), "check")
    assert found.response.status_code == 200, found.response.text
    assert len(found.sent) == 1 and _member(found.sent[0].content.decode()) == CONTROL
    assert "new-member" in found.row["passes"]
    assert found.row["unknown_members"] == "check"  # the mode that decided it (design §3), nothing more
    assert found.policies and all(policy is CHECK_POLICY for policy in found.policies)
    # The request audit records the Pass by its rules' codes (the new rule's is 9), never losing it.
    assert found.lost == 0 and any(json.loads(entry)[0] == 1 for entry in found.audited)


@pytest.mark.parametrize("agent", sorted(CASES))
async def test_strict_a_control_member_is_refused_and_a_clean_request_decided_under_refuse(
    tmp_path, monkeypatch, agent
):
    made = CASES[agent][3]
    found = await _through(tmp_path / "member", monkeypatch, agent, made(CONTROL), "refuse")
    assert found.response.status_code == 400 and found.sent == []
    assert found.row["block_reason"] == STRICT[agent][1] and "new-member" not in found.row["passes"]
    assert found.row["unknown_members"] == "refuse"
    clean = await _through(tmp_path / "clean", monkeypatch, agent, made(), "refuse")
    assert clean.response.status_code == 200 and len(clean.sent) == 1
    assert clean.row["unknown_members"] == "refuse"
    assert clean.policies and all(policy is REFUSE_POLICY for policy in clean.policies)


SECRET_MEMBERS = [
    {"backupSecret": "hunter2-alpha-77"},  # a secret by its key's name, read with it: ``assignment:backupSecret``
    {"settings": json.dumps({"zeta_password": "Zq9wLm3pT7xx"})},  # in a JSON text: ``assignment:zeta_password``
]


@pytest.mark.parametrize("member", SECRET_MEMBERS, ids=["key-value", "json-text"])
@pytest.mark.parametrize("agent", sorted(CASES))
async def test_a_member_s_name_never_reaches_the_history_the_audit_or_a_log_line(
    tmp_path, monkeypatch, caplog, agent, member
):
    """A secret under a key that names it stops the request (``key: value``), its rule naming the key
    (``assignment:<key>``, as ``field-secret:<key>`` names a field): the history, the request audit, the agent's words
    and the log say its type and length, never a member's name, the rule or the value."""
    caplog.set_level(logging.DEBUG)
    found = await _through(tmp_path, monkeypatch, agent, CASES[agent][3](member), "check")
    assert found.response.status_code == 400 and found.sent == []
    assert "sensitive values would leave in clear" in found.response.text
    assert found.audited  # the refusal was audited, by its codes
    kept = json.dumps([found.row, found.detail]) + found.response.text + caplog.text + "".join(found.audited)
    for word in ("x_render", "backupSecret", "zeta_password", "hunter2", "Zq9wLm3pT7xx", "assignment:"):
        assert word not in kept, word
    assert [leak["kind"] for leak in found.detail["leaks"]] == ["new_member"]


@pytest.mark.parametrize(("given", "kept"), [("check", "check"), ("refuse", "refuse"), ("off", None), (1, None)])
def test_the_history_keeps_the_mode_by_its_two_values_alone(given, kept):
    assert safe_summary({"unknown_members": given})["unknown_members"] == kept
    assert safe_summary({})["unknown_members"] is None  # a row recorded before 0.15.0


def test_a_new_member_s_leak_is_placed_in_no_block(tmp_path):
    """A new member stands in no block of the request: its leak names none, even where the blocks recorded outnumber
    the slots checked (a tool's input that ran outside this computer has one of its own), and so no rule of it is kept,
    which names the member (``assignment:<key>``)."""
    with _sinks(tmp_path, "check") as (_, recorder, _store):
        leak = Leak("ASSIGNED_SECRET", EntityClass.SECRET, "assignment:backupSecret", SlotKind.NEW_MEMBER,
                    "ASSIGNED_SECRET[16]", True, slot=0, start=0, end=16)  # fmt: skip
        assert recorder._place(leak, ["b-1"]) == {}
        known = Leak("EMAIL", EntityClass.PII, "pii:email", SlotKind.USER_TEXT, "EMAIL[21]", True, slot=0, start=3,
                     end=24)  # fmt: skip
        assert recorder._place(known, ["b-1"])["block"] == "b-1"
