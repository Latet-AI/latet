"""The gateway's routes: the path as sent, routes that change while running, headers that stay here, and
the client requests leave with."""

import json

import httpx
from starlette.testclient import TestClient

from latet.gateway import app
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.forward import upstream_error
from latet.gateway.headers import DROP_PREFIXES, REQUEST_DROP, forwardable
from latet.gateway.routes import DEFAULT_UPSTREAMS, RouteTable, Upstream, readable_path, wire_path

PRETEND = Upstream("anthropic", "/anthropic", "http://pretend-model.invalid")


def test_the_wire_path_is_the_path_as_sent():
    scope = {"path": "/bedrock/model/arn:x/abc/invoke", "raw_path": b"/bedrock/model/arn:x%2Fabc/invoke"}
    assert wire_path(scope) == "/bedrock/model/arn:x%2Fabc/invoke"


def test_without_a_raw_path_the_path_is_encoded_again():
    assert wire_path({"path": "/openai/v1/a b/responses"}) == "/openai/v1/a%20b/responses"


def test_a_target_keeps_the_path_as_sent():
    openai = Upstream("openai", "/openai", "https://api.openai.com")
    assert openai.target("/openai/v1/a%2Fb/responses", "x=1") == "https://api.openai.com/v1/a%2Fb/responses?x=1"


def test_routes_change_while_running():
    routes = RouteTable(DEFAULT_UPSTREAMS)
    acme = Upstream("acme", "/acme", "https://llm.acme.com")
    assert routes.match("/acme/v1/messages") is None
    routes.use([*DEFAULT_UPSTREAMS, acme])
    assert routes.match("/acme/v1/messages") is acme
    assert routes.match("/anthropic/v1/messages").name == "anthropic"
    routes.use(DEFAULT_UPSTREAMS)
    assert routes.match("/acme/v1/messages") is None


def test_latet_headers_are_never_forwarded():
    headers = [
        ("X-Latet-Project", "C:\\Users\\x"),
        ("x-latet-token", "t"),
        ("authorization", "Bearer k"),
        ("Host", "127.0.0.1:5283"),
    ]
    assert forwardable(headers, REQUEST_DROP, DROP_PREFIXES) == [("authorization", "Bearer k")]


def test_errors_take_the_shape_their_client_reads():
    anthropic = json.loads(upstream_error("blocked", 400, shape="anthropic").body)
    assert anthropic == {"type": "error", "error": {"type": "invalid_request_error", "message": "Latet: blocked"}}
    bedrock = json.loads(upstream_error("blocked", 400, shape="bedrock").body)
    assert bedrock["message"] == bedrock["error"]["message"] == "Latet: blocked"
    response = upstream_error("blocked", 400, shape="openai")
    assert json.loads(response.body)["error"] == {
        "message": "Latet: blocked",
        "type": "latet_error",
        "code": "latet_blocked",
    }
    assert response.headers["x-should-retry"] == "false"


def test_an_error_that_names_latet_first_is_not_marked_again():
    said = "Latet did not send this request: you did not approve it in the Live Reviewer"
    anthropic = json.loads(upstream_error(said, 400, shape="anthropic").body)
    assert anthropic["error"]["message"] == said
    bedrock = json.loads(upstream_error(said, 400, shape="bedrock").body)
    assert bedrock["message"] == bedrock["error"]["message"] == said
    assert json.loads(upstream_error(said, 400, shape="openai").body)["error"]["message"] == said


def test_a_path_is_read_as_any_server_reads_it():
    assert readable_path("/v1/chat/complet%69ons") == "/v1/chat/completions"
    assert readable_path("/model/arn%3Aaws%3Ax%2Fabc/invoke") == "/model/arn%3Aaws%3Ax%2Fabc/invoke"  # kept
    assert readable_path("/a%7eb%2D%5F%2e") == "/a~b-_."


def test_requests_leave_through_the_client_the_gateway_is_given():
    seen = []

    def model(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))  # a stream: the gateway relays it as it comes

    config = GatewayConfig(upstreams=[PRETEND], client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(model)))
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
        assert client.get("/anthropic/v1/models").text == "ok"
    assert seen == ["http://pretend-model.invalid/v1/models"]


def test_without_a_client_given_the_gateway_makes_its_own(monkeypatch):
    made = []

    def make_client() -> httpx.AsyncClient:
        made.append(True)
        empty = httpx.MockTransport(lambda request: httpx.Response(204, stream=httpx.ByteStream(b"")))
        return httpx.AsyncClient(transport=empty)

    monkeypatch.setattr(app, "make_client", make_client)
    with TestClient(create_app(GatewayConfig(upstreams=[PRETEND])), base_url="http://127.0.0.1:5283") as client:
        assert client.get("/anthropic/v1/models").status_code == 204
    assert made == [True]
