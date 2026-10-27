"""Forgetting values of an API key's vault (Task 11b), through the applications API (``POST /_latet/apps/v1/forget``,
for a token given ``forget``, which nothing implies) and through the administration (``POST
/_latet/server/v1/admin/identities/<key>/forget``): each value found exactly and taken out with everything that names
it, in PostgreSQL and in the memory of every instance; its next mask gives it a new fake, and its old fake restores
nothing; the answer counts, and the entry of the administration log, written in the forget's own transaction before the
answer, counts too, naming no value. A log that does not take it forgets nothing."""

import asyncio
import contextlib
import json

import httpx
import psycopg
import pytest

from latet.bridge.apps import APPS_API, MOST_TEXTS
from latet.config import LlmReview
from latet.gateway.routes import Upstream
from latet.server.app import create_server_app
from latet.server.machines import StoresUnavailable
from latet.server.master import root_key
from latet.server.tokens import route_key
from latet.server.vault import PgVaultStore

from ..e2e.fake_upstream import FakeUpstream
from ..unit.test_reviewer import FakeServer, reviewer
from .conftest import dump, valkey_dump
from .support import URL, captured_logs, listening, until

pytestmark = pytest.mark.server

CANARY = "mario.rossi84@gmail.com"  # at a mail provider's: no domain of its own kept beside it
OTHER = "giulia.verdi@gmail.com"
WHERE = ("10.0.3.9", 40000)  # the client's address, as the administration log records it
ADMIN = "/_latet/server/v1/admin"
FORGET_WORDS = "the forget could not be written in the administration log, so nothing was forgotten: try again"


def _token(server, key_id, name, permissions):
    """Another token of the API key ``key_id``, with ``permissions``: its id and its secret."""
    with server.identities.changing() as changes:
        info, secret = server.identities.add_token(key_id, name, permissions, within=changes)
    return info.id, secret


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


async def _post(client, secret, operation, body):
    return await client.post(f"{APPS_API}/{operation}", json=body, headers=_bearer(secret))


async def _masked(client, secret, text):
    answer = await _post(client, secret, "mask", {"texts": [text]})
    assert answer.status_code == 200, answer.text
    return answer.json()["results"][0]["text"]


async def _unmasked(client, secret, text):
    answer = await _post(client, secret, "unmask", {"texts": [text]})
    assert answer.status_code == 200, answer.text
    return answer.json()["results"][0]


async def _forget(client, secret, values):
    return await _post(client, secret, "forget", {"values": values})


def _entry_of(server, key_id, value):
    """The vault's row of ``value`` in ``key_id``'s vault, as the database has it: every column, the sealed ones too,
    and the keyed hashes of its strings but its fake's, which a forget retires (``_fake_strings``)."""
    store = PgVaultStore(server.db, key_id, root_key(server.db, server.master, key_id))
    found = store.by_original(value)
    if found is None:
        return None
    with server.db.connection() as conn:
        row = conn.execute(
            "SELECT id, original, original_key, surrogate, surrogate_key FROM vault_entities"
            " WHERE identity_id = %s AND id = %s",
            (key_id, found.id),
        ).fetchone()
        strings = [bytes(r[0]) for r in conn.execute(
            "SELECT key FROM vault_strings WHERE identity_id = %s"
            " AND ((entity_id = %s AND role <> 'surrogate') OR also = %s)",
            (key_id, found.id, found.id),
        )]  # fmt: skip
        words = [bytes(r[0]) for r in conn.execute(
            "SELECT word FROM vault_words WHERE identity_id = %s AND entity_id = %s AND word IS NOT NULL",
            (key_id, found.id),
        )]  # fmt: skip
    return found, [row[0].encode(), *(bytes(part) for part in row[1:]), *strings, *words]


def _fake_strings(server, key_id, entity):
    """The keyed hashes of ``entity``'s fake as the vault's strings keep it (and of how it reads, if it is written in
    escapes): what a forget leaves, retired, of no entry (the final security review's H2)."""
    with server.db.connection() as conn:
        return [bytes(r[0]) for r in conn.execute(
            "SELECT key FROM vault_strings WHERE identity_id = %s AND entity_id = %s AND role = 'surrogate'",
            (key_id, entity.id),
        )]  # fmt: skip


def _retired(server, key_id):
    """The keyed hashes of the key's retired fakes, of no entry."""
    with server.db.connection() as conn:
        return {bytes(r[0]) for r in conn.execute(
            "SELECT key FROM vault_strings WHERE identity_id = %s AND role = 'retired' AND entity_id IS NULL"
            " AND also IS NULL",
            (key_id,),
        )}  # fmt: skip


async def test_forget_needs_its_own_permission(server):
    """A key's first token (no permissions of its own: scan, mask and unmask) cannot forget, nor can a token with every
    other permission; a token given forget can, and only forget."""
    key, first = server.identities.add_key("Billing CI")
    _, logs = _token(server, key.id, "logs", ["mask", "unmask"])
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    _, everything = _token(server, key.id, "all", ["mask", "unmask", "forget"])
    async with _calling(server) as client:
        for secret in (first, logs):
            refused = await _forget(client, secret, [CANARY])
            assert refused.status_code == 403
            assert refused.json() == {"error": "this token lacks the forget permission"}
        for secret in (eraser, everything):
            assert (await _forget(client, secret, [CANARY])).json() == {"forgotten": 0, "unknown": 1}
        for operation in ("scan", "mask", "unmask"):  # forget alone: nothing else
            answer = await _post(client, eraser, operation, {"texts": [f"to {CANARY}"]})
            assert answer.json() == {"error": f"this token lacks the {operation} permission"}


async def test_a_forgotten_value_gets_a_new_fake_and_its_old_fake_restores_nothing(server):
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        old = await _masked(client, secret, f"write to {CANARY}")
        kept = await _masked(client, secret, f"cc {OTHER}")
        answer = await _forget(client, eraser, [CANARY, "nobody@nowhere.example", CANARY])
        assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
        assert answer.json() == {"forgotten": 1, "unknown": 1}  # each value counted once
        assert server.spaces._spaces[key.id].parts.vault.generation == 1  # drawn in at once, no value refused first
        assert await _unmasked(client, secret, old) == {"text": old, "restored": 0}
        new = await _masked(client, secret, f"write to {CANARY}")
        assert new != old and CANARY not in new
        assert await _unmasked(client, secret, new) == {"text": f"write to {CANARY}", "restored": 1}
        assert await _unmasked(client, secret, kept) == {"text": f"cc {OTHER}", "restored": 1}  # the others stay
        assert (await _forget(client, eraser, [CANARY])).json() == {"forgotten": 1, "unknown": 0}  # the new one


async def test_a_companys_domain_is_a_value_of_its_own(server):
    """An address at a company's domain keeps the domain as a value of its own (its fake is the domain of the address's
    fake): forgetting the address leaves the domain, which still restores the old fake's domain part, until it is
    forgotten too."""
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    address = "mario.rossi@acme-intra.it"
    async with _calling(server) as client:
        old = await _masked(client, secret, f"write to {address}")
        local, domain = old.removeprefix("write to ").split("@")
        assert (await _forget(client, eraser, [address])).json() == {"forgotten": 1, "unknown": 0}
        back = await _unmasked(client, secret, old)
        assert back == {"text": f"write to {local}@acme-intra.it", "restored": 1}  # the domain alone
        assert (await _forget(client, eraser, ["acme-intra.it"])).json() == {"forgotten": 1, "unknown": 0}
        assert await _unmasked(client, secret, old) == {"text": old, "restored": 0}
        new = await _masked(client, secret, f"write to {address}")
    assert new != old and domain not in new and "acme-intra" not in new


async def test_the_entry_is_written_before_the_answer_and_names_no_value(server, monkeypatch):
    key, secret = server.identities.add_key("Billing CI")
    token, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        fake = await _masked(client, secret, f"write to {CANARY}")
        with captured_logs(level="debug") as logs:
            answer = await _forget(client, eraser, [CANARY, OTHER])
    assert answer.json() == {"forgotten": 1, "unknown": 1}
    [entry] = server.audit.query(action="apps.forgotten")
    assert (entry.actor, entry.target, entry.address) == (key.id, key.id, WHERE[0])
    assert entry.details == {"token": token, "values": 2, "forgotten": 1, "unknown": 1}
    written = json.dumps([[e.action, e.details] for e in server.audit.query()])
    told = logs.getvalue()
    for value in (CANARY, OTHER, "mario.rossi84", fake.removeprefix("write to ")):
        assert value not in written and value not in told


@pytest.mark.parametrize(
    "failure",
    [
        lambda: StoresUnavailable("OperationalError"),
        lambda: psycopg.OperationalError("connection to server at db.acme.lan failed"),
        lambda: RuntimeError("a fault of Latet's own"),
    ],
    ids=["stores", "postgresql", "own-fault"],
)
async def test_a_forget_the_log_does_not_take_forgets_nothing(server, monkeypatch, failure):
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        fake = await _masked(client, secret, f"write to {CANARY}")

        def unwritable(*args, **kwargs):
            raise failure()

        monkeypatch.setattr(server.audit, "record", unwritable)
        refused = await _forget(client, eraser, [CANARY])
        assert refused.status_code == 503 and refused.json() == {"error": FORGET_WORDS}
        assert CANARY not in refused.text
        monkeypatch.undo()
        assert await _masked(client, secret, f"write to {CANARY}") == fake  # nothing forgotten: the same fake
        assert await _unmasked(client, secret, fake) == {"text": f"write to {CANARY}", "restored": 1}
    assert server.audit.query(action="apps.forgotten") == []


def _reviewed(runtime, fake):
    """The key's space with the stub local reviewer the reviewer's tests use (its server ``fake``)."""
    mediator = runtime.mediator
    mediator.llm_review = LlmReview(enabled=True, model="fake-model")
    mediator.reviewer = reviewer(fake, mediator.llm_review, memory=runtime.store)
    return mediator


def _rows_of(server, key_id):
    """How many rows of the key's history each table has."""
    with server.db.connection() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE identity_id = %s", (key_id,)).fetchone()[0]
            for table in ("exchanges", "blocks", "review_records")
        }


async def test_nothing_of_a_forgotten_value_stays_in_the_database(server, bus, prefix, upstream):
    """Its entry, its strings, its words and edges, the local reviewer's remembered answer that found it, and what the
    key's gateway kept of it (review 11b's M6, Ruling 11b-R1: a conversation of an agent through the key's first
    token, which names its entry in Valkey, and the key's history, which keeps the old fake with it) are gone: nothing
    of it nor of its entry's id is in any table of the server's (every value of every table read), nor in Valkey. Its
    fake's keyed hash alone stays, retired, of no entry, so that no other value is given that fake (H2)."""
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    text = "mia moglie Maria ha la chemio giovedì"
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(key)
        _reviewed(runtime, FakeServer())
        masked = await _masked(client, secret, f"{text}, {CANARY}")
        assert "Maria" not in masked and CANARY not in masked
        body = {"model": "m", "messages": [{"role": "user", "content": f"write to {CANARY} about the invoice"}]}
        turn = await client.post(
            f"/u/{route_key(secret)}/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-x"}
        )
        assert turn.status_code == 200 and CANARY in turn.text  # its fake restored in the answer
        assert runtime.recorder is not None and runtime.recorder.drain(timeout=10)
        found = [_entry_of(server, key.id, value) for value in ("Maria", CANARY)]
        assert all(found)
        fakes = {hashed for entity, _ in found for hashed in _fake_strings(server, key.id, entity)}
        assert len(fakes) == 2
        shown = [entity.id.encode() for entity, _ in found]
        assert shown[1] in valkey_dump(bus, prefix)  # the conversation's field names its entry
        with server.db.connection() as conn:  # the answer that found Maria, and the turn's, which found nothing
            answers = conn.execute("SELECT COUNT(*) FROM reviews WHERE identity_id = %s", (key.id,)).fetchone()[0]
        assert answers == 2
        before = _rows_of(server, key.id)
        assert before["exchanges"] >= 1 and before["blocks"] >= 1
        answer = await _forget(client, eraser, ["Maria", CANARY])
    assert answer.json() == {"forgotten": 2, "unknown": 0}
    everything = dump(server.db) + valkey_dump(bus, prefix)
    for entity, parts in found:
        for part in parts:  # its id, its value sealed, its keyed hashes, its strings and words
            assert part not in everything, entity.etype
        assert entity.original.encode() not in everything
    assert _retired(server, key.id) == fakes
    with server.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE identity_id = %s", (key.id,)).fetchone()[0] == 1
        left = conn.execute("SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s", (key.id,)).fetchone()[0]
    assert left == 1  # chemio, which nobody asked to forget
    after = _rows_of(server, key.id)
    assert after["exchanges"] < before["exchanges"] and after["blocks"] < before["blocks"]


async def test_the_reviewers_answers_in_memory_go_with_the_value(instances):
    """Review 11b's N3: the local reviewer remembers its answers in each instance's memory too, values in clear in
    their findings. The instance that forgot a value, which knows it, drops the answers that found it; another, which
    hears of the forget by the entry's id alone, drops every answer it remembers in memory (a text sent again finds
    its answer in PostgreSQL, which the forget took those that held a value from)."""
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    _, eraser = _token(a, key.id, "erasure", ["forget"])
    reviewers = []
    async with _calling(a, b) as (first, second):
        for one, client in ((a, first), (b, second)):
            reviewers.append(_reviewed(await one.spaces.runtime(key), FakeServer()).reviewer)
            await _masked(client, secret, "mia moglie Maria ha la chemio giovedì")
            await _masked(client, secret, "the customer Ferri called")

        def held(reviewer):
            return {f.value for answer in reviewer._texts.values() for f in answer.findings}

        assert all({"Maria", "Ferri"} <= held(r) for r in reviewers)
        engine = (await b.spaces.runtime(key)).built.engine
        heard = engine.rules_version
        assert (await _forget(first, eraser, ["Maria"])).json() == {"forgotten": 1, "unknown": 0}
        await until(lambda: engine.rules_version > heard, 10)
    assert "Maria" not in held(reviewers[0]) and "Ferri" in held(reviewers[0])  # the values known: those alone
    assert held(reviewers[1]) == set() and len(reviewers[1]._pieces) == 0  # known by their entries: every one


async def test_another_instance_no_longer_masks_it_with_the_old_fake(instances):
    """Two instances on one store: the one that did not forget it, which holds the value in memory and the text masked
    in its engine's cache, drops both as it hears of it, and masks it with the new fake."""
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    _, eraser = _token(a, key.id, "erasure", ["forget"])
    async with _calling(a, b) as (first, second):
        old = await _masked(first, secret, f"write to {CANARY}")
        assert await _masked(second, secret, f"write to {CANARY}") == old  # held in its memory and its cache
        engine = (await b.spaces.runtime(key)).built.engine
        heard = engine.rules_version
        assert (await _forget(first, eraser, [CANARY])).json() == {"forgotten": 1, "unknown": 0}
        await until(lambda: engine.rules_version > heard, 10)
        there = await _masked(second, secret, f"write to {CANARY}")
        assert there != old and CANARY not in there
        assert await _masked(first, secret, f"write to {CANARY}") == there  # the same new fake on both
        assert await _unmasked(second, secret, old) == {"text": old, "restored": 0}


async def test_another_instance_that_let_it_go_from_memory_drops_its_masked_text_too(instances):
    """The instance's engine keeps the text masked with the old fake though the value left its memory (its working set
    let it go): it drops its masked texts all the same when it hears of the forget, and masks it anew."""
    a, b = instances
    for one in (a, b):
        one.settings.key_working_values = 100
    key, secret = a.identities.add_key("Billing CI")
    _, eraser = _token(a, key.id, "erasure", ["forget"])
    async with _calling(a, b) as (first, second):
        old = await _masked(first, secret, f"write to {CANARY}")
        assert await _masked(second, secret, f"write to {CANARY}") == old
        texts = [f"login by customer.number{n:05d}@gmail.com" for n in range(300)]
        assert (await _post(second, secret, "mask", {"texts": texts})).status_code == 200
        runtime = await b.spaces.runtime(key)
        assert all(e.original != CANARY for e in runtime.built.vault.entities())  # out of its memory
        heard = runtime.built.engine.rules_version
        assert (await _forget(first, eraser, [CANARY])).json() == {"forgotten": 1, "unknown": 0}
        await until(lambda: runtime.built.engine.rules_version > heard, 10)
        there = await _masked(second, secret, f"write to {CANARY}")
    assert there != old and CANARY not in there


async def test_a_value_kept_again_before_the_forget_is_heard_is_masked_with_its_new_fake(server):
    """An instance that hears of a forget only once the value was met again and kept, elsewhere, with a fake of the
    key's new generation, under the same id (an entry kept so by hand: Latet's own get an id of the new generation):
    it drops its masked texts and learns the entry PostgreSQL holds at once, never waiting for the entry's own message
    (review 11b's M2), so that it masks the value with the new fake, never with the old one."""
    from dataclasses import replace

    from latet.server.spaces import _forget

    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        old = await _masked(client, secret, f"write to {CANARY}")
        runtime = await server.spaces.runtime(key)
        space = server.spaces._spaces[key.id]
        entity = runtime.built.vault.by_original(CANARY)
        # Elsewhere, unheard here: forgotten, then kept again with a fake of the new generation.
        loose = PgVaultStore(server.db, key.id, root_key(server.db, server.master, key.id))
        assert loose.forget_values([CANARY])[0] == [entity.id]
        loose.load_generation()
        assert loose.save_entity(replace(entity, surrogate="giada.ferri84@gmail.example", counter=0)) is None
        heard = runtime.built.engine.rules_version
        await asyncio.to_thread(_forget, space, entity.id)
        assert runtime.built.engine.rules_version > heard  # its masked texts dropped
        new = await _masked(client, secret, f"write to {CANARY}")  # the entry's own message not heard yet
    assert new == "write to giada.ferri84@gmail.example" and new != old


@pytest.fixture
def upstream(monkeypatch):
    fake = FakeUpstream().start()
    # OpenAI's route of every space built in the test leads to the stub model on 127.0.0.1, which says back what it got.
    monkeypatch.setattr("latet.gateway.app.DEFAULT_UPSTREAMS", (Upstream("openai", "/openai", fake.url),))
    yield fake
    fake.stop()


async def test_the_gateway_of_the_same_key_masks_it_anew(server, upstream):
    """A conversation through the key's address, after the value was forgotten through the applications API: what leaves
    holds a new fake, never the old one, and the answer is restored with the value."""
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        old = (await _masked(client, secret, f"write to {CANARY}")).removeprefix("write to ")
        assert (await _forget(client, eraser, [CANARY])).json() == {"forgotten": 1, "unknown": 0}
        before = len(upstream.received)
        body = {"model": "m", "messages": [{"role": "user", "content": f"write to {CANARY} about the invoice"}]}
        answer = await client.post(
            f"/u/{route_key(secret)}/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-x"}
        )
    assert answer.status_code == 200
    sent = b"".join(upstream.received[before:]).decode()
    assert CANARY not in sent and old not in sent and "@" in sent
    said = "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )
    assert CANARY in said  # the new fake restored


BAD = [
    pytest.param(b"[]", id="not-an-object"),
    pytest.param(b"not json " + CANARY.encode(), id="not-json"),
    pytest.param(json.dumps({"values": CANARY}).encode(), id="values-a-string"),
    pytest.param(json.dumps({"values": [CANARY, 1]}).encode(), id="values-not-all-strings"),
    pytest.param(json.dumps({"values": []}).encode(), id="values-empty"),
    pytest.param(json.dumps({"texts": [CANARY]}).encode(), id="texts-not-values"),
    pytest.param(json.dumps({"values": [CANARY] * (MOST_TEXTS + 1)}).encode(), id="too-many-values"),
]


@pytest.mark.parametrize("body", BAD)
async def test_bad_bodies(server, body):
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        await _masked(client, secret, f"write to {CANARY}")
        answer = await client.post(f"{APPS_API}/forget", content=body, headers=_bearer(eraser))
        assert answer.status_code == 400
        assert set(answer.json()) == {"error"} and CANARY not in answer.text
        assert await _masked(client, secret, f"write to {CANARY}")  # nothing forgotten
    assert server.audit.query(action="apps.forgotten") == []


async def test_a_body_past_the_servers_limit_is_413(server):
    server.settings = server.settings.model_copy(update={"max_request_mb": 1})
    key, _ = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        answer = await _forget(client, eraser, ["x" * (1024 * 1024)])
    assert answer.status_code == 413


async def test_a_forget_never_waits_for_the_threads_of_the_unmasks_entries(server):
    """Review 11b's I2: a forget runs in threads of its own, its entry of the administration log with it, never in the
    threads of the applications API's entries, which every key's unmasks write theirs in: those busy, it is done all
    the same, and they never wait for it."""
    import threading

    from latet.server import machines

    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    release = threading.Event()
    async with _calling(server) as client:
        await _masked(client, secret, f"write to {CANARY}")
        busy = [machines._APPS_LOG.submit(release.wait, 30) for _ in range(machines.APPS_LOG_THREADS)]
        try:
            answer = await asyncio.wait_for(_forget(client, eraser, [CANARY]), 5)
        finally:
            release.set()
            for job in busy:
                job.result(30)
    assert answer.json() == {"forgotten": 1, "unknown": 0}
    assert len(server.audit.query(action="apps.forgotten")) == 1


async def test_the_metrics_count_values_asked_and_forgotten(server):
    from latet.server.operations import server_app

    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with listening(server):
        transport = httpx.ASGITransport(app=server_app(server), client=WHERE)
        async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
            await _masked(client, secret, f"to {CANARY}")
            assert (await _forget(client, eraser, [CANARY, OTHER, "x"])).status_code == 200
    text = server.metrics.render()
    assert 'latet_apps_texts_total{operation="forget"} 3' in text
    assert 'latet_apps_values_total{operation="forget"} 1' in text
    assert CANARY not in text


async def test_a_store_that_does_not_answer_forgets_nothing_and_says_503(server, monkeypatch):
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    async with _calling(server) as client:
        fake = await _masked(client, secret, f"write to {CANARY}")
        store = (await server.spaces.runtime(key)).store

        def silent(*args, **kwargs):
            raise psycopg.OperationalError("the connection to db.acme.lan was closed")

        monkeypatch.setattr(store, "forget_values", silent)
        answer = await _forget(client, eraser, [CANARY])
        assert answer.status_code == 503 and CANARY not in answer.text and "acme" not in answer.text
        monkeypatch.undo()
        assert await _masked(client, secret, f"write to {CANARY}") == fake


async def _admin(server):
    return {"x-latet-admin": server.admin.issue("first administrator")}


async def test_the_administrator_forgets_values_of_a_key(instances):
    """``POST …/identities/<key>/forget``: the counts, a ``key.forgotten`` entry with them (never a value), and every
    instance's space drops the value: the next mask on either gives a new fake, and the old one restores nothing."""
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    admin = await _admin(a)
    async with _calling(a, b) as (first, second):
        old = await _masked(first, secret, f"write to {CANARY}")
        assert await _masked(second, secret, f"write to {CANARY}") == old
        engines = [(await one.spaces.runtime(key)).built.engine for one in (a, b)]
        heard = [engine.rules_version for engine in engines]
        answer = await first.post(
            f"{ADMIN}/identities/{key.id}/forget", json={"values": [CANARY, OTHER]}, headers=admin
        )
        assert answer.status_code == 200 and answer.json() == {"forgotten": 1, "unknown": 1}
        await until(lambda: all(e.rules_version > h for e, h in zip(engines, heard, strict=True)), 10)
        new = await _masked(second, secret, f"write to {CANARY}")
        assert new != old and await _masked(first, secret, f"write to {CANARY}") == new
        assert await _unmasked(first, secret, old) == {"text": old, "restored": 0}
    [entry] = a.audit.query(action="key.forgotten")
    assert entry.target == key.id and entry.actor.startswith("admin:")
    assert entry.details == {"values": 2, "forgotten": 1, "unknown": 1, "status": 200}
    assert CANARY not in json.dumps([[e.action, e.details] for e in a.audit.query()])


async def test_the_administrators_forget_takes_what_the_gateway_kept_too(instances, bus, prefix, upstream):
    """Ruling 11b-R1 through the administration: an agent's conversation through the key's first token names the
    value's entry in Valkey, which both instances hold, and the key's history keeps it with its fake. After the forget
    neither Valkey nor the history names it, the conversation each instance holds forgets it, and the conversation
    met again names the new entry alone, never the old one written back: its earlier texts, kept as they left with the
    old fake and the old entry, are masked anew."""
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    admin = await _admin(a)
    body = {"model": "m", "messages": [{"role": "user", "content": f"write to {CANARY} about the invoice"}]}
    route = f"/u/{route_key(secret)}/openai/v1/chat/completions"
    async with _calling(a, b) as (first, second):
        turn = await first.post(route, json=body, headers={"authorization": "Bearer sk-x"})
        assert turn.status_code == 200 and CANARY in turn.text
        runtimes = [await one.spaces.runtime(key) for one in (a, b)]
        assert runtimes[0].recorder is not None and runtimes[0].recorder.drain(timeout=10)
        old, _ = _entry_of(a, key.id, CANARY)
        assert f"e:{old.id}".encode() in valkey_dump(bus, prefix)
        assert (await second.post(route, json=body, headers={"authorization": "Bearer sk-x"})).status_code == 200
        assert runtimes[1].recorder is not None and runtimes[1].recorder.drain(timeout=10)  # recorded before the forget
        held = [s for s in runtimes[1].built.engine.conversations._here.values() if old.id in s.session.entity_ids]
        assert held  # the other instance holds the conversation, the entry in it
        before = _rows_of(a, key.id)
        heard = runtimes[1].built.engine.rules_version
        answer = await first.post(f"{ADMIN}/identities/{key.id}/forget", json={"values": [CANARY]}, headers=admin)
        assert answer.json() == {"forgotten": 1, "unknown": 0}
        assert old.id.encode() not in valkey_dump(bus, prefix)
        await until(lambda: runtimes[1].built.engine.rules_version > heard, 10)
        assert all(old.id not in s.session.entity_ids for s in held)
        after = _rows_of(a, key.id)
        assert after["exchanges"] < before["exchanges"] and after["blocks"] < before["blocks"]
        sent = len(upstream.received)
        again = await second.post(route, json=body, headers={"authorization": "Bearer sk-x"})
        assert again.status_code == 200 and CANARY in again.text  # masked anew, restored with the new fake
        new, _ = _entry_of(a, key.id, CANARY)
        left = b"".join(upstream.received[sent:]).decode()
        assert old.surrogate not in left and new.surrogate in left and CANARY not in left
        assert new.id != old.id and new.surrogate != old.surrogate
    everything = valkey_dump(bus, prefix)
    assert f"e:{new.id}".encode() in everything and old.id.encode() not in everything
    assert old.id.encode() not in dump(a.db)


async def test_the_administration_refuses_what_is_no_forget_of_a_key(server):
    key, _ = server.identities.add_key("Billing CI")
    person, _ = server.identities.add_person("Emily Carter")
    admin = await _admin(server)
    async with _calling(server) as client:

        async def forget(identity_id, body):
            return await client.post(f"{ADMIN}/identities/{identity_id}/forget", json=body, headers=admin)

        a_person = await forget(person.id, {"values": [CANARY]})
        assert a_person.status_code == 409 and "dashboard" in a_person.json()["error"]
        assert (await forget("u_nosuchidxx", {"values": [CANARY]})).status_code == 404
        for body in ({"values": []}, {"values": CANARY}, {"texts": [CANARY]}, {"values": [CANARY] * (MOST_TEXTS + 1)}):
            refused = await forget(key.id, body)
            assert refused.status_code == 400 and CANARY not in refused.text
        stranger = await client.post(f"{ADMIN}/identities/{key.id}/forget", json={"values": [CANARY]})
        assert stranger.status_code == 401
    assert server.audit.query(action="key.forgotten") == []
    assert CANARY not in json.dumps([[e.action, e.details] for e in server.audit.query()])


async def test_an_administrators_forget_the_log_does_not_take_forgets_nothing(server, monkeypatch):
    key, secret = server.identities.add_key("Billing CI")
    admin = await _admin(server)
    async with _calling(server) as client:
        fake = await _masked(client, secret, f"write to {CANARY}")
        real = server.audit.record

        def unwritable(*args, **kwargs):
            if len(args) > 1 and args[1] == "key.forgotten":
                raise psycopg.OperationalError("the chain's lock was not taken")
            return real(*args, **kwargs)

        monkeypatch.setattr(server.audit, "record", unwritable)
        answer = await client.post(f"{ADMIN}/identities/{key.id}/forget", json={"values": [CANARY]}, headers=admin)
        assert answer.status_code >= 500 and CANARY not in answer.text
        monkeypatch.undo()
        assert await _masked(client, secret, f"write to {CANARY}") == fake
    assert server.audit.query(action="key.forgotten") == []


async def test_a_disabled_key_can_still_have_values_forgotten(server):
    """A request that values leave a key's vault is served whatever the key's state: disabled, its values are there."""
    key, secret = server.identities.add_key("Billing CI")
    admin = await _admin(server)
    async with _calling(server) as client:
        await _masked(client, secret, f"write to {CANARY}")
        server.identities.disable(key.id)
        await server.spaces.drop(key.id)
        answer = await client.post(f"{ADMIN}/identities/{key.id}/forget", json={"values": [CANARY]}, headers=admin)
    assert answer.status_code == 200 and answer.json() == {"forgotten": 1, "unknown": 0}
