"""The clouds' fixed routes: Amazon Bedrock and Google Vertex, whose address follows the region."""

import logging

import pytest

from latet.gateway.cloud import (
    BedrockUpstream,
    Signing,
    VertexUpstream,
    model_of,
    redact_access_log,
    shape_of,
    vertex_host,
)

ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile%2Fabc123"
ENCODED_ARN = "arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Aapplication-inference-profile%2Fabc123"
VERTEX = "/projects/p/locations/{}/publishers/anthropic/models/claude-opus-5-5:streamRawPredict"


def test_bedrock_turns_go_to_the_region_s_runtime():
    path = "/bedrock/us-east-1/model/us.anthropic.claude-sonnet-4-5-20250929-v1:0/invoke-with-response-stream"
    resolved = BedrockUpstream().resolve("POST", path, "")
    assert resolved is not None and resolved.shape == "bedrock" and resolved.region == "us-east-1"
    assert resolved.host == "bedrock-runtime.us-east-1.amazonaws.com"
    assert resolved.url == (
        "https://bedrock-runtime.us-east-1.amazonaws.com"
        "/model/us.anthropic.claude-sonnet-4-5-20250929-v1:0/invoke-with-response-stream"
    )


def test_a_model_arn_reaches_bedrock_as_sent():
    resolved = BedrockUpstream().resolve("POST", f"/bedrock/eu-west-1/model/{ARN}/invoke", "")
    assert resolved is not None
    assert resolved.url == f"https://bedrock-runtime.eu-west-1.amazonaws.com/model/{ARN}/invoke"


def test_the_model_list_goes_to_the_control_plane():
    resolved = BedrockUpstream().resolve("GET", "/bedrock/eu-west-1/inference-profiles", "type=SYSTEM_DEFINED")
    assert resolved is not None and resolved.shape is None
    assert resolved.url == "https://bedrock.eu-west-1.amazonaws.com/inference-profiles?type=SYSTEM_DEFINED"
    one = BedrockUpstream().resolve("GET", f"/bedrock/us-east-1/inference-profiles/{ENCODED_ARN}", "")
    assert one is not None and one.url == f"https://bedrock.us-east-1.amazonaws.com/inference-profiles/{ENCODED_ARN}"


def test_a_token_count_is_its_own_shape():
    resolved = BedrockUpstream().resolve("POST", "/bedrock/us-east-1/model/x/count-tokens", "")
    assert resolved is not None and resolved.shape == "bedrock-count"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/bedrock/evil.com/model/x/invoke"),
        ("POST", "/bedrock/us-east-1.x/model/x/invoke"),
        ("POST", "/bedrock/model/x/invoke"),
        ("POST", "/bedrock/us-east-1/model/x/converse"),
        ("POST", "/bedrock/us-east-1/model/x/converse-stream"),
        ("GET", "/bedrock/us-east-1/model/x/invoke"),
        ("POST", "/bedrock/us-east-1/inference-profiles"),
        ("POST", "/bedrock/us-east-1/model/a/b/invoke"),
    ],
)
def test_what_latet_cannot_read_has_no_route(method, path):
    assert BedrockUpstream().resolve(method, path, "") is None


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("global", "aiplatform.googleapis.com"),
        ("us", "aiplatform.us.rep.googleapis.com"),
        ("eu", "aiplatform.eu.rep.googleapis.com"),
        ("us-east5", "us-east5-aiplatform.googleapis.com"),
        ("europe-west1", "europe-west1-aiplatform.googleapis.com"),
    ],
)
def test_vertex_goes_to_the_request_s_location(location, host):
    assert vertex_host(location) == host
    resolved = VertexUpstream().resolve("POST", "/vertex/v1" + VERTEX.format(location), "")
    assert resolved is not None and resolved.shape == "vertex" and resolved.host == host
    assert resolved.url == f"https://{host}/v1" + VERTEX.format(location)


def test_vertex_adds_the_api_version_when_the_base_lacks_it():
    resolved = VertexUpstream().resolve("POST", "/vertex" + VERTEX.format("global"), "")
    assert resolved is not None and resolved.url == "https://aiplatform.googleapis.com/v1" + VERTEX.format("global")


def test_vertex_counts_tokens_too():
    path = "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/count-tokens:rawPredict"
    assert VertexUpstream().resolve("POST", path, "") is not None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/vertex/v1/projects/p/locations/evil.com/publishers/anthropic/models/m:rawPredict"),
        ("POST", "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/m:predict"),
        ("POST", "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/m:countTokens"),
        ("GET", "/vertex/v1" + VERTEX.format("us-east5")),
        ("POST", "/vertex/v1beta1/projects/p/locations/evil.com/publishers/google/models/g:generateContent"),
        ("POST", "/vertex/v2/publishers/google/models/g:generateContent"),
        ("GET", "/vertex"),
    ],
)
def test_vertex_routes_only_claudes_two_calls_and_googles_own(method, path):
    assert VertexUpstream().resolve(method, path, "") is None


GEMINI = "/projects/p/locations/{}/publishers/google/models/gemini-3-pro:streamGenerateContent"


@pytest.mark.parametrize(
    ("method", "path", "query", "url"),
    [
        (
            "POST",
            "/vertex/v1beta1" + GEMINI.format("us-central1"),
            "alt=sse",
            "https://us-central1-aiplatform.googleapis.com/v1beta1" + GEMINI.format("us-central1") + "?alt=sse",
        ),
        (
            "POST",
            "/vertex/v1" + GEMINI.format("global"),
            "",
            "https://aiplatform.googleapis.com/v1" + GEMINI.format("global"),
        ),
        (
            "POST",
            "/vertex/v1beta1/publishers/google/models/g:generateContent",
            "",
            "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/g:generateContent",
        ),
        (
            "GET",
            "/vertex/v1beta1/projects/p/locations/us/publishers/google/models",
            "",
            "https://aiplatform.us.rep.googleapis.com/v1beta1/projects/p/locations/us/publishers/google/models",
        ),
        (
            "POST",
            "/vertex/v1/projects/p/locations/europe-west4/publishers/google/models/i:predict",
            "",
            "https://europe-west4-aiplatform.googleapis.com/v1/projects/p/locations/europe-west4/publishers/google/models/i:predict",
        ),
    ],
)
def test_vertex_routes_googles_own_calls_to_their_location(method, path, query, url):
    resolved = VertexUpstream().resolve(method, path, query)
    assert resolved is not None and resolved.url == url and resolved.shape is None


def test_vertex_sends_googles_calls_to_the_tests_fake_too():
    resolved = VertexUpstream(base="http://127.0.0.1:9").resolve("POST", "/vertex/v1beta1" + GEMINI.format("us"), "")
    assert resolved is not None and resolved.url == "http://127.0.0.1:9/v1beta1" + GEMINI.format("us")


def test_vertex_refuses_what_it_does_not_read():
    assert VertexUpstream().refuses_unread


@pytest.mark.parametrize(
    ("method", "path", "shape"),
    [
        ("POST", "/bedrock/us-east-1/model/x/invoke", "bedrock"),
        ("POST", "/llm-acme-com/model/x/invoke-with-response-stream", "bedrock"),
        ("POST", f"/llm-acme-com/model/{ARN}/invoke", "bedrock"),
        ("POST", "/bedrock/us-east-1/model/x/count-tokens", "bedrock-count"),
        ("POST", "/gw/vertex_ai/v1" + VERTEX.format("global"), "vertex"),
        ("POST", "/anthropic/v1/messages", None),
        ("GET", "/bedrock/us-east-1/model/x/invoke", None),
    ],
)
def test_a_cloud_request_is_known_by_its_path_on_any_route(method, path, shape):
    assert shape_of(method, path) == shape


def test_the_model_is_in_the_path():
    assert model_of("/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/invoke") == "us.anthropic.claude-opus-5-5"
    assert model_of(f"/bedrock/us-east-1/model/{ARN}/invoke") == ARN.replace("%2F", "/")
    assert model_of("/vertex/v1" + VERTEX.format("eu")) == "claude-opus-5-5"
    assert model_of("/anthropic/v1/messages") is None


KEY = "0123456789abcdef0123456789abcdef"


def test_a_signed_route_names_its_profile_and_its_key():
    resolved = BedrockUpstream().resolve("POST", f"/bedrock/eu-west-1/profile/work/{KEY}/model/x/invoke", "")
    assert resolved is not None and resolved.signing == Signing("eu-west-1", profile="work", key=KEY)
    assert resolved.url == "https://bedrock-runtime.eu-west-1.amazonaws.com/model/x/invoke"
    assert resolved.path == "/bedrock/eu-west-1/model/x/invoke"  # what a record keeps: no key


def test_a_session_route_names_its_session():
    resolved = BedrockUpstream().resolve("GET", f"/bedrock/eu-west-1/session/{KEY}/inference-profiles", "")
    assert resolved is not None and resolved.signing == Signing("eu-west-1", session=KEY)
    assert resolved.url == "https://bedrock.eu-west-1.amazonaws.com/inference-profiles"
    assert resolved.path == "/bedrock/eu-west-1/inference-profiles"


@pytest.mark.parametrize(
    "path",
    [
        "/bedrock/eu-west-1/profile/work/short/model/x/invoke",
        f"/bedrock/eu-west-1/profile/a%20b/{KEY}/model/x/invoke",
        f"/bedrock/eu-west-1/profile/work/{KEY.upper()}/model/x/invoke",
        "/bedrock/eu-west-1/session/xyz/model/x/invoke",
        f"/bedrock/eu-west-1/profile/work/{KEY}/model/x/converse",
    ],
)
def test_a_malformed_signed_route_has_no_route(path):
    assert BedrockUpstream().resolve("POST", path, "") is None


def test_the_access_log_never_holds_a_signing_key_nor_a_session(caplog):
    redact_access_log()
    redact_access_log()  # once is enough, twice changes nothing
    access = logging.getLogger("uvicorn.access")
    line = '%s - "%s %s HTTP/%s" %d'
    paths = (
        f"/bedrock/eu-west-1/profile/work/{'a' * 32}/model/x/invoke",
        f"/bedrock/eu-west-1/session/{'f' * 32}/model/x/invoke",
        f"/_latet/engine/v1/aws-sessions/{'e' * 32}/hold",  # a session is held for as long as it lives
        f"/_latet/engine/v1/aws-sessions/{'d' * 32}",
        "/anthropic/v1/messages?beta=true",
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for path in paths:
            access.info(line, "127.0.0.1:5000", "POST", path, "1.1", 200)
    assert all(letter * 32 not in caplog.text for letter in "afed")
    assert "/bedrock/eu-west-1/profile/work/" in caplog.text and "/model/x/invoke" in caplog.text
    assert "/_latet/engine/v1/aws-sessions/***/hold" in caplog.text
    assert "/_latet/engine/v1/aws-sessions/***" in caplog.text
    assert "/anthropic/v1/messages?beta=true" in caplog.text
