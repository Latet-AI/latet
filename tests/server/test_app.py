"""A Latet server's app: health and readiness for any Host, the server's own name for everything else; a computer
enrols once with its code; an identity's address leads to its app, its bridge open to its own token only and nothing
else under /_latet; the dashboard to the person signed in, who signs out when they wish; web pages refused; an API key
with no dashboard and no computer; a body bounded on the wire and once decoded; unknown tokens and addresses limited by
the client's address before anything is looked up, and a store that does not answer failing a request closed and
holding up no other; each computer adding its names to its person's identity, never taking any away; and an instance
that loses Valkey, or whose ear goes silent, drops its spaces and says it is not ready until it hears again."""

import asyncio
import concurrent.futures
import contextlib
import functools
import gzip
import json
import logging
import secrets
import threading
import time
import zlib
from collections import Counter

import brotli
import httpx
import psycopg
import pytest
import redis
import zstandard
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from latet.gateway.security import BodyLimit
from latet.logline import Paced
from latet.server import admin as admin_module
from latet.server import app as app_module
from latet.server import identities as identities_module
from latet.server import live as live_module
from latet.server import logs as logs_module
from latet.server import machines
from latet.server.app import create_server_app
from latet.server.audit import ChainCheck
from latet.server.identities import Notes
from latet.server.live import CHAIN_FRESH, CHAIN_SKEW, ChainVerdicts
from latet.server.logs import REFUSED, RequestLog
from latet.server.shared import SharedItemsError
from latet.server.spaces import IdentityUnavailable, NotListening, SpaceUnavailable
from latet.server.tokens import route_key

from .conftest import drop_listeners
from .support import URL, listening, until

pytestmark = pytest.mark.server

SERVER = "/_latet/server/v1"
ENGINE = "/_latet/engine/v1"
API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}


# Each computer's key, by its token, as its enrolment gave it: what a sign-in link and an identity ask for (R16).
KEYS: dict[str, str] = {}


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


def _mine(token):
    """The headers a computer's CLI sends: its token, and its key when the test knows it."""
    return {"x-latet-token": token, **({"x-latet-computer-key": KEYS[token]} if token in KEYS else {})}


def _enrolled(server, client, name="Emily Carter"):
    """A person and the token of their computer (its key in ``KEYS``)."""
    person, code = server.identities.add_person(name)
    answer = client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"})
    assert answer.status_code == 200
    KEYS[answer.json()["token"]] = answer.json()["computer_key"]
    return person, answer.json()["token"]


def _another_computer(server, client, person, label="desktop"):
    """The token of a second computer of ``person``'s, enrolled with a code of its own (its key in ``KEYS``)."""
    code = server.identities.code(person.id)
    answer = client.post(f"{SERVER}/enrol", json={"code": code, "label": label})
    assert answer.status_code == 200
    KEYS[answer.json()["token"]] = answer.json()["computer_key"]
    return answer.json()["token"]


def _signed_in(client, token):
    """The person of ``token`` signed in to their dashboard in ``client``: the session's id."""
    link = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"]
    assert client.get(link.removeprefix(URL), follow_redirects=False).status_code == 303
    return client.cookies.get("latet_session")


def _masked(client, token, texts):
    """``texts`` as the space of ``token``'s identity masks them, through its bridge."""
    items = [{"text": text, "kind": "user_text"} for text in texts]
    answer = client.post(
        f"/u/{route_key(token)}{ENGINE}/mask", json={"session": "s1", "items": items}, headers={"x-latet-token": token}
    )
    assert answer.status_code == 200
    return [item["text"] for item in answer.json()["items"]]


def test_health_and_readiness_answer_any_host_and_nothing_else_does(server, client):
    for host in ("10.0.3.7:5283", "latet.acme.lan"):
        health = client.get("/_latet/health", headers={"host": host}).json()
        assert health == {"ok": True, "server": True, "docs": health["docs"]} and isinstance(health["docs"], bool)
        ready = client.get("/_latet/ready", headers={"host": host})
        assert ready.status_code == 200 and ready.json() == {"ready": True}  # its state, and nothing else
    _, token = _enrolled(server, client)
    foreign = {"host": "10.0.3.7:5283", "x-latet-token": token}
    for path in (f"/u/{route_key(token)}/anthropic/v1/messages", f"{SERVER}/whoami", f"{API}/me", "/_latet/ui/"):
        assert client.get(path, headers=foreign).status_code == 403
    assert [e.actor for e in server.audit.query(action="server.started")] == ["server"]


def test_a_code_enrols_one_computer_and_tries_are_limited(server, client, monkeypatch):
    person, code = server.identities.add_person("Emily Carter")
    first = client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"})
    assert first.status_code == 200
    found = first.json()
    assert found["identity"] == {"id": person.id, "name": "Emily Carter", "kind": "person"}
    assert (
        found["base"] == f"{URL}/u/{route_key(found['token'])}" and found["anthropic"] == found["base"] + "/anthropic"
    )
    assert client.post(f"{SERVER}/enrol", json={"code": code, "label": "desktop"}).status_code == 401  # spent
    monkeypatch.setattr("latet.server.machines.ENROL_LIMIT", 3)
    tries = [client.post(f"{SERVER}/enrol", json={"code": "nope", "label": "x"}).status_code for _ in range(3)]
    assert tries == [401, 401, 429]  # the enrolment gave its slot back: the second, third and fourth failed tries
    [added] = server.audit.query(action="token.added")
    [made] = server.identities.tokens(person.id)  # which computer enrolled: its token's id (S8, area 3 M7)
    assert (added.actor, added.target, added.details) == (person.id, person.id, {"token": made.id})


def test_a_computer_asks_who_it_is_sends_its_identity_and_forgets_its_token(server, client):
    person, token = _enrolled(server, client)
    mine = _mine(token)
    who = client.get(f"{SERVER}/whoami", headers=mine).json()
    assert (who["id"], who["kind"], who["url"]) == (person.id, "person", URL)
    identity = {"usernames": ["emily"], "full_name": "Emily Carter", "hostname": "emily-laptop"}
    assert client.put(f"{SERVER}/identity", json=identity, headers=mine).json() == {"ok": True}
    assert client.put(f"{SERVER}/identity", json={"usernames": "emily"}, headers=mine).status_code == 400
    item = {"text": "cd /home/emily/billing-api", "kind": "user_text"}
    found = client.post(f"/u/{route_key(token)}{ENGINE}/mask", json={"session": "s1", "items": [item]}, headers=mine)
    assert "emily" not in found.json()["items"][0]["text"]  # the computer's user, masked by its person's space
    [made] = server.identities.tokens(person.id)
    assert client.delete(f"{SERVER}/token", headers=mine).json() == {"ok": True}
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 401
    entries = server.audit.query(actor=person.id)
    assert [e.action for e in entries] == ["token.revoked", "token.added"]
    assert [e.details for e in entries] == [{"token": made.id}] * 2  # which computer left, and when it came
    assert token not in repr([e.details for e in entries])


def test_an_enrolment_and_a_forgotten_token_are_kept_with_their_entries_or_not_at_all(server, client, monkeypatch):
    person, code = server.identities.add_person("Emily Carter")
    kept, failing = server.audit.record, {"token.added", "token.revoked"}

    def refused(actor, action, *args, **kwargs):
        if action in failing:
            raise RuntimeError("the log is gone")
        return kept(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    assert client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"}).status_code == 503
    assert server.identities.tokens(person.id) == []  # no token that nobody holds
    failing.discard("token.added")
    enrolled = client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"})  # the code was not spent
    assert enrolled.status_code == 200
    mine = {"x-latet-token": enrolled.json()["token"]}
    assert client.delete(f"{SERVER}/token", headers=mine).status_code == 503
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # not revoked: it can try again
    failing.clear()
    assert client.delete(f"{SERVER}/token", headers=mine).json() == {"ok": True}
    assert [e.action for e in server.audit.query(actor=person.id)] == ["token.revoked", "token.added"]


def test_an_api_key_has_no_dashboard_and_no_computer(server, client):
    _, secret = server.identities.add_key("Billing CI")
    theirs = {"x-latet-token": secret}
    who = client.get(f"{SERVER}/whoami", headers=theirs).json()
    assert who["kind"] == "key" and who["openai"] == f"{URL}/u/{route_key(secret)}/openai/v1"
    for method, path in (("POST", "login-code"), ("PUT", "identity"), ("DELETE", "token")):
        assert client.request(method, f"{SERVER}/{path}", json={}, headers=theirs).status_code == 403
    assert server.identities.by_secret(secret) is not None


def test_an_address_leads_to_its_identitys_app_and_its_bridge_opens_to_its_own_token_only(server, client):
    _, token = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")
    base = f"/u/{route_key(token)}"
    assert client.get(f"{base}{ENGINE}/ping", headers={"x-latet-token": token}).json()["ok"] is True
    assert client.get(f"{base}{ENGINE}/ping", headers={"x-latet-token": other}).status_code == 401
    assert client.get(f"{base}{ENGINE}/ping").status_code == 401
    for path in ("/_latet/api/v1/me", "/_latet/health", "/_latet/admin/shutdown"):
        assert client.get(f"{base}{path}", headers={"x-latet-token": token}).status_code == 404


def test_odd_and_unknown_addresses_are_refused(server, client):
    _enrolled(server, client)
    for path in ("/u/short/anthropic/v1/messages", "/u/" + "a" * 26 + "/anthropic/v1/messages", "/u/"):
        assert client.post(path, json={}).status_code == 403


def test_a_web_page_is_refused_on_an_identitys_address_and_on_the_machines_calls(server, client):
    _, token = _enrolled(server, client)
    page = {"origin": "https://evil.example", "x-latet-token": token}
    assert client.post(f"/u/{route_key(token)}/anthropic/v1/messages", json={}, headers=page).status_code == 403
    assert client.get(f"{SERVER}/whoami", headers=page).status_code == 403
    assert client.get(f"{SERVER}/whoami", headers={**page, "origin": URL}).status_code == 403


def test_a_page_of_the_servers_own_is_no_machine_either(server, client):
    """``sec-fetch-site``, whatever it says, is a browser's: an identity's address and the machines' calls refuse it,
    a same-origin request included (stricter than a computer's proxy routes), as the administration does."""
    _, token = _enrolled(server, client)
    for site in ("same-origin", "none", "same-site", "cross-site"):
        page = {"sec-fetch-site": site, "x-latet-token": token}
        assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers=page).status_code == 403, site
        assert client.post(f"/u/{route_key(token)}/anthropic/v1/messages", json={}, headers=page).status_code == 403
        assert client.get(f"{SERVER}/whoami", headers=page).status_code == 403, site
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200  # a computer's own


def test_a_web_page_spends_no_limit_of_its_visitors_address(server, client, monkeypatch):
    """A page a person visits can make their browser send requests to the server: refused before anything is looked
    up, they count nothing against the address the person's own agents share (its one budget of misses, two here)."""
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)
    monkeypatch.setattr(app_module, "LOOKUP_LIMIT", 2)
    page = {"sec-fetch-site": "cross-site"}
    for n in range(5):
        assert client.post(f"/u/{'e' * 25}{'abcde'[n]}/anthropic/v1/messages", json={}, headers=page).status_code == 403
        assert (
            client.get(f"{SERVER}/whoami", headers={**page, "x-latet-token": f"latet_pat_{n:043d}"}).status_code == 403
        )
    assert client.post(f"/u/{'f' * 26}/anthropic/v1/messages", json={}).status_code == 403  # still within its limit
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "9" * 43}).status_code == 401


def test_a_page_of_another_site_spends_no_session_limit_of_its_visitors_address(server, client, monkeypatch):
    """A page of a sibling site can set a session cookie of the server's name in its visitor's browser and have the
    browser send it to the dashboard's API (a same-site request carries it): refused before anything is looked up, it
    counts nothing against the address the visitor's own dashboard shares (Task 14's re-review, N2). The dashboard's own
    requests are its page's (``same-origin``)."""
    monkeypatch.setattr(app_module, "LOOKUP_LIMIT", 2)
    looked = []
    subject_of = server.sign_ins.subject_of
    monkeypatch.setattr(server.sign_ins, "subject_of", lambda sid: looked.append(sid) or subject_of(sid))
    junk = {"cookie": "latet_session=planted-by-a-sibling"}
    pages = [{"sec-fetch-site": "same-site"}, {"sec-fetch-site": "cross-site"}, {"origin": "https://shop.acme.lan"}]
    for page in pages:
        for _ in range(3):
            answer = client.get(f"{API}/me", headers={**junk, **page})
            assert answer.status_code == 403 and answer.headers.get("retry-after") is None, page
    counted = server.bus.get(server.bus.rate_key(machines.miss_key("testclient")))
    assert looked == [] and counted is None  # nothing looked up, nothing counted
    _, token = _enrolled(server, client)
    _signed_in(client, token)
    own = {"sec-fetch-site": "same-origin", "origin": URL}
    assert client.get(f"{API}/me", headers=own).status_code == 200  # the dashboard's own page
    assert client.get(f"{API}/me").status_code == 200  # a request no browser marks (the CLI, a test)


def test_a_sign_in_link_opens_nothing_for_a_person_disabled_unheard(server, client):
    person, token = _enrolled(server, client)
    sid = _signed_in(client, token)
    assert client.get(f"{API}/me").status_code == 200  # this instance holds the person, enabled, and their space
    client.cookies.clear()
    link = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"]
    with server.db.connection() as conn:  # no message: as when it was lost
        conn.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (person.id,))
    again = client.get(link.removeprefix(URL), follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].endswith("login=expired")  # as PostgreSQL has them
    assert "set-cookie" not in again.headers
    server.identities.forget()  # past the five seconds the memory holds them
    client.cookies.set("latet_session", sid, domain="latet.acme.lan", path="/_latet")
    assert client.get(f"{API}/me").status_code == 401  # their session leads to their space no more


def test_a_sign_in_link_opens_nothing_once_the_token_that_asked_for_it_is_gone(server, client):
    """S6 (area 6, M1): a code is bound to the token that asked for it, and opens a session only while that token is
    there, as PostgreSQL has it at the sign-in. Asked of an instance whose memory still held the token revoked on
    another (its message late, or lost: the race with ``end(subject)``), the code opens nothing."""
    person, token = _enrolled(server, client)
    mine = _mine(token)
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # held in this instance's memory
    asked = client.post(f"{SERVER}/login-code", headers=mine)
    assert asked.status_code == 200
    with server.db.connection() as conn:  # revoked through another instance, its message not heard here yet
        conn.execute("DELETE FROM tokens WHERE identity_id = %s", (person.id,))
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # the memory still says it is valid
    # A new link is refused at once: its computer key is checked as PostgreSQL has the token's row now (R16).
    assert client.post(f"{SERVER}/login-code", headers=mine).status_code == 401
    again = client.get(asked.json()["url"].removeprefix(URL), follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].endswith("login=expired")
    assert "set-cookie" not in again.headers and client.get(f"{API}/me").status_code == 401


def test_a_token_revoked_between_the_sign_ins_check_and_its_session_leaves_no_session(server, client, monkeypatch):
    """R6 (the re-review's Minor on S6): the token is checked, then the session opened. A revocation whose end of the
    person's sessions runs between the two would leave a session of twelve hours: the token is checked again once the
    session is opened, and the session ended when the token is gone."""
    person, token = _enrolled(server, client)
    link = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"]
    opened, sessions = server.sign_ins.open, []

    def revoked_meanwhile(subject):
        with server.db.connection() as conn:  # revoked on another instance, between the check and the opening
            conn.execute("DELETE FROM tokens WHERE identity_id = %s", (person.id,))
        server.sign_ins.end(subject=subject)  # the revocation's end of the person's sessions: before this one
        sessions.append(opened(subject))
        return sessions[-1]

    monkeypatch.setattr(server.sign_ins, "open", revoked_meanwhile)
    signed = client.get(link.removeprefix(URL), follow_redirects=False)
    assert signed.status_code == 303 and signed.headers["location"].endswith("login=expired")
    assert "set-cookie" not in signed.headers and [server.sign_ins.subject_of(sid) for sid in sessions] == [None]


def test_valkeys_key_names_hold_no_clients_address_and_no_one_signed_in(server, prefix):
    """S7 (area 6, M3): after a person's sign-in, and refusals from a client's address that each limit counts (unknown
    tokens, addresses, sessions, codes, administration tokens and calls with none), no name of Valkey's keys holds the
    address, the person's id, or the plain hash of it."""
    with TestClient(create_server_app(server), base_url=URL, client=("203.0.113.9", 50000)) as client:
        person, token = _enrolled(server, client)
        _signed_in(client, token)
        client.cookies.clear()
        client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "0" * 43})
        client.post(f"/u/{'b' * 26}/anthropic/v1/messages", json={})
        client.get(f"{API}/me", cookies={"latet_session": "s" * 43})
        client.post(f"{SERVER}/enrol", json={"code": "nope", "label": "x"})
        client.get(f"{SERVER}/admin/status", headers={"x-latet-admin": "latet_adm_" + "0" * 43})
        client.get(f"{SERVER}/admin/status")
        client.get("/_latet/ui/login?code=nope", follow_redirects=False)  # a sign-in link of no one (R1)
    names = b"\n".join(server.bus.client.scan_iter(match=prefix + "*", count=1000))
    assert b"rate:" in names and b"signin:subject:" in names
    for who in (b"203.0.113.9", person.id.encode(), live_module._hash(person.id).encode()):
        assert who not in names, who


def test_a_sign_in_link_opens_the_persons_dashboard_once(server, client):
    _, token = _enrolled(server, client)
    assert client.get(f"{API}/me").status_code == 401
    link = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"]
    assert link.startswith(f"{URL}/_latet/ui/login?code=")
    signed = client.get(link.removeprefix(URL), follow_redirects=False)
    assert signed.status_code == 303 and "secure" in signed.headers["set-cookie"].lower()
    assert client.get(f"{API}/me").status_code == 200
    client.cookies.clear()
    again = client.get(link.removeprefix(URL), follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].endswith("login=expired")
    assert client.get(f"{API}/me").status_code == 401
    assert client.get("/_latet/ui/").status_code == 200  # the dashboard's page itself needs no sign-in


def test_an_instance_that_loses_valkey_drops_its_spaces_and_is_not_ready_until_it_hears_again(server, client):
    _, token = _enrolled(server, client)
    mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"
    assert client.get(ping, headers=mine).status_code == 200 and server.spaces.count() == 1
    assert drop_listeners(server.bus) == 1
    lost = None
    for _ in range(250):
        answer = client.get("/_latet/ready")
        if answer.status_code == 503 and server.spaces.count() == 0:
            lost = answer.json()
            break
        time.sleep(0.02)
    assert lost == {"ready": False}  # why is in the instance's log and its metrics, not in the answer
    for _ in range(250):
        if client.get("/_latet/ready").status_code == 200:
            break
        time.sleep(0.02)
    assert client.get("/_latet/ready").status_code == 200
    assert client.get(ping, headers=mine).status_code == 200  # its space made again from PostgreSQL


def test_unknown_tokens_and_addresses_are_limited_by_address(server, client, monkeypatch):
    """One budget of misses for each address, every kind together (A2): the address that spent it on tokens is refused
    an unknown address at once, and another address has a budget of its own."""
    monkeypatch.setattr("latet.server.machines.LOOKUP_LIMIT", 3)
    monkeypatch.setattr("latet.server.app.LOOKUP_LIMIT", 3)
    guesses = [f"latet_pat_{n:043d}" for n in range(5)]
    assert [client.get(f"{SERVER}/whoami", headers={"x-latet-token": g}).status_code for g in guesses] == [
        401,
        401,
        401,
        429,
        429,
    ]
    paths = [f"/u/{'a' * 25}{c}/anthropic/v1/messages" for c in "bcdefg"]
    assert [client.post(path, json={}).status_code for path in paths] == [429] * 6
    other = TestClient(create_server_app(server), base_url=URL, client=("203.0.113.7", 50000))  # no lifespan of its own
    assert [other.post(path, json={}).status_code for path in paths] == [403, 403, 403, 429, 429, 429]
    _, token = _enrolled(server, client)  # a token and an address the server knows are never limited: misses are
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200
    assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers={"x-latet-token": token}).status_code == 200


def test_a_body_larger_than_the_limit_is_refused_before_it_is_read(server):
    server.settings = server.settings.model_copy(update={"max_request_mb": 1})
    with TestClient(create_server_app(server), base_url=URL) as client:
        _, token = _enrolled(server, client)
        base, mine = f"/u/{route_key(token)}", {"x-latet-token": token}
        too_much = b"x" * (1024 * 1024 + 1)
        declared = client.post(f"{base}{ENGINE}/mask", content=too_much, headers=mine)  # its length says so
        assert declared.status_code == 413 and "max_request_mb" in declared.text
        grown = client.post(f"{base}{ENGINE}/mask", content=iter([too_much]), headers=mine)  # chunked: cut short
        assert grown.status_code == 413
        piece = b"x" * (256 * 1024)
        many = client.post(f"{base}{ENGINE}/mask", content=iter([piece] * 5), headers=mine)  # many chunks, past it
        assert many.status_code == 413 and "max_request_mb" in many.text
        fine = client.post(f"{base}{ENGINE}/mask", json={"session": "s", "items": []}, headers=mine)
        assert fine.status_code == 200


# --- what a compressed body decodes to (the pre-read's C1) -------------------------------------------------------------


def _answered() -> httpx.Response:
    """A provider's answer, as a stream: the gateway relays it as it comes."""
    body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude", "content": []}
    data = json.dumps(body).encode()
    return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(data))


def _provider(monkeypatch):
    """The providers every space of the test reaches: a stand-in that records what it gets, never the network."""
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _answered()

    monkeypatch.setattr(
        "latet.gateway.app.make_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return sent


def test_a_compressed_body_that_would_decode_past_the_limit_is_refused_and_nothing_is_sent(server, monkeypatch):
    sent = _provider(monkeypatch)
    server.settings = server.settings.model_copy(update={"max_request_mb": 1})
    big = {"model": "claude", "max_tokens": 8, "messages": [{"role": "user", "content": "x" * (4 * 1024 * 1024)}]}
    small = {"model": "claude", "max_tokens": 8, "messages": [{"role": "user", "content": "hello"}]}
    raw = json.dumps(big).encode()
    with TestClient(create_server_app(server), base_url=URL) as client:
        _, token = _enrolled(server, client)
        address, headers = f"/u/{route_key(token)}/anthropic/v1/messages", {"x-api-key": "sk-test"}
        packed = {
            "gzip": gzip.compress(raw),
            "deflate": zlib.compress(raw),
            "br": brotli.compress(raw),
            "zstd": zstandard.ZstdCompressor().compress(raw),
            "gzip, gzip": gzip.compress(gzip.compress(raw)),
        }
        for coding, body in packed.items():
            assert len(body) < 64 * 1024  # a few KiB on the wire, four MiB once decoded
            stopped = client.post(address, content=body, headers={**headers, "content-encoding": coding})
            assert stopped.status_code == 413 and "max_request_mb" in stopped.text, coding
        assert sent == []  # nothing reached the provider
        fine = client.post(
            address, content=gzip.compress(json.dumps(small).encode()), headers={**headers, "content-encoding": "gzip"}
        )
        assert fine.status_code == 200 and len(sent) == 1  # a compressed body under the limit leaves as before
        assert client.get("/_latet/health").status_code == 200  # and the instance serves on


# --- nothing under an identity's address but its bridge (Task 11's hand-off, load-bearing) ----------------------------


async def _asgi(app, path, raw=None, headers=(), method="GET", body=b"", client="10.0.3.9", sent=None, query=b""):
    """The status, and the scope, of a request with exactly this path and raw path, as a client's bytes make them (an
    HTTP client of Python's would write the path otherwise: it takes the dot segments out). ``sent``: a list that gets
    what the app sends, its headers included; ``query``: the query string."""
    sent, waiting = [] if sent is None else sent, [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if waiting:
            return waiting.pop()
        await asyncio.Event().wait()  # the client stays: it goes only when the request is cancelled
        raise AssertionError("unreachable")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode() if raw is None else raw, "query_string": query, "root_path": "",
        "headers": [(b"host", b"latet.acme.lan"), *headers], "client": (client, 50000),
        "server": ("latet.acme.lan", 443),
    }  # fmt: skip
    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), scope


def _location(sent: list[dict]) -> str:
    """Where an answer ``_asgi`` heard sends the client (its ``location``)."""
    [start] = [message for message in sent if message["type"] == "http.response.start"]
    return dict(start["headers"])[b"location"].decode()


async def test_nothing_under_an_identitys_address_but_its_computers_calls_reaches_its_space(server, monkeypatch):
    app = create_server_app(server)
    reached = []
    real = server.spaces.app

    async def spied(identity):
        reached.append(identity.id)
        return await real(identity)

    monkeypatch.setattr(server.spaces, "app", spied)
    async with app.router.lifespan_context(app):
        person, code = await asyncio.to_thread(server.identities.add_person, "Emily Carter")
        _, token, _ = await asyncio.to_thread(server.identities.enrol, code, "laptop")
        base, mine = f"/u/{route_key(token)}", [(b"x-latet-token", token.encode())]
        # The space's app still mounts the CLI's calls (/_latet/admin/*), which on a server trust the identity the
        # server names: only the plugin's bridge and the CLI's two by POST (E0c's Task 9) are let through, written
        # as they are read.
        for rest in (
            "/_latet/admin/known", "/_latet/admin/login-code", "/_latet/admin/reload", "/_latet/admin/destinations",
            "/_latet/admin/user-name", "/_latet/admin/changes/c1", "/_latet/admin/shutdown", "/_latet/api/v1/me",
            "/_latet/ui/", "/_latet/ui/login", "/_latet/health", "/_latet", "/_latet/engine", "/_latet/engine/v2/ping",
            "/_latet/enginex/v1/ping", "/_latet/engine/v1/../../admin/known", "/_latet/engine/v1/./ping",
            "/_latet//engine/v1/ping", "/_latet/engine/v1/ping/",
        ):  # fmt: skip
            status, scope = await _asgi(app, base + rest, headers=mine)
            assert status == 404 and scope.get(REFUSED), rest
        # The CLI's two by any other method, the CLI's others by POST, the bridge by a method none of its routes takes,
        # or written otherwise (a slash after it, another case, an escaped letter): 404, before anything is looked up.
        for method, rest in (
            *((m, call) for m in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
              for call in ("/_latet/admin/destinations", "/_latet/admin/user-name")),
            ("POST", "/_latet/admin/known"), ("POST", "/_latet/admin/login-code"), ("POST", "/_latet/admin/reload"),
            ("POST", "/_latet/admin/changes/c1"), ("POST", "/_latet/admin/shutdown"),
            ("POST", "/_latet/admin/user-name/"), ("POST", "/_latet/admin/User-name"), ("POST", "/_latet/admin//user-name"),
            ("PUT", f"{ENGINE}/mask"), ("PATCH", f"{ENGINE}/mask"), ("OPTIONS", f"{ENGINE}/mask"), ("HEAD", f"{ENGINE}/ping"),
        ):  # fmt: skip
            status, scope = await _asgi(app, base + rest, headers=mine, method=method)
            assert status == 404 and scope.get(REFUSED), (method, rest)
        escaped = f"{base}/_latet/admin/user%2Dname"
        status, _ = await _asgi(app, f"{base}/_latet/admin/user-name", escaped.encode(), headers=mine, method="POST")
        assert status == 404
        for rest in (f"{ENGINE}/ping", "/_latet/admin/user-name", "/_latet/admin/destinations"):  # nor as a WebSocket
            closed = await _websocket(app, base + rest, headers=mine)
            assert closed == [{"type": "websocket.close", "code": 4404, "reason": "there is no such route"}], rest
        for path, raw in (
            (f"{base}/_latet/admin/known", f"{base}/%5Flatet/admin/known"),
            (f"{base}/_latet/engine/v1/../../admin/known", f"{base}/_latet/engine/v1/%2E%2E/%2E%2E/admin/known"),
            (f"{base}/_latet/engine/v1/ping", f"{base}/_latet/engine/v1/p%69ng"),
            (f"{base}/_latet/engine/v1/ping", f"{base}%2F_latet/engine/v1/ping"),
        ):
            status, _ = await _asgi(app, path, raw.encode(), headers=mine)
            assert status in (403, 404), raw
        # The route key and a slash, as they are written: an address written otherwise is no one's.
        for raw in (f"{base}%2Fanthropic/v1/messages", f"/u%2F{route_key(token)}/anthropic/v1/messages"):
            status, _ = await _asgi(app, f"{base}/anthropic/v1/messages", raw.encode(), headers=mine)
            assert status == 403, raw
        assert reached == []
        status, _ = await _asgi(app, f"{base}{ENGINE}/ping", headers=mine)
        assert status == 200 and reached == [person.id]
        # The CLI's two, by POST with the owner's token: its space reads them (the user name's fake, a destination).
        json_body = [*mine, (b"content-type", b"application/json")]
        for rest, body in (("/_latet/admin/user-name", b'{"name": "emily"}'), ("/_latet/admin/destinations", b"{}")):
            status, scope = await _asgi(app, base + rest, headers=json_body, method="POST", body=body)
            assert status in (200, 400) and not scope.get(REFUSED), rest
        assert reached == [person.id] * 3


# --- the token header, the Host (the pre-read's M1) ---------------------------------------------------------------------


def test_a_token_sent_twice_is_no_token_and_a_host_sent_twice_no_host(server, client):
    _, token = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")
    ping = f"/u/{route_key(token)}{ENGINE}/ping"
    assert client.get(ping, headers={"x-latet-token": token}).status_code == 200
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200
    for twice in ([("x-latet-token", token), ("x-latet-token", other)], [("x-latet-token", other), ("x-latet-token", token)], [("x-latet-token", token)] * 2):  # fmt: skip
        assert client.get(ping, headers=twice).status_code == 401  # the gate and the machines read it alike
        assert client.get(f"{SERVER}/whoami", headers=twice).status_code == 401
    assert client.get("/_latet/ui/", headers=[("host", "latet.acme.lan"), ("host", "evil.example")]).status_code == 403
    assert client.get("/_latet/ui/", headers=[("host", "evil.example"), ("host", "latet.acme.lan")]).status_code == 403


# --- readiness, health, and what every answer says (M2, M4) ------------------------------------------------------------


def test_readiness_asks_the_stores_at_most_once_a_second_whoever_asks(server, client, monkeypatch):
    asked = []
    for name, store in (("database", server.db), ("valkey", server.bus)):
        real = store.ping
        monkeypatch.setattr(store, "ping", lambda real=real, name=name: asked.append(name) or real())
    monkeypatch.setattr(app_module, "READY_SECONDS", 60.0)
    probes = [client.get("/_latet/ready", headers={"host": "10.0.3.7:5283"}).status_code for _ in range(30)]
    assert set(probes) == {200} and sorted(asked) == ["database", "valkey"]  # a flood costs one look at each
    monkeypatch.setattr(app_module, "READY_SECONDS", 0.0)
    asked.clear()
    assert client.get("/_latet/ready").status_code == 200 and sorted(asked) == ["database", "valkey"]
    server.listening.clear()  # what it hears is looked at each time
    assert client.get("/_latet/ready").status_code == 503
    server.listening.set()


def test_every_answer_of_the_server_says_nosniff(server, client):
    _, token = _enrolled(server, client)
    mine = _mine(token)
    _signed_in(client, token)
    answers = [
        client.get("/_latet/health"),
        client.get("/_latet/ready"),
        client.get(f"{SERVER}/whoami", headers=mine),
        client.get(f"{SERVER}/whoami"),  # a refusal of the machines' calls
        client.get(f"{API}/me"),  # the dashboard's API, through the gate
        client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers=mine),  # the bridge, an identity's app
        client.get("/_latet/ui/", headers={"host": "evil.example"}),  # a refusal of the gate
        client.post(f"/u/{route_key(token)}{ENGINE}/mask", content=b"x" * (33 * 1024 * 1024), headers=mine),  # 413
        client.get("/nothing-here"),
    ]
    assert [a.headers.get("x-content-type-options") for a in answers] == ["nosniff"] * len(answers)


# --- signing out (M3) ---------------------------------------------------------------------------------------------------


def test_a_person_signs_out_and_their_session_ends_wherever_it_is_checked(server, client, prefix):
    _, token = _enrolled(server, client)
    sid = _signed_in(client, token)
    assert client.get(f"{API}/me").status_code == 200
    assert client.post(f"{API}/logout").status_code == 403  # no page of another origin signs anyone out
    out = client.post(f"{API}/logout", headers=UI)
    assert out.status_code == 200 and out.json() == {"ok": True}
    cookie = out.headers["set-cookie"].lower()
    assert "latet_session=" in cookie and "max-age=0" in cookie and "secure" in cookie
    assert server.sign_ins.subject_of(sid) is None
    assert list(server.bus.client.scan_iter(match=f"{prefix}signin:session:*")) == []
    client.cookies.set("latet_session", sid, domain="latet.acme.lan", path="/_latet")  # a copy of the cookie
    assert client.get(f"{API}/me").status_code == 401


# --- a person's identity from each of their computers (R-28, the pre-read's M5) ----------------------------------------


def test_each_computer_adds_its_names_to_its_persons_identity_and_takes_none_away(server, client):
    person, laptop = _enrolled(server, client)
    desktop = _another_computer(server, client, person)

    def put(token, body):
        return client.put(f"{SERVER}/identity", json=body, headers=_mine(token))

    first = {
        "usernames": ["emily"],
        "hostname": "emily-laptop",
        "full_name": "Emily Carter",
        "emails": ["emily@acme.com"],
    }
    second = {"username": "ecarter", "hostname": "emily-desktop", "full_name": "E. Carter", "emails": ["e.c@acme.com"]}
    assert put(laptop, first).json() == {"ok": True}
    assert put(desktop, second).json() == {"ok": True}
    # A computer describes itself: it never clears what another said, nor turns its person's identity off.
    assert put(desktop, {"enabled": False, "usernames": [], "emails": [], "hostname": None}).json() == {"ok": True}
    assert put(desktop, {"enabled": False, "usernames": ["e-carter"]}).json() == {"ok": True}  # its name, and no more
    assert put(laptop, {"usernames": ["EMILY"], "emails": ["Emily@Acme.com"]}).json() == {"ok": True}  # known already
    runtime = client.portal.call(server.spaces.runtime, person)
    held = runtime.config.identity
    assert held.enabled and held.usernames == ["emily", "ecarter", "e-carter"]
    assert held.emails == ["emily@acme.com", "e.c@acme.com"]
    assert (held.hostname, held.hostnames, held.full_name) == ("emily-laptop", ["emily-desktop"], "Emily Carter")
    texts = [
        "cd /home/emily/billing-api",
        "cd /home/ecarter/web",
        "ssh emily-laptop",
        "ssh emily-desktop",
        "by Emily Carter",
    ]
    masked = _masked(client, laptop, texts)
    for value in ("/emily/", "/ecarter/", "emily-laptop", "emily-desktop", "Emily Carter"):
        assert all(value not in text for text in masked), value
    for body in (
        "not json",
        "[1, 2]",
        "null",
        '{"usernames": ["emily"',
        '{"usernames": "emily"}',
        '{"hostnames": "x"}',
    ):
        refused = client.put(f"{SERVER}/identity", content=body, headers=_mine(laptop))
        assert refused.status_code == 400, body
    assert put(laptop, {"usernames": [f"user{n}" for n in range(machines.NAMES_KEPT + 1)]}).status_code == 400
    assert put(laptop, {"hostname": "h" * (machines.NAME_CHARS + 1)}).status_code == 400
    room = machines.NAMES_KEPT - 3
    assert put(laptop, {"usernames": [f"user{n}" for n in range(room)]}).json() == {"ok": True}  # up to the cap
    full = put(desktop, {"usernames": ["one-more"]})
    assert full.status_code == 409 and "one-more" not in full.text  # past it nothing is added, and it says so
    assert len(client.portal.call(server.spaces.runtime, person).config.identity.usernames) == machines.NAMES_KEPT


def test_a_body_larger_than_a_machines_call_takes_is_refused_unparsed(server, client, monkeypatch):
    """A code and a label, an identity: small by nature, and parsed on the event loop every identity shares. Past what
    they take, refused whatever max_request_mb lets through."""
    parsed = []
    real = machines._object_of
    monkeypatch.setattr(machines, "_object_of", lambda raw: parsed.append(len(raw)) or real(raw))
    _, token = _enrolled(server, client)
    parsed.clear()
    label = "x" * machines.ENROL_BYTES
    assert client.post(f"{SERVER}/enrol", json={"code": "nope", "label": label}).status_code == 413
    huge = {"usernames": ["emily"], "full_name": "E" * machines.IDENTITY_BYTES}
    put = client.put(f"{SERVER}/identity", json=huge, headers=_mine(token))
    assert put.status_code == 413 and parsed == []  # neither was parsed
    piece = json.dumps({"usernames": ["emily"]}).encode()
    chunked = client.put(f"{SERVER}/identity", content=iter([piece[:-1], b" " * machines.IDENTITY_BYTES, b"}"]), headers=_mine(token))  # fmt: skip
    assert chunked.status_code == 413 and parsed == []  # its length unsaid: read no further than the limit


def test_sign_in_links_are_limited_for_each_person(server, client, monkeypatch):
    monkeypatch.setattr(machines, "LOGIN_CODES", 2)
    _, token = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")

    def ask(secret):
        return client.post(f"{SERVER}/login-code", headers=_mine(secret)).status_code

    assert [ask(token) for _ in range(3)] == [200, 200, 429]
    assert ask(other) == 200  # another person's own count


# --- the limit asked before any lookup, nothing on the event loop (R-6, the pre-read's I1) -----------------------------


def _counting(server, monkeypatch):
    """The lookups of tokens and addresses (``Identities._owner``) and the slots taken (``Bus.slot``, which ``take``
    asks too) from now on."""
    lookups, slots = [], []
    owner, slot = server.identities._owner, server.bus.slot
    monkeypatch.setattr(server.identities, "_owner", lambda *args: lookups.append(args) or owner(*args))
    monkeypatch.setattr(server.bus, "slot", lambda *args: slots.append(args) or slot(*args))
    return lookups, slots


def test_beyond_the_limit_nothing_is_looked_up_and_what_the_instance_holds_costs_no_store(server, client, monkeypatch):
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)
    monkeypatch.setattr(app_module, "LOOKUP_LIMIT", 2)
    _, token = _enrolled(server, client)
    mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"
    lookups, slots = _counting(server, monkeypatch)
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # looked up, and held from now on
    assert client.get(ping, headers=mine).status_code == 200
    lookups.clear()
    for _ in range(3):
        assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200
        assert client.get(ping, headers=mine).status_code == 200
    assert lookups == [] and slots == []  # held: neither PostgreSQL nor Valkey asked
    guesses = [f"latet_pat_{n:043d}" for n in range(6)]
    assert [client.get(f"{SERVER}/whoami", headers={"x-latet-token": g}).status_code for g in guesses] == [401, 401] + [
        429
    ] * 4
    assert len(lookups) == 2  # past the limit, no lookup
    lookups.clear()
    routes = [f"/u/{'b' * 25}{c}/anthropic/v1/messages" for c in "cdefgh"]
    assert [client.post(path, json={}).status_code for path in routes] == [429] * 6
    assert lookups == []  # the address's one budget, spent on tokens (A2): no address looked up either


def test_a_credential_found_good_skips_a_full_limit_and_is_still_looked_up(server, client, monkeypatch):
    """Important 3 (the coordinator's ruling): what this instance found good within the hour passes an address past its
    limit, looked up as ever, so that a stale computer behind the same address locks out none of its colleagues; one
    that has gone since is refused and counted, then limited like any other."""
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)
    monkeypatch.setattr(app_module, "LOOKUP_LIMIT", 2)
    person, token = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")
    for secret in (token, other):  # found good by this instance
        assert client.get(f"/u/{route_key(secret)}{ENGINE}/ping", headers={"x-latet-token": secret}).status_code == 200
    session = _signed_in(client, token)  # a dashboard's session opened here, its cookie set aside for the end
    client.cookies.clear()
    stale = [f"/u/{'c' * 25}{c}/anthropic/v1/messages" for c in "defg"]  # a revoked computer's address, say
    guesses = [f"latet_pat_{n:043d}" for n in range(4)]
    assert [client.post(path, json={}).status_code for path in stale] == [403, 403, 429, 429]
    assert [client.get(f"{SERVER}/whoami", headers={"x-latet-token": g}).status_code for g in guesses][-1] == 429
    server.identities.forget()  # past the five seconds the memory holds a lookup
    lookups, slots = _counting(server, monkeypatch)
    mine = _mine(token)
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200
    assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers=mine).status_code == 200
    assert len(lookups) == 2 and slots == []  # looked up, the full limit not asked
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "7" * 43}).status_code == 429
    assert len(lookups) == 2  # a token never found good: refused with no lookup
    server.identities.revoke_secret(other)  # found good, gone since
    server.identities.forget()
    lookups.clear()
    misses = server.bus.rate_key(machines.miss_key("testclient"))
    gone, counted = {"x-latet-token": other}, int(server.bus.get(misses))
    assert client.get(f"{SERVER}/whoami", headers=gone).status_code == 401 and len(lookups) == 1  # looked up, counted
    assert int(server.bus.get(misses)) == counted + 1
    assert client.get(f"{SERVER}/whoami", headers=gone).status_code == 429 and len(lookups) == 1  # then limited
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # its colleague still served
    assert server.identities.by_secret(token).id == person.id
    # A dashboard's session alike: the address's one budget of misses is spent (A2), so a guessed session is refused at
    # once; the one opened here passes it from its first request, looked up all the same, no slot taken.
    guessed = [client.get(f"{API}/me", cookies={"latet_session": c * 43}).status_code for c in "rst"]
    assert guessed == [429, 429, 429]
    slots.clear()
    assert client.get(f"{API}/me", cookies={"latet_session": session}).status_code == 200 and slots == []


def test_every_429_of_a_limit_says_when_to_try_again(server, client, monkeypatch):
    for module in (machines, app_module):
        monkeypatch.setattr(module, "LOOKUP_LIMIT", 1)
    monkeypatch.setattr(machines, "ENROL_LIMIT", 1)
    monkeypatch.setattr(machines, "LOGIN_CODES", 1)
    _, token = _enrolled(server, client)
    mine = _mine(token)
    client.post(f"{SERVER}/login-code", headers=mine)
    answers = {
        "an unknown address": [client.post(f"/u/{c * 26}/anthropic/v1/messages", json={}) for c in "de"][-1],
        "an unknown token": [
            client.get(f"{SERVER}/whoami", headers={"x-latet-token": f"latet_pat_{n:043d}"}) for n in range(2)
        ][-1],
        "a code tried": [  # the enrolment gave its slot back: the second failed try is the one past the limit
            client.post(f"{SERVER}/enrol", json={"code": "nope", "label": "x"}) for _ in range(2)
        ][-1],
        "a sign-in link": client.post(f"{SERVER}/login-code", headers=mine),
        "an unknown session": [
            client.get(f"{API}/me", cookies={"latet_session": c * 43})
            for c in "pq"  # never found good
        ][-1],
    }
    for what, answer in answers.items():
        assert answer.status_code == 429, what
        assert 1 <= int(answer.headers["retry-after"]) <= 600, what


def _held_up(server, client, monkeypatch, target, name, request):
    """While ``target.name`` waits on a store (in the gate's threads, never the event loop's), ``request`` waits for
    it, and a request that needs no store is answered at once."""
    release, entered = threading.Event(), threading.Event()
    real = getattr(target, name)

    def slow(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, slow)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pending = pool.submit(request)
        assert entered.wait(5)
        started = time.monotonic()
        assert client.get("/_latet/health").status_code == 200
        assert time.monotonic() - started < 2.0  # not after the slow store, ten seconds on
        release.set()
        return pending.result(15).status_code


def test_a_slow_store_holds_up_no_other_request(server, client, monkeypatch):
    _, token = _enrolled(server, client)
    _signed_in(client, token)
    route = f"/u/{route_key(token)}{ENGINE}/ping"
    server.identities.forget()  # the sign-in held the token: looked up again from now on
    whoami = functools.partial(client.get, f"{SERVER}/whoami", headers={"x-latet-token": token})
    assert _held_up(server, client, monkeypatch, server.identities, "_owner", whoami) == 200
    monkeypatch.undo()
    server.identities.forget()
    ping = functools.partial(client.get, route, headers={"x-latet-token": token})
    assert _held_up(server, client, monkeypatch, server.identities, "_owner", ping) == 200
    monkeypatch.undo()
    me = functools.partial(client.get, f"{API}/me")
    assert _held_up(server, client, monkeypatch, server.sign_ins, "subject_of", me) == 200
    monkeypatch.undo()
    server.identities.forget()
    guess = functools.partial(client.get, f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "0" * 43})
    assert _held_up(server, client, monkeypatch, server.bus, "slot", guess) == 401
    monkeypatch.undo()
    monkeypatch.setattr(app_module, "READY_SECONDS", 0.0)
    ready = functools.partial(client.get, "/_latet/ready")
    assert _held_up(server, client, monkeypatch, server.db, "ping", ready) == 200


def test_a_store_that_does_not_answer_fails_the_request_closed_and_is_named_by_its_type(
    server, client, monkeypatch, caplog
):
    _, token = _enrolled(server, client)

    def unanswered(*args, **kwargs):
        raise redis.TimeoutError("Timeout reading from 127.0.0.1:56379")

    monkeypatch.setattr(machines, "UNANSWERED", Paced())  # said once a minute: whatever an earlier test made happen
    monkeypatch.setattr(server.bus, "slot", unanswered)
    monkeypatch.setattr(server.identities, "_owner", unanswered)  # the token was found good: looked up past the limit
    with caplog.at_level(logging.WARNING, logger="latet"):
        assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 503
        assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers={"x-latet-token": token}).status_code == 503
        monkeypatch.setattr(server.sign_ins, "subject_of", unanswered)
        client.cookies.set("latet_session", "s" * 43, domain="latet.acme.lan", path="/_latet")
        assert client.get(f"{API}/me").status_code == 503
    said = [record.getMessage() for record in caplog.records]
    assert any("TimeoutError" in line for line in said)
    assert not any("56379" in line or "Timeout reading" in line for line in said)
    assert all(record.exc_info is None for record in caplog.records)


# --- the shared limiter, the two pools (fix round 1: Importants 2 and 3, Task 15's ruling) ---------------------------


def _route_keys(count):
    return ["".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(26)) for _ in range(count)]


async def test_a_burst_of_unknown_tokens_and_addresses_makes_no_more_lookups_than_the_limit(server, monkeypatch):
    """The slot is taken before the lookup, in one step: a burst from one address, all at once, makes as many lookups as
    the limit and no more (R-6 (3), whatever the gate's threads), its tokens and its addresses from one budget (A2)."""
    for module in (machines, app_module):
        monkeypatch.setattr(module, "LOOKUP_LIMIT", 3)
    app = create_server_app(server)
    async with app.router.lifespan_context(app):
        lookups, _ = _counting(server, monkeypatch)
        tokens = [(b"x-latet-token", f"latet_pat_{n:043d}".encode()) for n in range(100)]
        answers = await asyncio.gather(
            *(_asgi(app, f"{SERVER}/whoami", headers=[token]) for token in tokens),
            *(_asgi(app, f"/u/{route}/anthropic/v1/messages") for route in _route_keys(100)),
        )
    statuses = [status for status, _ in answers]
    assert len(lookups) <= 3 and statuses.count(429) >= 197


def test_a_good_credential_gives_its_slot_back(server, client, monkeypatch, prefix):
    _, token = _enrolled(server, client)
    mine = _mine(token)
    for _ in range(10):  # each time as if this instance had never found it good: a slot taken, and given back
        server.known_secrets, server.known_routes = machines.Known(), machines.Known()
        server.identities.forget()
        assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200
        assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers=mine).status_code == 200
    assert server.bus.get(server.bus.rate_key(machines.miss_key("testclient"))) in (None, b"0")


async def test_an_ipv6_address_is_counted_by_its_64_and_its_48_and_a_mapped_one_as_its_ipv4(server, monkeypatch):
    """R3 (the re-review's out-of-scope note): a host given a /48 has 65,536 /64 buckets; its misses are counted under
    its /48 too, which counts as one host: another /64 of the same /48 is refused once the /48 is past its limit, and
    another /48 has a count of its own."""
    for module in (machines, app_module):
        monkeypatch.setattr(module, "LOOKUP_LIMIT", 3)
    app = create_server_app(server)
    guesses = iter(f"latet_pat_{n:043d}" for n in range(100))

    async def guess(client):
        token = [(b"x-latet-token", next(guesses).encode())]
        return (await _asgi(app, f"{SERVER}/whoami", headers=token, client=client))[0]

    async with app.router.lifespan_context(app):
        lookups, _ = _counting(server, monkeypatch)
        for host in range(5):  # five addresses of one /64, four guesses each
            for _ in range(4):
                await guess(f"2001:db8:1:2::{host + 1:x}")
        assert len(lookups) == 3
        assert await guess("2001:db8:1:3::1") == 429 and len(lookups) == 3  # another /64 of the same /48: refused
        assert await guess("2001:db8:2:3::1") == 401 and len(lookups) == 4  # another /48: a count of its own
        assert [await guess("::ffff:198.51.100.7") for _ in range(3)] == [401, 401, 401]
        assert await guess("198.51.100.7") == 429  # the same address, written as an IPv4 one


async def test_lookups_held_up_hold_no_other_thread_of_the_process(server, monkeypatch):
    """The work done before a credential is known runs in threads of its own: with every one of them waiting on a
    store, the rest of the process (the spaces' builds, ``asyncio.to_thread``) is held up by none."""
    app = create_server_app(server)
    release, entered = threading.Event(), []
    real = server.identities._owner

    def slow(*args):
        entered.append(threading.current_thread().name)
        release.wait(10)
        return real(*args)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr(server.identities, "_owner", slow)
        tokens = [(b"x-latet-token", f"latet_pat_{n:043d}".encode()) for n in range(machines.CREDENTIAL_THREADS + 2)]
        calls = [asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=[token])) for token in tokens]
        try:
            await until(lambda: len(entered) == machines.CREDENTIAL_THREADS)
            started = time.monotonic()
            assert await asyncio.to_thread(lambda: 7) == 7
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            await asyncio.gather(*calls)
    assert len(entered) == len(tokens) and all(name.startswith("latet-credentials") for name in entered)


# --- no one holds the credentials' threads (the fix wave's S1: final review area 6 I1, area 3 I2) ---------------------

# What readiness and another address's lookup wait for at most while one address floods the server: Valkey's and
# PostgreSQL's round trips, never the flood's.
FLOOD_BOUND = 0.5


async def test_a_flood_of_refused_credentials_from_one_address_holds_up_neither_readiness_nor_another_address(
    server, monkeypatch
):
    """One address sends a flood of addresses nobody has, through a Valkey that answers in 20 ms (another host's, say):
    past its limit the flood is refused on the event loop, or in a credentials' thread with no round trip once the limit
    has said so, so that it asks Valkey its limit's worth of slots (a few more while the threads were at it), and
    readiness and another address's lookup, asked once its limit's worth is answered, are answered within
    ``FLOOD_BOUND``. Before, each refusal held a thread for its round trips, and both waited behind the whole flood."""
    app = create_server_app(server)
    flood, flooding = 400, "203.0.113.9"
    slots: list[str] = []
    slot = server.bus.slot

    def far(key, limit, window):  # a Valkey 20 ms away
        slots.append(key)
        time.sleep(0.02)
        return slot(key, limit, window)

    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.bus, "slot", far)
        sent = [asyncio.create_task(_asgi(app, f"/u/{route}/x", client=flooding)) for route in _route_keys(flood)]
        try:
            await until(lambda: sum(task.done() for task in sent) > machines.LOOKUP_LIMIT, 20)
            started = time.monotonic()
            ready, _ = await asyncio.wait_for(_asgi(app, "/_latet/ready", client="10.0.3.7"), 10)
            readied = time.monotonic() - started
            started = time.monotonic()
            other, _ = await asyncio.wait_for(_asgi(app, f"/u/{_route_keys(1)[0]}/x", client="198.51.100.20"), 10)
            looked = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    assert (ready, other) == (200, 403)  # ready; and no such address, looked up
    assert readied < FLOOD_BOUND and looked < FLOOD_BOUND, (readied, looked)
    statuses = [status for status, _ in answers]
    assert statuses.count(403) == machines.LOOKUP_LIMIT and statuses.count(429) == flood - machines.LOOKUP_LIMIT
    flooded = [key for key in slots if key == machines.miss_key(flooding)]
    assert len(flooded) <= machines.LOOKUP_LIMIT + machines.CREDENTIAL_THREADS + 1, len(flooded)


async def test_a_flood_of_junk_sign_in_codes_from_one_address_holds_up_no_other_address(server, monkeypatch):
    """R1 (the re-review's open route of S1, its probe): junk codes from one address on the dashboard's sign-in, through
    a Valkey 20 ms away. Each takes a slot of its address's window first (``miss_key``), given back when a code opens
    a session; past the limit the codes are refused on the event loop, or in a credentials' thread with no round trip
    once the limit has said so: the flood asks Valkey its limit's worth of slots and redeems as many codes, a few more
    while the threads were at it, and another address's lookup is answered within ``FLOOD_BOUND``. Before, every code
    took a thread and a redeem, unlimited, and held another address's lookup for seconds."""
    app = create_server_app(server)
    flood, flooding = 400, "203.0.113.9"
    slots: list[str] = []
    slot, redeemed = server.bus.slot, server.sign_ins.redeemed

    def far(key, limit, window):  # a Valkey 20 ms away
        slots.append(key)
        time.sleep(0.02)
        return slot(key, limit, window)

    def far_redeemed(code):
        time.sleep(0.02)
        return redeemed(code)

    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.bus, "slot", far)
        monkeypatch.setattr(server.sign_ins, "redeemed", far_redeemed)
        codes = [secrets.token_urlsafe(24) for _ in range(flood)]
        heard: list[list[dict]] = [[] for _ in codes]  # what the app sent each, its redirect's address among it
        sent = [
            asyncio.create_task(
                _asgi(app, "/_latet/ui/login", query=f"code={code}".encode(), client=flooding, sent=heard[i])
            )
            for i, code in enumerate(codes)
        ]
        try:
            await until(lambda: sum(task.done() for task in sent) > machines.LOOKUP_LIMIT, 20)
            started = time.monotonic()
            other, _ = await asyncio.wait_for(_asgi(app, f"/u/{_route_keys(1)[0]}/x", client="198.51.100.20"), 10)
            looked = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    assert other == 403 and looked < FLOOD_BOUND, looked  # no such address, looked up within the bound
    # Every one back to the dashboard's page (a browser opened it): the codes looked up open nothing (expired), the
    # rest are refused for the address's limit (limited, E0c's Task 11: before, a JSON 429 the browser showed raw).
    assert [status for status, _ in answers] == [303] * flood
    said = [_location(one) for one in heard]
    assert said.count("/_latet/ui/?login=expired") == machines.LOOKUP_LIMIT
    assert said.count("/_latet/ui/?login=limited") == flood - machines.LOOKUP_LIMIT
    assert all(scope.get(REFUSED) for _, scope in answers)  # every answer the log's paced refusal
    flooded = [key for key in slots if key == machines.miss_key(flooding)]
    assert len(flooded) <= machines.LOOKUP_LIMIT + machines.CREDENTIAL_THREADS + 1, len(flooded)


async def _answering_at_once(scope, receive, send):
    """A person's space that answers every request at once: what the gate does is what a test of it weighs."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def _space_answering_at_once(identity):
    return _answering_at_once


def _dashboard_of(app, sid, client="10.0.3.9"):
    """A request of a dashboard's, with the session ``sid``: its status, and its scope."""
    return _asgi(app, f"{API}/me", headers=[(b"cookie", f"latet_session={sid}".encode())], client=client)


async def test_one_sessions_flood_of_dashboard_requests_holds_up_neither_another_person_nor_readiness(
    server, monkeypatch
):
    """A3 (the F-R3 check's older finding): a dashboard's session found open, and its person, are answered for from this
    instance's memory for a few seconds (``live.SESSION_HELD``), as a token's identity is: one session's flood of
    dashboard requests, through a Valkey 20 ms away, takes no credentials' thread past its first request, and another
    person's dashboard, looked up, and readiness answer within ``FLOOD_BOUND``. Before, every request of the flood was
    looked up in those threads, never shed (a session found valid passes its address's limit and the mark), and the
    other person's waited behind them all."""
    app = create_server_app(server)
    flood = 400
    looked: list[str] = []
    subject_of = server.sign_ins.subject_of

    def far(sid):  # a Valkey 20 ms away
        looked.append(sid)
        time.sleep(0.02)
        return subject_of(sid)

    emily, _ = server.identities.add_person("Emily Carter")  # before the instance listens: no message drops a read
    daniel, _ = server.identities.add_person("Daniel Okafor")
    hers, his = server.sign_ins.open(emily.id), server.sign_ins.open(daniel.id)
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.spaces, "app", _space_answering_at_once)
        monkeypatch.setattr(server.sign_ins, "subject_of", far)
        assert (await _dashboard_of(app, hers, "203.0.113.9"))[0] == 200  # looked up, held from now on
        sent = [asyncio.create_task(_dashboard_of(app, hers, "203.0.113.9")) for _ in range(flood)]
        try:
            await until(lambda: sum(task.done() for task in sent) > flood // 4, 20)
            started = time.monotonic()
            other, _ = await asyncio.wait_for(_dashboard_of(app, his, "198.51.100.20"), 10)
            looked_up = time.monotonic() - started
            started = time.monotonic()
            ready, _ = await asyncio.wait_for(_asgi(app, "/_latet/ready", client="10.0.3.7"), 10)
            readied = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    assert (other, ready) == (200, 200)
    assert looked_up < FLOOD_BOUND and readied < FLOOD_BOUND, (looked_up, readied)
    assert [status for status, _ in answers] == [200] * flood
    assert looked == [hers, his]  # her first request alone was looked up, and his


async def test_one_sessions_flood_past_its_few_seconds_in_memory_takes_one_lookup(server, monkeypatch):
    """A3: once the few seconds a session is held in memory are over, a flood of its requests under way is looked up
    once, the others waiting for that lookup's answer on the event loop: one session's flood takes one credentials'
    thread every few seconds, not one for each request it has in flight then (400 lookups through a Valkey 20 ms away
    are two seconds of the threads), and another person's dashboard answers within ``FLOOD_BOUND`` all the same."""
    monkeypatch.setattr(live_module, "SESSION_HELD", 0.3)
    app = create_server_app(server)
    flood = 400
    looked: list[str] = []
    subject_of = server.sign_ins.subject_of

    def far(sid):  # a Valkey 20 ms away
        looked.append(sid)
        time.sleep(0.02)
        return subject_of(sid)

    emily, _ = server.identities.add_person("Emily Carter")  # before the instance listens: no message drops a read
    daniel, _ = server.identities.add_person("Daniel Okafor")
    hers, his = server.sign_ins.open(emily.id), server.sign_ins.open(daniel.id)
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.spaces, "app", _space_answering_at_once)
        monkeypatch.setattr(server.sign_ins, "subject_of", far)
        assert (await _dashboard_of(app, hers, "203.0.113.9"))[0] == 200
        await asyncio.sleep(0.35)  # its few seconds in memory over
        looked.clear()
        sent = [asyncio.create_task(_dashboard_of(app, hers, "203.0.113.9")) for _ in range(flood)]
        try:
            await until(lambda: sum(task.done() for task in sent) > flood // 4, 20)
            started = time.monotonic()
            other, _ = await asyncio.wait_for(_dashboard_of(app, his, "198.51.100.20"), 10)
            looked_up = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    assert other == 200 and looked_up < FLOOD_BOUND, looked_up
    assert [status for status, _ in answers] == [200] * flood
    assert 1 <= looked.count(hers) <= 2 and looked.count(his) == 1, (looked.count(hers), looked.count(his))


async def test_a_sessions_lookup_runs_on_for_the_others_when_the_request_that_began_it_goes(server, monkeypatch):
    """A3: the request that began a session's lookup goes (its client gone) while the lookup is under way: the lookup
    runs on, and the request of the same session that waits for its answer is answered."""
    app = create_server_app(server)
    entered, release = threading.Event(), threading.Event()
    subject_of = server.sign_ins.subject_of

    def held_up(sid):  # a Valkey that takes its time
        entered.set()
        release.wait(5)
        return subject_of(sid)

    emily, _ = server.identities.add_person("Emily Carter")
    hers = server.sign_ins.open(emily.id)
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.spaces, "app", _space_answering_at_once)
        monkeypatch.setattr(server.sign_ins, "subject_of", held_up)
        first = asyncio.create_task(_dashboard_of(app, hers))
        try:
            await until(entered.is_set)
            second = asyncio.create_task(_dashboard_of(app, hers))
            await asyncio.sleep(0.05)  # waiting for the first one's lookup
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
        finally:
            release.set()
        status, _ = await asyncio.wait_for(second, 5)
    assert status == 200


async def test_a_request_after_a_sessions_end_waits_for_no_lookup_begun_before_it(server, monkeypatch):
    """A3: a session's lookup reads it open, and its end is heard before the lookup answers: the request under way gets
    what the lookup read, but one that comes after the end waits for no lookup begun before it, and is refused."""
    app = create_server_app(server)
    entered, release = threading.Event(), threading.Event()
    subject_of = server.sign_ins.subject_of

    def held_up(sid):  # read at once, answered late
        found = subject_of(sid)
        entered.set()
        release.wait(5)
        return found

    emily, _ = server.identities.add_person("Emily Carter")
    hers = server.sign_ins.open(emily.id)
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.spaces, "app", _space_answering_at_once)
        monkeypatch.setattr(server.sign_ins, "subject_of", held_up)
        first = asyncio.create_task(_dashboard_of(app, hers))
        try:
            await until(entered.is_set)
            server.sign_ins.end(sid=hers)  # signed out, the lookup under way
            after = asyncio.create_task(_dashboard_of(app, hers))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        statuses = [status for status, _ in await asyncio.wait_for(asyncio.gather(first, after), 5)]
    assert statuses == [200, 401]


def _far_stores(server, monkeypatch):
    """PostgreSQL and Valkey 20 ms away for a lookup of a token's or an address's identity: its read
    (``Identities._owner``), and the credential kept as found valid (``Known.keep``, a write of Valkey's). What each
    found, by its identity's name, and what each kept, by its name in Valkey: the two lists."""
    looked, kept = [], []
    owner, write = server.identities._owner, server.bus.set

    def far_owner(*args):
        found = owner(*args)
        looked.append(found.identity.name if found is not None else "")
        time.sleep(0.02)
        return found

    def far_write(name, *args):
        kept.append(name)
        time.sleep(0.02)
        return write(name, *args)

    monkeypatch.setattr(server.identities, "_owner", far_owner)
    monkeypatch.setattr(server.bus, "set", far_write)
    return looked, kept


def _known_name(known, credential):
    """The name in Valkey under which ``known`` keeps ``credential`` as found valid."""
    return known._name(known.hashed(credential))


async def test_one_tokens_flood_past_its_few_seconds_in_memory_takes_one_lookup(server, monkeypatch):
    """B2 (the check's "fix before main"): once the few seconds a token's identity is held in memory are over
    (``identities.CACHE_SECONDS``), a flood of its requests under way is looked up once, the others waiting for that
    lookup's answer on the event loop, as a session's are: one identity's flood takes one credentials' thread every few
    seconds, not one for each request it has under way then (the cap of requests in flight counts neither the machines'
    calls nor the bridge's, and comes after the lookup), and another person's lookup and readiness answer within
    ``FLOOD_BOUND``. No request rides into another's identity: the lookups are joined by each token's keyed hash."""
    monkeypatch.setattr(identities_module, "CACHE_SECONDS", 0.3)
    app = create_server_app(server)
    flood = 400
    hers = await _enrolled_here(server, "Emily Carter")  # before the instance listens: no message drops a read
    his = await _enrolled_here(server, "Daniel Okafor")
    mine, theirs = [(b"x-latet-token", hers.encode())], [(b"x-latet-token", his.encode())]
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        looked, kept = _far_stores(server, monkeypatch)
        assert (await _asgi(app, f"{SERVER}/whoami", headers=mine, client="203.0.113.9"))[0] == 200
        await asyncio.sleep(0.35)  # its few seconds in memory over
        looked.clear()
        kept.clear()
        sent = [
            asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=mine, client="203.0.113.9"))
            for _ in range(flood)
        ]
        answered: list[dict] = []
        try:
            await until(lambda: sum(task.done() for task in sent) > flood // 4, 20)
            started = time.monotonic()
            other, _ = await asyncio.wait_for(
                _asgi(app, f"{SERVER}/whoami", headers=theirs, client="198.51.100.20", sent=answered), 10
            )
            looked_up = time.monotonic() - started
            started = time.monotonic()
            ready, _ = await asyncio.wait_for(_asgi(app, "/_latet/ready", client="10.0.3.7"), 10)
            readied = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    body = b"".join(m.get("body", b"") for m in answered if m["type"] == "http.response.body")
    assert (other, ready) == (200, 200) and json.loads(body)["name"] == "Daniel Okafor"
    assert looked_up < FLOOD_BOUND and readied < FLOOD_BOUND, (looked_up, readied)
    assert [status for status, _ in answers] == [200] * flood
    keeps = kept.count(_known_name(server.known_secrets, hers))
    assert 1 <= keeps <= 2 and looked.count("Emily Carter") <= 2 and looked.count("Daniel Okafor") == 1, keeps
    assert len(server.lookups) == 0  # nothing kept once the lookups ended


async def test_a_request_after_a_tokens_revocation_waits_for_no_lookup_begun_before_it(server, monkeypatch):
    """B2: a token's lookup reads its identity, and the token is revoked before the lookup answers: the request under
    way gets what the lookup read, but one that comes after the revocation, heard here (``Identities.generation``),
    waits for no lookup begun before it, and is refused."""
    app = create_server_app(server)
    entered, release = threading.Event(), threading.Event()
    owner = server.identities._owner

    def held_up(*args):  # read at once, answered late
        found = owner(*args)
        entered.set()
        release.wait(5)
        return found

    token = await _enrolled_here(server, "Emily Carter")
    mine = [(b"x-latet-token", token.encode())]
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.identities, "_owner", held_up)
        first = asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=mine))
        try:
            await until(entered.is_set)
            await asyncio.to_thread(server.identities.revoke_secret, token)  # revoked, the lookup under way
            after = asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=mine))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        statuses = [status for status, _ in await asyncio.wait_for(asyncio.gather(first, after), 5)]
    assert statuses == [200, 401]


async def test_a_tokens_lookup_is_joined_by_requests_of_the_same_token_alone(server, monkeypatch):
    """B2: the lookups are joined by each credential's keyed hash: another person's token, asked while a lookup of a
    first one is under way, is looked up on its own and answered with its own identity, never the other's."""
    app = create_server_app(server)
    entered, release = threading.Event(), threading.Event()
    owner = server.identities._owner

    def held_up(*args):  # hers read at once and answered late, his answered at once
        found = owner(*args)
        if found is not None and found.identity.name == "Emily Carter":
            entered.set()
            release.wait(5)
        return found

    hers = await _enrolled_here(server, "Emily Carter")
    his = await _enrolled_here(server, "Daniel Okafor")
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.identities, "_owner", held_up)
        first = asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=[(b"x-latet-token", hers.encode())]))
        answered: list[dict] = []
        try:
            await until(entered.is_set)
            other, _ = await asyncio.wait_for(
                _asgi(app, f"{SERVER}/whoami", headers=[(b"x-latet-token", his.encode())], sent=answered), 2
            )
        finally:
            release.set()
        status, _ = await asyncio.wait_for(first, 5)
    body = b"".join(m.get("body", b"") for m in answered if m["type"] == "http.response.body")
    assert (status, other) == (200, 200) and json.loads(body)["name"] == "Daniel Okafor"


async def test_one_addresss_flood_past_its_few_seconds_in_memory_takes_one_lookup(server, monkeypatch):
    """B2: an identity's address alike: once the few seconds it is held in memory are over, a flood of requests to it
    under way is looked up once (the cap of requests in flight, raised here so that it refuses none of them, counts a
    request only once its address is looked up), and another person's address and readiness answer within
    ``FLOOD_BOUND``."""
    monkeypatch.setattr(identities_module, "CACHE_SECONDS", 0.3)
    server.settings = server.settings.model_copy(update={"requests_in_flight": 1_000})
    app = create_server_app(server)
    flood = 400
    hers = await _enrolled_here(server, "Emily Carter")  # before the instance listens: no message drops a read
    his = await _enrolled_here(server, "Daniel Okafor")

    def proxied(token, client):
        return _asgi(app, f"/u/{route_key(token)}/anthropic/v1/messages", method="POST", body=b"{}", client=client)

    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.spaces, "app", _space_answering_at_once)
        looked, kept = _far_stores(server, monkeypatch)
        assert (await proxied(hers, "203.0.113.9"))[0] == 200
        await asyncio.sleep(0.35)  # its few seconds in memory over
        looked.clear()
        kept.clear()
        sent = [asyncio.create_task(proxied(hers, "203.0.113.9")) for _ in range(flood)]
        try:
            await until(lambda: sum(task.done() for task in sent) > flood // 4, 20)
            started = time.monotonic()
            other, _ = await asyncio.wait_for(proxied(his, "198.51.100.20"), 10)
            looked_up = time.monotonic() - started
            started = time.monotonic()
            ready, _ = await asyncio.wait_for(_asgi(app, "/_latet/ready", client="10.0.3.7"), 10)
            readied = time.monotonic() - started
        finally:
            answers = await asyncio.gather(*sent)
    assert (other, ready) == (200, 200)
    assert looked_up < FLOOD_BOUND and readied < FLOOD_BOUND, (looked_up, readied)
    assert [status for status, _ in answers] == [200] * flood
    keeps = kept.count(_known_name(server.known_routes, route_key(hers)))
    assert 1 <= keeps <= 2 and looked.count("Emily Carter") <= 2 and looked.count("Daniel Okafor") == 1, keeps


async def test_a_session_ended_on_another_instance_is_refused_here_at_once(instances, monkeypatch):
    """A3: what an instance's memory holds of a dashboard's session (``live.SESSION_HELD``) is dropped the moment the
    session ends on any instance, told on the bus as a token's revocation is: a sign-out on one instance, and the end of
    a person's every session there (a token of theirs revoked, say), are refused on the other at once, not seconds
    later when its memory would let them go."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    signed_out, revoked = a.sign_ins.open(emily.id), a.sign_ins.open(emily.id)
    app = create_server_app(b)
    monkeypatch.setattr(b.spaces, "app", _space_answering_at_once)
    async with listening(a, b):
        assert [(await _dashboard_of(app, sid))[0] for sid in (signed_out, revoked)] == [200, 200]
        assert b.sign_ins.held(signed_out) == emily.id == b.sign_ins.held(revoked)  # held in b's memory from now on
        started = time.monotonic()
        a.sign_ins.end(sid=signed_out)  # signed out on a
        await until(lambda: b.sign_ins.held(signed_out) is None, 1)
        assert [(await _dashboard_of(app, sid))[0] for sid in (signed_out, revoked)] == [401, 200]
        a.sign_ins.end(subject=emily.id)  # her every session ended on a
        await until(lambda: b.sign_ins.held(revoked) is None, 1)
        assert (await _dashboard_of(app, revoked))[0] == 401
        assert time.monotonic() - started < live_module.SESSION_HELD
        later = a.sign_ins.open(emily.id)
        assert (await _dashboard_of(app, later))[0] == 200
        a.identities.disable(emily.id)  # disabled on a: her person dropped from b's memory, her session with it
        await until(lambda: b.identities.held(emily.id) is None and b.sign_ins.held(later) is None, 1)
        assert (await _dashboard_of(app, later))[0] == 401


async def test_past_a_number_of_waiting_calls_a_lookup_is_refused_at_once_and_readiness_waits_for_none(
    server, monkeypatch
):
    """With every credentials' thread held by a lookup that waits on a store, and ``CREDENTIALS_WAITING`` calls waiting
    in all, the next is refused at once (503, ``Retry-After``), nothing of it looked up; readiness asks its stores in
    threads of its own and answers meanwhile."""
    monkeypatch.setattr(machines, "CREDENTIALS_WAITING", machines.CREDENTIAL_THREADS + 2)
    app = create_server_app(server)
    release, entered = threading.Event(), []
    real = server.identities._owner

    def slow(*args):
        entered.append(args)
        release.wait(10)
        return real(*args)

    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        monkeypatch.setattr(server.identities, "_owner", slow)
        tokens = [(b"x-latet-token", f"latet_pat_{n:043d}".encode()) for n in range(machines.CREDENTIALS_WAITING)]
        calls = [
            asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=[token], client=f"10.0.4.{n}"))
            for n, token in enumerate(tokens)
        ]
        try:
            await until(lambda: len(entered) == machines.CREDENTIAL_THREADS and machines._WAITING.count == len(tokens))
            one_more, sent = [(b"x-latet-token", ("latet_pat_" + "9" * 43).encode())], []
            asked = _asgi(app, f"{SERVER}/whoami", headers=one_more, client="10.0.5.1", sent=sent)
            status, _ = await asyncio.wait_for(asked, 2)  # at once, while the threads are held
            [start] = [message for message in sent if message["type"] == "http.response.start"]
            assert status == 503 and dict(start["headers"])[b"retry-after"] == machines.QUEUED_RETRY_AFTER.encode()
            assert len(entered) == machines.CREDENTIAL_THREADS  # nothing of it looked up
            ready, _ = await asyncio.wait_for(_asgi(app, "/_latet/ready", client="10.0.3.7"), 2)
            assert ready == 200
        finally:
            release.set()
            await asyncio.gather(*calls)
    assert machines._WAITING.count == 0


def test_a_use_is_noted_in_a_thread_of_its_own_and_a_held_row_holds_up_no_lookup(server, client):
    """A token's, an address's and an administration token's use is written in a thread of its own (``Notes``): with
    their rows held by a long transaction (a removal's cascade over a large space, say), their lookups are answered at
    once, and the uses are written once the rows are free."""
    person, token = _enrolled(server, client)
    administrator = server.admin.issue("first administrator")
    server.identities.forget()  # nothing held: each lookup reads the database
    server.identities.notes, server.admin.notes = Notes(server.db, time.time), Notes(server.db, time.time)
    held, release = threading.Event(), threading.Event()

    def hold():
        with server.db.transaction() as conn:
            conn.execute("SELECT 1 FROM tokens WHERE identity_id = %s FOR UPDATE", (person.id,))
            conn.execute("SELECT 1 FROM identities WHERE id = %s FOR UPDATE", (person.id,))
            conn.execute("SELECT 1 FROM admin_tokens FOR UPDATE")
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert server.identities.by_secret(token).id == person.id
        assert server.identities.by_route(route_key(token)).id == person.id
        assert server.identities.seen(person.id) is not None  # due: handed to the notes' thread
        assert server.admin.check(administrator) is not None
        assert time.monotonic() - started < 1.0  # the rows are held ten seconds
    finally:
        release.set()
        holder.join()
    identities_module._NOTING.submit(lambda: None).result(10)  # one thread: what it was given before is written
    tokens = server.identities.tokens(person.id)
    [admin_token] = server.admin.list()
    assert tokens[0].last_used is not None and admin_token.last_used is not None
    assert server.identities.current(person.id).last_seen is not None


def test_a_persons_held_lock_holds_up_no_other_persons_lookup(server, client):
    """Important 2: a person's runtime lock held (a resync, a settings save) and their computer's PUT identity calls
    waiting on it: another person's lookup, their dashboard and readiness are answered all the same, and a person has
    one such call at a time."""
    person, emily = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")
    _signed_in(client, other)
    mine = _mine(emily)
    runtime = client.portal.call(server.spaces.runtime, person)
    applied, update_config = [], runtime.update_config

    def spied(change):
        applied.append(threading.current_thread().name)
        return update_config(change)

    runtime.update_config = spied
    held, release = threading.Event(), threading.Event()

    def hold():
        with runtime._lock:  # as a resync or a settings save holds it
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        with concurrent.futures.ThreadPoolExecutor(machines.CREDENTIAL_THREADS) as pool:
            puts = [
                pool.submit(client.put, f"{SERVER}/identity", json={"usernames": [f"emily{n}"]}, headers=mine)
                for n in range(machines.CREDENTIAL_THREADS)
            ]
            time.sleep(0.5)
            server.identities.forget()  # nothing held: Daniel's lookup needs a thread
            started = time.monotonic()
            assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": other}).status_code == 200
            assert client.get(f"{API}/me").status_code == 200
            assert client.get("/_latet/ready").status_code == 200
            assert time.monotonic() - started < 2.0  # the lock is held three seconds
            time.sleep(2.5)
            release.set()
            answers = [future.result(20) for future in puts]
    finally:
        release.set()
        holder.join()
    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [200] + [429] * (machines.CREDENTIAL_THREADS - 1)  # one at a time for a person
    assert all(answer.headers.get("retry-after") for answer in answers if answer.status_code == 429)
    assert len(applied) == 1 and applied[0].startswith("latet-work")  # in the threads of the work that may wait


def test_the_logs_writes_held_up_hold_no_lookup(server, client, monkeypatch):
    """Important 2: the administration log's writes (its chain is written one at a time for the server) wait in
    threads of their own: with every one of them held, a computer's lookup is answered all the same."""
    monkeypatch.setattr(machines, "ENROL_LIMIT", 100)
    _, other = _enrolled(server, client, "Daniel Okafor")
    release, entered = threading.Event(), []
    record = server.audit.record

    def slow(*args, **kwargs):
        entered.append(threading.current_thread().name)
        release.wait(10)
        return record(*args, **kwargs)

    monkeypatch.setattr(server.audit, "record", slow)
    codes = [server.identities.add_person(f"Person {n}")[1] for n in range(machines.STORE_THREADS + 1)]
    with concurrent.futures.ThreadPoolExecutor(len(codes)) as pool:
        enrolled = [pool.submit(client.post, f"{SERVER}/enrol", json={"code": c, "label": "laptop"}) for c in codes]
        try:
            _until(lambda: len(entered) == machines.STORE_THREADS)
            server.identities.forget()  # nothing held: the lookup needs a thread
            started = time.monotonic()
            assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": other}).status_code == 200
            assert time.monotonic() - started < 2.0
        finally:
            release.set()
        assert [future.result(20).status_code for future in enrolled] == [200] * len(codes)
    assert all(name.startswith("latet-store") for name in entered)


def test_an_enrolment_and_its_entry_wait_behind_no_persons_settings(server, client):
    """A-19: what waits on the chain's lock or a row has threads of its own: with every thread of the people's work
    held (eight people's settings applied under their locks, say), a computer still enrols, its entry written."""
    person, code = server.identities.add_person("Emily Carter")
    release = threading.Event()
    held = [machines._WORK.submit(release.wait, 10) for _ in range(machines.WORK_THREADS)]
    try:
        started = time.monotonic()
        assert client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"}).status_code == 200
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        for future in held:
            future.result(10)
    assert [e.action for e in server.audit.query(actor=person.id)] == ["token.added"]


def test_a_token_found_valid_before_a_restart_meets_no_limit_at_its_first_try(server, client, monkeypatch):
    """A-18: what an instance found valid is kept in Valkey for an hour, as keyed hashes: a restarted or new instance
    knows it from the first try, though strangers at its address filled their window."""
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)
    _, token = _enrolled(server, client)  # found valid from its enrolment
    for n in range(3):  # strangers behind the same address fill its window
        client.get(f"{SERVER}/whoami", headers={"x-latet-token": f"latet_pat_{n:043d}"})
    restarted = machines.Known(server.bus, server.master.derive(b"known"), kind="secret")  # its memory empty
    server.known_secrets, server.limits = (
        restarted,
        machines.Limits(),
    )  # nor does it remember the address past its limit
    server.identities.forget()
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200  # looked up all the same
    server.known_secrets = machines.Known()  # an instance that knows nothing of it meets the limit
    server.identities.forget()
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 429


def test_a_colleagues_token_known_only_elsewhere_passes_while_its_address_is_past_its_limit(
    server, client, monkeypatch
):
    """R2 (the re-review's Minor on S1's trade-off): once this instance remembers an address past its limit, what it
    does not hold is refused on the event loop, but a token found valid on any instance in the last hour (Valkey's known
    set: one this instance never saw, after a restart or on another instance) is read there and passes, looked up with
    no slot taken; a stranger's is read and refused, nothing looked up. One this instance found valid passes, as ever."""
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)
    _, token = _enrolled(server, client)
    _, other = _enrolled(server, client, "Daniel Okafor")
    for n in range(3):  # strangers behind the same address fill its window: this instance remembers it so
        client.get(f"{SERVER}/whoami", headers={"x-latet-token": f"latet_pat_{n:043d}"})
    elsewhere = machines.Known(server.bus, server.master.derive(b"known"), kind="secret")  # Valkey's memory alone
    elsewhere.keep(other)
    server.known_secrets = machines.Known(server.bus, server.master.derive(b"known"), kind="secret")
    server.known_secrets.keep(token)  # found valid here
    server.identities.forget()
    lookups, slots = _counting(server, monkeypatch)
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": other}).status_code == 200
    assert len(lookups) == 1 and slots == []  # read in Valkey, looked up, no slot taken
    stranger = {"x-latet-token": "latet_pat_" + "9" * 43}
    assert client.get(f"{SERVER}/whoami", headers=stranger).status_code == 429
    assert len(lookups) == 1 and slots == []  # read in Valkey, refused, nothing looked up
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200


def test_one_address_spends_one_budget_of_misses_across_every_kind(server, monkeypatch):
    """A2 (the follow-up's Important 2): an address has one budget of misses, every kind together, where it had one for
    each of five kinds (about 305 a minute to spend at once): an unknown address, token and session spend it, and its
    administration token, its sign-in link and its next unknown address are then refused at once (429)."""
    for module in (machines, app_module, admin_module):
        monkeypatch.setattr(module, "LOOKUP_LIMIT", 3)
    with TestClient(create_server_app(server), base_url=URL, client=("203.0.113.9", 50000)) as client:
        spent = [
            client.post(f"/u/{'b' * 26}/anthropic/v1/messages", json={}).status_code,  # an address no one has
            client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "0" * 43}).status_code,
            client.get(f"{API}/me", cookies={"latet_session": "s" * 43}).status_code,  # a session no one has
        ]
        admin = client.get(f"{SERVER}/admin/status", headers={"x-latet-admin": "latet_adm_" + "0" * 43}).status_code
        link = client.get("/_latet/ui/login?code=nope", follow_redirects=False)
        address = client.post(f"/u/{'c' * 26}/anthropic/v1/messages", json={}).status_code
        refused = [admin, link.status_code, address]
    # The sign-in link back to the dashboard's page, which says the address tried too many (E0c's Task 11).
    assert spent == [403, 401, 401] and refused == [429, 303, 429]
    assert link.headers["location"] == "/_latet/ui/?login=limited" and "nope" not in link.headers["location"]


async def test_three_hundred_addresses_at_a_pace_the_threads_absorb_are_each_looked_up(server):
    """F-R3 (the residuals' check, Important 1): three hundred addresses, a hundred at a time, four addresses no one has
    each, 1,200 misses in all: every one is looked up (403) and none refused for the instance's sake, where a ceiling
    of 1,000 misses a minute, every address together, refused the rest of the minute after the thousandth."""
    app = create_server_app(server)
    statuses: list[int] = []
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        for _ in range(4):  # four addresses no one has from each
            for wave in range(3):  # a hundred addresses at a time
                calls = (
                    _asgi(app, f"/u/{route}/x", client=f"203.0.{wave}.{n}") for n, route in enumerate(_route_keys(100))
                )
                statuses += [status for status, _ in await asyncio.gather(*calls)]
    assert statuses == [403] * 1_200, sorted(set(statuses))  # neither a 429 of a ceiling nor a 503 of a shed


async def _enrolled_here(server, name):
    """The token of a computer of a new person's, enrolled without a call (its slot and its entry aside)."""
    _, code = server.identities.add_person(name)
    with server.identities.changing() as changes:
        found = server.identities.enrol(code, "laptop", within=changes)
    assert found is not None
    KEYS[found[1]] = found[2]
    return found[1]


async def test_a_flood_that_outpaces_the_credentials_threads_sheds_what_no_instance_knows(server, monkeypatch):
    """F-R3: once ``CREDENTIALS_BUSY`` calls wait for the credentials' threads (only a flood that outpaces them gets
    there), what this instance does not hold, and no instance found valid in the last hour, is refused at once on the
    event loop, 503 with ``Retry-After``, nothing queued and no slot taken: a token new to every instance, an enrolment;
    a sign-in link alike, sent back to the dashboard's page, which says the server is busy (a browser opened it). A
    known credential passes: one whose identity the instance holds at once, one it found valid and one found valid
    elsewhere once the threads reach them."""
    monkeypatch.setattr(machines, "CREDENTIALS_BUSY", machines.CREDENTIAL_THREADS + 2)
    app = create_server_app(server)
    release, real = threading.Event(), server.identities._owner

    def slow(*args):  # a store that the flood keeps busy
        release.wait(10)
        return real(*args)

    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        cached, here, elsewhere = [await _enrolled_here(server, name) for name in ("Emily", "Daniel", "Priya")]
        server.identities.forget()
        assert (await _asgi(app, f"{SERVER}/whoami", headers=[(b"x-latet-token", cached.encode())]))[0] == 200
        server.known_secrets.keep(here)  # found valid here
        machines.Known(server.bus, server.master.derive(b"known"), kind="secret").keep(elsewhere)  # Valkey's alone
        monkeypatch.setattr(server.identities, "_owner", slow)
        junk = [[(b"x-latet-token", f"latet_pat_{n:043d}".encode())] for n in range(machines.CREDENTIALS_BUSY)]
        clients = [f"10.0.6.{n}" for n in range(len(junk))]
        expected = Counter(
            (machines.miss_key(client), machines.LOOKUP_LIMIT, machines.LOOKUP_WINDOW) for client in clients
        )
        slots, slot = [], server.bus.slot
        admissions, enter = [], machines._WAITING.enter

        def watched_slot(*args):
            slots.append(args)
            return slot(*args)

        def entering():
            admissions.append(None)  # attempted admission, even if the real guard rejects it
            return enter()

        monkeypatch.setattr(server.bus, "slot", watched_slot)
        monkeypatch.setattr(machines._WAITING, "enter", entering)
        flood = [
            asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=tokens, client=client))
            for client, tokens in zip(clients, junk, strict=True)
        ]
        waiting: list[asyncio.Task] = []
        try:
            await until(lambda: machines._WAITING.count >= machines.CREDENTIALS_BUSY)
            admitted = len(admissions)
            assert admitted == len(junk)

            async def refused(asked):
                try:
                    return await asyncio.wait_for(asked, 2)
                finally:
                    assert len(admissions) == admitted, "a busy refusal attempted credential admission"

            sent: list[dict] = []
            new = [(b"x-latet-token", ("latet_pat_" + "8" * 43).encode())]
            asked = _asgi(app, f"{SERVER}/whoami", headers=new, client="198.51.100.30", sent=sent)
            status, token_scope = await refused(asked)
            [start] = [message for message in sent if message["type"] == "http.response.start"]
            assert status == 503 and dict(start["headers"])[b"retry-after"] == b"1"
            assert token_scope.get(REFUSED)
            heard: list[dict] = []
            asked = _asgi(app, "/_latet/ui/login", query=b"code=nope", client="198.51.100.31", sent=heard)
            link, link_scope = await refused(asked)
            body, as_json = b'{"code": "nope", "label": "x"}', [(b"content-type", b"application/json")]
            enrolled: list[dict] = []
            enrolment, enrol_scope = await refused(
                _asgi(
                    app,
                    f"{SERVER}/enrol",
                    method="POST",
                    body=body,
                    headers=as_json,
                    client="198.51.100.32",
                    sent=enrolled,
                )
            )
            [enrol_start] = [message for message in enrolled if message["type"] == "http.response.start"]
            assert (link, enrolment) == (303, 503)
            assert dict(enrol_start["headers"])[b"retry-after"] == b"1" and enrol_scope.get(REFUSED)
            assert Counter(tuple(slots)) <= expected  # only the six admitted flood clients, each at most once
            assert _location(heard) == "/_latet/ui/?login=busy"  # the dashboard's page says it (E0c's Task 11)
            assert link_scope.get(REFUSED)  # the server's own refusal, which the log paces as the others
            mine = [(b"x-latet-token", cached.encode())]
            assert (await asyncio.wait_for(_asgi(app, f"{SERVER}/whoami", headers=mine), 2))[0] == 200  # held
            waiting = [
                asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=[(b"x-latet-token", token.encode())]))
                for token in (here, elsewhere)
            ]
            await asyncio.sleep(0.2)
            assert not any(task.done() for task in waiting)  # queued behind the flood: not refused
        finally:
            release.set()
            finished = await asyncio.wait_for(asyncio.gather(*flood, *waiting, return_exceptions=True), 10)
        assert not any(isinstance(result, BaseException) for result in finished)
        statuses = [task.result()[0] for task in waiting]
        assert Counter(tuple(slots)) == expected  # late calls and all three known-token positives included
    assert statuses == [200, 200] and machines._WAITING.count == 0


async def test_a_flood_at_an_address_costs_a_few_reads_and_shuts_out_none_of_its_colleagues(server, monkeypatch):
    """R2: a stranger floods the address he shares with a colleague, past its limit: every token of the flood and the
    colleague's, which only another instance found valid, are read in Valkey's known set together, a few reads a
    second for the instance (``KnownReads``), each for every token waiting; the colleague's passes, the flood is
    refused, nothing of it looked up."""
    app = create_server_app(server)
    async with app.router.lifespan_context(app):
        await until(server.listening.is_set)
        _, code = server.identities.add_person("Emily Carter")
        with server.identities.changing() as changes:
            found = server.identities.enrol(code, "laptop", within=changes)
        assert found is not None
        token = found[1]
        server.known_secrets.keep(token)  # found valid: Valkey's known set holds it
        server.known_secrets = machines.Known(server.bus, server.master.derive(b"known"), kind="secret")  # restarted
        server.identities.forget()
        flooding = "203.0.113.9"
        server.limits.past(machines.miss_key(flooding), 60)  # the stranger filled the address's window
        lookups, _ = _counting(server, monkeypatch)
        reads, read = [], server.known_secrets.held_elsewhere
        monkeypatch.setattr(
            server.known_secrets, "held_elsewhere", lambda hashes: reads.append(len(hashes)) or read(hashes)
        )
        junk = [[(b"x-latet-token", f"latet_pat_{n:043d}".encode())] for n in range(300)]
        flood = [
            asyncio.create_task(_asgi(app, f"{SERVER}/whoami", headers=tokens, client=flooding)) for tokens in junk
        ]
        mine = [(b"x-latet-token", token.encode())]
        theirs, _ = await asyncio.wait_for(_asgi(app, f"{SERVER}/whoami", headers=mine, client=flooding), 10)
        answers = await asyncio.wait_for(asyncio.gather(*flood), 10)
    assert theirs == 200 and [status for status, _ in answers] == [429] * len(junk)
    assert len(reads) <= 3 and sum(reads) == len(junk) + 1 and len(lookups) == 1


def test_a_refusal_of_a_persons_settings_is_answered_as_one_not_as_a_store_that_failed(server, client, monkeypatch):
    person, token = _enrolled(server, client)
    mine = _mine(token)
    assert client.put(f"{SERVER}/identity", json={"usernames": ["emily"]}, headers=mine).status_code == 200
    runtime = server.spaces._spaces[person.id].runtime

    def refused(change, **kwargs):
        raise SharedItemsError({"terms": 2})

    monkeypatch.setattr(runtime, "update_config", refused)
    answer = client.put(f"{SERVER}/identity", json={"usernames": ["emily.carter"]}, headers=mine)
    assert answer.status_code == 409 and "2 terms set for everyone" in answer.json()["error"]


async def test_a_no_store_answer_says_it_once_whatever_its_app_said():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"cache-control", b"max-age=600")]})
        await send({"type": "http.response.body", "body": b"{}"})

    for path, said in ((f"{SERVER}/whoami", b"no-store"), ("/_latet/ui/login", b"no-store"), ("/_latet/ui/", b"max-age=600")):  # fmt: skip
        sent = []

        async def send(message, sent=sent):
            sent.append(message)

        await app_module.AnswerHeaders(app)({"type": "http", "path": path, "headers": []}, None, send)
        headers = sent[0]["headers"]
        assert [value for name, value in headers if name == b"cache-control"] == [said], path
        assert (b"x-content-type-options", b"nosniff") in headers


# --- what is said, what is kept, and whose a space is (fix round 1: the security Minors, Task 12's hand-off) ---------


def test_a_sign_in_link_opened_by_another_site_signs_no_one_in(server, client):
    """Login CSRF: a person's link opened in a colleague's browser by a page, a mail or a chat signs nobody in, and the
    code is left for its owner."""
    _, token = _enrolled(server, client)
    link = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"].removeprefix(URL)
    for site in ("cross-site", "same-site"):
        page = {"sec-fetch-site": site, "sec-fetch-mode": "navigate"}
        refused = client.get(link, headers=page, follow_redirects=False)
        assert refused.status_code == 303 and refused.headers["location"].endswith("login=expired"), site
        assert "set-cookie" not in refused.headers and not client.cookies.get("latet_session")
    typed = client.get(link, headers={"sec-fetch-site": "none"}, follow_redirects=False)  # the code was not spent
    assert typed.status_code == 303 and "latet_session=" in typed.headers["set-cookie"]
    client.cookies.clear()
    again = client.post(f"{SERVER}/login-code", headers=_mine(token)).json()["url"].removeprefix(URL)
    assert "latet_session=" in client.get(again, follow_redirects=False).headers["set-cookie"]  # no Fetch Metadata


def test_what_a_proxy_adds_to_a_request_never_reaches_a_provider(server, monkeypatch):
    sent = _provider(monkeypatch)
    handshakes = []

    async def relay(websocket, *args, **kwargs):
        handshakes.append({name.lower() for name in websocket.headers})
        await websocket.close()

    monkeypatch.setattr("latet.gateway.ws.relay", relay)
    added = {
        "x-forwarded-for": "10.20.30.40",
        "x-forwarded-host": "latet.acme.lan",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
        "forwarded": "for=10.20.30.40;host=latet.acme.lan;proto=https",
        "via": "1.1 caddy",
        "x-real-ip": "10.20.30.40",
    }
    with TestClient(create_server_app(server), base_url=URL) as client:
        _, token = _enrolled(server, client)
        address = f"/u/{route_key(token)}"
        assert client.post(f"{address}/anthropic/v1/messages", json=SMALL, headers={**added, "x-api-key": "sk-test"}).status_code == 200  # fmt: skip
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"{address}/openai/v1/responses", headers={**added, "host": "latet.acme.lan"}),
        ):
            pass
    assert sent and not set(added) & {name.lower() for name in sent[0].headers}
    assert handshakes and not set(added) & handshakes[0]


def test_the_machines_answers_and_the_sign_in_are_never_kept_by_a_cache(server, client):
    person, code = server.identities.add_person("Emily Carter")
    enrolled = client.post(f"{SERVER}/enrol", json={"code": code, "label": "laptop"})
    mine = {"x-latet-token": enrolled.json()["token"], "x-latet-computer-key": enrolled.json()["computer_key"]}
    link = client.post(f"{SERVER}/login-code", headers=mine)
    answers = {
        "enrol": enrolled,
        "login-code": link,
        "whoami": client.get(f"{SERVER}/whoami", headers=mine),
        "a refusal": client.get(f"{SERVER}/whoami"),
        "a page": client.get(f"{SERVER}/whoami", headers={**mine, "origin": URL}),
        "the sign-in": client.get(link.json()["url"].removeprefix(URL), follow_redirects=False),
    }
    for what, answer in answers.items():
        assert answer.headers.get("cache-control") == "no-store", what
    assert enrolled.json()["identity"]["id"] == person.id


def test_a_computer_that_forgets_its_token_closes_its_persons_space_here_at_once(server, client, monkeypatch):
    """Task 12's hand-off: whatever becomes of the revocation's message, the instance that revoked the token closes the
    person's space at once (what the token opened ends with it)."""
    person, token = _enrolled(server, client)
    mine = _mine(token)
    assert client.get(f"/u/{route_key(token)}{ENGINE}/ping", headers=mine).status_code == 200
    assert person.id in server.spaces._spaces
    publish = server.bus.publish

    def lost(channel, message):
        if channel == "identities":
            raise redis.TimeoutError("Timeout reading from socket")
        publish(channel, message)

    monkeypatch.setattr(server.bus, "publish", lost)
    assert client.delete(f"{SERVER}/token", headers=mine).json() == {"ok": True}
    assert person.id not in server.spaces._spaces


# --- the requests one identity has in flight on an instance (Task 11's fix-round concern 1, routed here) -------------

SMALL = {"model": "claude", "max_tokens": 8, "messages": [{"role": "user", "content": "hello"}]}


def _held_provider(monkeypatch):
    """The providers every space of the test reaches: a stand-in that holds each request until ``release`` is set (or
    fails it while ``modes`` has ``fail``), keeping what arrived."""
    release, arrived, modes = threading.Event(), [], set()

    async def answer(request: httpx.Request) -> httpx.Response:
        arrived.append(request)
        if "fail" in modes:
            raise httpx.ConnectError("the provider is unreachable")
        await asyncio.to_thread(release.wait, 10)  # the test's thread sets it
        return _answered()

    monkeypatch.setattr(
        "latet.gateway.app.make_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return release, arrived, modes


def _until(check, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "not within the time"
        time.sleep(0.01)


def test_the_requests_one_identity_has_in_flight_are_capped_and_another_identitys_go_on(server, monkeypatch, caplog):
    release, arrived, _ = _held_provider(monkeypatch)
    server.settings = server.settings.model_copy(update={"requests_in_flight": 2})
    with TestClient(create_server_app(server), base_url=URL) as client:
        emily, mine = _enrolled(server, client)
        _, theirs = _enrolled(server, client, "Daniel Okafor")
        _signed_in(client, mine)

        def ask(token):
            address = f"/u/{route_key(token)}/anthropic/v1/messages"
            return client.post(address, json=SMALL, headers={"x-api-key": "sk-test"})

        with concurrent.futures.ThreadPoolExecutor(3) as pool, caplog.at_level(logging.WARNING, logger="latet"):
            held = [pool.submit(ask, mine) for _ in range(2)]
            _until(lambda: len(arrived) == 2)
            crowded = [ask(mine), ask(mine)]
            assert [answer.status_code for answer in crowded] == [429, 429]
            assert {answer.headers["retry-after"] for answer in crowded} == {str(app_module.CROWDED_RETRY_AFTER)}
            assert "requests in flight" in crowded[0].json()["error"] and len(arrived) == 2  # nothing more reached it
            other = pool.submit(ask, theirs)  # another identity: its own cap
            _until(lambda: len(arrived) == 3)
            # The plugin's bridge and the dashboard are no requests to a destination: never held up by them.
            assert client.get(f"/u/{route_key(mine)}{ENGINE}/ping", headers={"x-latet-token": mine}).status_code == 200
            assert client.get(f"{API}/me").status_code == 200
            release.set()
            assert [f.result(10).status_code for f in held] == [200, 200] and other.result(10).status_code == 200
        assert ask(mine).status_code == 200  # its places given back
    said = [r.getMessage() for r in caplog.records if emily.id in r.getMessage() and "in flight" in r.getMessage()]
    assert len(said) == 1  # once a minute for each identity


async def test_a_request_gives_its_place_back_however_it_ends(server, monkeypatch):
    release, arrived, modes = _held_provider(monkeypatch)
    server.settings = server.settings.model_copy(update={"requests_in_flight": 1})
    app = create_server_app(server)
    async with app.router.lifespan_context(app):
        _, code = await asyncio.to_thread(server.identities.add_person, "Emily Carter")
        _, token, _ = await asyncio.to_thread(server.identities.enrol, code, "laptop")
        path, body = f"/u/{route_key(token)}/anthropic/v1/messages", json.dumps(SMALL).encode()
        headers = [(b"x-api-key", b"sk-test"), (b"content-type", b"application/json")]

        async def ask():
            return (await _asgi(app, path, headers=headers, method="POST", body=body))[0]

        release.set()
        assert [await ask(), await ask()] == [200, 200]  # answered: given back
        modes.add("fail")
        assert [await ask(), await ask()] == [502, 502]  # its provider unreachable: given back
        modes.clear()
        real = server.spaces.app

        async def failing(identity):
            async def broken(scope, receive, send):
                raise RuntimeError("the space failed")

            return broken

        monkeypatch.setattr(server.spaces, "app", failing)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await ask()  # its space's app raised: given back
        monkeypatch.setattr(server.spaces, "app", real)
        release.clear()
        before = len(arrived)
        pending = asyncio.create_task(ask())
        await until(lambda: len(arrived) > before)
        assert (await _asgi(app, path, headers=headers, method="POST", body=body))[0] == 429  # it holds the one place
        pending.cancel()  # its client gone
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        release.set()
        assert await ask() == 200  # given back


async def test_a_websocket_holds_a_place_while_a_turn_is_answered_and_sockets_have_a_cap_of_their_own(server,
                                                                                                    monkeypatch):  # fmt: skip
    """A-16: an idle session (Codex keeps its socket open between turns) holds no place of the identity's requests in
    flight; a turn being answered does; and the sockets an identity holds open have a cap of their own."""
    release, _, _ = _held_provider(monkeypatch)
    release.set()
    server.settings = server.settings.model_copy(update={"requests_in_flight": 1})
    monkeypatch.setattr(app_module, "SOCKETS_PER_IDENTITY", 2)
    app = create_server_app(server)
    steps = {name: asyncio.Event() for name in ("opened", "ask", "asked", "answered", "close")}
    begun, turn_held = [], []

    async def relay(websocket, *args, **kwargs):  # as gateway.ws counts a turn: from the frame to its last event
        await websocket.accept()
        steps["opened"].set()
        turns = websocket.scope["latet.turns"]
        await steps["ask"].wait()
        refused = turns.begin()
        begun.append(refused)
        if refused is None:
            turn_held.append(True)
            steps["asked"].set()
            await steps["answered"].wait()
            turns.end()
            turn_held.clear()
        await steps["close"].wait()
        await websocket.close()

    monkeypatch.setattr("latet.gateway.ws.relay", relay)
    async with app.router.lifespan_context(app):
        _, code = await asyncio.to_thread(server.identities.add_person, "Emily Carter")
        _, token, _ = await asyncio.to_thread(server.identities.enrol, code, "laptop")
        path, body = f"/u/{route_key(token)}/anthropic/v1/messages", json.dumps(SMALL).encode()
        headers = [(b"x-api-key", b"sk-test"), (b"content-type", b"application/json")]
        sockets = f"/u/{route_key(token)}/openai/v1/responses"

        async def ask():
            return (await _asgi(app, path, headers=headers, method="POST", body=body))[0]

        first = asyncio.create_task(_websocket(app, sockets))
        await asyncio.wait_for(steps["opened"].wait(), 5)
        assert await ask() == 200  # open and idle: no place held
        steps["ask"].set()
        await asyncio.wait_for(steps["asked"].wait(), 5)
        assert begun == [None] and await ask() == 429  # a turn being answered holds the identity's one place
        second = asyncio.create_task(_websocket(app, sockets))  # another socket: within the sockets' cap
        await until(lambda: len(begun) == 2)
        assert "requests in flight" in begun[1]  # its turn is refused: the one place is the first turn's
        steps["answered"].set()
        await until(lambda: not turn_held)
        assert await ask() == 200  # the turn answered: its place given back, the socket still open
        denied = await asyncio.wait_for(_websocket(app, sockets, answers=True), 5)  # a third: past the sockets' cap
        assert denied[0] == {"type": "websocket.http.response.start", "status": 429, "headers": denied[0]["headers"]}
        assert "WebSockets open" in json.loads(denied[1]["body"])["error"]
        closed = await asyncio.wait_for(_websocket(app, sockets), 5)
        assert closed == [{"type": "websocket.close", "code": 4429, "reason": "too many WebSockets open: close one"}]
        steps["close"].set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert await ask() == 200


async def _websocket(app, path, answers=False, headers=()):
    """What ``app`` sends of a WebSocket's handshake at ``path`` and after it, until it ends. ``answers``: the server
    can answer a handshake with an HTTP response of its own (ASGI's websocket.http.response); ``headers``: the
    handshake's own, beside its host."""
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
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan"), *headers],
        "client": ("10.0.3.9", 50000), "server": ("latet.acme.lan", 443), "subprotocols": [],
        "extensions": {"websocket.http.response": {}} if answers else {},
    }  # fmt: skip
    await app(scope, receive, send)
    return sent


# --- the gate and its log (Task 13's hand-offs), its answers for a space (Task 12's) ----------------------------------


def test_request_log_stays_outermost_and_the_gate_marks_what_it_answers_itself(server, monkeypatch):
    app = create_server_app(server)
    order = [m.cls for m in app.user_middleware]
    assert order == [RequestLog, app_module.AnswerHeaders, BodyLimit, app_module.ServerGate]
    scopes = []

    async def outer(scope, receive, send):
        scopes.append(scope)
        await app(scope, receive, send)

    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 2)  # one budget for every kind of miss (A2)
    monkeypatch.setattr(app_module, "LOOKUP_LIMIT", 2)
    with TestClient(outer, base_url=URL) as client:
        _, token = _enrolled(server, client)
        mine, base = {"x-latet-token": token}, f"/u/{route_key(token)}"
        cases = [
            (lambda: client.get("/_latet/health"), 200, False),
            (lambda: client.get(f"{base}{ENGINE}/ping", headers=mine), 200, False),
            (lambda: client.get("/_latet/ui/", headers={"host": "evil.example"}), 403, True),
            (lambda: client.get(f"{base}/_latet/admin/known", headers=mine), 404, True),
            (lambda: client.get(f"{base}{ENGINE}/ping"), 401, True),
            (lambda: client.get(f"{API}/me"), 401, True),
            (lambda: client.post(f"/u/{'c' * 26}/anthropic/v1/messages", json={}), 403, True),
            (lambda: client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "1" * 43}), 401, False),
            (lambda: client.post(f"/u/{'d' * 26}/anthropic/v1/messages", json={}), 429, True),
            (lambda: client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "2" * 43}), 429, True),
        ]
        for send, status, refused in cases:
            assert send().status_code == status
            assert bool(scopes[-1].get(REFUSED)) is refused, (status, scopes[-1]["path"])


def test_a_space_the_instance_cannot_hand_out_is_refused_as_its_reason_says(server, client, monkeypatch):
    _, token = _enrolled(server, client)
    mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"
    _signed_in(client, token)
    assert client.get(ping, headers=mine).status_code == 200
    server.listening.clear()  # it does not hear Valkey: no space is built or handed out
    assert client.get(ping, headers=mine).status_code == 503 and client.get(f"{API}/me").status_code == 503
    server.listening.set()
    for raised, status, said in (
        (NotListening("x"), 503, "not available here"),
        (SpaceUnavailable("x"), 503, "not available here"),
        (IdentityUnavailable("x"), 401, "unauthorized"),
        (LookupError("x"), 503, "cannot answer now"),  # a KeyError of a build's: a fault, never an identity gone
        (RuntimeError("x"), 503, "cannot answer now"),  # its build failed: a store did not answer, say
    ):

        async def failing(identity, raised=raised):
            raise raised

        monkeypatch.setattr(server.spaces, "app", failing)
        for answer in (client.get(ping, headers=mine), client.get(f"{API}/me")):
            assert answer.status_code == status and said in answer.json()["error"], raised


def test_a_computers_identity_whose_space_cannot_be_built_is_refused_as_the_gate_refuses_it(
    server, client, monkeypatch, caplog
):
    """S8 (area 3, M4): ``PUT identity`` answers 503, with a line once a minute naming the error by its type, when its
    person's space cannot be built because a store does not answer: it answered 500, with a traceback at each call."""
    _, token = _enrolled(server, client)
    mine = _mine(token)

    async def unbuilt(identity):
        raise psycopg.OperationalError("connection to server at 10.0.0.9 failed: password authentication failed")

    monkeypatch.setattr(server.spaces, "runtime", unbuilt)
    monkeypatch.setattr(machines, "UNBUILT", Paced())  # said once a minute: whatever an earlier test made happen
    with caplog.at_level(logging.WARNING, logger="latet"):
        answers = [client.put(f"{SERVER}/identity", json={"usernames": [f"e{n}"]}, headers=mine) for n in range(3)]
    assert [answer.status_code for answer in answers] == [503] * 3
    assert all("cannot answer now" in answer.json()["error"] for answer in answers)
    said = [record.getMessage() for record in caplog.records if "could not be built" in record.getMessage()]
    assert len(said) == 1 and "OperationalError" in said[0]
    assert "10.0.0.9" not in caplog.text and all(record.exc_info is None for record in caplog.records)


def test_an_identity_disabled_unheard_is_refused_by_the_space_it_would_have(server, client):
    person, token = _enrolled(server, client)
    mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"
    assert client.get(f"{SERVER}/whoami", headers=mine).status_code == 200  # its token held by this instance
    assert client.get(ping, headers={"x-latet-token": "latet_pat_" + "3" * 43}).status_code == 401  # its address too
    with server.db.connection() as conn:  # no message: as when it was lost
        conn.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (person.id,))
    assert client.get(ping, headers=mine).status_code == 401  # its space is built as PostgreSQL has it: none
    assert server.spaces.count() == 0


def test_a_computer_that_forgets_its_token_ends_its_persons_sign_ins_off_the_event_loop(server, client, monkeypatch):
    person, token = _enrolled(server, client)
    sid = server.sign_ins.open(person.id)
    ended = []
    real = server._ended

    def recorded(identity_id):
        ended.append((identity_id, threading.current_thread().name))
        real(identity_id)

    monkeypatch.setattr(server, "_ended", recorded)
    assert client.delete(f"{SERVER}/token", headers={"x-latet-token": token}).json() == {"ok": True}
    # In the threads of what waits on a row or on Valkey (never the lookups', nor behind a person's lock), whatever the
    # revocation's message becomes.
    assert [identity for identity, _ in ended] == [person.id] and ended[0][1].startswith("latet-store")
    assert server.sign_ins.subject_of(sid) is None
    other = _another_computer(server, client, person)
    cleaned = []
    publish = server.bus.publish

    def lost(channel, message):  # the revocation's own message does not reach the instances either
        if channel == "identities":
            raise redis.TimeoutError("Timeout reading from socket")
        publish(channel, message)

    monkeypatch.setattr(server.bus, "publish", lost)
    monkeypatch.setattr(server, "_ended", lambda identity_id: (_ for _ in ()).throw(redis.TimeoutError("down")))
    monkeypatch.setattr(server, "_clean", lambda identity_id, *, removed: cleaned.append((identity_id, removed)))
    assert client.delete(f"{SERVER}/token", headers={"x-latet-token": other}).json() == {"ok": True}
    assert cleaned == [(person.id, False)]  # what could not be ended now, the beat's check tries again


# --- the beat (R-14, Task 12's and Task 13's hand-offs) -----------------------------------------------------------------


def _quiet_beat(server, monkeypatch, seconds):
    """The beat at ``seconds``, its steps doing nothing: no Valkey, no job."""
    monkeypatch.setattr(app_module, "BEAT_SECONDS", seconds)
    monkeypatch.setattr(app_module, "_alive", lambda _server: None)
    monkeypatch.setattr(app_module, "_audit", lambda _server: None)

    async def nothing():
        return None

    monkeypatch.setattr(server, "check", nothing)


async def test_a_beat_held_with_the_event_loop_judges_the_ears_silence_a_beat_later(server, monkeypatch, caplog):
    """A-05: a loop held past three beats (two large restores at once) held the ear too, which reads what was
    published meanwhile once it is let go: the beat does not take that for a silent ear, nor close every space."""
    _quiet_beat(server, monkeypatch, 0.2)
    silenced = []
    monkeypatch.setattr(server, "silenced", lambda: silenced.append(time.monotonic()))
    server.listening.set()
    server.beat_heard = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="latet.server.app"):
        beating = asyncio.create_task(app_module.beat(server))
        try:
            await asyncio.sleep(0.05)  # its first round: the ear heard its own beat just now
            server.beat_heard = time.monotonic() - 10  # nothing heard since, as far as the ear can tell...
            time.sleep(1.0)  # noqa: ASYNC251 - ...since the event loop itself is held: the point of the test
            await asyncio.sleep(0.05)  # the beat wakes, late
            assert silenced == []  # not judged at once
            woke = time.monotonic()
            await until(lambda: silenced, seconds=5)  # judged a beat later: the ear still silent
            assert silenced[0] - woke >= 0.05
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
    held = [r.getMessage() for r in caplog.records if "was held" in r.getMessage()]
    assert len(held) == 1 and "past its beat" in held[0]


async def test_a_round_of_the_beat_that_fails_ends_no_beat(server, monkeypatch, caplog):
    """A-17: whatever fails in a round (its steps are guarded each: this is what lies between them), the beat goes on,
    and says it once a minute by the error's type."""
    _quiet_beat(server, monkeypatch, 0.05)
    calls = []

    def broken(after):
        calls.append(after)
        raise RuntimeError("the words of a fault")

    monkeypatch.setattr(server, "ear_silent", broken)
    with caplog.at_level(logging.WARNING, logger="latet.server.app"):
        beating = asyncio.create_task(app_module.beat(server))
        try:
            await until(lambda: len(calls) >= 5, seconds=5)
            assert not beating.done()
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
    said = [r.getMessage() for r in caplog.records if "round" in r.getMessage()]
    assert len(said) == 1 and "RuntimeError" in said[0] and "the words of a fault" not in caplog.text


def test_a_purge_scans_valkeys_keys_once_and_asks_nothing_of_the_database(server, monkeypatch):
    """A-15, after the fix wave's S9: an identity's change counts go with its row (their foreign key), so the purge
    has no step in the database; it scans Valkey's keys once, and is marked done for the instances that come late."""
    person, _ = server.identities.add_person("Emily Carter")
    scans = []
    real = server.bus.purge_families
    monkeypatch.setattr(
        server.bus, "purge_families", lambda owner, families: scans.append(owner) or real(owner, families)
    )

    def refused(*args, **kwargs):
        raise AssertionError("the database was asked")

    monkeypatch.setattr(server.db, "connection", refused)
    assert server._purge(person.id) and scans == [person.id]
    assert server._purge(person.id) and scans == [person.id]  # marked done: never again


def test_a_build_that_fails_with_a_lookup_error_of_its_own_is_no_identity_gone(server, client, monkeypatch):
    """C-16: a KeyError or an IndexError of a build that failed is a fault (503: the computer keeps its token), never an
    identity gone or disabled (401)."""
    _, token = _enrolled(server, client)
    mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"

    async def faulty(identity):
        raise KeyError("a fault in the build")

    monkeypatch.setattr(server.spaces, "app", faulty)
    assert client.get(ping, headers=mine).status_code == 503

    async def gone(identity):
        raise IdentityUnavailable("there is no such identity, or it is disabled")

    monkeypatch.setattr(server.spaces, "app", gone)
    assert client.get(ping, headers=mine).status_code == 401


async def test_an_identity_enabled_again_drops_the_clean_up_still_pending_from_before(server, monkeypatch):
    """C-18: tried again after the identity was enabled, a clean-up pending from its disabling would end the sessions
    the person opens from then on."""
    person, _ = await asyncio.to_thread(server.identities.add_person, "Emily Carter")
    monkeypatch.setattr(server, "_end", lambda identity_id, removed: (False, ["TimeoutError"]))  # it keeps failing
    await server.heard("identities", {"id": person.id, "event": "disabled"})
    await until(lambda: server._cleaning is not None and server._cleaning.done())
    assert server._cleanups == {person.id: False}  # pending: the beat's check tries it again
    await server.heard("identities", {"id": person.id, "event": "enabled"})
    assert person.id not in server._cleanups
    await server.heard("identities", {"id": person.id, "event": "removed"})
    await server.heard("identities", {"id": person.id, "event": "enabled"})  # a removal's purge is never dropped
    assert server._cleanups == {person.id: True}


def test_the_beat_says_the_instance_lives_checks_its_spaces_and_writes_the_logs_counts_once_a_minute(
    server, monkeypatch
):
    monkeypatch.setattr(app_module, "BEAT_SECONDS", 0.1)
    monkeypatch.setattr(app_module, "FLUSH_SECONDS", 0.5)
    checks, flushes = [], []
    real = server.check

    async def check():
        checks.append(time.monotonic())
        await real()

    monkeypatch.setattr(server, "check", check)
    monkeypatch.setattr(logs_module, "flush", lambda: flushes.append(time.monotonic()))
    refusals = []  # what the administration counted of its refusals, written as a job of its own (C-34)
    monkeypatch.setattr(server.refusals, "flush", lambda audit: refusals.append(threading.current_thread().name))
    with TestClient(create_server_app(server), base_url=URL):
        time.sleep(1.3)
        heard = time.monotonic() - server.beat_heard
    assert len(checks) >= 8 and 2 <= len(flushes) <= 3  # every beat checks, the log's counts once in FLUSH_SECONDS
    assert len(refusals) == len(flushes) and threading.main_thread().name not in refusals  # alike, off the loop
    assert heard < 0.5  # its own beat, heard on its ear
    assert server.bus.get(f"instance:{server.instance}") == b"1"
    # The administration log's chain, checked (hourly, on one instance), its verdict sealed: nothing of it in clear.
    assert ChainVerdicts(server.bus, server.master).read() == "ok"
    assert b"ok" not in (server.bus.get("audit:chain") or b"ok")
    assert server.bus.get("job:audit-prune") == server.instance.encode()


def test_a_chain_verdict_is_believed_only_sealed_for_its_key_and_fresh(server):
    """Whoever has Valkey's password makes no verdict on the administration log's chain, and moves none: what the
    status reads is ``unknown`` unless the beat sealed it there, bound to its key, a little while ago."""
    now = [1_000_000.0]
    verdicts = ChainVerdicts(server.bus, server.master, clock=lambda: now[0])
    assert verdicts.read() == "unknown"  # none yet
    for forged in (b"1", b'{"ok": true, "at": 1000000.0}', b"\x01" * 64):  # written by hand
        server.bus.set("audit:chain", forged, 600)
        assert verdicts.read() == "unknown"
    verdicts.keep(ChainCheck(True, 3))
    assert verdicts.read() == "ok"
    verdicts.keep(ChainCheck(False, 3, broken_at=2))
    assert verdicts.read() == "broken"
    verdict = {"ok": True, "at": now[0], "broken_at": None}
    for sealer, key in (
        (server.master.sealer(b"audit-chain"), server.bus.key("another:key")),  # moved from another key
        (server.master.sealer(b"sign-ins"), server.bus.key("audit:chain")),  # sealed with another key
    ):
        server.bus.client.set(server.bus.key("audit:chain"), sealer.seal_json(verdict, key))
        # An instance that read none before (one that did keeps its own: the test below).
        assert ChainVerdicts(server.bus, server.master, clock=lambda: now[0]).read() == "unknown"
    verdicts.keep(ChainCheck(True, 3))
    now[0] += CHAIN_FRESH + 1  # written back long after: stale
    assert verdicts.read() == "unknown"
    now[0] -= CHAIN_FRESH + 1 + CHAIN_SKEW + 1  # stamped ahead of this clock by more than instances may differ
    assert verdicts.read() == "unknown"
    now[0] += CHAIN_SKEW  # within it: another instance's clock a little ahead
    assert verdicts.read() == "ok"


def test_a_chain_verdict_older_than_one_read_is_never_believed_again(server, monkeypatch):
    """Whoever has Valkey's password can keep a sealed verdict and write it back while it is fresh: an instance that
    read a newer one never goes back to it (Task 14's review, Minor 8). One reader for the server, which the beat's
    check keeps through and the administration's status reads through."""
    client = TestClient(create_server_app(server), base_url=URL)  # no lifespan: no beat keeps a verdict meanwhile
    now = [time.time()]
    monkeypatch.setattr(server, "clock", lambda: now[0])
    chain = server.chain
    assert isinstance(chain, ChainVerdicts) and server.chain is chain  # one for the instance
    chain.keep(ChainCheck(True, 3))
    kept_ok = server.bus.get("audit:chain")  # a copy of the "ok" someone keeps
    now[0] += 600
    chain.keep(ChainCheck(False, 4, broken_at=4))  # the beat's next check: broken
    assert chain.read() == "broken"
    server.bus.set("audit:chain", kept_ok, 600)  # the older verdict written back, still fresh
    assert chain.read() == "broken"  # never back to an older one
    server.bus.client.delete(server.bus.key("audit:chain"))  # nor hidden by taking it away
    assert chain.read() == "broken"
    server.bus.set("audit:chain", b"\x01" * 64, 600)  # nor by writing junk over it
    assert chain.read() == "broken"
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    server.bus.set("audit:chain", kept_ok, 600)
    status = client.get(f"{SERVER}/admin/status", headers=admin).json()
    assert status["audit"] == {"chain": "broken"}  # the administration's status reads through the same reader
    now[0] += CHAIN_FRESH + 1  # the newest it read is stale now: what it says is unknown, never the older one
    assert chain.read() == "unknown"
    fresh = ChainVerdicts(server.bus, server.master, clock=lambda: now[0] - CHAIN_FRESH - 1)
    assert fresh.read() == "ok"  # an instance that never read the newer one is told the older (the limit, said)


def test_a_slow_job_holds_up_no_beat(server, monkeypatch):
    monkeypatch.setattr(app_module, "BEAT_SECONDS", 0.1)
    release = threading.Event()

    def slow_audit(_server):
        release.wait(5)

    monkeypatch.setattr(app_module, "_audit", slow_audit)
    with TestClient(create_server_app(server), base_url=URL):
        time.sleep(0.8)
        heard = time.monotonic() - server.beat_heard
        release.set()
    assert heard < 0.5 and not server.ear_silent(3 * app_module.BEAT_SECONDS)


def test_an_ear_that_hears_nothing_is_cancelled_within_three_beats_and_the_instance_is_not_ready(server, monkeypatch):
    monkeypatch.setattr(app_module, "BEAT_SECONDS", 0.2)
    held = threading.Event()
    listen = server.bus.listen

    async def holding(*channels, ready=None):  # the connection stays open, and delivers nothing while held
        async with contextlib.aclosing(listen(*channels, ready=ready)) as messages:
            async for item in messages:
                if not held.is_set():
                    yield item

    monkeypatch.setattr(server.bus, "listen", holding)
    with TestClient(create_server_app(server), base_url=URL) as client:
        _, token = _enrolled(server, client)
        mine, ping = {"x-latet-token": token}, f"/u/{route_key(token)}{ENGINE}/ping"
        assert client.get(ping, headers=mine).status_code == 200 and server.spaces.count() == 1
        held.set()
        started, silent = time.monotonic(), None
        while time.monotonic() - started < 5:
            if client.get("/_latet/ready").status_code == 503 and server.spaces.count() == 0:
                silent = time.monotonic() - started
                break
            time.sleep(0.02)
        assert silent is not None and silent < 5 * app_module.BEAT_SECONDS  # three beats, and one late
        held.clear()  # it hears again
        for _ in range(250):
            if client.get("/_latet/ready").status_code == 200:
                break
            time.sleep(0.02)
        assert client.get(ping, headers=mine).status_code == 200
