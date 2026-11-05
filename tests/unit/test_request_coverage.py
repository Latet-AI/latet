"""Strict HTTP envelope behavior, through the production forwarder and transport."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.cloud import BedrockUpstream, VertexUpstream
from latet.gateway.routes import Upstream

from ..request_recorder import RecordingClient, RecordingTransport
from .test_request_outcomes import _mediator
from .test_request_transport import _Answer


@contextmanager
def gateway(*, server: bool = False, upstream: Upstream | None = None, status: int = 200, **options: Any):
    mediator = _mediator()
    mediator.on_server = server
    transport = RecordingTransport(
        lambda request: httpx.Response(
            status, headers={"content-type": "application/json"}, stream=_Answer(b'{"data":[]}')
        )
    )
    made: list[RecordingClient] = []

    def client():
        made.append(RecordingClient(transport))
        return made[-1]

    configured = GatewayConfig(
        port=5283,
        upstreams=[upstream or Upstream("openai", "/openai", "https://api.openai.com")],
        mediator=mediator,
        client=client,
        **options,
    )
    try:
        with TestClient(create_app(configured), base_url="http://127.0.0.1:5283") as inbound:
            yield inbound, transport, made, mediator
    finally:
        mediator.close()


def counts(made):
    return sum(client.builds for client in made), sum(client.sends for client in made)


@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize(
    ("target", "headers"),
    [
        ("/openai/v1/models?private-mariorossi=1", []),
        ("/openai/v1/models?limit=private-mariorossi", []),
        ("/openai/v1/models", [("x-private-mariorossi", "unique-secret-value")]),
    ],
)
def test_bodyless_get_blocks_query_name_value_and_header_canaries(server, target, headers):
    with gateway(server=server) as (client, transport, made, _):
        response = client.get(target, headers=headers)
        assert response.status_code == 400
        assert counts(made) == (0, 0)
        assert transport.sent == []


@pytest.mark.parametrize(
    "headers",
    [
        [("authorization", "Bearer first"), ("authorization", "Bearer second")],
        [("authorization", "Bearer same"), ("authorization", "Bearer same")],
        [("content-type", "application/json"), ("content-type", "text/plain")],
        [("content-encoding", "gzip"), ("content-encoding", "identity")],
    ],
)
def test_duplicate_query_and_header_occurrences_are_covered(headers):
    with gateway() as (client, transport, made, _):
        response = client.get("/openai/v1/models", headers=headers)
        assert response.status_code == 400
        assert counts(made) == (0, 0)
        assert not transport.sent


def test_an_unnamed_query_name_refuses():
    with gateway() as (client, _, made, _):
        response = client.get("/openai/v1/models?=blank&limit=1&limit=2")
        assert response.status_code == 400
        assert counts(made) == (0, 0)


def test_an_unnamed_header_is_removed_only_where_its_family_is_declared_removable():
    with gateway() as (client, transport, _, _):
        response = client.get("/openai/v1/models", headers={"x-stainless-future-observation": "must-go-away"})
        assert response.status_code == 200
        assert all(name.lower() != b"x-stainless-future-observation" for name, _ in transport.sent[0].headers)


def test_an_unnamed_header_elsewhere_refuses_naming_it():
    name = "x-unpublished-envelope-canary"
    with gateway() as (client, _, made, _):
        response = client.get("/openai/v1/models", headers={name: "not-to-be-mentioned"})
        assert response.status_code == 400
        assert name in response.text
        assert "not-to-be-mentioned" not in response.text
        assert counts(made) == (0, 0)


def test_a_refused_header_name_reaches_the_agent_only(caplog):
    name = "x-private-header-name-canary"
    value = "private-header-value-canary"
    with gateway() as (client, _, made, _):
        response = client.get("/openai/v1/models", headers={name: value})
        assert response.status_code == 400
        assert name in response.text and value not in response.text
        assert name not in caplog.text and value not in caplog.text
        assert "envelope-header-unknown" in caplog.text
        assert counts(made) == (0, 0)


@pytest.mark.parametrize(
    ("upstream", "target"),
    [
        (BedrockUpstream(), "/bedrock/us-mariorossi-acme-1/inference-profiles"),
        (VertexUpstream(), "/vertex/v1/projects/fixture-project/locations/mariorossi-acme1/publishers/google/models"),
    ],
)
def test_a_canary_region_or_location_blocks_before_resolve(monkeypatch, upstream, target):
    calls = []
    resolve = type(upstream).resolve

    def watched(self, *args):
        calls.append(args)
        return resolve(self, *args)

    monkeypatch.setattr(type(upstream), "resolve", watched)
    with gateway(upstream=upstream) as (client, _, made, _):
        response = client.get(target)
        assert response.status_code == 400
        assert calls == []
        assert counts(made) == (0, 0)


def test_a_bodyless_request_is_checked_with_a_route_pseudo_session_and_records_nothing(monkeypatch):
    seen = []
    with gateway() as (client, _, _, mediator):
        verify = mediator.verifier.verify

        def checked(slots, session_key, *args, **kwargs):
            slots = list(slots)
            seen.append((slots, session_key, kwargs.get("record")))
            return verify(slots, session_key, *args, **kwargs)

        monkeypatch.setattr(mediator.verifier, "verify", checked)
        response = client.get("/openai/v1/models", headers={"user-agent": "envelope-test-agent"})
        assert response.status_code == 200
        assert seen, "a bodyless request still checks its envelope"
        assert any("envelope-test-agent" in text for slots, _, _ in seen for text, _, _ in slots)
        assert all(key.startswith("route|") and record is False for _, key, record in seen)


def test_free_text_envelope_values_are_checked_not_inspected():
    with gateway() as (client, _, made, _):
        response = client.get("/openai/v1/models", headers={"user-agent": "contact emily.carter@acme.com"})
        assert response.status_code == 400
        assert "emily.carter@acme.com" not in response.text
        assert counts(made) == (0, 0)


def test_transport_cannot_change_the_proved_destination(monkeypatch):
    original = RecordingClient.build_request

    def changed(self, *args, **kwargs):
        request = original(self, *args, **kwargs)
        request.url = request.url.copy_with(host="unproved.invalid")
        return request

    monkeypatch.setattr(RecordingClient, "build_request", changed)
    with gateway() as (client, transport, _, _):
        response = client.get("/openai/v1/models")
        assert response.status_code >= 400
        assert transport.sent == []


def test_final_generated_headers_obey_bounds_before_build(monkeypatch):
    from dataclasses import replace

    from latet.gateway import forward
    from latet.gateway.coverage import LIMITS, inventory

    # The provider Host is longer than the incoming loopback Host. Its generated bytes count too.
    upstream = Upstream("openai", "/openai", "https://very-long-configured-provider.example")
    with gateway(upstream=upstream) as (client, transport, made, _):
        request = client.build_request("GET", "/openai/v1/models")
        size = sum(len(k) + len(v) + 4 for k, v in request.headers.raw)
        monkeypatch.setattr(
            forward, "inventory", lambda envelope: inventory(envelope, replace(LIMITS, header_bytes=size))
        )
        response = client.get("/openai/v1/models")
        assert response.status_code == 422
        assert counts(made) == (0, 0)
        assert transport.sent == []


def envelope(path=b"/openai/v1/models", query=b"", headers=()):
    from latet.gateway.coverage import Envelope

    return Envelope("http", "GET", path, query, headers, b"", "openai")


@pytest.mark.parametrize(
    "resource", ["raw_target", "query_occurrences", "header_occurrences", "header_bytes", "ledger_bytes", "occurrences"]
)
def test_inventory_exact_bound_and_one_beyond(resource):
    from dataclasses import replace

    from latet.gateway.coverage import LIMITS, EnvelopeRefused, inventory

    if resource == "raw_target":
        good, bad, cap = envelope(path=b"/" + b"a" * 15), envelope(path=b"/" + b"a" * 16), 16
    elif resource == "query_occurrences":
        good, bad, cap = envelope(query=b"a=1&b=2&c=3"), envelope(query=b"a=1&b=2&c=3&"), 3
    elif resource == "header_occurrences":
        good, bad, cap = envelope(headers=((b"a", b"1"),) * 3), envelope(headers=((b"a", b"1"),) * 4), 3
    elif resource == "header_bytes":
        good, bad, cap = envelope(headers=((b"a", b"1"),)), envelope(headers=((b"a", b"12"),)), 6
    else:
        good, bad = envelope(path=b"/a"), envelope(path=b"/a/")
        count = len(inventory(good).entries)
        cap = count * 256 if resource == "ledger_bytes" else count
    limits = replace(LIMITS, **{resource: cap})
    assert inventory(good, limits).envelope is good
    with pytest.raises(EnvelopeRefused, match="coverage-limit"):
        inventory(bad, limits)


def test_raw_target_does_not_share_proof_after_normalization():
    from dataclasses import replace

    from latet.gateway.coverage import EnvelopeProof, inventory
    from latet.gateway.request_profiles import admit

    original = envelope(path=b"/openai/v1/mod%65ls")
    approved = admit(inventory(original), Upstream("openai", "/openai", "https://api.openai.com"))
    proof = EnvelopeProof(
        original,
        len(approved.inventory.entries),
        approved.profile.id,
        approved.treatments,
        approved.headers,
        "https://api.openai.com/v1/mod%65ls",
        approved.checked,
    )
    assert proof.proves(original)
    assert not proof.proves(replace(original, raw_path=b"/openai/v1/models"))
    assert not proof.proves(replace(original))
    assert not replace(proof, treatments=proof.treatments[:-1]).proves(original)


@pytest.mark.parametrize(
    ("path", "query"),
    [
        (b"/openai/v1/models/%2fprivate-canary", b""),
        (b"/openai/v1/models/%2Fprivate-canary", b""),
        (b"/openai/v1/models/%3Aprivate-canary", b""),
        (b"/openai/v1/%2e%2e/models", b""),
        (b"/openai/v1/mod%GGels", b""),
        (b"/openai/v1/models", b"private+name=private+value"),
        (b"/openai/v1/models", b"%E2%82%AC=secret"),
        (b"/openai/v1/models", b"limit=%FF"),
        (b"/openai/v1/models", b"limit=%"),
        (b"/openai/v1/models", b"=blank"),
    ],
)
def test_raw_canaries_and_ambiguous_encodings_have_no_profile(path, query):
    from latet.gateway.coverage import EnvelopeRefused, inventory
    from latet.gateway.request_profiles import admit

    with pytest.raises(EnvelopeRefused):
        admit(inventory(envelope(path, query)), Upstream("openai", "/openai", "https://api.openai.com"))


def test_control_credentials_are_removed_provider_credentials_destination_bound():
    provider = "Bearer opaque-provider-credential"
    controls = {"x-latet-token": "synthetic-admin-token", "x-latet-project": "synthetic-route-token"}
    with gateway() as (client, transport, _, _):
        response = client.get("/openai/v1/models", headers={"authorization": provider, **controls})
        assert response.status_code == 200
        sent = transport.sent[0].headers
        assert (b"authorization", provider.encode()) in sent
        assert all(not name.lower().startswith(b"x-latet-") for name, _ in sent)
        assert all(value.encode() not in repr(sent).encode() for value in controls.values())
    with gateway() as (client, _, made, _):
        assert client.get("/openai/v1/models", headers={"x-goog-api-key": "foreign-provider-key"}).status_code == 400
        assert counts(made) == (0, 0)


def test_removable_telemetry_never_reaches_verifier_or_provider(monkeypatch):
    with gateway() as (client, transport, _, mediator):
        seen = []
        verify = mediator.verifier.verify

        def checked(slots, *args, **kwargs):
            slots = list(slots)
            seen.extend(text for text, _, _ in slots)
            return verify(slots, *args, **kwargs)

        monkeypatch.setattr(mediator.verifier, "verify", checked)
        assert (
            client.get("/openai/v1/models", headers={"x-stainless-unpublished": "secret-telemetry-canary"}).status_code
            == 200
        )
        assert all("secret-telemetry-canary" not in text for text in seen)
        assert all(value != b"secret-telemetry-canary" for _, value in transport.sent[0].headers)


@pytest.mark.parametrize("status", [401, 403])
def test_removal_is_counted_and_provider_auth_failure_explains_it(status):
    with gateway(status=status) as (client, _, _, mediator):
        response = client.get("/openai/v1/models", headers={"x-stainless-unpublished": "removed-canary"})
        assert response.status_code == status
        assert "Latet removed" in response.text
        assert "removed-canary" not in response.text
        assert getattr(mediator, "envelope_removed_headers", 0) == 1


def test_normal_framing_does_not_claim_a_telemetry_removal_on_auth_failure():
    with gateway(status=401) as (client, _, _, mediator):
        response = client.get("/openai/v1/models")
        assert response.status_code == 401
        assert "Latet removed" not in response.text
        assert getattr(mediator, "envelope_removed_headers", 0) == 0


@pytest.mark.parametrize(
    ("target", "headers"),
    [
        ("/openai/v1/models?unknown=canary", {}),
        ("/openai/v1/models", {"x-private-header-canary": "private-value"}),
        ("/openai/v1/models", {"user-agent": "contact emily.carter@acme.com"}),
    ],
)
def test_an_uncovered_or_sensitive_envelope_never_calls_the_egress_adapter(target, headers):
    pinned = []

    async def pin(url):
        pinned.append(url)
        raise AssertionError("an envelope refusal must precede pin/DNS/connect")

    upstream = Upstream("openai", "/openai", "https://api.openai.com", own=True)
    with gateway(upstream=upstream, pin=pin) as (client, transport, made, _):
        assert client.get(target, headers=headers).status_code == 400
        assert pinned == [] and transport.sent == [] and counts(made) == (0, 0)


@pytest.mark.parametrize("over", ["target", "header-bytes", "header-count", "query-count"])
async def test_raw_bounds_precede_path_decode_header_copy_and_body_read(monkeypatch, over):
    from starlette.requests import Request

    from latet.gateway import forward
    from latet.gateway.capture import Capture
    from latet.gateway.coverage import LIMITS
    from latet.gateway.routes import RouteTable

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/openai/v1/models",
        "raw_path": b"/openai/v1/models",
        "query_string": b"",
        "headers": [],
    }
    if over == "target":
        scope["raw_path"] = b"/" * (LIMITS.raw_target + 1)
    elif over == "header-bytes":
        scope["headers"] = [(b"x", b"v" * LIMITS.header_bytes)]
    elif over == "header-count":
        scope["headers"] = [(b"x", b"v")] * (LIMITS.header_occurrences + 1)
    else:
        scope["query_string"] = b"&" * LIMITS.query_occurrences

    async def unread():
        raise AssertionError("body read before raw envelope cap")

    def undecoded(scope):
        raise AssertionError("path decoded before raw envelope cap")

    monkeypatch.setattr(forward, "wire_path", undecoded)
    mediator = _mediator()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    try:
        async with RecordingClient(transport) as client:
            forwarder = forward.Forwarder(RouteTable(), Capture(None), client, mediator)
            response = await forwarder.handle(Request(scope, unread))
            assert response.status_code == 422
            assert client.builds == client.sends == 0 and transport.sent == []
    finally:
        mediator.close()


def test_scope_preflight_counts_query_separator_and_quoted_fallback_exactly():
    from dataclasses import replace

    from latet.gateway.coverage import LIMITS, EnvelopeRefused, preflight_scope

    scope = {"path": "/\N{EURO SIGN}", "query_string": b"a=1", "headers": []}
    # /%E2%82%AC is10bytes; '?' plus a=1 is4bytes.
    preflight_scope(scope, replace(LIMITS, raw_target=14))
    with pytest.raises(EnvelopeRefused, match="coverage-limit"):
        preflight_scope(scope, replace(LIMITS, raw_target=13))


def test_absent_mediator_refuses_before_preflight(monkeypatch):
    from latet.gateway import forward
    from latet.gateway.outcome import NotAttempted

    turns = []
    original = forward._Turn

    def turn():
        made = original()
        turns.append(made)
        return made

    monkeypatch.setattr(forward, "_Turn", turn)

    def forbidden(scope):
        raise AssertionError("missing mediation must refuse before preflight")

    monkeypatch.setattr(forward, "preflight_scope", forbidden)
    transport = RecordingTransport(lambda request: httpx.Response(200, stream=_Answer(b"legacy")))
    made = []

    def client():
        made.append(RecordingClient(transport))
        return made[-1]

    config = GatewayConfig(port=5283, mediator=None, client=client)
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as inbound:
        response = inbound.get("/openai/v1/models?legacy=unchanged")
        assert response.status_code == 400 and "request protection is unavailable" in response.text
    assert counts(made) == (0, 0) and transport.sent == []
    assert turns[-1].execution == NotAttempted("proof-incomplete")
