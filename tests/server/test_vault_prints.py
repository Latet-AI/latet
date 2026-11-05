"""Each word row of an API key's vault in PostgreSQL keeps a print beside its keyed hash (``latet.server.prints``): a
cheap keyed fingerprint of its word, which an instance holds in memory to find which words of a text are any value's
or fake's (the server performance plan's Task 6 on); and the change count of the transaction that wrote it last
(``counted``), by which an instance catches up (S3). A vault kept by 0.10.0 gets its prints in the background, on one
instance at a time, a print written only beside the word whose keyed hash is the row's."""

import asyncio
import hashlib
import logging
import struct
import threading
import time
from dataclasses import replace

import psycopg
import pytest

from latet.engine.stored import edges_of, word_of
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server import spaces as spaces_module
from latet.server import vault as vault_module
from latet.server.backends import vault_changes
from latet.server.master import root_key
from latet.server.prints import PRINT_PERSON, SLOT, print_key, prints
from latet.server.vault import PgVaultStore

from ..conftest import ROOT_KEY
from .conftest import dump, valkey_dump
from .support import listening, until

pytestmark = pytest.mark.server

OTHER_KEY = b"\x08" * 32
# The statements PgVaultStore executed in 0.10.0 (Step 1, counted on the plan's base, 3526c2d6): a new value kept at
# once (its entry, its fake's string, its value's, its two words, the count), and an applications call of ten new
# values kept together (four each, the count once).
ONE_VALUE_STATEMENTS = 5
TEN_VALUES_TOGETHER_STATEMENTS = 41

LAURA = Entity("e" + "1" * 19, "PERSON", EntityClass.NER, "Laura Bianchi", "Ada Rossi", 0, 1.0, "ner")
IDA = Entity("e" + "2" * 19, "PERSON", EntityClass.NER, "Ida Rossi", "Ugo Bianchi", 0, 1.0, "ner")
EMAIL = Entity("e" + "3" * 19, "EMAIL", EntityClass.PII, "Laura.Bianchi@nordest.it", "g.rossi@posta.it", 0, 1.0, None)
SECRET = Entity("e" + "5" * 19, "API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4", "jqb_9jI0bA3tA3", 0, 1.0, None)
IBAN = Entity("e" + "6" * 19, "IBAN", EntityClass.PII, "IT60X0542811101000000123456", "IT84W7093766632581800787511", 0, 1.0, None)  # fmt: skip
HELD = Entity("e" + "7" * 19, "PASSWORD", EntityClass.SECRET, "hunter2", "pw9Lq4z", 0, 1.0, None)  # a `held` row too
WORDLESS = Entity("e" + "8" * 19, "BADGE", EntityClass.CUSTOM, "x9-k2", "q7-z4", 0, 1.0, None)  # no anchor: NULL words
KEPT = (LAURA, IDA, EMAIL, SECRET, IBAN, HELD, WORDLESS)  # six of them with words


def _entity(n: int, value: str, fake: str) -> Entity:
    return Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, value, fake, 0, time.time(), None)


def _read(store: PgVaultStore, query: str, *args):
    with store.db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _rows(store: PgVaultStore) -> list[tuple[str, str, bytes | None, int | None, int | None]]:
    """The store's identity's word rows: entry, role, keyed hash, print, count."""
    query = (
        "SELECT entity_id, role, word, print, counted FROM vault_words WHERE identity_id = %s ORDER BY entity_id, role"
    )
    return [
        (str(entity), str(role), bytes(word) if word is not None else None, printed, counted)
        for entity, role, word, printed, counted in _read(store, query, store.identity)
    ]


def _plain(e: Entity, role: str) -> str | None:
    """The word a row of ``role`` hashes: the value's or the fake's word, or one of their edges (`held`: none)."""
    if role in ("original", "surrogate"):
        return word_of(e, role)
    base, _, side = role.partition("-")
    if side not in ("head", "tail"):
        return None
    head, tail = edges_of(e, base)
    return head if side == "head" else tail


def _expected(e: Entity, role: str, root: bytes = ROOT_KEY) -> int | None:
    word = _plain(e, role)
    return None if word is None else prints(print_key(root), word)[SLOT[role]]


def _check_prints(store: PgVaultStore, entities: dict[str, Entity], root: bytes = ROOT_KEY) -> int:
    """Every row's print is its role's slot of its word's prints, NULL where the word is: how many rows have one."""
    printed = 0
    for entity_id, role, word, found, _ in _rows(store):
        expected = _expected(entities[entity_id], role, root) if word is not None else None
        assert found == expected, (entity_id, role)
        printed += found is not None
    return printed


def _counter(store: PgVaultStore) -> int:
    found = _read(store, "SELECT counter FROM change_counters WHERE owner = %s", store.identity)
    return found[0][0] if found else 0


def _with_words(store: PgVaultStore, *entities: Entity) -> int:
    """How many rows of the identity's (of ``entities``', when given) have a word."""
    rows = [row for row in _rows(store) if row[2] is not None]
    return len([row for row in rows if not entities or row[0] in {e.id for e in entities}])


def _printless(store: PgVaultStore, *entities: Entity) -> None:
    """The prints and counts gone (of ``entities`` alone, when given), as 0.10.0 left its rows: the migration adds both
    columns NULL."""
    ids = [e.id for e in entities] or [row[0] for row in _rows(store)]
    with store.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET print = NULL, counted = NULL WHERE identity_id = %s AND entity_id = ANY(%s)",
            (store.identity, ids),
        )


@pytest.fixture
def keys(server):
    """Two API keys' vaults: the first's root key ``ROOT_KEY``, the second's another."""
    billing, _ = server.identities.add_key("Billing CI")
    support, _ = server.identities.add_key("Support bot")
    return PgVaultStore(server.db, billing.id, ROOT_KEY), PgVaultStore(server.db, support.id, OTHER_KEY)


@pytest.fixture
def told(server):
    """A key's vault whose prints are made on one instance at a time (Valkey's lock), and the changes it tells."""
    key, _ = server.identities.add_key("Prints CI")
    heard: list[tuple[str, str | None, int]] = []
    store = PgVaultStore(server.db, key.id, ROOT_KEY, changed=lambda *change: heard.append(change), jobs=server.bus)
    yield store, heard
    for together in list(store._together.values()):  # a test that failed halfway: its schema can be dropped
        together.rollback()


def test_every_word_row_has_its_print(told):
    """Kept at once, in an applications call, given a new fake, made by the backfill: each row of a word has its print,
    of the word its keyed hash is of, in its role's slot; a row with no word (no anchor, a secret held glued) has
    none."""
    store, _ = told
    for e in KEPT:
        assert store.save_entity(e) is None
    together = store.together()
    a, b = _entity(10, "m.conti@posta.it", "r.galli@posta.it"), _entity(11, "p.neri@posta.it", "u.sala@posta.it")
    assert store.save_entity(a) is None and store.save_entity(b) is None
    together.commit()
    drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
    assert store.replace_fake(IBAN, drawn) is None
    entities = {e.id: e for e in (*KEPT, a, b)} | {drawn.id: drawn}
    roles = {(entity_id, role) for entity_id, role, *_ in _rows(store)}
    assert {(SECRET.id, "original-head"), (drawn.id, "surrogate-tail"), (HELD.id, "held")} <= roles
    printed = _check_prints(store, entities)
    assert printed == _with_words(store) and printed == 2 * 8 + 2 + 2 + 4  # the words, then the edges
    # Made by the backfill: an entry's words gone (kept before the words were), made again with their prints.
    with store.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (store.identity, SECRET.id))
    assert store.backfill_words() == 1
    assert _check_prints(store, entities) == printed and store.prints_complete()


def test_a_value_and_a_fake_of_one_word_have_unrelated_prints(keys):
    """`bianchi` is Laura's value's word and Ugo's fake's, `rossi` Ida's value's and Ada's fake's: each pair has two
    prints, which no reader tells to be of one word, as their keyed hashes are two. Another key's root key gives every
    row another print."""
    mine, theirs = keys
    assert mine.save_entity(LAURA) is None and mine.save_entity(IDA) is None
    assert word_of(LAURA, "original") == word_of(IDA, "surrogate") == "bianchi"
    assert word_of(IDA, "original") == word_of(LAURA, "surrogate") == "rossi"
    rows = {(entity_id, role): (word, found) for entity_id, role, word, found, _ in _rows(mine)}
    for value, fake in ((LAURA, IDA), (IDA, LAURA)):
        value_word, value_print = rows[(value.id, "original")]
        fake_word, fake_print = rows[(fake.id, "surrogate")]
        assert value_print is not None and fake_print is not None and value_print != fake_print
        assert value_word != fake_word
    key = print_key(ROOT_KEY)
    assert len(set(prints(key, "bianchi"))) == 4 and not set(prints(key, "bianchi")) & set(prints(key, "rossi"))
    # Another key, the same values: none of its prints is one of mine.
    assert theirs.save_entity(LAURA) is None and theirs.save_entity(IDA) is None
    their_prints = {found for *_, found, _ in _rows(theirs)}
    assert len(their_prints) == 4 and not their_prints & {found for *_, found, _ in _rows(mine)}
    assert _check_prints(theirs, {LAURA.id: LAURA, IDA.id: IDA}, OTHER_KEY) == 4


def test_the_database_holds_no_word_in_clear(keys):
    """What a reader of the database sees, every column of every table: neither word, in any case, nor a print of it
    made without the key."""
    mine, theirs = keys
    for e in (LAURA, IDA, EMAIL):
        assert mine.save_entity(e) is None and theirs.save_entity(e) is None
    seen = dump(mine.db).lower()
    assert b"rossi" not in seen and b"bianchi" not in seen
    bare = {
        str(p).encode()
        for word in ("rossi", "bianchi", "laura.bianchi", "g.rossi@posta")
        for p in struct.unpack("<4q", hashlib.blake2b(word.encode(), digest_size=32, person=PRINT_PERSON).digest())
    }
    assert not bare & set(seen.split(b"\n"))


def test_each_write_of_the_words_is_counted_and_stamped(told, monkeypatch):
    """S3: the statement that moves the identity's change count writes that count into the rows of the entries it
    counts, in the same round trip, so that an instance catches up by the rows counted past what it read: a value kept
    at once, an applications call's values (the call's last count), a rule, a fake given anew, and each part of the
    backfill of the words and of the prints, which move the count once a part and tell it as `words`."""
    store, heard = told

    def counted(e: Entity) -> set[int | None]:
        return {c for entity_id, *_, c in _rows(store) if entity_id == e.id}

    first = _entity(1, "Laura.Bianchi@nordest.it", "g.rossi@posta.it")
    assert store.save_entity(first) is None
    assert heard == [("entity", first.id, 1)] and _counter(store) == 1 and counted(first) == {1}
    together = store.together()
    a, b = _entity(2, "m.conti@posta.it", "r.galli@posta.it"), _entity(3, "p.neri@posta.it", "u.sala@posta.it")
    assert store.save_entity(a) is None and store.save_entity(b) is None
    together.commit()
    assert heard[1:] == [("entity", a.id, 2), ("entity", b.id, 3)] and _counter(store) == 3
    assert counted(a) == counted(b) == {3} and counted(first) == {1}
    store.save_rule(replace(first, rule="email"))
    assert heard[-1] == ("rule", first.id, 4) and counted(first) == {4}  # its rows name an entry changed since
    assert store.save_entity(SECRET) is None and heard[-1] == ("entity", SECRET.id, 5)
    new = replace(SECRET, surrogate="kzr_4Hq8Lm2Pn6", counter=1)
    assert store.replace_fake(SECRET, new) is None
    assert heard[-1] == ("entity", SECRET.id, 6) and counted(SECRET) == {6} and _counter(store) == 6
    # The backfill of the words, two entries a part: each part counted once, its rows stamped, told as `words`.
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    with store.db.connection() as conn:
        conn.execute(
            "DELETE FROM vault_words WHERE identity_id = %s AND entity_id = ANY(%s)",
            (store.identity, [first.id, a.id, b.id]),
        )
    heard.clear()
    assert store.backfill_words() == 3
    assert heard == [("words", None, 7), ("words", None, 8)] and _counter(store) == 8
    assert counted(first) == counted(a) == {7} and counted(b) == {8} and counted(SECRET) == {6}
    assert store.backfill_words() == 0 and len(heard) == 2 and _counter(store) == 8  # nothing made: nothing counted
    # The prints of a vault kept before them, a part at a time: counted, stamped and told the same way.
    _printless(store, a, SECRET)
    assert store.fill_prints() == _with_words(store, a, SECRET) == 2 + 4  # a's two words; the secret's, and two edges
    assert heard[2:] == [("words", None, 9), ("prints", None, 9)]  # its part, then its end (T13's I-1)
    assert counted(a) == counted(SECRET) == {9} and _counter(store) == 9
    assert counted(first) == {7} and counted(b) == {8}


def test_a_new_value_costs_no_more_statements_than_before(told, monkeypatch):
    """The print travels in the statement that writes its word, and the stamp in the one that moves the count: a value
    kept at once, and an applications call of ten, cost the statements they cost in 0.10.0 (Step 1)."""
    store, _ = told
    assert store.save_entity(_entity(0, "a.b@posta.it", "c.d@posta.it")) is None  # the count's row is there
    sent: list[str] = []
    execute, many = psycopg.Cursor.execute, psycopg.Cursor.executemany

    def counted(cursor, query, params=None, **kwargs):
        sent.append(query)
        return execute(cursor, query, params, **kwargs)

    def counted_many(cursor, query, params_seq, **kwargs):
        sent.append(query)
        return many(cursor, query, params_seq, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counted)
    monkeypatch.setattr(psycopg.Cursor, "executemany", counted_many)
    assert store.save_entity(_entity(1, "Laura.Bianchi@nordest.it", "g.rossi@posta.it")) is None
    assert len(sent) <= ONE_VALUE_STATEMENTS, sent
    sent.clear()
    together = store.together()
    for n in range(10):
        assert store.save_entity(_entity(10 + n, f"p{n}.q{n}@nordest.it", f"f{n}.g{n}@posta.it")) is None
    together.commit()
    assert len(sent) <= TEN_VALUES_TOGETHER_STATEMENTS, sent


def test_prints_are_made_for_a_vault_from_0_10_0(told, keys, monkeypatch):
    """A vault kept by 0.10.0 (its rows with no print): not complete, its prints made a part at a time, each part
    counted and told; complete after, and a second run makes none. Another key's vault is left as it is."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    store, heard = told
    _, theirs = keys
    for e in KEPT:
        assert store.save_entity(e) is None
    assert theirs.save_entity(LAURA) is None
    assert store.prints_complete() and theirs.prints_complete()
    _printless(store)
    _printless(theirs)
    assert not store.prints_complete() and not theirs.prints_complete()
    heard.clear()
    count, words = _counter(store), _with_words(store)
    assert store.fill_prints() == words
    assert store.prints_complete() and not theirs.prints_complete()
    assert _check_prints(store, {e.id: e for e in KEPT}) == words
    assert heard == [
        ("words", None, count + 1), ("words", None, count + 2), ("words", None, count + 3),  # 6 entries
        ("prints", None, count + 3),  # every row walked: the other instances build their indexes again (T13's I-1)
    ]  # fmt: skip
    assert _counter(store) == count + 3
    assert store.fill_prints() == 0 and _counter(store) == count + 3 and len(heard) == 4  # once
    assert theirs.fill_prints() == 2 and theirs.prints_complete()  # Laura's value's word and her fake's
    assert _check_prints(theirs, {LAURA.id: LAURA}, OTHER_KEY) == 2


def test_a_print_is_written_only_beside_its_word(keys):
    """A row whose keyed hash is not the word its entry makes now (written by hand, swapped with another's, or the
    entry no longer opens) gets a NULL word and print (R5-1); every other row gets its own."""
    mine, _ = keys
    for e in (LAURA, IDA, EMAIL, SECRET):
        assert mine.save_entity(e) is None
    sealed = _entity(9, "m.conti@posta.it", "r.galli@posta.it")
    assert PgVaultStore(mine.db, mine.identity, OTHER_KEY).save_entity(sealed) is None  # does not open here
    _printless(mine)
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET word = sha256(word) WHERE identity_id = %s AND entity_id = %s AND role = 'original'",
            (mine.identity, LAURA.id),
        )
        # Ida's fake's word and the address's swapped: each row holds a hash its entry does not make.
        conn.execute(
            "UPDATE vault_words AS w SET word = o.word FROM vault_words AS o WHERE w.identity_id = %(i)s"
            " AND o.identity_id = %(i)s AND w.role = 'surrogate' AND o.role = 'surrogate'"
            " AND ((w.entity_id = %(a)s AND o.entity_id = %(b)s) OR (w.entity_id = %(b)s AND o.entity_id = %(a)s))",
            {"i": mine.identity, "a": IDA.id, "b": EMAIL.id},
        )
    words = _with_words(mine)
    assert mine.fill_prints() == words - 1 - 2 - 2 and mine.unreadable == 1
    rows = {(entity_id, role): found for entity_id, role, _, found, _ in _rows(mine)}
    assert rows[(LAURA.id, "original")] is None and rows[(LAURA.id, "surrogate")] == _expected(LAURA, "surrogate")
    assert rows[(IDA.id, "surrogate")] is None and rows[(EMAIL.id, "surrogate")] is None
    assert rows[(IDA.id, "original")] == _expected(IDA, "original")
    assert rows[(sealed.id, "original")] is None and rows[(sealed.id, "surrogate")] is None
    assert rows[(SECRET.id, "original-head")] == _expected(SECRET, "original-head")
    assert mine.prints_complete()
    assert all(word is None for _, _, word, printed, _ in _rows(mine) if printed is None)
    assert mine.fill_prints() == 0  # nothing more to give: the rows left are none of their entries' words


def test_prints_are_made_on_one_instance_at_a_time(server, prefix, monkeypatch, caplog):
    """Two instances fill one vault's prints: the one that holds the job (``prints:<identity>`` in Valkey) fills them,
    the other gives none meanwhile and leaves them to it. Neither Valkey nor the log holds a word of the vault."""
    key, _ = server.identities.add_key("Billing CI")
    a = PgVaultStore(server.db, key.id, ROOT_KEY, jobs=server.bus)
    b = PgVaultStore(server.db, key.id, ROOT_KEY, jobs=server.bus)
    for e in KEPT:
        assert a.save_entity(e) is None
    _printless(a)
    words = _with_words(a)
    with server.bus.hold(f"prints:{key.id}", 300) as mine:  # another instance's job under way
        assert mine and b.fill_prints() == 0 and not b.prints_complete()
    inside, release = threading.Event(), threading.Event()
    opened = a._opened

    def opening(row):
        inside.set()
        release.wait(30)
        return opened(row)

    monkeypatch.setattr(a, "_opened", opening)
    filled: list[int] = []
    with caplog.at_level(logging.DEBUG):
        worker = threading.Thread(target=lambda: filled.append(a.fill_prints()))
        worker.start()
        try:
            assert inside.wait(30)
            assert b.fill_prints() == 0  # a holds the job
        finally:
            release.set()
            worker.join(60)
    assert filled == [words] and a.prints_complete() and b.fill_prints() == 0
    said = " ".join(record.getMessage() for record in caplog.records).casefold()
    held = valkey_dump(server.bus, prefix).lower()
    for word in ("rossi", "bianchi", "hunter"):
        assert word not in said and word.encode() not in held


def test_the_words_are_complete_once_nothing_is_left_to_make(keys):
    """``words_complete``: every entry has its two words of the current version, and no NULL word the backfill would
    make now; a value with no anchor, and an entry that does not open, keep their NULL words and leave it complete."""
    mine, _ = keys
    for e in KEPT:
        assert mine.save_entity(e) is None
    assert mine.words_complete()
    sealed = _entity(9, "m.conti@posta.it", "x.y@posta.it")
    assert PgVaultStore(mine.db, mine.identity, OTHER_KEY).save_entity(sealed) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (mine.identity, sealed.id))
    assert not mine.words_complete()
    assert mine.backfill_words() == 0 and mine.words_complete()  # the one that does not open: two NULL words
    with mine.db.connection() as conn:  # a NULL word an older store wrote (a script it did not read)
        conn.execute(
            "UPDATE vault_words SET word = NULL, print = NULL WHERE identity_id = %s AND entity_id = %s"
            " AND role = 'original'",
            (mine.identity, LAURA.id),
        )
    assert not mine.words_complete()
    assert mine.backfill_words() == 1 and mine.words_complete() and mine.prints_complete()
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (mine.identity, EMAIL.id))
    assert not mine.words_complete()
    assert mine.backfill_words() == 1 and mine.words_complete()


async def test_a_words_message_from_another_instance_counts_as_applied(instances, monkeypatch):
    """A part of the backfill on one instance moves the identity's count and tells it as `words`: the other instance's
    space counts it as applied, with no catch-up from PostgreSQL."""
    first, second = instances
    caught_up: list[str] = []
    resync = spaces_module._resync

    def counted(space, db):
        caught_up.append(space.identity.id)
        return resync(space, db)

    monkeypatch.setattr(spaces_module, "_resync", counted)
    key, _ = first.identities.add_key("Billing CI")
    root = root_key(first.db, first.master, key.id)
    store = PgVaultStore(first.db, key.id, root, changed=vault_changes(first, key.id, f"{first.instance}:test"))
    async with listening(first, second):
        for n in range(3):
            kept = _entity(n, f"p{n}.q{n}@nordest.it", f"f{n}.g{n}@posta.it")
            assert await asyncio.to_thread(store.save_entity, kept) is None
        await second.spaces.runtime(key)
        space = second.spaces._spaces[key.id]
        await until(lambda: space.vault_counts.applied == 3)
        with first.db.connection() as conn:
            conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (key.id,))
        assert await asyncio.to_thread(store.backfill_words) == 3
        assert _counter(store) == 4
        await until(lambda: space.vault_counts.applied == 4)
        for _ in range(3):
            await second.check()
        await asyncio.sleep(0.2)
        assert caught_up == []


def _stamps(store: PgVaultStore, e: Entity) -> list[int | None]:
    """The count each row of ``e``'s carries."""
    return [counted for entity_id, *_, counted in _rows(store) if entity_id == e.id]


def _live_without_rows(store: PgVaultStore) -> list[str]:
    """The entries of the identity's vault that have no word row."""
    query = (
        "SELECT e.id FROM vault_entities e WHERE e.identity_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM vault_words w WHERE w.identity_id = e.identity_id AND w.entity_id = e.id)"
    )
    return [str(entity_id) for (entity_id,) in _read(store, query, store.identity)]


WRITERS = (
    "kept at once",
    "kept together",
    "rule",
    "rule kept together",
    "fake given anew",
    "words made",
    "prints made",
    "unusable word nulled",
)


@pytest.mark.parametrize("writer", WRITERS)
def test_each_write_stamps_every_row_of_its_entries_with_the_count_it_tells(told, writer):
    """Task 8's fix round 5, beside the guard of the sources (``test_vault_writes_counted``): after each public writer,
    every row of each entry it wrote carries the last count it told, an entry it did not write keeps its own, and every
    entry of the vault keeps a row at least (a catch-up names an entry by its rows: one with none would be named by
    nothing, and a vault confirms its memory by those names)."""
    store, heard = told
    first = _entity(1, "Laura.Bianchi@nordest.it", "g.rossi@posta.it")
    other = _entity(2, "m.conti@posta.it", "r.galli@posta.it")
    for e in (first, other):
        assert store.save_entity(e) is None
    kept = _stamps(store, other)
    heard.clear()
    written = [first]
    if writer == "kept at once":
        written = [_entity(3, "p.neri@posta.it", "u.sala@posta.it")]
        assert store.save_entity(written[0]) is None
    elif writer == "kept together":
        written = [_entity(3, "p.neri@posta.it", "u.sala@posta.it"), _entity(4, "a.riva@posta.it", "c.moro@posta.it")]
        together = store.together()
        for e in written:
            assert store.save_entity(e) is None
        together.commit()
    elif writer == "rule":
        store.save_rule(replace(first, rule="email"))
    elif writer == "rule kept together":
        written = [_entity(3, "p.neri@posta.it", "u.sala@posta.it")]
        together = store.together()
        assert store.save_entity(written[0]) is None
        store.save_rule(replace(written[0], rule="email"))
        together.commit()
    elif writer == "fake given anew":
        assert store.replace_fake(first, replace(first, surrogate="t.villa@posta.it", counter=1)) is None
    elif writer == "words made":
        with store.db.connection() as conn:  # kept before the words were
            conn.execute(
                "DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (store.identity, first.id)
            )
        assert store.backfill_words() == 1
    else:
        _printless(store, first)  # kept by 0.10.0
        if writer == "unusable word nulled":
            with store.db.connection() as conn:  # a keyed hash its entry does not make: the word is NULLed (R5-1)
                conn.execute(
                    "UPDATE vault_words SET word = sha256(word) WHERE identity_id = %s AND entity_id = %s"
                    " AND role = 'original'",
                    (store.identity, first.id),
                )
        assert store.fill_prints() > 0
    assert heard, "the write told nothing"
    last = max(count for *_, count in heard)
    for e in written:
        assert _stamps(store, e) and set(_stamps(store, e)) == {last}, (writer, e.id)
    assert _stamps(store, other) == kept
    assert not _live_without_rows(store)
