"""An API key's vault in PostgreSQL, read as it is needed rather than whole (``PgVaultStore`` as a ``KeyVaultStore``):
how many values it holds, its newest ones, those whose value or fake has no anchor word, one value by itself or by its
fake, and the values and fakes a text may hold, found by the keyed hash of each one's anchor word (``engine.anchors``).

The word index holds no word in clear, and is one space's alone: two keys with one root key find nothing of each other.
It follows its entries: written with a value, moved with a fake given anew, dropped with a value forgotten; and a vault
kept before it gets its words once, from any instance, at once on two."""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import quote

import psycopg
import pytest

from latet.engine.anchors import ANCHORS_VERSION, anchor, words_in
from latet.engine.stored import fake_word, value_word
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, FakeTaken, MaskingError
from latet.server import vault as vault_module
from latet.server.vault import PgVaultStore

from ..conftest import ROOT_KEY
from .support import listening

pytestmark = pytest.mark.server

OTHER_KEY = b"\x08" * 32
LAURA = "Laura.Bianchi@nordest.it"  # its anchor: bianchi; found by its local part, laura.bianchi (version 7)
FAKE = "g.rossi@posta.it"  # its anchor: rossi; found by its local part with its domain's name, g.rossi@posta (v8)


def _entity(n: int, value: str, fake: str, etype: str = "EMAIL", eclass: EntityClass = EntityClass.PII) -> Entity:
    return Entity(f"e{n:019d}", etype, eclass, value, fake, 0, time.time(), None)


def _read(store: PgVaultStore, query: str, *args):
    with store.db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _words_of(store: PgVaultStore) -> int:
    """How many word rows the store's identity has."""
    return _read(store, "SELECT COUNT(*) FROM vault_words WHERE identity_id = %s", store.identity)[0][0]


def _word(store: PgVaultStore, entity_id: str, role: str) -> bytes | None:
    return _read(
        store,
        "SELECT word FROM vault_words WHERE identity_id = %s AND entity_id = %s AND role = %s",
        store.identity,
        entity_id,
        role,
    )[0][0]


@pytest.fixture
def keys(server):
    """Two API keys' vaults on one server, with one root key: only the identity tells their words apart."""
    billing, _ = server.identities.add_key("Billing CI")
    support, _ = server.identities.add_key("Support bot")
    return PgVaultStore(server.db, billing.id, ROOT_KEY), PgVaultStore(server.db, support.id, ROOT_KEY)


def test_a_kept_value_is_found_by_its_anchor_and_by_its_fakes(keys):
    mine, theirs = keys
    e = _entity(1, LAURA, FAKE)
    assert mine.save_entity(e) is None
    assert mine.with_words("original", {value_word(e)}) == [e] and value_word(e) == "laura.bianchi"
    assert mine.with_words("surrogate", {fake_word(e)}) == [e] and fake_word(e) == "g.rossi@posta"
    # A text's words, in another case and quoted as in a URL, find it as well.
    assert mine.with_words("original", words_in("write to LAURA.BIANCHI%40NORDEST.IT today", dotted=True)) == [e]
    assert mine.with_words("surrogate", words_in('{"to": "G.ROSSI@POSTA.IT"}', dotted=True)) == [e]
    # A value's word is not a fake's, nor a fake's a value's.
    assert mine.with_words("surrogate", {anchor(e.original)}) == []
    assert mine.with_words("original", {anchor(e.surrogate)}) == []
    assert mine.with_words("original", []) == [] and mine.with_words("original", {"nordestina"}) == []
    # Another key's vault, with the same root key: the same words find nothing of it, and its own value only.
    assert (
        theirs.with_words("original", {"laura.bianchi"}) == []
        and theirs.with_words("surrogate", {"g.rossi@posta"}) == []
    )
    their = replace(e, id="e" + "9" * 19)
    assert theirs.save_entity(their) is None
    assert theirs.with_words("original", {"laura.bianchi"}) == [their] and mine.with_words(
        "original", {"laura.bianchi"}
    ) == [e]


def test_a_value_found_by_several_of_its_words_is_answered_once(keys):
    mine, _ = keys
    e, other = _entity(1, LAURA, FAKE), _entity(2, "m.conti@posta.it", "Orsa-7", etype="USERNAME")
    assert mine.save_entity(e) is None and mine.save_entity(other) is None
    found = mine.with_words("original", ["laura.bianchi", "laura.bianchi", "laura", "conti", "posta"])
    assert sorted(found, key=lambda x: x.id) == [e, other]


def test_forgetting_drops_the_words(keys):
    mine, _ = keys
    e = _entity(1, LAURA, FAKE)
    assert mine.save_entity(e) is None and _words_of(mine) == 2
    mine.forget_entity(e.id)
    assert (
        mine.with_words("original", {"laura.bianchi"}) == [] and mine.with_words("surrogate", {"g.rossi@posta"}) == []
    )
    assert _words_of(mine) == 0


def test_a_refused_value_leaves_no_words(keys):
    """A value whose fake is a string the vault knows is kept nowhere: nor are its words (one transaction)."""
    mine, _ = keys
    assert mine.save_entity(_entity(1, LAURA, FAKE)) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_entity(2, "m.conti@posta.it", LAURA))  # its fake is a value: refused in its transaction
    with pytest.raises(FakeTaken):
        mine.save_entity(_entity(3, "m.conti@posta.it", FAKE))  # its fake is another value's
    assert _words_of(mine) == 2 and mine.with_words("original", {"conti"}) == []


def _counter(store: PgVaultStore) -> int:
    """The identity's count of changes, as PostgreSQL has it (0: none yet)."""
    found = _read(store, "SELECT counter FROM change_counters WHERE owner = %s", store.identity)
    return found[0][0] if found else 0


def _entries(store: PgVaultStore) -> int:
    """The identity's entries, as another connection sees them."""
    return _read(store, "SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s", store.identity)[0][0]


@pytest.fixture
def told(server):
    """A key's vault, and the changes it tells (``changed``: what, the entry's id, its count), as it tells them."""
    key, _ = server.identities.add_key("Billing CI")
    heard: list[tuple[str, str, int]] = []
    store = PgVaultStore(server.db, key.id, ROOT_KEY, changed=lambda *change: heard.append(change))
    yield store, heard
    for together in list(store._together.values()):  # a test that failed halfway: its schema can be dropped
        together.rollback()


def test_a_calls_changes_are_kept_in_one_transaction_and_told_once_it_commits(told):
    """Ruling T10i: the values an applications call keeps together (``together``) cost one transaction and one commit,
    where each cost its own: no other connection sees them before it commits, the identity's count of changes moves
    once for all of them, and each is told after the commit with a count of its own, in the order it was kept, so that
    an instance that misses one is caught up as before. A value whose fake the vault knows is refused alone (its
    savepoint) and the call goes on; the thread's own lookups find what it kept meanwhile. A rule is kept at once, in a
    transaction of its own (review 10b's M2: the call's transaction locks no row another may write)."""
    store, heard = told
    first = _entity(1, LAURA, FAKE)
    assert store.save_entity(first) is None  # kept at once: no call keeps it together
    count = _counter(store)
    assert heard == [("entity", first.id, count)] and store.count == 1
    heard.clear()
    a = _entity(2, "m.conti@posta.it", "r.galli@posta.it")
    b = _entity(3, "Orsa-7", "Lince-4", etype="USERNAME")
    c = _entity(4, "p.neri@posta.it", "u.sala@posta.it")
    together = store.together()
    assert store.save_entity(a) is None and store.save_entity(b) is None
    with pytest.raises(FakeTaken):
        store.save_entity(_entity(5, "e.riva@posta.it", FAKE))  # another value's fake: refused alone
    store.save_rule(replace(first, rule="email"))  # at once
    assert heard == [("rule", first.id, count + 1)] and _counter(store) == count + 1
    assert store.save_entity(c) is None
    assert store.by_original(a.original) == a and store.with_words("surrogate", {"r.galli@posta"}) == [a]
    assert _entries(store) == 1 and store.count == 1
    together.commit()
    assert _entries(store) == 4 and _counter(store) == count + 4 and store.count == 4
    assert heard == [
        ("rule", first.id, count + 1), ("entity", a.id, count + 2), ("entity", b.id, count + 3), ("entity", c.id, count + 4)
    ]  # fmt: skip
    assert store.by_original("e.riva@posta.it") is None
    rule = store.load_entity(first.id)
    assert rule is not None and rule.rule == "email"
    assert store.save_entity(_entity(6, "l.costa@posta.it", "v.fumo@posta.it")) is None  # at once again
    assert heard[-1][2] == count + 5 and _entries(store) == 5


def test_a_call_that_ends_in_error_keeps_and_tells_nothing(told):
    store, heard = told
    together = store.together()
    e = _entity(1, LAURA, FAKE)
    assert store.save_entity(e) is None
    together.rollback()
    together.rollback()  # once more: nothing
    assert _entries(store) == 0 and _counter(store) == 0 and heard == [] and store.count == 0
    assert store.save_entity(e) is None and _entries(store) == 1  # nothing of it held: kept at once now


def test_a_calls_commit_that_fails_keeps_and_tells_nothing(told):
    store, heard = told
    together = store.together()
    e = _entity(1, LAURA, FAKE)
    assert store.save_entity(e) is None
    assert together.conn is not None
    with store.db.connection() as conn:  # its connection lost before it commits
        conn.execute("SELECT pg_terminate_backend(%s)", (together.conn.info.backend_pid,))
    with pytest.raises(psycopg.OperationalError):
        together.commit()
    assert _entries(store) == 0 and heard == [] and store.count == 0
    assert store.save_entity(e) is None and _entries(store) == 1 and len(heard) == 1


def test_a_few_calls_at_once_keep_their_values_together_and_the_pool_stays_free(server):
    """Ruling 10b-R2 (review I2): a call that keeps its values together holds a connection of the instance's one pool
    until it ends. Ten calls at once take no more than the few the instance allows (``Database.together``); the others
    keep each value at once, as before, and an agent's request or an unmask's log entry still gets a connection without
    waiting."""
    key, _ = server.identities.add_key("Billing CI")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    calls = 10
    started, release = threading.Barrier(calls + 1), threading.Event()
    held: list[bool] = []
    errors: list[BaseException] = []

    def call(n: int) -> None:
        together = store.together()
        try:
            assert store.save_entity(_entity(n, f"c{n}.x@posta.it", f"f{n}.y@posta.it")) is None
            held.append(together.conn is not None)
            started.wait(30)
            release.wait(30)
            together.commit()
        except BaseException as exc:
            errors.append(exc)
            together.rollback()
            started.abort()

    threads = [threading.Thread(target=call, args=(n,)) for n in range(calls)]
    for thread in threads:
        thread.start()
    try:
        started.wait(30)
        with server.db.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
    finally:
        release.set()
        for thread in threads:
            thread.join(60)
    assert errors == [] and len(held) == calls
    assert 1 <= sum(held) <= server.db.together_at_most < calls  # and the pool answered within its 2 s
    assert _entries(store) == calls and store.count == calls


@pytest.mark.timing
def test_a_forget_waits_for_a_locked_row_no_longer_than_its_bound(keys, monkeypatch):
    """Review 10b's M2: a forget that meets a row another transaction holds (a call that keeps its values together,
    whose new value is the forgotten value's fake) waits ``FORGET_LOCK_SECONDS`` at most, then fails (the dashboard
    answers it, and it is tried again), never as long as the other transaction lasts. It runs in the dashboard's work
    threads, never on the event loop (``test_dashboard_loop``)."""
    monkeypatch.setattr(vault_module, "FORGET_LOCK_SECONDS", 1)
    mine, _ = keys
    e = _entity(1, LAURA, FAKE)
    assert mine.save_entity(e) is None
    done: list[BaseException | None] = []

    def forget() -> None:
        try:
            mine.forget_entity(e.id)
            done.append(None)
        except BaseException as exc:
            done.append(exc)

    with psycopg.connect("", **mine.db._parameters) as locker:  # another transaction, holding the row
        locker.execute(
            "SELECT 1 FROM vault_strings WHERE identity_id = %s AND entity_id = %s FOR UPDATE", (mine.identity, e.id)
        )
        waiting = threading.Thread(target=forget)
        waiting.start()
        waiting.join(4)
        alive = waiting.is_alive()
        locker.rollback()
    waiting.join(30)
    assert not alive and len(done) == 1 and isinstance(done[0], psycopg.OperationalError)
    assert mine.load_entity(e.id) == e  # nothing of it forgotten: tried again, it is
    mine.forget_entity(e.id)
    assert mine.load_entity(e.id) is None


def _linked(n: int, fake: str) -> Entity:
    """A name kept as a link writes it (0.9.9, Ruling 099-R1): its fake in the same escapes, which reads otherwise."""
    return Entity(f"e{n:019d}", "PERSON", EntityClass.NER, f"Utente%20Numero{n}", fake, 0, time.time(), "ner", True)


def _person(n: int, value: str, fake: str) -> Entity:
    return _entity(n, value, fake, etype="PERSON", eclass=EntityClass.NER)


def test_a_fake_in_escapes_never_reads_as_a_string_the_vault_knows(keys):
    """0.9.9's check (N3, I-1), kept by the store, since a key's memory holds few of its strings: a fake kept in
    escapes does not read as a value or another fake of the vault (`Giulia%20Martini` and a real Giulia Martini), nor is
    a fake how another one reads; a value of that name is a value all the same; and once its entry is forgotten or its
    fake given anew, the reading is retired with the fake (the final security review's H2), never another fake."""
    mine, theirs = keys
    assert mine.save_entity(_person(1, "Giulia Martini", "Paolo Fontana")) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_linked(2, "Giulia%20Martini"))  # reads as a value the vault knows
    assert mine.save_entity(_person(3, "Anna Rossi", "Luca Bianchi")) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_linked(4, "Luca%20Bianchi"))  # reads as another value's fake
    assert mine.save_entity(_linked(5, "Marco%20Verdi")) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_person(6, "Laura Neri", "Marco Verdi"))  # a fake that another fake reads as
    assert mine.save_entity(_person(7, "Marco Verdi", "Elena Conti")) is None  # a real Marco Verdi: a value
    assert theirs.save_entity(_person(6, "Laura Neri", "Marco Verdi")) is None  # another key's vault: its own strings
    gone = _linked(8, "Sara%20Galli")
    assert mine.save_entity(gone) is None
    mine.forget_entity(gone.id)
    with pytest.raises(FakeTaken):
        mine.save_entity(_person(9, "Ida Russo", "Sara Galli"))
    moved = _linked(10, "Teo%20Bassi")
    assert mine.save_entity(moved) is None
    assert mine.replace_fake(moved, replace(moved, surrogate="Ugo%20Sala", counter=1)) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_person(11, "Eva Riva", "Teo Bassi"))
    with pytest.raises(FakeTaken):
        mine.save_entity(_person(12, "Rita Pace", "Ugo Sala"))


def _fastest(call, times: int = 3) -> float:
    took = []
    for n in range(times):
        started = time.perf_counter()
        call(n)
        took.append(time.perf_counter() - started)
    return min(took)


@pytest.mark.timing
def test_a_string_that_is_no_fake_costs_two_probes_whatever_shares_a_word_with_it(keys):
    """Review Important 2: a key's vault asks the store whether a string is a fake as written and as a fake in escapes
    reads by the string's keyed hashes, two probes, never by opening every fake that shares a word with it (3,000 here,
    which a lookup by their word opens in tens of milliseconds, 100,000 in seconds)."""
    from latet.engine.stored import StoredVault

    mine, _ = keys
    for n in range(3_000):
        assert mine.save_entity(_person(n, f"Cliente Numero{n:05d}", f"Giulia Ab{n:04d}")) is None  # anchor: giulia
    link = _linked(9_999, "Giulia%20Martini")
    assert mine.save_entity(link) is None
    assert mine.by_reading("Giulia Martini") == link and mine.by_reading("Giulia Ab0001") is None
    vault = StoredVault(ROOT_KEY, mine, working=1_000)
    opened = _fastest(lambda n: mine.with_words("surrogate", {"giulia"}))
    miss = _fastest(lambda n: vault.by_surrogate(f"Giulia Nessuno{n}"))  # a string of its own each time: no miss kept
    assert miss < min(0.05, opened / 3), (miss, opened)
    assert vault.by_surrogate("Giulia Martini") == link and vault.is_surrogate("Giulia Martini")


def test_a_vault_kept_before_knows_how_its_fakes_read_once_its_words_are_made(keys, monkeypatch):
    """Review Important 3: entries kept by 0.9.9 or 0.9.10 (no words, and no row for how a fake in escapes reads; an API
    key's identity existed then, its vault in memory): once the words are made (``backfill_words``, as the key's space
    is built), a fake equal to how an old fake reads is refused, as a vault holding the old entry refuses it. A
    collision those versions left (an old fake that another old fake reads as) is left as it is, and the words are
    made all the same."""
    mine, _ = keys
    old = [_linked(1, "Elena%20DeLuca"), _person(2, "Ida Russo", "Sara Galli"), _linked(3, "Sara%20Galli")]
    with monkeypatch.context() as at_0_9_10:
        at_0_9_10.setattr(vault_module, "_WORDS", "SELECT %s, %s, %s, %s, %s, %s, %s, %s")  # neither words
        at_0_9_10.setattr(PgVaultStore, "_read_as", lambda self, conn, e: None)  # nor readings
        for e in old:
            assert mine.save_entity(e) is None
    assert mine.backfill_words() == 3 and mine.backfill_words() == 0
    with pytest.raises(FakeTaken):
        mine.save_entity(_person(4, "Laura Neri", "Elena DeLuca"))  # how the first old fake reads
    assert mine.with_words("surrogate", {"elena"}) == [old[0]]


def test_a_key_draws_a_fake_that_reads_as_no_value_it_keeps_out_of_memory(server):
    """``StoredVault.intern`` of a value found where escapes read (`Mario%20Rossi`): the first fake drawn reads as a
    real person the key's vault keeps out of memory; the store refuses it, and the vault takes the fake a vault that
    holds that person in memory takes."""
    from latet.engine.stored import StoredVault
    from latet.engine.vault import Vault, fake_reading

    key, _ = server.identities.add_key("Billing CI")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    first = Vault(ROOT_KEY).intern("PERSON", EntityClass.NER, "Mario%20Rossi", reads="Mario Rossi")
    reading = fake_reading(first)
    assert reading is not None
    real = Vault(ROOT_KEY).intern("PERSON", EntityClass.NER, reading)
    expected = Vault(ROOT_KEY, [real]).intern("PERSON", EntityClass.NER, "Mario%20Rossi", reads="Mario Rossi")
    assert expected.surrogate != first.surrogate
    keeper = StoredVault(ROOT_KEY, store, working=1000)
    keeper.intern("PERSON", EntityClass.NER, reading)
    for n in range(3):
        keeper.intern("EMAIL", EntityClass.PII, f"customer.{n}@gmail.com")
    vault = StoredVault(ROOT_KEY, store, working=1)
    assert store.by_original(reading) is not None and reading not in {e.original for e in vault.entities()}
    got = vault.intern("PERSON", EntityClass.NER, "Mario%20Rossi", reads="Mario Rossi")
    assert got.surrogate == expected.surrogate and fake_reading(got) != reading


def test_replacing_a_fake_moves_its_word(keys):
    mine, _ = keys
    e = _entity(1, LAURA, FAKE)
    assert mine.save_entity(e) is None
    new = replace(e, surrogate="f.galli@posta.it", counter=1)
    assert mine.replace_fake(e, new) is None
    assert mine.with_words("surrogate", {"g.rossi@posta"}) == []
    assert mine.with_words("surrogate", {"f.galli@posta"}) == [new] and mine.with_words(
        "original", {"laura.bianchi"}
    ) == [new]
    assert _words_of(mine) == 2
    # An entry kept before the words: its new fake's word is written, and its value's comes with the others'.
    old = _entity(2, "m.conti@posta.it", "Orsa-7", etype="USERNAME")
    assert mine.save_entity(old) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (mine.identity, old.id))
    drawn = replace(old, surrogate="Cometa-42", counter=1)
    assert mine.replace_fake(old, drawn) is None
    assert mine.with_words("surrogate", {"cometa"}) == [drawn] and mine.with_words("original", {"conti"}) == []
    assert mine.backfill_words() == 1
    assert mine.with_words("original", {"conti"}) == [drawn] and mine.with_words("surrogate", {"cometa"}) == [drawn]


SECRET = Entity("e" + "5" * 19, "API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4", "jqb_9jI0bA3tA3", 0, 1.0, None)
IBAN = Entity("e" + "6" * 19, "IBAN", EntityClass.PII, "IT60X0542811101000000123456", "IT84W7093766632581800787511", 0, 1.0, None)  # fmt: skip
NAMED = Entity("e" + "4" * 19, "PERSON", EntityClass.NER, "Massimiliano Ferri", "Bartolomeo Conti", 0, 1.0, "ner")


def _glued(store: PgVaultStore) -> tuple[list[Entity], list[Entity], list[Entity], list[Entity]]:
    """What texts that glue letters to a secret, an IBAN's fake and a name find: the secret by its value, the IBAN by
    its fake, and the name by neither (a vault finds a name, and restores its fake, as whole words alone)."""
    return (
        store.with_words("original", words_in("  D%27Angelotok_9fQ2xL7pZ4acme.it")),
        store.with_words("surrogate", words_in("pay IT84W7093766632581800787511D'Angelo")),
        store.with_words("original", words_in("Massimilianox wrote")),
        store.with_words("surrogate", words_in("xBartolomeo wrote")),
    )


def test_a_secret_and_a_fake_glued_to_letters_are_found_by_their_edges(keys):
    """Ruling T10h: a value the vault's detectors find inside a word (a secret), and a fake a restore finds there (one
    that is no word), are kept with their edges (``stored.words_of``), and found by a text that glues letters to them;
    a name, which is found as a whole word, is kept with its anchor alone."""
    mine, theirs = keys
    for e in (SECRET, IBAN, NAMED):
        assert mine.save_entity(e) is None
    assert _glued(mine) == ([SECRET], [IBAN], [], [])
    assert _glued(theirs) == ([], [], [], [])
    assert _words_of(mine) == 2 * 3 + 2 + 2  # the secret's value's head and its fake's, the IBAN's fake's two
    # Given a new fake, the entry's fake's edges are the new one's.
    drawn = replace(IBAN, surrogate="IT02L1234512345123456789012", counter=1)
    assert mine.replace_fake(IBAN, drawn) is None
    assert mine.with_words("surrogate", words_in("pay IT84W7093766632581800787511D'Angelo")) == []
    assert mine.with_words("surrogate", words_in("pay IT02L1234512345123456789012D'Angelo")) == [drawn]
    assert _words_of(mine) == 2 * 3 + 2 + 2


def test_a_vault_kept_before_the_edges_gets_them_once_its_words_are_made(keys):
    """Ruling T10h: a vault whose words are of version 4 (no edges) has them made again, its edges with them, once: the
    counts that say whether words are missing count an entry's two words alone."""
    mine, _ = keys
    for e in (SECRET, IBAN, NAMED):
        assert mine.save_entity(e) is None
    assert mine.backfill_words() == 0  # nothing missing: the edges are no words the counts miss
    with mine.db.connection() as conn:  # as version 4 kept them
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND role LIKE %s", (mine.identity, "%-%"))
        conn.execute("UPDATE vault_words SET version = 4 WHERE identity_id = %s", (mine.identity,))
    assert _glued(mine) == ([], [], [], [])
    assert mine.backfill_words() == 3 and mine.backfill_words() == 0
    assert _glued(mine) == ([SECRET], [IBAN], [], []) and _versions(mine) == {ANCHORS_VERSION}


def test_backfill_gives_words_to_every_old_value(keys, monkeypatch, caplog):
    """Values kept before the words (rows with no word rows): each gets both, once, a part of the vault at a time; an
    entry that does not open gets none, and is counted. Another key's vault is left as it is."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    mine, theirs = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 6)]
    for e in kept:
        assert mine.save_entity(e) is None
    # An entry sealed with another root key, among them: it does not open with this one.
    assert PgVaultStore(mine.db, mine.identity, OTHER_KEY).save_entity(_entity(6, LAURA, FAKE)) is None
    their = _entity(7, "m.conti@posta.it", "Orsa-7", etype="USERNAME")
    assert theirs.save_entity(their) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words")  # as a vault kept by 0.9.x is
    anchors = {value_word(e) for e in kept}
    assert mine.with_words("original", anchors) == []
    with caplog.at_level(logging.DEBUG):
        assert mine.backfill_words() == 5
        assert mine.backfill_words() == 0  # once
    assert mine.with_words("original", anchors) == kept
    assert mine.with_words("surrogate", {fake_word(e) for e in kept}) == kept
    assert _words_of(mine) == 12 and mine.unreadable == 1  # the one that does not open: two NULL words
    assert theirs.with_words("original", {"conti"}) == [] and theirs.backfill_words() == 1
    assert theirs.with_words("original", {"conti"}) == [their]
    said = " ".join(record.getMessage() for record in caplog.records).casefold()
    assert "bianchi" not in said and "surname1" not in said and "fake1" not in said


def test_two_instances_fill_the_words_at_once(keys, monkeypatch):
    """Two instances building one key's space at once: every value gets its words once, and neither fails."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 3)
    mine, _ = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 21)]
    for e in kept:
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words")
    a, b = PgVaultStore(mine.db, mine.identity, ROOT_KEY), PgVaultStore(mine.db, mine.identity, ROOT_KEY)
    with ThreadPoolExecutor(2) as pool:
        filled = [f.result() for f in [pool.submit(a.backfill_words), pool.submit(b.backfill_words)]]
    assert sum(filled) == 20 and _words_of(mine) == 40
    assert mine.with_words("original", {value_word(e) for e in kept}) == kept
    # Once every value has its words, two counts say so: the vault is not read again.
    monkeypatch.setattr(vault_module, "_WORDLESS", "SELECT 1 / 0")
    assert a.backfill_words() == 0


def test_a_value_forgotten_while_its_words_are_filled_is_left_out(keys, monkeypatch):
    """Another instance forgets a value between the reading of a part and the writing of its words: the part is read
    again, without it, and nothing fails."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 10)
    mine, _ = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 4)]
    for e in kept:
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words")
    elsewhere = PgVaultStore(mine.db, mine.identity, ROOT_KEY)
    opened, seen = mine._opened, []

    def opening(row):
        if not seen:
            elsewhere.forget_entity(kept[1].id)  # once the part is read, before its words are written
        seen.append(row[0])
        return opened(row)

    monkeypatch.setattr(mine, "_opened", opening)
    assert mine.backfill_words() == 2
    assert seen == [kept[0].id, kept[1].id, kept[2].id]  # held while its words are written: the one gone left out
    assert _words_of(mine) == 4 and mine.with_words("original", {value_word(e) for e in kept}) == [
        kept[0],
        kept[2],
    ]


def test_a_value_without_anchor_is_loaded_apart(keys):
    mine, theirs = keys
    anchored = _entity(1, LAURA, FAKE)
    no_fake_word = _entity(2, "m.conti@posta.it", "ab-1", etype="USERNAME")
    no_value_word = _entity(3, "x7", "Cometa-42", etype="PASSWORD", eclass=EntityClass.SECRET)
    for e in (anchored, no_fake_word, no_value_word):
        assert mine.save_entity(e) is None
    assert mine.load_unanchored() == [no_fake_word, no_value_word]
    assert theirs.load_unanchored() == []
    # Found by the word it has, never by the one it lacks.
    assert mine.with_words("original", {"conti"}) == [no_fake_word]
    assert mine.with_words("surrogate", {"cometa"}) == [no_value_word]
    assert _word(mine, no_fake_word.id, "surrogate") is None and _word(mine, no_value_word.id, "original") is None


def test_a_value_and_a_fake_in_another_script_are_found_by_their_words(keys):
    """Ruling T7a: a value or a fake written in another script than the Latin one has a word as well, and is not one
    that the space keeps in memory for want of one."""
    mine, _ = keys
    russian = _entity(1, "Иван Петров", "Сергей Смирнов", etype="PERSON", eclass=EntityClass.NER)
    chinese = _entity(2, "王小明", "李华强", etype="PERSON", eclass=EntityClass.NER)
    greek = _entity(3, "Γιώργος Παπαδόπουλος", "Νίκος Γεωργίου", etype="PERSON", eclass=EntityClass.NER)
    for e in (russian, chinese, greek):
        assert mine.save_entity(e) is None
    assert mine.with_words("original", words_in("Вчера звонил Иван Петров.")) == [russian]
    assert mine.with_words("surrogate", words_in(json.dumps({"to": "Сергей Смирнов"}))) == [russian]
    assert mine.with_words("original", words_in("请联系王小明先生")) == [chinese]
    assert mine.with_words("surrogate", words_in(quote("李华强"))) == [chinese]
    assert mine.with_words("original", words_in("ΓΙΩΡΓΟΣ ΠΑΠΑΔΟΠΟΥΛΟΣ")) == [greek]
    assert mine.load_unanchored() == []


def test_backfill_turns_an_old_null_word_into_a_word(keys, monkeypatch):
    """A value kept before its script was read (its word NULL) gets its word; one with none to get (`ab-1`) keeps its
    NULL, and is not counted again."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    mine, _ = keys
    russian = _entity(1, "Иван Петров", "Сергей Смирнов", etype="PERSON", eclass=EntityClass.NER)
    arabic = _entity(2, "محمد علي", "ab-1", etype="PERSON", eclass=EntityClass.NER)
    latin = _entity(3, LAURA, FAKE)
    for e in (russian, arabic, latin):
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:  # as the store wrote them before it read other scripts
        conn.execute(
            "UPDATE vault_words SET word = NULL WHERE identity_id = %s AND entity_id = ANY(%s) AND NOT (entity_id = %s"
            " AND role = 'surrogate')",
            (mine.identity, [russian.id, arabic.id], arabic.id),
        )
    assert mine.load_unanchored() == [russian, arabic]
    assert mine.with_words("original", {"петров"}) == [] and mine.with_words("original", {"محمد"}) == []
    assert mine.backfill_words() == 2
    assert mine.with_words("original", {"петров"}) == [russian] and mine.with_words("surrogate", {"смирнов"}) == [
        russian
    ]
    assert mine.with_words("original", {"محمد"}) == [arabic]
    assert mine.load_unanchored() == [arabic] and _word(mine, arabic.id, "surrogate") is None
    assert mine.backfill_words() == 0  # once


def test_a_fake_given_anew_while_its_word_is_filled_keeps_its_own(keys, monkeypatch):
    """Another instance gives an entry a new fake between the reading of a part and the writing of its words: the
    entry is left out of the part, and its word is the new fake's, never the old one's."""
    mine, _ = keys
    old = _entity(1, "m.conti@posta.it", "Сергей Смирнов", etype="USERNAME")
    assert mine.save_entity(old) is None
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET word = NULL WHERE identity_id = %s AND role = 'surrogate'", (mine.identity,)
        )
    elsewhere = PgVaultStore(mine.db, mine.identity, ROOT_KEY)
    new = replace(old, surrogate="ab-2", counter=1)  # a fake with no word
    opened, seen = mine._opened, []

    def opening(row):
        if not seen:
            assert elsewhere.replace_fake(old, new) is None  # once the part is read, before its words are written
        seen.append(row[0])
        return opened(row)

    monkeypatch.setattr(mine, "_opened", opening)
    assert mine.backfill_words() == 0
    assert _word(mine, old.id, "surrogate") is None and mine.with_words("surrogate", {"смирнов"}) == []
    assert mine.load_unanchored() == [new]


def _versions(store: PgVaultStore) -> set[int]:
    return {row[0] for row in _read(store, "SELECT version FROM vault_words WHERE identity_id = %s", store.identity)}


def test_words_of_an_older_version_of_the_anchors_are_made_again(keys, monkeypatch):
    """Ruling T7b: a change of ``anchor`` bumps ``ANCHORS_VERSION``, and the words kept under an older one are made
    again, once; those of the current one are left as they are."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    mine, _ = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 4)]
    current = _entity(4, LAURA, FAKE)
    for e in (*kept, current):
        assert mine.save_entity(e) is None
    assert _versions(mine) == {ANCHORS_VERSION}
    with mine.db.connection() as conn:  # as an older anchor() wrote them: other words
        conn.execute(
            "UPDATE vault_words SET version = %s, word = sha256(word) WHERE identity_id = %s AND entity_id = ANY(%s)",
            (ANCHORS_VERSION - 1, mine.identity, [e.id for e in kept]),
        )
    anchors = {value_word(e) for e in kept}
    assert mine.with_words("original", anchors) == []
    assert mine.backfill_words() == 3
    assert mine.with_words("original", anchors) == kept
    assert mine.with_words("surrogate", {fake_word(e) for e in kept}) == kept
    assert mine.with_words("original", {"laura.bianchi"}) == [current] and _versions(mine) == {ANCHORS_VERSION}
    assert mine.backfill_words() == 0


def test_a_fake_given_anew_while_the_words_are_made_again_leaves_the_values_word_written(keys, monkeypatch):
    """Minor 2 of the review: each word is held by its own string's keyed hash, so an entry whose fake changed
    meanwhile still gets its value's word, and keeps the new fake's."""
    mine, _ = keys
    old = _entity(1, "m.conti@posta.it", "Сергей Смирнов", etype="USERNAME")
    assert mine.save_entity(old) is None
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET version = %s, word = sha256(word) WHERE identity_id = %s",
            (ANCHORS_VERSION - 1, mine.identity),
        )
    elsewhere = PgVaultStore(mine.db, mine.identity, ROOT_KEY)
    new = replace(old, surrogate="Cometa-42", counter=1)
    opened, seen = mine._opened, []

    def opening(row):
        if not seen:
            assert elsewhere.replace_fake(old, new) is None  # once the part is read, before its words are written
        seen.append(row[0])
        return opened(row)

    monkeypatch.setattr(mine, "_opened", opening)
    assert mine.backfill_words() == 1  # its value's word
    assert mine.with_words("original", {"conti"}) == [new] and mine.with_words("surrogate", {"cometa"}) == [new]
    assert mine.with_words("surrogate", {"смирнов"}) == [] and _versions(mine) == {ANCHORS_VERSION}


def test_an_entry_that_does_not_open_gets_null_words_and_the_vault_is_not_walked_again(keys, monkeypatch):
    """Minor 6 of the review: an entry sealed with another root key gets two NULL words of the current version, so
    that the counts say every entry has its words and no later build walks the vault for it."""
    mine, _ = keys
    readable, sealed = _entity(1, LAURA, FAKE), _entity(2, "m.conti@posta.it", "Orsa-7")
    assert mine.save_entity(readable) is None
    assert PgVaultStore(mine.db, mine.identity, OTHER_KEY).save_entity(sealed) is None
    with mine.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (mine.identity,))
    assert mine.backfill_words() == 1  # the one that opens got its words
    assert _word(mine, sealed.id, "original") is None and _word(mine, sealed.id, "surrogate") is None
    assert _words_of(mine) == 4 and _versions(mine) == {ANCHORS_VERSION}
    monkeypatch.setattr(vault_module, "_WORDLESS", "SELECT 1 / 0")  # the counts say there is nothing to walk
    assert mine.backfill_words() == 0
    assert mine.load_unanchored() == [] and mine.unreadable == 1


def test_the_rules_of_the_newest_values_are_cut_oldest_first(keys, monkeypatch):
    """Minor 1 of the review: the newest values are read newest first, and the rules cut among them are written oldest
    first, as every write of several entries of a vault holds them (no two writes wait for each other)."""
    mine, _ = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 5)]
    for e in kept:
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:  # rules kept with a field's name, by an earlier Latet
        conn.execute(
            "UPDATE vault_entities SET rule = 'field:olivia.bennett@northwind.com' WHERE identity_id = %s",
            (mine.identity,),
        )
    written: list[list[str]] = []
    cut = mine._cut_field_names
    monkeypatch.setattr(mine, "_cut_field_names", lambda rows: (written.append([row[2] for row in rows]), cut(rows)))
    assert mine.load_recent(4) == [replace(e, rule="field") for e in kept[::-1]]
    assert written == [[e.id for e in kept]]


def test_lookups_by_original_and_by_fake(keys, caplog):
    mine, theirs = keys
    e = _entity(1, LAURA, FAKE)
    assert mine.save_entity(e) is None
    assert mine.by_original(LAURA) == e and mine.by_surrogate(FAKE) == e
    assert mine.by_original(FAKE) is None and mine.by_surrogate(LAURA) is None
    assert mine.by_original("someone.else@nordest.it") is None and mine.by_surrogate("h.verdi@posta.it") is None
    assert theirs.by_original(LAURA) is None and theirs.by_surrogate(FAKE) is None
    # An entry that does not open (its type changed by hand, which its seal binds) is found as none, and counted.
    changed = _entity(2, "m.conti@posta.it", "Orsa-7")
    assert mine.save_entity(changed) is None
    with mine.db.connection() as conn:
        conn.execute("UPDATE vault_entities SET etype = 'USERNAME' WHERE id = %s", (changed.id,))
    with caplog.at_level(logging.WARNING):
        assert mine.by_original("m.conti@posta.it") is None and mine.by_surrogate("Orsa-7") is None
    assert mine.unreadable == 1
    said = " ".join(record.getMessage() for record in caplog.records).casefold()
    assert "conti" not in said and "orsa" not in said


def test_recent_values_newest_first_and_bounded(keys, monkeypatch):
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 2)
    mine, theirs = keys
    kept = [_entity(n, f"person{n}.surname{n}@nordest.it", f"fake{n}.name{n}@posta.it") for n in range(1, 6)]
    for e in kept:
        assert mine.save_entity(e) is None
    assert mine.count_entities() == 5 and theirs.count_entities() == 0
    assert mine.load_recent(2) == [kept[4], kept[3]]
    assert mine.load_recent(5) == kept[::-1] and mine.load_recent(50) == kept[::-1]
    assert mine.load_recent(0) == [] and theirs.load_recent(5) == []


def test_many_words_in_chunks(keys, monkeypatch):
    """A text of many words asks for them all in one call: sent ``WORDS_AT_ONCE`` at a time."""
    mine, _ = keys
    e, other = _entity(1, LAURA, FAKE), _entity(2, "m.conti@posta.it", "Orsa-7", etype="USERNAME")
    assert mine.save_entity(e) is None and mine.save_entity(other) is None
    words = [f"w{n:05d}x" for n in range(11_998)] + ["laura.bianchi", "conti"]  # of seven: none asked as an edge
    sent: list[int] = []
    execute = psycopg.Cursor.execute

    def counted(cursor, query, params=None, **kwargs):
        if isinstance(query, str) and "vault_words" in query and "ANY" in query:
            sent.append(len(params[3]))  # the words of the part (then how many entries it may still open)
        return execute(cursor, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counted)
    found = mine.with_words("original", words)
    assert sorted(found, key=lambda x: x.id) == [e, other]
    assert vault_module.WORDS_AT_ONCE == 5_000 and sorted(sent) == [2_000, 5_000, 5_000]


def test_the_word_index_holds_no_word(keys, server):
    """Words are kept as keyed hashes alone: no column of the index holds one, in any case. A value's word and a
    fake's are never one hash, and another space's key gives other hashes for the same words."""
    mine, _ = keys
    person = _entity(1, "Laura Bianchi", "Ada Rossi", etype="PERSON", eclass=EntityClass.NER)
    email = _entity(2, "info@bianchi.it", FAKE)  # a role's mailbox: its anchor, bianchi
    company = _entity(3, "Rossi S.p.A.", "Verdi S.r.l.", etype="ORG", eclass=EntityClass.NER)
    for e in (person, email, company):
        assert mine.save_entity(e) is None
    rows = _read(mine, "SELECT * FROM vault_words")
    assert len(rows) == 6
    for row in rows:
        for value in row:
            if isinstance(value, bytes | bytearray | memoryview):
                seen = bytes(value).lower()
                assert b"bianchi" not in seen and b"rossi" not in seen
            else:
                seen = str(value).casefold()
                assert "bianchi" not in seen and "rossi" not in seen
    assert _word(mine, person.id, "original") == _word(mine, email.id, "original")  # one anchor, one hash
    assert _word(mine, company.id, "original") != _word(mine, person.id, "surrogate")  # rossi: a value's, a fake's
    elsewhere, _ = server.identities.add_key("Elsewhere")
    theirs = PgVaultStore(mine.db, elsewhere.id, OTHER_KEY)
    assert theirs.save_entity(email) is None
    assert _word(theirs, email.id, "original") != _word(mine, email.id, "original")


async def test_an_api_keys_own_vault_limit_is_its_spaces(server):
    """A key's own limit of values (NULL: the server's) is read with the key, and is the one its space keeps to."""
    billing, _ = server.identities.add_key("Billing CI")
    support, _ = server.identities.add_key("Support bot")
    assert billing.vault_limit is None
    with server.db.connection() as conn:
        conn.execute("UPDATE identities SET vault_limit = 5000 WHERE id = %s", (billing.id,))
    with pytest.raises(psycopg.errors.CheckViolation), server.db.connection() as conn:
        conn.execute("UPDATE identities SET vault_limit = 999 WHERE id = %s", (support.id,))
    billing, support = server.identities.current(billing.id), server.identities.current(support.id)
    assert billing is not None and support is not None
    assert (billing.vault_limit, support.vault_limit) == (5000, None)
    async with listening(server):
        await server.spaces.runtime(billing)
        await server.spaces.runtime(support)
        assert server.spaces._spaces[billing.id].parts.vault.limit == 5000
        assert server.spaces._spaces[support.id].parts.vault.limit == server.settings.vault_limit


def _named(store: PgVaultStore, count: int) -> list[Entity]:
    """``count`` values kept, each with a word of its own."""
    kept = [_entity(n, f"person{n}.surname{n:04d}@nordest.it", f"fake{n}.name{n:04d}@posta.it") for n in range(count)]
    for e in kept:
        assert store.save_entity(e) is None
    return kept


def _filler(store: PgVaultStore, count: int) -> None:
    """``count`` entries written straight into the tables, each with two words of its own that no text's word hashes
    to: rows a lookup by words never finds, and need not open."""
    tag = store.identity + ":"
    with store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate,"
            " surrogate_key, counter, created) SELECT %(i)s, %(t)s || g, 'EMAIL', 2, '\\x00'::bytea,"
            " sha256(convert_to(%(t)s || g || ':o', 'UTF8')), '\\x00'::bytea,"
            " sha256(convert_to(%(t)s || g || ':s', 'UTF8')), 0, 0 FROM generate_series(1, %(n)s) g",
            {"i": store.identity, "t": tag, "n": count},
        )
        conn.execute(
            "INSERT INTO vault_words (identity_id, entity_id, role, word, version) SELECT %(i)s, %(t)s || g, r,"
            " sha256(convert_to(%(t)s || g || r, 'UTF8')), %(v)s FROM generate_series(1, %(n)s) g,"
            " unnest(ARRAY['original', 'surrogate']) r",
            {"i": store.identity, "t": tag, "n": count, "v": ANCHORS_VERSION},
        )


def _buffers(store: PgVaultStore, query: str, args: tuple) -> int:
    """The pages of PostgreSQL's buffers ``query`` reads (found there or read from disk), as it runs it now."""
    with store.db.connection() as conn:
        [[plan]] = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, args).fetchone()
    top = plan["Plan"] if isinstance(plan, dict) else plan[0]["Plan"]
    return top["Shared Hit Blocks"] + top["Shared Read Blocks"]


def test_a_new_keys_lookup_by_words_reads_what_its_words_find_before_its_statistics_know_it(keys, server, monkeypatch):
    """Task 10's F2: a key PostgreSQL's statistics do not know yet (made after the tables were last analyzed, beside a
    large key that they know) is asked for its values by words as a known one is, through the words first: its lookup
    reads a number of pages that grows with what its words find, never one that grows with the square of its values
    (a scan of its entries, each with a scan of its words: 147 million pages and a minute for 12,000 values)."""
    big, new = keys
    with server.db.connection() as conn:
        for table in ("vault_entities", "vault_words"):  # the statistics stay as they are while the test lasts
            conn.execute(f"ALTER TABLE {table} SET (autovacuum_enabled = false)")
    _filler(big, 30_000)
    with server.db.connection() as conn:
        conn.execute("ANALYZE vault_entities, vault_words")
    _filler(new, 12_000)
    kept = _named(new, 40)
    words = {value_word(e) for e in kept} | {f"w{n:03d}" for n in range(120)}
    asked: list[tuple[str, tuple]] = []
    read = new._read
    monkeypatch.setattr(new, "_read", lambda query, args, **kw: asked.append((query, args)) or read(query, args, **kw))
    assert sorted(new.with_words("original", words), key=lambda e: e.id) == kept
    [(query, args)] = asked
    assert _buffers(new, query, args) < 3_000
    # The values without anchor, and the entries with a NULL word that a build reads again: the same, by their words.
    asked.clear()
    assert new.load_unanchored() == []
    [(query, args)] = asked
    assert _buffers(new, query, args) < 3_000
    assert _buffers(new, vault_module._NULL_WORDS, (new.identity, 0, new.identity, 1_000)) < 3_000


def test_a_line_naming_a_fake_address_and_ip_opens_those_not_the_fakes_that_share_their_domain(keys):
    """Task 10's F3: the fakes at one company share its domain and `example`, the IPv4 fakes their first number; a
    line read for fakes opens the address and the IPv4 address it names, by their dotted words, and no other."""
    mine, _ = keys
    people = [
        _entity(n, f"person{n}@nordest.it", f"{first}.{last}@liberty-d05e.example")
        for n, (first, last) in enumerate((f, la) for f in ("lily", "jack", "ugo") for la in ("harris", "neri", "ross"))
    ]
    hosts = [
        Entity(f"h{n:019d}", "IP_ADDRESS", EntityClass.PII, f"151.12.0.{n}", f"198.51.100.{n}", 0, 1.0, None)
        for n in range(1, 30)
    ]
    for e in (*people, *hosts):
        assert mine.save_entity(e) is None
    line = "WARN auth login failed for Jack.Neri@liberty-d05e.example from 198.51.100.7: invalid password"
    assert sorted(mine.with_words("surrogate", words_in(line, dotted=True)), key=lambda e: e.id) == [
        people[4],
        hosts[6],
    ]


def test_a_line_naming_a_fake_address_opens_it_not_the_fakes_that_share_its_local_part(keys):
    """Version 8: at a million values thousands of address fakes at as many companies share a local part; a line read
    for fakes opens the one it names, by its local part with its domain's name."""
    mine, _ = keys
    shared = [_entity(n, f"p{n}@c{n}.it", f"lily.harris@c{n:03d}-d05e.example") for n in range(50)]
    for e in shared:
        assert mine.save_entity(e) is None
    line = "WARN auth login failed for Lily.Harris@c007-d05e.example: invalid password"
    assert mine.with_words("surrogate", words_in(line, dotted=True)) == [shared[7]]


def test_an_edge_is_kept_under_a_label_of_its_own(keys):
    """Review 10b's M5: a value's edge (``original-head``) is hashed under a label of its own, so that a reader of the
    database does not see that an entry's head is another entry's anchor (a string that begins another); a text's word
    finds both, each by its own hash."""
    mine, _ = keys
    word = _entity(1, "9fQ2xL", "Orsa-7", etype="USERNAME")  # its anchor: 9fq2xl
    secret = _entity(2, "tok_9fQ2xL7pZ4", "tok_Kx81mPq2Ld", etype="API_KEY", eclass=EntityClass.SECRET)
    assert mine.save_entity(word) is None and mine.save_entity(secret) is None
    anchor_hash = _word(mine, word.id, "original")
    head_hash = _word(mine, secret.id, "original-head")
    assert anchor_hash is not None and head_hash is not None and bytes(anchor_hash) != bytes(head_hash)
    assert sorted(e.id for e in mine.with_words("original", words_in("key tok_9fQ2xL7pZ4acme and 9fQ2xL"))) == [
        word.id,
        secret.id,
    ]


def test_a_secret_no_word_finds_glued_is_loaded_with_the_vaults_newest_by_every_instance(server):
    """Review 10b's I1, out of memory (versions 8 and 9): a secret of one run of ``ONE_RUN_HELD`` letters or fewer, or
    whose anchor of six letters or fewer begins or ends it, is found by no word of a text that glues letters or digits
    to it: the store keeps a `held` row for it, and loads the newest of them (``load_held``), so that the space of
    every instance holds them in memory for good and masks them glued; a secret its head finds is not loaded."""
    from latet.config import Config
    from latet.engine.build import build_masker
    from latet.engine.pipeline import Engine, Session
    from latet.engine.stored import StoredVault
    from latet.engine.types import SlotKind

    key, _ = server.identities.add_key("Billing CI")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    keeper = StoredVault(ROOT_KEY, store, working=1_000)
    held = [keeper.intern("API_KEY", EntityClass.SECRET, s) for s in ("ab12", "hunter2", "tok_ab12")]
    found = keeper.intern("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4")
    for n in range(5):
        keeper.intern("EMAIL", EntityClass.PII, f"customer.{n}@gmail.com")
    assert store.load_unanchored() == [] and {e.id for e in store.load_held(10)} == {e.id for e in held}
    vault = StoredVault(ROOT_KEY, store, working=1)  # another instance, built after
    assert {e.id for e in held} <= {e.id for e in vault.entities()} and found.id not in {e.id for e in vault.entities()}
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    texts = ["ref xab12y end", "ref xhunter2y end", "id tok_ab12x end", "key tok_9fQ2xL7pZ4acme"]
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        masked = [
            Engine(vault, masker).mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)).text for t in texts
        ]
    for secret, text in zip(("ab12", "hunter2", "tok_ab12", "tok_9fQ2xL7pZ4"), masked, strict=True):
        assert secret not in text, text


def _tokens(store: PgVaultStore, count: int) -> list[Entity]:
    """``count`` tokens of log lines, each a secret of 16 hex digits held glued (``stored.held_glued``), kept."""
    kept = []
    for n in range(count):
        token = f"{(n * 0x9E3779B97F4A7C15) % (1 << 64):016x}"
        e = _entity(
            100 + n, token, f"{(n * 0xC2B2AE3D27D4EB4F) % (1 << 64):016x}", "GENERIC_API_KEY", EntityClass.SECRET
        )
        assert store.save_entity(e) is None
        kept.append(e)
    return kept


def test_the_secrets_held_glued_are_loaded_newest_first_and_no_more_than_asked(keys):
    """Ruling 10b-R3 (review N1): a space loads the newest of its secrets held glued, as many as it holds
    (``key_held_secrets``), never all of them; the values without anchor apart, all of them. A held secret of seven to
    sixteen letters keeps its edges too: past the bound it is found glued on one side, as a longer token is."""
    mine, _ = keys
    tokens = _tokens(mine, 30)
    wordless = _entity(1, "x9-k2", "q7-z4", "BADGE", EntityClass.CUSTOM)
    assert mine.save_entity(wordless) is None
    assert [e.id for e in mine.load_held(10)] == [e.id for e in tokens[::-1][:10]]
    assert mine.load_unanchored() == [wordless]
    assert _word(mine, tokens[0].id, "original-head") is not None and _word(mine, tokens[0].id, "held") is None
    glued = tokens[0].original + "acme"  # glued after: its head finds it
    assert tokens[0] in mine.with_words("original", words_in(f"key={glued} end"))


def test_a_build_reads_no_held_secret_again(keys, monkeypatch):
    """Review N1: a build of the space read every held secret again (``backfill_words``' NULL words): those have their
    words, and the build reads none of them."""
    mine, _ = keys
    _tokens(mine, 30)
    opened: list[str] = []
    real = PgVaultStore._opened
    monkeypatch.setattr(PgVaultStore, "_opened", lambda self, row: opened.append(row[0]) or real(self, row))
    assert mine.backfill_words() == 0 and opened == []


def test_a_rule_learnt_for_a_value_the_call_keeps_is_kept_with_it(told):
    """Review N2: a rule learnt for a value the same call keeps (its row the call's alone, not committed) is written in
    the call's transaction, and told once it commits; a rule of a value kept before is kept at once, as in fix round
    1."""
    store, heard = told
    a = _entity(1, LAURA, FAKE)
    together = store.together()
    assert store.save_entity(a) is None
    store.save_rule(replace(a, rule="email"))
    together.commit()
    kept = store.load_entity(a.id)
    assert kept is not None and kept.rule == "email"
    assert [(kind, entity_id) for kind, entity_id, _ in heard] == [("entity", a.id), ("rule", a.id)]


def test_a_text_of_too_many_words_has_the_vaults_words_met_in_memory(keys, monkeypatch):
    """Ruling T7c: past ``WORDS_ASKED_AT_MOST`` different words, the vault's own words are read once and met with the
    text's in memory, none dropped: the same values are found, by no statement of words."""
    mine, theirs = keys
    kept = _named(mine, 6)
    assert theirs.save_entity(_entity(99, "person1.surname0001@nordest.it", "x.y@posta.it")) is None
    words = {value_word(e) for e in kept[:4]} | {f"w{n:03d}" for n in range(30)}
    asked = mine.with_words("original", words)
    monkeypatch.setattr(vault_module, "WORDS_ASKED_AT_MOST", 10)
    monkeypatch.setattr(vault_module, "_WITH_WORDS", "SELECT 1 / 0")  # never asked by statements of words
    assert sorted(mine.with_words("original", words), key=lambda e: e.id) == sorted(asked, key=lambda e: e.id)
    assert sorted(asked, key=lambda e: e.id) == kept[:4]
    fakes = {fake_word(e) for e in kept[4:]} | {f"w{n:03d}" for n in range(30)}
    assert sorted(mine.with_words("surrogate", fakes), key=lambda e: e.id) == kept[4:]


@pytest.mark.parametrize("many", [False, True], ids=["by-statements", "met-in-memory"])
def test_a_call_opens_no_more_values_than_it_may(keys, monkeypatch, many):
    """Ruling T7c: a call whose words find more entries than one call may open is refused in words that name none,
    never answered with a part of them; one within the bound is answered whole."""
    mine, _ = keys
    kept = _named(mine, 5)
    monkeypatch.setattr(vault_module, "CANDIDATES_AT_MOST", 3)
    if many:
        monkeypatch.setattr(vault_module, "WORDS_ASKED_AT_MOST", 2)
    opened, count = mine._opened, []
    monkeypatch.setattr(mine, "_opened", lambda row: count.append(1) or opened(row))
    words = {value_word(e) for e in kept}
    with pytest.raises(MaskingError) as refused:
        mine.with_words("original", words)
    assert isinstance(refused.value, vault_module.TooManyCandidates)
    assert "person" not in str(refused.value) and "surname" not in str(refused.value)
    assert len(count) <= 4  # no more than the bound and one opened
    assert len(mine.with_words("original", set(sorted(words)[:3]))) == 3


def test_entries_that_do_not_open_count_toward_what_a_call_may_read(keys, monkeypatch):
    """Review Minor 1: the bound counts the rows read, an entry that does not open as well, so that it never cuts a
    part short and answers without an entry it did not read."""
    mine, _ = keys
    kept = [_entity(n, f"x{n}.lanzavecchia@acme.it", f"f{n}.name@posta.it", etype="USERNAME") for n in range(5)]
    for e in kept:
        assert mine.save_entity(e) is None
    with mine.db.connection() as conn:  # the two oldest no longer open (their values written over by hand)
        conn.execute(
            "UPDATE vault_entities SET original = surrogate WHERE identity_id = %s AND id = ANY(%s)",
            (mine.identity, [kept[0].id, kept[1].id]),
        )
    monkeypatch.setattr(vault_module, "CANDIDATES_AT_MOST", 3)
    with pytest.raises(vault_module.TooManyCandidates):
        mine.with_words("original", {"lanzavecchia"})


def test_entries_by_their_ids(keys):
    mine, theirs = keys
    kept = _named(mine, 3)
    their = _entity(9, "m.conti@posta.it", "Orsa-7", etype="USERNAME")
    assert theirs.save_entity(their) is None
    assert mine.by_ids([kept[2].id, kept[0].id, kept[0].id, their.id, "e-none"]) == [kept[0], kept[2]]
    assert mine.by_ids([]) == []
    mine.forget_entity(kept[0].id)
    assert mine.by_ids([e.id for e in kept]) == kept[1:]


def test_words_of_a_later_version_of_the_anchors_are_left_as_they_are(keys):
    """Ruling T7d: while the instances are upgraded one by one, an older Latet neither counts as missing nor makes again
    the words a newer one made, a NULL one included."""
    mine, _ = keys
    kept = _named(mine, 3)
    with mine.db.connection() as conn:  # as a newer Latet wrote them, one with no word
        conn.execute("UPDATE vault_words SET version = %s WHERE identity_id = %s", (ANCHORS_VERSION + 1, mine.identity))
        conn.execute(
            "UPDATE vault_words SET word = NULL WHERE identity_id = %s AND entity_id = %s AND role = 'original'",
            (mine.identity, kept[0].id),
        )
    assert mine.backfill_words() == 0
    assert _versions(mine) == {ANCHORS_VERSION + 1}
    assert _word(mine, kept[0].id, "original") is None  # a newer Latet's NULL stays its own


# The migrations of Latet 0.9.10, the last release, in order: a server of that release has these, and only these.
RELEASED = ("001_core", "002_admin_token_issuer", "003_audit_seek", "004_computer_key", "005_vault_decoded")
OWN = [
    "latet.server.migrations:006_token_permissions",
    "latet.server.migrations:007_key_vaults",
    "latet.server.migrations:008_vault_word_edges",
    "latet.server.migrations:009_vault_held",
    "latet.server.migrations:010_forget_permission",
    "latet.server.migrations:011_added_token_permissions",
    "latet.server.migrations:012_retired_fakes",
]


def _columns(db, table: str) -> set[str]:
    with db.connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (db.schema, table),
        )}  # fmt: skip


def _tables(db) -> set[str]:
    with db.connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (db.schema,)
        )}  # fmt: skip


def test_a_server_of_0_9_10_takes_the_api_keys_migrations_after_its_own(settings, schema, monkeypatch):
    """A database of 0.9.10 (its five migrations, 005_vault_decoded the last) is migrated to this Latet's: a key's
    permissions (006) and its vault read as needed (007) come after, an instance refusing the schema before; a value
    0.9.10 kept, found where escapes read, keeps how it was found, and gets its words once the vault is built."""
    from latet.server.db import Database, SchemaMismatch, check_schema, migrate
    from latet.server.db import _migrations as every

    from .conftest import identity_in

    released = [(name, text) for name, text in every(["latet.server.migrations"]) if name.split(":")[1] in RELEASED]
    db = Database(settings.database_url, schema=schema, max_size=2)
    try:
        with monkeypatch.context() as at_0_9_10:
            at_0_9_10.setattr("latet.server.db._migrations", lambda _packages: released)
            assert [name.split(":")[1] for name in migrate(db)] == list(RELEASED)
            check_schema(db)
            identity = identity_in(db, "k_billingci0", "Billing CI", kind="key")
            at_0_9_10.setattr(vault_module, "_WORDS", "SELECT %s, %s, %s, %s, %s, %s, %s, %s")  # 0.9.10 keeps none
            kept = replace(_entity(1, LAURA, FAKE), decoded=True)
            assert PgVaultStore(db, identity, ROOT_KEY).save_entity(kept) is None
        assert "vault_words" not in _tables(db) and "permissions" not in _columns(db, "tokens")
        with pytest.raises(SchemaMismatch, match="older than this Latet"):
            check_schema(db)
        assert migrate(db) == OWN
        check_schema(db)
        assert "permissions" in _columns(db, "tokens") and "vault_limit" in _columns(db, "identities")
        store = PgVaultStore(db, identity, ROOT_KEY)
        assert store.with_words("original", {"laura.bianchi"}) == [] and store.backfill_words() == 1
        assert store.with_words("original", {"laura.bianchi"}) == [kept]
        assert store.with_words("surrogate", {"g.rossi@posta"}) == [kept] and store.load_entity(kept.id) == kept
    finally:
        db.close()


def test_a_new_server_applies_every_migration_in_the_order_of_its_number(settings, schema):
    from latet.server.db import Database, check_schema, migrate

    db = Database(settings.database_url, schema=schema, max_size=2)
    try:
        applied = migrate(db)
        check_schema(db)
        assert [name.split(":")[1] for name in applied[: len(RELEASED)]] == list(RELEASED)
        assert applied[len(RELEASED) :] == OWN and applied == sorted(applied)
    finally:
        db.close()
