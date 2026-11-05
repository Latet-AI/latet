"""The RE2 budget of an API key's parts (Task 8's fix round 8, the review of round 7 and the held-edges proof).

A key's vault reads every text for the values its detectors hold whatever the text (``StoredVault.matched``): the
secrets of one run held glued, those glued on both sides while in memory. A part of them is one alternation of many
terms that start every way, and its automaton needs about 520 to 730 bytes for each byte of its pattern. Under the
ordinary budget (``BOUNDED_PER_BYTE``, at least ``BOUNDED_SMALLEST``) a part of some 120 such secrets or more ran out
of it: RE2 said "DFA out of memory" and read each text about ten times slower, never with a term less. A key's parts
are given ``SCALED_PER_BYTE`` a byte of their pattern (at most ``RE2_MEMORY``), and what that adds is weighed with its
space (``StoredVault.detectors_bytes``). Every other vault's parts keep the ordinary budget: a vault that holds every
value in memory (a person's on 0.10.0's ``Vault``, the computer's daemon) would otherwise take some 40 KB a value.

The checks read the budget each alternation was given and what RE2 says as it scans, never a time."""

import random

import re2

from latet.engine.detectors import terms
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import BOUNDED_PER_BYTE, BOUNDED_SMALLEST, RE2_MEMORY
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_stored_vault import _Store

_SECRETS = 199  # the one-run secrets the serve of Task 8's benchmark taught a key (16 hex digits each)


def _secrets(source: Vault, count: int = _SECRETS) -> list:
    rng = random.Random(8)
    return [source.intern("GENERIC_API_KEY", EntityClass.SECRET, f"{rng.getrandbits(64):016x}") for _ in range(count)]


def _key_vault(count: int = _SECRETS, **ceiling: int) -> StoredVault:
    source = Vault(ROOT_KEY)
    store = _Store()
    for entity in _secrets(source, count):
        store.save_entity(entity)
    vault = StoredVault(ROOT_KEY, store, working=max(1000, count), held_at_most=1000, **ceiling)
    assert len(vault._matched) == count  # every one read in every text
    return vault


def _lines() -> list[str]:
    """Log lines with hex ids that are none of the secrets, and with prefixes of hex runs: what a key's texts hold."""
    rng = random.Random(9)
    return [
        f"2026-10-24T09:{n % 60:02d}:00Z INFO orders request_id={rng.getrandbits(64):016x} "
        f"session={rng.getrandbits(48):012x} user=user{n}@example.it took={rng.randint(3, 900)}ms "
        f"trace={rng.getrandbits(64):016x}{rng.getrandbits(16):x}"
        for n in range(600)
    ]


def _alternations(detector) -> list:
    return [m for m in detector._set._matchers if isinstance(m, terms._Alternation)]


def _pattern_bytes(values) -> int:
    return len("|".join(re2.escape(v) for v in sorted(set(values))).encode())


def test_a_keys_parts_are_given_a_budget_of_their_patterns_bytes():
    vault = _key_vault()
    vault.matchers.current()
    matchers = vault._texts._parts
    parts = [part for part in (*matchers._full, matchers._fresh, matchers._long) if part.members]
    assert parts
    for part in parts:
        known, echo, _ = part.made
        values = [e.original for e in part.members.values()]
        fakes = [e.surrogate for e in part.members.values()]
        for detector, strings in ((known, values), (echo, fakes)):
            (alternation,) = _alternations(detector)
            wanted = min(RE2_MEMORY, max(BOUNDED_SMALLEST, terms.SCALED_PER_BYTE * _pattern_bytes(strings)))
            assert alternation.memory >= wanted
            assert alternation.memory >= 1024 * _pattern_bytes(strings) or alternation.memory == RE2_MEMORY


def test_a_keys_part_of_199_secrets_reads_its_texts_without_running_out_of_memory(monkeypatch, capfd):
    """RE2 says when its automaton ran out of its budget and it reads on more slowly ("DFA out of memory"), where it
    may write it (``log_errors``, which the engine keeps off): a key holding the 199 secrets reads none so."""
    quiet = terms.quiet_options

    def telling(**settings):
        options = quiet(**settings)
        options.log_errors = True
        return options

    monkeypatch.setattr(terms, "quiet_options", telling)
    vault = _key_vault()
    held = vault.matchers.current()
    capfd.readouterr()
    for line in _lines():
        list(held.known.detect(line, DetectContext()))
        list(held.echo.detect(line, DetectContext()))
    assert "DFA out of memory" not in capfd.readouterr().err


def test_what_a_keys_parts_take_more_is_weighed():
    vault = _key_vault()
    assert vault.detectors_bytes == 0  # none compiled yet
    vault.matchers.current()
    extra = vault.detectors_bytes
    assert extra > 0
    parts = vault._texts._parts
    ordinary = 0
    given = 0
    for part in [*parts._full, parts._fresh, parts._long]:
        if part.made is None or not part.members:
            continue
        for detector in part.made:
            for alternation in _alternations(detector):
                given += alternation.memory
                ordinary += min(RE2_MEMORY, max(BOUNDED_SMALLEST, BOUNDED_PER_BYTE * alternation.pattern_bytes))
    assert extra == given - ordinary
    empty = StoredVault(ROOT_KEY, _Store(), working=1000, held_at_most=1000)
    empty.matchers.current()
    assert empty.detectors_bytes == 0


def test_a_vault_that_holds_every_value_keeps_the_ordinary_budget():
    """A person's vault on 0.10.0's ``Vault`` and the computer's daemon hold every value in their parts: theirs keep the
    budget they had, which their weight a value (``spaces._PER_VALUE``) was measured with."""
    source = Vault(ROOT_KEY)
    secrets = _secrets(source)
    source.matchers.current()
    for part in [*source.matchers._full, source.matchers._fresh]:
        if part.made is None or not part.members:
            continue
        known = part.made[0]
        values = [e.original for e in part.members.values()]
        (alternation,) = _alternations(known)
        assert alternation.memory == min(RE2_MEMORY, max(BOUNDED_SMALLEST, BOUNDED_PER_BYTE * _pattern_bytes(values)))
    assert len(secrets) == _SECRETS


def _ordinary(alternation) -> int:
    return min(RE2_MEMORY, max(BOUNDED_SMALLEST, BOUNDED_PER_BYTE * alternation.pattern_bytes))


def _given(vault) -> list:
    """Every alternation of a key's parts, made."""
    parts = vault._texts._parts
    return [
        alternation
        for part in (*parts._full, parts._fresh, parts._long)
        if part.made is not None and part.members
        for detector in part.made
        for alternation in _alternations(detector)
    ]


def test_a_keys_parts_take_no_more_than_the_ceiling_past_the_ordinary_budget():
    """Fix round 9 (the review of round 8's I-1, the owner's ruling: never a refusal for size where the release before
    masked): what a key's parts take past the ordinary budget has a ceiling for the vault (``detectors_extra_most``, a
    server's an eighth of a space's share). A part whose budget would carry them past it is compiled at the ordinary
    budget, slower and exact, and only what is granted is weighed. 5,000 one-run secrets would take some 150 MiB more."""
    ceiling = 16 << 20
    vault = _key_vault(5000, detectors_extra_most=ceiling)
    vault.matchers.current()
    given = _given(vault)
    granted = [a for a in given if a.memory > _ordinary(a)]
    ordinary = [a for a in given if a.memory == _ordinary(a)]
    assert granted and ordinary  # some within the ceiling, the rest at the ordinary budget
    assert vault.detectors_bytes == sum(a.memory - _ordinary(a) for a in granted) <= ceiling
    rng = random.Random(10)
    for _ in range(300):  # parts made again as values come: what they held is given back, the ceiling holds
        vault.intern("GENERIC_API_KEY", EntityClass.SECRET, f"{rng.getrandbits(64):016x}")
    vault.matchers.current()
    assert vault.detectors_bytes <= ceiling  # (parts laid afresh may each want more than the ceiling: none granted)
    assert vault.detectors_bytes == sum(a.memory - _ordinary(a) for a in _given(vault) if a.memory > _ordinary(a))


def test_a_part_made_again_gives_its_grant_back_before_it_asks_again():
    """Fix round 9's guarantee, carried to Task 13 by name (the final review's M-8): a part that changes is made again
    within the room the other parts leave, its own grant given back first. Under a ceiling of exactly what the vault's
    parts were granted, a value forgotten makes its part again, and the part is granted again what it asks: had it kept
    its grant while it asked, it would find no room left, and every alternation of it would keep the ordinary budget."""
    source = Vault(ROOT_KEY)
    secrets = _secrets(source)

    def vault_of(ceiling: int) -> StoredVault:
        store = _Store()
        for entity in secrets:
            store.save_entity(entity)
        return StoredVault(ROOT_KEY, store, working=1000, held_at_most=1000, detectors_extra_most=ceiling)

    probe = vault_of(1 << 30)
    probe.matchers.current()
    granted = probe.detectors_bytes
    assert granted > 0
    vault = vault_of(granted)  # room for what its parts take now, and no more
    vault.matchers.current()
    assert vault.detectors_bytes == granted and all(a.memory > _ordinary(a) for a in _given(vault))
    assert vault.forget(secrets[0].id) is not None
    vault.matchers.current()
    given = _given(vault)
    assert given and all(a.memory > _ordinary(a) for a in given)  # the part made again granted, as the others are
    assert 0 < vault.detectors_bytes == sum(a.memory - _ordinary(a) for a in given) <= granted


def test_a_ceiling_of_nothing_leaves_a_keys_parts_at_the_ordinary_budget():
    vault = _key_vault(detectors_extra_most=0)
    vault.matchers.current()
    assert vault.detectors_bytes == 0
    assert all(a.memory == _ordinary(a) for a in _given(vault))


def test_the_default_ceiling_is_an_eighth_of_the_share_of_a_servers_default_budget():
    from latet.engine import stored
    from latet.server import spaces

    assert int(2048 * 1024 * 1024 * spaces.SPACE_SHARE * spaces.DETECTORS_SHARE) == stored.DETECTORS_EXTRA_MOST
