"""A Latet server as E0c operates it, served as `latet server run` serves it (``server_app``). Task 9: a person's
dashboard names the server and what is set for everyone; a computer makes its two calls (a destination its agent names,
the user name's fake) through its identity's space with its own token, a person's alone, one at a time and thirty a
minute for each identity, done in the server's work threads, by POST alone; the space gets the address a request came
by; nothing answered under an identity's address is kept on the way. Task 10: the metrics count what a person's
computer has masked and see PostgreSQL and Valkey, on a socket of their own, whose path the instance's own port answers
as any path it does not have."""

import asyncio
import contextlib
import ipaddress
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from latet import observe
from latet.config import Config, Destination, TermConfig
from latet.runtime import Runtime, digest
from latet.server import app as app_module
from latet.server import egress
from latet.server.logs import REFUSED
from latet.server.operations import server_app
from latet.server.tokens import route_key

from .sharing import share_rules
from .support import open_test_server, until

pytestmark = pytest.mark.server

MACHINE = "/_latet/server/v1"
ADMIN = f"{MACHINE}/admin"
API = "/_latet/api/v1"
ENGINE = "/_latet/engine/v1"
UI = {"x-latet-ui": "1"}
OUTSIDE = ipaddress.ip_address("1.2.3.4")  # an address on the internet (the documentation ranges are refused)
INSIDE = ipaddress.ip_address("10.0.3.9")
W = "billing@acme.com"  # a value the administrator lets through for everyone: it waits for each person
SMALL = {"model": "claude", "max_tokens": 8, "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture(autouse=True)
def _egress(monkeypatch):
    """Each test its own lookups under way; no test resolves ipv4only.arpa nor any name with the system's resolver
    (pytest-socket does not stop it): a name resolves to an address on the internet, but where a test says otherwise."""
    monkeypatch.setattr(egress, "_LOOKUPS", egress._Lookups())
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))
    monkeypatch.setattr(egress, "resolve", lambda host, port: [OUTSIDE])


def test_a_persons_dashboard_on_the_server_names_the_server_and_no_rule_for_everyone(server):
    """A rule an earlier version kept among the shared settings is applied by no space and named by no dashboard: the
    Rules page shows her own rules alone (rules for everyone are the Enterprise Edition's)."""
    server.shared.set(Config(terms=[TermConfig(value="Project Falcon", type="PROJECT")]), actor="test")
    _, code = server.identities.add_person("Emily Carter")
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        enrolled = client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"}).json()
        cli = {"x-latet-token": enrolled["token"], "x-latet-computer-key": enrolled["computer_key"]}
        link = client.post(f"{MACHINE}/login-code", headers=cli).json()["url"]
        client.get(link.removeprefix(server.settings.url), follow_redirects=False)
        me = client.get("/_latet/api/v1/me").json()
        config = client.get("/_latet/api/v1/config").json()
    assert me["server"]["url"] == server.settings.url and me["server"]["identity"]["name"] == "Emily Carter"
    assert "shared" not in config and config["terms"] == []


def test_a_computer_names_a_destination_and_asks_the_user_names_fake_through_its_space(server):
    _, code = server.identities.add_person("Emily Carter")
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        token = client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"}).json()["token"]
        space, mine = f"/u/{route_key(token)}", {"x-latet-token": token}
        wanted = {"destinations": [{"url": "https://llm.acme.com", "agent": "claude"}]}
        assert client.post(f"{space}/_latet/admin/destinations", json=wanted, headers=mine).json()["names"]
        named = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
        assert named.json() == {"surrogate": None}
        stranger = {"x-latet-token": "latet_pat_" + "x" * 43}
        refused = client.post(f"{space}/_latet/admin/destinations", json=wanted, headers=stranger)
        assert refused.status_code in (401, 403, 404)
        for path in ("/_latet/admin/reload", "/_latet/admin/login-code"):
            assert client.post(f"{space}{path}", headers=mine).status_code == 404, path


def test_the_metrics_count_a_persons_masked_values_and_see_the_database_and_valkey(server, tmp_path):
    server.settings = server.settings.model_copy(update={"metrics_token_file": _metrics_token(tmp_path)})
    _, code = server.identities.add_person("Emily Carter")
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        secret = client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"}).json()["token"]
        mine = {"x-latet-token": secret}
        items = [{"text": "deploy with ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i", "kind": "user_text"}]
        client.post(
            f"/u/{route_key(secret)}/_latet/engine/v1/mask", json={"session": "s", "items": items}, headers=mine
        )
        text = _scraped(client, "latet_vault_near_limit 0")
    assert 'latet_masking_values_total{type="GITHUB_PAT"}' in text
    assert "latet_database_up 1" in text and "latet_valkey_up 1" in text and "latet_spaces_open 1" in text
    assert (
        "latet_bus_rejected 0" in text and "latet_vault_near_limit 0" in text and "latet_identities_enabled 1" in text
    )
    assert "latet_audit_chain_known" in text and "latet_credentials_waiting" in text
    assert 'latet_http_requests_total{kind="plugin",code="200"} 1' in text
    assert route_key(secret) not in text and secret not in text and "R7tK2pQ9" not in text
    assert "latet_build_info" in server.metrics.render()  # the server's own registry, served or not


def test_the_instances_own_port_answers_the_metrics_path_as_any_path_it_does_not_have(server, tmp_path):
    server.settings = server.settings.model_copy(update={"metrics_token_file": _metrics_token(tmp_path)})
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        own = client.get("/_latet/metrics", headers=SCRAPED)
        unknown = client.get("/_latet/no-such-path", headers=SCRAPED)
        by_address = client.get("http://10.0.5.10:5283/_latet/metrics", headers=SCRAPED)  # past a load balancer
        on_its_socket = client.get(f"{METRICS_SOCKET}/_latet/metrics", headers=SCRAPED)
        health_there = client.get(f"{METRICS_SOCKET}/_latet/health")
    assert own.status_code == unknown.status_code == 404 and own.content == unknown.content  # nothing rendered
    assert by_address.status_code == 403 and "latet_" not in own.text + by_address.text  # another Host: the gate's
    assert on_its_socket.status_code == 200 and "latet_build_info" in on_its_socket.text
    assert (health_there.status_code, health_there.json()) == (404, {"error": "there is no such route"})


def test_a_closed_server_hears_no_report_and_renders_no_metrics(schema, prefix):
    found = open_test_server(schema, prefix)
    try:
        server_app(found)
        assert found.metrics in observe._listeners  # its app is made: it hears what the engine and the recorder say
    finally:
        found.close()
    assert found.metrics not in observe._listeners
    with pytest.raises(RuntimeError):  # its threads let go first: no scrape asks a store that is closing
        asyncio.run(found.metrics.scrape())


# --- helpers ----------------------------------------------------------------------------------------------------------

METRICS_SOCKET = "http://127.0.0.1:7283"  # an instance's metrics socket, as Prometheus asks it
SCRAPED = {"authorization": "Bearer metrics-" + "y" * 32}


def _metrics_token(folder):
    token = folder / "metrics.token"
    token.write_text("metrics-" + "y" * 32, "utf-8")
    return token


def _scraped(client, wanted):
    """A scrape once what it reads of PostgreSQL and Valkey is in: the first scrape starts those reads, none waits."""
    for _ in range(40):
        text = client.get(f"{METRICS_SOCKET}/_latet/metrics", headers=SCRAPED).text
        if wanted in text:
            return text
        time.sleep(0.25)  # a text is served again for a second
    raise AssertionError(f"{wanted} never came")


def _admin(server):
    return {"x-latet-admin": server.admin.issue("operations")}


# Each computer's key, by its token, as its enrolment gave it: what a sign-in link and an identity ask for (R16).
KEYS: dict[str, str] = {}


def _enrolled(client, server, name="Emily Carter"):
    """A person and their computer's token: (their id, the token); its key in ``KEYS``."""
    person, code = server.identities.add_person(name)
    enrolled = client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"}).json()
    KEYS[enrolled["token"]] = enrolled["computer_key"]
    return person.id, enrolled["token"]


def _calls(token):
    """Where a computer with ``token`` makes its calls (its identity's space), and its CLI's headers: its token, and
    its key, which the server's sign-in link and identity ask for."""
    keyed = {"x-latet-computer-key": KEYS[token]} if token in KEYS else {}  # an API key has none
    return f"/u/{route_key(token)}", {"x-latet-token": token, **keyed}


def _provider(monkeypatch):
    """The providers every space of the test reaches: a stand-in that records what it gets, never the network."""
    sent: list[httpx.Request] = []

    def answer(request):
        sent.append(request)
        body = b'{"id": "msg_1", "type": "message", "role": "assistant", "model": "claude", "content": []}'
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(
        "latet.gateway.app.make_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return sent


@contextlib.asynccontextmanager
async def _serving(*servers):
    """Each instance's app, as `latet server run` serves it, started, and a client of each."""
    async with contextlib.AsyncExitStack() as stack:
        clients = []
        for server in servers:
            app = server_app(server)
            await stack.enter_async_context(app.app.router.lifespan_context(app.app))
            transport = httpx.ASGITransport(app=app)
            client = httpx.AsyncClient(transport=transport, base_url=server.settings.url, trust_env=False)
            clients.append(await stack.enter_async_context(client))
        yield clients


async def _person(client, admin, name="Emily Carter"):
    """A person the administration makes, and their computer's token: (their id, the token)."""
    made = (await client.post(f"{ADMIN}/identities", json={"kind": "person", "name": name}, headers=admin)).json()
    enrolled = await client.post(f"{MACHINE}/enrol", json={"code": made["code"], "label": "laptop"})
    KEYS[enrolled.json()["token"]] = enrolled.json()["computer_key"]
    return made["identity"]["id"], enrolled.json()["token"]


async def _sign_in(client, token, url):
    link = (await client.post(f"{MACHINE}/login-code", headers=_calls(token)[1])).json()["url"]
    assert (await client.get(link.removeprefix(url), follow_redirects=False)).status_code == 303


async def _asgi(app, path, headers=(), method="POST", body=b""):
    """The status, and the scope, of a request through ``app`` as a client's bytes make it."""
    sent, waiting = [], [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if waiting:
            return waiting.pop()
        await asyncio.Event().wait()  # the client stays: it goes only when the request is cancelled
        raise AssertionError("unreachable")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan"), *headers], "client": ("10.0.3.9", 50000),
        "server": ("latet.acme.lan", 443),
    }  # fmt: skip
    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), scope


# --- an API key has no computer (T9.12) -------------------------------------------------------------------------------


def test_an_api_key_makes_neither_of_the_computers_calls_and_its_settings_stay_its_administrators(server):
    admin = _admin(server)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        path = f"{ADMIN}/identities/{key['identity']['id']}/settings"
        read = client.get(path, headers=admin).json()
        # Its administrator wants the user name's fake: a name the key sent would be interned without the refusal.
        review = {"user_name": "surrogate", "paths": "block", "host_name": "block", "git_author": "block"}
        wants = {**read["settings"], "auto_mode_review": review, "etag": read["etag"]}
        assert client.put(path, json=wants, headers=admin).status_code == 200
        before = client.get(path, headers=admin).json()
        space, theirs = _calls(key["key"])
        calls = {
            "/_latet/admin/user-name": {"name": "billing-ci"},
            "/_latet/admin/destinations": {"destinations": [{"url": "https://collector.example.net"}]},
        }
        for call, body in calls.items():
            refused = client.post(f"{space}{call}", json=body, headers=theirs)
            assert refused.status_code == 403 and "an API key has no computer" in refused.json()["error"], call
        assert client.get(path, headers=admin).json() == before
    with server.db.connection() as conn:
        kept = conn.execute("SELECT count(*) FROM vault_entities WHERE identity_id = %s", (key["identity"]["id"],))
        assert kept.fetchone()[0] == 0


async def test_the_gates_refusals_of_a_computers_call_are_its_own_and_made_before_the_space_is_asked(
    server, monkeypatch
):
    """Marked as the server's own (``REFUSED``): the log paces what anyone can make happen at will."""
    monkeypatch.setattr(app_module, "COMPUTER_CALL_LIMIT", 1)
    asked: list[str] = []
    real = server.spaces.app

    async def spied(identity):
        asked.append(identity.kind)
        return await real(identity)

    monkeypatch.setattr(server.spaces, "app", spied)
    app = server_app(server)
    async with app.app.router.lifespan_context(app.app):
        _, code = await asyncio.to_thread(server.identities.add_person, "Emily Carter")
        _, token, _ = await asyncio.to_thread(server.identities.enrol, code, "laptop")
        _, secret = await asyncio.to_thread(server.identities.add_key, "Billing CI")
        body, typed = b'{"name": "emily"}', (b"content-type", b"application/json")
        theirs, mine = [(b"x-latet-token", secret.encode()), typed], [(b"x-latet-token", token.encode()), typed]
        key = await _asgi(app, f"/u/{route_key(secret)}/_latet/admin/user-name", theirs, body=body)
        first = await _asgi(app, f"/u/{route_key(token)}/_latet/admin/user-name", mine, body=body)
        past = await _asgi(app, f"/u/{route_key(token)}/_latet/admin/user-name", mine, body=body)
    assert [(status, scope.get(REFUSED)) for status, scope in (key, first, past)] == [
        (403, True),
        (200, None),
        (429, True),
    ]
    assert asked == ["person"]  # neither refusal asked a space


# --- one at a time, thirty a minute, in the work threads (T9.13) ------------------------------------------------------


async def test_two_calls_of_one_identitys_computer_at_once_give_one_answer_and_one_wait(server, monkeypatch):
    held, started = threading.Event(), threading.Event()
    threads: list[str] = []
    add = Runtime.add_destinations

    def holding(self, wanted, *, judged=None):
        threads.append(threading.current_thread().name)
        started.set()
        held.wait(10)
        return add(self, wanted, judged=judged)

    monkeypatch.setattr(Runtime, "add_destinations", holding)
    admin = _admin(server)
    async with _serving(server) as (client,):
        _, token = await _person(client, admin)
        space, mine = _calls(token)
        wanted = {"destinations": [{"url": "https://gw.acme.com"}]}
        first = asyncio.create_task(client.post(f"{space}/_latet/admin/destinations", json=wanted, headers=mine))
        await until(started.is_set, seconds=30)  # the first is in its work, its space built
        second = await client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
        held.set()
        answered = await first
        again = await client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
    assert (second.status_code, second.headers["retry-after"]) == (429, "1") and "already" in second.json()["error"]
    assert answered.json() == {"names": ["gw-acme-com"]} and again.status_code == 200
    assert threads and all(name.startswith("latet-work") for name in threads)


def test_past_its_calls_a_minute_an_identitys_computer_is_told_to_wait_a_minute(server, monkeypatch):
    monkeypatch.setattr(app_module, "COMPUTER_CALL_LIMIT", 2)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        _, other = _enrolled(client, server, "Daniel Okafor")
        space, mine = _calls(token)
        asked = [client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine) for _ in range(3)]
        theirs, them = _calls(other)  # another identity's computer counts apart
        others = client.post(f"{theirs}/_latet/admin/user-name", json={"name": "daniel"}, headers=them)
    assert [a.status_code for a in asked] == [200, 200, 429] and asked[-1].headers["retry-after"] == "60"
    assert others.status_code == 200


def test_past_its_calls_a_minute_an_identitys_computer_is_refused_with_no_thread_and_no_round_trip(server, monkeypatch):
    """Once Valkey's limiter says an identity's computer is past its calls for the window, this instance remembers it
    until the window ends: its next calls are refused on the event loop, no credentials' thread taken, no round trip."""
    monkeypatch.setattr(app_module, "COMPUTER_CALL_LIMIT", 2)
    asked: list[str] = []
    slot = server.bus.slot

    def counted(key, limit, window):
        asked.append(key)
        return slot(key, limit, window)

    monkeypatch.setattr(server.bus, "slot", counted)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)
        answers = [client.post(f"{space}/_latet/admin/user-name", json={"name": "e"}, headers=mine) for _ in range(5)]
    assert [a.status_code for a in answers] == [200, 200, 429, 429, 429]
    assert all(a.headers["retry-after"] == "60" for a in answers[2:])
    assert len([key for key in asked if key.startswith("computer-call:")]) == 3  # the last two asked Valkey nothing


def test_a_computers_call_valkey_cannot_count_is_refused_with_when_to_try_again(server, monkeypatch):
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)

        def unanswered(*args, **kwargs):
            raise ConnectionError("Valkey did not answer")

        with monkeypatch.context() as patched:
            patched.setattr(server.bus, "slot", unanswered)
            refused = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
        answered = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
    assert refused.status_code == 503 and refused.headers["retry-after"] == "1" and "emily" not in refused.text
    assert answered.status_code == 200  # the mark of the call refused was let go


async def test_fifty_calls_of_one_identity_at_once_take_no_thread_of_the_default_executor(server, monkeypatch):
    admin = _admin(server)
    async with _serving(server) as (client,):
        _, token = await _person(client, admin)
        space, mine = _calls(token)
        name = {"name": "emily"}
        assert (await client.post(f"{space}/_latet/admin/user-name", json=name, headers=mine)).status_code == 200
        taken: list[object] = []

        async def failing(work, *args, **kwargs):
            taken.append(work)
            raise AssertionError("a computer's call took a thread of the default executor")

        with monkeypatch.context() as patched:
            patched.setattr(asyncio, "to_thread", failing)
            answers = await asyncio.gather(
                *(client.post(f"{space}/_latet/admin/user-name", json=name, headers=mine) for _ in range(50))
            )
    assert taken == [] and {a.status_code for a in answers} <= {200, 429}
    assert any(a.status_code == 200 for a in answers) and any(a.status_code == 429 for a in answers)


# --- by POST alone; nothing kept on the way (T9.5, T9.20) -------------------------------------------------------------


def test_the_two_calls_by_any_other_method_or_as_a_websocket_reach_nothing(server, monkeypatch):
    sent = _provider(monkeypatch)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)
        for call in ("/_latet/admin/destinations", "/_latet/admin/user-name"):
            for method in ("GET", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
                assert client.request(method, f"{space}{call}", headers=mine).status_code == 404, (method, call)
            handshake = {**mine, "host": "latet.acme.lan"}
            with (
                pytest.raises(WebSocketDisconnect) as closed,
                client.websocket_connect(f"{space}{call}", headers=handshake),
            ):
                pass
            assert closed.value.code == 4404
    assert sent == []


def test_every_answer_under_an_identitys_address_says_no_store(server, monkeypatch):
    sent = _provider(monkeypatch)
    server.shared.set(Config(upstreams=[Destination(name="llm", url="https://llm.acme.com")]), actor="test")
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)
        known = {"destinations": [{"url": "https://llm.acme.com"}]}
        answers = {
            "the bridge": client.get(f"{space}{ENGINE}/ping", headers=mine),
            "the user name": client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine),
            "a destination": client.post(f"{space}/_latet/admin/destinations", json=known, headers=mine),
            "a model's answer": client.post(
                f"{space}/anthropic/v1/messages", json=SMALL, headers={**mine, "x-api-key": "sk-test"}
            ),
            "the gate's 404": client.get(f"{space}/_latet/admin/user-name", headers=mine),
            "the gate's 401": client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}),
        }
    for what, answer in answers.items():
        assert answer.headers.get("cache-control") == "no-store", what
    assert answers["a model's answer"].status_code == 200 and sent


# --- the address a request came by (T9.2, T9.21) ----------------------------------------------------------------------


def test_the_space_gets_the_address_a_request_came_by_and_the_route_check_names_no_address(server, monkeypatch):
    seen: list[tuple[str, object]] = []
    real = server.spaces.app

    async def spied(identity):
        app = await real(identity)

        async def handed(scope, receive, send):
            seen.append((scope["path"], scope.get("latet.space")))
            await app(scope, receive, send)

        return handed

    monkeypatch.setattr(server.spaces, "app", spied)
    url = server.settings.url
    with TestClient(server_app(server), base_url=url) as client:
        person, token = _enrolled(client, server)
        _, other = _enrolled(client, server, "Daniel Okafor")
        route, theirs = route_key(token), route_key(other)
        space, mine = _calls(token)
        answers = []
        for bridge in (f"{url}/u/{route}", f"{url}/u/{theirs}"):  # her own address, and another of the same server
            facts = {"bridge": bridge, "session": "s1", "env": {"ANTHROPIC_BASE_URL": f"{bridge}/anthropic"},
                     "sources": {}, "entrypoint": "cli", "surface": "terminal"}  # fmt: skip
            answers.append(client.post(f"{space}{ENGINE}/route-check", json=facts, headers=mine))
        assert [answer.status_code for answer in answers] == [200, 200]
        recorder = server.spaces._spaces[person].runtime.recorder
        assert recorder is not None and recorder.drain(timeout=10), "route-check history did not finish recording"
        link = client.post(f"{MACHINE}/login-code", headers=mine).json()["url"]
        client.get(link.removeprefix(url), follow_redirects=False)
        history = client.get(f"{API}/exchanges").json()
        details = [client.get(f"{API}/exchanges/{item['id']}").text for item in history["items"]]
    # Set by the gate for a request under her address, which no client writes; the dashboard's API gets none.
    assert {space_ for path, space_ in seen if path.startswith(ENGINE)} == {f"{url}/u/{route}"}
    assert {space_ for path, space_ in seen if path.startswith(API)} == {None}
    assert answers[1].json()["ok"] is False  # another address of the server is never Latet's for her
    assert history["items"] and details
    for text in (*(a.text for a in answers), str(history), *details):
        assert route not in text and theirs not in text


# --- what a computer names, judged; what waits, kept waiting (T9.3, T9.4, T9.14) --------------------------------------


def test_a_destination_a_computer_names_is_judged_where_it_leads_and_a_shared_one_is_named(server, monkeypatch):
    server.shared.set(Config(upstreams=[Destination(name="llm", url="https://llm.acme.com")]), actor="test")
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)
        call = f"{space}/_latet/admin/destinations"
        monkeypatch.setattr(egress, "resolve", lambda host, port: [INSIDE])
        inside = client.post(call, json={"destinations": [{"url": "https://gw.acme.com"}]}, headers=mine)
        itself = client.post(call, json={"destinations": [{"url": f"{server.settings.url}/v1"}]}, headers=mine)
        shared = client.post(call, json={"destinations": [{"url": "https://llm.acme.com/"}]}, headers=mine)
        big = client.post(call, json={"destinations": [{"url": "https://gw.acme.com/" + "x" * 70_000}]}, headers=mine)
    assert inside.status_code == 400 and "own network" in inside.json()["error"] and "10.0.3.9" not in inside.text
    assert itself.status_code == 400 and "server itself" in itself.json()["error"]
    assert shared.json() == {"names": ["llm"]} and big.status_code == 413


def test_the_servers_own_user_name_is_never_read_and_the_computers_is_interned_when_wanted(server, monkeypatch):
    def nobodys():
        raise AssertionError("the server's own user name is nobody's")

    monkeypatch.setattr("latet.config.os_user_name", nobodys)
    with TestClient(server_app(server), base_url=server.settings.url) as client:
        _, token = _enrolled(client, server)
        space, mine = _calls(token)
        kept = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine).json()
        big = client.post(f"{space}/_latet/admin/user-name", json={"name": "x" * 20_000}, headers=mine)
        refused = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily", "x": 1}, headers=mine)
        link = client.post(f"{MACHINE}/login-code", headers=mine).json()["url"]
        client.get(link.removeprefix(server.settings.url), follow_redirects=False)
        view = client.get(f"{API}/config").json()
        review = {**view["auto_mode_review"], "user_name": "surrogate"}
        wanted = {"auto_mode_review": review, "etag": view["etag"]}  # the page's etag (Task 11)
        assert client.put(f"{API}/config", json=wanted, headers=UI).status_code == 200
        fake = client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine).json()
    assert kept == {"surrogate": None} and big.status_code == 413 and refused.status_code == 400
    assert fake["surrogate"] and fake["surrogate"] != "emily"


async def test_her_save_while_an_administrators_loosening_waits_applies_hers_and_never_the_loosening(server):
    share_rules(server)  # a value let through for everyone is a rule for everyone: an edition that shares them
    admin = _admin(server)
    async with _serving(server) as (client,):
        person, token = await _person(client, admin)
        space, mine = _calls(token)
        await _sign_in(client, token, server.settings.url)
        assert (
            await client.post(f"{space}/_latet/admin/user-name", json={"name": "e"}, headers=mine)
        ).status_code == 200
        runtime = server.spaces._spaces[person].runtime
        await asyncio.to_thread(server.shared.set, Config(allow_values=[W]), actor="test")
        await until(lambda: bool(runtime.mediator.approvals.waiting_changes()), seconds=30)
        view = (await client.get(f"{API}/config")).json()
        assert W not in view["allow_values"]
        mercury = {"terms": [*view["terms"], {"value": "Mercury Labs", "type": "CLIENT"}], "etag": view["etag"]}
        saved = await client.put(f"{API}/config", json=mercury, headers=UI)
        stale = await client.put(f"{API}/config", json={"allow_values": [W], "etag": view["etag"]}, headers=UI)
        fresh = (await client.get(f"{API}/config")).json()["etag"]
        named = await client.put(f"{API}/config", json={"allow_values": [W], "etag": fresh}, headers=UI)
        approved = await asyncio.to_thread(runtime.reviewed)
    assert saved.status_code == 200 and "Mercury Labs" in [t.value for t in runtime.config.terms]
    assert stale.status_code == 409 and stale.json()["reason"] == "settings_stale"  # read before her own save
    assert named.status_code == 200  # named by the save, and still not the save's: it waits
    assert W not in runtime.config.allow_values and approved is not None and W not in approved.allow_values
    [change] = runtime.mediator.approvals.waiting_changes()
    assert W in change.config.allow_values and "Mercury Labs" in [t.value for t in change.config.terms]


async def test_a_destination_added_while_a_change_waits_keeps_it_waiting_on_both_instances_and_its_approval_keeps_it(
    instances,
):
    a, b = instances
    admin = _admin(a)
    async with _serving(a, b) as (here, there):
        person, token = await _person(here, admin)
        space, mine = _calls(token)
        for client in (here, there):  # her space on both instances
            built = await client.post(f"{space}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
            assert built.status_code == 200
        ours, theirs = a.spaces._spaces[person].runtime, b.spaces._spaces[person].runtime
        await asyncio.to_thread(a.shared.set, Config(ocr="off"), actor="test")  # images' text no longer read
        for runtime in (ours, theirs):  # what lowers protection waits for her on each instance
            await until(lambda runtime=runtime: bool(runtime.mediator.approvals.waiting_changes()), seconds=30)
        wanted = {"destinations": [{"url": "https://gw.acme.com"}]}
        named = await here.post(f"{space}/_latet/admin/destinations", json=wanted, headers=mine)
        assert named.json() == {"names": ["gw-acme-com"]}
        await until(lambda: "gw-acme-com" in [d.name for d in theirs.config.upstreams], seconds=30)
        sent = {"usernames": ["emily"], "hostname": "emily-laptop"}
        assert (await there.put(f"{MACHINE}/identity", json=sent, headers=mine)).status_code == 200

        def asked_again(runtime) -> bool:  # the change waiting there holds what waits and what she added meanwhile
            return any(
                c.config.ocr == "off" and "gw-acme-com" in [d.name for d in c.config.upstreams]
                for c in runtime.mediator.approvals.waiting_changes()
            )

        for runtime in (ours, theirs):  # each instance hears the other's change in a moment
            await until(lambda runtime=runtime: runtime.config.identity.hostname == "emily-laptop", seconds=30)
            assert "gw-acme-com" in [d.name for d in runtime.config.upstreams] and runtime.config.ocr == "auto"
            await until(lambda runtime=runtime: asked_again(runtime), seconds=30)
        await _sign_in(there, token, a.settings.url)
        await until(
            lambda: any(c.base == digest(theirs.config) for c in theirs.mediator.approvals.waiting_changes()),
            seconds=30,
        )
        [change] = [c for c in theirs.mediator.approvals.waiting_changes() if c.base == digest(theirs.config)]
        decided = await there.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI)
        assert decided.json() == {"ok": True, "state": "approved"}
        for runtime in (ours, theirs):
            await until(lambda runtime=runtime: runtime.config.ocr == "off", seconds=30)
            assert "gw-acme-com" in [d.name for d in runtime.config.upstreams]
            assert runtime.config.identity.hostname == "emily-laptop"
