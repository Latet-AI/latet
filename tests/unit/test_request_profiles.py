"""Exact HTTP profile routing: no bodyless, method or sibling inference."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from latet.config import Destination
from latet.gateway.cloud import BedrockUpstream, VertexUpstream
from latet.gateway.routes import Upstream

from .test_request_coverage import counts, gateway


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "/openai/v1/models?unpublished=true"),
        ("GET", "/openai/v1/responses/resp_canary"),
        ("DELETE", "/openai/v1/models/model_canary"),
        ("HEAD", "/openai/v1/models"),
        ("OPTIONS", "/openai/v1/models"),
        ("GET", "/openai/v1/files"),
    ],
)
def test_known_model_list_requires_complete_profile(method, target):
    with gateway() as (client, _, made, _):
        response = client.request(method, target)
        assert response.status_code == 400
        assert counts(made) == (0, 0)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-claude-code-session-id": "00000000-0000-0000-0000-000000000001"},
        {"authorization": "Bearer sk-ant-oat-fixture"},
    ],
)
def test_a_sibling_subprofile_is_never_more_permissive(headers):
    upstream = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
    with gateway(upstream=upstream) as (client, _, made, _):
        response = client.get("/anthropic/v1/models?unpublished=true", headers=headers)
        assert response.status_code == 400
        assert counts(made) == (0, 0)


def test_a_destination_with_a_query_is_refused_by_the_settings():
    with pytest.raises(ValidationError):
        Destination(name="company", url="https://provider.invalid?private=query")


def test_unknown_route_refusal_says_the_route_is_unsupported_without_an_exception_or_the_raw_target():
    """No exception can name a route (P2-6 F-4 (a)): the refusal says this version does not support the call and that
    the agent or its settings must not make it, naming the route by its template alone, never the target sent."""
    with gateway() as (client, _, made, _):
        response = client.get("/openai/private-path-canary?private-query-canary=private-value-canary")
        assert response.status_code == 400
        assert "route" in response.text and "does not support this call" in response.text
        assert "exception" not in response.text
        assert "private-path-canary" not in response.text
        assert "private-query-canary" not in response.text
        assert "private-value-canary" not in response.text
        assert counts(made) == (0, 0)


@pytest.mark.parametrize(
    ("upstream", "target"),
    [
        (Upstream("anthropic", "/anthropic", "https://api.anthropic.com"), "/anthropic/v1/models?limit=1000"),
        (Upstream("openai", "/openai", "https://api.openai.com"), "/openai/v1/models"),
        (Upstream("openrouter", "/openrouter", "https://openrouter.ai"), "/openrouter/api/v1/models"),
        (
            Upstream("gemini", "/gemini", "https://generativelanguage.googleapis.com"),
            "/gemini/v1beta/models?pageSize=1000",
        ),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles?type=SYSTEM_DEFINED&maxResults=1000"),
        (VertexUpstream(), "/vertex/v1/publishers/google/models?alt=json"),
    ],
)
def test_each_ancillary_profile_preserves_exact_target(upstream, target):
    with gateway(upstream=upstream) as (client, transport, made, _):
        response = client.get(target)
        assert response.status_code == 200
        assert counts(made) == (1, 1)
        resolved = upstream.resolve("GET", target.split("?", 1)[0], target.partition("?")[2])
        assert resolved is not None
        parts = urlsplit(resolved.url)
        assert transport.sent[0].raw_path == (parts.path + ("?" + parts.query if parts.query else "")).encode()


_ANTHROPIC = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
_GEMINI = Upstream("gemini", "/gemini", "https://generativelanguage.googleapis.com")
# A model list's second page, as its SDK asks it: Anthropic's auto-paginating models.list() by the last model's id,
# google-genai's pager by Google's base64 page token (its = escaped), the AWS paginator of ListInferenceProfiles by
# its nextToken (its + / = escaped).
CURSORS = [
    (_ANTHROPIC, "/anthropic/v1/models?limit=20", "after_id", "claude-opus-4-5-20251101"),
    (_ANTHROPIC, "/anthropic/v1/models?beta=true", "before_id", "claude-3-5-sonnet-20241022"),
    (_GEMINI, "/gemini/v1beta/models?pageSize=50", "pageToken", "Chltb2RlbHMvZ2VtaW5pLTIuNS1mbGFzaC0wMDE%3D"),
    (VertexUpstream(), "/vertex/v1/publishers/google/models?alt=json", "pageToken", "Cg0KC2dlbWluaS0yLjU_Ze-1"),
    (
        BedrockUpstream(),
        "/bedrock/us-east-1/inference-profiles?type=SYSTEM_DEFINED&maxResults=10",
        "nextToken",
        "AAMA-EFRSURBSGo%2BdGhpcyBpcyBhbiBvcGFxdWUgdG9rZW4%3D",
    ),
]


@pytest.mark.parametrize(
    ("upstream", "target", "name", "value"), CURSORS, ids=["after_id", "before_id", "gemini", "vertex", "bedrock"]
)
def test_a_model_list_cursor_passes_by_its_grammar_with_no_owner_row(upstream, target, name, value):
    """G2 review M-1 (D1, pagination bounded): a model list's cursor needed an exact ``request_resources`` row for its
    very value, which no one can list in advance, so every second page was refused (envelope-resource). Anthropic's
    after_id and before_id pass by Claude's model-id grammar, Google's pageToken and AWS's nextToken by a bounded
    opaque-token grammar, checked as text by the final check; the request leaves as it came."""
    with gateway(upstream=upstream) as (client, transport, made, _):
        response = client.get(f"{target}&{name}={value}")
        assert response.status_code == 200, response.text
        assert counts(made) == (1, 1)
        assert transport.sent[0].raw_path.endswith(f"&{name}={value}".encode())


@pytest.mark.parametrize(
    ("upstream", "target", "name", "value"),
    [
        (_ANTHROPIC, "/anthropic/v1/models?limit=20", "after_id", "mario-rossi-acme"),
        (_ANTHROPIC, "/anthropic/v1/models?limit=20", "before_id", "claude-opus-4-5-mariorossi"),
        (_GEMINI, "/gemini/v1beta/models?pageSize=50", "pageToken", "mario.rossi%40acme.example"),
        (_GEMINI, "/gemini/v1beta/models?pageSize=50", "pageToken", "A" * 2049),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles?maxResults=10", "nextToken", "mario%20rossi"),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles?maxResults=10", "nextToken", "A" * 2049),
    ],
    ids=["after_id", "before_id", "pageToken-email", "pageToken-long", "nextToken-space", "nextToken-long"],
)
def test_a_cursor_outside_its_grammar_is_refused_naming_the_parameter(upstream, target, name, value):
    with gateway(upstream=upstream) as (client, transport, made, _):
        response = client.get(f"{target}&{name}={value}")
        assert response.status_code == 400
        words = response.text
        assert f"request envelope refused (envelope-query-value): query parameter {name}" in words
        assert "mario" not in words and value not in words
        assert counts(made) == (0, 0) and transport.sent == []


def test_an_opaque_cursor_is_checked_as_text_by_the_final_check():
    """A token passes by its grammar alone, never on its word: the final check reads it, as a header Latet checks."""
    from latet.gateway import request_profiles
    from latet.gateway.coverage import Envelope, inventory

    for upstream, path, query in (
        (_GEMINI, b"/gemini/v1beta/models", b"pageToken=Q2hsdGIyUmxiSE09"),
        (BedrockUpstream(), b"/bedrock/us-east-1/inference-profiles", b"nextToken=Q2hsdGIyUmxiSE09"),
    ):
        envelope = Envelope("http", "GET", path, query, (), b"", upstream.name)
        admission = request_profiles.admit(inventory(envelope), upstream)
        assert admission.checked == ("Q2hsdGIyUmxiSE09",) and "checked" in admission.treatments
    # A token within its grammar that holds what the final check stops (an AWS access key, a GitHub token) is refused.
    for upstream, target in (
        (_GEMINI, "/gemini/v1beta/models?pageSize=5&pageToken=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles?maxResults=5&nextToken=AKIAZ3MSJV4WWNAHJRHA"),
    ):
        with gateway(upstream=upstream) as (client, transport, made, _):
            response = client.get(target)
            assert response.status_code == 400 and "envelope-sensitive" in response.text
            assert counts(made) == (0, 0) and transport.sent == []


def test_the_manifest_says_each_cursors_grammar():
    import json
    from pathlib import Path

    manifest = json.loads((Path(__file__).parents[1] / "data/request-coverage/profiles.json").read_text("utf-8"))
    grammars = manifest["envelope"]["query_grammars"]
    for name in ("after_id", "before_id"):
        assert grammars[name].startswith("Claude model id (model_ids.CLAUDE)")
    for name in ("pageToken", "nextToken"):
        assert grammars[name].startswith("opaque token") and "final check" in grammars[name]
    assert "exact configured value" not in {grammars[name] for name in ("after_id", "before_id", "pageToken")}


def test_the_manifest_states_the_open_list_its_grammars_and_bounds():
    """D4 (b), option B (minors after 0.11, item 1): the manifest's header policy states the open list as the code holds
    it, each grammar and bound the very one admission reads, its families, its treatments and where it holds."""
    import json
    from pathlib import Path

    from latet.gateway import request_policy, request_profiles

    manifest = json.loads((Path(__file__).parents[1] / "data/request-coverage/profiles.json").read_text("utf-8"))
    policy = manifest["envelope"]["header_policy"]
    rule = policy["open_list"]
    assert rule["anthropic_name_grammar"] == request_profiles._OPEN_NAME.pattern == "anthropic-[a-z0-9-]{1,48}"
    assert rule["anthropic_name_word_most"] == request_profiles._OPEN_WORD == 16
    assert rule["anthropic_value_grammar"] == request_profiles._OPEN_VALUE.pattern == "[ -~]{1,1024}"
    assert rule["anthropic_most"] == request_profiles._OPEN_MOST == 8
    assert rule["beta_token_grammar"] == request_profiles._OPEN_BETA.pattern
    assert rule["beta_most"] == request_profiles._OPEN_BETAS == 64
    assert rule["claude_code_prefix"] == request_profiles._HINT_PREFIX == "x-claude-code-"
    assert rule["families"] == ["anthropic (Vertex's Claude routes included)", "bedrock model calls"]
    assert rule["rule"] == request_profiles.OPEN_RULE and request_profiles.OPEN_RULE in request_policy.BUILT_IN
    assert "verify_only" in rule["where"] and "rewrites" in rule["where"]
    assert (
        "envelope-sensitive" in rule["anthropic_treatment"] and "removed and counted" in rule["claude_code_treatment"]
    )
    # The review of Task 3, I-1, and its re-review, I-A: the name, which arrives in lowercase, read in three forms, and
    # what is not found there said.
    assert "in lowercase, in capitals and with each word capitalised, and its value" in rule["anthropic_treatment"]
    assert "in another mix of capitals is not found there" in rule["anthropic_treatment"]
    # The scoped check of Task 3's second fix round, N-1: no word of the name longer than 16 characters, and what is not
    # found said of every value, a secret whose form mixes capitals as well as a value of yours.
    assert "no word of it, between its dashes, longer than 16 characters" in rule["anthropic_treatment"]
    assert "a value of yours, or a secret whose form mixes them" in rule["anthropic_treatment"]
    assert "the name is refused only when it is past one of its bounds" in rule["anthropic_treatment"]
    assert (
        "a word longer than that bound, a longer name or a character outside the grammar" in rule["anthropic_treatment"]
    )
    assert "a word of its name longer than 16 characters" in rule["past_bounds"]
    # Its scoped check, P-1: a refused header's name is told with each word longer than 16 characters by its length.
    from latet.gateway import coverage

    assert coverage.NAMED_WORD == request_profiles._OPEN_WORD == 16
    assert "each word of it longer than 16 characters told by its length" in policy["unknown"]
    assert "exception" in rule["exceptions"] and "first" in rule["exceptions"]
    assert manifest["envelope"]["revision"] == request_profiles.REVISION == "http-envelope-1"
    assert not request_profiles.CARRIES_FROM, "no profile revision: exceptions apply first"


def test_codex_0162_chatgpt_models_source_derived_fixture():
    """Pinned c1382380 source, not a captured client or real ChatGPT sign-in qualification."""
    upstream = Upstream("chatgpt", "/backend-api/codex", "https://chatgpt.com", keep_prefix=True)
    headers = {
        "authorization": "Bearer fake-chatgpt-provider-credential",
        "version": "0.162.0",
        "originator": "codex_cli_rs",
        "user-agent": "codex_cli_rs/0.162.0 (Windows NT 10.0; x86_64) test-terminal",
    }
    with gateway(upstream=upstream) as (client, transport, made, _):
        response = client.get("/backend-api/codex/models?client_version=0.162.0", headers=headers)
        assert response.status_code == 200
        assert counts(made) == (1, 1)
        assert transport.sent[0].raw_path == b"/backend-api/codex/models?client_version=0.162.0"
        assert (b"authorization", headers["authorization"].encode()) in transport.sent[0].headers


@pytest.mark.parametrize(
    "query",
    [
        "",
        "client_version=private-value",
        "client_version=0.162.0&unknown=1",
        "client_version=0.162.0&client_version=0.162.1",
    ],
)
def test_codex_model_list_has_its_own_bounded_query_grammar(query):
    upstream = Upstream("chatgpt", "/backend-api/codex", "https://chatgpt.com", keep_prefix=True)
    with gateway(upstream=upstream) as (client, _, made, _):
        response = client.get("/backend-api/codex/models?" + query)
        assert response.status_code == 400
        assert counts(made) == (0, 0)


def test_unconfigured_resource_is_never_trusted_on_first_sight():
    with gateway() as (client, _, made, _):
        for _ in range(2):
            response = client.get("/openai/v1/models/arbitrary-model")
            assert response.status_code == 400
        assert counts(made) == (0, 0)


def test_configured_resource_values_are_exact_and_destination_bound():
    upstream = Upstream(
        "company", "/company", "https://company.invalid", envelope_expected=frozenset({("model", "approved-model")})
    )
    with gateway(upstream=upstream) as (client, transport, made, _):
        assert client.get("/company/v1/models/approved-model").status_code == 200
        assert client.get("/company/v1/models/another-model").status_code == 400
        assert counts(made) == (1, 1)
        assert transport.sent[0].raw_path == b"/v1/models/approved-model"


RESOURCE = {"destination": "openai", "url": "https://api.openai.com", "role": "model", "value": "approved-model"}


def test_request_resource_settings_are_closed_bounded_and_private():
    from latet.config import Config

    configured = Config.model_validate({"request_resources": [RESOURCE]})
    assert configured.model_dump(mode="json").get("request_resources") == [RESOURCE]
    assert "approved-model" not in repr(configured)
    for rows in (
        [RESOURCE, RESOURCE],
        [RESOURCE] * 129,
        [{**RESOURCE, "role": "unknown-role"}],
        [{**RESOURCE, "wildcard": True}],
        [{**RESOURCE, "url": "https://api.openai.com?query=secret"}],
        [{**RESOURCE, "value": "x" * 2049}],
    ):
        with pytest.raises(ValidationError):
            Config.model_validate({"request_resources": rows})


def test_a_resource_role_that_names_a_header_is_one_no_grammar_reads():
    """An owner's exact row is what admits a header that has no grammar of its own (a billing organization or project,
    x-session-id). A header read by its agent's grammar (ChatGPT's session_id, Hermes Agent's session id) is never
    admitted by a row: no role names it, and a row that names it is refused."""
    from typing import get_args

    from latet.config import Config, RequestResource
    from latet.gateway.request_profiles import _RESOURCE_HEADERS, ID_HEADERS

    roles = set(get_args(RequestResource.model_fields["role"].annotation))
    assert roles >= _RESOURCE_HEADERS and not roles & set(ID_HEADERS)
    hermes = {**RESOURCE, "role": "session_id", "value": "20261103_101500_a1b2c3"}
    with pytest.raises(ValidationError):
        Config.model_validate({"request_resources": [hermes]})


def test_request_resources_are_identity_only_not_shared_or_project_authority():
    from latet.config import Config, ProjectSettings
    from latet.project_file import REPO_KEYS
    from latet.server.shared import OWN_KEYS, layered

    configured = Config.model_validate({"request_resources": [RESOURCE]})
    assert "request_resources" in OWN_KEYS
    assert "request_resources" not in ProjectSettings.model_fields
    assert "request_resources" not in REPO_KEYS
    assert layered(configured, {}).model_dump(mode="json").get("request_resources") == []
    assert layered(Config(), {"request_resources": [RESOURCE]}).model_dump(mode="json").get("request_resources") == [
        RESOURCE
    ]


def test_real_runtime_settings_bind_and_replace_resource_snapshots(tmp_path):
    from latet.config import Config, IdentityConfig, load_config
    from latet.gateway.app import create_app
    from latet.gateway.coverage import EnvelopeRefused, inventory
    from latet.gateway.request_profiles import admit
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY
    from .test_request_coverage import envelope

    configured = Config.model_validate({"identity": {"enabled": False}, "request_resources": [RESOURCE]})
    runtime = make_runtime(configured, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        create_app(runtime.gateway)
        route = runtime.gateway.routes.match("/openai/v1/models/approved-model")
        assert route is not None
        approved = admit(inventory(envelope(path=b"/openai/v1/models/approved-model")), route)
        assert approved.profile.id == "openai.model.1"
        runtime.apply_config(Config(identity=IdentityConfig(enabled=False)), save=True)
        current = runtime.gateway.routes.match("/openai/v1/models/approved-model")
        with pytest.raises(EnvelopeRefused):
            admit(inventory(envelope(path=b"/openai/v1/models/approved-model")), current)
        # The request already holding its immutable route keeps that snapshot; P2-6 owns policy revocation.
        assert admit(inventory(envelope(path=b"/openai/v1/models/approved-model")), route)
        assert load_config(runtime.config_file).model_dump(mode="json").get("request_resources") == []
    finally:
        runtime.close()


def test_azure_deployment_and_version_are_exact_configured_input():
    from latet.config import Config
    from latet.gateway.coverage import EnvelopeRefused, inventory
    from latet.gateway.request_profiles import admit
    from latet.gateway.routes import RouteTable, destination_upstream

    from .test_request_coverage import envelope

    destination = Destination(
        name="azure",
        url="https://azure.invalid/openai/deployments/approved-deployment",
        request_profile="azure-openai-1",
    )
    row = {"destination": "azure", "url": destination.url, "role": "api-version", "value": "2024-10-21"}
    configured = Config.model_validate({"upstreams": [destination], "request_resources": [row]})
    rows = tuple((r.destination, r.url, r.role, r.value) for r in configured.request_resources)
    route = RouteTable([destination_upstream(destination)], rows).match("/azure/chat/completions")
    from dataclasses import replace

    came = replace(
        envelope(path=b"/azure/chat/completions", query=b"api-version=2024-10-21"), method="POST", destination="azure"
    )
    assert admit(inventory(came), route).profile.family == "openai"
    with pytest.raises(EnvelopeRefused):
        admit(inventory(replace(came, raw_path=b"/azure/another-deployment/chat/completions")), route)
    with pytest.raises(EnvelopeRefused):
        admit(inventory(replace(came, query=b"api-version=private-version")), route)


def test_manifest_matches_the_active_envelope_revision_and_legacy_body_boundary():
    import json
    from pathlib import Path

    from latet.gateway import request_profiles
    from latet.gateway.coverage import LIMITS

    manifest = json.loads((Path(__file__).parents[1] / "data/request-coverage/profiles.json").read_text("utf-8"))[
        "envelope"
    ]
    assert manifest["revision"] == request_profiles.REVISION
    assert set(manifest["regions"]) == request_profiles.BEDROCK_REGIONS
    assert set(manifest["locations"]) == request_profiles.VERTEX_LOCATIONS
    assert manifest["limits"]["raw_target"] == LIMITS.raw_target
    assert manifest["limits"]["header_bytes"] == LIMITS.header_bytes
    assert all(row["body"].startswith("legacy-0.10") for row in manifest["profiles"])


@pytest.mark.parametrize(
    ("family", "method", "target", "resources"),
    [
        ("anthropic", "POST", "/v1/messages", ()),
        ("anthropic", "POST", "/v1/messages/count_tokens?beta=true", ()),
        ("anthropic", "GET", "/v1/models/approved", (("model", "approved"),)),
        ("openai", "POST", "/v1/chat/completions", ()),
        ("openai", "POST", "/v1/completions", ()),
        ("openai", "POST", "/v1/responses", ()),
        ("openai", "POST", "/v1/embeddings", ()),
        ("openai", "GET", "/v1/models/approved", (("model", "approved"),)),
        ("openrouter", "POST", "/api/v1/chat/completions", ()),
        ("gemini", "POST", "/v1beta/models/approved:generateContent", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/models/approved%3astreamGenerateContent?alt=sse", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/models/approved:countTokens", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/models/approved:embedContent", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/models/approved:batchEmbedContents", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/cachedContents", ()),
        ("gemini", "PATCH", "/v1beta/cachedContents/approved", (("cache", "approved"),)),
        ("gemini", "GET", "/v1beta/models/approved", (("model", "approved"),)),
        ("gemini", "POST", "/v1beta/openai/chat/completions", ()),
    ],
)
def test_each_inference_profile_has_an_exact_positive_and_uncovered_sibling(family, method, target, resources):
    from dataclasses import replace

    from latet.gateway.coverage import EnvelopeRefused, inventory
    from latet.gateway.request_profiles import admit

    from .test_request_coverage import envelope

    upstream = Upstream(family, "/" + family, "https://provider.invalid", envelope_expected=frozenset(resources))
    path, _, query = ("/" + family + target).partition("?")
    came = replace(envelope(path=path.encode(), query=query.encode()), method=method, destination=family)
    assert admit(inventory(came), upstream).profile.method == method
    with pytest.raises(EnvelopeRefused):
        admit(inventory(replace(came, query=(query + "&" if query else "").encode() + b"uncovered=canary")), upstream)


def test_request_resource_row_and_utf8_caps_at_exact_boundary_and_one_beyond():
    from latet.config import Config

    rows = [{**RESOURCE, "value": f"resource-{number}"} for number in range(128)]
    assert len(Config.model_validate({"request_resources": rows}).request_resources) == 128
    with pytest.raises(ValidationError):
        Config.model_validate({"request_resources": [*rows, {**RESOURCE, "value": "resource-129"}]})
    token = {**RESOURCE, "role": "pageToken", "value": "\U0001f642" * 512}
    assert len(Config.model_validate({"request_resources": [token]}).request_resources[0].value.encode()) == 2048
    with pytest.raises(ValidationError):
        Config.model_validate({"request_resources": [{**token, "value": token["value"] + "a"}]})


@pytest.mark.parametrize("escape", ["%3A", "%3a"])
def test_signed_bedrock_profile_preserves_escaped_arn_and_removes_control_route(escape, caplog):
    from urllib.parse import unquote

    from latet.gateway.aws import profile_tag

    from .test_request_transport import TOKEN, _Aws

    wire_model = (
        f"arn{escape}aws{escape}bedrock{escape}us-east-1{escape}123456789012{escape}inference-profile%2Fapproved"
    )
    upstream = BedrockUpstream(envelope_expected=frozenset({("model", unquote(wire_model))}))
    key = profile_tag(TOKEN, "work")
    target = f"/bedrock/us-east-1/profile/work/{key}/model/{wire_model}/invoke"
    with gateway(upstream=upstream, aws=_Aws(), bridge_token=TOKEN) as (client, transport, made, _):
        response = client.post(target, json={"messages": [{"role": "user", "content": "hello"}], "max_tokens": 1})
        assert response.status_code == 200
        assert counts(made) == (1, 1)
        assert transport.sent[0].raw_path == f"/model/{wire_model}/invoke".encode()
        assert key not in repr(transport.sent[0]) and key not in caplog.text and TOKEN not in caplog.text
        assert dict(transport.sent[0].headers)[b"authorization"].startswith(b"AWS4-HMAC-SHA256 ")


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("us", "aiplatform.us.rep.googleapis.com"),
        ("eu", "aiplatform.eu.rep.googleapis.com"),
        ("global", "aiplatform.googleapis.com"),
    ],
)
def test_vertex_enumerated_location_keeps_the_documented_host_mapping(location, host):
    resources = frozenset({("model", "approved"), ("project", "approved-project")})
    upstream = VertexUpstream(envelope_expected=resources)
    target = (
        f"/vertex/v1/projects/approved-project/locations/{location}/publishers/anthropic/models/approved:rawPredict"
    )
    with gateway(upstream=upstream) as (client, transport, made, _):
        assert client.post(target, json={"messages": [{"role": "user", "content": "hello"}]}).status_code == 200
        assert counts(made) == (1, 1)
        assert transport.sent[0].host == host
