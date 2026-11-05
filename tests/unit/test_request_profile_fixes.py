"""P2-1 review regressions: public status semantics, exact company clouds, configured credential profiles."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from starlette.requests import Request

from latet.config import Destination
from latet.gateway import forward
from latet.gateway.capture import Capture
from latet.gateway.cloud import BedrockUpstream
from latet.gateway.routes import RouteTable, Upstream, destination_upstream

from ..request_recorder import RecordingClient, RecordingTransport
from .test_request_coverage import counts, gateway
from .test_request_outcomes import _mediator


def manifest_envelope():
    from pathlib import Path

    return json.loads((Path(__file__).parents[1] / "data/request-coverage/profiles.json").read_text("utf-8"))[
        "envelope"
    ]


@pytest.mark.parametrize(
    ("upstream", "target", "url"),
    [
        (Upstream("openai", "/openai", "https://api.openai.com"), "/openai/v1/models", "https://api.openai.com"),
        (
            Upstream("anthropic", "/anthropic", "https://api.anthropic.com"),
            "/anthropic/v1/models",
            "https://api.anthropic.com",
        ),
        (
            Upstream("chatgpt", "/backend-api/codex", "https://chatgpt.com", keep_prefix=True),
            "/backend-api/codex/models?client_version=0.162.0",
            "https://chatgpt.com",
        ),
        (
            Upstream("gemini", "/gemini", "https://generativelanguage.googleapis.com"),
            "/gemini/v1beta/models",
            "https://generativelanguage.googleapis.com",
        ),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles", "https://bedrock.us-east-1.amazonaws.com"),
    ],
    ids=["openai", "anthropic", "chatgpt", "gemini", "bedrock"],
)
def test_api_version_expectation_never_grants_an_unrelated_api_key_header(upstream, target, url):
    resources = ((upstream.name, url, "api-version", "2024-10-21"),)
    with gateway(upstream=upstream, request_resources=resources) as (client, transport, made, _):
        response = client.get(target, headers={"api-key": "opaque-unapproved-carrier"})
        assert response.status_code == 400
        assert counts(made) == (0, 0) and transport.sent == []
        assert "opaque-unapproved-carrier" not in response.text


def test_api_version_expectation_does_not_declare_a_query_role_on_standard_openai():
    resources = (("openai", "https://api.openai.com", "api-version", "2024-10-21"),)
    with gateway(request_resources=resources) as (client, transport, made, _):
        assert client.get("/openai/v1/models?api-version=2024-10-21").status_code == 400
        assert counts(made) == (0, 0) and not transport.sent


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize(
    "operation", ["invoke", "invoke-with-response-stream", "count-tokens", "rawPredict", "streamRawPredict"]
)
def test_generic_company_cloud_ingress_preserves_native_verified_bytes(server, operation, monkeypatch, request):
    upstream = Upstream("company", "/company", "https://company.invalid")
    is_vertex = operation in {"rawPredict", "streamRawPredict"}
    if is_vertex:
        target = f"/company/v1/projects/approved-project/locations/us-east5/publishers/anthropic/models/approved-model:{operation}"
        headers = {"x-api-key": "native-company-credential"}
    else:
        target = f"/company/model/approved-model/{operation}"
        headers = {"authorization": "AWS4-HMAC-SHA256 native-company-signature", "x-amz-date": "20261102T120000Z"}
    inner = b'{ "anthropic_version": "bedrock-2023-05-31", "max_tokens": 1, "messages": [{"role":"user","content":"hello"}] }'
    body = (
        json.dumps({"input": {"invokeModel": {"body": base64.b64encode(inner).decode()}}}, indent=2).encode()
        if operation == "count-tokens"
        else inner
    )
    resources = (
        ("company", upstream.base_url, "model", "approved-model"),
        ("company", upstream.base_url, "project", "approved-project"),
    )
    with gateway(server=server, upstream=upstream, request_resources=resources) as (client, transport, made, mediator):
        seen = []
        original = mediator.prepare_http

        def prepared(*args, **kwargs):  # prepare_http's own arguments, the launch's (``launch=``) among them
            result = original(*args, **kwargs)
            seen.append((result.mode, result.shape))
            return result

        monkeypatch.setattr(mediator, "prepare_http", prepared)
        response = client.post(target, content=body, headers={"content-type": "application/json", **headers})
        assert response.status_code == 200
        profile_id = f"company.{'vertex' if is_vertex else 'bedrock'}.{operation}.1"
        entry = next(row for row in manifest_envelope()["profiles"] if row["id"] == profile_id)
        assert request.node.nodeid in entry["fixture_ids"]
        assert counts(made) == (1, 1)
        assert seen == [
            ("verify", "vertex" if is_vertex else "bedrock-count" if operation == "count-tokens" else "bedrock")
        ]
        assert transport.sent[0].raw_path == target.removeprefix("/company").encode()
        assert transport.sent[0].host == "company.invalid"
        assert transport.sent[0].content == body
        for name, value in headers.items():
            assert (name.encode(), value.encode()) in transport.sent[0].headers
        for bad_target, extra in [
            (target + "?uncovered=canary", {}),
            (target, {"x-uncovered": "canary"}),
            (target.replace("approved-model", "unapproved-model"), {}),
        ]:
            refusal = client.post(
                bad_target, content=body, headers={"content-type": "application/json", **headers, **extra}
            )
            assert refusal.status_code == 400
            assert refusal.json()["error"]["type"] == "invalid_request_error"
            if not is_vertex:
                assert refusal.json()["message"] == refusal.json()["error"]["message"]
        if is_vertex:
            assert (
                client.post(target.replace("us-east5", "private-canary1"), content=body, headers=headers).status_code
                == 400
            )
            assert (
                client.post(
                    target.replace("approved-project", "unapproved-project"), content=body, headers=headers
                ).status_code
                == 400
            )
        assert counts(made) == (1, 1) and len(transport.sent) == 1


async def test_nonlimit_preflight_encoding_refusal_is_400_in_provider_shape():
    mediator = _mediator()
    transport = RecordingTransport(lambda request: httpx.Response(200))

    async def unread():
        raise AssertionError("preflight refusal must not read body")

    scope = {"type": "http", "method": "GET", "path": "/anthropic/\ud800", "query_string": b"", "headers": []}
    try:
        async with RecordingClient(transport) as client:
            response = await forward.Forwarder(RouteTable(), Capture(None), client, mediator).handle(
                Request(scope, unread)
            )
            assert response.status_code == 400
            assert json.loads(response.body)["error"]["type"] == "invalid_request_error"
            assert client.builds == client.sends == 0
    finally:
        mediator.close()


def test_envelope_categories_have_explicit_statuses_independent_of_refusal_words():
    from latet.gateway.coverage import ENVELOPE_STATUSES, EnvelopeRefused
    from latet.gateway.mediate import VERIFY, Prepared
    from latet.gateway.outcome import decide

    from .test_request_outcomes import _envelope

    assert ENVELOPE_STATUSES["coverage-limit"] == 422
    assert ENVELOPE_STATUSES["envelope-state-unavailable"] == 503
    assert all(
        status == 400
        for code, status in ENVELOPE_STATUSES.items()
        if code not in {"coverage-limit", "envelope-state-unavailable"}
    )
    for code, status in ENVELOPE_STATUSES.items():
        assert EnvelopeRefused(code).status == status
    arena = Prepared(
        VERIFY,
        "chat",
        b"",
        block_reason="wording changes; coverage-limit is just text here",
        envelope_refusal="envelope-sensitive",
    )
    decision = decide(arena, _envelope(b""))
    assert (decision.code, decision.status) == ("envelope-sensitive", 400)
    ordinary = decide(
        Prepared(VERIFY, "chat", b"", block_reason="coverage-limit appears in ordinary refusal wording"), _envelope(b"")
    )
    assert (ordinary.code, ordinary.status) == ("refused", 400)


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize(
    ("selector", "url"),
    [
        ("azure-openai-1", "https://azure.invalid/openai/deployments/approved-deployment"),
        ("foundry-1", "https://foundry.invalid/models"),
    ],
    ids=["azure", "foundry"],
)
def test_explicit_destination_profile_owns_api_key_and_exact_query_roles(server, selector, url, request):
    destination = Destination(name="company", url=url, request_profile=selector)
    upstream = destination_upstream(destination)
    resources = (("company", url, "api-version", "2024-10-21"),)
    body = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
    headers = {"api-key": "opaque-approved-destination-credential", "content-type": "application/json"}
    with gateway(server=server, upstream=upstream, request_resources=resources) as (client, transport, made, _):
        assert (
            client.post("/company/chat/completions?api-version=2024-10-21", content=body, headers=headers).status_code
            == 200
        )
        entry = next(
            row
            for row in manifest_envelope()["destination_profile_selectors"]["profiles"]
            if row["selector"] == selector
        )
        assert request.node.nodeid in entry["fixture_ids"]
        assert transport.sent[0].host == ("azure.invalid" if selector == "azure-openai-1" else "foundry.invalid")
        assert (
            transport.sent[0].raw_path
            == (url.partition(".invalid")[2] + "/chat/completions?api-version=2024-10-21").encode()
        )
        assert (b"api-key", headers["api-key"].encode()) in transport.sent[0].headers
        for target in [
            "/company/chat/completions",
            "/company/chat/completions?api-version=unapproved",
            "/company/another-deployment/chat/completions?api-version=2024-10-21",
        ]:
            assert client.post(target, content=body, headers=headers).status_code == 400
        assert (
            client.post(
                "/company/chat/completions?api-version=2024-10-21",
                content=body,
                headers={**headers, "x-unpublished": "canary"},
            ).status_code
            == 400
        )
        assert counts(made) == (1, 1) and len(transport.sent) == 1
    # Same destination/base URL/resource settings, default profile: no credential authority inferred from any of them.
    with gateway(upstream=destination_upstream(Destination(name="company", url=url)), request_resources=resources) as (
        client,
        _,
        made,
        _,
    ):
        assert (
            client.post(
                "/company/chat/completions", content=body, headers={**headers, "x-latet-request-profile": selector}
            ).status_code
            == 400
        )
        assert counts(made) == (0, 0)


def test_destination_selector_is_closed_and_replaced_as_an_immutable_runtime_snapshot(tmp_path):
    from pydantic import ValidationError

    from latet.config import Config, IdentityConfig, ProjectSettings
    from latet.gateway.app import create_app
    from latet.project_file import REPO_KEYS
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY

    for invalid in ["wildcard", "azure-openai-2", "custom-regex"]:
        with pytest.raises(ValidationError):
            Destination(name="company", url="https://company.invalid/models", request_profile=invalid)
    assert "request_profile" not in ProjectSettings.model_fields and "request_profile" not in REPO_KEYS
    destination = Destination(name="company", url="https://company.invalid/models", request_profile="foundry-1")
    initial = Config(identity=IdentityConfig(enabled=False), upstreams=[destination])
    runtime = make_runtime(initial, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        create_app(runtime.gateway)
        before = runtime.gateway.routes.match("/company/chat/completions")
        assert before.request_profile == "foundry-1"
        runtime.apply_config(
            initial.model_copy(update={"upstreams": [destination.model_copy(update={"request_profile": "standard"})]}),
            save=True,
        )
        after = runtime.gateway.routes.match("/company/chat/completions")
        assert after.request_profile == "standard" and before.request_profile == "foundry-1"
    finally:
        runtime.close()
