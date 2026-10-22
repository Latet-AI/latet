"""ES.5: the actual gate's narrow API-key refusal precedes body and space access."""

import asyncio
import json

import pytest
from starlette.responses import JSONResponse

from latet.api.rest import NO_COMPUTER
from latet.server.app import create_server_app
from latet.server.identities import route_key
from latet.server.logs import REFUSED

pytestmark = pytest.mark.server


@pytest.mark.parametrize("path", ["user-name", "destinations"])
@pytest.mark.parametrize("kind", ["key", "person"])
async def test_computer_call_keys_refuse_exactly_before_reading_but_people_reach_their_space(
    server, monkeypatch, path, kind
):
    if kind == "key":
        identity, token = server.identities.add_key("Billing CI")
    else:
        identity, code = server.identities.add_person("Emily Carter")
        _, token, _ = server.identities.enrol(code, "laptop")
    touched, read, sent = [], [], []

    async def space(found):
        touched.append(found.id)

        async def endpoint(scope, receive, send):
            await receive()
            await JSONResponse({"ok": True})(scope, receive, send)

        return endpoint

    async def receive():
        read.append(True)
        assert kind == "person", "an API key's body was read"
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(server.spaces, "app", space)
    address = f"/u/{route_key(token)}/_latet/admin/{path}"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": address, "raw_path": address.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan"), (b"x-latet-token", token.encode())],
        "client": ("10.0.3.9", 50000), "server": ("latet.acme.lan", 443),
    }  # fmt: skip
    await asyncio.wait_for(create_server_app(server)(scope, receive, send), 10)
    [start] = [message for message in sent if message["type"] == "http.response.start"]
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    if kind == "key":
        assert start["status"] == 403 and json.loads(body) == {"error": NO_COMPUTER}
        assert scope.get(REFUSED) and touched == [] and read == []
        assert dict(start["headers"])[b"content-type"].startswith(b"application/json")
    else:
        assert start["status"] == 200 and not scope.get(REFUSED)
        assert touched == [identity.id] and read == [True]


async def test_unrelated_gateway_refusals_keep_their_existing_prefix(server):
    sent = []

    async def receive():
        pytest.fail("an unknown route's body was read")

    async def send(message):
        sent.append(message)

    path = "/u/" + "a" * 26 + "/anthropic/v1/messages"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan")], "client": ("10.0.3.9", 50000),
        "server": ("latet.acme.lan", 443),
    }  # fmt: skip
    await asyncio.wait_for(create_server_app(server)(scope, receive, send), 10)
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert json.loads(body)["error"].startswith("latet: ") and scope.get(REFUSED)
