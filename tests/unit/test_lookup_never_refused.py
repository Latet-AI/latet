"""A request is never refused for its size where the previous release masked it (the owner's ruling of 2026-10-29, Task
8's fix round 6; the Task 9 review's C1 and its LookupLimitExceeded question). A stored vault's lookup has bounds, and
past them it reads as the release before it read, slower, never refusing:

- the interior windows (``anchors.words_and_windows``): past ``INTERIOR_WINDOWS`` positions the remaining runs have no
  windows, their words asked all the same; windows past their share of ``LOOKUP_WORD_ROLES``, or a lookup of windows
  past the candidates, characters or bytes, are dropped whole. 0.10.x had no windows: a text read without them is read
  as it read then, and a text's readings are read in a fixed order, so that every process cuts at the same run;
- an API key's call past the words, candidates, characters or bytes reads its words as 0.10.2 did
  (``KeyVaultStore.with_words``: the whole index past ``WORDS_ASKED_AT_MOST`` words), whose ``TooManyCandidates`` (422)
  stays where it was;
- a person's call past any of them reads the whole vault (``whole_past_bounds``), as 0.10.0 held it.

No ``LookupLimitExceeded`` leaves the stored vault."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from latet.engine import anchors, lookup
from latet.engine.anchors import EDGE, words_in
from latet.engine.lookup import LookupBudget
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass, Policy
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_interior_lookup import DATA, MINIMAL, TOKEN, masker, positions, round_trips
from .test_stored_vault import _Store

ROOT = Path(__file__).resolve().parents[2]
BIANCHI = "Laura.Bianchi@nordest.it"


class _Spied(_Store):
    """A key's store that notes the words each lookup asks for (``asked``) and how often its whole vault is read."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[set[str]] = []
        self.wholes = 0

    def with_words(self, role, words):
        words = set(words)
        self.asked.append(words)
        return super().with_words(role, words)

    def load_entities(self):
        self.wholes += 1
        return super().load_entities()


def _kept(store, *entities):
    for entity in entities:
        assert store.save_entity(entity) is None


def _values():
    """A vault of a few values: an address, a token, a name in another script; and its plain copy, 0.10.0's."""
    source = Vault(ROOT_KEY)
    kept = [
        source.intern("EMAIL", EntityClass.PII, BIANCHI),
        source.intern("API_KEY", EntityClass.SECRET, TOKEN),
        source.intern("EMAIL", EntityClass.PII, "renata.colombo@ferrovie-test.example"),
    ]
    return source, kept


def _vault(store, *, person=False, working=0):
    whole = {"glued_all": True, "whole_past_bounds": True} if person else {}  # a person's space
    return StoredVault(ROOT_KEY, store, working=working, held_at_most=0, **whole)


def _masked(vault, texts, *, scoped=False):
    reader = masker(vault, Policy.MASK)
    with vault.holding(scoped=scoped):
        vault.prefetch(texts)
        return [reader.mask(text, DATA) for text in texts]


# --- the windows ------------------------------------------------------------------------------------------------------


_ORDER = """
import json, sys
from latet.engine import lookup
from latet.engine.anchors import words_and_windows
from latet.engine.lookup import LookupBudget
lookup.INTERIOR_WINDOWS = int(sys.argv[1])
budget = LookupBudget()
plain, windows = words_and_windows(sys.argv[2], dotted=True, interior=budget.interior)
print(json.dumps([sorted(plain), sorted(windows), budget.windows]))
"""


def test_a_texts_readings_are_read_in_one_order_whatever_the_process():
    """The readings of a text (as written, its escapes blanked once and twice, read as letters, as the masker reads
    them) are read in a fixed order, so that past the windows' bound every process (two instances, two workers) cuts at
    the same run: a set of readings is iterated in an order that changes with ``PYTHONHASHSEED``."""
    text = "abcdefghijklmnop\\nqrstuvwxyz0123%41bcdefghij&amp;klmnopqrstuv\\\\nwxyzabcdefgh"
    readings = {
        reading
        for seed in ("0", "1", "2", "3", "4", "5")
        for reading in [
            subprocess.run(
                [sys.executable, "-c", _ORDER, "25", text],
                capture_output=True,
                text=True,
                check=True,
                cwd=ROOT,
                env={**os.environ, "PYTHONHASHSEED": seed},
            ).stdout
        ]
    }
    assert len(readings) == 1, readings
    plain, windows, counted = json.loads(readings.pop())
    assert plain and windows and counted <= 25


def test_past_the_windows_bound_the_remaining_runs_have_no_windows_and_their_words_are_asked(monkeypatch):
    monkeypatch.setattr(lookup, "INTERIOR_WINDOWS", 2)
    budget = LookupBudget()
    plain, windows = anchors.words_and_windows("aaaaaaaaa bbbbbbbbbbbb cccccccccc", interior=budget.interior)
    assert {"aaaaaaaaa", "bbbbbbbbbbbb", "bbbbbb", "cccccccccc", "cccccc"} <= plain
    assert windows == {"aaaaaa"} - plain  # the first run's two positions, one word (its edge already)
    assert budget.windows == 2 and budget.windows_off
    # And every later text of the call: no window, whatever its size.
    plain, windows = anchors.words_and_windows("dddddddd", interior=budget.interior)
    assert windows == set() and "dddddddd" in plain
    # ``words_in`` gives both, as before.
    assert words_in("eeeeeeeeee", interior=LookupBudget().interior) == anchors.words_and_windows(
        "eeeeeeeeee", interior=None
    )[0] | {"e" * EDGE}


def test_a_call_past_the_windows_bound_masks_its_texts_by_their_words(monkeypatch):
    """No refusal: the call past the windows' bound masks the address of its texts by its words, as 0.10.x did."""
    monkeypatch.setattr(lookup, "INTERIOR_WINDOWS", 1)
    source, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    texts = [f"write to {BIANCHI} about {'x' * 40}", f"and {'y' * 30} to {BIANCHI}"]
    done = _masked(_vault(store), texts)
    expected = [masker(source, Policy.MASK).mask(text, DATA) for text in texts]
    assert [d.text for d in done] == [e.text for e in expected]
    assert all(BIANCHI not in d.text for d in done)


def test_windows_past_their_share_of_the_word_roles_are_dropped_and_the_words_asked(monkeypatch):
    _source, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    text = f"write to {BIANCHI}, {MINIMAL}"
    plain, windows = anchors.words_and_windows(text, dotted=True, interior=LookupBudget().interior)
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", len(plain))  # the plain words fit, the windows do not
    [done] = _masked(_vault(store), [text])
    assert any(plain <= asked for asked in store.asked)
    assert not any(asked & windows for asked in store.asked)
    assert BIANCHI not in done.text  # found by its word


@pytest.mark.parametrize("ceiling", ["LOOKUP_CANDIDATES", "LOOKUP_CHARACTERS"])
def test_a_lookup_of_windows_past_its_candidates_or_characters_is_dropped_whole(monkeypatch, ceiling):
    """The windows find the token glued inside the address (``MINIMAL``); past the bound their lookup is dropped
    whole, and the call masks as its words alone mask it: never less."""
    _source, kept = _values()
    alone, dropped = _Spied(), _Spied()
    _kept(alone, *kept)
    _kept(dropped, *kept)
    text = f"write to {BIANCHI} and {MINIMAL}"
    with monkeypatch.context() as limited:
        limited.setattr(lookup, "INTERIOR_WINDOWS", 0)  # words alone
        [words_alone] = _masked(_vault(alone), [text])
    bianchi = next(e for e in kept if e.original == BIANCHI)
    room = (
        1
        if ceiling == "LOOKUP_CANDIDATES"
        else sum(len(s) for s in (bianchi.id, bianchi.etype, bianchi.original, bianchi.surrogate, bianchi.rule or ""))
    )
    monkeypatch.setattr(lookup, ceiling, room)
    [done] = _masked(_vault(dropped), [text])
    assert positions(done) >= positions(words_alone)
    assert BIANCHI not in done.text


def test_whatever_the_windows_bring_in_no_text_is_masked_less_than_by_words_alone(monkeypatch):
    """The Community closure's oracle, over a key's vault and a person's: under every bound the windows may meet, no
    position masked by the words alone is left in clear."""
    from scripts.vault_fuzz import fuzz_texts

    texts = fuzz_texts(120)
    source = Vault(ROOT_KEY)
    reader = masker(source, Policy.MASK)
    for text in texts:
        reader.mask(text, DATA)
    for person in (False, True):
        alone = _Store()
        _kept(alone, *source._by_id.values())
        with monkeypatch.context() as limited:
            limited.setattr(lookup, "INTERIOR_WINDOWS", 0)
            expected = [positions(d) for d in _masked(_vault(alone, person=person), texts)]
        for windows, words in ((40, lookup.LOOKUP_WORD_ROLES), (lookup.INTERIOR_WINDOWS, 300), (200, 1000)):
            store = _Store()
            _kept(store, *source._by_id.values())
            with monkeypatch.context() as limited:
                limited.setattr(lookup, "INTERIOR_WINDOWS", windows)
                limited.setattr(lookup, "LOOKUP_WORD_ROLES", words)
                found = [positions(d) for d in _masked(_vault(store, person=person), texts)]
            for text, now, before in zip(texts, found, expected, strict=True):
                assert now >= before, (person, windows, words, text)


# --- an API key: 0.10.2's words -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", ["LOOKUP_WORD_ROLES", "LOOKUP_CANDIDATES", "LOOKUP_CHARACTERS"])
@pytest.mark.parametrize("scoped", [False, True], ids=["gateway", "apps"])
def test_a_keys_call_past_a_bound_reads_its_words_as_0_10_2_did(monkeypatch, ceiling, scoped):
    source, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    texts = [f"write to {BIANCHI}", "renata.colombo@ferrovie-test.example called, about the tickets", f"x{TOKEN}"]
    monkeypatch.setattr(lookup, ceiling, 1)
    vault = _vault(store)
    done = _masked(vault, texts, scoped=scoped)
    expected = [masker(source, Policy.MASK).mask(text, DATA).text for text in texts]
    assert [d.text for d in done] == expected
    assert store.wholes == 0  # a key's whole vault is never read: its words are
    for text, masked in zip(texts, done, strict=True):
        round_trips(vault, text, masked.text)


def test_a_keys_restore_past_a_bound_still_restores(monkeypatch):
    _source, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    vault = _vault(store)
    [done] = _masked(vault, [f"write to {BIANCHI}"])
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    round_trips(vault, f"write to {BIANCHI}", done.text)
    with vault.holding():
        vault.prefetch_fakes([done.text, done.text + " again"])
        assert {e.id for e in vault.candidates_in(done.text)} >= {kept[0].id}


# --- a person: the whole vault ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", ["LOOKUP_WORD_ROLES", "LOOKUP_CANDIDATES", "LOOKUP_CHARACTERS"])
def test_a_persons_call_past_a_bound_reads_the_whole_vault_and_masks_as_0_10_0(monkeypatch, ceiling):
    source, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    texts = [f"write to {BIANCHI}", "renata.colombo@ferrovie-test.example called", f"x{TOKEN}y", MINIMAL]
    expected = [masker(source, Policy.MASK).mask(text, DATA).text for text in texts]
    monkeypatch.setattr(lookup, ceiling, 1)
    vault = _vault(store, person=True)
    for _turn in range(3):  # every later turn of the conversation too: the whole history again
        done = _masked(vault, texts)
        assert [d.text for d in done] == expected
    assert store.wholes >= 1
    for text, masked in zip(texts, done, strict=True):
        round_trips(vault, text, masked.text)


def test_a_persons_call_within_its_bounds_never_reads_the_whole_vault():
    _, kept = _values()
    store = _Spied()
    _kept(store, *kept)
    _masked(_vault(store, person=True), [f"write to {BIANCHI}"])
    assert store.wholes == 0


def test_a_persons_call_past_a_bound_keeps_no_copy_of_the_whole_read(monkeypatch):
    """The review of the merge of Task 9 (I1): a person's call past a bound reads the snapshot of its whole vault by
    reference, and its texts are weighed by the snapshot's own detectors. What the call's words find in it is not copied
    into the call's candidates, nor weighed by a detector of the call's own, which the space never weighed (some 100 MB
    a call at 200,000 values), nor worked out through the words memo, which would let go of the entries the calls find
    again. What it masks and restores is 0.10.0's: one text naming most of a vault of 400 addresses."""
    source = Vault(ROOT_KEY)
    values = [source.intern("EMAIL", EntityClass.PII, f"person{n}@sharedcorp.example") for n in range(400)]
    known = source.intern("EMAIL", EntityClass.PII, BIANCHI)
    store = _Spied()
    _kept(store, known, *values)
    vault = _vault(store, person=True, working=20)
    _masked(vault, [f"write to {BIANCHI}"])  # within the bounds: its entry in the memo
    memo = set(vault._words_memo)
    assert known.id in memo
    text = "Copy to " + ", ".join(value.original for value in values[:300]) + "."
    expected = masker(source, Policy.MASK).mask(text, DATA).text
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 50)  # the domain's word names all 400: past the bound
    with vault.holding():
        vault.prefetch([text])
        call = vault._call()
        assert call.lookup.past and call.whole is not None and len(call.whole) == len(values) + 1
        assert call.candidates == {} and call.weighing is None
        done = masker(vault, Policy.MASK).mask(text, DATA)
        assert call.candidates == {} and call.weighing is None
    assert done.text == expected
    assert set(vault._words_memo) == memo
    round_trips(vault, text, done.text)


def test_a_window_left_unanswered_past_a_bound_is_asked_as_a_later_texts_word(monkeypatch):
    """A key's call past a bound reads its words as 0.10.2 did and leaves its windows unanswered: a later text whose
    word one of them is asks for it, and finds its value (a window taken as asked would leave it in clear)."""
    source = Vault(ROOT_KEY)
    store = _Spied()
    _kept(store, source.intern("PERSON", EntityClass.NER, "Bcdefg"))
    vault = _vault(store)
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 1)
    with vault.holding():
        vault.prefetch(["abcdefgh"])  # past the word roles: its window `bcdefg` is no word 0.10.2 read
        assert vault._call().lookup.past
        done = masker(vault, Policy.MASK).mask("write to Bcdefg", DATA)
    assert "Bcdefg" not in done.text
    assert any("bcdefg" in asked for asked in store.asked)
