"""The vault's values and their fakes, as the masker and the final check find them again wherever they appear: a new
value costs RE2 the compile of a small part of them, never of every value, and RE2 holds the GIL while it compiles (on a
server every identity's event loop waits meanwhile); every value of the vault is matched at every version, a new one at
once and a forgotten one no more."""

import asyncio
import gc
import random
from types import SimpleNamespace

import pytest
import re2

from latet.engine import known as known_module
from latet.engine import vault as vault_module
from latet.engine.masker import Masker
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault

from ..conftest import ROOT_KEY


def _secrets(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return ["sk-" + rng.randbytes(16).hex() for _ in range(count)]


def _vault_of(originals: list[str], seed: int = 99) -> Vault:
    """A vault that knows ``originals`` as secrets, each with a fake of its own (made here: a vault makes them one at a
    time)."""
    vault = Vault(ROOT_KEY)
    fakes = ["fk-" + fake[3:] for fake in _secrets(len(originals), seed)]
    entities = [
        Entity(vault.entity_id("API_KEY", value), "API_KEY", EntityClass.SECRET, value, fake, 0, 0.0)
        for value, fake in zip(originals, fakes, strict=True)
    ]
    return Vault(ROOT_KEY, entities)


def _all_found(masker: Masker, values: list[str]) -> set[str]:
    text = " ".join(values)
    return {text[span.start : span.end] for span in masker.known_in(text)}


class _Ticks:
    """How late the event loop wakes, asked to wake every 5 ms: how long anything held it (RE2's compile in another
    thread holds the GIL). Python's collection of the test run's memory is put off meanwhile: it holds the GIL too, a
    pass over every object the run keeps (hundreds of milliseconds in a whole suite), whatever the masker does."""

    def __init__(self) -> None:
        self.late: list[float] = []
        self._task: asyncio.Task[None] | None = None

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            asked = loop.time()
            await asyncio.sleep(0.005)
            self.late.append(loop.time() - asked - 0.005)

    async def __aenter__(self) -> "_Ticks":
        gc.collect()
        gc.disable()
        self._task = asyncio.create_task(self._tick())
        await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.sleep(0.02)
        assert self._task is not None
        self._task.cancel()
        gc.enable()

    @property
    def worst(self) -> float:
        return max(self.late, default=0.0)


@pytest.mark.timing
async def test_a_new_value_among_100000_secrets_holds_the_event_loop_for_tens_of_milliseconds_at_most():
    old = _secrets(100_000, 1)
    vault = _vault_of(old)
    masker = Masker(vault, [])
    async with _Ticks() as ticks:
        await asyncio.to_thread(masker.known_in, "the first reading")  # every value, compiled a part at a time
    assert ticks.worst < 0.1, f"the first reading held the loop {ticks.worst * 1000:.0f} ms"
    new = _secrets(300, 2)  # past the part new values go to: it is folded into the others at least once

    def kept_and_masked(value: str) -> str:
        vault.intern("API_KEY", EntityClass.SECRET, value)
        return masker.mask(f"the key is {value}", SlotKind.TOOL_RESULT).text

    async with _Ticks() as ticks:
        for value in new:
            assert value not in await asyncio.to_thread(kept_and_masked, value)
            assert ticks.worst < 0.1, f"a new value held the loop {ticks.worst * 1000:.0f} ms"  # 0.9 s, every value
    assert _all_found(masker, [*old, *new]) == {*old, *new}  # every value, old and new, still matched


def test_a_new_value_compiles_a_small_part_of_the_values_and_their_cost_per_value_stays_bounded(monkeypatch):
    vault = _vault_of(_secrets(20_000, 3))
    masker = Masker(vault, [])
    masker.known_in("the first reading")
    compiled: list[int] = []  # the bytes of each pattern RE2 compiles
    real = re2._Regexp

    class Counted(real):
        def __init__(self, pattern, options):
            compiled.append(len(pattern))
            super().__init__(pattern, options)

    monkeypatch.setattr(re2, "_Regexp", Counted)
    added = _secrets(1_000, 4)
    worst = 0
    for value in added:
        vault.intern("API_KEY", EntityClass.SECRET, value)
        before = len(compiled)
        assert masker.known_in(f"the key is {value}")
        worst = max(worst, *compiled[before:])
    assert worst < 128 * 1024  # a part of at most 96 KiB of values; every value's pattern is 760 KiB
    assert sum(compiled) / len(added) < 16 * 1024  # per new value, whatever the vault holds: 1.5 MiB before
    assert _all_found(masker, added) == set(added)


def test_a_value_forgotten_is_matched_no_more_and_one_learnt_from_another_instance_is_matched_at_once():
    values = _secrets(5_000, 5)
    vault = _vault_of(values)
    masker = Masker(vault, [])
    gone, replaced = values[10], values[20]
    assert masker.known_in(f"x {gone} {replaced}")
    forgotten = vault.by_original(gone)
    assert forgotten is not None
    vault.forget(forgotten.id)
    assert masker.known_in(f"x {gone}") == [] and masker.surrogates_in(f"x {forgotten.surrogate}") == []
    before = vault.by_original(replaced)
    assert before is not None
    after = Entity(before.id, before.etype, before.eclass, before.original, "fk-" + "0" * 32, 0, 0.0)
    vault.learn(after)  # another instance gave it another fake
    assert masker.surrogates_in(f"x {before.surrogate}") == []
    assert [s.rule for s in masker.surrogates_in(f"x {after.surrogate}")] == ["echo"]
    assert masker.known_in(f"x {replaced}")
    [taught] = _secrets(1, 51)  # a value another instance kept: learnt here
    vault.learn(
        Entity(vault.entity_id("API_KEY", taught), "API_KEY", EntityClass.SECRET, taught, "fk-" + "1" * 32, 0, 0.0)
    )
    assert masker.known_in(f"x {taught}") and masker.surrogates_in("x fk-" + "1" * 32)
    assert _all_found(masker, values) == set(values) - {gone}


def test_long_values_are_gathered_in_parts_of_a_bounded_size_each_new_one_compiling_one_of_them(monkeypatch):
    # A value of a structure in strict data mode can be a whole JSON string: its fake is matched (echo). Each one in a
    # part of its own would make a scan as many searches as there are such values; each compiled again with every new
    # value would make every new value cost all of them.
    vault = _vault_of(_secrets(1_000, 11))
    masker = Masker(vault, [])
    masker.known_in("the first reading")
    compiled: list[int] = []
    real = re2._Regexp

    class Counted(real):
        def __init__(self, pattern, options):
            compiled.append(len(pattern))
            super().__init__(pattern, options)

    monkeypatch.setattr(re2, "_Regexp", Counted)
    rng = random.Random(12)
    long = [" ".join(rng.randbytes(8).hex() for _ in range(600)) for _ in range(200)]  # 10 KB each
    for value in long:
        vault.intern("NOTE", EntityClass.STRUCT, value)
        compiled.clear()
        masker.surrogates_in("a text")
        assert max(compiled, default=0) < 128 * 1024  # never more than a part's worth at once
    assert len(masker._refresh()[1]._parts) <= 40  # 2 MB of them in parts of 96 KiB at most, not 200 parts
    fakes = [e.surrogate for e in vault.entities() if e.eclass == EntityClass.STRUCT]
    text = "\n".join(fakes)
    assert {text[s.start : s.end] for s in masker.surrogates_in(text)} == set(fakes)


def test_a_reading_that_fails_half_way_leaves_every_value_to_the_next(monkeypatch):
    from latet.engine import known

    vault = _vault_of(_secrets(3_000, 9))
    masker = Masker(vault, [])
    masker.known_in("the first reading")
    added = _secrets(10, 10)
    for value in added:
        vault.intern("API_KEY", EntityClass.SECRET, value)
    placed, place = [], known.ValueMatchers._place

    def fails_at_the_third(self, key, entity):
        placed.append(key)
        if len(placed) == 3:
            raise MemoryError
        return place(self, key, entity)

    monkeypatch.setattr(known.ValueMatchers, "_place", fails_at_the_third)
    with pytest.raises(MemoryError):  # fails closed: the masking it serves stops
        masker.known_in("a reading that runs out of memory")
    monkeypatch.undo()
    assert len(placed) == 3  # it took what the vault changed, and placed two of the ten
    assert _all_found(masker, added) == set(added)  # the next reading reads every value again


def test_every_value_the_vault_knows_is_matched_whatever_it_learnt_and_forgot_between_two_readings():
    rng = random.Random(6)
    values = _secrets(3_000, 7)
    vault = _vault_of(values)
    masker = Masker(vault, [])
    masker.known_in("the first reading")
    fresh = iter(_secrets(5_000, 8))
    for step in range(800):
        action = rng.random()
        known = vault.entities()
        if action < 0.6:
            vault.intern("API_KEY", EntityClass.SECRET, next(fresh))
        elif action < 0.8 and known:
            vault.forget(rng.choice(known).id)
        elif known:
            one = rng.choice(known)
            vault.learn(Entity(one.id, one.etype, one.eclass, one.original, "fk-" + rng.randbytes(16).hex(), 0, 0.0))
        if step % rng.randint(1, 40) == 0:  # read now and then: several changes between two readings
            sample = rng.sample(vault.entities(), min(50, len(vault.entities())))
            assert _all_found(masker, [e.original for e in sample]) == {e.original for e in sample}
    now = vault.entities()
    assert _all_found(masker, [e.original for e in now]) == {e.original for e in now}
    fakes = " ".join(e.surrogate for e in now)
    assert {fakes[s.start : s.end] for s in masker.surrogates_in(fakes)} == {e.surrogate for e in now}


def test_the_parts_count_the_characters_they_hold_whatever_the_vault_learnt_and_forgot(monkeypatch):
    """What the parts' memory grows with (A-14): the characters they hold, each value's or its fake's, the longer, at
    every reading, whatever the vault learnt and forgot between two (more than it keeps track of now and then: every
    value read again), a long value (a whole JSON string of strict data mode's, longer than a part too) counted whole.
    The vault counts the same as each value comes and goes (``Vault.chars``), with no reading: what a server weighs a
    value's length by as it is kept (``spaces.space_size``, RP1), where the parts' count waited for their next reading."""
    monkeypatch.setattr(vault_module, "_TOUCHED_KEPT", 4)
    rng = random.Random(9)
    vault = _vault_of(_secrets(300, 10))
    masker = Masker(vault, [])
    fresh = iter(_secrets(2_000, 11))

    def held() -> int:
        return sum(max(len(e.original), len(e.surrogate)) for e in vault.entities())

    assert vault.chars == held() > 0  # before any reading
    masker.known_in("the first reading")
    assert vault.matchers.chars == held() > 0
    for step in range(200):
        action, known = rng.random(), vault.entities()
        if action < 0.5:
            vault.intern("API_KEY", EntityClass.SECRET, next(fresh))
        elif action < 0.56:
            length = rng.choice((9_000, 20_000, 100_000))
            vault.intern("CUSTOM", EntityClass.STRUCT, "".join(rng.choice("abcdef") for _ in range(length)))
        elif action < 0.8 and known:
            vault.forget(rng.choice(known).id)
        elif known:
            one = rng.choice(known)
            vault.learn(Entity(one.id, one.etype, one.eclass, one.original, "fk-" + rng.randbytes(40).hex(), 0, 0.0))
        assert vault.chars == held()  # at once, read or not
        if step % 7 == 0:
            masker.known_in("a reading")
            assert vault.matchers.chars == held()
    masker.known_in("a last reading")
    assert vault.matchers.chars == held() == vault.chars


def test_a_reading_of_many_parts_pauses_after_each_compile(monkeypatch):
    """RE2 holds the GIL for the whole of a compile, tens of milliseconds for a full part: a reading that compiles
    several parts in a row (a space's first reading, or every value read again once more changed than are kept track
    of, as 17,000 new values in one request) pauses after each compile, so that the event loop takes its turn between
    two (it waited for several in a row, up to 0.17 s at 100,000 secrets); a reading of a new value, which compiles the
    fresh part alone, pauses for nothing, nor does a reading that compiles nothing."""
    compiled: list[int] = []
    paused: list[tuple[int, float]] = []
    detector = known_module.KnownValuesDetector

    def counted(*args, **kwargs):
        compiled.append(1)
        return detector(*args, **kwargs)

    def pause(seconds: float) -> None:
        paused.append((len(compiled), seconds))

    monkeypatch.setattr(known_module, "KnownValuesDetector", counted)
    monkeypatch.setattr(known_module, "time", SimpleNamespace(sleep=pause), raising=False)
    vault = _vault_of(_secrets(5_000, 12))  # three parts: two of 2,000 values and one of 1,000
    vault.matchers.current()
    assert paused == [(n, known_module.LET_GO) for n in range(1, 7)]  # after each of the six compiles
    assert 0 < known_module.LET_GO <= 0.002  # a pause of no time gives the loop no turn on Windows
    vault.intern("API_KEY", EntityClass.SECRET, "sk-" + "5" * 32)
    vault.matchers.current()
    assert len(compiled) == 8 and len(paused) == 6  # the fresh part's two compiles: no pause
    vault.matchers.current()
    assert len(compiled) == 8 and len(paused) == 6  # nothing changed: nothing compiled
