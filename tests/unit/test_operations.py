"""Operations, in front of the server's app (E0b's ``create_server_app``): it hands every request on as it came, its
scope, its messages and its answer, adds nothing to a scope (the gate sets ``latet.space``, T9.2), and its state is the
inner app's, through which `latet server run` tells the app its server. The metrics it answers on a socket of their own
and the counts it makes are tests/unit/test_metrics.py's."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from latet.server.machines import Limits
from latet.server.metrics import Metrics
from latet.server.operations import Operations

URL = "https://latet.acme.lan"
ROUTE = "r" * 26


def _inner(seen):
    """The server's app, as a stand-in: each scope it was handed, answering a request with what it was sent, a WebSocket
    with its first message, and the lifespan as Starlette does."""

    async def app(scope, receive, send):
        seen.append(scope)
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] == "websocket":
            await receive()  # the handshake
            await send({"type": "websocket.accept"})
            message = await receive()
            await send({"type": "websocket.send", "text": message.get("text")})
            await send({"type": "websocket.close", "code": 1000})
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = [(b"content-type", b"text/plain"), (b"x-inner", b"yes")]
        await send({"type": "http.response.start", "status": 207, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    app.state = SimpleNamespace()
    return app


@pytest.fixture
def server():
    found = SimpleNamespace(
        settings=SimpleNamespace(url=URL, metrics_token_file=None, metrics_port=7283),  # metrics off
        chain=SimpleNamespace(read=lambda: "unknown"),
        limits=Limits(),
        metrics=Metrics(),
        edition=SimpleNamespace(metrics=lambda metrics: None),  # E5's Metrics.watch asks the edition for its own
    )
    yield found
    found.metrics.close()


def test_every_request_is_handed_on_and_answered_as_the_app_answers_it(server):
    seen = []
    with TestClient(Operations(_inner(seen), server), base_url=URL) as client:
        answer = client.post(f"/u/{ROUTE}/anthropic/v1/messages", content=b"what was sent")
        assert (answer.status_code, answer.text, answer.headers["x-inner"]) == (207, "what was sent", "yes")
        assert client.get("/_latet/server/v1/admin/admin-tokens").status_code == 207  # nothing answered here
        with client.websocket_connect(f"/u/{ROUTE}/openai/v1/responses") as socket:
            socket.send_text("a turn")
            assert socket.receive_text() == "a turn"
    assert [scope["type"] for scope in seen] == ["lifespan", "http", "http", "websocket"]
    for scope in seen:
        assert not any(key.startswith("latet.") for key in scope)  # no scope key of its own


async def test_the_scope_handed_on_is_the_one_that_came(server):
    seen, sent = [], []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/_latet/health", "headers": [], "query_string": b""}
    await Operations(_inner(seen), server)(scope, receive, send)
    assert len(seen) == 1 and seen[0] is scope
    assert scope.keys() == {"type", "method", "path", "headers", "query_string"}  # nothing added to it
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]


def test_its_state_is_the_inner_apps(server):
    inner = _inner([])
    operations = Operations(inner, server)
    assert operations.state is inner.state
    operations.state.server = "the uvicorn server"  # what `latet server run` tells the app (cli.main.uvicorn_server)
    assert inner.state.server == "the uvicorn server"
