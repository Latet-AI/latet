"""Hermes Agent signed in with ChatGPT, as its source builds a request (hermes-agent 25ce7ccf), through the envelope's
admission and P2-2's body coverage: its headers and members are named by their own forms.

- ``session_id``: its session id, ``new_session_id()``: ``f"{now:%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"``
  (``hermes_state_ids.py:53-60``), sent as ChatGPT's header (``agent/transports/codex.py:864-872``).
- ``x-client-request-id`` and the body's ``prompt_cache_key``: ``_content_cache_key()``, ``"pck_" + sha256[:24]``
  (``codex.py:428-449``), else the bounded scope, which is the session id (``codex.py:31-55``, ``798-806``, ``869``).
- ``prompt_cache_retention``: ``"24h"`` (``codex.py:357-372``), one of OpenAI's documented ``in-memory`` and ``24h``.
- ``context_management``: ``[{"type": "compaction", "compact_threshold": N}]`` (``agent/native_compaction.py:3, 142``).
"""

from __future__ import annotations

import json

import pytest

from latet.gateway import request_profiles as rp
from latet.gateway.body_coverage import BodyRefused, Ledger, inspect_body
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
        ("context_management", [{"type": "compaction", "note": "mario.rossi@example.com"}]),
        ("context_management", {"type": "compaction"}),
    ],
)
def test_a_member_outside_its_shape_is_refused(member, value):
    try:
        said = _inspect(hermes_body(**{member: value})).problem
    except BodyRefused as error:
        said = str(error)
    assert said is not None and "mario.rossi" not in said
