"""An upstream's answer that sends the request elsewhere (301, 302, 303, 307, 308, or any 3xx naming a ``Location``)
never reaches the client as a redirect: a client that followed it would send its request, unmasked, straight to the
address it names, past Latet. Latet follows none either. The client is answered 502 with a clear error, with no
``Location`` and nothing of the upstream's answer: over HTTP, in a stream and at a WebSocket's handshake, on a computer
(here) as on a server (``tests/unit/server/test_identity_app.py``)."""

import logging
import socket
import threading

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY

EMAIL = "emily.carter@acme.com"
REDIRECTS = (301, 302, 303, 307, 308)
PROVIDER = "http://127.0.0.1:1"  # the provider's address: a mock transport answers in its place
ELSEWHERE = {
    "the same host": f"{PROVIDER}/v1/elsewhere",
    "the same host, as a path": "/v1/elsewhere",
    "another host": "https://collector.example.net/v1/chat/completions",
}
CHAT = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
HEADERS = {"authorization": "Bearer sk-test"}
FOLLOWABLE = {"location", "content-location", "refresh", "link"}  # headers a client could read an address from


class _Provider:
    """A mock provider: what it answers next (``answer``), and every request it was sent."""

    def __init__(self) -> None:
        self.answer = httpx.Response(200)
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self.answer


@pytest.fixture(scope="module")
def _gateway():
    provider = _Provider()
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("openai", "/openai", PROVIDER), Upstream("anthropic", "/anthropic", PROVIDER)],
        mediator=Mediator(built.engine, built.verifier),
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
        yield client, provider


@pytest.fixture
def gateway(_gateway):
    """A computer's gateway, masking with the default rules, in front of a mock provider that has been sent nothing
    yet; and the provider."""
    client, provider = _gateway
    provider.sent.clear()
    return client, provider


def _moved(status: int, location: str | None, content_type: str = "text/html") -> httpx.Response:
    """A provider's answer sending the request elsewhere, with a body a browser would follow too."""
    headers = {"content-type": content_type}
    if location is not None:
        headers["location"] = location
    body = f'<a href="{location}">Moved</a>'.encode() if location is not None else b"Moved"
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _refused(answer: httpx.Response, status: int, location: str | None = None) -> None:
    """The client is answered 502 with the clear error, and nothing it could follow: no Location nor any header naming
    an address, nothing of the provider's answer."""
    assert answer.status_code == 502, answer.status_code
    assert not FOLLOWABLE & {name.lower() for name in answer.headers}
    message = answer.json()["error"]["message"]
    assert f"the provider answered with a redirect ({status})" in message and "Latet does not follow" in message
    assert "Moved" not in answer.text and "elsewhere" not in answer.text and "collector" not in answer.text
    assert location is None or location not in answer.text


def _sent_once_masked(provider: _Provider) -> None:
    """The request went to the provider once, masked, and Latet went nowhere else."""
    assert [str(request.url) for request in provider.sent] == [f"{PROVIDER}/v1/chat/completions"]
    assert EMAIL not in provider.sent[0].content.decode()


@pytest.mark.parametrize("where", ELSEWHERE)
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_redirect_reaches_the_client_as_a_clear_error_with_nothing_to_follow(gateway, status, where):
    client, provider = gateway
    provider.answer = _moved(status, ELSEWHERE[where])
    answer = client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS, follow_redirects=False)
    _refused(answer, status, ELSEWHERE[where])
    _sent_once_masked(provider)


@pytest.mark.parametrize("where", ELSEWHERE)
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_streamed_answers_redirect_reaches_the_client_as_a_clear_error(gateway, status, where):
    client, provider = gateway
    provider.answer = _moved(status, ELSEWHERE[where], "text/event-stream")
    streamed = {**CHAT, "stream": True}
    answer = client.post("/openai/v1/chat/completions", json=streamed, headers=HEADERS, follow_redirects=False)
    _refused(answer, status, ELSEWHERE[where])
    _sent_once_masked(provider)


@pytest.mark.parametrize("status", [300, 304, 305, 306])
def test_any_other_3xx_naming_a_location_reaches_the_client_as_the_same_error(gateway, status):
    client, provider = gateway
    provider.answer = _moved(status, ELSEWHERE["another host"])
    answer = client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS, follow_redirects=False)
    _refused(answer, status, ELSEWHERE["another host"])
    _sent_once_masked(provider)


@pytest.mark.parametrize("status", REDIRECTS)
def test_a_redirect_naming_no_location_reaches_the_client_as_the_same_error(gateway, status):
    client, provider = gateway
    provider.answer = _moved(status, None)
    _refused(client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS, follow_redirects=False), status)
    _sent_once_masked(provider)


def test_a_3xx_that_sends_the_request_nowhere_reaches_the_client_as_it_came(gateway):
    """Not Modified, naming no Location, is no redirect: it reaches the client as it came."""
    client, provider = gateway
    provider.answer = httpx.Response(304, stream=httpx.ByteStream(b""))
    assert client.get("/openai/v1/models", headers=HEADERS, follow_redirects=False).status_code == 304


def test_claude_codes_client_reads_the_error_in_its_own_shape(gateway):
    client, provider = gateway
    provider.answer = _moved(307, ELSEWHERE["another host"])
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
    answer = client.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "sk-test"}, follow_redirects=False)
    assert answer.status_code == 502 and not FOLLOWABLE & {name.lower() for name in answer.headers}
    error = answer.json()["error"]
    assert error["type"] == "invalid_request_error" and "answered with a redirect (307)" in error["message"]
    assert [str(request.url) for request in provider.sent] == [f"{PROVIDER}/v1/messages"]


def test_the_log_says_a_redirect_was_refused_and_never_where_it_led(gateway, caplog):
    client, provider = gateway
    provider.answer = _moved(308, ELSEWHERE["another host"])
    with caplog.at_level(logging.WARNING, logger="latet.gateway.forward"):
        client.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS, follow_redirects=False)
    said = [record.getMessage() for record in caplog.records if record.name == "latet.gateway.forward"]
    assert any("redirect (308)" in line for line in said), said
    assert not any("collector" in line or "elsewhere" in line for line in said)


# --- a WebSocket's handshake ---------------------------------------------------------------------------------------


class _Answering:
    """A server on 127.0.0.1 that answers every connection with ``answer``, keeping the head of each request read."""

    def __init__(self, answer: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") -> None:
        self.answer, self.heads = answer, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                head = b""
                while b"\r\n\r\n" not in head and (chunk := conn.recv(4096)):
                    head += chunk
                self.heads.append(head.lower())
                conn.sendall(self.answer)

    def close(self) -> None:
        self.sock.close()


@pytest.mark.parametrize("where", ["the same host", "the same host, as a path", "another host"])
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_websockets_redirect_reaches_the_client_as_a_clear_error_and_is_not_followed(status, where):
    moved, elsewhere = _Answering(), _Answering()
    location = {
        "the same host": f"ws://127.0.0.1:{moved.port}/v1/elsewhere",
        "the same host, as a path": "/v1/elsewhere",
        "another host": f"ws://localhost:{elsewhere.port}/v1/responses",  # another name: another origin
    }[where]
    moved.answer = f"HTTP/1.1 {status} Moved\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()
    config = GatewayConfig(port=5283, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{moved.port}")])
    try:
        with (
            TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client,
            pytest.raises(WebSocketDenialResponse) as denied,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}),  # this computer's
        ):
            pass
    finally:
        moved.close()
        elsewhere.close()
    _refused(denied.value, status, location)
    assert len(moved.heads) == 1 and b"get /v1/responses " in moved.heads[0] and elsewhere.heads == []
