"""What a token of an API key may do, as the server's gate enforces it: an address calls a model through Latet, which
masks what goes and restores what comes back, and the plugin's bridge restores an agent's texts too (rehydrate, scope),
with no entry of the administration log for each restore; so a token added to a key, whatever it may do, is refused
both (Ruling T13b: on a provider route, on a WebSocket and on every call of the bridge), before its space is asked and
before anything reaches the model, and restores through the applications API alone, where each unmask is logged; a
token without permissions of its own (a key's first, a person's, every token made before permissions existed) calls as
it did; and a token revoked on one instance is refused by the other within its few seconds, the grant it held in memory
included."""

import asyncio
import json
import time

import pytest
from starlette.testclient import TestClient

from latet.api.rest import NO_COMPUTER
from latet.gateway.routes import Upstream
from latet.server.app import create_server_app
from latet.server.identities import CACHE_SECONDS
from latet.server.tokens import route_key

from ..e2e.fake_upstream import FakeUpstream
from .support import URL, until

pytestmark = pytest.mark.server

MACHINE = "/_latet/server/v1"
ADMIN = f"{MACHINE}/admin"
ENGINE = "/_latet/engine/v1"
NO_GATEWAY = "this token calls the applications API alone: an agent calls through Latet with the key's first token"
APPS_API = "/_latet/apps/v1"
MAIL = "emily.carter@acme.com"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def _openai(upstream, monkeypatch):
    """OpenAI's route of every space built in the test leads to the stub model on 127.0.0.1."""
    monkeypatch.setattr("latet.gateway.app.DEFAULT_UPSTREAMS", (Upstream("openai", "/openai", upstream.url),))


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


def _token(server, key_id, name, permissions):
    """Another token of the API key ``key_id``, with ``permissions``: its secret."""
    with server.identities.changing() as changes:
        _, secret = server.identities.add_token(key_id, name, permissions, within=changes)
    return secret


def _chat(client, secret, text):
    """An agent's request to OpenAI through the address of ``secret``."""
    body = {"model": "m", "messages": [{"role": "user", "content": text}]}
    path = f"/u/{route_key(secret)}/openai/v1/chat/completions"
    return client.post(path, json=body, headers={"authorization": "Bearer sk-test"})


def _said(answer):
    """The text of a model's answer, as the agent reads it."""
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )


def _items(*texts):
    return {"session": "s1", "items": [{"text": text, "kind": "user_text"} for text in texts]}


async def _websocket(app, path):
    """What ``app`` sends of a WebSocket's handshake at ``path`` and after it, until it ends."""
    sent, waiting = [], [{"type": "websocket.connect"}]

    async def receive():
        if waiting:
            return waiting.pop()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "wss", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": [(b"host", b"latet.acme.lan")],
        "client": ("10.0.3.9", 50000), "server": ("latet.acme.lan", 443), "subprotocols": [], "extensions": {},
    }  # fmt: skip
    await app(scope, receive, send)
    return sent


def _added(server, key_id):
    """A token of each kind an administrator adds to the API key ``key_id`` (``keys add-token``): their secrets."""
    every = (
        ("logs", ["mask"]),
        ("reader", ["unmask"]),
        ("audit", ["scan"]),
        ("erasure", ["forget"]),  # Task 11b: forget brings no address
        ("assistant", ["mask", "unmask"]),  # all three, kept in its row (Ruling T13c)
        ("all of it", ["mask", "unmask", "forget"]),
    )
    return [_token(server, key_id, name, given) for name, given in every]


def test_a_token_added_to_a_key_cannot_use_the_gateway(server, client, upstream, monkeypatch):
    key, _ = server.identities.add_key("Billing CI")
    restricted = _added(server, key.id)
    reached = []
    real = server.spaces.app

    async def spied(identity):
        reached.append(identity.id)
        return await real(identity)

    monkeypatch.setattr(server.spaces, "app", spied)
    before = (len(upstream.requests), len(upstream.received))
    for secret in restricted:
        refused = _chat(client, secret, f"write to {MAIL}")
        assert refused.status_code == 403
        assert refused.json() == {"error": f"latet: {NO_GATEWAY}"}
        assert MAIL not in refused.text
        models = client.get(f"/u/{route_key(secret)}/openai/v1/models", headers={"authorization": "Bearer sk-test"})
        assert models.status_code == 403 and models.json() == {"error": f"latet: {NO_GATEWAY}"}
    assert (len(upstream.requests), len(upstream.received)) == before  # the stub model saw nothing
    assert reached == []  # refused by the gate: the key's space was never asked, nothing was masked


async def test_a_token_added_to_a_key_opens_no_websocket(server, upstream):
    app = create_server_app(server)
    async with app.router.lifespan_context(app):
        key, _ = await asyncio.to_thread(server.identities.add_key, "Billing CI")
        logs = await asyncio.to_thread(_token, server, key.id, "logs", ["mask"])
        assistant = await asyncio.to_thread(_token, server, key.id, "assistant", ["mask", "unmask"])
        before = len(upstream.ws_received)
        for secret in (logs, assistant):
            closed = await _websocket(app, f"/u/{route_key(secret)}/openai/v1/responses")
            assert closed == [{"type": "websocket.close", "code": 4403, "reason": NO_GATEWAY}]
        assert len(upstream.ws_received) == before


def test_a_keys_first_token_and_a_persons_use_the_gateway_as_before(server, client, upstream):
    key, first = server.identities.add_key("Billing CI")  # the key's first token, made as every key's before
    _, code = server.identities.add_person("Emily Carter")
    _, laptop, _ = server.identities.enrol(code, "laptop")  # a person's computer: all three, as before
    before = len(upstream.received)
    for secret in (first, laptop, "rotated"):
        if secret == "rotated":  # rotated, the key's first token serves agents still
            secret = server.identities.rotate_token(key.id)[1]
        answer = _chat(client, secret, f"write to {MAIL} about the invoice")
        assert answer.status_code == 200
        assert MAIL in _said(answer)  # the answer restored
    assert len(upstream.received) == before + 3
    assert MAIL not in b"".join(upstream.received[before:]).decode()  # what left was masked


# Every call of the plugin's bridge (``bridge.api.Bridge.routes``), and one it does not serve: by method and path.
BRIDGE_CALLS = (
    ("GET", "ping"), ("POST", "route-check"), ("POST", "mask"), ("POST", "verify"), ("POST", "outbound"),
    ("POST", "instructions"), ("POST", "approvals/wait"), ("POST", "rehydrate"), ("POST", "scope"),
    ("POST", "aws-sessions"), ("DELETE", "aws-sessions/session-one"), ("GET", "aws-sessions/session-one/hold"),
    ("GET", "something-new"),
)  # fmt: skip


def test_a_token_added_to_a_key_is_refused_every_call_of_the_bridge(server, client):
    """Ruling T13b: rehydrate and scope restore a key's fakes with no entry of the administration log, so no token added
    to a key makes any call of the bridge, under the key's first token's address or its own, whatever it may do: it
    restores through the applications API alone, its unmask an ``apps.unmasked`` entry. The key's first token calls the
    bridge as before."""
    key, first = server.identities.add_key("Billing CI")
    mine = {"x-latet-token": first}
    masked = client.post(f"/u/{route_key(first)}{ENGINE}/mask", json=_items(f"write to {MAIL}"), headers=mine)
    [text] = [item["text"] for item in masked.json()["items"]]
    assert masked.status_code == 200 and MAIL not in text
    added = _added(server, key.id)
    for secret in added:
        theirs = {"x-latet-token": secret}
        for base in (f"/u/{route_key(first)}{ENGINE}", f"/u/{route_key(secret)}{ENGINE}"):
            for method, call in BRIDGE_CALLS:
                body = {"session": "s1", "items": [text]} if method == "POST" else None
                refused = client.request(method, f"{base}/{call}", json=body, headers=theirs)
                assert refused.status_code == 403, (method, call)
                assert refused.json() == {"error": f"latet: {NO_GATEWAY}"}, (method, call)
                assert MAIL not in refused.text
    # The key's first token restores the same text through the bridge, and adds fakes to a session, as before.
    again = {"session": "s1", "items": [text]}
    restored = client.post(f"/u/{route_key(first)}{ENGINE}/rehydrate", json=again, headers=mine)
    assert restored.status_code == 200 and restored.json()["items"] == [f"write to {MAIL}"]
    assert client.post(f"/u/{route_key(first)}{ENGINE}/scope", json=again, headers=mine).status_code == 200
    assert client.get(f"/u/{route_key(first)}{ENGINE}/something-new", headers=mine).status_code == 404  # as before
    # The assistant (mask and unmask) restores it through the applications API, and the log says so.
    assert server.audit.query(action="apps.unmasked") == []
    bearer = {"authorization": f"Bearer {added[4]}"}
    unmasked = client.post(f"{APPS_API}/unmask", json={"texts": [text]}, headers=bearer)
    assert unmasked.status_code == 200
    assert unmasked.json()["results"] == [{"text": f"write to {MAIL}", "restored": 1}]
    [entry] = server.audit.query(action="apps.unmasked")
    [token] = [t.id for t in server.identities.tokens(key.id) if t.label == "assistant"]
    assert entry.details == {"token": token, "texts": 1, "restored": 1}


@pytest.mark.timing
def test_a_revoked_token_is_refused_everywhere(instances):
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    key, full = a.identities.add_key("Billing CI")
    logs = _token(a, key.id, "logs", ["mask"])
    [token] = [t.id for t in a.identities.tokens(key.id) if t.label == "logs"]
    theirs, bearer = {"x-latet-token": logs}, {"authorization": f"Bearer {logs}"}
    base, own = f"/u/{route_key(full)}{ENGINE}", f"/u/{route_key(logs)}{ENGINE}"
    with (
        TestClient(create_server_app(a), base_url=URL) as first,
        TestClient(create_server_app(b), base_url=URL) as second,
    ):
        for client in (first, second):  # used on both instances: each holds its grant in memory
            assert client.post(f"{APPS_API}/mask", json={"texts": ["hello"]}, headers=bearer).status_code == 200
            assert client.get(f"{own}/ping", headers=theirs).json() == {"error": f"latet: {NO_GATEWAY}"}
        assert b.identities.remembered_grant(secret=logs) is not None
        assert b.identities.remembered_grant(route=route_key(logs)) is not None
        held = time.monotonic()  # the second instance holds both since before now, for CACHE_SECONDS at least
        revoked = first.delete(f"{ADMIN}/identities/{key.id}/tokens", params={"token": token}, headers=admin)
        assert revoked.status_code == 200 and revoked.json() == {"revoked": 1}
        gone = {"error": "latet: there is no such address"}

        def second_refuses():
            masked = second.post(f"{APPS_API}/mask", json={"texts": ["hello"]}, headers=bearer)
            pinged = second.get(f"{own}/ping", headers=theirs)
            return masked.status_code == 401 and pinged.json() == gone

        # Within the few seconds the memory holds them: the revocation's message dropped them, not their age.
        asyncio.run(until(second_refuses, CACHE_SECONDS - (time.monotonic() - held) - 0.5))
        assert time.monotonic() - held < CACHE_SECONDS
        for client in (first, second):
            assert client.post(f"{APPS_API}/mask", json={"texts": ["hello"]}, headers=bearer).status_code == 401
            assert client.post(f"{base}/mask", json=_items("hello"), headers=theirs).status_code == 401
            assert client.get(f"{own}/ping", headers=theirs).json() == gone
            assert client.get(f"{base}/ping", headers={"x-latet-token": full}).status_code == 200  # the key's other


def test_the_clis_two_calls_are_refused_to_a_key(server, client):
    """The CLI's two calls are a computer's: a token added to a key is refused them as every call of an agent's
    (Ruling T13b), before anything else is asked; a key's first token is refused them as an API key, as before."""
    key, full = server.identities.add_key("Billing CI")
    for path in ("/_latet/admin/user-name", "/_latet/admin/destinations"):
        for secret in _added(server, key.id):
            for address in (route_key(secret), route_key(full)):
                refused = client.post(f"/u/{address}{path}", json={}, headers={"x-latet-token": secret})
                assert refused.status_code == 403 and refused.json() == {"error": f"latet: {NO_GATEWAY}"}, path
        as_a_key = client.post(f"/u/{route_key(full)}{path}", json={}, headers={"x-latet-token": full})
        assert as_a_key.status_code == 403 and as_a_key.json() == {"error": NO_COMPUTER}


def test_whoami_gives_no_address_to_a_token_that_cannot_use_it(server, client):
    """``whoami`` answers a token's addresses only when they would serve it (a key's first token, Ruling T13b), as the
    administration gives them: a token added to a key, whatever it may do, is told who it is, and no address that would
    refuse it."""
    key, full = server.identities.add_key("Billing CI")
    who = {"id": key.id, "name": "Billing CI", "kind": "key"}
    found = client.get(f"{MACHINE}/whoami", headers={"x-latet-token": full}).json()
    assert found.items() >= who.items() and found["base"] == f"{URL}/u/{route_key(full)}"
    for secret in _added(server, key.id):
        found = client.get(f"{MACHINE}/whoami", headers={"x-latet-token": secret})
        assert found.status_code == 200 and found.json().items() >= who.items()
        assert not {"base", "anthropic", "openai"} & set(found.json()) and route_key(secret) not in found.text
