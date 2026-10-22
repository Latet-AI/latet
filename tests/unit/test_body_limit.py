"""A request body bounded before more of it than the limit is buffered (a server's ``BodyLimit``, R-4): 413 when its
length says it is over the limit, before the app sees it, and 413 when it grows past the limit as the app reads it,
whatever the app makes of a body cut short; the app is told its client is gone and gets nothing past the limit; a body
of the limit's size passes whole; a WebSocket passes untouched."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from latet.gateway.security import BodyLimit

LIMIT = 1024 * 1024
TOO_MUCH = b"x" * (LIMIT + 1)


@pytest.fixture
def received():
    """What the app was handed of each body, chunk by chunk."""
    return []


@pytest.fixture
def client(received):
    async def read_all(request: Request) -> JSONResponse:
        got = 0
        async for chunk in request.stream():
            got += len(chunk)
            received.append(len(chunk))
        return JSONResponse({"read": got})

    async def read_json(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except ValueError:  # what an app answers of a body it cannot read: never what the client gets here
            return JSONResponse({"error": "invalid body"}, status_code=400)
        return JSONResponse({"keys": len(data)})

    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_bytes(await websocket.receive_bytes())
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/body", read_all, methods=["POST"]),
            Route("/json", read_json, methods=["POST"]),
            WebSocketRoute("/ws", echo),
        ]
    )
    with TestClient(BodyLimit(app, LIMIT)) as found:
        yield found


async def _driven(app, chunks, headers=()):
    """Every message ``app`` sends for a request whose body comes in ``chunks``, after its ``headers``."""
    waiting = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent = []

    async def receive():
        return waiting.pop(0) if waiting else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "raw_path": b"/", "headers": list(headers)}
    await app(scope, receive, send)
    return sent


async def test_a_length_over_the_limit_is_refused_before_the_app_sees_the_request():
    handed = []

    async def app(scope, receive, send):
        while (message := await receive())["type"] == "http.request":
            handed.append(len(message["body"]))
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read"})

    # Its pieces each under the limit: only its length says it is over it, and nothing of it reaches the app.
    sent = await _driven(BodyLimit(app, LIMIT), [b"x" * 300_000] * 4, [(b"content-length", b"1200000")])
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413] and handed == []
    assert b"max_request_mb" in b"".join(m.get("body", b"") for m in sent)


def test_a_length_over_the_limit_is_refused_through_a_client_too(client, received):
    refused = client.post("/body", content=TOO_MUCH)
    assert refused.status_code == 413 and "max_request_mb" in refused.json()["error"]
    assert received == []


def test_a_body_that_grows_past_the_limit_is_refused_whatever_the_app_makes_of_it(client, received):
    for body in ([TOO_MUCH], [b"x" * 300_000] * 5, [b'{"a": "', TOO_MUCH, b'"}']):
        received.clear()
        assert client.post("/body", content=iter(body)).status_code == 413
        assert sum(received) <= LIMIT  # never a byte past the limit handed to the app
    grown = client.post("/json", content=iter([b'{"a": "', TOO_MUCH, b'"}']))
    assert grown.status_code == 413 and "max_request_mb" in grown.text  # not the app's 400 of a body cut short


def test_a_body_of_the_limits_size_passes_whole(client):
    assert client.post("/body", content=iter([b"x" * LIMIT])).json() == {"read": LIMIT}
    assert client.post("/body", content=b"x" * LIMIT).json() == {"read": LIMIT}
    assert client.post("/body", content=iter([b"x" * 10] * 3)).json() == {"read": 30}


async def test_a_length_that_is_no_number_is_judged_by_what_comes():
    handed = []

    async def app(scope, receive, send):
        while (message := await receive())["type"] == "http.request":  # until it is told its client is gone
            handed.append(len(message["body"]))
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read"})

    for declared in (b"lots", b"-5", b""):
        handed.clear()
        sent = await _driven(BodyLimit(app, LIMIT), [b"x" * 600_000, b"x" * 600_000], [(b"content-length", declared)])
        assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]
        assert sum(handed) <= LIMIT and b"read" not in b"".join(m.get("body", b"") for m in sent)


async def test_once_an_answer_has_begun_what_the_app_sends_after_the_limit_never_leaves():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (message := await receive())["type"] == "http.request":
            await send({"type": "http.response.body", "body": b"chunk;", "more_body": True})
            if not message["more_body"]:
                break
        await send({"type": "http.response.body", "body": b"the end"})
        raise RuntimeError("a body cut short")  # what an app may raise of it: swallowed once refused

    sent = await _driven(BodyLimit(app, LIMIT), [b"x" * 600_000] * 3)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]  # the first chunk's answer only


def test_a_websocket_passes_untouched(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(TOO_MUCH)
        assert ws.receive_bytes() == TOO_MUCH
