"""Hermes Agent signed in with ChatGPT, as its source builds a request (hermes-agent 25ce7ccf), through the envelope's
admission and P2-2's body coverage: its headers and members are named by their own forms.

- ``session_id``: its session id, ``new_session_id()``: ``f"{now:%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"``
  (``hermes_state_ids.py:53-60``), sent as ChatGPT's header (``agent/transports/codex.py:864-872``).
- ``x-client-request-id`` and the body's ``prompt_cache_key``: ``_content_cache_key()``, ``"pck_" + sha256[:24]``
  (``codex.py:428-449``), else the bounded scope, which is the session id (``codex.py:31-55``, ``798-806``, ``869``).
- ``prompt_cache_retention``: ``"24h"`` (``codex.py:357-372``), one of OpenAI's documented ``in-memory`` and ``24h``.
- ``context_management``: ``[{"type": "compaction", "compact_threshold": N}]`` (``agent/native_compaction.py:3, 142``).

Issue 17 (hermes-agent 3944af0e, openai/codex 72532eca): a conversation the unified runtime creates has the session id
``local_identity()``, ``"local-" + sha256(...).hexdigest()`` (``hermes_state_keys.py:71-74``), sent as the header like
any other (``agent/transports/codex.py:871-873``); ``x-openai-internal-codex-residency`` is the ChatGPT token's
``chatgpt_data_residency`` claim (``agent/codex_headers.py:76-78``), the header Codex sends as ``us``
(``model-provider-info/src/lib.rs:48, 495``); a replayed assistant message keeps its ``phase``, Codex's
``MessagePhase`` (``protocol/src/models.rs:1053-1062``).
"""

from __future__ import annotations

import json

import pytest

from latet.gateway import request_profiles as rp
from latet.gateway.body_coverage import NEW_MEMBER_RULE, BodyRefused, Ledger, inspect_body
from latet.gateway.coverage import EnvelopeRefused, envelope_of, inventory, preflight_scope
from latet.gateway.routes import DEFAULT_UPSTREAMS
from latet.protocols.base import BodyScan

CHATGPT = next(route for route in DEFAULT_UPSTREAMS if route.name == "chatgpt")
SESSION = "20261103_143000_a1b2c3"
CACHE_KEY = "pck_" + "4f6c1a520d1e4b8e9a372c5e"
# The OpenAI Python SDK's own headers, as Hermes's client sends them (the SDK's telemetry is removable).
HEADERS = [
    ("host", "127.0.0.1:5283"),
    ("accept", "application/json"),
    ("content-type", "application/json"),
    ("user-agent", "OpenAI/Python 2.8.1"),
    ("x-stainless-lang", "python"),
    ("authorization", "Bearer fake-chatgpt-token"),
    ("session_id", SESSION),
    ("x-client-request-id", CACHE_KEY),
]


def hermes_body(**fields) -> dict:
    text = {"type": "input_text", "text": "write the release notes"}
    return {
        "model": "gpt-5.1-codex",
        "instructions": "You are Hermes.",
        "input": [{"type": "message", "role": "user", "content": [text]}],
        "stream": True,
        "store": False,
        "prompt_cache_key": CACHE_KEY,
        "prompt_cache_retention": "24h",
        "context_management": [{"type": "compaction", "compact_threshold": 180000}],
        **fields,
    }


def _admitted(headers: list[tuple[str, str]]):
    body = json.dumps(hermes_body()).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/backend-api/codex/responses",
        "raw_path": b"/backend-api/codex/responses",
        "query_string": b"",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in [*headers, ("content-length", "1")]],
    }
    try:
        preflight_scope(scope)
        return rp.admit(inventory(envelope_of(scope, "http", CHATGPT.name, body)), CHATGPT)
    except EnvelopeRefused as error:
        return error


def _inspect(body: dict):
    return inspect_body(body, "responses", CHATGPT, BodyScan(), False, Ledger())


@pytest.mark.parametrize("request_id", [CACHE_KEY, SESSION, "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"])
def test_hermes_s_headers_pass_by_their_forms(request_id):
    headers = [(k, request_id if k == "x-client-request-id" else v) for k, v in HEADERS]
    admission = _admitted(headers)
    assert isinstance(admission, rp.Admission), admission.agent_message()
    assert ("session_id", SESSION) in admission.headers


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("session_id", "20261103_143000_a1b2c"),
        ("session_id", "20261103_143000_A1B2C3"),
        ("session_id", "cron_billing-report_20261103_143000"),  # a job's free name: refused, never guessed
        ("session_id", "mario.rossi@example.com"),
        ("x-client-request-id", "pck_4f6c1a520d1e4b8e9a372c5"),
        ("x-client-request-id", "pck_mario.rossi"),
        ("x-client-request-id", "the billing job"),
    ],
)
def test_a_value_outside_hermes_s_forms_is_refused_naming_the_header(name, value):
    refused = _admitted([(k, value if k == name else v) for k, v in HEADERS])
    assert isinstance(refused, EnvelopeRefused) and refused.code == "envelope-header-value"
    assert refused.agent_message().endswith(f": header {name}") and value not in refused.agent_message()


@pytest.mark.parametrize("key", [CACHE_KEY, SESSION])
def test_hermes_s_body_passes_with_its_cache_key_retention_and_compaction(key):
    assert _inspect(hermes_body(prompt_cache_key=key)).problem is None


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("prompt_cache_key", "pck_mario.rossi"),
        ("prompt_cache_retention", "7d"),
        ("context_management", [{"type": "truncation", "compact_threshold": 1000}]),
        ("context_management", [{"type": "compaction", "compact_threshold": "high"}]),
        ("context_management", {"type": "compaction"}),
    ],
)
def test_a_member_outside_its_shape_is_refused(member, value):
    try:
        said = _inspect(hermes_body(**{member: value})).problem
    except BodyRefused as error:
        said = str(error)
    assert said is not None and "mario.rossi" not in said


NOTED = [{"type": "compaction", "note": "mario.rossi@example.com"}]


def test_a_member_no_profile_declares_beside_compaction_is_read_by_the_new_rule():
    """A member no profile declares in a compaction entry is read whole by the new rule, its texts left to the final
    check, which stops the address (``Verifier.verify``'s ``new_members``)."""
    inspection = _inspect(hermes_body(context_management=NOTED))
    assert inspection.problem is None and NEW_MEMBER_RULE in inspection.rules
    assert inspection.new_members == ["note", "mario.rossi@example.com"]


def test_strict_a_member_no_profile_declares_beside_compaction_is_refused():
    with pytest.raises(BodyRefused) as caught:
        inspect_body(
            hermes_body(context_management=NOTED),
            "responses",
            CHATGPT,
            BodyScan(),
            False,
            Ledger(),
            unknown_members="refuse",
        )
    assert str(caught.value) == "request body refused (unknown member in context management)"
    assert "mario.rossi" not in str(caught.value)


LOCAL_SESSION = "local-" + "9f2c4e1a" * 8


def test_a_conversation_of_hermes_s_unified_runtime_passes_by_its_local_session_id():
    admission = _admitted([(k, LOCAL_SESSION if k == "session_id" else v) for k, v in HEADERS])
    assert isinstance(admission, rp.Admission), admission.agent_message()
    assert ("session_id", LOCAL_SESSION) in admission.headers


@pytest.mark.parametrize(
    "value",
    [
        "local-" + "9f2c4e1a" * 8 + "0",  # 65 hex digits
        "local-" + "9f2c4e1a" * 7 + "9f2c4e1",  # 63
        "local-" + "9F2C4E1A" * 8,
        "local-mario.rossi@example.com",
        "local-" + "9f2c4e1a" * 4,
        "LOCAL-" + "9f2c4e1a" * 8,
    ],
)
def test_a_local_session_id_outside_its_form_is_refused(value):
    refused = _admitted([(k, value if k == "session_id" else v) for k, v in HEADERS])
    assert isinstance(refused, EnvelopeRefused) and refused.code == "envelope-header-value"
    assert value not in refused.agent_message()


RESIDENCY = "x-openai-internal-codex-residency"


@pytest.mark.parametrize("region", ["us", "eu", "jp", "no_constraint"])
def test_the_residency_of_a_chatgpt_workspace_passes_by_its_form(region):
    admission = _admitted([*HEADERS, (RESIDENCY, region)])
    assert isinstance(admission, rp.Admission), admission.agent_message()
    assert (RESIDENCY, region) in admission.headers


@pytest.mark.parametrize(
    "region", ["US", "", "usa", "us; token=abc", "us,eu", "mario.rossi@example.com", "no_constraints", "eu "]
)
def test_a_residency_outside_its_form_is_refused_naming_the_header(region):
    refused = _admitted([*HEADERS, (RESIDENCY, region)])
    assert isinstance(refused, EnvelopeRefused) and refused.code == "envelope-header-value"
    assert refused.agent_message().endswith(f": header {RESIDENCY}")


def _models(upstream_name: str, region: str):
    route = next(r for r in DEFAULT_UPSTREAMS if r.name == upstream_name)
    path = f"{route.prefix}/models" if upstream_name == "chatgpt" else f"{route.prefix}/v1/models"
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"client_version=0.162.0" if upstream_name == "chatgpt" else b"",
        "headers": [
            (b"host", b"127.0.0.1:5283"),
            (b"authorization", b"Bearer fake-chatgpt-token"),
            (RESIDENCY.encode(), region.encode()),
        ],
    }
    try:
        return rp.admit(inventory(envelope_of(scope, "http", route.name, b"")), route)
    except EnvelopeRefused as error:
        return error


def test_the_residency_passes_on_chatgpt_s_model_list_and_on_no_other_family():
    admission = _models("chatgpt", "us")
    assert isinstance(admission, rp.Admission) and admission.profile.id == "chatgpt.codex.models.1"
    assert (RESIDENCY, "us") in admission.headers
    refused = _models("openai", "us")
    assert isinstance(refused, EnvelopeRefused) and refused.code == "envelope-header-unknown"


def _assistant(**fields) -> dict:
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "the notes are ready"}], **fields}  # fmt: skip


@pytest.mark.parametrize("phase", ["commentary", "partial_answer", "final_answer"])
def test_a_replayed_assistant_message_passes_with_its_phase(phase):
    user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "write the notes"}]}
    assert _inspect(hermes_body(input=[user, _assistant(phase=phase)])).problem is None


@pytest.mark.parametrize("phase", ["final", "Final_Answer", "mario.rossi@example.com", "", None, 1, ["commentary"]])
def test_a_phase_outside_codex_s_values_is_refused(phase):
    with pytest.raises(BodyRefused):
        _inspect(hermes_body(input=[_assistant(phase=phase)]))


def _phased(role: str) -> dict:
    message = {"type": "message", "role": role, "phase": "final_answer",
               "content": [{"type": "input_text", "text": "write the notes"}]}  # fmt: skip
    return hermes_body(input=[message])


@pytest.mark.parametrize("role", ["user", "developer", "system"])
def test_a_phase_on_a_message_not_the_model_s_is_an_unknown_member(role):
    """Only the model's own messages carry one: on any other it is a member the profile does not declare, read by the
    new rule as any unknown member (never as the model's phase), and refused as such where new members are refused (an
    exact exception may name it, as any unknown member)."""
    inspection = _inspect(_phased(role))
    assert inspection.problem is None and NEW_MEMBER_RULE in inspection.rules
    assert inspection.new_members == ["phase", "final_answer"]


@pytest.mark.parametrize("role", ["user", "developer", "system"])
def test_strict_a_phase_on_a_message_not_the_model_s_is_an_unknown_member(role):
    with pytest.raises(BodyRefused) as refused:
        inspect_body(_phased(role), "responses", CHATGPT, BodyScan(), False, Ledger(), unknown_members="refuse")
    assert refused.value.code.startswith("body-unknown-member")
