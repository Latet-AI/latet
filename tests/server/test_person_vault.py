"""A person's space on a Latet server keeps its vault in PostgreSQL as an API key's does (Task 9 of the server
performance plan): the values it used last in memory, and for good every value no word of a text finds glued, whatever
its age (``StoredVault(glued_all=True)``), every other one found in PostgreSQL by the words of its texts. A value found
once is masked for ever through the gateway, its conversation's answer is restored, two instances mask a new value
alike, the dashboard's vault page lists and reveals the whole vault a page at a time, and the names its filters reject
are forgotten from PostgreSQL. A vault kept before the words were is served from memory, as 0.10.0 served it, until
its words are made in the background; the space built after that is on its store."""

import asyncio
import json
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from latet.engine import stored as stored_vaults
from latet.engine.detectors.pii import PiiDetector
from latet.engine.masker import Masker
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.stored import StoredVault, glued_both, held_glued
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault
from latet.server import spaces as spaces_module
from latet.server import vault as pg
from latet.server.app import create_server_app
from latet.server.index import gone_owner
from latet.server.master import root_key
from latet.server.tokens import route_key
from latet.server.vault import PgVaultStore

from .support import URL, captured_logs, listening, until
from .test_key_vault import upstream  # noqa: F401 - the stub model every space of a test reaches

pytestmark = pytest.mark.server


@pytest.fixture(autouse=True)
def on_its_store(monkeypatch):
    """The person spaces of these tests on their store whatever their size (``stored.WHOLE_VALUES`` 0), as a vault of
    more than 35,000 values is: those of a space held whole are in ``test_person_vault_whole.py``."""
    monkeypatch.setattr(stored_vaults, "WHOLE_VALUES", 0)


WORKING = 100  # the working set of the tests' spaces (the server's key_working_values, set below its range)
NAME = "Ferruccio Lanzavecchia"  # a customer's name, learnt once: no detector of the tests reads it
NAME_FAKE = "Giorgio Fantini"
FIRST = "laura.bianchi@gmail.com"
TOKEN = "Kq7mZ2xW9pL4vR8tN3bYa6Cd5Ef1Gh0JkMn2Pq4R"  # a long token: glued on both sides, no word of a text finds it
SHORTS = ("Xq9z5-ab", "Rk4m8-cd", "Tb7w2-ef")  # held glued (``held_glued``), their fakes not glued on both sides
CHAT = "/v1/chat/completions"


def _email(n: int) -> str:
    return f"customer.number{n:05d}@gmail.com"


def _fake(n: int) -> str:
    return f"fk{n:05d}.zeta@posta.example"


def _working(*servers, held: int | None = None) -> None:
    for server in servers:
        server.settings.key_working_values = WORKING
        if held is not None:
            server.settings.key_held_secrets = held


def _store(server, person) -> PgVaultStore:
    return PgVaultStore(server.db, person.id, root_key(server.db, server.master, person.id), limit=1_000_000)


def _keep(server, person, entities) -> None:
    """``entities`` in the person's vault, kept as its space keeps them (their words and prints), in one transaction:
    thousands of values a request at a time would take minutes."""
    store = _store(server, person)
    together = store.together()
    try:
        for entity in entities:
            assert store.save_entity(entity) is None
        together.commit()
    except BaseException:
        together.rollback()
        raise
    finally:
        store.close()


def _emails(count: int, start: int = 0, at: float | None = None) -> list[Entity]:
    """``count`` addresses of a mail provider's (no domain of their own to keep), each with a fake of its own, the
    later ones kept later."""
    base = time.time() if at is None else at
    return [
        Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, _email(n), _fake(n), 0, base + n / 1000)
        for n in range(start, start + count)
    ]


def _secret(server, person, value: str, n: int) -> Entity:
    """A secret of the person's, with a fake of its shape (``Vault.intern``), kept before the addresses."""
    made = Vault(root_key(server.db, server.master, person.id)).intern("API_KEY", EntityClass.SECRET, value)
    return Entity(f"s{n:019d}", made.etype, made.eclass, made.original, made.surrogate, 0, time.time() - 3600 + n)


async def _runtime(server, person):
    return await server.spaces.runtime(person)


def _held(vault, value: str) -> bool:
    return any(e.original == value for e in vault.entities())


async def _leaves(runtime, text: str) -> str:
    """``text`` as the space sends it in a tool's output (``Engine.mask_text``, the gateway's path without HTTP)."""
    engine = runtime.built.engine

    def masked() -> str:
        session = engine.session("cc:person-vault")
        return engine.mask_text(text, SlotKind.TOOL_RESULT, session).text

    return await asyncio.to_thread(masked)


def _token_of(server, person_name: str):
    """A new person and the token of their computer, enrolled without a call."""
    person, code = server.identities.add_person(person_name)
    with server.identities.changing() as changes:
        found = server.identities.enrol(code, "laptop", within=changes)
    assert found is not None
    return person, found[1]


def _chat(client, token, messages) -> str:
    """The text of the answer through the person's address, as the agent reads it."""
    body = {"model": "gpt-4.1-mini", "messages": messages}
    answer = client.post(f"/u/{route_key(token)}/openai{CHAT}", json=body, headers={"authorization": "Bearer sk-test"})
    assert answer.status_code == 200, answer.text
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )


def _stored(server, person) -> bool:
    """Whether the person's space on ``server``, built by a request, is on its store."""
    return isinstance(server.spaces._spaces[person.id].runtime.built.vault, StoredVault)


def _tool_output(text: str) -> list[dict]:
    """A conversation whose last message is a tool's output holding ``text``."""
    call = {
        "id": "call_Rt8vQ2mZ5kB1xW7nCd4eF9hJ",
        "type": "function",
        "function": {"name": "read_ticket", "arguments": "{}"},
    }
    return [
        {"role": "user", "content": "read the ticket"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": text},
    ]


# --- what a person's space holds -----------------------------------------------------------------------------------


async def test_a_persons_space_holds_no_whole_vault(server):
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    glued = [_secret(server, person, TOKEN, 0)]
    assert glued_both(glued[0], "original")
    _keep(server, person, [*glued, *_emails(5_000)])
    async with listening(server):
        vault = (await _runtime(server, person)).built.vault
        assert isinstance(vault, StoredVault) and vault.glued_all and vault.working == WORKING
        assert vault.count <= WORKING + len(glued)
        assert _held(vault, TOKEN) and not _held(vault, _email(0))


async def test_a_person_whose_values_have_no_words_is_served_from_memory_until_they_are_made(server):
    """Ruling T9-S2: a vault kept before the words were (0.10.0's, or an older version of the anchors) is served whole
    from memory, as before, while its words and prints are made in the background on one instance; the space built
    after them is on its store, and masks a value as it did."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(300))
    with server.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (person.id,))  # as 0.10.0 left them
    text = f"2026-10-24 login by {_email(5)} from the office"
    async with listening(server):
        runtime = await _runtime(server, person)
        assert type(runtime.built.vault) is Vault and runtime.built.vault.count == 300
        before = await _leaves(runtime, text)
        assert before == text.replace(_email(5), _fake(5))
        store = runtime.store
        await until(lambda: store.words_complete() and store.prints_complete(), seconds=60)
        assert type(runtime.built.vault) is Vault  # the live space keeps its vault until it closes
        await server.spaces.drop(person.id)
        again = await _runtime(server, person)
        assert isinstance(again.built.vault, StoredVault)
        assert await _leaves(again, text) == before


def test_a_persons_value_found_long_ago_is_masked_through_the_gateway(server, upstream):  # noqa: F811
    _working(server)
    person, token = _token_of(server, "Emily Carter")
    name = Entity("n" + "0" * 19, "PERSON", EntityClass.NER, NAME, NAME_FAKE, 0, time.time() - 3600)
    _keep(server, person, [name, *_emails(6_000)])
    text = f"2026-10-24 10:02:11 ticket 4411 closed, customer {NAME}, rating 5"
    with captured_logs() as logs, TestClient(create_server_app(server), base_url=URL) as client:
        before = len(upstream.received)
        _chat(client, token, _tool_output(text))
        sent = b"".join(upstream.received[before:]).decode()
        assert _stored(server, person)
    assert NAME not in sent and "Lanzavecchia" not in sent and NAME_FAKE in sent
    assert NAME not in logs.getvalue() and "Lanzavecchia" not in logs.getvalue()


def test_a_persons_conversation_restores_its_answer(server, upstream):  # noqa: F811
    _working(server)
    person, token = _token_of(server, "Emily Carter")
    _keep(server, person, _emails(1_000))
    with TestClient(create_server_app(server), base_url=URL) as client:
        before = len(upstream.received)
        said = _chat(client, token, [{"role": "user", "content": f"write to {FIRST} about the invoice"}])
        sent = b"".join(upstream.received[before:]).decode()
        # A value of the vault out of memory, said back by the model: restored too.
        again = _chat(client, token, [{"role": "user", "content": f"write to {_email(3)} and {FIRST}"}])
        assert _stored(server, person)
    assert FIRST not in sent and FIRST in said
    assert _email(3) in again and FIRST in again


async def test_a_persons_long_secret_glued_on_both_sides_is_masked_whatever_its_age(server):
    """Review Focus 3 and Ruling T9-S3: a long token glued on both sides, and short secrets glued so, which no word of a
    text finds, are held for good in a person's space whatever their age and however many: masked as its whole vault
    masked them (an API key's holds its newest ``key_held_secrets`` of the short ones alone)."""
    _working(server, held=1)  # below the short secrets kept: a person's holds every one all the same
    person, _ = server.identities.add_person("Emily Carter")
    secrets = [_secret(server, person, value, n) for n, value in enumerate((TOKEN, *SHORTS))]
    assert glued_both(secrets[0], "original")
    assert all(held_glued(e) and not glued_both(e, "surrogate") for e in secrets[1:])
    _keep(server, person, [*secrets, *_emails(6_000)])
    async with listening(server):
        runtime = await _runtime(server, person)
        assert isinstance(runtime.built.vault, StoredVault)
        for secret in secrets:
            text = f"the gateway logged ab{secret.original}cd today"
            left = await _leaves(runtime, text)
            assert secret.original not in left and secret.surrogate in left, secret.id


async def test_two_instances_mask_a_persons_new_value_alike(instances):
    """A value one instance keeps is masked with the same fake on the other, which never had it in memory: a new
    address, and a long token glued on both sides, which no word finds (Ruling T9-S3: what is held for good is read again
    after another instance's insert)."""
    a, b = instances
    _working(a, b)
    person, _ = a.identities.add_person("Emily Carter")
    _keep(a, person, _emails(300))
    token_text = f"the gateway logged x{TOKEN}y today"
    async with listening(a, b):
        first, second = await _runtime(a, person), await _runtime(b, person)
        assert isinstance(first.built.vault, StoredVault) and isinstance(second.built.vault, StoredVault)
        text = f"cc {FIRST} on the renewal"
        there = await _leaves(first, text)
        assert FIRST not in there
        assert await _leaves(second, text) == there
        secret = _secret(a, person, TOKEN, 9)
        _keep(a, person, [secret])  # kept on another instance: its message heard or not, the next call reads it
        assert await _leaves(second, token_text) == token_text.replace(TOKEN, secret.surrogate)


# --- the dashboard --------------------------------------------------------------------------------------------------


async def test_the_dashboards_vault_page_lists_the_whole_vault(server):
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    base = time.time() - 7200
    secrets = [
        Entity(f"t{n:019d}", "API_KEY", EntityClass.SECRET, f"tok_{n:02d}Zx9Qw8Er7Ty6", f"tok_{n:02d}Mn5Bv4Cx3Lk2", 0,
               base - n)
        for n in range(10)
    ]  # fmt: skip
    _keep(server, person, [*secrets, *_emails(5_990, at=base)])
    async with listening(server):
        runtime = await _runtime(server, person)
        assert isinstance(runtime.built.vault, StoredVault)
        dashboard = runtime.gateway.dashboard

        def page(q="", etype="", eclass="", number=1, per_page=50):
            return dashboard._vault_page(q, etype, eclass, number, per_page)

        first = await asyncio.to_thread(page)
        assert first["total"] == 6_000 and first["pages"] == 120 and first["page"] == 1
        assert [item["id"] for item in first["items"]] == [f"e{n:019d}" for n in range(5_989, 5_939, -1)]
        assert first["types"] == {"EMAIL": 5_990, "API_KEY": 10}
        assert all("original" not in item for item in first["items"])
        assert _email(5_989) not in json.dumps(first)
        last = await asyncio.to_thread(page, number=999)
        assert last["page"] == 120 and [item["id"] for item in last["items"]] == [
            *(f"e{n:019d}" for n in range(39, -1, -1)), *(f"t{n:019d}" for n in range(10))
        ]  # fmt: skip
        typed = await asyncio.to_thread(page, etype="API_KEY")
        assert typed["total"] == 10 and typed["types"] == first["types"]
        assert [item["id"] for item in typed["items"]] == [f"t{n:019d}" for n in range(10)]
        assert (await asyncio.to_thread(page, eclass="SECRET"))["total"] == 10
        assert (await asyncio.to_thread(page, eclass="PII"))["total"] == 5_990
        assert (await asyncio.to_thread(page, eclass="NOPE"))["total"] == 0
        found = await asyncio.to_thread(page, q="fk00042")  # part of a fake
        assert [item["id"] for item in found["items"]] == [f"e{42:019d}"] and found["types"] == first["types"]
        searched = await asyncio.to_thread(page, q="customer.number001", per_page=10)  # part of values: ten pages
        assert searched["total"] == 100 and searched["pages"] == 10
        past = await asyncio.to_thread(page, q="customer.number001", number=50, per_page=10)
        assert past["page"] == 10 and [item["id"] for item in past["items"]] == [
            f"e{n:019d}" for n in range(109, 99, -1)
        ]
        assert (await asyncio.to_thread(page, q=f"e{7:019d}"))["total"] == 1  # an entry's id
        assert runtime.built.vault.count <= WORKING + 10  # the listing filled no working set


async def test_rejected_names_are_forgotten_from_the_store(server):
    """A name the local model found that today's filters reject is forgotten as the space starts, out of its memory as
    it is: from PostgreSQL, as a forget is (its count of forgets moved, so that every instance's caches drop it)."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    product = Entity("p" + "0" * 19, "PERSON", EntityClass.NER, "Claude Code", "Marco Fontana", 0, time.time() - 3600)
    kept = Entity(
        "p" + "0" * 18 + "1", "PERSON", EntityClass.NER, "Giulia Bianchi", "Sara Ricci", 0, time.time() - 3599
    )
    _keep(server, person, [product, kept, *_emails(2_000)])
    with server.db.connection() as conn:
        row = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (gone_owner(person.id),)).fetchone()
    gone = row[0] if row is not None else 0
    async with listening(server):
        runtime = await _runtime(server, person)
        assert isinstance(runtime.built.vault, StoredVault)
        store = runtime.store
        assert await asyncio.to_thread(store.by_original, "Claude Code") is None
        assert await asyncio.to_thread(store.by_original, "Giulia Bianchi") is not None
    with server.db.connection() as conn:
        row = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (gone_owner(person.id),)).fetchone()
    assert row is not None and row[0] == gone + 1


async def test_a_persons_value_out_of_memory_is_revealed(server):
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(6_000))
    async with listening(server):
        runtime = await _runtime(server, person)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault) and vault.in_memory(f"e{0:019d}") is None
        answer = await runtime.gateway.dashboard.reveal(SimpleNamespace(path_params={"id": f"e{0:019d}"}))
        assert answer.status_code == 200 and json.loads(answer.body) == {"original": _email(0)}
        missing = await runtime.gateway.dashboard.reveal(SimpleNamespace(path_params={"id": "e-none"}))
        assert missing.status_code == 404


async def test_a_persons_stored_launch_key_is_never_revealed_nor_found(server):
    """N-1 on a server: a live launch key the person's vault holds is Latet's own credential: its reveal answers a
    plain label's flag in its place, and the page's search, read from the store, never finds it by its words."""
    from latet.engine.types import EntityClass

    key = "latet_" + "lk_" + "Qm9_" * 10 + "z1A"
    person, _ = server.identities.add_person("Ada Lovelace")
    made = Vault(root_key(server.db, server.master, person.id)).intern("LATET_LAUNCH_KEY", EntityClass.SECRET, key)
    _keep(server, person, [made, *_emails(3)])
    async with listening(server):
        runtime = await _runtime(server, person)
        dashboard = runtime.gateway.dashboard
        answer = await dashboard.reveal(SimpleNamespace(path_params={"id": made.id}))
        assert json.loads(answer.body) == {"original": None, "launch_key": True}
        for q in ("qm9_qm9", key.lower()):
            found = await asyncio.to_thread(runtime.store.listed, q=q, etype="", eclass="", page=1, per_page=50)
            assert all(entity.id != made.id for entity in found[0])
        typed = await asyncio.to_thread(
            runtime.store.listed, q="", etype="LATET_LAUNCH_KEY", eclass="", page=1, per_page=50
        )
        assert [entity.id for entity in typed[0]] == [made.id]


# --- the stored vault against the plain one ---------------------------------------------------------------------------


OVERLAPPING = ("Anna Maria", "Maria Rossi")
NAMES_TEXTS = (
    "met Anna Maria Rossi today",
    "Anna Maria, Maria Rossi and Anna Maria Rossi signed",
    '{"owner": "Anna Maria Rossi", "cc": "Maria Rossi"}',
    "AnnaMaria Rossi wrote to maria rossi",
    " ".join(["Anna Maria Rossi"] * 40),
)


def _masked_and_restored(vault, texts):
    """``texts`` masked as a person's gateway masks them (its words asked first, in one call), then each restored as
    an unmask of the whole vault restores it: the characters masked, and whether each comes back exactly."""
    masker = Masker(vault, [PiiDetector()])
    with vault.holding():
        vault.prefetch(list(texts))
        done = [masker.mask(text, SlotKind.TOOL_RESULT) for text in texts]
    covered, exact = [], []
    for text, masked in zip(texts, done, strict=True):
        covered.append(sorted({i for r in masked.replacements for i in range(r.start, r.end)}))
        with vault.holding():
            vault.prefetch_fakes([masked.text])
            back, _ = restore_text(
                masked.text, restores_for(vault.candidates_in(masked.text), parts=False, whole_vault=True)
            )
        exact.append(back == text)
    return covered, exact


async def test_overlapping_names_mask_alike_on_the_stored_and_the_plain_vault(server):
    """Carried from Task 8's review (minor 7): two names that overlap in a text (`Anna Maria` and `Maria Rossi` in `Anna
    Maria Rossi`) are masked over the same characters by a person's stored vault, out of memory as they are, as by the
    whole vault 0.10.0 held, and every text comes back exactly from both."""
    person, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, person.id)
    made = Vault(key)
    names = [made.intern("PERSON", EntityClass.NER, value) for value in OVERLAPPING]
    _keep(server, person, [*names, *_emails(300)])
    plain_covered, plain_exact = await asyncio.to_thread(_masked_and_restored, Vault(key, names), NAMES_TEXTS)
    store = _store(server, person)
    try:
        stored = StoredVault(key, store, working=0, glued_all=True)
        assert not any(_held(stored, value) for value in OVERLAPPING)
        stored_covered, stored_exact = await asyncio.to_thread(_masked_and_restored, stored, NAMES_TEXTS)
    finally:
        store.close()
    assert stored_covered == plain_covered and all(plain_covered[:3]) and plain_covered[4]
    assert stored_exact == plain_exact == [True] * len(NAMES_TEXTS)


# --- fix round 1 (the Task 9 review) ----------------------------------------------------------------------------------

CANARY = "canary-value-9f3k"
UV_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"


def _gone(server, person) -> int:
    with server.db.connection() as conn:
        row = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (gone_owner(person.id),)).fetchone()
    return row[0] if row is not None else 0


def test_a_lockfile_in_a_persons_conversation_is_masked_on_every_turn(server, upstream):  # noqa: F811
    """C1 (with Task 8's round 6, never refused): `uv.lock` (about 196,000 lookup words with its interior windows) as a
    tool's output, on three turns of one conversation that carries it, through the gateway: each turn leaves, its
    known value masked, from a person's vault on its store (read whole past the call's bounds)."""
    _working(server)
    person, token = _token_of(server, "Emily Carter")
    name = Entity("n" + "0" * 19, "PERSON", EntityClass.NER, NAME, NAME_FAKE, 0, time.time() - 3600)
    _keep(server, person, [name, *_emails(1_000)])
    output = UV_LOCK.read_text("utf-8") + f"\nchecked by {NAME}\n"
    messages = _tool_output(output)
    with TestClient(create_server_app(server), base_url=URL) as client:
        for turn in range(3):
            before = len(upstream.received)
            _chat(client, token, [*messages, {"role": "user", "content": f"turn {turn}"}])
            sent = b"".join(upstream.received[before:]).decode()
            assert NAME not in sent and NAME_FAKE in sent, turn
        assert _stored(server, person)


async def test_a_persons_refresh_reads_again_only_what_changed(server, monkeypatch):
    """I2: after another instance's commit a person's vault confirms only the entries named since: the values it holds
    for good (every held-glued secret, whatever their number) are not read whole again at every call, and a new one
    kept elsewhere is held."""
    person, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, person.id)
    held = [_secret(server, person, f"Q{n:03d}x-ab", n) for n in range(40)]
    _keep(server, person, [*held, *_emails(300)])
    store = PgVaultStore(server.db, person.id, key, index=True, limit=1_000_000)
    try:
        assert store.build_index()
        store.current()
        vault = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)
        assert all(vault.in_memory(e.id) is not None for e in held)
        reads: list[str] = []
        for name in ("bounded_retained", "load_held", "load_glued", "load_unanchored"):
            real = getattr(store, name)
            monkeypatch.setattr(store, name, lambda *a, real=real, name=name, **k: reads.append(name) or real(*a, **k))
        token = _secret(server, person, TOKEN, 99)
        _keep(server, person, [token])  # another instance's commit

        def refreshed() -> None:
            with vault.holding():  # a call: it looks at the store first
                pass

        await asyncio.to_thread(refreshed)
        assert reads == [] and vault.in_memory(token.id) is not None
    finally:
        store.close()


async def test_a_persons_conversation_loaded_elsewhere_asks_the_store_nothing_one_by_one(server, monkeypatch):
    """I3: a conversation another instance showed values loads none of them one query at a time, nor on the event loop:
    a person's vault on its store finds them by their words, and its answer's hold brings them back at once."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(300))
    async with listening(server):
        runtime = await _runtime(server, person)
        store = runtime.store
        asked: list[str] = []
        monkeypatch.setattr(store, "load_entity", lambda entity_id: asked.append(entity_id))
        ids = {f"e{n:019d}" for n in range(50)}
        spaces_module._learn_ids(runtime, store, ids)  # on the event loop, as the Live Reviewer's path calls it
        await asyncio.to_thread(spaces_module._learn_ids, runtime, store, ids)
        assert asked == []


async def test_the_names_of_a_persons_vault_are_read_a_part_at_a_time(server, monkeypatch):
    """I4: the names a person's filters reject are found a part of the vault at a time, never every name opened at
    once."""
    person, _ = server.identities.add_person("Emily Carter")
    names = [
        Entity(f"p{n:019d}", "PERSON", EntityClass.NER, f"Giulia Bianchi{n}", f"Sara Ricci{n}", 0, time.time() + n)
        for n in range(50)
    ]
    _keep(server, person, names)
    monkeypatch.setattr(pg, "ROWS_AT_ONCE", 10)
    store = _store(server, person)
    opened = [0]
    real = store._opened

    def counted(row):
        opened[0] += 1
        return real(row)

    monkeypatch.setattr(store, "_opened", counted)
    try:
        names_read = store.of_class(EntityClass.NER)
        first = next(iter(names_read))
        assert first.original == "Giulia Bianchi0" and opened[0] <= 10
        assert sum(1 for _ in names_read) == 49
    finally:
        store.close()


async def test_a_persons_whole_read_past_a_bound_is_shared_and_leaves_the_store_as_it_was(server, monkeypatch):
    """Task 8 review I-1: past a bound of its calls, a person's vault reads its store whole once for the calls at once
    and after it while the store holds the same, through a reader that changes nothing (no count set anew, no rule
    written); the read is weighed (``whole_count``) and masks as the whole vault did."""
    from latet.engine import lookup

    person, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, person.id)
    _keep(server, person, _emails(300))
    store = _store(server, person)
    reads, cuts = [], []
    real = store.whole_entities
    monkeypatch.setattr(store, "whole_entities", lambda: reads.append(1) or real())
    monkeypatch.setattr(store, "_cut_field_names", lambda cut: cuts.append(cut))
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 1)  # every call past a bound
    store.count, store.unreadable = 777, 3
    vault = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)
    text = f"login by {_email(42)} today"
    out: list[str] = []

    def call() -> None:
        masker = Masker(vault, [PiiDetector()])
        with vault.holding():
            vault.prefetch([text])
            out.append(masker.mask(text, SlotKind.TOOL_RESULT).text)

    try:
        await asyncio.to_thread(call)  # its first call keeps the rule a value was found by: the store moves once
        reads.clear()
        out.clear()
        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        await asyncio.to_thread(call)
    finally:
        store.close()
    assert out == [text.replace(_email(42), _fake(42))] * 3
    assert reads == [1] and not any(cuts) and (store.count, store.unreadable) == (777, 3)
    assert vault.whole_count == 300


async def test_a_persons_vault_page_and_reveal_hold_nothing_of_another(server):
    """I6 (T9-S5): two people on one server; one's page lists, counts, types and finds only its own values (those of one
    instant ordered by id), and its dashboard reveals none of the other's."""
    _working(server)
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    tied = time.time() - 600
    _keep(server, emily, [replace_created(e, tied) for e in _emails(30)])
    _keep(server, daniel, [Entity(f"d{n:019d}", "API_KEY", EntityClass.SECRET, f"tok_{n:02d}Zx9Qw8Er7Ty6",
                                  f"tok_{n:02d}Mn5Bv4Cx3Lk2", 0, tied) for n in range(5)])  # fmt: skip
    async with listening(server):
        runtime = await _runtime(server, emily)
        page = await asyncio.to_thread(runtime.gateway.dashboard._vault_page, "", "", "", 1, 10)
        assert page["total"] == 30 and page["types"] == {"EMAIL": 30}
        assert [item["id"] for item in page["items"]] == [f"e{n:019d}" for n in range(10)]
        assert (await asyncio.to_thread(runtime.gateway.dashboard._vault_page, "mn5bv4", "", "", 1, 10))["total"] == 0
        other = await runtime.gateway.dashboard.reveal(SimpleNamespace(path_params={"id": f"d{0:019d}"}))
        assert other.status_code == 404


def replace_created(entity: Entity, created: float) -> Entity:
    from dataclasses import replace

    return replace(entity, created=created)


async def test_a_persons_value_out_of_memory_is_forgotten_from_the_store(server):
    """I6: the dashboard's forget of a value out of the space's memory forgets it in PostgreSQL (its count of forgets
    moved), reveals it no more, and a text holding it again gets a new fake."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(300))
    gone = _gone(server, person)
    async with listening(server):
        runtime = await _runtime(server, person)
        assert runtime.built.vault.in_memory(f"e{0:019d}") is None
        assert await asyncio.to_thread(runtime.forget, f"e{0:019d}") is not None
        assert await asyncio.to_thread(runtime.store.by_original, _email(0)) is None
        answer = await runtime.gateway.dashboard.reveal(SimpleNamespace(path_params={"id": f"e{0:019d}"}))
        assert answer.status_code == 404
        left = await _leaves(runtime, f"login by {_email(0)}")
        assert _email(0) not in left and _fake(0) not in left
    assert _gone(server, person) == gone + 1


async def test_a_persons_words_job_stops_when_its_space_closes(server, monkeypatch):
    """I6 (T9-S2): a space dropped while its words are made: the job stops at its next part, the words stay
    incomplete, and the next build is served from memory again (and makes the rest)."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(300))
    with server.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (person.id,))
    monkeypatch.setattr(pg, "ROWS_AT_ONCE", 50)
    parted, go = threading.Event(), threading.Event()
    real = PgVaultStore._cut_field_names

    def paused(self, cut):
        real(self, cut)
        # In the words job alone (the fills' thread), after its first part: never in the space's build (n1).
        if threading.current_thread().name.startswith("latet-vault-prints") and not parted.is_set():
            parted.set()
            go.wait(30)

    monkeypatch.setattr(PgVaultStore, "_cut_field_names", paused)
    async with listening(server):
        runtime = await _runtime(server, person)
        store = runtime.store
        assert type(runtime.built.vault) is Vault
        assert await asyncio.to_thread(parted.wait, 30)
        dropping = asyncio.create_task(server.spaces.drop(person.id))
        await until(lambda: store.closed, seconds=30)
        go.set()
        await dropping
        assert not await asyncio.to_thread(_store(server, person).words_complete)
        monkeypatch.setattr(PgVaultStore, "_cut_field_names", real)
        again = await _runtime(server, person)
        assert type(again.built.vault) is Vault


async def test_a_persons_words_job_leaves_a_lease_held_elsewhere_and_says_a_failure_by_its_type(server, monkeypatch):
    """I6 (T9-S2, T9-S8): another instance holding the job: the space serves from memory and makes nothing; a job that
    fails says so by its type alone, never the words of its error, and the space keeps masking."""
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(50))
    with server.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (person.id,))
    filled: list[int] = []
    real = PgVaultStore.backfill_words
    monkeypatch.setattr(PgVaultStore, "backfill_words", lambda self: filled.append(1) or real(self))
    text = f"login by {_email(5)}"
    with server.bus.hold(f"words:{person.id}", 60) as mine:
        assert mine
        async with listening(server):
            runtime = await _runtime(server, person)
            await until(lambda: not server.spaces._index_tasks, seconds=30)  # the words job over (n1)
            assert filled == [] and await _leaves(runtime, text) == text.replace(_email(5), _fake(5))
            await server.spaces.drop(person.id)

    def failing(self):
        raise RuntimeError(f"the words of {CANARY} could not be made")

    monkeypatch.setattr(PgVaultStore, "backfill_words", failing)
    with captured_logs() as logs:
        async with listening(server):
            runtime = await _runtime(server, person)
            await until(lambda: "RuntimeError" in logs.getvalue(), seconds=30)
            assert await _leaves(runtime, text) == text.replace(_email(5), _fake(5))
    assert CANARY not in logs.getvalue() and _email(5) not in logs.getvalue()


async def test_a_commit_between_two_past_bound_turns_updates_the_shared_read(server, monkeypatch):
    """The re-review's R-I1: a person's whole read past a bound is shared by reference (no call copies it, and a
    snapshot a call still holds stays weighed), updated from what a commit named rather than read whole again (and masks
    as a whole read would), and read whole again after a forget, which names nothing."""
    from latet.engine import lookup

    person, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, person.id)
    _keep(server, person, [replace_created(e, time.time() - 600) for e in _emails(300)])
    store = PgVaultStore(server.db, person.id, key, index=True, limit=1_000_000)
    reads: list[int] = []
    real = store.whole_entities
    monkeypatch.setattr(store, "whole_entities", lambda: reads.append(1) or real())
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 1)  # every call past a bound
    try:
        assert store.build_index()
        store.current()
        vault = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)

        def masked(text: str, over: StoredVault = vault) -> str:
            masker = Masker(over, [PiiDetector()])
            with over.holding():
                over.prefetch([text])
                call = over._call()
                assert call is not None and len(call.candidates) < 300  # the whole vault not copied into the call
                return masker.mask(text, SlotKind.TOOL_RESULT).text

        first = f"login by {_email(7)}"
        assert await asyncio.to_thread(masked, first) == first.replace(_email(7), _fake(7))
        assert reads == [1] and vault.whole_count == 300
        new = Entity("x" + "0" * 19, "EMAIL", EntityClass.PII, FIRST, "zz.new.fake@posta.example", 0, time.time())
        _keep(server, person, [new])  # another instance's commit
        second = f"login by {FIRST} and {_email(8)}"
        here = await asyncio.to_thread(masked, second)
        assert reads == [1] and vault.whole_count == 301  # updated, not read whole again
        fresh = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)
        assert here == await asyncio.to_thread(masked, second, fresh)  # as a whole read masks it
        assert here == second.replace(FIRST, new.surrogate).replace(_email(8), _fake(8))
        other = _store(server, person)
        other.forget_entity(f"e{9:019d}")  # a forget names nothing: read whole again
        other.close()
        reads.clear()
        third = f"login by {_email(10)}"
        assert await asyncio.to_thread(masked, third) == third.replace(_email(10), _fake(10))
        assert reads == [1]
    finally:
        store.close()


def test_a_value_one_instance_showed_a_conversation_is_restored_by_the_other(instances, upstream):  # noqa: F811
    """n2: a person's conversation moving between instances, on its store: the answer B restores names a value A
    masked first, which B's memory never held; B's hold before the answer brings it back."""
    a, b = instances
    _working(a, b)
    person, token = _token_of(a, "Emily Carter")
    _keep(a, person, _emails(300))
    with TestClient(create_server_app(a), base_url=URL) as one, TestClient(create_server_app(b), base_url=URL) as two:
        said = _chat(one, token, [{"role": "user", "content": f"write to {FIRST} about the invoice"}])
        assert FIRST in said
        again = _chat(two, token, [{"role": "user", "content": f"write to {FIRST} about the invoice"}])
        assert FIRST in again and _stored(b, person)


def _masked_past(vault: StoredVault, text: str) -> str:
    """``text`` masked as a person's gateway masks it, its call past its lookup's bounds (the test's settings)."""
    masker = Masker(vault, [PiiDetector()])
    with vault.holding():
        vault.prefetch([text])
        return masker.mask(text, SlotKind.TOOL_RESULT).text


async def test_a_commit_caught_up_by_another_call_meanwhile_is_never_lost_from_the_shared_read(server, monkeypatch):
    """The re-review's R2-C1: one call's refresh is in its resync while another instance keeps V and another call
    catches V up; V is in every later past-bound call's whole read (masked as a fresh whole read masks it), never lost
    for good; and a value forgotten elsewhere is never masked with its retired fake."""
    from latet.engine import lookup

    person, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, person.id)
    _keep(server, person, [replace_created(e, time.time() - 600) for e in _emails(300)])
    store = PgVaultStore(server.db, person.id, key, index=True, limit=1_000_000)
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 1)  # every call past a bound
    try:
        assert store.build_index()
        store.current()
        vault = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)
        first = f"login by {_email(7)}"
        assert await asyncio.to_thread(_masked_past, vault, first) == first.replace(_email(7), _fake(7))
        entered, gate = threading.Event(), threading.Event()
        real = vault._resynced_now  # the resync of a call's refresh (``_caught_up``)

        def paused(*args, **kwargs):
            entered.set()
            gate.wait(30)
            return real(*args, **kwargs)

        monkeypatch.setattr(vault, "_resynced_now", paused)
        w = Entity("w" + "0" * 19, "EMAIL", EntityClass.PII, "walter.white@gmail.com", "ww.fake@posta.example", 0,
                   time.time())  # fmt: skip
        _keep(server, person, [w])  # a change the first call's refresh sees, and resyncs for

        def refreshed() -> None:
            with vault.holding():
                pass

        caller = threading.Thread(target=refreshed)
        caller.start()
        assert await asyncio.to_thread(entered.wait, 30)
        v = Entity("v" + "0" * 19, "PERSON", EntityClass.NER, NAME, NAME_FAKE, 0, time.time())  # found by no detector
        _keep(server, person, [v])  # another instance keeps V
        await asyncio.to_thread(store.current)  # and another call catches it up, meanwhile
        gate.set()
        await asyncio.to_thread(caller.join, 30)
        monkeypatch.setattr(vault, "_resynced_now", real)
        text = f"ticket closed by {NAME}, cc {_email(11)}"
        here = await asyncio.to_thread(_masked_past, vault, text)
        assert here == text.replace(NAME, NAME_FAKE).replace(_email(11), _fake(11))
        fresh = StoredVault(key, store, working=0, glued_all=True, whole_past_bounds=True)
        assert here == await asyncio.to_thread(_masked_past, fresh, text)
        other = _store(server, person)
        other.forget_entity(f"e{9:019d}")
        other.close()
        forgotten = f"login by {_email(9)}"
        left = await asyncio.to_thread(_masked_past, vault, forgotten)
        assert _email(9) not in left and _fake(9) not in left  # a new fake, never the retired one
    finally:
        store.close()


async def test_a_persons_detectors_take_the_keys_ceiling_on_a_small_budget(server, monkeypatch):
    """Task 8's fix round 9 for people, now on the stored vault (its ledger entry: a server's people get the key's
    budget, with its ceiling, once Task 9 moves them onto it). A person whose tool outputs log a fresh token of 16 hex
    digits in each line holds thousands of them in its detectors. On a server of 256 MiB (a share of 128), its parts
    take an eighth of the share at most past the ordinary RE2 budget, as an API key's do, the rest compiled at the
    ordinary budget (slower, exact): never the ceiling of the default budget (128 MiB, the whole share), and what they
    take never refuses a value."""
    monkeypatch.setattr(server.spaces, "budget", 256 << 20)
    person, _ = server.identities.add_person("Emily Carter")
    rng = random.Random(11)
    tokens = [f"{rng.getrandbits(64):016x}" for _ in range(5000)]
    async with listening(server):
        for at in range(0, len(tokens), 1000):
            batch = tokens[at : at + 1000]
            runtime = await _runtime(server, person)
            masked = await _leaves(runtime, "\n".join(f"session_token={token}" for token in batch))
            assert not any(token in masked for token in batch)
        vault = (await _runtime(server, person)).built.vault
        assert isinstance(vault, StoredVault) and vault.glued_all
        await asyncio.to_thread(vault.matchers.current)
        ceiling = int((256 << 20) * spaces_module.SPACE_SHARE * spaces_module.DETECTORS_SHARE)
        assert vault.detectors_extra_most == ceiling
        assert len(vault._matched) >= 4000
        assert vault.detectors_bytes <= ceiling  # a part of 2,000 tokens wants 32 MiB more: past 16, ordinary


async def test_what_a_persons_detectors_take_more_is_weighed_with_its_space(server):
    """The twin of round 8's key test for a person on the stored vault (the review of the merge of Task 9, M4): her
    parts are given ``SCALED_PER_BYTE`` too, as every text is read for them, and what that adds past the ordinary budget
    (``StoredVault.detectors_bytes``) is granted within the ceiling, counted in her space's size, never in its share."""
    person, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await _runtime(server, person)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault)
        await _leaves(runtime, "nothing to mask here")
        await asyncio.to_thread(vault.matchers.current)
        assert vault.detectors_bytes == 0  # no secret held: nothing more
        fixed = spaces_module._fixed(runtime) - vault.words_bytes
        hexes = [f"{n * 7919 + 104729:016x}"[::-1] for n in range(199)]  # one run of 16 hex digits each
        masked = await _leaves(runtime, "\n".join(f"password={value}" for value in hexes))
        assert not any(value in masked for value in hexes)
        await asyncio.to_thread(vault.matchers.current)
        assert len(vault._matched) >= 150
        extra = vault.detectors_bytes
        assert extra >= 1 << 20
        assert spaces_module.space_size(runtime) >= fixed + extra
        assert spaces_module._fixed(runtime) - vault.words_bytes < fixed + extra  # in its size, never in its share
