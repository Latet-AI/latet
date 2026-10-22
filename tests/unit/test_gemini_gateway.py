"""Google's Gemini API as a route of Latet's gateway: its address, the errors a Google SDK reads, and the API key, which
reaches Google and no record."""

import json
import logging

import pydantic
import pytest
from starlette.testclient import TestClient

from latet.config import Destination
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.capture import Capture
from latet.gateway.cloud import VertexUpstream, redact_access_log
from latet.gateway.forward import error_shape, upstream_error
from latet.gateway.mediate import Prepared
from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream

from ..e2e.fake_upstream import FakeUpstream

KEY = "AIzaSyFAKE-key-0123456789"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def test_gemini_is_a_route_and_a_reserved_name():
    [gemini] = [u for u in DEFAULT_UPSTREAMS if u.name == "gemini"]
    assert (gemini.prefix, gemini.base_url, gemini.refuses_unread) == (
        "/gemini",
        "https://generativelanguage.googleapis.com",
        True,
    )
    assert not any(u.refuses_unread for u in DEFAULT_UPSTREAMS if u.name != "gemini")
    with pytest.raises(pydantic.ValidationError):
        Destination(name="gemini", url="https://llm.acme.com")


def test_the_key_reaches_google_as_it_was_sent(upstream, tmp_path):
    config = GatewayConfig(upstreams=[Upstream("gemini", "/gemini", upstream.url, refuses_unread=True)])
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
        client.get("/gemini/v1beta/models", headers={"x-goog-api-key": KEY})
        client.get(f"/gemini/v1beta/models?key={KEY}&pageSize=5")
    by_header, by_query = upstream.requests[-2:]
    assert by_header["headers"]["x-goog-api-key"] == KEY and by_header["path"] == "/v1beta/models"
    assert by_query["query"] == f"key={KEY}&pageSize=5"


def test_a_capture_keeps_no_key(tmp_path):
    exchange = Capture(tmp_path).exchange("http", "gemini", "GET", f"https://g.example/v1beta/models?key={KEY}&x=1")
    assert exchange is not None
    exchange.request([("x-goog-api-key", KEY)], b"", None)
    written = "".join(path.read_text("utf-8") for path in tmp_path.iterdir())
    assert KEY not in written and "x=1" in written


def test_the_access_log_keeps_no_key(caplog):
    redact_access_log()
    access = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "POST", f"/gemini/v1beta/models/g:x?alt=sse&key={KEY}", "1.1", 200
        )
    assert KEY not in caplog.text and "alt=sse" in caplog.text


def test_an_error_is_in_googles_shape():
    answer = upstream_error("Latet: stopped", 400, shape="google")
    assert answer.status_code == 400
    assert json.loads(answer.body) == {
        "error": {"code": 400, "message": "Latet: stopped", "status": "INVALID_ARGUMENT"}
    }
    for status, name in ((404, "NOT_FOUND"), (413, "INVALID_ARGUMENT"), (502, "UNAVAILABLE"), (500, "INTERNAL")):
        assert json.loads(upstream_error("x", status, shape="google").body)["error"]["status"] == name
    assert json.loads(upstream_error("x", 418, shape="google").body)["error"]["status"] == "UNKNOWN"


def test_each_route_answers_in_the_shape_its_clients_read():
    gemini = Upstream("gemini", "/gemini", "https://g", refuses_unread=True)
    vertex = VertexUpstream()
    openai = Upstream("openai", "/openai", "https://o")
    assert error_shape(gemini, None) == "google"
    assert error_shape(openai, Prepared("rewrite", "gemini", b"")) == "google"  # a destination speaking Gemini
    assert error_shape(vertex, Prepared("rewrite", "gemini", b"")) == "google"
    assert error_shape(vertex, Prepared("passthrough", None, b"{}")) == "google"  # a Google body Latet refused
    assert error_shape(vertex, Prepared("verify", "anthropic", b"")) == "anthropic"  # Claude Code's, as today
    assert error_shape(vertex, None) == "anthropic"
    assert error_shape(openai, None) == "openai"


def _live(client: TestClient, url: str) -> None:
    """A Live API session as `@google/genai` opens one, the user's turn its first frame."""
    with client.websocket_connect(url, headers={"host": "127.0.0.1:5283"}) as ws:
        ws.send_text('{"clientContent": {"turns": [{"role": "user", "parts": [{"text": "pay IT60 X054"}]}]}}')
        ws.receive_text()


def test_the_live_apis_websocket_is_refused_and_nothing_reaches_google(upstream):
    from starlette.websockets import WebSocketDisconnect

    config = GatewayConfig(upstreams=[Upstream("gemini", "/gemini", upstream.url, refuses_unread=True)])
    live = "/gemini/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    before = len(upstream.ws_received)
    app = create_app(config)
    with TestClient(app, base_url="http://127.0.0.1:5283") as client, pytest.raises(WebSocketDisconnect) as closed:
        _live(client, f"{live}?key={KEY}")
    assert closed.value.code == 4400 and "Live API" in (closed.value.reason or "")
    assert len(upstream.ws_received) == before
