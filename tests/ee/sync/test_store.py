"""What term sync keeps, the same in memory and on PostgreSQL: a term gone from its source kept until it is removed,
ids that say nothing of the terms, and each source's last run."""

import hashlib
from dataclasses import replace

import pytest

from latet.ee.sync.store import Asked, MemoryTermStore, Run, StoredTerm, TooMany

from .fakes import id_of

HALDEN, KESTREL = ("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")
COMPOSED, DECOMPOSED = "Caf\N{LATIN SMALL LETTER E WITH ACUTE}", "Cafe\N{COMBINING ACUTE ACCENT}"


def test_a_term_gone_from_its_source_is_kept_and_removed_only_once_gone(store):
    assert store.apply("customers", [HALDEN, KESTREL], now=100.0) == (2, 0)
    assert store.apply("customers", [KESTREL], now=200.0) == (0, 1)
    assert {(t.value, t.gone) for t in store.terms()} == {("Halden Insurance", 200.0), ("Kestrel Analytics", None)}
    assert store.counts() == {"customers": {"terms": 1, "by_type": {"CLIENT": 1}, "gone": 1}}
    gone, total = store.gone()
    kestrel = id_of(store, "customers", "CLIENT", "Kestrel Analytics")
    assert total == 1 and store.remove([kestrel, gone[0].id]) == {"CLIENT": 1}  # still in its source: left alone
    assert [t.value for t in store.terms()] == ["Kestrel Analytics"]


def test_a_term_back_in_its_source_is_no_longer_gone_and_the_version_moves_with_changes_only(store):
    store.apply("customers", [HALDEN, KESTREL], now=1.0)
    store.apply("customers", [KESTREL], now=2.0)
    version = store.version()
    store.apply("customers", [HALDEN, KESTREL], now=3.0)
    assert store.version() == version + 1 and all(t.gone is None for t in store.terms())
    store.apply("customers", [HALDEN, KESTREL], now=4.0)
    assert store.version() == version + 1  # nothing changed


def test_an_id_says_nothing_of_the_term(store):
    store.apply("customers", [HALDEN], now=1.0)  # on a server, the first run makes the key of the ids
    ours = id_of(store, "customers", "CLIENT", "Halden Insurance")
    assert ours == id_of(store, "customers", "CLIENT", "Halden Insurance") and [t.id for t in store.terms()] == [ours]
    assert ours != id_of(MemoryTermStore(b"\x08" * 32), "customers", "CLIENT", "Halden Insurance")
    assert ours != hashlib.sha256(b"Halden Insurance").hexdigest()[:24]


def test_the_terms_of_a_source_no_longer_set_up_become_gone(store):
    store.apply("customers", [HALDEN], now=1.0)
    store.apply("projects", [("Project Falcon", "PROJECT")], now=1.0)
    assert store.retire(["projects"], now=5.0) == 1
    assert {t.value: t.gone for t in store.terms()} == {"Halden Insurance": 5.0, "Project Falcon": None}


def test_removing_takes_the_gone_terms_of_one_source_or_of_every_one(store):
    store.apply("customers", [HALDEN, KESTREL], now=1.0)
    store.apply("projects", [("Project Falcon", "PROJECT")], now=1.0)
    store.retire([], now=2.0)
    assert store.remove(source="projects") == {"PROJECT": 1}
    assert store.remove() == {"CLIENT": 2} and store.terms() == []


def test_the_last_run_of_each_source_is_kept(store):
    run = Run("customers", "file", 1.0, 2.0, "failed", "unreachable", "cannot read customers.csv: No such file")
    store.record_run(run)
    assert store.runs() == {"customers": run}


def test_a_change_of_case_at_the_source_is_neither_gone_nor_new(store):
    store.apply("customers", [("Halden Insurance", "CLIENT")], now=1.0)
    assert store.apply("customers", [("HALDEN INSURANCE", "CLIENT")], now=2.0) == (0, 0)
    assert [t.value for t in store.terms()] == ["Halden Insurance"]  # the first spelling stays


def test_a_sync_asked_for_is_kept_until_a_run_that_began_after_it(store):
    store.ask(["customers"], "u_sec0000001", now=10.0)
    assert store.asks() == {"customers": Asked("customers", 10.0, "u_sec0000001")}
    store.record_run(Run("customers", "file", 5.0, 12.0, "ok"))  # began before the ask: the ask stays
    assert "customers" in store.asks()
    store.record_run(Run("customers", "file", 11.0, 13.0, "ok"))
    assert store.asks() == {}


def test_a_source_forgotten_keeps_no_last_run_nor_a_sync_asked_for(store):
    """The final review's M4: a source taken out, changed or set up again under its name forgets its last run and a
    sync asked for, so that its next run is of the source as it is now, at once; the others keep theirs."""
    store.record_run(Run("customers", "file", 1.0, 2.0, "ok"))
    store.record_run(Run("projects", "url", 1.0, 2.0, "failed", "unreachable", "no answer (ConnectError)"))
    store.ask(["customers", "projects", "crm"], None, now=3.0)
    store.forget_runs(["customers", "crm", "directory"])
    assert set(store.runs()) == {"projects"} and set(store.asks()) == {"projects"}
    store.forget_runs([])
    assert set(store.runs()) == {"projects"}


def test_kept_counts_every_row_gone_ones_included(store):
    store.apply("customers", [HALDEN, KESTREL], now=1.0)
    store.apply("customers", [KESTREL], now=2.0)
    assert store.kept() == 2


def test_a_run_past_the_terms_kept_at_most_changes_nothing(store):
    store.apply("customers", [HALDEN], now=1.0)
    with pytest.raises(TooMany):
        store.apply("projects", [("Project Falcon", "PROJECT"), ("Project Heron", "PROJECT")], now=2.0, most=2)
    assert store.kept() == 1 and store.apply("customers", [HALDEN], now=3.0, most=1) == (0, 0)


def test_two_spellings_of_one_term_in_one_run_are_one_term_the_first_kept(store):
    assert store.apply("customers", [(COMPOSED, "CLIENT"), (DECOMPOSED, "CLIENT")], now=1.0) == (1, 0)
    assert [t.value for t in store.terms()] == [COMPOSED]


def test_the_terms_kept_at_most_count_the_gone_ones(store):
    store.apply("customers", [HALDEN], now=1.0)
    store.retire([], now=2.0)  # gone from its source, still kept and masked: it counts
    version = store.version()
    with pytest.raises(TooMany) as caught:
        store.apply("projects", [("Project Falcon", "PROJECT"), ("Project Heron", "PROJECT")], now=3.0, most=2)
    assert caught.value.kept == 3 and store.kept() == 1 and store.version() == version
    assert [(t.value, t.gone) for t in store.terms()] == [("Halden Insurance", 2.0)]


def test_a_run_that_began_when_a_sync_was_asked_for_takes_it(store):
    store.ask(["customers"], None, now=10.0)
    store.record_run(Run("customers", "file", 10.0, 11.0, "ok"))
    assert store.asks() == {}


def test_removing_no_ids_removes_nothing_and_no_ids_given_removes_every_gone_one(store):
    store.apply("customers", [HALDEN, KESTREL], now=1.0)
    store.apply("customers", [KESTREL], now=2.0)
    version = store.version()
    assert store.remove([]) == {} and store.kept() == 2 and store.version() == version
    assert store.remove() == {"CLIENT": 1} and [t.value for t in store.terms()] == ["Kestrel Analytics"]


def test_a_stored_term_printed_shows_no_value_and_still_compares_it(store):
    store.apply("customers", [HALDEN], now=1.0)
    (term,) = store.terms()
    assert "Halden" not in repr(term) and term.id in repr(term)
    assert term == StoredTerm(term.id, "customers", "CLIENT", "Halden Insurance", 1.0, None)
    assert term != replace(term, value="Kestrel Analytics")
