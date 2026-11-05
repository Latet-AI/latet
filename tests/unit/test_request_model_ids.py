"""Strict model-id grammars per provider (model_ids), in paths and in P2-2's body ``model`` rule, with stock settings."""

from __future__ import annotations

import pytest

from latet.gateway import model_ids
from latet.gateway.body_coverage import Ledger, inspect_body
from latet.gateway.routes import Upstream
from latet.protocols.base import BodyScan

# Documented forms: Anthropic's models overview (first-party aliases and dated ids, Bedrock and Vertex ids), AWS
# Bedrock's model ids and system-defined cross-Region inference profiles, Google's Gemini model codes, OpenAI's models.
ACCEPTED = {
    "bedrock": [
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "eu.anthropic.claude-sonnet-4-20250514-v1:0",
        "apac.anthropic.claude-3-5-sonnet-20240620-v1:0",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "anthropic.claude-3-sonnet-20240229-v1:0:28k",
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-opus-5-5",
    ],
    "bedrock-profile": ["us.anthropic.claude-sonnet-4-5-20250929-v1:0", "global.anthropic.claude-opus-4-6-v1"],
    "vertex": ["claude-sonnet-4-5@20250929", "claude-3-5-sonnet-v2@20241022", "claude-opus-4-6", "claude-haiku-5-5"],
    "google": [
        "gemini-2.5-pro",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash-001",
        "gemini-2.5-flash-preview-09-2025",
        "gemini-3-pro-preview",
        "gemini-flash-latest",
        "gemini-embedding-001",
        "text-embedding-005",
    ],
    "anthropic": [
        "claude-opus-5-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-haiku-latest",
        "claude-haiku-4-5",
        "claude-fable-5-1",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4o-2024-08-06",
        "gpt-4.1-nano",
        "gpt-5",
        "gpt-5.2-codex",
        "gpt-5.1-codex-max",
        "o3",
        "o4-mini",
        "o3-deep-research",
        "gpt-oss-120b",
        "chatgpt-4o-latest",
        "codex-mini-latest",
        "text-embedding-3-small",
    ],
}
FREE_TEXT = [
    "your-model",
    "claude-x",
    "mario.rossi@acme.com",
    "C:\\Users\\mario.rossi\\repo",
    "has a space",
    "anthropic.claude-mario-rossi-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0-mario",
    "gemini-2.5-pro-mario-rossi",
    "gpt-mario",
    "claude-sonnet-mario",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123def456",
    "gpt-4o" + "-mini" * 30,
]
# The built-in list P2-2 shipped before the grammars: every entry stays accepted.
FORMER = {
    "anthropic": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5", "claude-sonnet-4-5",
                  "claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-3-5-sonnet-20241022",
                  "claude-3-7-sonnet-20250219"],
    "chat": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-5", "gpt-5-mini", "gpt-5-nano",
             "o1", "o3", "o3-mini", "o4-mini"],
    "responses": ["gpt-4o", "gpt-4.1", "gpt-5", "gpt-5-codex", "gpt-5.1", "gpt-5.1-codex", "gpt-5.2", "gpt-5.2-codex",
                  "o3", "o4-mini"],
    "embeddings": ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-embedding-001"],
}  # fmt: skip


@pytest.mark.parametrize(("kind", "value"), [(kind, value) for kind, values in ACCEPTED.items() for value in values])
def test_documented_model_ids_pass_their_provider_grammar(kind, value):
    assert model_ids.path_model(kind, value)


@pytest.mark.parametrize("kind", sorted(ACCEPTED))
@pytest.mark.parametrize("value", FREE_TEXT)
def test_free_text_and_account_bound_values_are_no_model_id(kind, value):
    assert not model_ids.path_model(kind, value)


def test_a_bedrock_inference_profile_read_needs_its_geography_prefix():
    assert not model_ids.path_model("bedrock-profile", "anthropic.claude-sonnet-4-5-20250929-v1:0")


@pytest.mark.parametrize(("protocol", "value"), [(p, v) for p, values in FORMER.items() for v in values])
def test_every_formerly_listed_body_model_is_inside_the_grammars(protocol, value):
    assert model_ids.body_model(protocol, "gemini" if protocol == "gemini" else "openai", value)


def _body(protocol: str, model: str, family: str = "openai"):
    upstream = Upstream(family, "/" + family, "https://provider.invalid")
    body = {"model": model, **({"contents": []} if protocol == "gemini" else {"messages": []})}
    if protocol == "responses":
        body = {"model": model, "input": []}
    return inspect_body(body, protocol, upstream, BodyScan(), False, Ledger())


@pytest.mark.parametrize(
    ("protocol", "family", "model"),
    [
        ("anthropic", "anthropic", "claude-opus-5-5"),
        ("chat", "openai", "gpt-4o-mini"),
        ("responses", "openai", "gpt-5.2-codex"),
        ("gemini", "gemini", "gemini-3-pro-preview"),
        ("chat", "gemini", "gemini-2.5-flash"),  # Gemini's OpenAI-compatible route takes Gemini's ids
    ],
)
def test_a_documented_body_model_passes_with_stock_settings(protocol, family, model):
    assert _body(protocol, model, family).problem is None


@pytest.mark.parametrize("model", FREE_TEXT)
@pytest.mark.parametrize("protocol", ["anthropic", "chat", "responses"])
def test_an_undocumented_body_model_still_needs_its_exact_row(protocol, model):
    assert _body(protocol, model).problem == "request body refused (model resource)"
    configured = Upstream(
        "company", "/company", "https://company.invalid", envelope_expected=frozenset({("model", model)})
    )
    body = {"model": model, "input": []} if protocol == "responses" else {"model": model, "messages": []}
    assert inspect_body(body, protocol, configured, BodyScan(), False, Ledger()).problem is None
