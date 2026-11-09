"""Root's rulings on P2-8a's findings 4-6 and on the second agent's concerns (2026-11-04), each through the actual
ingress of a computer to the recording transport at S1-S4 (`tests/request_coverage.py`'s cell), or the production
forwarder alone where no record is read:

4. Gemini's cache creation takes the GenAI SDK's `model: "models/<id>"`, the same model-id grammar after the prefix.
5. A refused WebSocket handshake says what it refused in its 1008 close reason (its category, and the header's or the
   query parameter's name, never a value, within the 123 bytes a close reason holds) and is written to the history.
   A request refused over HTTP for its envelope (a header, a query parameter, a route) is written to the history too,
   with its category (D4, R-79), and its refusal names an unknown query parameter as it names a header.
6. OpenRouter's `vendor/model` ids pass by the OpenRouter route's grammar for Anthropic's, OpenAI's and Google's
   models and `openrouter/auto`; `GET /v1/models?beta=true` is admitted on the Anthropic profile; an unknown member
   inside a Gemini part is refused as `body-unknown-member`; a model list that passed is recorded under its profile's
   route, never as `unknown-route`.

No provider is called: the recording transport answers in its place."""

from __future__ import annotations

import json

import pytest

from latet.gateway import model_ids
from latet.gateway.routes import Upstream

from ..request_coverage import EMAIL, Case, computer_cell, run_case
from ..request_recorder import RecordingWSUpstream
from .test_request_coverage import counts, gateway

GEMINI_KEY = (("x-goog-api-key", "AIza-rc"), ("content-type", "application/json"))
OPENAI_KEY = (("authorization", "Bearer sk-rc"), ("content-type", "application/json"))
CODEX_WS = (
    ("authorization", "Bearer eyJ-rc"),
    ("originator", "codex_cli_rs"),
    ("session-id", "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"),
)
TURN = json.dumps(
    {
        "type": "response.create",
        "model": "gpt-5.2-codex",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "store": False,
    }
)
CANARY = "private-value-canary"
# A query parameter's name of one word longer than 16 characters (Task 8 of the 0.12 plan): told by its length alone.
LONG_QUERY = "querycanary" + "x" * 9


@pytest.fixture
async def cell(tmp_path):
    upstream = RecordingWSUpstream().start()
    try:
        async with computer_cell(tmp_path, upstream) as made:
            yield made
    finally:
        upstream.stop()


def _sent_once_masked(cell, observation):
    assert observation["client"]["status"] == 200, observation["client"]
    assert observation["counts"]["send"] == 1
    assert all(EMAIL.encode() not in sent.content for sent in cell.transport.sent)


def _refused(observation, code):
    assert observation["client"]["status"] == 400, observation["client"]
    assert observation["counts"] == {"build": 0, "send": 0, "connect": 0, "frames_up": 0}
    assert [record["block_reason"] for record in observation["records"]] == [code]


# --- 4. Gemini cache creation ----------------------------------------------------------------------------------------


def _cache(model: str) -> bytes:
    return json.dumps(
        {"model": model, "contents": [{"role": "user", "parts": [{"text": f"write to {EMAIL}"}]}], "ttl": "300s"}
    ).encode()


@pytest.mark.parametrize("model", ["models/gemini-2.5-pro", "models/gemini-2.0-flash-001", "gemini-2.5-pro"])
async def test_gemini_cache_creation_takes_the_genai_sdks_model_resource(cell, model):
    case = Case("cache", (), "POST", "/gemini/v1beta/cachedContents", GEMINI_KEY, _cache(model))
    _sent_once_masked(cell, await run_case(cell, case))


@pytest.mark.parametrize("model", ["models/private-canary", "models/", "models/models/gemini-2.5-pro", "tunedModels/x"])
async def test_the_cache_models_prefix_keeps_the_model_grammar(cell, model):
    case = Case("cache", (), "POST", "/gemini/v1beta/cachedContents", GEMINI_KEY, _cache(model))
    observation = await run_case(cell, case)
    assert observation["client"]["status"] == 400 and observation["counts"]["send"] == 0
    assert "model resource" in observation["client"]["body"]["error"]


async def test_the_models_prefix_is_cache_creations_alone(cell):
    body = json.dumps({"model": "models/gemini-2.5-pro", "contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    target = "/gemini/v1beta/models/gemini-2.5-pro:generateContent"
    observation = await run_case(cell, Case("generate", (), "POST", target, GEMINI_KEY, body.encode()))
    assert observation["client"]["status"] == 400 and observation["counts"]["send"] == 0


# --- 5. a refused WebSocket handshake ---------------------------------------------------------------------------------


def _closed(observation):
    assert observation["counts"] == {"build": 0, "send": 0, "connect": 0, "frames_up": 0}
    code, reason = observation["client"]["close"]
    assert code == 1008
    assert len(reason.encode("utf-8")) <= 123
    return reason


@pytest.mark.parametrize(
    ("extra", "query", "code", "name"),
    [
        ((("x-drift-header", CANARY),), "", "envelope-header-unknown", "header x-drift-header"),
        ((), f"?drift={CANARY}", "envelope-query-unknown", "query parameter drift"),
        # Task 8 of the 0.12 plan: a long word of an unknown query parameter's name is told by its length.
        ((), f"?{LONG_QUERY}-x={CANARY}", "envelope-query-unknown", "query parameter <20 characters>-x"),
    ],
)
async def test_a_refused_handshake_names_what_it_refused_and_is_recorded(cell, extra, query, code, name):
    target = "/backend-api/codex/responses" + query
    case = Case("ws", (), "GET", target, (*CODEX_WS, *extra), transport="ws", frames=(TURN,))
    observation = await run_case(cell, case)
    reason = _closed(observation)
    assert code in reason and name in reason, reason
    assert CANARY not in reason and LONG_QUERY not in reason
    [record] = observation["records"]
    assert record["transport"] == "ws" and record["block_reason"] == code
    assert record["request_headers"] == []
    assert CANARY not in json.dumps(observation["records"]) and CANARY not in json.dumps(observation["logs"])
    runtime = await cell.runtime()
    [raw] = runtime.gateway.recorder._rc_records
    assert CANARY not in repr(raw.summary)


@pytest.mark.parametrize(
    ("long", "told"),
    [
        # The scoped check of N-1, P-1: a word longer than 16 characters is told by its length, never as it is.
        ("x-" + "a" * 120, "header x-<120 characters>"),
        ("x-" + "-".join(["a" * 16] * 7), "header x-" + "a" * 16 + "-"),  # short words, the name cut at 64
    ],
)
async def test_a_long_header_name_still_fits_the_close_reason(cell, long, told):
    case = Case(
        "ws", (), "GET", "/backend-api/codex/responses", (*CODEX_WS, (long, "1")), transport="ws", frames=(TURN,)
    )
    reason = _closed(await run_case(cell, case))
    assert "envelope-header-unknown" in reason and told in reason and "a" * 17 not in reason
    assert len(reason.encode()) <= 123


# --- 5. a request refused over HTTP for its envelope -------------------------------------------------------------------


def _chat() -> bytes:
    return json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}).encode()


@pytest.mark.parametrize(
    ("target", "extra", "code", "named"),
    [
        (
            "/openai/v1/chat/completions",
            (("x-drift-header", CANARY),),
            "envelope-header-unknown",
            "header x-drift-header",
        ),
        (f"/openai/v1/chat/completions?drift={CANARY}", (), "envelope-query-unknown", "query parameter drift"),
        (
            f"/openai/v1/chat/completions?{LONG_QUERY}={CANARY}",
            (),
            "envelope-query-unknown",
            "query parameter <20 characters>",
        ),
        (f"/openai/v1/files?purpose={CANARY}", (), "envelope-route", "route /{destination}/{unsupported-route}"),
    ],
)
async def test_an_envelope_refusal_over_http_is_recorded_with_its_category(cell, target, extra, code, named):
    """D4, R-79: the history carries an envelope refusal's category as it carries any refusal's; its record keeps the
    route template its profile names, no header and no query, and the agent is told the header's or the query
    parameter's name, never a value."""
    observation = await run_case(cell, Case("envelope", (), "POST", target, (*OPENAI_KEY, *extra), _chat()))
    _refused(observation, code)
    words = observation["client"]["body"]["error"]
    assert named in words and CANARY not in words and LONG_QUERY not in words
    [record] = observation["records"]
    assert record["transport"] == "http" and "blocked" in record["flags"]
    assert record["path"].startswith("/{destination}/") and "drift" not in record["path"]
    assert record["request_headers"] == [] and not record["verified"]
    assert CANARY not in json.dumps(observation["records"]) and CANARY not in json.dumps(observation["logs"])


def test_the_query_parameter_is_named_by_the_production_forwarder_alone():
    """The same words without a runtime: the forwarder names the query parameter, never its value."""
    with gateway() as (client, transport, made, _):
        response = client.get(f"/openai/v1/models?drift={CANARY}")
        assert response.status_code == 400
        assert "query parameter drift" in response.text and CANARY not in response.text
        assert counts(made) == (0, 0) and transport.sent == []


# --- 6. smaller points -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-opus-4.1",
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3.7-sonnet:thinking",
        "anthropic/claude-haiku-4.5",
        "openai/gpt-4o",
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2-codex",
        "google/gemini-2.5-pro",
        "google/gemini-2.5-flash:free",
        "openrouter/auto",
    ],
)
def test_openrouter_model_ids_pass_by_its_grammar(value):
    assert model_ids.body_model("openai", "openrouter", value)


@pytest.mark.parametrize(
    "value",
    [
        "anthropic/private-canary",
        "anthropic/claude-sonnet-4.5 extra",
        "anthropic/claude-sonnet-4.5:private",
        "openai/gpt-4o/../x",
        "meta-llama/llama-3.1-70b-instruct",  # no grammar of Latet's: an exact request_resources row
        "claude-sonnet-4-5",  # OpenRouter names its models by vendor
        "openrouter/" + "a" * 130,
    ],
)
def test_openrouter_model_ids_outside_its_grammar_are_refused(value):
    assert not model_ids.body_model("openai", "openrouter", value)


def test_the_vendor_form_is_openrouters_alone():
    assert not model_ids.body_model("openai", "openai", "openai/gpt-4o")
    assert not model_ids.body_model("anthropic", "anthropic", "anthropic/claude-sonnet-4.5")


async def test_an_openrouter_chat_with_a_vendor_model_is_masked_and_sent(cell):
    body = json.dumps(
        {"model": "anthropic/claude-sonnet-4.5", "messages": [{"role": "user", "content": f"to {EMAIL}"}]}
    )
    headers = (("authorization", "Bearer sk-or-v1-rc"), ("content-type", "application/json"))
    case = Case("or", (), "POST", "/openrouter/api/v1/chat/completions", headers, body.encode())
    _sent_once_masked(cell, await run_case(cell, case))


@pytest.mark.parametrize("query", ["?beta=true", "?beta=true&limit=20", "?limit=20&beta=true"])
def test_the_anthropic_model_list_takes_beta_true(query):
    upstream = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
    with gateway(upstream=upstream) as (client, transport, made, _):
        response = client.get("/anthropic/v1/models" + query)
        assert response.status_code == 200
        assert counts(made) == (1, 1)
        assert transport.sent[0].raw_path == ("/v1/models" + query).encode()


@pytest.mark.parametrize("query", ["?beta=false", "?beta=true&beta=true", "?beta="])
def test_the_anthropic_model_lists_beta_keeps_its_value_grammar(query):
    upstream = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
    with gateway(upstream=upstream) as (client, _, made, _):
        assert client.get("/anthropic/v1/models" + query).status_code == 400
        assert counts(made) == (0, 0)


@pytest.mark.parametrize(
    "part",
    [{"text": "hello", "drift_member": "x"}, {"text": "hello", "driftMember": {"a": 1}}, {"drift_member": "x"}],
)
async def test_an_unknown_member_inside_a_gemini_part_is_body_unknown_member(cell, part):
    body = json.dumps({"contents": [{"role": "user", "parts": [part]}]}).encode()
    target = "/gemini/v1beta/models/gemini-2.5-pro:generateContent"
    observation = await run_case(cell, Case("part", (), "POST", target, GEMINI_KEY, body))
    _refused(observation, "body-unknown-member")
    assert "unknown member" in observation["client"]["body"]["error"]


@pytest.mark.parametrize(
    ("target", "headers"),
    [
        ("/openrouter/api/v1/models", (("authorization", "Bearer sk-or-v1-rc"),)),
        ("/openai/v1/models", (("authorization", "Bearer sk-rc"),)),
        ("/anthropic/v1/models?limit=20", (("x-api-key", "sk-ant-api03-rc"), ("anthropic-version", "2023-06-01"))),
        ("/gemini/v1beta/models?pageSize=50", (("x-goog-api-key", "AIza-rc"),)),
    ],
)
async def test_a_model_list_that_passed_is_recorded_under_its_profile(cell, target, headers):
    observation = await run_case(cell, Case("models", (), "GET", target, headers))
    assert observation["client"]["status"] == 200 and observation["counts"]["send"] == 1
    [record] = observation["records"]
    assert "unknown-route" not in record["flags"], record["flags"]
    assert "{destination}" in record["path"], record["path"]
    runtime = await cell.runtime()
    [raw] = runtime.gateway.recorder._rc_records
    assert type(raw.summary.get("profile")) is str and raw.summary["profile"]
