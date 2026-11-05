"""scripts/bench_engine.py: its commands, and a bench of known values that refuses a folder where no bench vault was
built, in words, before it reaches for the test services."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import bench_engine  # noqa: E402
import vault_fuzz  # noqa: E402


def test_the_parser_has_detectors_and_known():
    parser = bench_engine.parser()
    detectors = parser.parse_args(["detectors", "--rounds", "3"])
    assert (detectors.command, detectors.rounds) == ("detectors", 3)
    known = parser.parse_args(["known", "--folder", "v1k", "--rounds", "2"])
    assert (known.command, known.folder, known.rounds) == ("known", Path("v1k"), 2)
    with pytest.raises(SystemExit):
        parser.parse_args(["known"])  # the folder of a build is required


def test_known_refuses_a_folder_without_a_builds_state(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("LATET_TEST_VALKEY_URL", raising=False)
    with pytest.raises(SystemExit) as refused:
        bench_engine.main(["known", "--folder", str(tmp_path)])
    said = str(refused.value.code)
    assert f"nothing was built in {tmp_path}" in said
    assert "bench_apps_server.py" in said and "build" in said


def test_the_fuzz_texts_are_read_against_the_fuzz_values_in_a_store_of_their_own():
    """The fuzz's texts hold the fuzz harness's values, not a bench vault's: they are read by a plain vault holding
    those values and by a key's stored vault whose store alone holds them (memory 0), and both find something."""
    texts = vault_fuzz.fuzz_texts(500)
    differ = bench_engine.fuzz_differ(vault_fuzz.MemoryStore(), texts, working=0, held=1000)
    assert differ.texts == 500
    assert differ.found_plain >= 100 and differ.found_stored >= 100, differ.line("fuzz texts")
    assert differ.fewer == 0, differ.line("fuzz texts")


def test_a_comparison_says_in_how_many_texts_each_side_found_something():
    """A comparison where neither side finds anything shows it, rather than a bare 0 characters differing."""
    line = bench_engine.Differ(texts=5000).line("fuzz texts")
    assert "found something in 0 (plain) and 0 (key's) of 5000 texts" in line


def test_a_known_record_says_what_the_key_vault_held_and_read_every_text_for():
    """Task 8's fix round 7: known compares populations in one state, and its record says it. What B held as it began
    (memory, held for good, held glued, matched: what every text is read for besides its words' values) is counted, so
    that a population a serve taught secrets to is told from one that was only built."""
    from latet.engine.stored import StoredVault
    from latet.engine.types import EntityClass
    from latet.engine.vault import Vault

    store = vault_fuzz.MemoryStore()
    source = Vault(vault_fuzz.ROOT_KEY)
    store.save_entity(source.intern("API_KEY", EntityClass.SECRET, "tok9fQ2xL7pZ4a"))  # one run: held glued
    store.save_entity(source.intern("EMAIL", EntityClass.PII, "Laura.Bianchi@nordest.it"))
    vault = StoredVault(vault_fuzz.ROOT_KEY, store, working=10, held_at_most=10)
    state = bench_engine.vault_state(vault)
    assert state == {"memory": 2, "held_for_good": 1, "held_glued": 1, "matched": 1, "detectors_bytes": 0}
    vault.matchers.current()  # the parts made: the one secret's alternation within the ordinary budget
    assert bench_engine.vault_state(vault)["detectors_bytes"] == 0


def test_the_known_records_state_reads_these_attributes_of_a_stored_vault():
    """The review of fix round 7 (its Minor 2): ``vault_state`` reads a stored vault's own attributes, by these names.
    A rename fails here, and the harness raises ``AttributeError`` rather than recording zeros."""
    from latet.engine.stored import StoredVault

    vault = StoredVault(vault_fuzz.ROOT_KEY, vault_fuzz.MemoryStore(), working=10, held_at_most=10)
    attributes = vars(vault)
    for name in ("_for_good", "_glued", "_matched"):
        assert name in attributes and len(attributes[name]) == 0
    assert callable(vault.entities) and vault.detectors_bytes == 0
    assert set(bench_engine.vault_state(vault)) == {
        "memory",
        "held_for_good",
        "held_glued",
        "matched",
        "detectors_bytes",
    }
