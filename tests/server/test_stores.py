"""An identity's vault and history in PostgreSQL: held to the contracts a computer's SQLite meets (E0a), each
identity's its own, sealed with its root key, and one entity for each value whichever instance masks it first.

What is sealed opens in its own row alone and for its own purpose: a value or a fake for its entry and while the row
says the type and the class it was found as, a review's answer for its text, a decision for its image, a block as a
block and an image as an image. The history keeps no conversation's key and no project's id in clear. Every change of a
vault the other instances hear of is counted with the identity's counter, in its own transaction."""

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest
import zstandard
from cryptography.exceptions import InvalidTag

from latet.engine.known import LET_GO
from latet.engine.masker import Masker
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, FakeTaken, Vault
from latet.server import history as history_module
from latet.server import vault as vault_module
from latet.server.history import PgHistoryStore
from latet.server.sealing import DERIVE, Sealer, derived, version_of
from latet.server.vault import PgVaultStore, UnreadableEntry, VaultFull, vault_usage
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ImageRecord

from ..conftest import ROOT_KEY
from ..unit.state.contracts import HistoryStoreContract, VaultStoreContract
from .conftest import dump, identity_in

pytestmark = pytest.mark.server

OTHER_KEY = b"\x08" * 32
CANARY = "emily.carter@acme.com"
ALONE = "emily.carter@gmail.com"  # its domain is a public one: the vault keeps no entry for it beside the address


def _summary(session: str = "s", **more):
    return {"ts": time.time(), "session": session, "agent": "codex", "flags": [], **more}


def _entity(n: str, value: str, fake: str) -> Entity:
    return Entity("e" + n * 19, "PASSWORD", EntityClass.SECRET, value, fake, 0, time.time(), None)


def _read(db, query, *args):
    with db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _write(db, query, *args):
    """A change made by hand: what someone who writes the database without the master key can do."""
    with db.connection() as conn:
        conn.execute(query, args)


def _counter(db, owner: str) -> int | None:
    """An identity's count of announced changes, as PostgreSQL has it."""
    found = _read(db, "SELECT counter FROM change_counters WHERE owner = %s", owner)
    return found[0][0] if found else None


def _until(check, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def _waiting_for_a_count(db) -> int:
    """How many connections wait for the lock of a change's count."""
    found = _read(
        db,
        "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        " AND query LIKE %s",
        "%change_counters%",
    )
    return found[0][0]


class TestPgVaultStore(VaultStoreContract):
    @pytest.fixture
    def stores(self, db):
        identity = identity_in(db)
        return PgVaultStore(db, identity, ROOT_KEY), PgVaultStore(db, identity, ROOT_KEY)

    @pytest.fixture
    def stranger(self, db, stores):
        return PgVaultStore(db, "u_emilycarte", OTHER_KEY)


class TestPgHistoryStore(HistoryStoreContract):
    @pytest.fixture
    def stores(self, db):
        identity = identity_in(db)
        return PgHistoryStore(db, identity, ROOT_KEY), PgHistoryStore(db, identity, ROOT_KEY)


def test_the_summaries_are_read_a_part_at_a_time_the_latest_first_and_no_more(db, monkeypatch):
    """S3 (area 3, I3): what the dashboard's stats read of a server's history, a part at a time and the latest ``most``
    alone, the identity's own alone."""
    monkeypatch.setattr(history_module, "SUMMARIES_AT_ONCE", 2)
    emily, daniel = identity_in(db), identity_in(db, "u_danielokaf", "Daniel Okafor")
    history, theirs = PgHistoryStore(db, emily, ROOT_KEY), PgHistoryStore(db, daniel, ROOT_KEY)
    now = time.time()
    for n in range(5):
        history.record(ExchangeRecord(_summary(ts=now - 100 + n, upstream=f"u{n}")))
    theirs.record(ExchangeRecord(_summary(ts=now, upstream="his")))
    parts = list(history.summaries_parts(now - 1000, 4))
    assert [[s["upstream"] for s in part] for part in parts] == [["u4", "u3"], ["u2", "u1"]]
    since = [s["upstream"] for part in history.summaries_parts(now - 98, 10) for s in part]
    assert since == ["u4", "u3", "u2"]


def test_the_stats_read_each_part_in_a_checkout_of_its_own(db, monkeypatch):
    """R4 (the re-review's Minor on S3): each part the stats read is a page of its own, read after the last one's time
    and sequence, its connection given back to the pool before the part is opened and aggregated: ten persons' stats at
    once hold none of the ten connections that the lookups, the vault's saves and readiness share. Two exchanges of one
    time are read once each, in the order they were kept."""
    monkeypatch.setattr(history_module, "SUMMARIES_AT_ONCE", 2)
    history, now = PgHistoryStore(db, identity_in(db), ROOT_KEY), time.time()
    for n in range(5):
        history.record(ExchangeRecord(_summary(ts=now - 100 + n, upstream=f"u{n}")))
    history.record(ExchangeRecord(_summary(ts=now - 98, upstream="tie")))  # the time of u2's, kept after it
    held, connection = [0], db.connection

    @contextlib.contextmanager
    def counted():
        held[0] += 1
        try:
            with connection() as conn:
                yield conn
        finally:
            held[0] -= 1

    monkeypatch.setattr(db, "connection", counted)
    during, seen = [], []
    for part in history.summaries_parts(now - 1000, 10):
        during.append(held[0])  # what the read holds while the part is opened and aggregated
        seen += [summary["upstream"] for summary in part]
    assert during == [0, 0, 0] and seen == ["u4", "u3", "tie", "u2", "u1", "u0"]


def test_an_identitys_change_counts_go_with_it(db):
    """S9 (area 1): an identity's two change counts, its vault's and its settings', are deleted with its row as the
    rest of its space is (their foreign key), whatever becomes of the purge; the shared settings' and another identity's
    stay; and no count is kept for an identity there is not."""
    emily, daniel = identity_in(db), identity_in(db, "u_danielokaf", "Daniel Okafor")
    Vault(ROOT_KEY, on_insert=PgVaultStore(db, emily, ROOT_KEY).save_entity).intern("EMAIL", EntityClass.PII, CANARY)
    for owner in (f"{emily}:settings", daniel, ""):
        _write(db, "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)", owner)
    owners = {row[0] for row in _read(db, "SELECT owner FROM change_counters")}
    assert owners == {emily, f"{emily}:settings", daniel, ""}
    _write(db, "DELETE FROM identities WHERE id = %s", emily)
    assert {row[0] for row in _read(db, "SELECT owner FROM change_counters")} == {daniel, ""}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _write(db, "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)", "u_nobodyhere:settings")


def test_an_entry_says_how_its_value_was_found_and_one_kept_before_says_none(db):
    """`decoded` (migration 005): whether a value was found where escapes read as what they stand for, in clear as an
    entry's rule is. An entry kept before the column, or by an instance whose INSERT names 0.9.9's columns, has none
    there: it loads as kept before (None), its reading made by 0.9.9's rule."""
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    found = vault.intern("PHONE", EntityClass.PII, "333+1234567", rule="known", reads="333 1234567")
    plain = vault.intern("PHONE", EntityClass.PII, "333 1234567", rule="pii:phone")
    rows = dict(_read(db, "SELECT id, decoded FROM vault_entities WHERE identity_id = %s", identity))
    assert rows == {found.id: True, plain.id: False}
    _write(db, "UPDATE vault_entities SET decoded = NULL WHERE id = %s", found.id)
    assert store.load_entity(found.id) == replace(found, decoded=None)
    with db.connection() as conn:
        conn.execute(
            _INSERT_099,
            (identity, "o" * 20, "PASSWORD", int(EntityClass.SECRET), b"x", b"k1", b"y", b"k2", 0, 0.0, None),
        )
    assert _read(db, "SELECT decoded FROM vault_entities WHERE id = %s", "o" * 20) == [(None,)]


# How an instance of Latet 0.9.9 keeps a value (server/vault.py at 0.9.9): it names the columns it writes.
_INSERT_099 = (
    "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate, surrogate_key,"
    " counter, created, rule) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
)


def test_the_migration_adds_how_a_value_was_found_to_a_database_of_0_9_9(settings, schema):
    """A database of 0.9.9's (four migrations, entries without the column) takes the fifth with `latet server
    migrate`; an instance refuses to start on it before (``check_schema``), and an entry kept before has none."""
    from latet.server.db import Database, SchemaMismatch, check_schema, migrate

    db = Database(settings.database_url, schema=schema, max_size=2)
    try:
        migrate(db)
        identity = identity_in(db)
        store = PgVaultStore(db, identity, ROOT_KEY)
        kept = Vault(ROOT_KEY, on_insert=store.save_entity).intern("PERSON", EntityClass.NER, "Emily Carter")
        with db.connection() as conn:
            conn.execute("ALTER TABLE vault_entities DROP COLUMN decoded")
            conn.execute("DELETE FROM schema_migrations WHERE name = 'latet.server.migrations:005_vault_decoded'")
        with pytest.raises(SchemaMismatch, match="older than this Latet"):
            check_schema(db)
        assert migrate(db) == ["latet.server.migrations:005_vault_decoded"]
        check_schema(db)
        assert store.load_entity(kept.id) == replace(kept, decoded=None)
    finally:
        db.close()


def test_one_value_found_as_two_types_by_two_instances_is_one_entity(db):
    identity = identity_in(db)
    mine = Vault(ROOT_KEY, on_insert=PgVaultStore(db, identity, ROOT_KEY).save_entity)
    theirs = Vault(ROOT_KEY, on_insert=PgVaultStore(db, identity, ROOT_KEY).save_entity)
    first = mine.intern("PASSWORD", EntityClass.SECRET, "hunter2-Lavender")
    second = theirs.intern("ASSIGNED_SECRET", EntityClass.SECRET, "hunter2-Lavender")
    assert second == first and second.etype == "PASSWORD"  # the first type wins, as in one vault's memory


def test_each_identitys_vault_and_history_are_its_own(db):
    """Daniel's stores, with the same root key as Emily's (each has its own on a server): only the identity tells
    them apart, and each method answers him nothing of hers and changes nothing of hers."""
    emily, daniel = identity_in(db), identity_in(db, "u_danielokaf", "Daniel Okafor")
    store, theirs = PgVaultStore(db, emily, ROOT_KEY), PgVaultStore(db, daniel, ROOT_KEY)
    entity = Vault(ROOT_KEY, on_insert=store.save_entity).intern("EMAIL", EntityClass.PII, CANARY)
    store.save_review(b"k" * 32, {"findings": [[CANARY, "email"]], "block": False})
    store.save_image_decision("f" * 64, {"rects": [[0, 0, 10, 10]], "withhold": False})
    assert theirs.load_entity(entity.id) is None and theirs.load_entities() == []
    assert theirs.load_review(b"k" * 32) is None and theirs.load_image_decision("f" * 64) is None
    theirs.forget_entity(entity.id)
    theirs.forget_reviews()
    assert store.load_entity(entity.id) == entity and store.load_review(b"k" * 32) is not None

    history, others = PgHistoryStore(db, emily, ROOT_KEY), PgHistoryStore(db, daniel, ROOT_KEY)
    text = f"write to {entity.surrogate}"
    block = BlockRecord(history.block_id(text), "$", "user_text", False, True, text, [])
    data = base64.b64encode(b"PNG, not really").decode()
    image = ImageRecord("$.messages[0]", "masked", history.image_id(data), "image/png", data)
    exchange = history.record(ExchangeRecord(_summary(upstream="openai"), [block], images=[image]))
    review = history.record_review(
        {"ts": time.time(), "session": "s", "project": None, "block": block.id, "kind": "user_text", "inline": True}
    )
    assert others.list() == ([], None) and others.detail(exchange) is None and others.image(image.id) is None
    assert others.sessions() == [] and others.in_order("s") == [] and others.reviews(session="s") == []
    assert others.known_blocks([block.id]) == set() and others.block_types([block.id]) == {}
    assert others.last_used() == {} and others.summaries_since(0) == []
    others.prune(now=time.time() + 400 * 86400)
    others.clear()
    assert history.detail(exchange) is not None and history.image(image.id) == ("image/png", b"PNG, not really")
    assert [r["id"] for r in history.reviews()] == [review] and history.known_blocks([block.id]) == {block.id}


def test_changes_to_a_vault_are_counted_with_them_and_told_with_their_count(db):
    told = []
    identity = identity_in(db)
    store = PgVaultStore(
        db, identity, ROOT_KEY, changed=lambda kind, entity, counter: told.append((kind, entity, counter)), limit=2
    )
    vault = Vault(ROOT_KEY, on_insert=store.save_entity, on_rule=store.save_rule)
    entity = vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com")
    vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com", rule="pii:email")
    store.forget_entity(entity.id)
    assert told == [("entity", entity.id, 1), ("rule", entity.id, 2), ("forgotten", entity.id, 3)]
    assert _counter(db, identity) == 3
    # Only a change is counted and told: not a fake refused, a value kept already, no rule, or a rule there already.
    first = _entity("1", "hunter2-Lavender", "pw_a1b2c3")
    assert store.save_entity(first) is None and _counter(db, identity) == 4
    with pytest.raises(FakeTaken):
        store.save_entity(_entity("2", "other", "hunter2-Lavender"))
    assert store.save_entity(first) == first
    store.save_rule(first)  # no rule to keep
    store.save_rule(replace(first, rule="secret:password"))
    store.save_rule(replace(first, rule="secret:assigned"))  # it has one already
    store.save_entity(_entity("3", "c.d@gmail.com", "pw_d4e5f6"))
    with pytest.raises(VaultFull):  # nor a value the vault has no room for
        store.save_entity(_entity("4", "e.f@gmail.com", "pw_g7h8i9"))
    assert _counter(db, identity) == 6 and [t[2] for t in told] == [1, 2, 3, 4, 5, 6]
    assert told[4] == ("rule", first.id, 5) and _counter(db, "") is None  # the shared settings' count is another


def test_a_change_to_a_vault_is_told_once_another_instance_can_read_it(db):
    """Told once its transaction has committed, never inside it: an instance that hears of a value kept or of its
    rule reads it, and one that hears of a value forgotten finds it gone."""
    identity = identity_in(db)
    elsewhere, seen = PgVaultStore(db, identity, ROOT_KEY), []

    def told(kind, entity_id, counter):
        found = elsewhere.load_entity(entity_id)  # what another instance reads on hearing it
        seen.append((kind, None if found is None else (found.etype, found.rule)))

    store = PgVaultStore(db, identity, ROOT_KEY, changed=told)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity, on_rule=store.save_rule)
    entity = vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com")
    vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com", rule="pii:email")
    store.forget_entity(entity.id)
    assert seen == [("entity", ("EMAIL", None)), ("rule", ("EMAIL", "pii:email")), ("forgotten", None)]


def test_a_change_to_a_vault_and_its_count_are_kept_together(db):
    """While its count waits (another change of the identity holds it), the value kept is not seen elsewhere: the
    two are one transaction, and an instance that reads the count reads what it counts."""
    told = []
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY, changed=lambda kind, entity, counter: told.append(counter))
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com")
    with ThreadPoolExecutor(1) as pool:
        with db.transaction() as conn:
            conn.execute("SELECT counter FROM change_counters WHERE owner = %s FOR UPDATE", (identity,))
            racing = pool.submit(vault.intern, "EMAIL", EntityClass.PII, "c.d@gmail.com")
            assert _until(lambda: _waiting_for_a_count(db) >= 1)
            assert len(PgVaultStore(db, identity, ROOT_KEY).load_entities()) == 1  # not kept yet
        racing.result(30)
    assert len(PgVaultStore(db, identity, ROOT_KEY).load_entities()) == 2 and told == [1, 2]


def test_nothing_of_a_vault_or_a_history_is_in_the_database_in_clear(db):
    identity = identity_in(db)
    store, history = PgVaultStore(db, identity, ROOT_KEY), PgHistoryStore(db, identity, ROOT_KEY)
    entity = Vault(ROOT_KEY, on_insert=store.save_entity).intern("EMAIL", EntityClass.PII, CANARY)
    store.save_review(b"k" * 32, {"findings": [[CANARY, "email"]], "block": False})
    text = f"write to {entity.surrogate} about billing-api"
    block = BlockRecord(history.block_id(text), "$", "user_text", False, True, text, [])
    summary = _summary(project={"id": "p1", "name": "billing-api"})
    response = {"headers": [], "text": f"You said: {text}", "text_spans": [], "tool_calls": []}
    history.record(ExchangeRecord(summary, [block], request_headers=[("x-note", CANARY)], response=response))
    seen = dump(db)
    for clear in (CANARY, entity.surrogate, "billing-api"):
        assert clear.encode() not in seen


def test_the_name_of_the_field_a_value_sat_in_is_in_the_database_nowhere_in_clear(db):
    """A JSON key can be a value itself (an address): the rule that found a value by its field names that field, and the
    vault keeps each value's rule in clear. It keeps the field's kind alone, and a rule kept with a field's name before
    is cut to its kind once the vault is read."""
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    vault.intern("DATA", EntityClass.STRUCT, "VIP customer since 2019", rule="strict:olivia.bennett@northwind.com")
    vault.intern("FIELD", EntityClass.STRUCT, "gold plan member", rule="field:marco.ricci@northwind.com")
    older = vault.intern("PASSWORD", EntityClass.SECRET, "hunter2222", rule="field-secret:sofia rossi password")
    seen = dump(db)
    for clear in ("olivia", "bennett", "northwind", "marco", "ricci", "sofia", "rossi"):
        assert clear.encode() not in seen
    assert sorted(e.rule for e in store.load_entities()) == ["field", "field-secret:password", "strict"]
    _write(db, "UPDATE vault_entities SET rule = %s WHERE id = %s", "field-secret:sofia rossi password", older.id)
    assert {e.id: e.rule for e in store.load_entities()}[older.id] == "field-secret:password"
    assert b"sofia" not in dump(db)  # cut where it was kept, too
    given = _entity("g", "blue tier", "Zmq9")  # given to the store as it is, not through a vault
    store.save_entity(replace(given, rule="strict:olivia.bennett@northwind.com"))
    unruled = _entity("u", "red tier", "Wpq7")
    store.save_entity(unruled)
    store.save_rule(replace(unruled, rule="field:marco.ricci@northwind.com"))
    assert b"olivia" not in dump(db) and b"marco" not in dump(db)


def test_the_history_keeps_no_conversations_key_or_projects_id_in_clear_and_its_filters_find_them(db):
    history = PgHistoryStore(db, identity_in(db), ROOT_KEY)
    session, project = "conv-4be1f0c97d2a", "5e2d7c9a1b3f"  # a conversation's key; the id of a project's folder
    exchange = history.record(ExchangeRecord(_summary(session, project={"id": project, "name": "billing-api"})))
    other = history.record(ExchangeRecord(_summary("conv-other")))
    review = history.record_review(
        {"ts": time.time(), "session": session, "project": project, "block": "b" * 32, "kind": "user_text",
         "inline": True, "findings": []}
    )  # fmt: skip
    seen = dump(db)
    assert session.encode() not in seen and project.encode() not in seen
    assert [s["id"] for s in history.list(session=session)[0]] == [exchange]
    assert [s["id"] for s in history.list(project=project)[0]] == [exchange]
    assert history.list(project="000000000000") == ([], None) and history.in_order(session) == [exchange]
    assert [r["id"] for r in history.reviews(session=session)] == [review]
    assert sorted((s["key"], s["exchanges"]) for s in history.sessions()) == [
        ("conv-4be1f0c97d2a", 1),
        ("conv-other", 1),
    ]
    assert history.in_order("conv-other") == [other]


def test_the_history_seals_as_every_part_of_a_server_does(db):
    """Through the sealer of its root key: the frame names the key's version, and the frame is sealed with the rest."""
    identity = identity_in(db)
    exchange = PgHistoryStore(db, identity, ROOT_KEY).record(ExchangeRecord(_summary()))
    [(sealed,)] = _read(db, "SELECT summary FROM exchanges")
    assert version_of(bytes(sealed)) == 1
    opened = Sealer(ROOT_KEY, b"exchanges").open(bytes(sealed), f"{identity}:{exchange}:summary")
    assert json.loads(zstandard.ZstdDecompressor().decompress(opened))["id"] == exchange


def test_a_block_and_an_image_of_the_history_do_not_open_as_each_other(db):
    history = PgHistoryStore(db, identity_in(db), ROOT_KEY)
    text = "write to Vega-7 about the outage"
    block = BlockRecord(history.block_id(text), "$", "user_text", False, True, text, [])
    data = base64.b64encode(b"PNG, not really").decode()
    image = ImageRecord("$.messages[0]", "masked", history.image_id(data), "image/png", data)
    history.record(ExchangeRecord(_summary(), [block], images=[image]))
    # Each copied by hand into the other's table, under its own id.
    _write(
        db, "INSERT INTO images (identity_id, id, data, last_seen) SELECT identity_id, id, data, last_seen FROM blocks"
    )
    _write(
        db,
        "INSERT INTO blocks (identity_id, id, kind, data, last_seen)"
        " SELECT identity_id, id, 'user_text', data, last_seen FROM images WHERE id = %s",
        image.id,
    )
    with pytest.raises(InvalidTag):
        history.image(block.id)
    with pytest.raises(InvalidTag):
        history.block_types([image.id])


def test_a_reviews_answer_or_an_image_decision_moved_to_another_row_opens_to_nothing(db):
    """ "Nothing to mask" for one text, or "send it as it is" for one image, never becomes the verdict on another."""
    store = PgVaultStore(db, identity_in(db), ROOT_KEY)
    store.save_review(b"a" * 32, {"findings": [], "block": False})
    store.save_review(b"b" * 32, {"findings": [[CANARY, "email"]], "block": True})
    store.save_image_decision("a" * 64, {"rects": [], "withhold": False})
    store.save_image_decision("b" * 64, {"rects": [], "withhold": True})
    assert store.load_review(b"b" * 32) == {"findings": [[CANARY, "email"]], "block": True}
    assert store.load_image_decision("b" * 64) == {"rects": [], "withhold": True}
    for table in ("reviews", "image_decisions"):  # the two rows of each table swap what they hold
        (first, one), (second, two) = _read(db, f"SELECT key, data FROM {table}")
        _write(db, f"UPDATE {table} SET data = %s WHERE key = %s", two, first)
        _write(db, f"UPDATE {table} SET data = %s WHERE key = %s", one, second)
    assert store.load_review(b"a" * 32) is None and store.load_review(b"b" * 32) is None
    assert store.load_image_decision("a" * 64) is None and store.load_image_decision("b" * 64) is None


def test_the_names_of_image_decisions_and_of_blocks_come_from_keys_derived_for_them(db):
    """By the derivation every key of a server is made with, apart from the keys that seal and those that hash."""
    identity = identity_in(db)
    PgVaultStore(db, identity, ROOT_KEY).save_image_decision("f" * 64, {"rects": [], "withhold": True})
    named = hmac.new(derived(ROOT_KEY, DERIVE, b"image-decisions"), b"f" * 64, hashlib.sha256).digest()
    assert [bytes(row[0]) for row in _read(db, "SELECT key FROM image_decisions")] == [named]
    ids = derived(ROOT_KEY, DERIVE, b"exchange-ids")
    history = PgHistoryStore(db, identity, ROOT_KEY)
    assert history.block_id("abc") == hmac.new(ids, b"abc", hashlib.sha256).hexdigest()[:32]
    assert history.image_id("abc") == "im_" + hmac.new(ids, b"image\x00abc", hashlib.sha256).hexdigest()[:32]


def test_an_entry_whose_type_or_class_was_changed_by_hand_does_not_open(db):
    """A class set to STRUCT would leave the value out of the values the masker knows: the entry is unreadable instead,
    counted and said."""
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    email = vault.intern("EMAIL", EntityClass.PII, ALONE)
    secret = vault.intern("PASSWORD", EntityClass.SECRET, "hunter2-Lavender")
    assert len(PgVaultStore(db, identity, ROOT_KEY).load_entities()) == 2
    _write(db, "UPDATE vault_entities SET eclass = %s WHERE id = %s", int(EntityClass.STRUCT), email.id)
    _write(db, "UPDATE vault_entities SET etype = 'NUMBER' WHERE id = %s", secret.id)
    fresh = PgVaultStore(db, identity, ROOT_KEY)
    assert fresh.load_entities() == [] and fresh.unreadable == 2


def test_a_value_or_a_fake_moved_to_another_entry_or_to_its_other_part_does_not_open(db):
    """S12 (area 5, Important 1): a value and its fake are sealed for their identity, their entry, which of the two
    they are, their type and their class (``PgVaultStore._aad``). Two entries of one identity, type and class whose
    sealed values a writer of the database swaps open neither (each fake would restore as the other person's value);
    and an entry whose value and fake are swapped does not open (the masker would replace the value with itself, and
    the request leave unmasked). Each is counted, and nothing of it is used."""
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    emily = vault.intern("EMAIL", EntityClass.PII, "emily.carter@gmail.com")
    daniel = vault.intern("EMAIL", EntityClass.PII, "daniel.okafor@gmail.com")
    oliver = vault.intern("EMAIL", EntityClass.PII, "oliver.jackson@gmail.com")
    assert len(PgVaultStore(db, identity, ROOT_KEY).load_entities()) == 3
    (one,), (two,) = (_read(db, "SELECT original FROM vault_entities WHERE id = %s", e.id)[0] for e in (emily, daniel))
    _write(db, "UPDATE vault_entities SET original = %s WHERE id = %s", two, emily.id)  # each other's values
    _write(db, "UPDATE vault_entities SET original = %s WHERE id = %s", one, daniel.id)
    _write(db, "UPDATE vault_entities SET original = surrogate, surrogate = original WHERE id = %s", oliver.id)
    fresh = PgVaultStore(db, identity, ROOT_KEY)
    assert fresh.load_entities() == [] and fresh.unreadable == 3
    for entity in (emily, daniel, oliver):
        with pytest.raises(UnreadableEntry):
            fresh.load_entity(entity.id)


def test_a_vault_row_is_sealed_for_its_identity_its_entry_its_part_its_type_and_its_class(db):
    """S12: the binding pinned as it is written (as the settings pin ``"{id}:own"``): a row sealed by hand for exactly
    those opens, and one sealed for anything less does not."""
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    entity = _entity("1", "hunter2-Lavender", "Kestrel-Fable-77")
    store.save_entity(entity)
    sealer = Sealer(ROOT_KEY, b"storage")
    for part, value in (("original", entity.original), ("surrogate", entity.surrogate)):
        bound = json.dumps(["vault", identity, entity.id, part, entity.etype, int(entity.eclass)])
        _write(
            db, f"UPDATE vault_entities SET {part} = %s WHERE id = %s", sealer.seal(value.encode(), bound), entity.id
        )
    assert PgVaultStore(db, identity, ROOT_KEY).load_entity(entity.id) == entity
    loose = json.dumps(["vault", identity, entity.etype, int(entity.eclass)])  # no entry, no part
    _write(
        db, "UPDATE vault_entities SET original = %s WHERE id = %s", sealer.seal(b"hunter2-Lavender", loose), entity.id
    )
    with pytest.raises(UnreadableEntry):
        PgVaultStore(db, identity, ROOT_KEY).load_entity(entity.id)


def test_an_entry_that_does_not_open_is_told_from_none_counted_once_and_its_value_still_masked(db, caplog):
    identity = identity_in(db)
    entity = Vault(ROOT_KEY, on_insert=PgVaultStore(db, identity, ROOT_KEY).save_entity).intern(
        "EMAIL", EntityClass.PII, ALONE
    )
    [(sealed,)] = _read(db, "SELECT original FROM vault_entities WHERE id = %s", entity.id)
    _write(
        db, "UPDATE vault_entities SET original = %s WHERE id = %s", sealed[:-1] + bytes([sealed[-1] ^ 1]), entity.id
    )
    store = PgVaultStore(db, identity, ROOT_KEY)
    with caplog.at_level(logging.WARNING, logger="latet.server.vault"):
        assert store.load_entities() == [] and store.unreadable == 1
        with pytest.raises(UnreadableEntry):
            store.load_entity(entity.id)
        assert store.load_entity("0" * 20) is None  # no entry at all: none
        # Found again, the value is masked as it is found, in this vault's memory, where it was "the fake is taken"
        # sixteen times and the request stopped.
        again = Vault(ROOT_KEY, on_insert=store.save_entity).intern("EMAIL", EntityClass.PII, ALONE)
        assert (again.id, again.surrogate) == (entity.id, entity.surrogate)
        with pytest.raises(UnreadableEntry):
            store.load_entity(entity.id)
    assert store.unreadable == 1  # one entry, however often it is met
    said = [record.getMessage() for record in caplog.records if record.name == "latet.server.vault"]
    assert len(said) == 1 and entity.id in said[0] and "emily" not in said[0]


def test_a_fake_is_never_a_string_the_vault_knows_and_a_value_equal_to_a_fake_stays_a_value(db):
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    first = Entity(
        "e" + "1" * 19, "PASSWORD", EntityClass.SECRET, "hunter2-Lavender", "pw_a1b2c3", 0, time.time(), None
    )
    assert store.save_entity(first) is None
    taken = Entity("e" + "2" * 19, "EMAIL", EntityClass.PII, "a.b@gmail.com", "hunter2-Lavender", 0, time.time(), None)
    with pytest.raises(FakeTaken):  # its fake is a value already known: the vault tries its next counter
        store.save_entity(taken)
    assert store.load_entity(taken.id) is None  # nothing of it was kept
    real = Entity("e" + "3" * 19, "PASSWORD", EntityClass.SECRET, "pw_a1b2c3", "pw_d4e5f6", 0, time.time(), None)
    assert store.save_entity(real) is None and store.load_entity(real.id) == real  # real, though a fake's twin
    with pytest.raises(FakeTaken):  # and that fake stays another value's
        store.save_entity(
            Entity("e" + "4" * 19, "PASSWORD", EntityClass.SECRET, "other", "pw_d4e5f6", 0, time.time(), None)
        )
    store.forget_entity(first.id)  # its strings go with it: the fake may be given again
    assert (
        store.save_entity(
            Entity("e" + "5" * 19, "EMAIL", EntityClass.PII, "c.d@gmail.com", "hunter2-Lavender", 0, time.time(), None)
        )
        is None
    )


def test_a_value_equal_to_a_fake_stays_known_when_that_fakes_entry_is_forgotten(db):
    store = PgVaultStore(db, identity_in(db), ROOT_KEY)
    first, real = _entity("1", "hunter2-Lavender", "pw_a1b2c3"), _entity("3", "pw_a1b2c3", "pw_d4e5f6")
    assert store.save_entity(first) is None and store.save_entity(real) is None
    store.forget_entity(first.id)
    with pytest.raises(FakeTaken):  # pw_a1b2c3 is a value the vault still knows: real's
        store.save_entity(_entity("6", "x.y@gmail.com", "pw_a1b2c3"))
    store.forget_entity(real.id)  # and it goes with real
    assert store.save_entity(_entity("6", "x.y@gmail.com", "pw_a1b2c3")) is None


def test_a_fake_that_was_a_value_forgotten_since_goes_with_its_own_entry(db):
    store = PgVaultStore(db, identity_in(db), ROOT_KEY)
    first, real = _entity("1", "hunter2-Lavender", "pw_a1b2c3"), _entity("3", "pw_a1b2c3", "pw_d4e5f6")
    assert store.save_entity(first) is None and store.save_entity(real) is None
    store.forget_entity(real.id)  # the value goes first: pw_a1b2c3 is first's fake alone again
    with pytest.raises(FakeTaken):
        store.save_entity(_entity("6", "x.y@gmail.com", "pw_a1b2c3"))
    store.forget_entity(first.id)  # and free once first goes: nothing is left of the value forgotten before
    assert store.save_entity(_entity("6", "x.y@gmail.com", "pw_a1b2c3")) is None


def _old_fake(db, identity: str, store: PgVaultStore) -> Entity:
    """An entry kept before the vault refused a fake that shows its value: `emily` and `emily59`."""
    entity_id = Vault(ROOT_KEY).entity_id("USERNAME", "emily")
    old = Entity(entity_id, "USERNAME", EntityClass.IDENTITY, "emily", "emily59", 0, time.time(), "identity:username")
    assert store.save_entity(old) is None
    return old


def test_a_fake_drawn_again_at_load_is_sealed_for_its_entry_counted_and_told(db):
    identity, told = identity_in(db), []
    store = PgVaultStore(db, identity, ROOT_KEY, changed=lambda kind, entity, counter: told.append((kind, entity)))
    old = _old_fake(db, identity, store)
    before, said = _counter(db, identity), len(told)
    vault = Vault(ROOT_KEY, store.load_entities(), on_insert=store.save_entity, on_replace=store.replace_fake)
    new = vault.by_original("emily")
    assert new is not None and "emily" not in new.surrogate.casefold()
    assert told[said:] == [("entity", old.id)] and _counter(db, identity) == (before or 0) + 1
    # Sealed for its entry as every fake is: nowhere in clear, and opened in its own row alone.
    assert new.surrogate.encode() not in dump(db) and PgVaultStore(db, identity, ROOT_KEY).load_entity(old.id) == new
    # A string of the vault's, the entry's fake, in place of the old one's.
    keyed = Sealer(ROOT_KEY, b"storage").keyed
    strings = "SELECT role, entity_id FROM vault_strings WHERE identity_id = %s AND key = %s"
    assert _read(db, strings, identity, keyed(new.surrogate)) == [("surrogate", old.id)]
    assert _read(db, strings, identity, keyed("emily59")) == []
    # The old fake is no string of the vault's any more: another value may be given it.
    assert store.save_entity(_entity("9", "z.z@gmail.com", "emily59")) is None
    with pytest.raises(FakeTaken):  # and the new one is the entry's
        store.save_entity(_entity("8", "y.y@gmail.com", new.surrogate))


def test_an_old_fake_that_is_another_entrys_value_as_well_stays_that_values(db):
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    _old_fake(db, identity, store)
    real = _entity("3", "emily59", "pw_d4e5f6")  # a user really named so: a value equal to the old fake
    assert store.save_entity(real) is None
    Vault(ROOT_KEY, store.load_entities(), on_replace=store.replace_fake)
    with pytest.raises(FakeTaken):  # emily59 is still a value the vault knows: real's
        store.save_entity(_entity("6", "x.y@gmail.com", "emily59"))
    assert store.load_entity(real.id) == real


def test_a_vault_at_its_limit_keeps_no_new_value_and_the_request_is_stopped(db):
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY, limit=2)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com")
    vault.intern("EMAIL", EntityClass.PII, "c.d@gmail.com")
    with pytest.raises(VaultFull, match="holds 2 values, and this server allows 2 at most"):
        vault.intern("EMAIL", EntityClass.PII, "e.f@gmail.com")
    assert (
        store.count == 2 and vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com") is not None
    )  # known: no room needed
    assert vault_usage(db, 2) == [(identity, 2)] and vault_usage(db, 3) == [] and vault_usage(db, 1000) == []


def _stored(db, identity: str) -> int:
    """What an identity's history keeps, in bytes, read from its rows as its limit counts them: each row's sealed
    values, and 256 bytes for the rest of it."""
    [(found,)] = _read(
        db,
        "SELECT (SELECT COALESCE(SUM(octet_length(summary) + COALESCE(octet_length(refs), 0)"
        " + COALESCE(octet_length(request_headers), 0) + COALESCE(octet_length(response), 0)"
        " + COALESCE(octet_length(leaks), 0) + COALESCE(octet_length(images), 0) + 256), 0)"
        " FROM exchanges WHERE identity_id = %s)"
        " + (SELECT COALESCE(SUM(octet_length(data) + 256), 0) FROM blocks WHERE identity_id = %s)"
        " + (SELECT COALESCE(SUM(octet_length(data) + 256), 0) FROM images WHERE identity_id = %s)"
        " + (SELECT COALESCE(SUM(octet_length(summary) + 256), 0) FROM review_records WHERE identity_id = %s)",
        identity, identity, identity, identity,
    )  # fmt: skip
    return int(found)


def _counted(db, identity: str) -> int:
    """What the history's own count says it keeps."""
    found = _read(db, "SELECT bytes FROM history_bytes WHERE identity_id = %s", identity)
    return int(found[0][0]) if found else 0


def _exchange(history, n: int, at: float) -> tuple[str, str]:
    """An exchange of about four KiB once sealed (a text that compresses little): its id and its block's."""
    text = f"block {n}: " + base64.b64encode(os.urandom(2400)).decode()
    block = BlockRecord(history.block_id(text), "$", "user_text", False, True, text, [])
    return history.record(ExchangeRecord(_summary(ts=at), [block])), block.id


def test_a_history_past_its_limit_drops_its_oldest_exchanges_and_keeps_the_newest(db, caplog, clock):
    emily, daniel = identity_in(db), identity_in(db, "u_danielokaf", "Daniel Okafor")
    history = PgHistoryStore(db, emily, ROOT_KEY, limit=16_000, clock=clock)
    theirs = PgHistoryStore(db, daniel, ROOT_KEY)  # the server's own limit: a gibibyte
    now = time.time()
    their_exchange, _ = _exchange(theirs, 0, now - 3600)
    with caplog.at_level(logging.WARNING, logger="latet.server.history"):
        recorded = [_exchange(history, n, now + n) for n in range(10)]
        history.record_review(
            {"ts": now + 10, "session": "s", "project": None, "block": recorded[-1][1], "kind": "user_text",
             "inline": True, "findings": []}
        )  # fmt: skip
        newest, _ = _exchange(history, 11, now + 11)
        kept = [s["id"] for s in history.list()[0]]  # the newest first
        ids = [exchange for exchange, _ in recorded] + [newest]
        assert 2 <= len(kept) < 11 and kept == ids[::-1][: len(kept)]  # the newest kept, the oldest gone
        gone = [block for exchange, block in recorded if exchange not in kept]
        assert history.known_blocks(gone) == set()  # with the blocks only they carried
        assert _stored(db, emily) == _counted(db, emily) <= 16_000  # counted from what is stored
        assert theirs.detail(their_exchange) is not None  # another identity's history is its own
        assert _stored(db, daniel) == _counted(db, daniel)
        said = [record.getMessage() for record in caplog.records if record.name == "latet.server.history"]
        assert len(said) == 1 and emily in said[0] and "history_limit_mb" in said[0]  # one line a minute at most
        clock.now += 61
        _exchange(history, 12, now + 12)
        _exchange(history, 13, now + 13)
        _exchange(history, 14, now + 14)
    said = [record.getMessage() for record in caplog.records if record.name == "latet.server.history"]
    assert len(said) == 2 and "since the line before" in said[1]
    history.prune(now=now + 30 * 86400)  # past the retention: everything goes, and the count with it
    assert history.list() == ([], None) and _stored(db, emily) == _counted(db, emily) == 0
    _exchange(history, 15, now + 15)
    history.clear()
    assert _stored(db, emily) == _counted(db, emily) == 0


def test_a_history_whose_newest_exchange_is_larger_than_its_limit_keeps_that_one_whole(db, clock):
    identity = identity_in(db)
    history = PgHistoryStore(db, identity, ROOT_KEY, limit=1_000, clock=clock)
    now = time.time()
    for n in range(3):
        newest, block = _exchange(history, n, now + n)
        detail = history.detail(newest)
        assert [s["id"] for s in history.list()[0]] == [newest]  # the others went
        assert detail is not None and [s["block"] for s in detail["request"]["slots"]] == [block]  # with its block
    assert _stored(db, identity) == _counted(db, identity) > 1_000


def test_a_count_of_the_history_that_drifted_past_its_limit_is_made_again_from_what_it_keeps(db, clock):
    """Rows taken out of a history some other way than through its store leave its count above what it keeps: when the
    count says it is past its limit and there is nothing to cut, it is counted again from what is kept, under the
    count's own row lock, and nothing is dropped for it."""
    identity = identity_in(db)
    history = PgHistoryStore(db, identity, ROOT_KEY, limit=16_000, clock=clock)
    now = time.time()
    _exchange(history, 0, now)
    _write(db, "UPDATE history_bytes SET bytes = bytes + 1000000 WHERE identity_id = %s", identity)  # it drifted
    _exchange(history, 1, now + 1)  # past its limit by the count: room is to be made, and there is nothing to cut
    assert _counted(db, identity) == _stored(db, identity) <= 16_000
    assert len(history.list()[0]) == 2


def test_a_count_made_again_waits_for_a_change_of_the_history_under_way(db, clock):
    """The count is read under its own row lock before the rows are summed: a change under way (one that holds that row
    at its last statement) is waited for, never summed without its rows and then written over its count."""
    identity = identity_in(db)
    history = PgHistoryStore(db, identity, ROOT_KEY, limit=16_000, clock=clock)
    _exchange(history, 0, time.time())
    _write(db, "UPDATE history_bytes SET bytes = bytes + 1000000 WHERE identity_id = %s", identity)
    with ThreadPoolExecutor(1) as pool, db.transaction() as conn:
        conn.execute("UPDATE history_bytes SET bytes = bytes WHERE identity_id = %s", (identity,))  # a change's count
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        recounting = pool.submit(history._make_room, time.time())
        waiting = "SELECT query FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)) AND query LIKE '%%history_bytes%%'"
        assert _until(lambda: _read(db, waiting, pid))
        [(query,)] = _read(db, waiting, pid)
        assert query.startswith("SELECT bytes FROM history_bytes")  # held at the read of the count, before the sum
    recounting.result(30)
    assert _counted(db, identity) == _stored(db, identity)


def test_an_exchange_kept_by_a_clock_behind_moves_no_block_or_image_back_in_time(db, monkeypatch):
    """Instances whose clocks differ carry the same block and image: each keeps the latest time it was carried at, so
    that no exchange that carried it is newer than it (making room drops what was kept before a cut)."""
    identity = identity_in(db)
    history = PgHistoryStore(db, identity, ROOT_KEY)
    now = [2_000_000_100.0]
    monkeypatch.setattr(history_module, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    text = "write to EMAIL_1 about billing-api"
    image = ImageRecord("$", "masked", history.image_id("aGk="), "image/png", "aGk=")
    history.record(
        ExchangeRecord(_summary(), [BlockRecord(history.block_id(text), "$", "user_text", False, True, text)], [image])
    )
    now[0] = 2_000_000_000.0  # another instance, whose clock is behind, carries both again
    carried = BlockRecord(history.block_id(text), "$", "user_text", False, False)
    history.record(ExchangeRecord(_summary(), [carried], [ImageRecord("$", "masked", image.id)]))
    assert _read(db, "SELECT last_seen FROM blocks WHERE identity_id = %s", identity) == [(2_000_000_100.0,)]
    assert _read(db, "SELECT last_seen FROM images WHERE identity_id = %s", identity) == [(2_000_000_100.0,)]


def test_a_history_that_cannot_make_room_says_so_once_a_minute(db, clock, monkeypatch, caplog):
    """Every exchange recorded past the limit tries to make room again: a failure is said by its type, the first at
    once and then once a minute with how many since the line before, never in the driver's words."""
    identity = identity_in(db)
    history = PgHistoryStore(db, identity, ROOT_KEY, limit=1_000, clock=clock)

    def fails(*args):
        raise RuntimeError("the driver's words, which name db.acme.lan")

    monkeypatch.setattr(history, "_drop", fails)
    now = time.time()
    with caplog.at_level(logging.WARNING, logger="latet.server.history"):
        for n in range(4):
            _exchange(history, n, now + n)
        said = [record.getMessage() for record in caplog.records if record.name == "latet.server.history"]
        assert len(said) == 1 and "RuntimeError" in said[0] and identity in said[0]
        clock.now += 61
        _exchange(history, 9, now + 9)
    said = [record.getMessage() for record in caplog.records if record.name == "latet.server.history"]
    assert len(said) == 2 and "4 times since the line before" in said[1] and "db.acme.lan" not in caplog.text
    assert len(history.list()[0]) == 5  # every exchange was kept all the same


def test_a_vault_over_its_limit_masks_with_every_value_it_holds_learns_none_and_says_so(db, caplog):
    """Over its limit (lowered since, or passed by instances at once), a vault is loaded whole: a value only the vault
    knows (found once by its context, then written alone) is masked; a new value stops the request."""
    identity = identity_in(db)
    earlier = Vault(ROOT_KEY, on_insert=PgVaultStore(db, identity, ROOT_KEY).save_entity)
    for n in range(3):
        earlier.intern("EMAIL", EntityClass.PII, f"person{n}@gmail.com")
    secret = earlier.intern("PASSWORD", EntityClass.SECRET, "hunter2-Lavender")  # the newest of four
    store = PgVaultStore(db, identity, ROOT_KEY, limit=2)
    with caplog.at_level(logging.WARNING, logger="latet.server.vault"):
        vault = Vault(ROOT_KEY, store.load_entities(), on_insert=store.save_entity)
    masked = Masker(vault, []).mask("and the password: hunter2-Lavender")  # no detector: the vault alone finds it
    assert masked.text == f"and the password: {secret.surrogate}"
    assert store.count == 4 and vault.get(secret.id) == secret
    with pytest.raises(VaultFull, match="holds 4 values, and this server allows 2 at most \\(vault_limit\\)"):
        vault.intern("EMAIL", EntityClass.PII, "new.person@gmail.com")  # the words say what it holds
    said = [record.getMessage() for record in caplog.records if record.name == "latet.server.vault"]
    assert len(said) == 1 and identity in said[0] and "2 over" in said[0] and "hunter2" not in said[0]


def test_a_vault_is_read_a_part_of_its_rows_at_a_time_the_gil_let_go_between_two(db, monkeypatch):
    """A space's build reads its whole vault off the event loop, but every row made Python's in one call held the loop
    every identity of the instance shares for 0.2 s at the limit (200,000 values, M-09): rows are fetched and opened
    ``ROWS_AT_ONCE`` at a time, the GIL let go between two. Every value is read, in its order, and an entry that does
    not open is counted."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 100, raising=False)
    identity = identity_in(db)
    store = PgVaultStore(db, identity, ROOT_KEY)
    kept = [_entity(f"{n % 10}", f"value-{n}-Lavender", f"fake-{n}-Heron") for n in range(250)]
    kept = [replace(e, id=f"e{n:019d}") for n, e in enumerate(kept)]
    for entity in kept[:120]:
        assert store.save_entity(entity) is None
    PgVaultStore(db, identity, b"\x09" * 32).save_entity(replace(kept[0], id="e" + "x" * 19, original="other"))
    for entity in kept[120:]:
        assert store.save_entity(entity) is None
    fetched: list[int] = []
    fetch = psycopg.ServerCursor.fetchmany

    def counted(cursor, size=0):
        rows = fetch(cursor, size)
        fetched.append(len(rows))
        return rows

    opened: list[int] = []
    let_go: list[tuple[int, float]] = []
    open_row = store._opened
    monkeypatch.setattr(psycopg.ServerCursor, "fetchmany", counted)
    monkeypatch.setattr(store, "_opened", lambda row: (opened.append(1), open_row(row))[1])
    monkeypatch.setattr(
        vault_module, "time", SimpleNamespace(time=time.time, sleep=lambda s: let_go.append((len(opened), s)))
    )
    assert store.load_entities() == kept  # in the order kept, the one that does not open left out
    assert store.count == 251 and store.unreadable == 1
    assert max(fetched) == 100 and sum(fetched) == 251  # never every row at once
    assert [n for n, _ in let_go][:3] == [100, 200, 251]  # the GIL let go after each part's rows were opened
    # S10 (open item 4): a pause that yields, the one ``engine.known`` takes: one of no time gives the event loop no turn
    # on Windows.
    assert {pause for _, pause in let_go} == {LET_GO}
