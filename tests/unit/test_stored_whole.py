"""A person's vault on a Latet server small enough to be held whole (``StoredVault(whole_at_most=...)``, the owner's
ruling of 2026-10-31): every value of its store in memory, its texts read from memory alone with no lookup of the store
for each, current through the store's log of changes as a vault on its store is (a value kept or forgotten on another
instance seen at the next call), every value weighed with its space, and the store's lookups again once it grows past
its bound."""

import dataclasses
import threading

import pytest

from latet.engine import stored
from latet.engine.lookup import LookupBudget
from latet.engine.stored import WHOLE_VALUES, StoredVault
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, MaskingError

from ..conftest import ROOT_KEY
from .test_stored_vault import _engine, _Store

BOUND = 50  # the tests' bound of a vault held whole (``whole_at_most``)
WORKING = 10  # their working set, well below it


def _name(n: int) -> str:
    """A person's name no detector finds without the vault (the name model off): found by its known value alone."""
    letters = "".join(chr(97 + (n // 26**i) % 26) for i in range(3))
    return f"Ottavio Brambilla{letters}"


class _Logged(_Store):
    """``_Store`` telling what changed by name as PostgreSQL's store does (``PgVaultStore.named_after``): a value kept
    names its entry; a forget names nothing (its rows went with it), so that a vault that sees it confirms its whole
    memory. Its freshness moves at each telling: two vaults over it are two instances of the server."""

    def __init__(self) -> None:
        super().__init__()
        self.told: list[frozenset[str] | None] = []
        self.recent_reads = 0

    def _tell(self, names: frozenset[str] | None) -> None:
        with self._lock:
            self.told.append(names)

    @property
    def named_seq(self) -> int:
        return len(self.told)

    @property
    def freshness(self) -> int:
        return len(self.told)

    def named_after(self, seq: int) -> tuple[int, set[str] | None]:
        with self._lock:
            names: set[str] = set()
            for told in self.told[seq:]:
                if told is None:
                    return len(self.told), None
                names |= told
            return len(self.told), names

    def save_entity(self, e: Entity) -> Entity | None:
        found = super().save_entity(e)
        if found is None:
            self._tell(frozenset({e.id}))
        return found

    def forget_entity(self, entity_id: str) -> None:
        super().forget_entity(entity_id)
        self._tell(None)

    def load_recent(self, n: int) -> list[Entity]:
        self.recent_reads += 1
        return super().load_recent(n)


def _kept(store: _Store, count: int, start: int = 0) -> list[Entity]:
    """``count`` names kept in ``store`` by a vault that holds none of them after (another instance's)."""
    keeper = StoredVault(ROOT_KEY, store, working=0, on_insert=store.save_entity)
    return [keeper.intern("PERSON", EntityClass.NER, _name(n), rule="ner") for n in range(start, start + count)]


def _person(store: _Store, bound: int = BOUND) -> StoredVault:
    """A person's vault over ``store`` (``glued_all``, its whole vault read past a bound), held whole up to ``bound``
    values."""
    return StoredVault(
        ROOT_KEY, store, working=WORKING, glued_all=True, whole_past_bounds=True, whole_at_most=bound,
        on_insert=store.save_entity, on_rule=store.save_rule,
    )  # fmt: skip


def _lookups(store: _Store) -> list[str]:
    """The lookups by words ``store`` was asked (``with_words``)."""
    return [call for call in store.calls if call.startswith("with_words")]


def _masked(vault: StoredVault, text: str) -> str:
    """``text`` as the gateway sends a tool's output (``Engine.mask_text``: a call of its own)."""
    engine = _engine(vault)
    return engine.mask_text(text, SlotKind.TOOL_RESULT, engine.session("t")).text


def test_the_bound_of_a_vault_held_whole():
    # The measurement of 2026-10-31 (notes/server-performance-small-vaults.md): on its store a person's vault masks about
    # as fast as 0.10.0's whole vault at 30,000 values and faster from 35,000; held whole, about 1.6 KB a value.
    assert WHOLE_VALUES == 35_000
    assert StoredVault.__init__.__kwdefaults__["whole_at_most"] == 0  # an API key's vault: on its store, as before


def test_a_small_persons_vault_is_held_whole_and_its_texts_ask_the_store_nothing():
    store = _Store()
    names = _kept(store, 30)
    vault = _person(store)
    assert vault.holds_all and vault.count == 30  # every one, its working set of 10 or not
    store.calls.clear()
    oldest = names[0]
    left = _masked(vault, f"ticket 4411 closed by {oldest.original} at 10:02")
    assert left == f"ticket 4411 closed by {oldest.surrogate} at 10:02"
    engine = _engine(vault)
    assert engine.unmask(f"reply to {names[1].surrogate}") == (f"reply to {names[1].original}", 1)
    with vault.holding():  # an unmask in a call, as the applications API makes it
        vault.prefetch_fakes([f"reply to {names[2].surrogate}"])
        assert engine.unmask(f"reply to {names[2].surrogate}") == (f"reply to {names[2].original}", 1)
    assert _lookups(store) == [] and vault.count == 30


def test_a_vault_held_whole_masks_on_the_event_loop_what_its_store_holds():
    """On the event loop a vault on its store reads memory alone: held whole, memory is every value."""
    import asyncio

    store = _Store()
    names = _kept(store, 30)
    vault = _person(store)

    async def on_the_loop() -> str:
        return _masked(vault, f"seen: {names[0].original}")

    assert asyncio.run(on_the_loop()) == f"seen: {names[0].surrogate}"


def test_a_vault_past_its_bound_is_on_its_store_as_before():
    store = _Store()
    names = _kept(store, BOUND + 1)
    vault = _person(store)
    assert not vault.holds_all and vault.count <= WORKING
    store.calls.clear()
    assert _masked(vault, f"seen: {names[0].original}") == f"seen: {names[0].surrogate}"
    assert _lookups(store)  # found in the store by its words


def test_a_vault_held_whole_leaves_it_once_it_grows_past_its_bound():
    store = _Store()
    names = _kept(store, BOUND)
    vault = _person(store)
    assert vault.holds_all
    with vault.holding():
        vault.intern("PERSON", EntityClass.NER, _name(BOUND), rule="ner")
    assert not vault.holds_all and vault.count <= WORKING  # its working set again once the call ended
    store.calls.clear()
    assert _masked(vault, f"seen: {names[0].original}") == f"seen: {names[0].surrogate}"
    assert _lookups(store)  # out of memory: found in the store by its words


def test_nothing_leaves_memory_while_a_call_begun_whole_is_open():
    """A call begun while every value was in memory reads memory alone to its end: a value kept meanwhile that carries
    the vault past its bound, on this thread or another, lets no value go before the call ends."""
    store = _Store()
    names = _kept(store, BOUND)
    vault = _person(store)
    engine = _engine(vault)
    session = engine.session("t")
    store.calls.clear()
    with vault.holding():
        other = threading.Thread(
            target=lambda: [vault.intern("PERSON", EntityClass.NER, _name(n), rule="ner") for n in range(BOUND, 60)]
        )
        other.start()
        other.join()
        assert not vault.holds_all and vault.count == 60
        for entity in names[:5]:
            text = f"seen: {entity.original}"
            assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == f"seen: {entity.surrogate}"
        assert _lookups(store) == [] and vault.count == 60
    assert vault.count <= WORKING


def test_a_value_kept_on_another_instance_is_masked_by_a_vault_held_whole():
    store = _Logged()
    _kept(store, 20)
    here, there = _person(store), _person(store)
    assert here.holds_all and there.holds_all
    new = there.intern("PERSON", EntityClass.NER, _name(20), rule="ner")  # kept on the other instance
    assert here.in_memory(new.id) is None
    store.calls.clear()
    assert _masked(here, f"call {new.original} back") == f"call {new.surrogate} back"
    assert here.in_memory(new.id) is not None and here.holds_all
    assert _lookups(store) == [] and store.calls == ["by_ids"]  # what changed read by its name, nothing else


def test_a_value_named_to_one_of_two_resyncs_at_once_is_taken_in():
    """Two calls' refreshes at once (the review of the change): the one named after a value kept elsewhere reads it
    while the other, named before, begins its own resync. The first takes the value in all the same: a resync of a vault
    is never cut short by another (one at a time), and the value, which no later telling names again, is masked."""
    store = _Logged()
    names = _kept(store, 20)
    vault = _person(store)
    new = _person(store).intern("PERSON", EntityClass.NER, _name(20), rule="ner")  # kept on another instance
    entered, go = threading.Event(), threading.Event()
    real = store.by_ids
    blocked: list[int] = []

    def by_ids(ids):
        if threading.get_ident() in blocked:
            entered.set()
            assert go.wait(10)
        return real(ids)

    store.by_ids = by_ids  # type: ignore[method-assign]

    def named_after_it() -> None:
        blocked.append(threading.get_ident())
        vault.resync(invalidate=False, only=[names[0].id, new.id])

    first = threading.Thread(target=named_after_it)
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=lambda: vault.resync(invalidate=False, only=[names[0].id]))
    second.start()
    second.join(1.0)  # done at once if a resync could cut the first one short; else waiting for it
    go.set()
    first.join(10)
    second.join(10)
    assert vault.holds_all and vault.in_memory(new.id) is not None
    assert _masked(vault, f"call {new.original} back") == f"call {new.surrogate} back"


def test_a_call_begun_whole_asks_the_store_once_its_refresh_finds_the_vault_on_it():
    """A call begun with every value in memory, the vault past its bound since (another thread's values): once the
    call's own refresh takes in what changed elsewhere as a vault on its store does (by its words, not into memory), its
    texts are read through the store again, and a value kept elsewhere meanwhile is masked in it."""
    store = _Logged()
    _kept(store, BOUND)
    vault = _person(store)
    engine = _engine(vault)
    session = engine.session("t")
    with vault.holding():
        grown = threading.Thread(
            target=lambda: [vault.intern("PERSON", EntityClass.NER, _name(n), rule="ner") for n in range(BOUND, 55)]
        )
        grown.start()
        grown.join()
        assert not vault.holds_all
        new = _person(store).intern("PERSON", EntityClass.NER, _name(70), rule="ner")  # kept on another instance
        assert vault.refresh()  # as the final check of a call, or a nested one, looks at the store again
        text = f"call {new.original} back"
        assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == f"call {new.surrogate} back"


def test_values_learnt_from_another_instance_past_the_bound_leave_it_too():
    """Values another instance kept, learnt here from its messages (``learn``), carry a vault past its bound as values
    kept here do: it is on its store from then on, its working set cut back."""
    store = _Store()
    _kept(store, BOUND)
    vault = _person(store)
    keeper = StoredVault(ROOT_KEY, store, working=0, on_insert=store.save_entity)
    for n in range(BOUND, BOUND + 5):
        vault.learn(keeper.intern("PERSON", EntityClass.NER, _name(n), rule="ner"))
    assert not vault.holds_all
    with vault.holding():
        pass
    assert vault.count <= WORKING


def test_a_value_forgotten_on_another_instance_leaves_a_vault_held_whole():
    store = _Logged()
    names = _kept(store, 20)
    here, there = _person(store), _person(store)
    gone = names[3]
    there.forget(gone.id)
    store.forget_entity(gone.id)  # as ``Runtime.forget`` forgets it in the store: its rows go, nothing names it
    assert here.in_memory(gone.id) is not None
    reads = store.recent_reads
    with here.holding():  # the next call reads the whole vault again (no change named), once
        pass
    assert here.in_memory(gone.id) is None and here.holds_all and here.count == 19
    assert store.recent_reads == reads + 1
    assert _engine(here).unmask(f"reply to {gone.surrogate}") == (f"reply to {gone.surrogate}", 0)


def test_a_vault_held_whole_that_reads_its_store_whole_again_takes_in_what_it_missed():
    """A catch-up that names nothing (a barrier, a beat's resync of a change never heard) reads the whole vault again
    while it is held whole: a value kept with no telling is taken in, one gone is let go."""
    store = _Logged()
    names = _kept(store, 20)
    vault = _person(store)
    keeper = StoredVault(ROOT_KEY, _Store())
    missed = keeper.intern("PERSON", EntityClass.NER, _name(40), rule="ner")
    with store._lock:  # kept and gone with no telling, as a lost message leaves them
        store.rows[missed.id] = missed
        del store.rows[names[0].id]
    vault.resync()
    assert vault.in_memory(missed.id) is not None and vault.in_memory(names[0].id) is None
    assert vault.holds_all and vault.count == 20


def test_a_whole_read_again_past_the_bound_leaves_it_and_falls_back_to_the_working_set():
    store = _Logged()
    _kept(store, BOUND)
    vault = _person(store)
    keeper = StoredVault(ROOT_KEY, _Store())
    with store._lock:  # more than its bound kept elsewhere, none of it told
        for n in range(BOUND, BOUND + 5):
            entity = keeper.intern("PERSON", EntityClass.NER, _name(n), rule="ner")
            store.rows[entity.id] = entity
    vault.resync()
    assert not vault.holds_all and vault.count <= WORKING


def test_a_vault_held_whole_counts_every_value_it_holds():
    """Its space weighs each value in memory (``spaces._values``: ``count`` and ``chars``): held whole, every one."""
    store = _Store()
    names = _kept(store, 30)
    vault = _person(store)
    assert vault.count == 30 == len(vault.entities())
    assert vault.chars == sum(max(len(e.original), len(e.surrogate)) for e in names)


def test_a_vault_held_whole_reads_no_whole_vault_past_a_bound(monkeypatch):
    """Past a bound of its call a person's vault on its store reads its whole vault for the call (Task 8's round 6):
    held whole, it holds it already, and reads nothing more."""
    from latet.engine import lookup

    store = _Store()
    names = _kept(store, 30)
    vault = _person(store)
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 1)
    read = []
    monkeypatch.setattr(store, "load_entities", lambda: read.append(1) or [])
    text = " ".join(e.original for e in names[:10])
    assert _masked(vault, text) == " ".join(e.surrogate for e in names[:10])
    assert read == [] and vault.whole_count == 0 and _lookups(store) == []


def test_an_api_keys_vault_is_never_held_whole():
    store = _Store()
    _kept(store, 5)
    vault = StoredVault(ROOT_KEY, store, working=WORKING)
    assert not vault.holds_all


def test_the_whole_bound_is_read_at_each_build(monkeypatch):
    """The bound a person's space is built with is ``stored.WHOLE_VALUES`` as it is then (``runtime._person_vault``)."""
    from latet import runtime

    store = _Store()
    _kept(store, 30)
    store.words_complete = lambda: True  # type: ignore[attr-defined]
    store.load_generation = lambda: 0  # type: ignore[attr-defined]

    class _Values:
        keep = staticmethod(store.save_entity)
        learn = staticmethod(lambda entity: None)

    monkeypatch.setattr(stored, "WHOLE_VALUES", 29)
    assert not runtime._person_vault(ROOT_KEY, store, _Values(), WORKING, 0, 1 << 20).holds_all
    monkeypatch.setattr(stored, "WHOLE_VALUES", 30)
    assert runtime._person_vault(ROOT_KEY, store, _Values(), WORKING, 0, 1 << 20).holds_all


# --- fix round 1 on the review (2026-11-01) ---------------------------------------------------------------------------


def _paused_in_words(vault: StoredVault, entity_id: str, thread: str):
    """A pause as ``thread`` starts indexing the words of ``entity_id`` (``_index_words``), the gap where an entry put
    out of memory and back would be found by no word: its hook, the event it sets, and the one that ends it."""
    paused, go = threading.Event(), threading.Event()
    real = StoredVault._index_words

    def slow(self, entity):
        if self is vault and entity.id == entity_id and threading.current_thread().name == thread:
            paused.set()
            assert go.wait(30)
        real(self, entity)

    return slow, paused, go


@pytest.mark.parametrize("how", ["resync", "learn"])
def test_a_value_changed_elsewhere_is_never_out_of_memory_while_it_is_placed_again(how, monkeypatch):
    """I2 of the review: an entry memory holds, changed on another instance (its rule set at its first sighting), placed
    again by a resync or learnt from a message: on another thread a call begun whole masks a text holding its value at
    that very moment. The entry is swapped in place, never put out first: the text is masked."""
    store = _Logged()
    names = _kept(store, 20)
    vault = _person(store)
    target = names[-1]
    engine = _engine(vault)
    engine.mask_text(f"warm {names[-2].original}", SlotKind.TOOL_RESULT, engine.session("w"))  # its detectors made
    changed = dataclasses.replace(target, rule="ner-other")
    with store._lock:
        store.rows[target.id] = changed
    slow, paused, go = _paused_in_words(vault, target.id, "changing")
    monkeypatch.setattr(StoredVault, "_index_words", slow)
    if how == "resync":
        change = threading.Thread(target=lambda: vault.resync(invalidate=False, only=[target.id]), name="changing")
    else:
        change = threading.Thread(target=lambda: vault.learn(changed), name="changing")
    change.start()
    assert paused.wait(10)
    # What a call begun whole reads, by the value's words (``_found_by``): the entry, all along.
    found = set(vault._found_by(stored.words_of(target, "original"), None, LookupBudget()))  # by their ids
    out: dict[str, str] = {}
    text = f"ticket closed by {target.original}"
    reader = threading.Thread(
        target=lambda: out.setdefault("text", engine.mask_text(text, SlotKind.TOOL_RESULT, engine.session("t")).text)
    )
    reader.start()
    reader.join(2.0)  # done meanwhile only if it read memory in the gap; else waiting for the vault's lock
    go.set()
    change.join(10)
    reader.join(10)
    assert target.id in found
    assert out["text"] == f"ticket closed by {target.surrogate}"
    assert vault.in_memory(target.id) == changed and vault.holds_all


def test_two_calls_that_see_one_change_resync_once(monkeypatch):
    """I3 of the review: calls whose refreshes see the same change elsewhere wait for one resync, and those that come
    after it find the change confirmed and read it no more."""
    store = _Logged()
    _kept(store, 20)
    vault = _person(store)
    _person(store).intern("PERSON", EntityClass.NER, _name(20), rule="ner")  # kept on another instance
    resyncs: list[int] = []
    real = StoredVault._resynced_now
    entered, go = threading.Event(), threading.Event()

    def counted(self, *args, **kwargs):
        resyncs.append(threading.get_ident())
        entered.set()
        assert go.wait(10)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(StoredVault, "_resynced_now", counted)
    first = threading.Thread(target=vault.refresh)
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=vault.refresh)
    second.start()
    second.join(0.5)
    go.set()
    first.join(10)
    second.join(10)
    assert len(resyncs) == 1
    with vault.holding():  # and the next call sees nothing new
        pass
    assert len(resyncs) == 1


def test_a_value_learnt_while_a_resync_reads_is_not_put_back_as_it_was():
    """M3 of the review: a resync compares memory with rows it read before its lock; a newer version of an entry learnt
    meanwhile (another instance's message) stays, never replaced by the older one it read."""
    store = _Logged()
    names = _kept(store, 20)
    vault = _person(store)
    target = names[-1]
    older = dataclasses.replace(target, rule="ner-older")
    newer = dataclasses.replace(target, rule="ner-newer")
    with store._lock:
        store.rows[target.id] = older
    reading, go = threading.Event(), threading.Event()
    real = store.by_ids

    def slow(ids):
        found = real(ids)
        reading.set()
        assert go.wait(10)
        return found

    store.by_ids = slow  # type: ignore[method-assign]
    resync = threading.Thread(target=lambda: vault.resync(invalidate=False, only=[target.id]))
    resync.start()
    assert reading.wait(10)
    with store._lock:
        store.rows[target.id] = newer
    vault.learn(newer)  # its message, heard while the resync read
    go.set()
    resync.join(10)
    assert vault.in_memory(target.id) == newer


def test_a_value_learnt_past_its_share_lets_the_whole_memory_go_before_refusing():
    """M2 of the review: held whole, a vault weighs every value; a value learnt from another instance that would carry
    its space past its share, with no call reading memory alone, makes it let go of all but its working set first, and
    is learnt (where 0.10.0's whole vault refused every text until its space was made again)."""
    store = _Store()
    _kept(store, BOUND)
    share = BOUND + 2  # the values its share holds, as its space weighs them

    def learning(entity: Entity) -> None:
        if vault.count + 1 > share:
            raise MaskingError("past its share")

    vault = StoredVault(
        ROOT_KEY, store, working=WORKING, glued_all=True, whole_past_bounds=True, whole_at_most=BOUND + 10,
        on_insert=store.save_entity, on_rule=store.save_rule, on_learn=learning,
    )  # fmt: skip
    keeper = StoredVault(ROOT_KEY, store, working=0, on_insert=store.save_entity)
    first, second, third = (keeper.intern("PERSON", EntityClass.NER, _name(n), rule="ner") for n in range(60, 63))
    vault.learn(first)
    vault.learn(second)
    assert vault.holds_all
    vault.learn(third)  # past its share held whole: on its store, its working set
    assert not vault.holds_all and vault.lacking is None and vault.in_memory(third.id) is not None
    assert vault.count <= WORKING + 1


def test_a_whole_read_again_takes_a_turn_of_the_process_and_is_weighed_while_it_runs(monkeypatch):
    """M1 of the review: a vault held whole that reads its store whole again (a change named by nothing) takes one of
    the process's ``WHOLE_READS_AT_ONCE`` turns, as a whole read past a bound does, and what it reads is weighed with its
    space while it runs (``whole_count``)."""
    store = _Logged()
    _kept(store, 30)
    vault = _person(store)
    turns = threading.BoundedSemaphore(1)
    monkeypatch.setattr(stored, "_WHOLE_READS", turns)
    seen: dict[str, object] = {}
    real = store.load_recent

    def reading(n):
        taken = not turns.acquire(blocking=False)
        if not taken:
            turns.release()
        seen["turn taken"], seen["weighed"] = taken, vault.whole_count
        return real(n)

    store.load_recent = reading  # type: ignore[method-assign]
    vault.resync()
    assert seen == {"turn taken": True, "weighed": 30} and vault.whole_count == 0


def test_a_persons_vault_is_counted_once_at_its_build():
    """M6 of the review: ``runtime._person_vault`` counts the store for its limit, and the vault decides with that
    count whether it is held whole, never counting it again."""
    from latet import runtime

    store = _Store()
    _kept(store, 30)
    store.words_complete = lambda: True  # type: ignore[attr-defined]
    store.load_generation = lambda: 0  # type: ignore[attr-defined]
    counted: list[int] = []
    real = store.count_entities
    store.count_entities = lambda: counted.append(1) or real()  # type: ignore[method-assign]

    class _Values:
        keep = staticmethod(store.save_entity)
        learn = staticmethod(lambda entity: None)

    vault = runtime._person_vault(ROOT_KEY, store, _Values(), WORKING, 0, 1 << 20)
    assert vault.holds_all and counted == [1] and store.count == 30
