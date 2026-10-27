"""An API key's space keeps its newest values in memory and finds every other one in PostgreSQL (``StoredVault``, R7):
a vault of any size works on every instance, a value found once is masked for ever, wherever and however old, and
unmask restores a fake of any age, made on any instance, before or after a restart. A person's space keeps its whole
vault, as before; a vault kept by an earlier Latet gets its words as its space is built; and the gateway restores a
key's conversation as it did. The working set follows the server's setting (``key_working_values``)."""

import asyncio
import contextlib
import json

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from latet.bridge.api import STORE_UNAVAILABLE
from latet.bridge.apps import APPS_API
from latet.engine.stored import HELD_SECRETS, StoredVault
from latet.gateway.routes import Upstream
from latet.server import vault as vault_module
from latet.server.app import create_server_app
from latet.server.db import Database
from latet.server.settings import ServerSettings, load_server_settings
from latet.server.tokens import route_key

from ..e2e.fake_upstream import FakeUpstream
from .support import DATABASE, URL, VALKEY, captured_logs, close_test_server, listening, open_test_server

pytestmark = pytest.mark.server

WORKING = 100  # the working set of the tests' spaces: the server's setting, below its range, as a test may set it
NAME = "Ferruccio Lanzavecchia"  # a customer's name: found by a rule of the key's, then by its vault alone
FIRST = "laura.bianchi@gmail.com"
WHERE = ("10.0.3.9", 40000)


def _email(n: int) -> str:
    return f"customer.number{n:05d}@gmail.com"  # a mail provider's: no domain of its own to keep


def _working(*servers, values: int = WORKING) -> None:
    """Each instance keeps ``values`` of a key's vault in memory, in the spaces it builds from now on."""
    for server in servers:
        server.settings.key_working_values = values


def _bearer(secret):
    return {"authorization": f"Bearer {secret}"}


@contextlib.asynccontextmanager
async def _calling(*servers):
    """A client for each instance, its ear on Valkey while it lasts, its spaces closed after."""
    async with listening(*servers), contextlib.AsyncExitStack() as stack:
        clients = []
        for server in servers:
            transport = httpx.ASGITransport(app=create_server_app(server), client=WHERE)
            clients.append(await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url=URL)))
        yield clients[0] if len(clients) == 1 else clients


async def _call(client, secret, operation, texts, kind="prose"):
    answer = await client.post(f"{APPS_API}/{operation}", json={"texts": texts, "kind": kind}, headers=_bearer(secret))
    assert answer.status_code == 200, answer.text
    return answer.json()["results"]


async def _masked(client, secret, texts, kind="prose"):
    return [result["text"] for result in await _call(client, secret, "mask", texts, kind)]


async def _others(client, secret, count=500, start=0):
    """``count`` other values masked after the ones before, so that those leave the space's memory."""
    await _masked(client, secret, [f"login by {_email(n)}" for n in range(start, start + count)])


async def _vault(server, key):
    return (await server.spaces.runtime(key)).built.vault


def _held(vault, value):
    return any(e.original == value for e in vault.entities())


async def _teach(server, key, client, secret):
    """The customer's name, found in prose by a rule of the key's own, then the rule removed: the vault keeps the value
    (a value learnt stays masked after its rule is gone). Its fake."""
    runtime = await server.spaces.runtime(key)
    rule = {"name": "customer-name", "type": "CUSTOMER", "pattern": NAME}

    def with_rule(current):
        return current.model_copy(update={"recognizers": [rule]})

    def without(current):
        return current.model_copy(update={"recognizers": []})

    await asyncio.to_thread(runtime.update_config, with_rule)
    [taught] = await _masked(client, secret, [f"call {NAME} about the renewal"])
    assert NAME not in taught
    await asyncio.to_thread(runtime.update_config, without)
    fake = taught.removeprefix("call ").removesuffix(" about the renewal")
    assert fake and fake != NAME
    return fake


async def test_a_keys_space_holds_its_working_set_only(server):
    _working(server)
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        [first] = await _masked(client, secret, [f"write to {FIRST}"])
        await _others(client, secret)
        vault = await _vault(server, key)
        assert isinstance(vault, StoredVault)
        assert vault.count <= WORKING and not _held(vault, FIRST)
        assert await _call(client, secret, "unmask", [first]) == [{"text": f"write to {FIRST}", "restored": 1}]
        # Masked again, out of memory as it was, with the fake it always had.
        assert await _masked(client, secret, [f"write to {FIRST}"]) == [first]


async def test_a_value_found_long_ago_is_masked_on_every_instance(instances):
    """R7: a name a rule of the key found once, the rule gone and five hundred values kept since, in data where no
    detector reads it, on the other instance: masked with its first fake, found in PostgreSQL by its words."""
    a, b = instances
    _working(a, b)
    key, secret = a.identities.add_key("Support bot")
    with captured_logs() as logs:
        async with _calling(a, b) as (first, second):
            fake = await _teach(a, key, first, secret)
            await _others(first, secret)
            assert not _held(await _vault(a, key), NAME)
            assert not _held(await _vault(b, key), NAME)  # built after: only its newest values in memory
            text = f"2026-10-24 10:02:11 ticket 4411 closed, customer {NAME}, rating 5"
            assert await _masked(second, secret, [text], "data") == [text.replace(NAME, fake)]
            assert await _masked(first, secret, [text], "data") == [text.replace(NAME, fake)]
            # Unmask restores it on either.
            back = await _call(second, secret, "unmask", [text.replace(NAME, fake)])
            assert back == [{"text": text, "restored": 1}]
    assert NAME not in logs.getvalue() and "Lanzavecchia" not in logs.getvalue()


async def test_a_persons_space_keeps_its_whole_vault(server):
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        assert not isinstance(await _vault(server, person), StoredVault)
        assert isinstance(await _vault(server, key), StoredVault)


async def test_the_working_set_follows_the_setting(server, monkeypatch):
    _working(server, values=1000)
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        await _others(client, secret, 750)
        await _others(client, secret, 750, start=750)
        vault = await _vault(server, key)
        assert vault.working == 1000 and vault.count <= 1000
    # As a variable of the server: within its range, and refused at start below it.
    monkeypatch.delenv("LATET_SERVER_CONFIG", raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", DATABASE)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_KEY_WORKING_VALUES", "50000")
    assert load_server_settings().key_working_values == 50_000
    monkeypatch.setenv("LATET_SERVER_KEY_WORKING_VALUES", "500")
    with pytest.raises(ValueError, match="key_working_values"):
        load_server_settings()


async def test_the_held_secrets_follow_the_setting(server, monkeypatch):
    """Ruling 10b-R3: the secrets a key's space holds glued for good are as many as the server's key_held_secrets."""
    _working(server)
    server.settings.key_held_secrets = 300
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        assert (await _vault(server, key)).held_at_most == 300
    assert ServerSettings.model_fields["key_held_secrets"].default == HELD_SECRETS == 1_000
    monkeypatch.delenv("LATET_SERVER_CONFIG", raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", DATABASE)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_KEY_HELD_SECRETS", "0")
    assert load_server_settings().key_held_secrets == 0
    monkeypatch.setenv("LATET_SERVER_KEY_HELD_SECRETS", "-1")
    with pytest.raises(ValueError, match="key_held_secrets"):
        load_server_settings()


def test_the_pool_follows_the_setting(server, monkeypatch):
    """Review 10b's (c): the connections of an instance's pool are the server's database_pool (10 when it says
    nothing), and the applications calls that keep their values in a transaction at once three tenths of them."""
    assert ServerSettings.model_fields["database_pool"].default == 10
    assert server.db.pool.max_size == server.settings.database_pool == 10 and server.db.together_at_most == 3
    monkeypatch.delenv("LATET_SERVER_CONFIG", raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", DATABASE)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_DATABASE_POOL", "40")
    settings = load_server_settings()
    assert settings.database_pool == 40
    db = Database(DATABASE, schema=server.db.schema, max_size=settings.database_pool)
    try:
        assert db.pool.max_size == 40 and db.together_at_most == 12
    finally:
        db.close()
    monkeypatch.setenv("LATET_SERVER_DATABASE_POOL", "2")
    with pytest.raises(ValueError, match="database_pool"):
        load_server_settings()


async def test_a_fake_made_on_one_instance_unmasks_on_the_other(instances):
    a, b = instances
    _working(a, b)
    _, secret = a.identities.add_key("Billing CI")
    async with _calling(a, b) as (first, second):
        [masked] = await _masked(first, secret, [f"write to {FIRST}"])
        await _others(first, secret)
        assert await _call(second, secret, "unmask", [masked]) == [{"text": f"write to {FIRST}", "restored": 1}]
        [made_there] = await _masked(second, secret, [f"cc {_email(9999)}"])
        assert await _call(first, secret, "unmask", [made_there]) == [{"text": f"cc {_email(9999)}", "restored": 1}]


async def test_after_a_restart_old_fakes_still_unmask(schema, prefix):
    server = open_test_server(schema, prefix)
    try:
        _working(server)
        _, secret = server.identities.add_key("Billing CI")
        async with _calling(server) as client:
            [masked] = await _masked(client, secret, [f"write to {FIRST}"])
            await _others(client, secret)
    finally:
        close_test_server(server)
    again = open_test_server(schema, prefix)
    try:
        _working(again)
        async with _calling(again) as client:
            assert await _call(client, secret, "unmask", [masked]) == [{"text": f"write to {FIRST}", "restored": 1}]
            assert await _masked(client, secret, [f"write to {FIRST}"]) == [masked]
    finally:
        close_test_server(again)


async def test_a_vault_from_before_gets_its_words(server):
    """Values kept by Latet 0.9 have no words: the space's build gives them theirs, and its known values and its unmask
    find them as they find any other."""
    _working(server)
    key, secret = server.identities.add_key("Support bot")
    async with _calling(server) as client:
        fake = await _teach(server, key, client, secret)
        [masked] = await _masked(client, secret, [f"write to {FIRST}"])
        await _others(client, secret, 150)
        await server.spaces.drop(key.id)
        with server.db.connection() as conn:
            conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (key.id,))  # as 0.9 left them
        text = f"ticket closed, customer {NAME}"
        assert await _masked(client, secret, [text], "data") == [text.replace(NAME, fake)]
        assert await _call(client, secret, "unmask", [masked]) == [{"text": f"write to {FIRST}", "restored": 1}]
        with server.db.connection() as conn:
            entries = conn.execute("SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s", (key.id,)).fetchone()
            words = conn.execute("SELECT COUNT(*) FROM vault_words WHERE identity_id = %s", (key.id,)).fetchone()
        assert words[0] == 2 * entries[0]


async def test_a_store_that_does_not_answer_masks_and_restores_nothing(server, monkeypatch):
    """No text is masked without the values the store keeps, nor restored without its fakes: each call is answered 503,
    and quotes nothing of what it was sent."""
    _working(server)
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        [masked] = await _masked(client, secret, [f"write to {FIRST}"])
        store = (await server.spaces.runtime(key)).store

        def silent(*args, **kwargs):
            raise psycopg.OperationalError("the connection was closed")

        monkeypatch.setattr(store, "with_words", silent)
        for operation, text in (("mask", f"to {FIRST}"), ("scan", f"to {FIRST}"), ("unmask", f"from {masked}")):
            answer = await client.post(f"{APPS_API}/{operation}", json={"texts": [text]}, headers=_bearer(secret))
            assert answer.status_code == 503, operation
            assert answer.json() == {"error": STORE_UNAVAILABLE}
            assert FIRST not in answer.text and masked not in answer.text


async def test_a_mask_whose_new_values_are_not_kept_is_answered_503_and_keeps_none_of_them(server, monkeypatch):
    """Ruling T10i: a mask call keeps its new values together, committed as it ends. One whose commit fails is answered
    503 (try again), quoting nothing it was sent: none of its values is kept, nor left in memory with a fake the store
    does not keep; the next call masks them, and its fakes unmask."""
    _working(server)
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        await _masked(client, secret, [f"write to {FIRST}"])
        store = (await server.spaces.runtime(key)).store
        together = store.together

        def failing():
            kept = together()

            def commit():
                kept.rollback()
                raise psycopg.OperationalError("the connection was closed")

            kept.commit = commit
            return kept

        monkeypatch.setattr(store, "together", failing)
        text = f"cc {_email(1)} and {_email(2)}"
        answer = await client.post(f"{APPS_API}/mask", json={"texts": [text]}, headers=_bearer(secret))
        assert answer.status_code == 503 and answer.json() == {"error": STORE_UNAVAILABLE}
        assert _email(1) not in answer.text
        assert store.by_original(_email(1)) is None and not _held(await _vault(server, key), _email(1))
        monkeypatch.undo()
        [masked] = await _masked(client, secret, [text])
        assert _email(1) not in masked and _email(2) not in masked
        assert await _call(client, secret, "unmask", [masked]) == [{"text": text, "restored": 2}]


async def test_a_call_past_what_one_call_may_weigh_is_refused_422(server, monkeypatch):
    """Ruling T8a (c): texts whose words name more of the key's values than one call may weigh are refused with 422 and
    words that say so, nothing masked; never a 500, never a part."""
    monkeypatch.setattr(vault_module, "CANDIDATES_AT_MOST", 3)
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        roles = ("info", "sales", "admin", "support", "office")  # mailboxes of roles: found by their domain's name
        await _masked(client, secret, [f"write to {role}@lanzavecchia.it" for role in roles])
        for operation in ("mask", "scan"):
            body = {"texts": ["ask lanzavecchia about the renewal"]}
            answer = await client.post(f"{APPS_API}/{operation}", json=body, headers=_bearer(secret))
            assert answer.status_code == 422, operation
            assert answer.json() == {
                "error": "the texts of this call hold the words of more than 3 values or fakes of this vault, more than "
                "one call may weigh: nothing of it was masked or restored"
            }
            assert "lanzavecchia" not in answer.text


@pytest.fixture
def upstream(monkeypatch):
    fake = FakeUpstream().start()
    # OpenAI's route of every space built in the test leads to the stub model on 127.0.0.1, which says back what it got.
    monkeypatch.setattr("latet.gateway.app.DEFAULT_UPSTREAMS", (Upstream("openai", "/openai", fake.url),))
    yield fake
    fake.stop()


def test_the_gateway_still_restores_a_keys_conversation(server, upstream):
    _working(server)
    _, secret = server.identities.add_key("Billing CI")
    with TestClient(create_server_app(server), base_url=URL) as client:
        first = client.post(f"{APPS_API}/mask", json={"texts": [f"write to {FIRST}"]}, headers=_bearer(secret))
        assert first.status_code == 200
        texts = [f"login by {_email(n)}" for n in range(500)]
        assert client.post(f"{APPS_API}/mask", json={"texts": texts}, headers=_bearer(secret)).status_code == 200
        before = len(upstream.received)
        body = {"model": "m", "messages": [{"role": "user", "content": f"write to {FIRST} about the invoice"}]}
        answer = client.post(
            f"/u/{route_key(secret)}/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-test"}
        )
    assert answer.status_code == 200
    said = "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )
    assert FIRST in said  # the answer restored
    assert FIRST not in b"".join(upstream.received[before:]).decode()  # what left was masked
