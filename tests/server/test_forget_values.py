"""Forgetting values of an API key's vault (Task 11b), in its store: each value found exactly, by its keyed hash, and
deleted with everything that names it (its entry, its strings, its words and edges), with the local reviewer's
remembered answers that hold it and the rows of the key's history that name its entry, in one transaction, the counter
moved once and each told once it is committed; the values the vault does not hold counted, never named. Another key's
vault, and the values not asked for, keep theirs."""

import json
import threading
import time
from dataclasses import replace

import psycopg
import pytest

from latet.engine.types import EntityClass
from latet.engine.vault import Entity, FakeTaken, fake_reading
from latet.server import history as history_module
from latet.server import vault as vault_module
from latet.server.history import PgHistoryStore
from latet.server.vault import PgVaultStore
from latet.storage.exchanges import BlockRecord, ExchangeRecord

from ..conftest import ROOT_KEY

pytestmark = pytest.mark.server

LAURA = "Laura.Bianchi@nordest.it"
SECRET = "sk-live-4f7Qz9Lm2Xc8Vb1Nn6Kd"  # a secret: found inside a word, kept with its edges


def _entity(n: int, value: str, fake: str, etype: str = "EMAIL", eclass: EntityClass = EntityClass.PII) -> Entity:
    return Entity(f"e{n:019d}", etype, eclass, value, fake, 0, time.time(), None)


def _rows(store: PgVaultStore, table: str, entity_id: str | None = None) -> int:
    """How many rows of ``table`` the store's identity has (naming ``entity_id``, when given)."""
    column = {"vault_entities": "id", "vault_words": "entity_id", "vault_strings": "entity_id"}.get(table)
    query = f"SELECT COUNT(*) FROM {table} WHERE identity_id = %s"
    args: list[str] = [store.identity]
    if entity_id is not None and column is not None:
        query += f" AND ({column} = %s" + (" OR also = %s)" if table == "vault_strings" else ")")
        args += [entity_id] * (2 if table == "vault_strings" else 1)
    with store.db.connection() as conn:
        return conn.execute(query, args).fetchone()[0]


def _counter(store: PgVaultStore) -> int:
    with store.db.connection() as conn:
        found = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (store.identity,)).fetchone()
    return found[0] if found else 0


@pytest.fixture
def keys(server):
    """Two API keys' vaults on one server, with one root key, each telling what it changes."""
    told: list[tuple[str, str, str, int]] = []
    billing, _ = server.identities.add_key("Billing CI")
    support, _ = server.identities.add_key("Support bot")

    def telling(identity):
        return lambda kind, entity_id, counter: told.append((identity, kind, entity_id, counter))

    mine = PgVaultStore(server.db, billing.id, ROOT_KEY, changed=telling(billing.id))
    theirs = PgVaultStore(server.db, support.id, ROOT_KEY, changed=telling(support.id))
    return mine, theirs, told


def test_values_are_forgotten_with_everything_that_names_them(keys):
    mine, theirs, told = keys
    laura, secret, kept = (
        _entity(1, LAURA, "g.rossi@posta.it"),
        _entity(2, SECRET, "sk-live-Zp3Rt6Wq0Ys5Ua8Ic2Oe", "SECRET", EntityClass.SECRET),
        _entity(3, "m.conti@acme.it", "p.galli@posta.it"),
    )
    for e in (laura, secret, kept):
        assert mine.save_entity(e) is None
    assert theirs.save_entity(_entity(4, LAURA, "a.neri@posta.it")) is None  # the same value, another key's
    mine.count = 3
    assert _rows(mine, "vault_words", secret.id) > 2  # a secret's edges too
    before = _counter(mine)
    told.clear()
    forgotten, unknown, generation = mine.forget_values([LAURA, SECRET, "nobody@nowhere.example", LAURA])
    assert sorted(forgotten) == sorted([laura.id, secret.id]) and unknown == 1  # each value counted once
    assert generation == 1  # the generation it moved to (none forgotten: None)
    for e in (laura, secret):
        for table in ("vault_entities", "vault_words", "vault_strings"):
            assert _rows(mine, table, e.id) == 0, table
        assert mine.by_original(e.original) is None and mine.by_surrogate(e.surrogate) is None
    assert mine.load_entity(kept.id) == kept and _rows(mine, "vault_words", kept.id) == 2
    assert theirs.by_original(LAURA) is not None  # another key's vault keeps its own
    assert mine.count == 1
    # The counter moved once by as many, and each was told once committed, with a count of its own.
    assert _counter(mine) == before + 2
    counts = (before + 1, before + 2)
    assert told == [
        (mine.identity, "forgotten", entity_id, count) for entity_id, count in zip(forgotten, counts, strict=True)
    ]


def test_a_value_is_found_exactly_as_given(keys):
    """The value as it was kept: another case, other spaces or another written form are other values (the vault keeps
    each form it met as an entry of its own), and are counted as unknown."""
    mine, _, _ = keys
    assert mine.save_entity(_entity(1, LAURA, "g.rossi@posta.it")) is None
    assert mine.forget_values([LAURA.lower(), f" {LAURA}", LAURA.replace("@", "%40"), ""])[:2] == ([], 4)
    assert mine.by_original(LAURA) is not None


def test_nothing_asked_forgets_nothing_and_moves_no_counter(keys):
    mine, _, told = keys
    assert mine.save_entity(_entity(1, LAURA, "g.rossi@posta.it")) is None
    before = _counter(mine)
    told.clear()
    assert mine.forget_values([])[:2] == ([], 0)
    assert mine.forget_values(["nobody@nowhere.example"]) == ([], 1, None)  # no generation moved
    assert _counter(mine) == before and told == []


def test_a_string_that_is_another_values_too_stays_that_values(keys):
    """A value equal to another entry's fake: forgetting the fake's entry leaves the string the value's (as one Forget
    does), and retired; forgetting the value leaves the fake its entry's; and forgetting both leaves nothing of either
    but their fakes, retired, of no entry."""
    mine, _, _ = keys
    first = _entity(1, "a.rossi@acme.it", "b.verdi@posta.it")
    second = _entity(2, "b.verdi@posta.it", "c.neri@posta.it")  # a real address equal to the first one's fake
    assert mine.save_entity(first) is None and mine.save_entity(second) is None
    assert mine.forget_values(["a.rossi@acme.it"])[:2] == ([first.id], 0)
    assert mine.by_original("b.verdi@posta.it") == second
    assert _rows(mine, "vault_strings", second.id) == 2  # its value's string and its fake's
    mine.generation = mine.load_generation()  # what a vault drawing here reads at its next value
    with pytest.raises(FakeTaken, match="fake"):  # the string stays known: no fake may be it
        mine.save_entity(_entity(3, "d.galli@acme.it", "b.verdi@posta.it"))
    again = _entity(4, "a.rossi@acme.it", "e.conti@posta.it")
    assert mine.save_entity(again) is None
    third = _entity(5, "e.conti@posta.it", "f.sala@posta.it")
    assert mine.save_entity(third) is None
    assert sorted(mine.forget_values(["a.rossi@acme.it", "e.conti@posta.it"])[0]) == sorted([again.id, third.id])
    assert _rows(mine, "vault_strings", again.id) == _rows(mine, "vault_strings", third.id) == 0
    assert _rows(mine, "vault_strings") == len(_retired(mine)) + 1  # the second's fake's: its value's is retired


def _review(
    store: PgVaultStore, text: str, findings: list[list[str]], unlocated: list[list[str]] | None = None
) -> bytes:
    key = text.encode().ljust(32, b"\x00")[:32]
    answer: dict = {"findings": findings, "block": False}
    if unlocated is not None:
        answer["unlocated"] = unlocated
    store.save_review(key, answer)
    return key


def test_the_reviewers_answers_that_hold_a_value_go_with_it(keys):
    """The local reviewer's remembered answers (sealed, their findings values in clear) that found a value asked for go
    with it, whether the vault held it or not; the others stay."""
    mine, theirs, _ = keys
    assert mine.save_entity(_entity(1, LAURA, "g.rossi@posta.it")) is None
    holding = _review(mine, "one", [[LAURA, "EMAIL"]])
    unplaced = _review(mine, "two", [], [["Maria Bianchi", "PERSON"]])  # seen in an image, not placed
    other = _review(mine, "three", [["Paolo Conti", "PERSON"]])
    part = _review(mine, "four", [[f"{LAURA} and more", "NOTE"]])  # a longer finding is another value
    elsewhere = _review(theirs, "one", [[LAURA, "EMAIL"]])
    assert mine.forget_values([LAURA, "Maria Bianchi"])[:2] == ([_entity(1, LAURA, "").id], 1)
    assert mine.load_review(holding) is None and mine.load_review(unplaced) is None
    assert mine.load_review(other) is not None and mine.load_review(part) is not None
    assert theirs.load_review(elsewhere) is not None


def test_what_runs_first_and_last_is_in_the_same_transaction(keys):
    """``first`` before any row of the vault is touched, ``last`` after them with the counts, both in the transaction
    that forgets: one that raises forgets nothing, moves no counter and tells nothing."""
    mine, _, told = keys
    e = _entity(1, LAURA, "g.rossi@posta.it")
    assert mine.save_entity(e) is None
    review = _review(mine, "one", [[LAURA, "EMAIL"]])
    mine.count, before = 1, _counter(mine)
    told.clear()
    seen: list = []

    def failing(conn, forgotten, unknown):
        seen.append((forgotten, unknown, conn.execute("SELECT COUNT(*) FROM vault_entities").fetchone()[0]))
        raise RuntimeError("the administration log did not take it")

    with pytest.raises(RuntimeError):
        mine.forget_values([LAURA, "x@y.example"], first=lambda conn: seen.append("first"), last=failing)
    assert seen == ["first", (1, 1, 0)]  # the entry deleted in the transaction when it ran
    assert mine.load_entity(e.id) == e and mine.load_review(review) is not None
    assert mine.count == 1 and _counter(mine) == before and told == []
    done: list = []
    assert mine.forget_values([LAURA], last=lambda conn, n, m: done.append((n, m)))[:2] == ([e.id], 0)
    assert done == [(1, 0)] and mine.load_entity(e.id) is None


def test_a_forget_waits_for_a_locked_row_no_longer_than_its_bound(keys, monkeypatch):
    """A row another transaction holds (a call keeping its values together) is waited for ``FORGET_LOCK_SECONDS`` at
    most, then the forget fails, nothing of it done, to be tried again."""
    monkeypatch.setattr(vault_module, "FORGET_LOCK_SECONDS", 1)
    mine, _, _ = keys
    e = _entity(1, LAURA, "g.rossi@posta.it")
    assert mine.save_entity(e) is None
    done: list[BaseException | None] = []

    def forget() -> None:
        try:
            mine.forget_values([LAURA])
            done.append(None)
        except BaseException as exc:
            done.append(exc)

    with psycopg.connect("", **mine.db._parameters) as locker:
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
    assert mine.load_entity(e.id) == e
    assert mine.forget_values([LAURA])[:2] == ([e.id], 0)


def _generation(store: PgVaultStore) -> int:
    with store.db.connection() as conn:
        found = conn.execute(
            "SELECT counter FROM change_counters WHERE owner = %s", (f"{store.identity}:forgets",)
        ).fetchone()
    return found[0] if found else 0


def test_a_forget_moves_the_generation_new_fakes_are_drawn_in(keys):
    """A value's fake is drawn from the value (the same value, the same fake), so a value forgotten and met again would
    get its old fake back, which the texts masked before it was forgotten hold: each forget that forgot something moves
    the vault's generation, which new fakes are drawn in, and the store refuses a value drawn in an older one (another
    instance that has not heard yet), the vault drawing it again in the new one."""
    from latet.server.vault import OlderGeneration

    mine, theirs, _ = keys
    assert mine.load_generation() == 0 and mine.generation == 0
    first = _entity(1, LAURA, "g.rossi@posta.it")
    assert mine.save_entity(first) is None
    assert mine.forget_values(["nobody@nowhere.example"])[:2] == ([], 1)
    assert _generation(mine) == 0  # nothing forgotten: the same generation
    assert mine.forget_values([LAURA])[:2] == ([first.id], 0)
    assert _generation(mine) == 1 and theirs.load_generation() == 0  # each key its own
    assert mine.generation == 0  # not read: the vault that draws learns it at its next value
    again = _entity(1, LAURA, "g.rossi@posta.it")  # drawn in generation 0: its old fake
    with pytest.raises(OlderGeneration):
        mine.save_entity(again)
    assert mine.generation == 1 and mine.by_original(LAURA) is None  # refused, and the generation read
    assert mine.save_entity(_entity(1, LAURA, "l.conti@posta.it")) is None  # drawn in the new one
    assert theirs.save_entity(_entity(5, "a@b.example", "c@d.example")) is None  # another key's, in its own


@pytest.mark.parametrize("together", [False, True], ids=["alone", "together"])
def test_a_value_kept_while_a_forget_commits_never_gets_its_old_fake(keys, together):
    """Review 11b's I1: another instance, in generation 0 (not told yet), keeps the value while the forget has deleted
    its entry and not committed (its ``last``, the log's entry, waits): its INSERT waits for the forget on the value's
    unique index, then finds the entry gone, the generation it read at its start the old one. It must not keep the fake
    drawn in generation 0, the old one: refused (OlderGeneration), the generation read, nothing kept; drawn in the new
    one, it is kept."""
    from latet.server.vault import OlderGeneration

    mine, _, _ = keys
    other = PgVaultStore(mine.db, mine.identity, ROOT_KEY)
    first = _entity(1, SECRET, "sk-live-Zp3Rt6Wq0Ys5Ua8Ic2Oe", "SECRET", EntityClass.SECRET)
    assert mine.save_entity(first) is None
    assert other.load_generation() == 0
    in_last, done = threading.Event(), []

    def slow_log(conn, forgotten, unknown):  # the log's entry, waiting on its chain
        in_last.set()
        time.sleep(1.5)

    forgetting = threading.Thread(target=lambda: done.append(mine.forget_values([SECRET], last=slow_log)))
    forgetting.start()
    try:
        assert in_last.wait(10)
        joined = other.together() if together else None
        try:
            with pytest.raises(OlderGeneration):
                other.save_entity(first)
        finally:
            if joined is not None:
                joined.rollback()
    finally:
        forgetting.join(30)
    assert [found[:2] for found in done] == [([first.id], 0)]
    assert other.generation == 1
    assert mine.by_original(SECRET) is None and _rows(mine, "vault_strings") == len(_retired(mine)) == 1  # its fake
    assert other.save_entity(_entity(1, SECRET, "sk-live-Hq5Wb8Nm1Zx4Cv7Lp0Tr", "SECRET", EntityClass.SECRET)) is None


def _value_keys(store: PgVaultStore, key: bytes) -> list[bytes] | None:
    """The keyed hashes an answer is kept with (None: none yet, as an older release kept it)."""
    with store.db.connection() as conn:
        row = conn.execute(
            "SELECT value_keys FROM reviews WHERE identity_id = %s AND key = %s", (store.identity, key)
        ).fetchone()
    return None if row[0] is None else [bytes(k) for k in row[0]]


def _unindexed(conn, identity: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE identity_id = %s AND value_keys IS NULL", (identity,)
    ).fetchone()[0]


def test_an_answer_is_kept_with_its_values_keyed_hashes_and_found_by_them(keys, monkeypatch):
    """Review 11b's I2: each answer is kept with the keyed hashes of the values it found, those it could not place too
    (none of them in clear), and a forget finds the answers that hold a value by them, through an index: it opens no
    answer, however many the key has."""
    mine, _, _ = keys
    holding = _review(mine, "one", [[LAURA, "EMAIL"]], [["Maria Bianchi", "PERSON"]])
    other = _review(mine, "two", [["Paolo Conti", "PERSON"]])
    hashed = _value_keys(mine, holding)
    assert hashed is not None and len(hashed) == 2 and all(LAURA.encode() not in k for k in hashed)
    opened: list[str] = []
    real = mine._sealer.open
    monkeypatch.setattr(mine._sealer, "open", lambda blob, aad: opened.append(aad) or real(blob, aad))
    assert mine.forget_values([LAURA])[:2] == ([], 1)
    assert opened == []
    monkeypatch.undo()
    assert mine.load_review(holding) is None and mine.load_review(other) is not None


def test_answers_kept_before_are_indexed_before_the_forget_a_part_at_a_time(keys, monkeypatch):
    """The answers an older release kept have no keyed hashes: a forget gives them theirs first, a part at a time
    (``ROWS_AT_ONCE``), before its own transaction begins, then finds them as every other; an answer that does not
    open gets none, and is never opened again."""
    monkeypatch.setattr(vault_module, "ROWS_AT_ONCE", 7)
    mine, _, _ = keys
    held = [_review(mine, f"text {n}", [[LAURA if n % 10 == 0 else f"v{n}", "EMAIL"]]) for n in range(40)]
    stranger = b"x" * 32
    with mine.db.connection() as conn:
        conn.execute("UPDATE reviews SET value_keys = NULL WHERE identity_id = %s", (mine.identity,))
        conn.execute(
            "INSERT INTO reviews (identity_id, key, data, ts) VALUES (%s, %s, %s, %s)",
            (mine.identity, stranger, b"sealed with another key", time.time()),
        )
    seen: list[int] = []
    assert mine.forget_values([LAURA], first=lambda conn: seen.append(_unindexed(conn, mine.identity)))[:2] == ([], 1)
    assert seen == [0]  # every answer had its keyed hashes when the forget's transaction began
    left = [key for key in held if mine.load_review(key) is not None]
    assert len(left) == 36 and all(_value_keys(mine, key) for key in left)
    assert LAURA not in json.dumps([mine.load_review(k) for k in left])
    assert _value_keys(mine, stranger) == []


def test_a_forget_takes_no_longer_with_many_answers(keys):
    """Review 11b's I2: a key with 200,000 remembered answers, none holding the value but one: the forget finds that
    one by the index, in well under a second (it opened every answer before, some 7 microseconds each, and fetched
    them all)."""
    mine, _, _ = keys
    with mine.db.connection() as conn:
        conn.execute(
            "INSERT INTO reviews (identity_id, key, data, ts, value_keys)"
            " SELECT %s, sha256(int4send(n)), sha256(int4send(-n)), %s, ARRAY[sha256(int4send(n + 1000000))]"
            " FROM generate_series(1, 200000) AS n",
            (mine.identity, time.time()),
        )
        conn.execute("ANALYZE reviews")
    holding = _review(mine, "one", [[LAURA, "EMAIL"]])
    started = time.perf_counter()
    assert mine.forget_values([LAURA])[:2] == ([], 1)
    took = time.perf_counter() - started
    assert mine.load_review(holding) is None
    assert took < 1.0, f"{took:.2f} s"


def _span(e: Entity) -> dict:
    return {"masked_start": 0, "masked_end": len(e.surrogate), "type": e.etype, "class": "PII", "entity": e.id}


def _exchange(history: PgHistoryStore, session: str, text: str, spans: list, said: list | None = None) -> str:
    """An exchange of the history: a block of ``text`` whose replacements name ``spans``' entries, and the model's
    answer, naming ``said``'s."""
    block = BlockRecord(history.block_id(text), "$", "user_text", False, True, text, spans)
    response = {"text": "ok", "text_spans": said or [], "tool_calls": []}
    summary = {"ts": time.time(), "session": session, "agent": "codex", "flags": []}
    return history.record(ExchangeRecord(summary, [block], response=response))


def _record(history: PgHistoryStore, *entities: Entity) -> str:
    """A record of the local reviewer's findings, each naming its entry."""
    found = [{"category": "EMAIL", "masked": True, "allowed": False, "entity": e.id, "at": None} for e in entities]
    return history.record_review(
        {"ts": time.time(), "session": "s1", "project": None, "block": "b", "kind": "text", "findings": found}
    )


def _history_bytes(history: PgHistoryStore) -> tuple[int, int]:
    """The bytes the history's count says it keeps, and those its rows take."""
    with history.db.connection() as conn:
        counted = conn.execute(
            "SELECT bytes FROM history_bytes WHERE identity_id = %s", (history.identity,)
        ).fetchone()[0]
        kept = conn.execute(history_module._KEPT, {"identity": history.identity}).fetchone()[0]
    return int(counted), int(kept)


def test_the_history_rows_that_name_a_forgotten_entry_go_with_it(keys):
    """Ruling 11b-R1: the key's history keeps what names a value's entry (a block's replacements, the model's answer,
    a reviewer's record of a finding), with its fake, until its retention: those rows go with the value, found by the
    keyed hashes of the entries each row names (an index, in the forget's transaction), and the history's count of
    bytes with them; the rows that name other entries stay, and another key's history keeps its own."""
    mine, theirs, _ = keys
    history, elsewhere = (PgHistoryStore(s.db, s.identity, ROOT_KEY) for s in (mine, theirs))
    laura, other = _entity(1, LAURA, "g.rossi@posta.it"), _entity(2, "m.conti@acme.it", "p.galli@posta.it")
    for e in (laura, other):
        assert mine.save_entity(e) is None
    named = _exchange(history, "s1", f"to {laura.surrogate}", [_span(laura)])
    answered = _exchange(history, "s1", "who is it", [], [_span(laura)])
    kept = _exchange(history, "s2", f"to {other.surrogate}", [_span(other)])
    record, other_record = _record(history, laura), _record(history, other)
    theirs_too = _exchange(elsewhere, "s1", f"to {laura.surrogate}", [_span(laura)])
    forgotten = mine.forget_values([LAURA], gone=history.forget_entities)
    assert forgotten[:2] == ([laura.id], 0)
    assert history.detail(answered) is None  # its answer named it
    assert history.detail(named)["request"]["slots"] == []  # its block named it: the exchange keeps its counts alone
    assert history.known_blocks([history.block_id(f"to {laura.surrogate}")]) == set()
    assert history.known_blocks([history.block_id("who is it")])  # it names none: it stays
    assert history.detail(kept) is not None and [r["id"] for r in history.reviews()] == [other_record]
    assert record != other_record and elsewhere.detail(theirs_too) is not None
    counted, rows = _history_bytes(history)
    assert counted == rows


def test_what_names_nothing_forgotten_moves_nothing(keys):
    mine, _, _ = keys
    history = PgHistoryStore(mine.db, mine.identity, ROOT_KEY)
    laura = _entity(1, LAURA, "g.rossi@posta.it")
    assert mine.save_entity(laura) is None
    kept = _exchange(history, "s1", f"to {laura.surrogate}", [_span(laura)])
    before = _history_bytes(history)
    assert mine.forget_values(["nobody@nowhere.example"], gone=history.forget_entities)[:2] == ([], 1)
    assert history.detail(kept) is not None and _history_bytes(history) == before


def test_history_rows_kept_before_are_given_their_entries_first_a_part_at_a_time(keys, monkeypatch):
    """The rows an older release kept name no entry beside them: a forget's caller gives them theirs first
    (``index_entities``), a part at a time, outside the forget's transaction; then the forget finds them as every
    other. A row that does not open names none, and is never opened again."""
    monkeypatch.setattr(history_module, "ROWS_AT_ONCE", 2)
    mine, _, _ = keys
    history = PgHistoryStore(mine.db, mine.identity, ROOT_KEY)
    laura, other = _entity(1, LAURA, "g.rossi@posta.it"), _entity(2, "m.conti@acme.it", "p.galli@posta.it")
    for e in (laura, other):
        assert mine.save_entity(e) is None
    named = [_exchange(history, f"s{n}", f"to {laura.surrogate} {n}", [_span(laura)]) for n in range(3)]
    answered = _exchange(history, "s9", "who is it", [], [_span(laura)])
    kept = [_exchange(history, f"o{n}", f"to {other.surrogate} {n}", [_span(other)]) for n in range(3)]
    record, other_record = _record(history, laura), _record(history, other)
    with mine.db.connection() as conn:
        for table in ("blocks", "exchanges", "review_records"):
            conn.execute(f"UPDATE {table} SET entity_keys = NULL WHERE identity_id = %s", (mine.identity,))
        conn.execute(
            "INSERT INTO blocks (identity_id, id, kind, data, last_seen) VALUES (%s, 'stranger', 'user_text', %s, %s)",
            (mine.identity, b"sealed with another key", time.time()),
        )
    assert history.index_entities() == 8 + 7 + 2  # blocks (the stranger too), exchanges, the reviewer's records
    assert history.index_entities() == 0
    assert mine.forget_values([LAURA], gone=history.forget_entities)[:2] == ([laura.id], 0)
    assert history.detail(answered) is None and all(history.detail(x)["request"]["slots"] == [] for x in named)
    assert all(history.detail(x) is not None for x in kept)
    assert [r["id"] for r in history.reviews()] == [other_record] and record != other_record
    with mine.db.connection() as conn:
        stranger = conn.execute(
            "SELECT entity_keys FROM blocks WHERE identity_id = %s AND id = 'stranger'", (mine.identity,)
        ).fetchone()[0]
    assert stranger == []


# --- a fake its entry no longer has is retired (the final security review's H2) ---------------------------------------


def _retired(store: PgVaultStore) -> list[tuple[bytes, str | None]]:
    """The identity's retired fakes: each row's key, and the entry it is the value of as well, if any."""
    with store.db.connection() as conn:
        rows = conn.execute(
            "SELECT key, also FROM vault_strings WHERE identity_id = %s AND role = 'retired' AND entity_id IS NULL",
            (store.identity,),
        ).fetchall()
    return [(bytes(key), also) for key, also in rows]


FAKE = "g.rossi@posta.it"
OTHER = "m.conti@acme.it"


@pytest.mark.parametrize("how", ["by the value", "by the entry"])
def test_a_forgotten_values_fake_is_given_to_no_other_value(keys, how):
    """The final security review's H2: a forget deleted the string of the value's fake, so another value could draw it
    again (a person's name after 3,036 new ones), and an unmask of a text masked before then gave that other person's
    value. The fake is retired: its row stays, its keyed hash alone, of no entry, and the store refuses it to any value
    (FakeTaken: the vault draws again), whichever way the value was forgotten, by itself (an API key's forget) or by
    its entry (the dashboard's)."""
    mine, theirs, _ = keys
    laura = _entity(1, LAURA, FAKE)
    assert mine.save_entity(laura) is None
    if how == "by the value":
        assert mine.forget_values([LAURA])[:2] == ([laura.id], 0)
        mine.generation = mine.load_generation()  # what a vault drawing here reads at its next value
    else:
        mine.forget_entity(laura.id)
    assert mine.by_original(LAURA) is None and mine.by_surrogate(FAKE) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_entity(2, OTHER, FAKE))
    assert mine.by_original(OTHER) is None
    assert mine.save_entity(_entity(2, OTHER, "p.galli@posta.it")) is None  # drawn again: another fake
    [(key, also)] = _retired(mine)
    assert len(key) == 32 and FAKE.encode() not in key and LAURA.encode() not in key and also is None
    assert theirs.save_entity(_entity(3, OTHER, FAKE)) is None  # another key's vault: its own strings


def test_a_fake_given_anew_retires_the_old_one(keys):
    """The third way a fake left its entry: given anew (``replace_fake``, one that showed its value). The old one is
    retired too, never another value's."""
    mine, _, _ = keys
    laura = _entity(1, LAURA, FAKE)
    assert mine.save_entity(laura) is None
    assert mine.replace_fake(laura, replace(laura, surrogate="l.verdi@posta.it", counter=1)) is None
    with pytest.raises(FakeTaken):
        mine.save_entity(_entity(2, OTHER, FAKE))
    assert len(_retired(mine)) == 1


def test_how_a_retired_fake_reads_is_retired_with_it(keys):
    """A fake written in escapes is kept with how it reads (``fake_reading``): that string is retired with it, so that
    no new fake is it, nor reads as it."""
    mine, _, _ = keys
    linked = Entity(
        "e" + "1" * 19, "PERSON", EntityClass.NER, "Utente%20Uno", "Sara%20Galli", 0, time.time(), "ner", True
    )
    assert fake_reading(linked) == "Sara Galli"
    assert mine.save_entity(linked) is None
    assert mine.forget_values(["Utente%20Uno"])[:2] == ([linked.id], 0)
    assert len(_retired(mine)) == 2 and mine.load_generation() == 1
    mine.generation = 1  # what a vault drawing here reads at its next value
    for fake in ("Sara Galli", "Sara%20Galli"):
        with pytest.raises(FakeTaken):
            mine.save_entity(_entity(2, "Ida Russo", fake, "PERSON", EntityClass.NER))
    assert mine.save_entity(_entity(2, "Ida Russo", "Rita Sala", "PERSON", EntityClass.NER)) is None


def test_a_retired_fake_stays_when_an_entry_of_the_same_id_is_forgotten_again(keys):
    """A person's value forgotten by its entry and met again has the same id (generation 0): its new entry, forgotten
    in turn, takes its own strings, never the retired fake of the first one, which stays as long as the vault."""
    mine, _, _ = keys
    first = _entity(1, LAURA, FAKE)
    assert mine.save_entity(first) is None
    mine.forget_entity(first.id)
    again = _entity(1, LAURA, "l.verdi@posta.it")
    assert mine.save_entity(again) is None
    mine.forget_entity(again.id)
    assert len(_retired(mine)) == 2 and _rows(mine, "vault_strings") == 2


def test_a_value_equal_to_a_forgotten_fake_keeps_it_retired(keys):
    """A real value equal to a fake given before: the fake's row names its entry as well (``also``). Forgetting the
    fake's entry leaves the string retired and that value's; forgetting the value then leaves it retired, so that no
    new fake is the old one."""
    mine, _, _ = keys
    first = _entity(1, "a.rossi@acme.it", "b.verdi@posta.it")
    second = _entity(2, "b.verdi@posta.it", "c.neri@posta.it")  # a real address equal to the first one's fake
    assert mine.save_entity(first) is None and mine.save_entity(second) is None
    mine.forget_entity(first.id)
    assert mine.by_original("b.verdi@posta.it") == second
    assert [also for _, also in _retired(mine)] == [second.id]
    mine.forget_entity(second.id)
    assert sorted(also or "" for _, also in _retired(mine)) == ["", ""]  # its own fake retired too
    with pytest.raises(FakeTaken):
        mine.save_entity(_entity(3, "d.galli@acme.it", "b.verdi@posta.it"))


def _keys_kept(db, identity: str) -> dict[str, list[list[bytes]]]:
    """The keyed hashes each row of the identity's history and reviewer's answers keeps of what it names."""
    found: dict[str, list[list[bytes]]] = {}
    with db.connection() as conn:
        for table, column in (("blocks", "entity_keys"), ("exchanges", "entity_keys"), ("review_records",
                              "entity_keys"), ("reviews", "value_keys")):  # fmt: skip
            rows = conn.execute(f"SELECT {column} FROM {table} WHERE identity_id = %s", (identity,)).fetchall()
            found[table] = [[bytes(key) for key in row[0]] if row[0] is not None else None for row in rows]
    return found


@pytest.mark.parametrize("kind", ["key", "person"])
def test_only_an_api_keys_history_and_answers_keep_the_hashes_a_forget_finds_them_by(server, kind):
    """The final review's M6 and the security review's L3 (Ruling F1): the keyed hashes of the entries and values a
    history's row or a reviewer's answer names are what a forget of values by themselves finds them by, which an API key
    alone does. A person's rows keep none (an empty list, not NULL: nothing to index later): no index written for them,
    and nothing that tells a database reader which of a person's rows name the same entry or value."""
    from latet.server.backends import space_backends

    if kind == "key":
        identity, _ = server.identities.add_key("Support")
    else:
        identity, _ = server.identities.add_person("Emily Carter")
    parts = space_backends(server, identity.id, ROOT_KEY, kind=kind)
    laura = _entity(1, LAURA, FAKE)
    assert parts.vault.save_entity(laura) is None
    _exchange(parts.history, "s1", f"to {laura.surrogate}", [_span(laura)], [_span(laura)])
    _record(parts.history, laura)
    _review(parts.vault, "one", [[LAURA, "EMAIL"]])
    kept = _keys_kept(server.db, identity.id)
    assert all(len(rows) == 1 for rows in kept.values()), kept
    if kind == "key":
        assert all(len(rows[0]) == 1 for rows in kept.values()), kept
    else:
        assert all(rows == [[]] for rows in kept.values()), kept


def test_an_unmask_of_a_text_masked_before_a_forget_restores_no_other_value(server):
    """The review's probe, made certain: a value's fake drawn for a new value once the value is forgotten (a generator
    that gives it first) is refused, the new value gets the next one, and the text masked before the forget is unmasked
    as it is, never to the new value."""
    from latet.config import Config
    from latet.engine.build import build_masker
    from latet.engine.pipeline import Engine
    from latet.engine.stored import StoredVault

    key, _ = server.identities.add_key("Support")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    vault = StoredVault(ROOT_KEY, store)
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    engine = Engine(vault, masker)
    first = vault.intern("PERSON", EntityClass.NER, "Tommaso Galli", rule="ner")
    old_text = f"call {first.surrogate} now"
    found = store.forget_values(["Tommaso Galli"])
    vault.forgotten(found.ids, found.generation)
    drawn = iter([first.surrogate, "Giulio Neri", "Marta Sala"])
    other = vault.intern("PERSON", EntityClass.NER, "Ida Russo", rule="ner", generate=lambda original, rng: next(drawn))
    assert other.surrogate == "Giulio Neri"
    with vault.holding(scoped=True):
        vault.prefetch_fakes([old_text])
        assert engine.unmask(old_text) == (old_text, 0)
