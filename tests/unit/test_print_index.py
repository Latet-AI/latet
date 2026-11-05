"""The prints of a space held in a process (``latet.server.index``, the server performance plan's Task 6): a sorted
array of distinct 64-bit prints and a small side set of the newest, asked whether a text's words' prints are among
them; and the cache of the entries fetched by print, which knows a print only with every entry that has it. Here the
index is fed by ``add`` alone (its database is never asked); ``tests/server/test_print_index.py`` builds it from
PostgreSQL and catches it up by the change counts."""

import numpy as np
import pytest

import latet.server.index as index_module
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server.index import INDEX_SIDE_MOST, PrintCache, PrintIndex
from latet.server.prints import print_key, prints

from ..conftest import ROOT_KEY

KEY = print_key(ROOT_KEY)


def _index() -> PrintIndex:
    return PrintIndex(None, "u_tests", KEY)  # type: ignore[arg-type]  # fed by add alone: no database


def _random(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(-(2**63), 2**63 - 1, n, dtype=np.int64)


def _entity(n: int) -> Entity:
    return Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, f"v{n}@nordest.it", f"f{n}@posta.it", 0, 1.0, None)


def test_present_finds_every_print_added_and_no_other():
    """100,000 prints added, in parts and in any order (the smallest and the largest a print can be among them): each
    is present, and 100,000 others are not, nor the number next to each print added."""
    index = _index()
    added = _random(100_000, 1)
    added[:2] = (-(2**63), 2**63 - 1)
    others = np.setdiff1d(_random(100_010, 2), added)[:100_000]
    for part in np.array_split(added, 7):
        index.add(part.tolist())
    assert index.present(added).all()
    assert not index.present(others).any()
    assert not index.present(np.setdiff1d(added[2:] + 1, added)).any()
    assert len(index) == len(np.unique(added))
    mixed = np.concatenate([others[:50], added[:50]])
    assert index.present(mixed).tolist() == [False] * 50 + [True] * 50
    assert index.present(np.array([], dtype=np.int64)).shape == (0,)
    assert not _index().present(added[:10]).any()  # an index that holds nothing


def test_the_side_set_merges_past_its_bound(monkeypatch):
    """New prints go to the side set while it holds ``INDEX_SIDE_MOST`` at most, then into the sorted array, all of them
    still present; one added twice is held once."""
    monkeypatch.setattr(index_module, "INDEX_SIDE_MOST", 1_000)
    index = _index()
    prints_ = _random(3_001, 3)
    index.add(prints_[:2_000].tolist())  # past the bound at once: merged
    assert index.side == 0 and len(index) == 2_000
    index.add(prints_[2_000:3_000].tolist())
    assert index.side == 1_000 and len(index) == 3_000  # at the bound: still beside the array
    index.add(prints_[:3_000].tolist())  # every one of them again: nothing new
    assert index.side == 1_000 and len(index) == 3_000
    index.add([int(prints_[3_000])])
    assert index.side == 0 and len(index) == 3_001  # past it: merged
    assert index.present(prints_).all()
    assert INDEX_SIDE_MOST == 65_536  # the module's own bound, as the plan sets it


def test_two_rows_of_one_print_are_one_print():
    """Two rows of one word (two values of one surname, an address's domain) give one print: held once."""
    index = _index()
    bianchi = prints(KEY, "bianchi")[0]
    index.add([bianchi, bianchi, prints(KEY, "rossi")[0]])
    index.add([bianchi])
    assert len(index) == 2 and index.side == 2
    assert index.present(np.array([bianchi], dtype=np.int64)).tolist() == [True]


def test_it_weighs_eight_bytes_a_print(monkeypatch):
    """What ``nbytes`` says: eight bytes for each print held, in the array or beside it, and little else."""
    monkeypatch.setattr(index_module, "INDEX_SIDE_MOST", 50_000)
    index = _index()
    assert index.nbytes <= 4096
    index.add(_random(200_000, 4).tolist())
    distinct, side = len(index), index.side
    assert side == 0 and index.nbytes <= 8 * distinct + 64 * side + 4096
    index.add(_random(10_000, 5).tolist())
    distinct, side = len(index), index.side
    assert side == 10_000 and index.nbytes <= 8 * distinct + 64 * side + 4096
    assert index.nbytes >= 8 * distinct


def test_a_words_prints_are_made_as_prints_makes_them():
    """``prints_of``: the four prints of each word, a row each, those of ``prints.prints`` under the index's key."""
    index = _index()
    words = ["bianchi", "rossi", "nordest", "東京", ""]
    made = index.prints_of(words)
    assert made.shape == (5, 4) and made.dtype == np.int64
    assert [tuple(row) for row in made.tolist()] == [prints(KEY, word) for word in words]
    assert index.prints_of([]).shape == (0, 4)


# --- the cache of the entries fetched by print ------------------------------------------------------------------------


def test_a_print_is_known_only_with_every_entry_that_has_it():
    """A print fetched is held with every entry that has it, and given back whole; one the cache never got, or got
    with no entry, is said: the first as not known, the second as known with no entry (a value forgotten since)."""
    cache = PrintCache(10)
    a, b, c = _entity(1), _entity(2), _entity(3)
    cache.put({11: [a, b], 12: [c], 13: []}, cache.epoch)
    found, unknown = cache.get({11, 12, 13, 14})
    assert {e.id for e in found} == {a.id, b.id, c.id} and len(found) == 3 and unknown == {14}
    found, unknown = cache.get({11})
    assert sorted(e.id for e in found) == [a.id, b.id] and unknown == set()
    assert cache.get({13}) == ([], set())
    # A fetch of 12 that finds one more entry now: the print is held with both.
    d = _entity(4)
    cache.put({12: [c, d]}, cache.epoch)
    found, _ = cache.get({12})
    assert sorted(e.id for e in found) == [c.id, d.id]
    # A cache too small for every entry of a print never gives a part of them.
    small = PrintCache(2)
    small.put({21: [a, b, c]}, small.epoch)
    assert small.get({21}) == ([], {21})


def test_an_entry_let_go_takes_its_prints_with_it():
    """The entries are let go the least recently used first, past the cache's capacity; each print one of them had is
    let go with it, its other entries no longer known by it."""
    cache = PrintCache(3)
    a, b, c, d = _entity(1), _entity(2), _entity(3), _entity(4)
    cache.put({11: [a, b]}, cache.epoch)
    cache.put({12: [c]}, cache.epoch)
    cache.get({11})  # a and b used since c
    cache.put({13: [d]}, cache.epoch)  # four: c, the least recently used, goes, and 12 with it
    found, unknown = cache.get({11, 12, 13})
    assert {e.id for e in found} == {a.id, b.id, d.id} and unknown == {12}
    cache.get({13})
    cache.put({12: [c]}, cache.epoch)  # four again: a goes (b was used with it, then d, then c came), and 11 with it
    found, unknown = cache.get({11, 12, 13})
    assert {e.id for e in found} == {c.id, d.id} and unknown == {11}
    found, unknown = cache.get({11})
    assert found == [] and unknown == {11}  # b is known by no print now: no part of 11 is given


def test_drop_forgets_entries_and_prints():
    """``drop``: the entries named (a value forgotten, a fake given anew, a rule changed) go, with every print they
    were known by, and the prints named (rows written since) go, whatever entries they were held with."""
    cache = PrintCache(10)
    a, b, c, d = _entity(1), _entity(2), _entity(3), _entity(4)
    cache.put({11: [a], 12: [b, c], 13: [a, d], 14: [d]}, cache.epoch)
    cache.drop([a.id], [12])
    found, unknown = cache.get({11, 12, 13, 14})
    assert [e.id for e in found] == [d.id] and unknown == {11, 12, 13}
    cache.drop(["e-not-held"], [99])  # nothing it holds: nothing changes
    assert [e.id for e in cache.get({14})[0]] == [d.id]


def test_a_fetch_older_than_a_drop_is_not_put():
    """A thread reads the cache's epoch, then fetches print 11 and finds entry a; meanwhile another instance keeps b,
    of the same print, and the index's catch-up drops 11 (or an entry: its fake given anew). The fetch, older than the
    drop, is not put: 11 stays unknown, never known with a alone, nor as a print of no entry. A fetch begun after the
    drop is put."""
    cache = PrintCache(10)
    a, b = _entity(1), _entity(2)
    since = cache.epoch
    fetched = {11: [a]}  # what PostgreSQL said before b was kept
    cache.drop([], [11])  # the catch-up that took b's rows in
    assert cache.epoch > since
    cache.put(fetched, since)
    assert cache.get({11}) == ([], {11}) and len(cache) == 0
    cache.put({11: []}, since)
    assert cache.get({11}) == ([], {11})
    since = cache.epoch
    cache.put({11: [a, b]}, since)
    found, unknown = cache.get({11})
    assert sorted(e.id for e in found) == [a.id, b.id] and unknown == set()
    # An entry dropped (its fake given anew) between the epoch's read and the put: its old self is not put back.
    since = cache.epoch
    cache.drop([a.id], [])
    cache.put({12: [a]}, since)
    assert cache.get({12}) == ([], {12}) and cache.get({11}) == ([], {11})
    # A drop of a print the cache does not hold yet moves the epoch all the same: a fetch of it may be under way. One
    # that names nothing does not.
    since = cache.epoch
    cache.drop([], [14])
    cache.put({14: [b]}, since)
    assert cache.get({14}) == ([], {14})
    since = cache.epoch
    cache.drop([], [])
    assert cache.epoch == since
    cache.put({13: [b]}, since)
    assert [e.id for e in cache.get({13})[0]] == [b.id]


@pytest.mark.parametrize("capacity", [1, 5])
def test_the_cache_holds_no_more_than_its_capacity(capacity):
    """However many entries are fetched, the cache holds ``capacity`` at most, and as many prints known with no entry."""
    cache = PrintCache(capacity)
    for n in range(50):
        cache.put({n: [_entity(n)], 1000 + n: []}, cache.epoch)
    assert len(cache) <= capacity
    found, unknown = cache.get(set(range(50)) | {1000 + n for n in range(50)})
    assert len(found) <= capacity and len(unknown) >= 100 - 2 * capacity
