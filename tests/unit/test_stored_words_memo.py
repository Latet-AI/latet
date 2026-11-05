"""The words of the entries a call's lookups find are worked out once and kept (``StoredVault._both_words``, Task 8's
fix round 7): at a million values a call's words find some 1,500 entries, most of them again in the next call, and each
call worked out every one's words again (``stored._words_of_both``, 11 ms a call). ``_words_of_both`` is a pure function
of an entry's value, fake, type and class; the memo is keyed by those and the id, so that an entry changed in any of
them is worked out anew, never answered from an older one; it holds about memory's entries at most
(``WORDS_MEMO_SHARE``) and is weighed with the space (``words_bytes``)."""

import sys
from dataclasses import replace

import pytest

from latet.engine import lookup, stored
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_stored_vault import _Store

LINES = [
    "write to laura.bianchi@nordest.it and ugo.neri@acme.it about the ticket",
    "renata.colombo@ferrovie-test.example called; token Kq7mZ2xW9pL4vR8tN3bY rotated",
    "laura.bianchi@nordest.it again, with marco.bianchi@nordest.it in copy",
]


def _values():
    source = Vault(ROOT_KEY)
    for etype, eclass, value in (
        ("EMAIL", EntityClass.PII, "laura.bianchi@nordest.it"),
        ("EMAIL", EntityClass.PII, "marco.bianchi@nordest.it"),
        ("EMAIL", EntityClass.PII, "ugo.neri@acme.it"),
        ("EMAIL", EntityClass.PII, "renata.colombo@ferrovie-test.example"),
        ("API_KEY", EntityClass.SECRET, "Kq7mZ2xW9pL4vR8tN3bY"),
    ):
        source.intern(etype, eclass, value)
    store = _Store()
    for entity in source._by_id.values():
        store.save_entity(entity)
    return source, store


def _read(vault, texts):
    out = []
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        for text in texts:
            reading = vault.matchers.for_text(text)
            out.append(
                (
                    sorted((s.start, s.end, s.etype) for s in reading.known.detect(text, stored.DetectContext())),
                    sorted((s.start, s.end, s.etype) for s in reading.echo.detect(text, stored.DetectContext())),
                )
            )
            reading.seen()
    return out


def test_a_calls_candidates_words_are_worked_out_once_across_calls(monkeypatch):
    _, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    worked = []
    real = stored._words_of_both
    monkeypatch.setattr(stored, "_words_of_both", lambda entity: worked.append(entity.id) or real(entity))
    _read(vault, LINES)
    first = len(worked)
    assert first > 0
    _read(vault, LINES)  # the next call: the same entries found, none worked out again
    assert len(worked) == first
    assert vault._words_memo  # a memo there is: memory's entries (``working``) at most


def test_what_a_call_reads_is_the_same_with_and_without_the_memo(monkeypatch):
    _, store = _values()
    with_memo = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    expected = [_read(with_memo, LINES), _read(with_memo, LINES[::-1])]
    monkeypatch.setattr(stored, "WORDS_MEMO_SHARE", 0)  # no memo
    without = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    assert [_read(without, LINES), _read(without, LINES[::-1])] == expected
    assert with_memo._words_memo and not without._words_memo


@pytest.mark.parametrize(
    "change",
    [
        {"surrogate": "g.ferri@posta-test.example"},
        {"original": "laura.bianchi@nordest-test.it"},
        {"etype": "PERSON"},
        {"eclass": EntityClass.SECRET},
        {"rule": "email"},
    ],
)
def test_an_entry_changed_is_never_answered_with_its_older_words(change):
    source, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    entity = next(e for e in source._by_id.values() if e.original.startswith("laura"))
    assert vault._both_words(entity) == stored._words_of_both(entity)
    assert entity.id in vault._words_memo
    changed = replace(entity, **change)
    assert vault._both_words(changed) == stored._words_of_both(changed)
    assert vault._both_words(entity) == stored._words_of_both(entity)


def test_the_memo_holds_about_memorys_entries_and_is_weighed(monkeypatch):
    monkeypatch.setattr(stored, "WORDS_MEMO_SHARE", 2)
    source = Vault(ROOT_KEY)
    entities = [source.intern("EMAIL", EntityClass.PII, f"user.number{n}@company{n}.example") for n in range(40)]
    vault = StoredVault(ROOT_KEY, _Store(), working=5, held_at_most=3)
    assert vault.words_bytes == 0
    kept = 2 * (5 + 3)
    for entity in entities:
        vault._both_words(entity)
        assert len(vault._words_memo) <= kept
    assert len(vault._words_memo) == kept
    least = sum(sys.getsizeof(word) for entity in entities[-kept:] for word in stored._words_of_both(entity))
    assert vault.words_bytes >= least
    held = vault.words_bytes
    vault._both_words(entities[-1])  # a hit: nothing more held
    assert vault.words_bytes == held


def test_a_persons_whole_vault_is_not_read_through_the_memo(monkeypatch):
    """Past a bound a person's call reads the whole vault (``_whole``): its entries are not kept in the memo, which
    would let go of the entries the calls find again for those of one call. Through ``prefetch``, as a call reads it
    (the review of the merge of Task 9, M1): what its words find in the snapshot reaches no memo either."""
    _, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0, glued_all=True, whole_past_bounds=True)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    with vault.holding():
        vault.prefetch(LINES)
        call = vault._call()
        assert call.lookup.past and call.whole is not None
    assert not vault._words_memo


def test_a_value_forgotten_leaves_the_memo_with_memory():
    source, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(vault, LINES)
    gone = [e.id for e in source._by_id.values() if e.id in vault._words_memo][:2]
    assert len(gone) == 2
    vault.forget(gone[0])
    vault.forgotten([gone[1]])
    assert not set(gone) & set(vault._words_memo)
    assert vault.words_bytes == sum(weight for _, _, weight in vault._words_memo.values())


def test_a_value_forgotten_is_not_kept_again_by_a_call_that_found_it_before():
    """A call that found an entry before it was forgotten still holds it among its candidates, and works out its words
    again (``_candidates``): they are not kept in the memo again, which outlives the call. A secret's words are most of
    it (its head and its tail)."""
    source, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(vault, LINES)
    entity = next(e for e in source._by_id.values() if e.id in vault._words_memo)
    vault.forget(entity.id)
    assert vault._both_words(entity) == stored._words_of_both(entity)  # the call's words, worked out as before
    assert entity.id not in vault._words_memo
    assert vault.words_bytes == sum(weight for _, _, weight in vault._words_memo.values())


def _strings(memo) -> str:
    """Everything the memo holds, written out: what a reader of the process's memory would find in it."""
    return repr(list(memo.items()))


def test_the_memo_holds_no_value_or_fake_but_a_keyed_digest_of_them():
    """Fix round 8 (the review of round 7's I-1): an entry is kept with a digest of its value, fake, type and class,
    keyed by the vault (one vault's digest of an entry is not another's), never with the strings themselves."""
    source, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(vault, LINES)
    assert vault._words_memo
    assert all(isinstance(made_of, bytes) and len(made_of) == 16 for made_of, _, _ in vault._words_memo.values())
    # The strings it holds are its ids and the entries' words: a value or a fake is among them only as a word of its
    # own (a domain, an IPv4 address, a run in lower case: its word is the whole string).
    strings = set(vault._words_memo).union(*(words for _, words, _ in vault._words_memo.values()))
    for entity in source._by_id.values():
        for string in (entity.original, entity.surrogate):
            assert string not in strings or string in stored._words_of_both(entity)
    written = _strings(vault._words_memo)  # an address is no word of its own: nowhere in the memo, in any form
    assert "laura.bianchi@nordest.it" not in written and "Kq7mZ2xW9pL4vR8tN3bY" not in written
    other = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(other, LINES)
    shared = set(vault._words_memo) & set(other._words_memo)
    assert shared and all(vault._words_memo[i][0] != other._words_memo[i][0] for i in shared)


def test_a_whole_resync_empties_the_memo():
    """A whole resync (``only`` None: every change the store cannot name, a forget elsewhere whose message was lost
    among them) empties the memo, whose words are fragments of the values: it is made again as calls find entries."""
    _, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(vault, LINES)
    assert vault._words_memo and vault.words_bytes > 0
    vault.resync(only=[])  # a resync by name that names nothing: the memo stays
    assert vault._words_memo
    vault.resync()
    assert not vault._words_memo and vault.words_bytes == 0
    _read(vault, LINES)
    assert vault._words_memo


def test_a_call_begun_before_a_whole_resync_keeps_no_word_after_it():
    """A call whose lookups began before a whole resync may hold an entry forgotten elsewhere meanwhile: the words it
    works out are its own, never kept in the memo past the resync."""
    source, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    entity = next(e for e in source._by_id.values() if e.original.startswith("ugo"))
    with vault.holding(scoped=True):
        call = vault._call()
        assert call is not None
        vault.resync()  # another thread's, as far as this call can tell
        vault._candidates(call, [entity])
        assert call.by_word  # the call has its words
    assert entity.id not in vault._words_memo
    with vault.holding(scoped=True):
        call = vault._call()
        assert call is not None
        vault._candidates(call, [entity])  # a call begun after it keeps them
    assert entity.id in vault._words_memo


def test_the_memo_weighs_what_it_keeps_alive():
    """An entry's weight counts what the memo keeps alive once the entry left memory and its call ended: its id, its
    digest, its words and the entry itself."""
    _, store = _values()
    vault = StoredVault(ROOT_KEY, store, working=10, held_at_most=0)
    _read(vault, LINES)
    for entity_id, (made_of, words, weight) in vault._words_memo.items():
        parts = made_of if isinstance(made_of, tuple) else ()  # what an entry is checked against, and what it holds
        kept = sys.getsizeof(entity_id) + sys.getsizeof(made_of) + sum(sys.getsizeof(part) for part in parts)
        assert weight >= kept + sys.getsizeof(words) + sum(sys.getsizeof(word) for word in words)
    assert vault.words_bytes == sum(weight for _, _, weight in vault._words_memo.values())
