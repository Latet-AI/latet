"""Two instances of a Latet server behind one PostgreSQL and one Valkey act as one server (server-design.md, "Done
when"): two people and an API key are masked each in their own space whichever instance serves them, and none sees
another's; a conversation moving between instances restores the same fakes; a decision taken in a dashboard served by
one instance lets a request waiting on the other leave; a change to the shared settings reaches every space; disabling
a person stops them on both at once; stopping an instance loses nothing; the administration log has each of these
actions, and the logs hold no token, key, route key or value."""

import asyncio
import contextlib
import json
import socket
import threading
import time
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from latet.engine.types import EntityClass
from latet.engine.vault import derive_keys
from latet.server import app as app_module
from latet.server import spaces as spaces_module
from latet.server.app import create_server_app
from latet.server.history import PgHistoryStore
from latet.server.tokens import route_key

from ..e2e.fake_upstream import FakeUpstream
from .conftest import dump, valkey_dump
from .support import URL, captured_logs, close_test_server, open_test_server, until

pytestmark = pytest.mark.server

SERVER = "/_latet/server/v1"
ADMIN = f"{SERVER}/admin"
API = "/_latet/api/v1"
ENGINE = "/_latet/engine/v1"
UI = {"x-latet-ui": "1"}
EMILY_MAIL, DANIEL_MAIL = "emily.carter@acme.com", "daniel.okafor@acme.com"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@contextlib.asynccontextmanager
async def _started(*servers):
    """Each instance's app, started: the apps."""
    async with contextlib.AsyncExitStack() as stack:
        apps = []
        for server in servers:
            app = create_server_app(server)
            await stack.enter_async_context(app.router.lifespan_context(app))
            apps.append(app)
        yield apps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False)


@contextlib.asynccontextmanager
async def _serving(*servers):
    """Each instance's app started, and a client for each."""
    async with _started(*servers) as apps, contextlib.AsyncExitStack() as stack:
        yield [await stack.enter_async_context(_client(app)) for app in apps]


async def _share(client, admin, upstream, *more):
    """The shared settings: the fake model as the destination `llm`, and as each destination named in ``more``."""
    shared = {"upstreams": [{"name": name, "url": upstream.url} for name in ("llm", *more)]}
    assert (await client.put(f"{ADMIN}/shared", json=shared, headers=admin)).status_code == 200


# Each computer's key, by its token, as its enrolment gave it: what a sign-in link asks for with the token (R16).
KEYS: dict[str, str] = {}


def _cli(token):
    """What a computer's CLI sends on a sign-in link: its token and its computer key."""
    return {"x-latet-token": token, "x-latet-computer-key": KEYS[token]}


async def _person(client, admin, name):
    """A person the administration makes, and their computer's token: (their id, the token, the enrolment code the
    computer spent); the computer's key in ``KEYS``."""
    made = (await client.post(f"{ADMIN}/identities", json={"kind": "person", "name": name}, headers=admin)).json()
    enrolled = await client.post(f"{SERVER}/enrol", json={"code": made["code"], "label": "laptop"})
    KEYS[enrolled.json()["token"]] = enrolled.json()["computer_key"]
    return made["identity"]["id"], enrolled.json()["token"], made["code"]


async def _sign_in(client, token):
    link = (await client.post(f"{SERVER}/login-code", headers=_cli(token))).json()["url"]
    assert (await client.get(link.removeprefix(URL), follow_redirects=False)).status_code == 303


async def _live_reviewer_on(client, identity_id, *servers):
    """The Live Reviewer turned on by the person signed in on ``client``, in her own settings (a rule: the Community
    Edition sets none for everyone), and in force in her space on each of ``servers``."""
    etag = (await client.get(f"{API}/config")).json()["etag"]
    on = {"live_review": {"enabled": True}, "etag": etag}
    assert (await client.put(f"{API}/config", json=on, headers=UI)).status_code == 200
    for server in servers:
        runtime = await server.spaces.runtime(server.identities.get(identity_id))
        await until(lambda runtime=runtime: runtime.config.live_review.enabled)


def _send(client, secret, text, destination="llm"):
    """An agent's request through its identity's address, to a shared destination."""
    body = {"model": "m", "messages": [{"role": "user", "content": text}]}
    path = f"/u/{route_key(secret)}/{destination}/v1/chat/completions"
    return client.post(path, json=body, headers={"authorization": "Bearer sk-test"})


async def _chat(client, secret, text, destination="llm"):
    """The text of the answer, as the agent reads it."""
    answer = await _send(client, secret, text, destination)
    assert answer.status_code == 200
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )


def _forms(value):
    """The ways a log line could write ``value`` (Task 13's N8): as it is, in either case, escaped as a part of a URL
    and as a JSON string."""
    return {value, value.upper(), value.lower(), quote(value, safe=""), json.dumps(value)[1:-1]}


def _held_nothing(lines, *secrets):
    """None of ``secrets`` in the lines (a log's, or a store's as its dump reads it), in any of its forms; for a token or
    a key, nor its route key in any of its."""
    for secret in secrets:
        held = [secret, route_key(secret)] if secret.startswith(("latet_pat_", "latet_key_")) else [secret]
        for value in held:
            for form in _forms(value):
                assert form not in lines, f"{value[:14]}... is there, written {form[:14]}..."


@contextlib.contextmanager
def _on_uvicorn(app):
    """``app`` served by uvicorn on a port of 127.0.0.1, in a thread with an event loop of its own, as `latet server
    run` serves it behind its proxy (E0c): uvicorn's logging left to configure_logging, the client's address and the
    scheme read from the proxy's headers. Its port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_config=None, log_level=None, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    served = uvicorn.Server(config)
    thread = threading.Thread(target=served.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not served.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield sock.getsockname()[1]
    finally:
        served.should_exit = True
        thread.join(20)
        sock.close()


async def test_each_identity_is_masked_in_its_own_space_whichever_instance_serves_it(instances, upstream, monkeypatch):
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    with captured_logs() as logs:
        async with _serving(a, b) as (one, two):
            await _share(one, admin, upstream)
            _, emily, her_code = await _person(one, admin, "Emily Carter")
            _, daniel, his_code = await _person(two, admin, "Daniel Okafor")
            # Real seed-26 generation makes Daniel's own fake contain Emily's name.
            daniels = await b.spaces.runtime(b.identities.by_secret(daniel))
            monkeypatch.setattr(daniels.built.vault, "_k_sur", derive_keys((26).to_bytes(32, "big"))["surrogate"])
            made = await one.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin)
            key = made.json()["key"]
            billing_mail = "billing-ci@acme.com"
            own_fake = "emily.scott@consolidated-a08c.example"
            for client, secret, mail in (
                (one, emily, EMILY_MAIL),
                (two, daniel, DANIEL_MAIL),
                (two, key, billing_mail),
            ):
                assert mail in await _chat(client, secret, f"write to {mail} about the invoice")
            assert all(mail not in upstream.all_received() for mail in (EMILY_MAIL, DANIEL_MAIL, billing_mail))
            await _sign_in(two, daniel)
            session = two.cookies["latet_session"]  # his dashboard's: a credential, as his token is
            foreign_entries = []
            for owner, secret, mail in ((a, emily, EMILY_MAIL), (b, key, billing_mail)):
                runtime = await owner.spaces.runtime(owner.identities.by_secret(secret))
                entity = runtime.built.vault.by_original(mail)
                assert entity is not None and entity.etype == "EMAIL"
                foreign_entries.append(entity)
            one.cookies.update(two.cookies)
            for client in (one, two):
                response = await asyncio.wait_for(client.get(f"{API}/vault"), 5)
                assert response.status_code == 200
                assert len(response.content) <= 16 * 1024
                page = response.json()
                assert page["total"] == len(page["items"]) <= 10
                originals = {}
                for entry in page["items"]:
                    revealed = await asyncio.wait_for(client.post(f"{API}/vault/{entry['id']}/reveal", headers=UI), 5)
                    assert revealed.status_code == 200
                    assert len(revealed.content) <= 1024
                    originals[entry["id"]] = revealed.json()["original"]
                assert len(originals) == len(page["items"])
                assert set(originals.values()) == {DANIEL_MAIL, "acme.com"}
                assert len(originals) == 2
                [email_id] = [entity_id for entity_id, value in originals.items() if value == DANIEL_MAIL]
                for query in ("emily", own_fake, email_id):
                    found = await asyncio.wait_for(client.get(f"{API}/vault", params={"q": query}), 5)
                    assert found.status_code == 200
                    assert len(found.content) <= 16 * 1024
                    matched = found.json()
                    assert matched["total"] == len(matched["items"]) == 1
                    [entry] = matched["items"]
                    assert entry["id"] == email_id
                    assert entry["type"] == "EMAIL" and entry["surrogate"] == own_fake
                    assert originals[entry["id"]] == DANIEL_MAIL
                for entity in foreign_entries:
                    assert entity.id not in originals
                    refused = await asyncio.wait_for(client.post(f"{API}/vault/{entity.id}/reveal", headers=UI), 5)
                    assert refused.status_code == 404
                    assert len(refused.content) <= 1024
            history = []
            for _ in range(250):  # the recorder writes a moment after the answer
                history = (await two.get(f"{API}/exchanges")).json()["items"]
                if history:
                    break
                await asyncio.sleep(0.02)
            assert len(history) == 1  # his own exchange only
    actions = {e.action for e in a.audit.query()}
    assert {"server.started", "shared.changed", "person.added", "key.added", "token.added"} <= actions
    _held_nothing(
        logs.getvalue(),
        emily,
        daniel,
        key,
        her_code,
        his_code,
        session,
        EMILY_MAIL,
        DANIEL_MAIL,
        billing_mail,
        own_fake,
    )


async def test_a_conversation_moving_between_instances_restores_the_same_fakes(instances, upstream):
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        _, emily, _ = await _person(one, admin, "Emily Carter")
        base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
        first = {"session": "s1", "items": [{"text": f"write to {EMILY_MAIL}", "kind": "user_text"}]}
        [masked] = (await one.post(f"{base}/mask", json=first, headers=token)).json()["items"]
        fake = masked["text"].removeprefix("write to ")
        again = {"session": "s3", "items": [{"text": f"and to {EMILY_MAIL}", "kind": "user_text"}]}
        [theirs] = (await two.post(f"{base}/mask", json=again, headers=token)).json()["items"]
        assert EMILY_MAIL not in fake and theirs["text"] == f"and to {fake}"  # one fake, whichever instance
        back = {"session": "s1", "items": [f"You said: write to {fake}"]}
        restored = (await two.post(f"{base}/rehydrate", json=back, headers=token)).json()["items"]
        assert restored == [f"You said: write to {EMILY_MAIL}"]  # the other instance knows what s1 was shown
        other = {"session": "s2", "items": [f"You said: write to {fake}"]}
        assert EMILY_MAIL not in (await two.post(f"{base}/rehydrate", json=other, headers=token)).json()["items"][0]


async def test_a_conversation_moving_between_instances_leaves_as_it_left_and_is_not_stopped(instances, upstream):
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        _, emily, _ = await _person(one, admin, "Emily Carter")
        base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
        text = "ask Emily Carter about the Orion budget"
        first = {"session": "s1", "items": [{"text": text, "kind": "user_text"}]}
        [left] = (await one.post(f"{base}/mask", json=first, headers=token)).json()["items"]
        assert left["text"] == text  # nobody knows these names yet
        check = {"session": "s1", "slots": [{"text": text, "kind": "user_text"}]}
        assert (await one.post(f"{base}/verify", json=check, headers=token)).json()["blocking"] == []
        here = await a.spaces.runtime(a.identities.by_secret(emily))
        learnt = await asyncio.to_thread(here.built.vault.intern, "CLIENT", EntityClass.CUSTOM, "Emily Carter")
        there = await b.spaces.runtime(b.identities.by_secret(emily))
        await until(lambda: there.built.vault.get(learnt.id) is not None)  # learnt since, everywhere
        [again] = (await two.post(f"{base}/mask", json=first, headers=token)).json()["items"]
        assert again["text"] == text  # as it left, on the other instance
        verdict = (await two.post(f"{base}/verify", json=check, headers=token)).json()
        assert verdict["blocking"] == [] and verdict["flagged"]  # found and reported, not stopped: it left already
        fresh = {"session": "s2", "items": [{"text": text, "kind": "user_text"}]}
        [new] = (await two.post(f"{base}/mask", json=fresh, headers=token)).json()["items"]
        assert "Emily Carter" not in new["text"]  # a new conversation: masked with what the vault knows now


async def test_a_conversation_in_a_project_with_a_term_of_its_own_leaves_as_it_left_on_another_instance(instances):
    """R-9: the project's rules have a name of their own, the same on every instance, whatever each one counts of the
    settings it applied; and a space built with a person's settings names its rules as one that applied the same
    settings while it was open does (the 9a review's hand-off)."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        there = await b.spaces.runtime(b.identities.get(emily_id))  # open on B before her project is set
        await _sign_in(one, emily)
        folder = "/home/emily/billing-api"
        project = (await one.post(f"{API}/projects", json={"path": folder}, headers=UI)).json()["id"]
        etag = (await one.get(f"{API}/config")).json()["etag"]  # the page's, which a save sends (E0c's Task 11)
        terms = {"terms": [{"value": "Orion", "type": "PROJECT"}], "etag": etag}
        assert (await one.put(f"{API}/projects/{project}/personal", json=terms, headers=UI)).status_code == 200
        await until(lambda: any(p.path == folder for p in there.config.projects))  # applied there while open
        await a.spaces.drop(emily_id)
        here = await a.spaces.runtime(a.identities.get(emily_id))  # built on A with them
        assert here.built.engine.rules_tag == there.built.engine.rules_tag
        base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
        text = "ask Emily Carter about the Orion contract"
        first = {"session": "s1", "cwd": folder, "items": [{"text": text, "kind": "user_text"}]}
        [left] = (await one.post(f"{base}/mask", json=first, headers=token)).json()["items"]
        assert "Orion" not in left["text"] and "Emily Carter" in left["text"]  # the project's term; no name known yet
        check = {"session": "s1", "cwd": folder, "slots": [{"text": left["text"], "kind": "user_text"}]}
        assert (await one.post(f"{base}/verify", json=check, headers=token)).json()["blocking"] == []
        learnt = await _learnt(a, emily, "Emily Carter")
        await until(lambda: there.built.vault.get(learnt.id) is not None)
        [again] = (await two.post(f"{base}/mask", json=first, headers=token)).json()["items"]
        assert again["text"] == left["text"]  # as it left, in its project, on the other instance
        verdict = (await two.post(f"{base}/verify", json=check, headers=token)).json()
        assert verdict["blocking"] == [] and verdict["flagged"]


async def test_a_decision_in_a_dashboard_on_one_instance_lets_a_request_waiting_on_the_other_leave(instances, upstream):
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        await _share(one, admin, upstream)
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        await _sign_in(two, emily)
        await _live_reviewer_on(two, emily_id, a, b)
        sent = asyncio.ensure_future(_send(one, emily, f"write to {EMILY_MAIL} about Orion"))
        batches = []
        for _ in range(250):
            batches = (await two.get(f"{API}/approvals")).json()["batches"]
            if batches:
                break
            await asyncio.sleep(0.02)
        [batch] = batches
        decided = await two.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
        assert decided.status_code == 200
        assert (await asyncio.wait_for(sent, 10)).status_code == 200
    assert EMILY_MAIL not in upstream.all_received()


async def test_the_text_sent_after_an_approval_does_not_wait_again_on_another_instance(instances):
    """Claude Code keeps the prompt it sent, masked, and its plugin hands that row back to the server: approved already,
    on whichever instance it arrives, as the text it came from is (never a second approval of the first's stand-ins)."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        await _sign_in(two, emily)
        await _live_reviewer_on(two, emily_id, a, b)
        base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
        first = {"session": "s1", "items": [{"text": f"write to {EMILY_MAIL} about Orion", "kind": "user_text"}]}
        pending = (await one.post(f"{base}/mask", json=first, headers=token)).json()["pending"]
        decided = await two.post(f"{API}/approvals/{pending}", json={"action": "approve"}, headers=UI)
        assert decided.status_code == 200
        answer: dict = {}
        for _ in range(50):
            answer = (await one.post(f"{base}/approvals/wait", json={"id": pending}, headers=token)).json()
            if "items" in answer:
                break
        [sent] = answer["items"]
        assert EMILY_MAIL not in sent["text"]
        again = {"session": "s1", "items": [{"text": sent["text"], "kind": "user_text"}]}
        reply = (await two.post(f"{base}/mask", json=again, headers=token)).json()
        assert not reply.get("pending"), reply
        assert reply["items"][0]["text"] == sent["text"]
        assert (await two.get(f"{API}/approvals")).json()["batches"] == []


async def test_a_value_let_through_on_one_instance_shows_in_clear_in_a_text_waiting_on_the_other(instances):
    """A text waits, opened on one instance (its texts are there); the person's dashboard is on the other, where she
    chooses Not sensitive for a value of it: her settings reach the first instance, which masks the waiting text again,
    and her Live Reviewer stream on the other is told the text as it will leave, the value in clear."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _started(a, b) as (app_one, app_two), _client(app_one) as one, _client(app_two) as two:
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        await _sign_in(two, emily)
        await _live_reviewer_on(two, emily_id, a, b)
        base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
        first = {"session": "s1", "items": [{"text": f"write to {EMILY_MAIL} about Orion", "kind": "user_text"}]}
        pending = (await one.post(f"{base}/mask", json=first, headers=token)).json()["pending"]
        read, reading = await _stream(app_two, two.cookies["latet_session"])
        try:
            await until(lambda: "event: snapshot" in read[0])
            assert f'"masked": "write to {EMILY_MAIL}' not in read[0]
            assert (await two.post(f"{API}/allow", json={"value": EMILY_MAIL}, headers=UI)).status_code == 200
            await until(lambda: "event: updated" in read[0])
        finally:
            reading.cancel()
        updated = read[0].split("event: updated", 1)[1].split("data: ", 1)[1].split("\n", 1)[0]
        summary = json.loads(updated)
        assert summary["id"] == pending and EMILY_MAIL in summary["items"][0]["masked"]
        [listed] = (await two.get(f"{API}/approvals")).json()["batches"]
        assert EMILY_MAIL in listed["items"][0]["masked"]


async def test_a_text_of_13_mib_with_the_live_reviewer_on_is_stopped_and_every_instance_still_hears(
    instances, upstream
):
    """Task 10's I4: a person pastes a log of 13 MiB into a prompt with the Live Reviewer on, her dashboard open on the
    other instance. The request is stopped, saying why, nothing of it sent; and nothing of it reaches the bus, whose
    subscribers Valkey would cut past its output limit: every instance still hears Valkey, its spaces open."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        await _share(one, admin, upstream)
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        await _sign_in(two, emily)
        await _live_reviewer_on(two, emily_id, a, b)
        assert (await two.get(f"{API}/approvals")).json()["batches"] == []  # her queue, watched there
        here = await a.spaces.runtime(a.identities.get(emily_id))
        there = await b.spaces.runtime(b.identities.get(emily_id))
        line = "2026-10-15 05:12:44 INFO billing worker processed invoice batch 4411 for account 99 in 12 ms\n"
        log = line * (13 * 1024 * 1024 // len(line))
        sent = len(upstream.received)
        stopped = await asyncio.wait_for(_send(one, emily, f"why did it fail? write to {EMILY_MAIL}\n{log}"), 120)
        assert stopped.status_code != 200 and "Live Reviewer" in stopped.text and "send less text" in stopped.text
        assert len(upstream.received) == sent  # nothing of it left
        assert (await two.get(f"{API}/approvals")).json()["batches"] == []  # nothing of it waits, anywhere
        await asyncio.sleep(0.5)
        for server, runtime, client in ((a, here, one), (b, there, two)):
            assert server.listening.is_set() and (await client.get("/_latet/ready")).status_code == 200
            assert await server.spaces.runtime(server.identities.get(emily_id)) is runtime  # no space closed


async def test_a_shared_change_reaches_every_space_and_disabling_a_person_stops_them_on_both(instances, upstream):
    """Emily's space is open on both instances, Daniel's on the other alone: the shared change, a destination added
    through the first, reaches his too, which only the other instance's copy of the shared settings, read again when it
    hears of the change, gives him (her space on the first also writes her approved settings, which hers on the other
    reads)."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        await _share(one, admin, upstream)
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        daniel_id, daniel, _ = await _person(two, admin, "Daniel Okafor")
        await _chat(one, emily, "the Falcon budget")
        await _chat(two, emily, "the Falcon budget")  # her space on each instance
        await _chat(two, daniel, "the Falcon budget")  # his on the other alone
        assert (await _send(two, daniel, "the Falcon budget", "gateway")).status_code == 404  # no such destination yet
        await _share(one, admin, upstream, "gateway")
        there = await b.spaces.runtime(b.identities.get(emily_id))
        his = await b.spaces.runtime(b.identities.get(daniel_id))
        await until(lambda: all("gateway" in [d.name for d in s.config.upstreams] for s in (there, his)))
        for secret in (emily, daniel):
            sent = len(upstream.received)
            await _chat(two, secret, "the Falcon budget", "gateway")
            assert len(upstream.received) == sent + 1  # through the destination added
        await one.patch(f"{ADMIN}/identities/{emily_id}", json={"disabled": True}, headers=admin)
        assert (await _send(one, emily, "hello")).status_code == 403
        await until(lambda: b.spaces.count() == 1)  # hers closed on the other instance, his open
        assert (await _send(two, emily, "hello")).status_code == 403
        assert await b.spaces.runtime(b.identities.get(daniel_id)) is his
    assert {"shared.changed", "person.disabled"} <= {e.action for e in a.audit.query()}


FRAME = {
    "type": "response.create",
    "model": "m",
    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"mail {EMILY_MAIL}"}]}],
}


async def test_disabling_a_person_closes_their_websocket_on_the_other_instance_and_nothing_more_reaches_their_destination(
    instances,
):
    """S5 (area 5, Important 2; area 3, M9): the WebSocket twin of the test above. Emily's socket is open through the
    other instance when the first disables her: it closes at once there (``CLOSED_CODE``), and nothing she sends after
    reaches her destination, a binary frame nor a text one."""
    a, b = instances
    received: list[str | bytes] = []

    async def destination(websocket):  # keeps every frame it is sent, and answers none
        await websocket.accept()
        while (message := await websocket.receive())["type"] != "websocket.disconnect":
            received.append(message.get("text") or message.get("bytes"))

    far = Starlette(routes=[WebSocketRoute("/backend-api/codex/responses", destination)])
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    proxied = {"host": "latet.acme.lan", "x-forwarded-proto": "https", "x-forwarded-for": "10.20.30.40"}
    with (
        _on_uvicorn(far) as there,
        _on_uvicorn(create_server_app(a)) as first,
        _on_uvicorn(create_server_app(b)) as second,
    ):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{first}", headers=proxied, trust_env=False) as one:
            shared = {"upstreams": [{"name": "llm", "url": f"http://127.0.0.1:{there}"}]}
            assert (await one.put(f"{ADMIN}/shared", json=shared, headers=admin)).status_code == 200
            emily_id, emily, _ = await _person(one, admin, "Emily Carter")
            where = f"ws://latet.acme.lan/u/{route_key(emily)}/llm/backend-api/codex/responses"
            headers = {"x-forwarded-proto": "https", "authorization": "Bearer sk-test", "session-id": "ws-1"}
            async with connect(where, host="127.0.0.1", port=second, proxy=None, additional_headers=headers) as ws:
                await ws.send(json.dumps(FRAME))
                await until(lambda: len(received) == 1)  # her turn reached her destination, masked
                disabled = await one.patch(f"{ADMIN}/identities/{emily_id}", json={"disabled": True}, headers=admin)
                assert disabled.status_code == 200
                await until(lambda: b.spaces.count() == 0)  # her space closed on the other instance
                with contextlib.suppress(ConnectionClosed):  # closed already, or about to be
                    await ws.send(b"\x00a binary frame, after the disabling")
                    await ws.send(json.dumps(FRAME))
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
                await asyncio.sleep(0.5)  # what would still reach it
    assert len(received) == 1 and EMILY_MAIL not in str(received[0])
    assert closed.value.rcvd is not None and closed.value.rcvd.code == spaces_module.CLOSED_CODE


async def test_disabling_a_person_ends_their_dashboards_streams_on_the_other_instance(instances):
    """S5 (area 5, Question 5): the dashboard's two live streams, the Live Reviewer's and the history's, open on the
    other instance, end when the first disables the person: no keepalive holds them open, nothing more is told there."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _started(a, b) as (app_one, app_two), _client(app_one) as one, _client(app_two) as two:
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        await _sign_in(two, emily)
        cookie = two.cookies["latet_session"]
        streams = [await _stream(app_two, cookie, path) for path in (f"{API}/approvals/stream", f"{API}/stream")]
        await until(lambda: all("retry: 3000" in read[0] for read, _ in streams))  # both open
        await one.patch(f"{ADMIN}/identities/{emily_id}", json={"disabled": True}, headers=admin)
        await asyncio.wait_for(asyncio.gather(*(task for _, task in streams)), 5)  # ended there, not cancelled here
    assert b.spaces.count() == 0


async def test_an_instance_whose_ear_goes_silent_steps_aside_while_the_other_serves_and_misses_nothing(
    instances, upstream, monkeypatch
):
    """R-14 across two instances: an instance whose connection to Valkey stays open and delivers nothing closes its
    spaces and is not ready within three beats, while the other serves on; a change made meanwhile through the other
    reaches it once it hears again, since it then reads everything from PostgreSQL: for Daniel too, whose space is on
    that instance alone (Emily's on the other writes her approved settings, which hers reads). Beats of half a second;
    the wait before listening again (``RETRY_SECONDS``) left as it is, a second, while which the instance refuses."""
    monkeypatch.setattr(app_module, "BEAT_SECONDS", 0.5)
    a, b = instances
    held, listen = asyncio.Event(), a.bus.listen

    async def holding(*channels, ready=None):  # A's connection stays open, and delivers nothing while held
        async with contextlib.aclosing(listen(*channels, ready=ready)) as messages:
            async for item in messages:
                if not held.is_set():
                    yield item

    monkeypatch.setattr(a.bus, "listen", holding)
    asked, silent = [], a.ear_silent

    def asking(after):  # how long a silence the beat waits for: three beats (R-14)
        asked.append(after)
        return silent(after)

    monkeypatch.setattr(a, "ear_silent", asking)
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        await _share(one, admin, upstream)
        _, emily, _ = await _person(one, admin, "Emily Carter")
        _, daniel, _ = await _person(one, admin, "Daniel Okafor")
        await _chat(one, emily, "the Falcon budget")
        await _chat(one, daniel, "the Falcon budget")  # his space on A alone, before and after
        assert a.spaces.count() == 2
        held.set()
        last = a.beat_heard  # when A last heard a beat of its own
        await until(lambda: a.spaces.count() == 0 and not a.listening.is_set())
        # Silent past three beats: noticed at the third or the fourth beat after the last one heard, the beat's own time
        # and the clock's grain deciding which, so before 4.5.
        assert set(asked) == {3 * app_module.BEAT_SECONDS}
        assert time.monotonic() - last < 4.5 * app_module.BEAT_SECONDS
        assert (await one.get("/_latet/ready")).status_code == 503 and (
            await _send(one, emily, "hi")
        ).status_code == 503
        assert (await two.get("/_latet/ready")).status_code == 200
        await _share(two, admin, upstream, "gateway")  # a destination added for everyone: A hears nothing
        sent = len(upstream.received)
        await _chat(two, emily, "the Falcon budget", "gateway")
        assert len(upstream.received) == sent + 1
        held.clear()
        await until(lambda: a.listening.is_set())
        for secret in (emily, daniel):
            sent = len(upstream.received)
            await _chat(one, secret, "the Falcon budget", "gateway")
            assert len(upstream.received) == sent + 1  # what changed while A was deaf, in force on A too


async def test_an_instance_stopped_loses_nothing(schema, prefix, upstream, monkeypatch):
    """Instance A stops while the exchange it served is still being written (each write of the history held a
    second): what A drains as its spaces close is what B then shows."""
    real = PgHistoryStore.record

    def held(self, record):
        time.sleep(1.0)  # in A's recorder's thread
        return real(self, record)

    monkeypatch.setattr(PgHistoryStore, "record", held)
    a, b = open_test_server(schema, prefix), open_test_server(schema, prefix)
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    try:
        async with _serving(b) as (two,):
            async with _serving(a) as (one,):
                await _share(one, admin, upstream)
                _, emily, _ = await _person(one, admin, "Emily Carter")
                await _chat(one, emily, f"write to {EMILY_MAIL}")
                base, token = f"/u/{route_key(emily)}{ENGINE}", {"x-latet-token": emily}
                first = {"session": "s1", "items": [{"text": f"mail {EMILY_MAIL}", "kind": "user_text"}]}
                [masked] = (await one.post(f"{base}/mask", json=first, headers=token)).json()["items"]
            close_test_server(a)  # instance A stops: its app ended, its connections closed
            monkeypatch.setattr(PgHistoryStore, "record", real)
            back = {"session": "s1", "items": [masked["text"]]}
            assert (await two.post(f"{base}/rehydrate", json=back, headers=token)).json()["items"] == [
                f"mail {EMILY_MAIL}"
            ]
            await _sign_in(two, emily)
            history = (await two.get(f"{API}/exchanges")).json()["items"]
            assert [item.get("upstream") for item in history].count("llm") == 1  # recorded on A, shown by B
    finally:
        close_test_server(a)
        close_test_server(b)


# --- another identity sees nothing, protocol by protocol, whichever instance serves it (R-26) -------------------------


async def _learnt(server, secret, value):
    """``value`` learnt by the vault of ``secret``'s identity, through ``server``'s space of it (the local reviewer
    or a person's mask would teach it so)."""
    runtime = await server.spaces.runtime(server.identities.by_secret(secret))
    return await asyncio.to_thread(runtime.built.vault.intern, "CLIENT", EntityClass.CUSTOM, value)


async def _exchanges(client, count):
    """The signed-in person's exchanges once ``count`` are recorded (the recorder writes a moment after the answer)."""
    items = []
    for _ in range(250):
        items = (await client.get(f"{API}/exchanges")).json()["items"]
        if len(items) >= count:
            return items
        await asyncio.sleep(0.02)
    raise AssertionError(f"{len(items)} exchanges recorded, not {count}")


async def _stream(app, cookie, path=f"{API}/approvals/stream"):
    """What a dashboard signed in with ``cookie`` reads of one of its live streams on ``app``, until the task reading it
    is cancelled: the text read so far, kept in the list it answers."""
    read, scope = [""], {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan"), (b"cookie", f"latet_session={cookie}".encode())],
        "client": ("10.0.3.9", 50000), "server": ("latet.acme.lan", 443),
    }  # fmt: skip
    asked = False

    async def receive():
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # the viewer stays until the task is cancelled
        raise AssertionError("unreachable")

    async def send(message):
        if message["type"] == "http.response.body":
            read[0] += message.get("body", b"").decode()

    async def reading():
        with contextlib.suppress(asyncio.CancelledError):
            await app(scope, receive, send)

    return read, asyncio.create_task(reading())


async def test_another_identity_shares_no_conversation_with_someone_else_whichever_instance_serves_it(instances):
    """``Given.masked`` and ``checked`` (R-26): a text that left in Emily's conversation has not left in Daniel's of the
    same name, on the other instance, whatever both vaults know."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        _, emily, _ = await _person(one, admin, "Emily Carter")
        _, daniel, _ = await _person(two, admin, "Daniel Okafor")
        hers, his = f"/u/{route_key(emily)}{ENGINE}", f"/u/{route_key(daniel)}{ENGINE}"
        text = "ask Emily Carter about the Orion budget"
        first = {"session": "s1", "items": [{"text": text, "kind": "user_text"}]}
        check = {"session": "s1", "slots": [{"text": text, "kind": "user_text"}]}
        [left] = (await one.post(f"{hers}/mask", json=first, headers={"x-latet-token": emily})).json()["items"]
        assert left["text"] == text  # her vault knows no name yet: it leaves as it is
        assert (await one.post(f"{hers}/verify", json=check, headers={"x-latet-token": emily})).json()["blocking"] == []
        await _learnt(a, emily, "Emily Carter")
        await _learnt(b, daniel, "Emily Carter")  # both vaults know it now
        [masked] = (await two.post(f"{his}/mask", json=first, headers={"x-latet-token": daniel})).json()["items"]
        assert "Emily Carter" not in masked["text"]  # his conversation s1: nothing of it left as it is
        his_check = (await two.post(f"{his}/verify", json=check, headers={"x-latet-token": daniel})).json()
        assert his_check["blocking"]  # it never left in his s1: stopped
        her_check = (await two.post(f"{hers}/verify", json=check, headers={"x-latet-token": emily})).json()
        assert her_check["blocking"] == [] and her_check["flagged"]  # hers left already, here as on the other instance


async def test_another_identity_reads_nothing_of_someone_elses_history_on_another_instance(instances, upstream):
    """``HistoryStore.detail``, ``clear`` and ``SentBlocks.first`` (R-26): the same request from two people names the
    same conversation, and each keeps what it sent in their own."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        await _share(one, admin, upstream)
        _, emily, _ = await _person(one, admin, "Emily Carter")
        _, daniel, _ = await _person(two, admin, "Daniel Okafor")
        await _sign_in(one, emily)
        await _sign_in(two, daniel)
        said = [
            {"role": "user", "content": f"write to {EMILY_MAIL} about the invoice"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "thanks, that is all"},  # no value in it: the same block, masked, for anyone
        ]

        async def asked(client, secret):
            answer = await client.post(f"/u/{route_key(secret)}/llm/v1/chat/completions", headers={"authorization": "Bearer sk-test"},
                                       json={"model": "m", "messages": said})  # fmt: skip
            assert answer.status_code == 200

        await asked(one, emily)
        [hers] = await _exchanges(one, 1)
        assert hers["masked"] == 1 and hers["new_masked"] == 1
        assert (await two.get(f"{API}/exchanges/{hers['id']}")).status_code == 404  # not his to read
        await asked(two, daniel)  # the same opening: the same conversation's name, in his own history
        await asked(two, daniel)
        second, first = await _exchanges(two, 2)
        assert (first["masked"], first["new_masked"]) == (1, 1)  # new in his conversation, whoever sent it first
        assert (second["masked"], second["new_masked"]) == (1, 0)  # sent before in his own
        for exchange, new in ((first, True), (second, False)):  # the block with no value: as new as his own sending
            slots = (await two.get(f"{API}/exchanges/{exchange['id']}")).json()["request"]["slots"]
            assert [s["new"] for s in slots if s["masked"] == "thanks, that is all"] == [new]
        assert (await two.request("DELETE", f"{API}/exchanges", headers=UI)).json() == {"ok": True}
        assert (await two.get(f"{API}/exchanges")).json()["items"] == []
        assert [item["id"] for item in (await one.get(f"{API}/exchanges")).json()["items"]] == [hers["id"]]
        assert (await one.get(f"{API}/exchanges/{hers['id']}")).status_code == 200  # hers, as she left it


async def test_another_identity_sees_nothing_of_someone_elses_live_reviewer_on_another_instance(instances, upstream):
    """``ApprovalsBackend.get``, ``answer``, ``waiting`` and the feed (R-26): Emily's batch, put through one instance,
    is no batch of Daniel's on the other, whose dashboard, live, hears nothing of it, while hers there does; each has
    the Live Reviewer on. (A batch with an image needs the text recognition's models, which a test's server has not:
    ``image`` with a second identity is Task 10's backend test.)"""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _started(a, b) as (app_one, app_two), _client(app_one) as one, _client(app_two) as two:
        await _share(one, admin, upstream)
        emily_id, emily, _ = await _person(one, admin, "Emily Carter")
        daniel_id, daniel, _ = await _person(two, admin, "Daniel Okafor")
        await _sign_in(one, emily)
        await _sign_in(two, daniel)
        await _live_reviewer_on(one, emily_id, a, b)
        await _live_reviewer_on(two, daniel_id, a, b)
        hers_read, hers_task = await _stream(app_two, one.cookies["latet_session"])
        his_read, his_task = await _stream(app_two, two.cookies["latet_session"])
        hers, his = f"/u/{route_key(emily)}{ENGINE}", f"/u/{route_key(daniel)}{ENGINE}"
        sent = {"session": "s1", "items": [{"text": f"write to {EMILY_MAIL}", "kind": "user_text"}]}
        pending = (await one.post(f"{hers}/mask", json=sent, headers={"x-latet-token": emily})).json()["pending"]
        await until(lambda: pending in hers_read[0])  # her dashboard on the other instance hears of it
        assert (await two.get(f"{API}/approvals")).json()["batches"] == []
        assert (await two.post(f"{API}/approvals/{pending}", json={"action": "approve"}, headers=UI)).status_code == 409
        theirs = await two.post(f"{his}/approvals/wait", json={"id": pending}, headers={"x-latet-token": daniel})
        assert theirs.status_code == 404  # nothing of his waits with that id
        [still] = (await one.get(f"{API}/approvals")).json()["batches"]
        assert still["id"] == pending  # Daniel decided nothing of hers
        waiting = asyncio.ensure_future(
            two.post(f"{hers}/approvals/wait", json={"id": pending}, headers={"x-latet-token": emily})
        )
        decided = await one.post(f"{API}/approvals/{pending}", json={"action": "approve"}, headers=UI)
        assert decided.status_code == 200
        answer = (await asyncio.wait_for(waiting, 10)).json()
        assert EMILY_MAIL not in answer["items"][0]["text"]  # her plugin's answer, on the other instance
        await asyncio.sleep(0.2)  # what the bus would still bring
        for task in (hers_task, his_task):
            task.cancel()
            await task
    assert EMILY_MAIL in hers_read[0]  # her own text, shown to her to decide on
    assert "event: snapshot" in his_read[0]  # his stream was live: its silence is not a stream that failed
    assert pending not in his_read[0] and "event: pending" not in his_read[0] and EMILY_MAIL not in his_read[0]


async def test_another_identity_holds_no_aws_session_of_someone_elses_on_another_instance(instances):
    """``AwsSessions.session`` (R-26): the credentials Emily's `latet claude` hands her space on one instance are hers
    on every instance, and nobody else's on any."""
    a, b = instances
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    async with _serving(a, b) as (one, two):
        _, emily, _ = await _person(one, admin, "Emily Carter")
        _, daniel, _ = await _person(two, admin, "Daniel Okafor")
        hers, his = f"/u/{route_key(emily)}{ENGINE}", f"/u/{route_key(daniel)}{ENGINE}"
        credentials = {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
        made = await one.post(f"{hers}/aws-sessions", json=credentials, headers={"x-latet-token": emily})
        session = made.json()["id"]
        # A session it found would be held open (a line every few seconds): nothing to wait for if it finds none.
        held = await asyncio.wait_for(
            two.get(f"{his}/aws-sessions/{session}/hold", headers={"x-latet-token": daniel}), 5
        )
        assert held.status_code == 404
        dropped = await two.delete(f"{his}/aws-sessions/{session}", headers={"x-latet-token": daniel})
        assert dropped.json() == {"dropped": False}
        mine = await two.delete(f"{hers}/aws-sessions/{session}", headers={"x-latet-token": emily})
        assert mine.json() == {"dropped": True}  # hers, on the other instance, until she drops it


# --- what the instances log (Task 13's hand-offs) ---------------------------------------------------------------------


@pytest.mark.parametrize("format", ["json", "text"])
async def test_the_logs_hold_no_secret_at_any_level_over_http_or_a_websocket(instances, upstream, format, prefix):
    """What two instances log while a person and a program work through them, at the debug level and through uvicorn
    as `latet server run` serves them: no token, key, route key, one-time code, administration token, provider key,
    client's address or value, in any of the forms a log could write them, nor from a path written another way (N8);
    a WebSocket has RequestLog's line and not uvicorn's, which names the client and the query (N1); and a library's
    debug lines, a handshake's headers or a frame's text, are not written (N2)."""
    a, b = instances
    administrator = a.admin.issue("first administrator")
    admin, provider, address = {"x-latet-admin": administrator}, "sk-provider-0123456789abcdef", "10.20.30.40"
    proxied = {"host": "latet.acme.lan", "x-forwarded-proto": "https", "x-forwarded-for": address}
    asked = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMILY_MAIL} about the invoice"}]}
    content = [{"type": "input_text", "text": f"mail {EMILY_MAIL}"}]
    frame = {
        "type": "response.create",
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": content}],
    }
    served_a, served_b = _on_uvicorn(create_server_app(a)), _on_uvicorn(create_server_app(b))
    with captured_logs(format, level="debug") as logs, served_a as first, served_b as second:
        ends = (f"http://127.0.0.1:{first}", f"http://127.0.0.1:{second}")
        async with (
            httpx.AsyncClient(base_url=ends[0], headers=proxied, trust_env=False) as one,
            httpx.AsyncClient(base_url=ends[1], headers=proxied, trust_env=False) as two,
        ):
            await _share(one, admin, upstream)
            _, emily, enrolment = await _person(one, admin, "Emily Carter")
            made = await two.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin)
            key = made.json()["key"]
            link = (await one.post(f"{SERVER}/login-code", headers=_cli(emily))).json()["url"]
            code = link.partition("code=")[2]
            assert (await two.get(link.removeprefix(URL), follow_redirects=False)).status_code == 303
            # Her dashboard, through uvicorn: its cookie is Secure, which httpx keeps from plain http, so it goes by hand.
            session = two.cookies["latet_session"]
            dashboard = {"cookie": f"latet_session={session}", "x-latet-ui": "1"}
            assert (await two.get(f"{API}/exchanges", headers=dashboard)).status_code == 200
            folder = "/home/emily/billing-api"
            project = (await two.post(f"{API}/projects", json={"path": folder}, headers=dashboard)).json()["id"]
            etag = (await two.get(f"{API}/config", headers=dashboard)).json()["etag"]  # the page's (E0c's Task 11)
            terms = {"terms": [{"value": "Orion", "type": "PROJECT"}], "etag": etag}
            assert (
                await two.put(f"{API}/projects/{project}/personal", json=terms, headers=dashboard)
            ).status_code == 200
            for client, secret in ((one, emily), (two, key)):
                path = f"/u/{route_key(secret)}/llm/v1/chat/completions?key={secret}&api-key={provider}"
                answer = await client.post(path, json=asked, headers={"authorization": f"Bearer {provider}"})
                assert answer.status_code == 200 and EMILY_MAIL in answer.text
            route = route_key(emily)
            written = (f"/U/{route.upper()}/llm/v1/models", f"/u%2F{route}/llm/v1/models", f"//u//{route}/llm",
                       f"/u;x/{route}/llm", f"/x/{route}/y")  # fmt: skip
            for path in written:
                assert (await two.get(ends[1] + path)).status_code in (403, 404)
            where = f"ws://latet.acme.lan/u/{route}/llm/backend-api/codex/responses?api-key={provider}&token={emily}"
            headers = {**proxied, "authorization": f"Bearer {provider}", "session-id": "ws-1"}
            del headers["host"]  # the address's own
            async with connect(where, host="127.0.0.1", port=second, proxy=None, additional_headers=headers) as ws:
                await ws.send(json.dumps(frame))
                said = []
                while (event := json.loads(await ws.recv()))["type"] != "response.completed":
                    said.append(event.get("delta") or "")
            assert "".join(said).startswith(f"You said: mail {EMILY_MAIL}")
    assert EMILY_MAIL not in upstream.all_received()
    lines = logs.getvalue()
    assert "WS /u/***/llm/backend-api/codex/responses 101" in lines  # RequestLog's line for the WebSocket
    assert "GET /_latet/api/v1/exchanges 200" in lines and "PUT /_latet/api/v1/projects/***/personal 200" in lines
    assert "[accepted]" not in lines and "WebSocket /" not in lines  # uvicorn's own, with the client and the query
    below = {(level, name) for level, name in _records(lines, format) if level in ("DEBUG", "INFO")}
    assert below and all(name.startswith(("latet.", "uvicorn")) for _, name in below)  # no library's below WARNING
    # The identities' names too, and a value the vault keeps (her email, masked): S8, area 3 M8.
    secrets = (emily, KEYS[emily], key, enrolment, code, session, project, folder, administrator, provider, EMILY_MAIL)
    _held_nothing(lines, *secrets, address, "Emily Carter", "Billing CI")
    # S13 (area 5, Minor 11): the whole server's stores after the same flows, as a reader of each sees them. PostgreSQL
    # keeps in clear, by design, the identities' names (S12) and the address the administration log records of a call.
    _held_nothing(dump(a.db).decode("latin-1"), *secrets)
    _held_nothing(valkey_dump(a.bus, prefix).decode("latin-1"), *secrets, address, "Emily Carter", "Billing CI")


def _records(lines, format):
    """The level and the logger of each record in ``lines``, as configure_logging writes them."""
    if format == "json":
        return [(r["level"], r["logger"]) for r in map(json.loads, lines.splitlines())]
    return [(found[2], found[3].rstrip(":")) for line in lines.splitlines() if len(found := line.split(" ", 4)) > 4]
