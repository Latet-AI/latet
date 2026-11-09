"""Task 8: the live bounded lookup, freshness and background index contract.

These tests deliberately use PostgreSQL and StoredVault's actual bounded_words seam. An
index hit is only a candidate; the original matcher and cumulative admission still decide.
"""

import asyncio
import contextlib
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest

from latet.bridge.apps import Apps
from latet.engine import lookup, stored
from latet.engine.anchors import words_in
from latet.engine.lookup import LookupBudget, LookupLimitExceeded
from latet.engine.masker import Masker, MaskReport
from latet.engine.pipeline import Engine, Session
from latet.engine.stored import StoredVault, word_of
from latet.engine.types import EntityClass
from latet.engine.vault import Vault, fake_reading
from latet.engine.verifier import Verifier
from latet.gateway.app import OnServer
from latet.server import index as indexes
from latet.server import spaces
from latet.server import vault as pg
from latet.server.db import Database
from latet.server.index import PrintCache, PrintIndex
from latet.server.master import root_key

from ..conftest import ROOT_KEY
from ..unit.test_call_joined import _mediator_over
from ..unit.test_interior_lookup import DATA, MINIMAL, TOKEN, masker, positions, round_trips
from .support import captured_logs, until
from .test_key_vault import APPS_API, _bearer, _calling, _masked, _working
from .test_print_index import LAURA, MARCO

pytestmark = pytest.mark.server


def _apps(vault):
    return Apps(_mediator_over(vault), OnServer("https://latet.invalid", "k_task8", kind="key"))


@pytest.fixture
def stores(server):
    identity, _ = server.identities.add_key("Indexed lookup")
    mine = pg.PgVaultStore(server.db, identity.id, ROOT_KEY, index=True, cache=8)
    remote = pg.PgVaultStore(server.db, identity.id, ROOT_KEY)
    try:
        yield mine, remote
    finally:
        for store in (mine, remote):
            for transaction in list(store._together.values()):
                transaction.rollback()
            store.close()


def _ready(store):
    assert isinstance(store.index, PrintIndex)
    assert store.index.build()
    assert store.index.ready
    store.index.check_readings()  # as build_index reads them, once the index is ready
    store.current()  # establish this store's observed watermark, independent of the worker's


def _vault(store, working=0):
    return StoredVault(ROOT_KEY, store, working=working, held_at_most=0)


def _token(store, value=TOKEN):
    entity = Vault(ROOT_KEY).intern("API_KEY", EntityClass.SECRET, value)
    assert store.save_entity(entity) is None
    return entity


def _words(text):
    return words_in(text, dotted=True, interior=LookupBudget().interior)


def _vault_rows(store):
    """The prints of the identity's word rows, one a row."""
    with store.db.connection() as conn:
        query = "SELECT print FROM vault_words WHERE identity_id = %s AND print IS NOT NULL"
        return [found for (found,) in conn.execute(query, (store.identity,))]


def _ask(store, text, role="original", budget=None):
    return store.bounded_words(role, _words(text), budget if budget is not None else LookupBudget())


def _queries(monkeypatch):
    """Observe both ordinary SQL and the bounded server cursor, without retaining parameters."""
    sent = []
    for cls in (psycopg.Cursor, psycopg.ServerCursor):
        execute = cls.execute

        def record(cursor, query, params=None, *, _execute=execute, **kwargs):
            sent.append(str(query))
            return _execute(cursor, query, params, **kwargs)

        monkeypatch.setattr(cls, "execute", record)
    return sent


async def _wait_ready(store):
    # Publication happens in a native worker. Observe the actual swapped index without blocking the event loop;
    # the timer is owned here and canceled on success, timeout or cancellation.
    loop, ready = asyncio.get_running_loop(), asyncio.Event()
    timer = None

    def check():
        nonlocal timer
        if store.index.ready:
            ready.set()
        else:
            timer = loop.call_later(0.01, check)

    check()
    try:
        await asyncio.wait_for(ready.wait(), 10)
    finally:
        if timer is not None:
            timer.cancel()


def test_bounded_words_uses_the_ready_index_without_the_legacy_word_query(stores, monkeypatch):
    mine, remote = stores
    entity = _token(remote)
    _ready(mine)
    # Both old lookups are poisoned: passing by optimizing with_words alone is impossible.
    monkeypatch.setattr(pg, "_BOUNDED_WORDS", "SELECT 1 / 0")
    monkeypatch.setattr(pg, "_WITH_WORDS", "SELECT 1 / 0")
    assert _ask(mine, f"x{TOKEN}y") == [entity]
    vault = _vault(mine)
    with vault.holding(scoped=True):
        vault.prefetch([MINIMAL])
        done = masker(vault).mask(MINIMAL, DATA)
    assert positions(done) == set(range(44))
    round_trips(vault, MINIMAL, done.text)


def test_an_absent_print_reads_only_the_change_counter(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    sent = _queries(monkeypatch)
    mine.current()
    assert _ask(mine, "unrelatedordinaryword") == []
    assert len(sent) == 1 and "change_counters" in sent[0]


def test_customers_seen_before_are_fetched_once(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, LAURA.original) == [LAURA]
    sent = _queries(monkeypatch)
    assert _ask(mine, LAURA.original) == [LAURA]
    assert not sent


def test_a_string_no_print_names_is_no_fake_without_asking_the_store(stores, monkeypatch):
    mine, _ = stores
    _ready(mine)
    vault = _vault(mine)
    with vault.holding():

        def forbidden(*args):
            pytest.fail("an absent fake print reached an exact SQL lookup")

        monkeypatch.setattr(mine, "by_surrogate", forbidden)
        monkeypatch.setattr(mine, "by_reading", forbidden)
        assert vault.by_surrogate("ordinaryunheldtext") is None


@pytest.mark.parametrize("working", [0, 8])
@pytest.mark.parametrize("consumed", [False, True], ids=["message-lost", "worker-caught-up"])
def test_each_vault_observes_a_change_even_when_the_worker_already_caught_up(stores, working, consumed):
    mine, remote = stores
    _ready(mine)
    vaults = [_vault(mine, working), _vault(mine, working)]
    engines = [Engine(vault, Masker(vault, [])) for vault in vaults]
    for engine in engines:
        assert engine.mask_text(LAURA.original, DATA, Session("before")).text == LAURA.original
    # Deliberately older than the one-day heuristic: only counter freshness can invalidate this cache.
    old = replace(LAURA, created=1.0)
    remote.save_entity(old)
    if consumed:
        mine.index.catch_up()
    for engine in engines:
        assert engine.mask_text(LAURA.original, DATA, Session("after")).text == LAURA.surrogate


@pytest.mark.parametrize("working", [0, 8])
def test_a_negative_fake_lookup_is_revoked_after_a_remote_insert(stores, working):
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine, working)
    with vault.holding():
        assert vault.by_surrogate(LAURA.surrogate) is None
    remote.save_entity(LAURA)
    mine.index.catch_up()  # the message worker has already consumed the counter
    with vault.holding():
        assert vault.by_surrogate(LAURA.surrogate) == LAURA


@pytest.mark.parametrize("remote_write", [False, True], ids=["local", "remote"])
@pytest.mark.parametrize("joined", [False, True], ids=["alone", "joined"])
def test_a_value_taught_after_its_word_was_queried_is_revalidated_before_settle(stores, remote_write, joined):
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine)
    mediator = _mediator_over(vault)
    session, report = Session("apps", kept=False), MaskReport()
    text = "customer Laura Bianchi completed renewal"
    with vault.holding(scoped=True):
        vault.prefetch([text])
        call = mediator.call()
        reader = mediator._masker(session)
        with reader.joined([text], DATA, report) if joined else contextlib.nullcontext():
            done = mediator.mask_value(text, DATA, session, report, keep_surrogates=False, call=call, as_text=True)
        assert LAURA.original in done.text
        if remote_write:
            remote.save_entity(LAURA)
        else:
            # A distinct gateway thread commits locally while the applications holding remains open.
            errors = []

            def teach():
                try:
                    mine.save_entity(LAURA)
                    vault.learn(LAURA)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=teach)
            thread.start()
            thread.join(5)
            assert not thread.is_alive() and not errors
        mediator.settle(call)
        assert LAURA.original not in done.text and LAURA.surrogate in done.text


def test_a_forgotten_value_is_not_restored_though_its_print_stays(stores):
    mine, remote = stores
    entity = _token(remote)
    _ready(mine)
    vault = _vault(mine)
    with vault.holding():
        assert _ask(mine, entity.surrogate, "surrogate") == [entity]
        assert vault.by_surrogate(entity.surrogate) == entity
    before = len(mine.index)
    remote.forget_entity(entity.id)
    vault.resync()  # lost forgotten notification; SQL remains the authority
    with vault.holding():
        assert _ask(mine, entity.surrogate, "surrogate") == []
        assert vault.by_surrogate(entity.surrogate) is None
    assert len(mine.index) == before
    assert remote.by_original(entity.original) is None


def test_restamping_a_cached_print_reloads_every_candidate(stores):
    mine, remote = stores
    initial = replace(LAURA, rule=None)  # save_rule fills an absent rule; it does not overwrite one
    remote.save_entity(initial)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [initial]
    remote.save_entity(MARCO)
    assert mine.current()
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}
    updated = replace(initial, rule="changed-rule")
    remote.save_rule(updated)
    assert mine.current()
    assert {e.id: e for e in _ask(mine, "Bianchi")}[LAURA.id] == updated


def test_a_fetch_older_than_a_drop_cannot_mark_a_print_complete(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    put, intercepted = PrintCache.put, []

    def changed_before_put(cache, fetched, since):
        if not intercepted and any(LAURA in entries for entries in fetched.values()):
            intercepted.append(True)
            remote.save_entity(MARCO)
            mine.current()
        return put(cache, fetched, since)

    monkeypatch.setattr(PrintCache, "put", changed_before_put)
    _ask(mine, "Bianchi")
    assert intercepted
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}


def test_a_lost_forget_revokes_a_fetch_in_flight_even_when_catch_up_names_no_rows(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    vault = _vault(mine)
    put, intercepted = PrintCache.put, []

    def forgotten_before_put(cache, fetched, since):
        if not intercepted and any(LAURA in entries for entries in fetched.values()):
            intercepted.append(True)
            remote.forget_entity(LAURA.id)
            vault.resync()
            mine.current()
        return put(cache, fetched, since)

    monkeypatch.setattr(PrintCache, "put", forgotten_before_put)
    _ask(mine, "Bianchi")
    assert intercepted
    assert _ask(mine, "Bianchi") == []


def test_indexed_candidates_preserve_identity_and_original_fake_roles(stores, server):
    mine, remote = stores
    entity = _token(remote)
    identity, _ = server.identities.add_key("Other indexed identity")
    other = pg.PgVaultStore(server.db, identity.id, ROOT_KEY, index=True)
    try:
        _ready(mine)
        _ready(other)
        assert _ask(mine, f"x{TOKEN}y") == [entity]
        assert _ask(mine, f"x{TOKEN}y", "surrogate") == []
        assert _ask(other, f"x{TOKEN}y", ("original", "surrogate")) == []
        assert _ask(mine, f"x{entity.surrogate}y", "surrogate") == [entity]
    finally:
        other.close()


def test_a_collision_of_prints_masks_nothing_more_than_its_text_holds(stores):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(MARCO)
    # Force the requested fake-role print onto an original-role row too. The role filter still applies.
    with mine.db.connection() as conn:
        conn.execute(
            "UPDATE vault_words SET print = %s WHERE identity_id = %s AND entity_id = %s AND role = 'original'",
            (mine._print("surrogate", "rossi"), mine.identity, MARCO.id),
        )
    _ready(mine)
    assert _ask(mine, "Rossi", "surrogate") == [LAURA]
    vault = _vault(mine)
    engine = Engine(vault, Masker(vault, []))
    assert engine.mask_text("Laura Bianchi", DATA, Session("collision")).text == LAURA.surrogate
    assert engine.mask_text("ordinary Bianchi", DATA, Session("not-a-name")).text == "ordinary Bianchi"


@pytest.mark.parametrize("cached", [False, True], ids=["sql", "cache"])
def test_candidates_and_characters_share_one_budget_across_lookups(stores, monkeypatch, cached):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(MARCO)
    _ready(mine)
    if cached:
        _ask(mine, "Bianchi")
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 3)
    budget = LookupBudget()
    assert len(_ask(mine, "Bianchi", budget=budget)) == 2
    with pytest.raises(LookupLimitExceeded, match="safe work limit"):
        _ask(mine, "Bianchi", budget=budget)
    assert budget.failed
    with pytest.raises(LookupLimitExceeded):
        _ask(mine, "nothingmatches", budget=budget)


def test_cached_candidates_charge_the_plaintext_character_limit(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    monkeypatch.setattr(lookup, "LOOKUP_CHARACTERS", 1)
    with pytest.raises(LookupLimitExceeded):
        _ask(mine, "Bianchi")


def test_unreadable_rows_are_charged_and_never_make_a_partial_cache_complete(stores, monkeypatch, caplog):
    mine, remote = stores
    for entity in (LAURA, MARCO):
        remote.save_entity(entity)
    with mine.db.connection() as conn:
        conn.execute("UPDATE vault_entities SET original = surrogate WHERE identity_id = %s", (mine.identity,))
    _ready(mine)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    for _ in range(2):
        with pytest.raises(LookupLimitExceeded):
            _ask(mine, "Bianchi")
    assert mine.unreadable == 1
    assert "does not open" in caplog.text
    assert LAURA.original not in caplog.text and LAURA.surrogate not in caplog.text


@pytest.mark.parametrize("field", ["original", "rule"])
def test_indexed_payload_limit_is_checked_before_wire_fields_and_decryption(stores, monkeypatch, field):
    mine, remote = stores
    entity = _token(remote, "AAAAAA" + "x" * 4096 + "ZZZZZZ")
    if field == "rule":
        remote.save_rule(replace(entity, rule="r" * 8192))
    _ready(mine)
    monkeypatch.setattr(lookup, "LOOKUP_BYTES", 128)
    opened = []
    real_open = mine._opened

    def opening(row):
        opened.append(row)
        return real_open(row)

    rows_seen = []
    for cls in (psycopg.Cursor, psycopg.ServerCursor):  # the client's cursor reads the bounded answer whole
        real_fetch = cls.fetchmany

        def fetched(cursor, *args, _real=real_fetch, **kwargs):
            rows = _real(cursor, *args, **kwargs)
            rows_seen.extend(rows)
            return rows

        monkeypatch.setattr(cls, "fetchmany", fetched)
    monkeypatch.setattr(mine, "_opened", opening)
    with pytest.raises(LookupLimitExceeded) as refused:
        _ask(mine, "aaaaaa")
    assert rows_seen, "the actual bounded SQL response must be inspected"
    assert not opened
    assert all(not isinstance(value, (str, bytes, memoryview)) for row in rows_seen for value in row)
    assert entity.original not in str(refused.value) and entity.surrogate not in str(refused.value)


def test_transaction_reads_its_own_new_words_without_publishing_them(stores):
    mine, remote = stores
    _ready(mine)
    before = mine.index.watermark
    transaction = mine.together()
    mine.save_entity(LAURA)
    assert _ask(mine, "Bianchi") == [LAURA]
    assert _ask(remote, "Bianchi") == []
    assert mine.index.watermark == before
    transaction.rollback()
    assert _ask(mine, "Bianchi") == []
    assert remote.by_original(LAURA.original) is None


def test_a_nested_lookup_past_its_bound_lets_the_outer_call_keep_its_values(stores, monkeypatch):
    """Task 8's fix round 6: a nested lookup past the call's bound reads as 0.10.2 did; the outer call keeps its new
    value (no refusal rolls it back)."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    vault = _vault(mine)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    with vault.holding(scoped=True):
        created = vault.intern("API_KEY", EntityClass.SECRET, "UncommittedNewSecret987654321")
        with vault.holding(scoped=True):
            vault.prefetch([LAURA.original])
            assert vault._call().lookup.past
            assert vault.in_memory(LAURA.id) == LAURA
    assert remote.by_original(created.original) == created


def test_an_unready_index_uses_the_authoritative_bounded_sql_path(stores, monkeypatch):
    mine, remote = stores
    entity = _token(remote)
    assert not mine.index.ready
    sent = _queries(monkeypatch)
    assert _ask(mine, f"x{TOKEN}y") == [entity]
    assert any("vault_words" in query for query in sent)


def test_a_catch_up_failure_uses_authoritative_sql_instead_of_an_older_negative(stores, monkeypatch):
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine)
    engine = Engine(vault, Masker(vault, []))
    assert engine.mask_text(LAURA.original, DATA, Session("before")).text == LAURA.original
    remote.save_entity(LAURA)

    def failed(*args, **kwargs):
        raise psycopg.OperationalError("synthetic index read unavailable")

    monkeypatch.setattr(mine.index, "catch_up", failed)
    assert engine.mask_text(LAURA.original, DATA, Session("after")).text == LAURA.surrogate


@pytest.mark.parametrize("damage", ["unreadable", "unmatched-word"])
def test_print_backfill_nulls_unusable_words_and_completes_without_deleting_values(server, damage):
    identity, _ = server.identities.add_key("Backfill completion")
    store = pg.PgVaultStore(server.db, identity.id, ROOT_KEY)
    store.save_entity(LAURA)
    with store.db.connection() as conn:
        conn.execute("UPDATE vault_words SET print = NULL WHERE identity_id = %s", (identity.id,))
        if damage == "unreadable":
            conn.execute("UPDATE vault_entities SET original = surrogate WHERE identity_id = %s", (identity.id,))
        else:
            conn.execute("UPDATE vault_words SET word = sha256(word) WHERE identity_id = %s", (identity.id,))
    assert not store.prints_complete()
    store.fill_prints()
    assert store.prints_complete()
    assert store.count_entities() == 1
    with store.db.connection() as conn:
        rows = conn.execute("SELECT word, print FROM vault_words WHERE identity_id = %s", (identity.id,)).fetchall()
    assert rows and all(word is None and printed is None for word, printed in rows)
    assert store.fill_prints() == 0


async def test_a_keys_space_serves_through_the_store_until_its_index_is_ready(server, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = PrintIndex.build

    def held(index):
        entered.set()
        assert release.wait(10), "test did not release the background build"
        return original(index)

    monkeypatch.setattr(PrintIndex, "build", held)
    _working(server, values=0)
    identity, secret = server.identities.add_key("Background index")
    try:
        async with _calling(server) as client:
            runtime = await server.spaces.runtime(identity)
            assert await asyncio.to_thread(entered.wait, 5), "space never scheduled its index"
            assert not runtime.store.index.ready
            [done] = await _masked(client, secret, ["password=" + TOKEN])
            assert TOKEN not in done
            release.set()
            await _wait_ready(runtime.store)
            assert await _masked(client, secret, ["password=" + TOKEN]) == [done]
    finally:
        release.set()


async def test_the_index_is_weighed_with_its_space(server):
    identity, _ = server.identities.add_key("Index weight")
    async with _calling(server):
        runtime = await server.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        before, held = spaces.space_size(runtime), runtime.store.index.nbytes
        runtime.store.index.add(range(10_000))
        extra = runtime.store.index.nbytes - held
        assert extra > 0 and spaces.space_size(runtime) >= before + extra


@pytest.mark.parametrize("failure", ["incomplete", "allocation", "sql"])
async def test_failed_or_incomplete_background_build_keeps_sql_available(server, monkeypatch, failure):
    entered = threading.Event()

    def failed(index):
        entered.set()
        if failure == "incomplete":
            return False
        if failure == "allocation":
            raise MemoryError("synthetic aggregate limit")
        raise psycopg.OperationalError("synthetic build unavailable")

    monkeypatch.setattr(PrintIndex, "build", failed)
    identity, secret = server.identities.add_key("Failed index build")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(identity)
        assert await asyncio.to_thread(entered.wait, 5)
        assert not runtime.store.index.ready
        [done] = await _masked(client, secret, ["password=" + TOKEN])
        assert TOKEN not in done


async def test_a_build_held_during_close_is_not_published_after_close(server, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = PrintIndex.build

    def held(index):
        entered.set()
        try:
            assert release.wait(10)
            return original(index)
        finally:
            finished.set()

    monkeypatch.setattr(PrintIndex, "build", held)
    identity, _ = server.identities.add_key("Closed index build")
    try:
        async with _calling(server):
            runtime = await server.spaces.runtime(identity)
            assert await asyncio.to_thread(entered.wait, 5)
            runtime.store.close()
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            assert not runtime.store.index.ready
    finally:
        release.set()


async def test_a_value_kept_elsewhere_is_masked_at_the_next_call_without_its_message(instances, monkeypatch):
    first, second = instances
    _working(first, second, values=0)
    identity, secret = first.identities.add_key("Lost index notification")
    async with _calling(first, second) as (_, client):
        runtime = await second.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.original]
        monkeypatch.setattr(second.spaces, "_enqueue", lambda *args: None)
        key = root_key(first.db, first.master, identity.id)
        writer = pg.PgVaultStore(first.db, identity.id, key)
        writer.save_entity(LAURA)
        assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.surrogate]


async def test_a_vault_from_0_10_0_serves_while_its_prints_are_made(server, monkeypatch):
    _working(server, values=0)
    identity, secret = server.identities.add_key("Legacy index backfill")
    key = root_key(server.db, server.master, identity.id)
    writer = pg.PgVaultStore(server.db, identity.id, key)
    transaction = writer.together()
    try:
        for number in range(3000):
            writer.save_entity(
                replace(
                    LAURA,
                    id=f"e{number:019d}",
                    original=f"Customer Number{number:05d}",
                    surrogate=f"Alternate Client{number:05d}",
                )
            )
        transaction.commit()
    finally:
        transaction.rollback()
    with server.db.connection() as conn:
        conn.execute("UPDATE vault_words SET print = NULL, counted = NULL WHERE identity_id = %s", (identity.id,))
    entered, release = threading.Event(), threading.Event()
    fill = pg.PgVaultStore.fill_prints

    def held(store):
        entered.set()
        assert release.wait(15)
        return fill(store)

    monkeypatch.setattr(pg.PgVaultStore, "fill_prints", held)
    try:
        async with _calling(server) as client:
            runtime = await server.spaces.runtime(identity)
            assert await asyncio.to_thread(entered.wait, 5)
            assert not runtime.store.index.ready
            assert await _masked(client, secret, ["Customer Number00000"], "data") == ["Alternate Client00000"]
            release.set()
            await _wait_ready(runtime.store)
            assert runtime.store.prints_complete()
            assert runtime.store.count_entities() == 3000
            assert await _masked(client, secret, ["Customer Number02999"], "data") == ["Alternate Client02999"]
    finally:
        release.set()


@pytest.mark.parametrize("lease", ["occupied", "failed"])
async def test_an_occupied_or_failed_backfill_lease_does_not_publish_an_incomplete_index(server, monkeypatch, lease):
    identity, secret = server.identities.add_key("Unavailable backfill lease")
    key = root_key(server.db, server.master, identity.id)
    writer = pg.PgVaultStore(server.db, identity.id, key)
    writer.save_entity(LAURA)
    with server.db.connection() as conn:
        conn.execute("UPDATE vault_words SET print = NULL WHERE identity_id = %s", (identity.id,))
    hold, entered = server.bus.hold, threading.Event()

    @contextlib.contextmanager
    def unavailable(name, seconds):
        if name == f"prints:{identity.id}":
            entered.set()
            if lease == "failed":
                raise ConnectionError("synthetic lease failure")
            yield False
        else:
            with hold(name, seconds) as acquired:
                yield acquired

    monkeypatch.setattr(server.bus, "hold", unavailable)
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(identity)
        assert await asyncio.to_thread(entered.wait, 5)
        assert not runtime.store.index.ready and not runtime.store.prints_complete()
        assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.surrogate]


def _legacy_key(server, label, count=30):
    """An API key whose vault 0.10 kept: ``count`` values, their word rows without a print or a count."""
    identity, secret = server.identities.add_key(label)
    writer = pg.PgVaultStore(server.db, identity.id, root_key(server.db, server.master, identity.id))
    for number in range(count):
        writer.save_entity(
            replace(
                LAURA,
                id=f"e{number:019d}",
                original=f"Customer Number{number:05d}",
                surrogate=f"Alternate Client{number:05d}",
            )
        )
    with server.db.connection() as conn:
        conn.execute("UPDATE vault_words SET print = NULL, counted = NULL WHERE identity_id = %s", (identity.id,))
    return identity, secret


async def test_an_instance_that_found_the_prints_being_made_elsewhere_uses_its_index_once_they_are(
    instances, monkeypatch
):
    """The final review's I-1, its first scenario (the documented upgrade): two instances build a legacy key's space
    while the first makes its prints (``prints:<identity>``, held). The second finds their lease taken, and serves
    through PostgreSQL meanwhile; once the first's fill ends, the second builds its index again and uses it, with no
    beat's check in between (the fill's end is told: its ``prints`` message)."""
    first, second = instances
    _working(first, second, values=0)
    identity, secret = _legacy_key(first, "Legacy key on two instances")
    entered, release = threading.Event(), threading.Event()
    fill = pg.PgVaultStore._fill_prints

    def held(store):  # under the lease: the first instance's alone
        entered.set()
        assert release.wait(15), "the test did not release the fill"
        return fill(store)

    monkeypatch.setattr(pg.PgVaultStore, "_fill_prints", held)
    try:
        async with _calling(first, second) as (_, client):
            mine = await first.spaces.runtime(identity)
            assert await asyncio.to_thread(entered.wait, 5), "the first instance never made the prints"
            theirs = await second.spaces.runtime(identity)
            await until(lambda: not second.spaces._index_tasks, seconds=10)  # its build over: the lease was taken
            assert not theirs.store.index.ready and not theirs.store.prints_complete()
            assert await _masked(client, secret, ["Customer Number00000"], "data") == ["Alternate Client00000"]
            release.set()
            await _wait_ready(mine.store)
            await _wait_ready(theirs.store)  # within ten seconds, by the fill's end alone
            assert await _masked(client, secret, ["Customer Number00029"], "data") == ["Alternate Client00029"]
    finally:
        release.set()


def _legacy_person(server, name, count=30):
    """A person whose vault 0.10.0 kept: ``count`` values, and no word row yet (their words and prints are made in the
    background, ``spaces._make_words``, the space serving the vault whole from memory meanwhile)."""
    person, _ = server.identities.add_person(name)
    writer = pg.PgVaultStore(server.db, person.id, root_key(server.db, server.master, person.id))
    for number in range(count):
        writer.save_entity(
            replace(
                LAURA,
                id=f"e{number:019d}",
                original=f"Customer Number{number:05d}",
                surrogate=f"Alternate Client{number:05d}",
            )
        )
    with server.db.connection() as conn:
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (person.id,))  # as 0.10.0 left them
    return person


async def _a_key_is_indexed_meanwhile(server, client):
    """A key whose prints are all made opens its space now: its index is built and used, whatever the background work
    of the vaults kept by 0.10 is doing."""
    identity, secret = server.identities.add_key("Prints all made")
    runtime = await server.spaces.runtime(identity)
    await _wait_ready(runtime.store)
    [done] = await _masked(client, secret, ["password=" + TOKEN])
    assert TOKEN not in done


async def test_a_legacy_vaults_fill_holds_no_build_turn_of_another_space(server, monkeypatch):
    """Item 6 of the minors after 0.11 (Task 13's final review, M-1): on a server upgraded from 0.10, two keys whose
    vaults 0.10 kept open their spaces first and have their prints made, each fill held on an event of its own; a third
    key, its prints all made, opens its space meanwhile: its index is built and used while both fills still wait, for a
    fill holds none of the instance's two index builds (``INDEX_BUILDS_AT_ONCE``). Each legacy key's index is ready
    once its own fill is released, with no beat's check: the fill that made the prints asks for its own space's build,
    since its ``prints`` message reaches the other instances' spaces alone. The third key opens once the second key's
    build has begun (``build_index`` entered), so that it never comes first."""
    _working(server, values=0)
    legacy = [_legacy_key(server, f"Legacy key {number}") for number in range(indexes.INDEX_BUILDS_AT_ONCE)]
    building = {identity.id: threading.Event() for identity, _ in legacy}
    entered = {identity.id: threading.Event() for identity, _ in legacy}
    release = {identity.id: threading.Event() for identity, _ in legacy}
    build, fill = pg.PgVaultStore.build_index, pg.PgVaultStore.fill_prints

    def begun(store):
        if store.identity in building:
            building[store.identity].set()
        return build(store)

    def held(store):
        if store.identity in release:
            entered[store.identity].set()
            assert release[store.identity].wait(30), "the test did not release the fill"
        return fill(store)

    monkeypatch.setattr(pg.PgVaultStore, "build_index", begun)
    monkeypatch.setattr(pg.PgVaultStore, "fill_prints", held)
    try:
        async with _calling(server) as client:
            first = await server.spaces.runtime(legacy[0][0])
            assert await asyncio.to_thread(entered[legacy[0][0].id].wait, 5), "the first key's prints were never made"
            second = await server.spaces.runtime(legacy[1][0])
            assert await asyncio.to_thread(building[legacy[1][0].id].wait, 5), "the second key's build never began"
            await _a_key_is_indexed_meanwhile(server, client)
            for runtime in (first, second):
                assert not runtime.store.index.ready and not runtime.store.prints_complete()
            for (identity, secret), runtime in zip(legacy, (first, second), strict=True):
                release[identity.id].set()
                await _wait_ready(runtime.store)
                assert runtime.store.prints_complete()
                assert await _masked(client, secret, ["Customer Number00029"], "data") == ["Alternate Client00029"]
    finally:
        for event in release.values():
            event.set()


async def test_a_persons_words_job_holds_no_build_turn_of_another_space(server, monkeypatch):
    """Item 6's twin: two people whose vaults 0.10.0 kept, with no words yet, open their spaces first and have their
    words and prints made (``spaces._make_words``), each job held on an event of its own; a key whose prints are all
    made opens its space meanwhile: its index is built and used while both jobs still wait. Each person's words and
    prints are made once their job is released, the space serving the vault whole from memory until then. The key
    opens once the second person's job has been handed to its thread (``Spaces._words_space`` entered: it hands it on
    before it first waits), so that it never comes first."""
    _working(server, values=0)
    people = [_legacy_person(server, f"Person of 0.10 {number}") for number in range(indexes.INDEX_BUILDS_AT_ONCE)]
    asked = {person.id: asyncio.Event() for person in people}
    entered = {person.id: threading.Event() for person in people}
    release = {person.id: threading.Event() for person in people}
    done = {person.id: threading.Event() for person in people}
    words_space, make = spaces.Spaces._words_space, spaces._make_words

    async def begun(self, space):
        if space.identity.id in asked:
            asked[space.identity.id].set()
        return await words_space(self, space)

    def held(store, jobs):
        entered[store.identity].set()
        try:
            assert release[store.identity].wait(30), "the test did not release the words job"
            return make(store, jobs)
        finally:
            done[store.identity].set()

    monkeypatch.setattr(spaces.Spaces, "_words_space", begun)
    monkeypatch.setattr(spaces, "_make_words", held)
    try:
        async with _calling(server) as client:
            first = await server.spaces.runtime(people[0])
            assert await asyncio.to_thread(entered[people[0].id].wait, 5), "the first person's words were never made"
            second = await server.spaces.runtime(people[1])
            await asyncio.wait_for(asked[people[1].id].wait(), 5)  # its job handed on before the key's build
            await _a_key_is_indexed_meanwhile(server, client)
            for person, runtime in zip(people, (first, second), strict=True):
                assert type(runtime.built.vault) is Vault  # served whole from memory meanwhile
                release[person.id].set()
                assert await asyncio.to_thread(done[person.id].wait, 30), "the words job never ended"
                store = runtime.store
                assert await asyncio.to_thread(lambda store=store: store.words_complete() and store.prints_complete())
    finally:
        for event in release.values():
            event.set()


async def test_a_fill_that_ends_while_its_space_is_built_has_its_index_built_at_once(server, monkeypatch):
    """Task 6's review, M-1: a fill of a space's prints that ends while a build of that space is under way, after the
    build found them not all made (another instance's, heard by its ``prints`` message; or the space's own), has the
    index built again as soon as that build ends, not at the next paced try (10, 20, 30 or 60 seconds after it). Here
    the fills' thread is busy with another vault's fill, so that no fill of the space's own makes up for it. The test
    waits for the third build's answer as it is recorded, never for the index to be ready: a build publishes it before
    it returns its answer."""
    _working(server, values=0)
    busy, _ = _legacy_key(server, "Legacy key whose fill holds the thread")
    target, _ = _legacy_key(server, "Legacy key made elsewhere")
    busy_entered, busy_release = threading.Event(), threading.Event()
    target_held, target_release, built_again = threading.Event(), threading.Event(), threading.Event()
    build, fill = pg.PgVaultStore.build_index, pg.PgVaultStore.fill_prints
    builds: list[bool] = []

    def held_fill(store):
        if store.identity == busy.id:
            busy_entered.set()
            assert busy_release.wait(30), "the test did not release the busy fill"
        return fill(store)

    def held_build(store):
        built = build(store)
        if store.identity == target.id:
            builds.append(built)
            if len(builds) == 2:  # its paced try, which found the prints not all made, held as it ends
                target_held.set()
                assert target_release.wait(30), "the test did not release the build"
            elif len(builds) == 3:  # the build the fill's end asked for, its answer recorded
                built_again.set()
        return built

    monkeypatch.setattr(pg.PgVaultStore, "fill_prints", held_fill)
    monkeypatch.setattr(pg.PgVaultStore, "build_index", held_build)
    try:
        async with _calling(server):
            await server.spaces.runtime(busy)
            assert await asyncio.to_thread(busy_entered.wait, 5), "the busy key's prints were never made"
            runtime = await server.spaces.runtime(target)
            space = server.spaces._spaces[target.id]
            assert space.index_build is not None
            await asyncio.wait({space.index_build})  # its first build: the prints not all made
            assert space.prints_fill is not None and not space.prints_fill.done()  # its fill waits for the thread
            space.index_tried = 0.0  # its paced try due now, as the beat's check starts it
            server.spaces._build_index_again()
            assert await asyncio.to_thread(target_held.wait, 5), "its paced try never began"
            other = pg.PgVaultStore(server.db, target.id, root_key(server.db, server.master, target.id))
            await asyncio.to_thread(other._fill_prints)  # another instance makes the prints meanwhile
            assert await asyncio.to_thread(runtime.store.prints_complete)
            server.spaces._index_soon(space)  # as its ``prints`` message has it
            assert space.index_wanted
            target_release.set()
            # Ten seconds: at once, not at the paced try's twenty.
            assert await asyncio.to_thread(built_again.wait, 10), "the index was not built again at once"
            assert builds == [False, False, True] and runtime.store.index.ready
    finally:
        busy_release.set()
        target_release.set()


async def test_an_index_whose_catch_up_failed_once_is_ready_again_after_the_beats_check(server, monkeypatch):
    """The final review's I-1, its second scenario (PostgreSQL restarted or failed over, a pool's wait past its
    bound): a catch-up of a ready index fails once, and from then on PostgreSQL's lookup answers, never the index. The
    beat's check builds it again: ready within a bound, and used."""
    identity, secret = server.identities.add_key("Index catch-up failed once")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        [done] = await _masked(client, secret, ["password=" + TOKEN])
        assert TOKEN not in done
        lost = runtime.store.index

        def failed(*args, **kwargs):
            raise psycopg.OperationalError("synthetic catch-up failure")

        monkeypatch.setattr(lost, "catch_up", failed)
        await asyncio.to_thread(runtime.store.current)
        assert not runtime.store.index.ready  # not used from now on
        assert await _masked(client, secret, ["password=" + TOKEN]) == [done]  # PostgreSQL's lookup answers
        await server.check()  # the beat's
        await _wait_ready(runtime.store)
        assert runtime.store.index is not lost
        assert await _masked(client, secret, ["password=" + TOKEN]) == [done]


async def test_an_index_not_used_and_used_again_is_said_once_each_way_and_never_a_value(server, monkeypatch):
    """The final review's I-1: a space whose index stops being used says so once, by the identity's id and the error's
    type, and once when it is used again; a build tried again in between that fails too says nothing more; the beat's
    check builds it again only once due (``INDEX_AGAIN_SECONDS``). No value, word or print is in the log."""
    identity, secret = server.identities.add_key("Index said once")
    with captured_logs() as logs:
        async with _calling(server) as client:
            runtime = await server.spaces.runtime(identity)
            await _wait_ready(runtime.store)
            [done] = await _masked(client, secret, ["password=" + TOKEN])

            def failed(*args, **kwargs):
                raise psycopg.OperationalError("synthetic failure " + TOKEN)

            monkeypatch.setattr(runtime.store.index, "catch_up", failed)
            await asyncio.to_thread(runtime.store.current)
            build = PrintIndex.build
            monkeypatch.setattr(PrintIndex, "build", failed)  # its first build again fails too
            await server.check()
            await until(lambda: not server.spaces._index_tasks, seconds=10)
            assert not runtime.store.index.ready
            monkeypatch.setattr(PrintIndex, "build", build)
            await server.check()  # not due yet: nothing built
            assert not server.spaces._index_tasks and not runtime.store.index.ready
            monkeypatch.setattr(spaces, "INDEX_AGAIN_SECONDS", (0.0,), raising=False)  # due now
            await server.check()
            await _wait_ready(runtime.store)
            assert await _masked(client, secret, ["password=" + TOKEN]) == [done]
    said = logs.getvalue()
    lines = [line for line in said.splitlines() if "vault index" in line]
    off = [line for line in lines if "is not used" in line]
    on = [line for line in lines if "is used again" in line]
    assert len(off) == 1 and len(on) == 1 and len(lines) == 2, lines
    assert identity.id in off[0] and "OperationalError" in off[0] and identity.id in on[0]
    assert TOKEN not in said and "synthetic" not in said


@pytest.mark.parametrize("refused", ["size", "failure"])
async def test_an_index_refused_for_its_size_is_not_built_again_while_its_space_lasts(server, monkeypatch, refused):
    """Root's ruling on Task 13's fix round (its concern 2): an index refused for its size (more print rows than
    ``INDEX_BUILD_BYTES`` allows) is not built again by the beat's check while its space lasts, however often it is
    due: a vault's size only grows, and each try counts its print rows under a build turn. It is said once, and the
    space answers through PostgreSQL; a new space of the identity tries again. A failure of any other kind is tried
    again as before."""
    builds: list[PrintIndex] = []
    build = PrintIndex.build

    def counted(index):
        builds.append(index)
        if refused == "failure" and len(builds) == 1:
            raise psycopg.OperationalError("synthetic build unavailable")
        return build(index)

    monkeypatch.setattr(PrintIndex, "build", counted)
    if refused == "size":
        monkeypatch.setattr(indexes, "INDEX_BUILD_BYTES", 8)  # room for one print row: LAURA's has more
    monkeypatch.setattr(spaces, "INDEX_AGAIN_SECONDS", (0.0,))  # every try due at once
    identity, secret = server.identities.add_key("Index past its size")
    pg.PgVaultStore(server.db, identity.id, root_key(server.db, server.master, identity.id)).save_entity(LAURA)
    with captured_logs() as logs:
        async with _calling(server) as client:
            runtime = await server.spaces.runtime(identity)
            await until(lambda: not server.spaces._index_tasks, seconds=10)
            for _ in range(3):  # three beats' checks, each with the space's index due
                await server.check()
                await until(lambda: not server.spaces._index_tasks, seconds=10)
            if refused == "size":
                assert len(builds) == 1 and not runtime.store.index.ready
            else:
                assert len(builds) == 2 and runtime.store.index.ready
            assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.surrogate]
            if refused == "size":
                await server.spaces.drop(identity.id)
                again = await server.spaces.runtime(identity)
                await until(lambda: not server.spaces._index_tasks, seconds=10)
                assert again is not runtime and len(builds) == 2  # a new space tries once again
    lines = [line for line in logs.getvalue().splitlines() if "vault index" in line and identity.id in line]
    off = [line for line in lines if "is not used" in line]
    if refused == "size":
        assert len(off) == 1 and "past the size" in off[0] and len(lines) == 1, lines
    else:
        assert len(off) == 1 and "OperationalError" in off[0] and len(lines) == 2, lines


async def test_indexes_lost_together_are_all_built_again_without_waiting_a_beat_for_each_two(server):
    """The final re-review's R-3: after a PostgreSQL restart every indexed space lost its index at once, and the beat's
    check started two builds, nothing starting the next as one ended: sixty spaces waited some five minutes. When a
    build ends, the next one due starts at once, still two at a time under their turns: six spaces lost at one beat's
    check are all ready again well before the next beat (``app.BEAT_SECONDS``), with no other check."""
    from latet.server.app import BEAT_SECONDS

    runtimes = []
    async with _calling(server):
        for number in range(6):
            identity, _ = server.identities.add_key(f"Index lost together {number}")
            runtime = await server.spaces.runtime(identity)
            await _wait_ready(runtime.store)
            runtimes.append(runtime)

        def failed(*args, **kwargs):
            raise psycopg.OperationalError("synthetic catch-up failure")

        for runtime in runtimes:
            runtime.store.index.catch_up = failed  # this index alone: a new one catches up as ever
            await asyncio.to_thread(runtime.store.current)
            assert not runtime.store.index.ready
        await server.check()  # one beat's
        await until(lambda: all(runtime.store.index.ready for runtime in runtimes), seconds=BEAT_SECONDS / 2)


async def test_an_index_lost_right_after_each_build_is_built_again_at_the_pace_of_a_failed_one(server, monkeypatch):
    """Item 9 of the minors after 0.11 (Task 13's re-review round 2), as the 0.12 plan rules it: an index whose
    catch-up fails right after each build (a PostgreSQL pool short of connections for a while) is not built again at
    every check. Its first loss is built again at the next check, as I-1's and R-3's are; each later loss within a
    minute (``INDEX_AGAIN_SECONDS[-1]``) of the build that made it ready waits as a failed build's try does, 10, 20,
    then 30 seconds, then a minute and never longer; an index that lived a minute before its loss is built again at
    once, and one lost right after that build waits 10 seconds again. The spaces' clock is the test's, moved by hand: no
    sleep, and each check's builds are counted once they end. It starts at a whole number of seconds, so that each
    whole step it is moved by adds up exactly (a fraction is rounded once a sum crosses a power of two)."""
    clock = [float(int(time.monotonic()))]
    monkeypatch.setattr(spaces, "time", type("Clock", (), {"monotonic": lambda: clock[0], "sleep": time.sleep}))
    identity, _ = server.identities.add_key("Index lost after each build")
    build = pg.PgVaultStore.build_index
    builds: list[bool] = []

    def failed(*args, **kwargs):
        raise psycopg.OperationalError("synthetic catch-up failure")

    def lost_after(store):
        built = build(store)
        if store.identity == identity.id:
            builds.append(built)
            if built:  # this index alone: its next catch-up fails, a fresh one's build catches up
                store.index.catch_up = failed
        return built

    monkeypatch.setattr(pg.PgVaultStore, "build_index", lost_after)
    async with _calling(server):
        runtime = await server.spaces.runtime(identity)
        space = server.spaces._spaces[identity.id]
        assert space.index_build is not None
        await asyncio.wait({space.index_build})  # its first build, judged
        assert builds == [True] and runtime.store.index.ready

        async def lost(lived=0.0):
            """The index lost to its failed catch-up, ``lived`` seconds after the build that made it ready."""
            clock[0] += lived
            await asyncio.to_thread(runtime.store.current)
            assert not runtime.store.index.ready

        async def checked(later=0.0):
            """The beat's check ``later`` seconds on: the builds it made, once they ended."""
            clock[0] += later
            before, count = space.index_build, len(builds)
            await server.check()
            if space.index_build is not before:
                await asyncio.wait({space.index_build})
            return len(builds) - count

        again = spaces.INDEX_AGAIN_SECONDS
        assert again == (10.0, 20.0, 30.0, 60.0)  # as shipped, as the server operations page says
        await lost()
        assert await checked() == 1  # the first loss: built again at the next check
        assert runtime.store.index.ready
        for wait in (*again, again[-1]):  # lost again right after each build: 10, 20, 30 seconds, then a minute twice
            await lost()
            assert await checked() == 0, f"built again at once, not {wait} seconds later"
            assert space.index_off and not runtime.store.index.ready
            assert await checked(wait - 1) == 0 and not runtime.store.index.ready
            assert await checked(1) == 1 and runtime.store.index.ready
        await lost(again[-1])  # it lived a minute: built again at once
        assert await checked() == 1 and runtime.store.index.ready
        await lost()  # and lost right after that build: 10 seconds again
        assert await checked() == 0
        assert await checked(again[0]) == 1 and runtime.store.index.ready
        assert builds == [True] * 9


def test_empty_cached_print_and_evicted_collision_group_are_fetched_completely(stores):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    remote.forget_entity(LAURA.id)
    mine.current()
    assert _ask(mine, "Bianchi") == []  # print remains, so this is an empty cache answer
    remote.save_entity(MARCO)
    mine.current()
    assert _ask(mine, "Bianchi") == [MARCO]
    # More than the eight-entry cache, all with a shared anchor: never keep a partial group complete.
    expected = {MARCO.id}
    for number in range(12):
        entity = replace(
            LAURA,
            id=f"e{100 + number:019d}",
            original=f"P{number} Bianchi",
            surrogate=f"Other{number} Surname{number}",
        )
        # A tied longest first word would anchor there, so it would not exercise this cache group at all.
        assert word_of(entity, "original") == "bianchi"
        assert remote.save_entity(entity) is None
        expected.add(entity.id)
    assert {entity.id for entity in _ask(remote, "Bianchi")} == expected
    mine.current()
    for _ in range(2):
        found = _ask(mine, "Bianchi")
        assert len(found) == 13 and {entity.id for entity in found} == expected
        printed = mine._print("original", "bianchi")
        assert mine._cache.get({printed}) == ([], {printed})  # oversized group stays unknown, never partial


def test_a_new_fake_for_the_same_id_revokes_cached_original_and_fake_candidates(stores):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Rossi", "surrogate") == [LAURA]
    assert _ask(mine, "Bianchi") == [LAURA]
    changed = replace(LAURA, surrogate="Carla Fontana", counter=1)
    assert remote.replace_fake(LAURA, changed) is None
    mine.current()
    assert _ask(mine, "Rossi", "surrogate") == []
    assert _ask(mine, "Fontana", "surrogate") == [changed]
    assert _ask(mine, "Bianchi") == [changed]


@pytest.mark.parametrize("operation", ["mask", "scan", "unmask"])
async def test_ready_index_past_its_bounds_is_served_and_storage_failure_is_503(server, monkeypatch, operation):
    identity, secret = server.identities.add_key("Indexed API limits")
    async with _calling(server) as client:
        await _masked(client, secret, ["password=" + TOKEN])
        runtime = await server.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        entity = runtime.store.by_original(TOKEN)
        assert entity is not None
        text = f"{entity.surrogate if operation == 'unmask' else TOKEN}y"
        with monkeypatch.context() as limited:
            limited.setattr(lookup, "LOOKUP_CHARACTERS", 1)
            answer = await client.post(f"{APPS_API}/{operation}", json={"texts": [text]}, headers=_bearer(secret))
            assert answer.status_code == 200, answer.text  # read past the bound as 0.10.2 read it
            if operation == "mask":
                assert TOKEN not in answer.text
            elif operation == "unmask":
                assert answer.json()["results"][0]["text"] == f"{TOKEN}y"

        def unavailable(*args, **kwargs):
            raise psycopg.OperationalError("synthetic indexed storage failure")

        monkeypatch.setattr(runtime.store, "bounded_words", unavailable)
        answer = await client.post(f"{APPS_API}/{operation}", json={"texts": [text]}, headers=_bearer(secret))
        assert answer.status_code == 503
        assert TOKEN not in answer.text and entity.surrogate not in answer.text


@pytest.mark.parametrize("operation", ["scan", "unmask"])
def test_a_commit_before_the_final_scan_or_restore_check_is_observed(stores, monkeypatch, operation):
    mine, remote = stores
    _ready(mine)
    apps = _apps(_vault(mine))
    method = "find" if operation == "scan" else "unmask"
    read = getattr(apps.engine, method)
    calls = []

    def insert_after_first_read(*args, **kwargs):
        answer = read(*args, **kwargs)
        if not calls:
            remote.save_entity(LAURA)
        calls.append(True)
        return answer

    monkeypatch.setattr(apps.engine, method, insert_after_first_read)
    if operation == "scan":
        response, found = apps._scan([LAURA.original], DATA)
        assert found == 1 and b'"findings":[]' not in response.body
    else:
        results, restored = apps._unmask([LAURA.surrogate])
        assert results == [{"text": LAURA.original, "restored": 1}] and restored == 1
    assert len(calls) == 2


def test_a_mostly_cached_call_joins_only_its_one_unread_text(stores, monkeypatch):
    mine, _ = stores
    _ready(mine)
    apps = _apps(_vault(mine))
    texts = [f"ordinary operational line number {number}" for number in range(500)]
    apps._mask(texts, DATA)
    joined = apps.engine.masker.joined
    seen = []

    @contextlib.contextmanager
    def record(lines, *args, **kwargs):
        seen.append(list(lines))
        with joined(lines, *args, **kwargs):
            yield

    monkeypatch.setattr(apps.engine.masker, "joined", record)
    new = "one new operational line"
    _, out, _, _ = apps._mask([*texts, new], DATA)
    assert len(out) == 501 and seen == [[new]]


def test_a_fresh_rebuild_catches_commits_after_its_snapshot_without_mutating_the_live_index(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    live = mine.index
    assert _ask(mine, LAURA.original) == [LAURA]
    entered, release = threading.Event(), threading.Event()
    build = PrintIndex.build

    def snapshot(index):
        answer = build(index)
        assert index is not live
        entered.set()
        assert release.wait(10)
        return answer

    monkeypatch.setattr(PrintIndex, "build", snapshot)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(mine.build_index)
        try:
            assert entered.wait(5)
            assert mine.index is live and live.ready
            remote.save_entity(MARCO)
            mine.current()
            watermark, epoch = live.watermark, mine._cache.epoch
            assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}
        finally:
            release.set()
        assert future.result(5)
    assert mine.index is not live and mine.index.watermark >= watermark
    assert mine._cache.epoch > epoch
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}


def test_a_rebuild_revoked_during_its_final_catchup_never_publishes(stores, monkeypatch):
    mine, _ = stores
    _ready(mine)
    live, catch_up = mine.index, PrintIndex.catch_up
    entered, release = threading.Event(), threading.Event()

    def finishing(index, *args, **kwargs):
        answer = catch_up(index, *args, **kwargs)
        if index is not live:
            entered.set()
            assert release.wait(10)
        return answer

    monkeypatch.setattr(PrintIndex, "catch_up", finishing)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(mine.build_index)
        try:
            assert entered.wait(5)
            mine.revoke_index()  # the space's immediate close barrier, before its asynchronous teardown
        finally:
            release.set()
        assert not future.result(5)
    assert mine.index is live
    with pytest.raises(psycopg.OperationalError, match="closed"):
        _ask(mine, "anything")


def test_an_oversized_aggregate_keeps_authoritative_sql_available(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    monkeypatch.setattr(indexes, "INDEX_BUILD_BYTES", 8)
    assert not mine.build_index() and not mine.index.ready
    assert _ask(mine, LAURA.original) == [LAURA]


def test_minimum_pool_transaction_can_finish_while_another_thread_waits_to_catch_up(server, monkeypatch):
    identity, _ = server.identities.add_key("Minimum pool index")
    db = Database(os.environ["LATET_TEST_DATABASE_URL"], schema=server.db.schema, min_size=1, max_size=1)
    store = pg.PgVaultStore(db, identity.id, ROOT_KEY, index=True)
    entered = threading.Event()
    connection = db.connection

    @contextlib.contextmanager
    def waiting(*args, **kwargs):
        if threading.current_thread().name.startswith("index-waiter"):
            entered.set()
        with connection(*args, **kwargs) as conn:
            yield conn

    try:
        _ready(store)
        monkeypatch.setattr(db, "connection", waiting)
        transaction = store.together()
        store.save_entity(LAURA)  # owns the only connection until rollback
        with ThreadPoolExecutor(1, thread_name_prefix="index-waiter") as executor:
            future = executor.submit(store.current)
            try:
                assert entered.wait(5)
                store.current()  # reads its own connection; must not wait on the other thread's store lock
                assert _ask(store, LAURA.original) == [LAURA]
            finally:
                transaction.rollback()
            future.result(5)
        assert _ask(store, LAURA.original) == [] and not len(store._cache)
    finally:
        store.close()
        db.close()


def test_build_concurrency_is_process_wide_and_closed_queued_stores_do_not_publish(server, monkeypatch):
    limit = indexes.INDEX_BUILDS_AT_ONCE
    stores = []
    for number in range(limit + 2):
        identity, _ = server.identities.add_key(f"Bounded build {number}")
        stores.append(pg.PgVaultStore(server.db, identity.id, ROOT_KEY, index=True))
    entered, release = threading.Event(), threading.Event()
    lock, active, peak = threading.Lock(), [], []
    original = PrintIndex.build

    def held(index):
        with lock:
            active.append(index)
            peak.append(len(active))
            if len(active) == limit:
                entered.set()
        try:
            assert release.wait(10)
            return original(index)
        finally:
            with lock:
                active.remove(index)

    monkeypatch.setattr(PrintIndex, "build", held)
    with ThreadPoolExecutor(len(stores)) as executor:
        futures = [executor.submit(store.build_index) for store in stores]
        try:
            assert entered.wait(5)
            for store in stores:
                store.revoke_index()
        finally:
            release.set()
        assert all(not future.result(5) for future in futures)
    assert max(peak) == limit and all(not store.index.ready for store in stores)
    for store in stores:
        store.close()


def test_build_peak_and_cached_plaintext_are_both_in_the_store_weight(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    _ask(mine, LAURA.original)
    baseline = mine.index_bytes
    assert baseline >= mine.index.nbytes + len(LAURA.original) * 4
    observed = []
    distinct = indexes._distinct

    def weigh(*args, **kwargs):
        observed.append(mine.index_bytes)
        return distinct(*args, **kwargs)

    monkeypatch.setattr(indexes, "_distinct", weigh)
    assert mine.build_index()
    assert max(observed) > baseline + mine.index.nbytes
    assert mine._building_index is None


def test_lease_is_sized_from_actual_population_and_expiry_only_duplicates_idempotent_work(stores, server, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    with mine.db.connection() as conn:
        conn.execute("UPDATE vault_words SET print = NULL WHERE identity_id = %s", (mine.identity,))
    mine._jobs = remote._jobs = server.bus
    hold, seen = server.bus.hold, []
    entered, release = threading.Event(), threading.Event()
    fill = mine._fill_prints

    @contextlib.contextmanager
    def recorded(name, seconds):
        seen.append(seconds)
        with hold(name, seconds) as acquired:
            yield acquired

    def delayed():
        entered.set()
        assert release.wait(10)
        return fill()

    monkeypatch.setattr(server.bus, "hold", recorded)
    monkeypatch.setattr(mine, "count_entities", lambda: 1_000_000)
    monkeypatch.setattr(mine, "_fill_prints", delayed)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(mine.fill_prints)
        try:
            assert entered.wait(5)
            assert seen[0] >= 2000
            lease = server.bus.key(f"lock:prints:{mine.identity}")
            assert server.bus.client.pttl(lease) > 300_000
            server.bus.client.pexpire(lease, 0)  # only this test identity's lease expires, with no wall-clock wait
            assert remote.fill_prints() > 0
        finally:
            release.set()
        assert future.result(5) == 0
    assert mine.prints_complete()
    assert remote.count_entities() == 1 and mine.build_index()


@pytest.mark.parametrize("fake", ["L.%42ianchi", "xy", "ab-1"])
def test_legacy_escaped_short_and_wordless_fake_exact_reads_are_never_rejected(stores, fake):
    mine, remote = stores
    entity = replace(LAURA, original="Laura%20Bianchi", surrogate=fake, decoded=None)
    remote.save_entity(entity)
    _ready(mine)
    vault = _vault(mine)
    with vault.holding():
        assert vault.by_surrogate(fake) == entity
        if reading := fake_reading(entity):
            assert vault.by_surrogate(reading) == entity


def test_refresh_and_cache_candidates_share_the_outer_calls_remaining_budget(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    vault = _vault(mine, working=8)
    # Warm the group; the next changed working-set read and this cache read share one allowance: past it, read on.
    _ask(mine, "Bianchi")
    remote.save_entity(MARCO)
    with monkeypatch.context() as limited:
        limited.setattr(lookup, "LOOKUP_CANDIDATES", 2)
        with vault.holding():
            vault.prefetch(["Laura Bianchi and Marco Bianchi"])
            assert vault._call().lookup.past
            assert vault.in_memory(LAURA.id) == LAURA and vault.in_memory(MARCO.id) == MARCO


async def test_a_false_forget_message_checks_sql_before_discarding_a_known_value(server):
    identity, secret = server.identities.add_key("Authoritative forget")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        runtime.store.save_entity(LAURA)
        assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.surrogate]
        space = server.spaces._spaces[identity.id]
        await asyncio.to_thread(spaces._forget, space, LAURA.id)
        assert runtime.store.by_original(LAURA.original) == LAURA
        assert await _masked(client, secret, [LAURA.original], "data") == [LAURA.surrogate]


async def test_index_build_lookup_and_cache_never_send_canaries_to_valkey_or_logs(server, monkeypatch, caplog):
    from redis.client import Pipeline, Redis

    sent = []
    command, pipeline = Redis.execute_command, Pipeline.execute

    def capture(client, *args, **kwargs):
        sent.append(repr(args))
        return command(client, *args, **kwargs)

    def capture_pipeline(client, *args, **kwargs):
        sent.append(repr(client.command_stack))
        return pipeline(client, *args, **kwargs)

    monkeypatch.setattr(Redis, "execute_command", capture)
    monkeypatch.setattr(Pipeline, "execute", capture_pipeline)
    identity, secret = server.identities.add_key("Index transport canary")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(identity)
        await _wait_ready(runtime.store)
        entity = Vault(ROOT_KEY).intern("API_KEY", EntityClass.SECRET, "xf94Kp8zA3nVm6Qw7Rt2Lj5sBc9Hy4De")
        runtime.store.save_entity(entity)
        for _ in range(2):
            assert await _masked(client, secret, [entity.original], "data") == [entity.surrogate]
        response = await client.post(f"{APPS_API}/unmask", json={"texts": [entity.surrogate]}, headers=_bearer(secret))
        assert response.status_code == 200 and response.json()["results"][0]["text"] == entity.original

        def failed(index):
            raise MemoryError(entity.original + entity.surrogate)

        monkeypatch.setattr(PrintIndex, "build", failed)
        assert not await asyncio.to_thread(runtime.store.build_index)
        wire = "\n".join(sent) + caplog.text
        protected = {entity.original, entity.surrogate, word_of(entity, "original"), word_of(entity, "surrogate")}
        protected.update(str(row[2]) for row in runtime.store._rows_of(entity) if row[2] is not None)
        assert sent and all(value not in wire for value in protected if value)


@pytest.mark.parametrize("nested", [False, True], ids=["standalone", "inside-open-call"])
def test_final_rewritten_verification_observes_a_commit_before_that_check(stores, nested):
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine)
    verifier = Verifier(Masker(vault, []))
    text = "xf94Kp8zA3nVm6Qw7Rt2Lj5sBc9Hy4De"
    slots = [(text, DATA, False)]
    with vault.holding() if nested else contextlib.nullcontext():
        assert not verifier.verify(slots, "before", rewritten=True, record=False).blocking
        _token(remote, text)
        assert verifier.verify(slots, "after", rewritten=True, record=False).blocking


def test_resync_revokes_an_inflight_exact_read_of_a_value_out_of_memory(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    vault = _vault(mine)
    original = mine.by_surrogate
    intercepted = []

    def stale(fake):
        answer = original(fake)
        if not intercepted:
            intercepted.append(True)
            remote.forget_entity(LAURA.id)
            vault.resync()
        return answer

    monkeypatch.setattr(mine, "by_surrogate", stale)
    assert vault.by_surrogate(LAURA.surrogate) is None
    assert vault.in_memory(LAURA.id) is None


def test_a_real_cross_role_print_collision_keeps_the_complete_group_for_the_next_role(stores, monkeypatch):
    mine, remote = stores
    original = pg.prints
    collided = original(mine._prints, "rossi")[pg.SLOT["surrogate"]]

    def colliding(key, word):
        values = list(original(key, word))
        if word == "bianchi":
            values[pg.SLOT["original"]] = collided
        return tuple(values)

    monkeypatch.setattr(pg, "prints", colliding)
    remote.save_entity(LAURA)
    remote.save_entity(MARCO)
    _ready(mine)
    assert _ask(mine, "Rossi", "surrogate") == [LAURA]
    sent = _queries(monkeypatch)
    assert {entity.id for entity in _ask(mine, "Bianchi", "original")} == {LAURA.id, MARCO.id}
    assert not sent


def test_legacy_word_lookup_uses_the_index_and_preserves_its_candidate_refusal(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(MARCO)
    _ready(mine)
    monkeypatch.setattr(pg, "_WITH_WORDS", "SELECT 1 / 0")
    assert {entity.id for entity in mine.with_words("original", ["bianchi"])} == {LAURA.id, MARCO.id}
    monkeypatch.setattr(pg, "CANDIDATES_AT_MOST", 1)
    with pytest.raises(pg.TooManyCandidates):
        mine.with_words("original", ["bianchi"])


def test_final_verification_cannot_certify_an_index_detached_during_its_counter_check(stores, monkeypatch):
    """T8-R1: B is published after current captures A, before A takes in the committed value."""
    mine, remote = stores
    _ready(mine)
    live = mine.index
    vault = _vault(mine, working=0)
    verifier = Verifier(Masker(vault, []))
    text = "xf94Kp8zA3nVm6Qw7Rt2Lj5sBc9Hy4De"
    slots = [(text, DATA, False)]
    assert not verifier.verify(slots, "before", rewritten=True, record=False).blocking
    assert vault.entities() == []
    built, captured = threading.Event(), threading.Event()
    publish, finish_check = threading.Event(), threading.Event()
    catch_up = PrintIndex.catch_up

    def interleaved(index, *args, **kwargs):
        if index is live:
            captured.set()
            assert finish_check.wait(10)
            return catch_up(index, *args, **kwargs)
        answer = catch_up(index, *args, **kwargs)
        built.set()  # B's final catch-up has finished at the old counter.
        assert publish.wait(10)
        return answer

    monkeypatch.setattr(PrintIndex, "catch_up", interleaved)
    with ThreadPoolExecutor(2) as executor:
        building = executor.submit(mine.build_index)
        try:
            assert built.wait(5)
            entity = _token(remote, text)  # committed before the final verification starts
            checking = executor.submit(verifier.verify, slots, "after", rewritten=True, record=False)
            assert captured.wait(5)
            publish.set()
            assert building.result(5)
            assert mine.index is not live
        finally:
            publish.set()
            finish_check.set()
        result = checking.result(5)
    assert result.blocking
    assert vault.in_memory(entity.id) is None  # working=0 after the verifier's holding closes


@pytest.mark.parametrize("failed", [False, True], ids=["readings-read", "readings-failed"])
def test_build_memory_is_reserved_before_the_aggregate_is_sent_and_readings_are_read_once_published(
    stores, monkeypatch, failed
):
    """T8-R2: the build's reservation is in the space's weight before its aggregate is made or sent, while its arrays
    are made distinct, and goes once they are let go. The readings are read after publication, with no reservation
    left: their failure keeps the published index and the exact lookup of fakes."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    _ask(mine, LAURA.original)
    live, baseline = mine.index, mine.index_bytes
    execute = psycopg.Cursor.execute
    observed = []

    def held(cursor, query, *args, **kwargs):
        if query == indexes._PRINTS:
            fresh = mine._building_index
            assert fresh is not None
            observed.append(("aggregate", fresh.building_bytes, mine.index_bytes))
        if query == indexes._READINGS:
            assert mine._building_index is None and mine.index is not live and mine.index.ready
            observed.append(("readings", 0, mine.index_bytes))
            if failed:
                raise psycopg.OperationalError("injected readings failure")
        return execute(cursor, query, *args, **kwargs)

    distinct = indexes._distinct

    def sorting(found):
        fresh = mine._building_index
        assert fresh is not None
        observed.append(("distinct", fresh.building_bytes, mine.index_bytes))
        return distinct(found)

    monkeypatch.setattr(psycopg.Cursor, "execute", held)
    monkeypatch.setattr(indexes, "_distinct", sorting)
    assert mine.build_index()
    assert [step for step, _, _ in observed] == ["aggregate", "distinct", "readings"]
    assert mine._building_index is None and mine.index is not live and mine.index.ready
    assert mine.index.building_bytes == 0 and mine.index.may_have_readings is failed
    rows = len(_vault_rows(mine))
    reserved = indexes.build_bytes(rows)
    assert reserved >= indexes.BUILD_BUFFERS * live.nbytes  # unique prints here: the old array has one a row
    assert all(held_bytes == reserved for step, held_bytes, _ in observed if step != "readings")
    assert all(weight >= baseline + reserved for step, _, weight in observed if step != "readings")


@pytest.mark.parametrize("count", [750, 1000])
def test_a_refreshed_settle_keeps_normal_batches_admitted_under_one_cumulative_budget(stores, monkeypatch, count):
    """A commit before settle requires fresh masking, without rebuilding a growing matcher for every text."""
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine, working=1000)
    apps = _apps(vault)
    texts = [f"login by customer.number{number:05d}@gmail.com" for number in range(count)]
    texts[0] += f"; archived customer {LAURA.original} completed renewal"
    settle = apps.mediator.settle
    before, budgets = {}, []

    def committed_before_settle(call):
        before.update({done.original: done.text for _, dones in call.new.values() for done in dones})
        assert len(before) == count
        assert LAURA.original in before[texts[0]]
        active = vault._call()
        assert active is not None
        budgets.append(active.lookup)
        assert remote.save_entity(LAURA) is None
        settle(call)
        active = vault._call()
        assert active is not None and active.lookup is budgets[0]

    monkeypatch.setattr(apps.mediator, "settle", committed_before_settle)
    try:
        _, out, _, _ = apps._mask(texts, DATA)
    except LookupLimitExceeded:
        budget = budgets[0]
        pytest.fail(
            f"normal refreshed batch refused: windows={budget.windows}, words={budget.words}, "
            f"candidates={budget.candidates}, characters={budget.characters}, bytes={budget.bytes}"
        )
    assert [item["text"] for item in out] == [before[text].replace(LAURA.original, LAURA.surrogate) for text in texts]
    assert len(budgets) == 1 and not budgets[0].failed and not budgets[0].past  # the fast lookup throughout
    assert 0 < budgets[0].candidates <= lookup.LOOKUP_CANDIDATES
    assert 0 < budgets[0].words <= lookup.LOOKUP_WORD_ROLES
    assert 0 < budgets[0].characters <= lookup.LOOKUP_CHARACTERS
    assert 0 < budgets[0].bytes <= lookup.LOOKUP_BYTES
    assert budgets[0].windows <= lookup.INTERIOR_WINDOWS


def test_refreshed_settle_prefetches_new_and_kept_originals_before_classifying_kept_texts(stores, monkeypatch):
    mine, remote = stores
    _ready(mine)
    vault = _vault(mine)
    mediator = _mediator_over(vault)
    session, report = Session("remembered"), MaskReport()
    texts = [f"archived customer {entity.original} completed renewal" for entity in (LAURA, MARCO)]
    first = mediator.mask_value(texts[0], DATA, session, report, keep_surrogates=False, as_text=True)
    assert first.text == texts[0]
    classified = []
    leaves = mediator._leaves_as_it_left

    def after_prefetch(*args, **kwargs):
        active = vault._call()
        assert active is not None
        assert _words(texts[0]) | _words(texts[1]) <= active.asked
        classified.append(True)
        return leaves(*args, **kwargs)

    with vault.holding():
        vault.prefetch(texts)
        call = mediator.call()
        done = [
            mediator.mask_value(text, DATA, session, report, keep_surrogates=False, call=call, as_text=True)
            for text in texts
        ]
        assert len(call.new) == 1 and len(call.kept) == 1
        for entity in (LAURA, MARCO):
            assert remote.save_entity(entity) is None
        monkeypatch.setattr(mediator, "_leaves_as_it_left", after_prefetch)
        mediator.settle(call)
    assert classified
    assert [item.text for item in done] == [
        text.replace(entity.original, entity.surrogate) for text, entity in zip(texts, (LAURA, MARCO), strict=True)
    ]


def test_sql_only_current_keeps_its_boolean_contract_and_revokes_a_consumed_negative(stores):
    mine, sql_only = stores
    assert sql_only.index is None and sql_only.current() is False
    vault = _vault(sql_only, working=0)
    with vault.holding():
        assert vault.by_surrogate(LAURA.surrogate) is None
    before = sql_only.freshness
    assert mine.save_entity(LAURA) is None
    # A background observer may consume the store check before the engine checks its own token.
    changed = sql_only.current()
    assert changed is False
    assert sql_only.freshness != before
    assert sql_only.current() is False
    with vault.holding():
        assert vault.by_surrogate(LAURA.surrogate) == LAURA


# --- Task 8 fix round 1: targeted cache invalidation, one lookup round trip a call ------------------------------------

RENATA = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "renata.colombo@ferrovie-test.example")


def _cached(store):
    return sorted(store._cache._entries)


def test_a_counter_advance_for_one_value_keeps_the_cached_answers_it_does_not_name(stores, monkeypatch):
    """A value kept elsewhere moves the counter: the catch-up drops what its rows name, and nothing else."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA] and _ask(mine, RENATA.original) == [RENATA]
    held = _cached(mine)
    assert held == sorted([LAURA.id, RENATA.id])
    _token(remote)  # unrelated words: names neither cached print nor entry
    assert mine.current()
    assert _cached(mine) == held
    sent = _queries(monkeypatch)
    assert _ask(mine, "Bianchi") == [LAURA] and _ask(mine, RENATA.original) == [RENATA]
    assert not any(pg._BY_PRINTS in query for query in sent)


def test_a_rule_given_to_a_cached_entry_drops_that_entry_alone(stores):
    """A rule kept for a value elsewhere (``save_rule``) stamps its rows: the catch-up names the entry, drops it with its
    prints, and keeps what it does not name; the next lookup gives the entry with its rule."""
    mine, remote = stores
    initial = replace(LAURA, rule=None)  # save_rule fills an absent rule
    remote.save_entity(initial)
    remote.save_entity(RENATA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [initial] and _ask(mine, RENATA.original) == [RENATA]
    ruled = replace(initial, rule="ner")
    remote.save_rule(ruled)
    assert mine.current()
    assert _cached(mine) == [RENATA.id]
    assert _ask(mine, "Bianchi") == [ruled]


def test_a_counter_advance_still_drops_every_print_and_entry_its_rows_name(stores):
    """A new value of a cached print, and a cached value given a new fake: neither stale answer is ever served."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    assert _ask(mine, "Rossi", "surrogate") == [LAURA]
    assert _ask(mine, RENATA.original) == [RENATA]
    remote.save_entity(MARCO)  # bianchi again
    changed = replace(LAURA, surrogate="Carla Fontana", counter=1)
    assert remote.replace_fake(LAURA, changed) is None
    assert mine.current()
    assert _cached(mine) == [RENATA.id]  # the one entry no new row names stays
    assert {e.id: e for e in _ask(mine, "Bianchi")} == {LAURA.id: changed, MARCO.id: MARCO}
    assert _ask(mine, "Rossi", "surrogate") == []
    assert _ask(mine, "Fontana", "surrogate") == [changed]


def test_a_wordless_entry_given_a_wordless_fake_is_dropped_by_its_rows_without_prints(stores):
    """An entry whose rows lose every print (a wordless value given a wordless fake) is still named by its stamp."""
    mine, remote = stores
    entity = replace(LAURA, original="x9-k2", surrogate="Ada Rossi", etype="BADGE", eclass=EntityClass.CUSTOM)
    assert word_of(entity, "original") is None and word_of(entity, "surrogate") == "rossi"
    remote.save_entity(entity)
    _ready(mine)
    assert _ask(mine, "Rossi", "surrogate") == [entity]
    wordless = replace(entity, surrogate="q7-z4", counter=1)
    assert word_of(wordless, "surrogate") is None
    assert remote.replace_fake(entity, wordless) is None
    assert mine.current()
    assert entity.id not in _cached(mine)
    assert _ask(mine, "Rossi", "surrogate") == []


@pytest.mark.parametrize("forget", ["entity", "values", "beside-a-new-value"])
def test_a_forget_with_no_message_empties_the_cache_at_the_next_counter_read(stores, forget):
    """Deleted rows name nothing: a forget's own count of forgets empties the whole cache at the catch-up."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA] and _ask(mine, RENATA.original) == [RENATA]
    if forget == "values":
        assert remote.forget_values([LAURA.original]).ids == [LAURA.id]
    else:
        remote.forget_entity(LAURA.id)
    if forget == "beside-a-new-value":
        remote.save_entity(MARCO)
    assert mine.current()
    assert _cached(mine) == []
    assert _ask(mine, "Bianchi") == ([MARCO] if forget == "beside-a-new-value" else [])
    assert _ask(mine, RENATA.original) == [RENATA]


def test_a_catch_up_without_this_stores_cache_empties_it_before_the_next_lookup(stores):
    """Another caller caught the index up and told no cache: the store cannot know what the rows named."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    remote.save_entity(MARCO)
    assert mine.index.catch_up() is not None  # no cache given
    mine.current()
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}


def test_a_watermark_moved_without_telling_the_cache_empties_it_before_the_next_lookup(stores):
    """The published index built again in place (its watermark moved, no catch-up told the cache): the lookup itself
    finds the cache behind and empties it, with no counter check in between."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    remote.save_entity(MARCO)
    assert mine.index.build()
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}


def test_a_lookup_inside_an_open_call_that_wrote_never_trusts_the_cache_past_the_index(stores):
    """The open call reads the counter through its own transaction: the cache is not used until the index caught up."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    transaction = mine.together()
    try:
        _token(mine, "OpenCallSecret55aa77bb99cc")  # the call's own row: its transaction is open
        remote.save_entity(MARCO)  # committed elsewhere meanwhile
        mine.current()  # through the open transaction's connection
        assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}
    finally:
        transaction.rollback()
    mine.current()
    assert {e.id for e in _ask(mine, "Bianchi")} == {LAURA.id, MARCO.id}


@pytest.mark.parametrize("path", ["sql", "index"])
def test_a_calls_words_are_hashed_at_most_words_at_once_digests_at_a_time(stores, monkeypatch, path):
    """RESUME-5's sequential bounded hashing, kept with one lookup a call: never more than WORDS_AT_ONCE of the words'
    keyed hashes (the SQL path) or prints (the index's) live before they are sent, or asked of the index."""
    from latet.engine.anchors import EDGE

    mine, remote = stores
    remote.save_entity(LAURA)
    if path == "index":
        _ready(mine)
    monkeypatch.setattr(pg, "WORDS_AT_ONCE", 8)
    roles = ("original", "surrogate")
    words = {f"word{n:04d}" for n in range(300)} | {f"edge{n:02d}" for n in range(40)} | {"bianchi", "laura"}
    assert all(len(word) == EDGE for word in words if word.startswith("edge"))
    live, highest = [0], [0]

    def made(word):
        live[0] += 2 if len(word) == EDGE else 1
        highest[0] = max(highest[0], live[0])

    if path == "sql":
        keyed_pair = mine._sealer.keyed_pair

        def hashing(text):
            made(text.split("\x00", 1)[1])
            return keyed_pair(text)

        monkeypatch.setattr(mine._sealer, "keyed_pair", hashing)
        execute = psycopg.Cursor.execute

        def sent(cursor, query, params=None, **kwargs):
            if str(query) == pg._BOUNDED_WORDS:
                live[0] = 0
            return execute(cursor, query, params, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", sent)
    else:
        printing, present = pg.prints, mine.index.present

        def printed(key, word):
            for _ in roles:
                made(word)
            return printing(key, word)

        def asked(found):
            live[0] = 0
            return present(found)

        monkeypatch.setattr(pg, "prints", printed)
        monkeypatch.setattr(mine.index, "present", asked)
    found = mine.bounded_words(roles, words, LookupBudget())
    assert {e.id for e in found} == {LAURA.id}
    assert 0 < highest[0] <= pg.WORDS_AT_ONCE


def test_a_calls_words_are_asked_of_the_store_in_one_lookup(stores, monkeypatch):
    """The store bounds each statement itself: a call of more words than one engine batch is one lookup."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    vault = _vault(mine)
    texts = [f"line {n} word{n:05d} other{n:05d} Laura Bianchi" for n in range(3000)]
    asked = []
    bounded = mine.bounded_words

    def counted(role, words, budget):
        asked.append(len(words))
        return bounded(role, words, budget)

    monkeypatch.setattr(mine, "bounded_words", counted)
    with vault.holding(scoped=True):
        vault.prefetch(texts)
    assert len(asked) == 1 and asked[0] > lookup.LOOKUP_BATCH


def test_indexed_fetches_share_one_autocommit_connection_and_no_server_cursor(stores, monkeypatch):
    """Groups of prints past WORDS_AT_ONCE are fetched one statement each, on one connection, with no transaction
    and no named cursor: each answer is bounded before it is sent."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    monkeypatch.setattr(pg, "WORDS_AT_ONCE", 1)
    fetched, declared, checkouts = [], [], []
    for cls, seen in ((psycopg.Cursor, fetched), (psycopg.ServerCursor, declared)):
        execute = cls.execute

        def record(cursor, query, params=None, *, _execute=execute, _seen=seen, **kwargs):
            if str(query) == pg._BY_PRINTS:
                _seen.append(cursor.connection.autocommit)
            return _execute(cursor, query, params, **kwargs)

        monkeypatch.setattr(cls, "execute", record)
    connection = mine.db.connection

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        checkouts.append(True)
        with connection(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(mine.db, "connection", counting)
    found = mine.bounded_words("original", {"bianchi", *_words(RENATA.original)}, LookupBudget())
    assert {e.id for e in found} == {LAURA.id, RENATA.id}
    assert len(fetched) >= 2 and all(fetched)  # one statement a group, each its own transaction (autocommit)
    assert not declared
    assert len(checkouts) == 1


# --- Task 8 fix round 4: lookups planned for their own parameters; the resync of what the catch-up named --------------


@pytest.mark.parametrize("path", ["sql", "index", "ids", "retained"])
def test_bounded_lookups_are_never_prepared_so_no_generic_plan_serves_them(server, path):
    """psycopg prepares a statement on a connection after five executions, and PostgreSQL then plans it once for any
    parameters (a generic plan): on a small vault the bounded lookups' generic plan walks every entry by its order of
    keeping, ten times the plan made for the call's own words. Each is sent unprepared, on any connection."""
    identity, _ = server.identities.add_key("Unprepared lookups")
    db = Database(os.environ["LATET_TEST_DATABASE_URL"], schema=server.db.schema, min_size=1, max_size=1)
    store = pg.PgVaultStore(db, identity.id, ROOT_KEY, index=path == "index")
    try:
        assert store.save_entity(LAURA) is None
        if path == "index":
            _ready(store)
        for _ in range(12):  # past psycopg's five and PostgreSQL's five custom plans
            budget = LookupBudget()
            if path in ("sql", "index"):
                store._cache.clear()  # each lookup asks PostgreSQL
                assert _ask(store, "Bianchi", budget=budget) == [LAURA]
            elif path == "ids":
                assert store.bounded_ids([LAURA.id], budget) == [LAURA]
            else:
                store.bounded_retained(10, True, budget)
        with db.connection() as conn:  # the pool's one connection: every lookup above ran on it
            prepared = [statement for (statement,) in conn.execute("SELECT statement FROM pg_prepared_statements")]
        assert not [statement for statement in prepared if "candidates AS MATERIALIZED" in statement]
    finally:
        store.close()
        db.close()


def test_the_sql_lookup_reads_the_words_first_then_the_entries_by_their_key():
    """The entries of the words found are asked as an array of ids (``id = ANY(ARRAY(...))``): PostgreSQL reads the
    words by their index, then the entries by their key, whatever the vault's size; ``id IN (...)`` let it hash-join a
    scan of every entry of the key (60 ms a statement at 200,000 values, against 25)."""
    assert "AND id = ANY(ARRAY(SELECT w.entity_id FROM vault_words w" in pg._BOUNDED_WORDS
    assert " id IN (" not in pg._BOUNDED_WORDS


def _confirmed(store, monkeypatch):
    """The ids each by-id confirmation of the store asks for, a list a call."""
    asked = []
    bounded = store.bounded_ids

    def counted(ids, budget):
        ids = list(ids)
        asked.append(sorted(ids))
        return bounded(ids, budget)

    monkeypatch.setattr(store, "bounded_ids", counted)
    return asked


def _held(vault, *entities):
    """``entities`` brought into the vault's memory, as a call that masks their values brings them."""
    with vault.holding(scoped=True):
        vault.prefetch([" ".join(e.original for e in entities)])
    for e in entities:
        assert vault.in_memory(e.id) == e


def _refreshed(vault):
    with vault.holding():
        pass


def test_a_change_named_by_the_catch_up_is_confirmed_alone(stores, monkeypatch):
    """A value kept elsewhere moves the counter: the next call re-reads only what the catch-up named among what memory
    holds (none of it here), not the whole working set."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    _token(remote)  # unrelated
    _refreshed(vault)
    assert all(len(ids) == 0 for ids in asked)
    assert vault.in_memory(LAURA.id) == LAURA and vault.in_memory(RENATA.id) == RENATA


def test_a_rule_kept_elsewhere_on_a_held_entry_is_never_served_stale(stores, monkeypatch):
    mine, remote = stores
    initial = replace(LAURA, rule=None)
    remote.save_entity(initial)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, initial, RENATA)
    asked = _confirmed(mine, monkeypatch)
    remote.save_rule(replace(initial, rule="ner"))
    _refreshed(vault)
    assert asked == [[LAURA.id]]
    assert vault.in_memory(LAURA.id) is None  # the stale entry left memory, found again as it is
    assert vault.in_memory(RENATA.id) == RENATA
    with vault.holding():
        assert vault.by_original(LAURA.original).rule == "ner"


def test_a_fake_given_anew_elsewhere_on_a_held_entry_is_never_served_stale(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    changed = replace(LAURA, surrogate="Carla Fontana", counter=1)
    assert remote.replace_fake(LAURA, changed) is None
    _refreshed(vault)
    assert asked == [[LAURA.id]]
    assert vault.in_memory(LAURA.id) is None
    with vault.holding():
        assert vault.by_original(LAURA.original) == changed
        assert vault.by_surrogate(LAURA.surrogate) is None


@pytest.mark.parametrize("forget", ["held", "other"])
def test_a_forget_elsewhere_with_no_message_resyncs_the_whole_working_set(stores, monkeypatch, forget):
    """Deleted rows name nothing: the forget count moved, and the whole working set is confirmed by its ids."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    other = _token(remote)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    remote.forget_entity(LAURA.id if forget == "held" else other.id)
    _refreshed(vault)
    assert asked and set(asked[0]) >= {LAURA.id, RENATA.id}
    assert (vault.in_memory(LAURA.id) is None) is (forget == "held")
    with vault.holding():
        assert (vault.by_original(LAURA.original) is None) is (forget == "held")


def test_a_forget_elsewhere_with_no_message_leaves_no_string_of_the_value_in_the_words_memo(stores):
    """Fix round 8 (the review of round 7's I-1): the memo of the words a key's calls found (``_both_words``) holds no
    value or fake, only a keyed digest of them; and once another instance forgot a value and its message was lost, the
    whole resync of the next call empties it, so that no word of the forgotten value stays either."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    assert LAURA.id in vault._words_memo
    memo = repr(list(vault._words_memo.items()))
    assert LAURA.original not in memo and LAURA.surrogate not in memo
    with vault._lock:  # out of memory, as the least recently used goes: the memo outlives memory and keeps its words
        vault._unindex(vault._by_id[LAURA.id])
    assert vault.in_memory(LAURA.id) is None and LAURA.id in vault._words_memo
    remote.forget_entity(LAURA.id)  # no message: this instance hears of it only from the store's counts
    _refreshed(vault)
    assert LAURA.id not in vault._words_memo
    kept = {word for _, words, _ in vault._words_memo.values() for word in words}
    assert not kept & set(stored._words_of_both(LAURA))
    assert vault.words_bytes == sum(weight for _, _, weight in vault._words_memo.values())


def test_with_no_index_ready_the_whole_working_set_is_confirmed(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    vault = _vault(mine, working=1000)  # mine's index never built: the SQL path
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    _token(remote)
    _refreshed(vault)
    assert asked and set(asked[0]) >= {LAURA.id, RENATA.id}


def test_after_a_barrier_the_whole_working_set_is_confirmed(stores, monkeypatch):
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    mine.invalidate()  # a lost message's barrier: nothing says what changed
    _refreshed(vault)
    assert asked and set(asked[0]) >= {LAURA.id, RENATA.id}


def test_a_catch_up_that_names_nothing_confirms_nothing_and_serves_what_is(stores, monkeypatch):
    """The counter moved and no row was stamped (a forget of an entry already gone): nothing to confirm."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA)
    asked = _confirmed(mine, monkeypatch)
    remote.forget_entity("e" + "0" * 19)  # no such entry: the counter moves, no row and no forget count
    _refreshed(vault)
    assert all(len(ids) == 0 for ids in asked)
    assert vault.in_memory(LAURA.id) == LAURA


def test_what_a_catch_up_names_is_told_before_its_watermark_moves(stores, monkeypatch):
    """A call that sees the new watermark (or the cache's new epoch) finds what the catch-up named already told: it
    never takes the change as seen and confirms nothing."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    seen = []
    drop = PrintCache.drop

    def dropping(cache, entities, found):
        seen.append((mine._named_seq, mine.index.watermark))
        return drop(cache, entities, found)

    monkeypatch.setattr(PrintCache, "drop", dropping)
    before = (mine._named_seq, mine.index.watermark)
    remote.save_entity(MARCO)
    mine.current()
    assert seen and seen[0][0] > before[0] and seen[0][1] == before[1]


# --- Task 8 fix round 5: a telling is one with its fence; the log bounded and weighed ----------------------------------


class _Watched:
    """A lock that says whether a thread had to wait for it (``waited``), and how often it was taken."""

    def __init__(self, lock):
        self._lock, self.waited, self.taken = lock, threading.Event(), 0

    def acquire(self, blocking=True, timeout=-1):
        if not self._lock.acquire(blocking=False):
            self.waited.set()
            if not self._lock.acquire(blocking, timeout):
                return False
        self.taken += 1
        return True

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _until_done_or_waiting(thread, watched, seconds=30):
    """Until ``thread`` ends or waits for ``watched``: whichever comes first, never a guess at a time."""
    deadline = time.monotonic() + seconds
    while thread.is_alive() and not watched.waited.is_set() and time.monotonic() < deadline:
        time.sleep(0.002)


def test_a_telling_is_never_read_before_the_cache_it_tells_of_is_dropped(stores, monkeypatch):
    """The review of fix round 4's I-1, step by step. A catch-up K takes in a change of entry E and tells it; before it
    drops E's old answer from the cache, a call R whose counter read came before the change committed (it does not wait
    for K's lock) sees freshness move for another reason, reads the telling, finds E out of memory (nothing to confirm)
    and brings E into memory from the cache, stale; then K drops it, and nothing names E again. A telling and its fence
    are one, under the log's lock, which a reader of the log takes too: R reads it once K's drop is done, and brings E
    in as it is now."""
    mine, remote = stores
    remote.save_entity(RENATA)
    vault = _vault(mine, working=1000)  # made before E is kept: E is not in its memory
    initial = replace(LAURA, rule=None)
    remote.save_entity(initial)
    _ready(mine)
    _refreshed(vault)  # its memory confirmed after the build
    assert _ask(mine, "Bianchi") == [initial]  # E's answer in the cache: another call's lookup
    assert initial.id in _cached(mine) and vault.in_memory(initial.id) is None
    mine.drop_cached("e" + "9" * 19)  # freshness moves for another reason: a forget's message, an id held nowhere
    ruled = replace(initial, rule="ner")
    remote.save_rule(ruled)  # E changes elsewhere
    before = mine.index.watermark
    watched = mine._named_lock = _Watched(mine._named_lock)
    told, resume = threading.Event(), threading.Event()
    drop, catch_up = PrintCache.drop, PrintIndex.catch_up

    def pausing(cache, entities, found):
        entities = list(entities)
        if threading.current_thread().name == "catch-up" and initial.id in entities:
            told.set()
            assert resume.wait(30)
        return drop(cache, entities, found)

    def early(index, counter=None, cache=None, named=None):
        if threading.current_thread().name == "reader":
            counter = before  # its counter read came before E's change committed
        return catch_up(index, counter, cache, named)

    monkeypatch.setattr(PrintCache, "drop", pausing)
    monkeypatch.setattr(PrintIndex, "catch_up", early)
    seen = []

    def reading():
        with vault.holding(scoped=True):
            vault.prefetch([initial.original])
            seen.append(vault.in_memory(initial.id))

    catching = threading.Thread(target=mine.current, name="catch-up")
    reader = threading.Thread(target=reading, name="reader")
    catching.start()
    try:
        assert told.wait(30)  # K told E and waits before its drop
        reader.start()
        _until_done_or_waiting(reader, watched)
    finally:
        resume.set()
        catching.join(30)
        if reader.ident is not None:
            reader.join(30)
    assert seen == [ruled]  # R read E after its old answer was dropped
    _refreshed(vault)
    assert vault.in_memory(initial.id) in (None, ruled)
    with vault.holding():
        assert vault.by_original(initial.original) == ruled


def _raising(*_args, **_kwargs):
    raise psycopg.OperationalError("synthetic")


@pytest.mark.parametrize(
    "mover", ["catch-up", "tell", "barrier", "forget message", "publication", "index failure", "count read alone"]
)
def test_every_move_of_freshness_is_told_with_its_fence_under_the_logs_lock(stores, monkeypatch, mover):
    """What a telling tells of (the cache told or emptied, the index published, the counter seen) happens under the
    log's lock with the telling: no reader of the log finds a telling whose fence is not up yet."""
    mine, remote = stores
    remote.save_entity(LAURA)
    _ready(mine)
    assert _ask(mine, "Bianchi") == [LAURA]
    fenced = []
    for name, original in (("clear", PrintCache.clear), ("drop", PrintCache.drop)):

        def watching(cache, *args, _original=original):
            fenced.append(mine._named_lock.locked())
            return _original(cache, *args)

        monkeypatch.setattr(PrintCache, name, watching)
    seq = mine.named_seq
    if mover == "catch-up":
        remote.save_entity(MARCO)
        mine.current()
    elif mover == "tell":
        remote.save_entity(MARCO)
        mine.index.catch_up()  # a catch-up that told no cache: the watermark moved past what the cache was told
        mine.current()
    elif mover == "barrier":
        mine.invalidate()
    elif mover == "forget message":
        mine.drop_cached(LAURA.id)
    elif mover == "publication":
        assert mine.build_index()
    elif mover == "index failure":
        remote.save_entity(MARCO)
        monkeypatch.setattr(PrintIndex, "catch_up", _raising)
        mine.current()
    else:
        mine.index.ready = False
        remote.save_entity(MARCO)
        mine.current()
    assert fenced and all(fenced), fenced
    assert mine.named_seq > seq


@pytest.mark.parametrize("reader", ["named_seq", "named_after"])
def test_a_reader_of_the_log_waits_for_a_telling_under_way(stores, reader):
    """A vault starts from ``named_seq`` and confirms from ``named_after``: neither reads past a telling whose fence is
    not up yet."""
    mine, _ = stores
    watched = mine._named_lock = _Watched(mine._named_lock)
    answered = []
    asking = threading.Thread(
        target=lambda: answered.append(mine.named_seq if reader == "named_seq" else mine.named_after(0))
    )
    with watched:  # a telling under way
        asking.start()
        _until_done_or_waiting(asking, watched)
        assert watched.waited.is_set() and not answered
    asking.join(30)
    assert answered


def test_the_log_of_what_changed_holds_a_bounded_number_of_names_and_is_weighed(stores, monkeypatch):
    """At most ``NAMED_IDS_KEPT`` names (a vault further behind confirms its whole memory, as one whose tellings were
    let go), at most ``NAMED_KEPT`` tellings, and what it holds counted in ``index_bytes``: the space's weight."""
    mine, _ = stores
    monkeypatch.setattr(pg, "NAMED_IDS_KEPT", 4)
    monkeypatch.setattr(pg, "NAMED_KEPT", 3)
    assert mine.index_bytes == 0 and mine._named_bytes == 0
    start = mine.named_seq
    names = [[f"e{n}{m}" + "0" * 18 for m in range(2)] for n in range(3)]
    for told in names:
        with mine._telling(told):
            pass
    assert mine._named_ids <= 4 and len(mine._named) <= 3
    assert mine.named_after(start) == (mine.named_seq, None)  # the log no longer reaches back: the whole memory
    assert mine.named_after(start + 1) == (mine.named_seq, {*names[1], *names[2]})
    held = mine._named_bytes
    assert held >= sum(sys.getsizeof(name) for told in names[1:] for name in told)
    assert mine.index_bytes == held  # no index, no cache: the log alone
    with mine._telling([f"e9{m}" + "0" * 18 for m in range(5)]):  # more names than the log keeps: not by name
        pass
    assert mine.named_after(mine.named_seq - 1) == (mine.named_seq, None)
    for _ in range(3):
        with mine._telling(None):
            pass
    assert mine._named_ids == 0 and len(mine._named) == 3
    assert 0 < mine._named_bytes < held and mine.index_bytes == mine._named_bytes


def test_by_default_the_log_keeps_no_more_names_than_a_lookup_hashes_at_once():
    assert pg.NAMED_IDS_KEPT == pg.WORDS_AT_ONCE


class _Untouchable:
    """A telling no reader may open: a walk of the whole log would."""

    def __iter__(self):
        raise AssertionError("the log was walked from its start")


def test_what_changed_is_read_from_the_tellings_after_a_vaults_own_alone(stores):
    mine, _ = stores
    for n in range(3):
        with mine._telling([f"e{n}" + "0" * 19]):
            pass
    mine._named[0] = _Untouchable()  # the oldest
    seq = mine.named_seq
    with mine._telling(["e9" + "0" * 18]):
        pass
    assert mine.named_after(seq - 1) == (seq + 1, {"e2" + "0" * 19, "e9" + "0" * 18})
    assert mine.named_after(mine.named_seq) == (mine.named_seq, set())


def test_a_direct_resync_is_not_followed_by_a_second_whole_one(stores, monkeypatch):
    """A space's resync after a lost message confirms the whole working set behind its own barrier: the next call
    finds that barrier already taken in, and confirms nothing again."""
    mine, remote = stores
    remote.save_entity(LAURA)
    remote.save_entity(RENATA)
    _ready(mine)
    vault = _vault(mine, working=1000)
    _held(vault, LAURA, RENATA)
    asked = _confirmed(mine, monkeypatch)
    vault.resync()
    assert len(asked) == 1 and set(asked[0]) >= {LAURA.id, RENATA.id}
    _refreshed(vault)
    assert len(asked) == 1
    remote.save_rule(replace(RENATA, rule="email"))  # and what changes after it is still confirmed
    _refreshed(vault)
    assert asked[1:] == [[RENATA.id]]


# --- Task 8 fix round 7: the words of what a call found, kept and weighed ----------------------------------------------


async def test_the_words_memo_is_weighed_with_its_space(server):
    """The words of the entries a key's calls found (``StoredVault.words_bytes``) count in its space's weight."""
    identity, secret = server.identities.add_key("Words weight")
    async with _calling(server) as client:
        await _masked(client, secret, ["password=" + TOKEN])
        runtime = await server.spaces.runtime(identity)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault)
        before = spaces.space_size(runtime)
        held = vault.words_bytes
        source = Vault(ROOT_KEY)
        for n in range(50):
            vault._both_words(source.intern("EMAIL", EntityClass.PII, f"user.number{n}@company{n}.example"))
        extra = vault.words_bytes - held
        assert extra > 0 and spaces.space_size(runtime) >= before + extra


async def test_what_a_keys_detectors_take_more_is_weighed_with_its_space(server):
    """Fix round 8: a key's parts are given ``SCALED_PER_BYTE`` for each byte of their patterns, as every text is read
    for them; what that adds past the ordinary budget (``StoredVault.detectors_bytes``) counts in its space's weight."""
    identity, secret = server.identities.add_key("Detectors weight")
    async with _calling(server) as client:
        await _masked(client, secret, ["nothing to mask here"])
        runtime = await server.spaces.runtime(identity)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault)
        await asyncio.to_thread(vault.matchers.current)
        assert vault.detectors_bytes == 0  # no secret held: nothing more
        fixed = spaces._fixed(runtime) - vault.words_bytes
        hexes = [f"{n * 7919 + 104729:016x}"[::-1] for n in range(199)]  # one run of 16 hex digits each
        await _masked(client, secret, [f"password={value}" for value in hexes], kind="data")
        await asyncio.to_thread(vault.matchers.current)
        assert len(vault._matched) >= 150
        extra = vault.detectors_bytes
        assert extra >= 1 << 20
        assert spaces.space_size(runtime) >= fixed + extra
        assert spaces._fixed(runtime) - vault.words_bytes < fixed + extra  # weighed in its size, never in its share


async def test_a_key_holding_thousands_of_one_run_tokens_on_a_small_budget_masks_every_request(server, monkeypatch):
    """Fix round 9 (the review of round 8's I-1; the owner's ruling: a request is never refused for its size where the
    release before masked it). A key logging a fresh token of 16 hex digits in each line holds thousands of them in its
    detectors. On a server of 256 MiB (a share of 128), round 8's budget for its parts would have counted some 200 MiB:
    past its share, every new value refused. Its parts take an eighth of the share at most past the ordinary budget,
    the rest compiled at the ordinary budget (slower, exact), and what they take never refuses a value."""
    monkeypatch.setattr(server.spaces, "budget", 256 << 20)
    identity, secret = server.identities.add_key("Many tokens")
    rng = random.Random(11)
    tokens = [f"{rng.getrandbits(64):016x}" for _ in range(5000)]
    async with _calling(server) as client:
        for at in range(0, len(tokens), 1000):
            batch = tokens[at : at + 1000]
            masked = await _masked(client, secret, [f"session_token={token}" for token in batch], kind="data")
            assert not any(token in text for token, text in zip(batch, masked, strict=True))
        runtime = await server.spaces.runtime(identity)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault)
        await asyncio.to_thread(vault.matchers.current)
        ceiling = int((256 << 20) * spaces.SPACE_SHARE * spaces.DETECTORS_SHARE)
        assert vault.detectors_extra_most == ceiling
        assert len(vault._matched) >= 4000
        assert vault.detectors_bytes <= ceiling  # a part of 2,000 tokens wants 32 MiB more: past 16, ordinary
