"""The vault over a store that other processes write too: what it learns from them, what a store that refuses or
fails leaves it knowing, and what the masker that reads it knows while other threads keep values."""

import sqlite3
import threading

import pytest

from latet.engine import known as known_module
from latet.engine.masker import Masker
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, FakeTaken, MaskingError, Vault
from latet.engine.verifier import Verifier

from ...conftest import ROOT_KEY


def test_a_name_kept_with_a_quote_before_names_were_trimmed_is_known_without_it():
    """The name model once took the opening quote console.table writes into a name ('Francesca Solieri): such an
    entry, loaded or learnt, is known without it, so that the text keeps its quote ('Marta Galli', never Marta Galli')
    and the fake restores to the name alone."""
    old = Vault(ROOT_KEY).intern("PERSON", EntityClass.NER, "'Francesca Solieri", rule="ner:person")  # as kept then
    for vault in (Vault(ROOT_KEY, [old]), _learnt(old)):
        assert vault.by_original("'Francesca Solieri") is None
        assert vault.by_surrogate(old.surrogate).original == "Francesca Solieri"
        text = "│ 0 │ 'Francesca Solieri' │ null │"
        masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT).text
        assert masked == f"│ 0 │ '{old.surrogate}' │ null │"
    # A value of another kind keeps its quotes: they may be part of it.
    term = Vault(ROOT_KEY).intern("CUSTOM", EntityClass.CUSTOM, "'Orion'", rule="term:custom")
    assert Vault(ROOT_KEY, [term]).by_original("'Orion'") is not None


def _learnt(entity: Entity) -> Vault:
    vault = Vault(ROOT_KEY)
    vault.learn(entity)
    return vault


def test_a_value_another_process_kept_is_learned_and_nothing_is_saved():
    entity = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "emily@gmail.com")
    saved = []
    here = Vault(ROOT_KEY, on_insert=saved.append)
    version = here.version
    here.learn(entity)
    here.learn(entity)  # twice: known once
    assert here.by_original("emily@gmail.com") == entity and here.by_surrogate(entity.surrogate) == entity
    assert here.get(entity.id) == entity and here.version == version + 1 and saved == []


def test_a_fake_learnt_that_reads_as_a_fake_written_in_escapes_puts_no_entry_out():
    """A server's instance learns another's entry whose fake (`Nicola Greco`) is how one of its own fakes, written in a
    link's escapes, reads (`Nicola%20Greco`, for `Mario%20Rossi`): PostgreSQL keeps one value per fake as written, and
    those are two fakes there. The entry of the link is kept: neither displaced nor forgotten."""
    from latet.server import spaces

    vault = Vault(ROOT_KEY)
    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    link = vault.intern("PERSON", EntityClass.NER, "Mario%20Rossi", rule="ner", reads="Mario Rossi")
    reading = link.surrogate.replace("%20", " ")
    assert vault.by_surrogate(reading) is link
    other = Entity("from-another-instance", "PERSON", EntityClass.NER, "Luca Bianchi", reading, 0, 0.0, "ner")
    assert not spaces._displaced(vault, other)
    assert spaces._learned(vault, other) is False
    assert vault.get(link.id) == link and vault.by_original("Mario%20Rossi") == link
    assert vault.by_surrogate(reading) == other and vault.by_surrogate(link.surrogate) == link


def _host() -> Entity:
    """A computer named `host`, with the fake it got before the vault refused one that shows its value."""
    entity_id = Vault(ROOT_KEY).entity_id("HOSTNAME", "host")
    return Entity(entity_id, "HOSTNAME", EntityClass.IDENTITY, "host", "host-1a2b3c", 0, 0.0, "identity:hostname")


def test_a_fake_drawn_again_at_load_never_shows_its_value_whatever_draws_it(monkeypatch):
    drawn = iter(["host-9", "HOST.local", "box-7"])  # the first two show it, whatever the case
    monkeypatch.setattr(Vault, "_generate", lambda self, etype, eclass, original, counter, *rest: next(drawn))
    kept = []
    vault = Vault(
        ROOT_KEY, [_host()], on_replace=lambda before, after: kept.append((before.surrogate, after.surrogate))
    )
    found = vault.by_original("host")
    assert found is not None and found.surrogate == "box-7" and kept == [("host-1a2b3c", "box-7")]
    assert vault.by_surrogate("host-1a2b3c") is None


def test_an_old_fake_no_other_can_replace_stays_and_the_log_names_its_entry_alone(monkeypatch, caplog):
    monkeypatch.setattr(Vault, "_generate", lambda self, etype, eclass, original, counter, *rest: f"host-{counter}")
    old = _host()
    vault = Vault(ROOT_KEY, [old], on_replace=lambda before, after: pytest.fail("nothing to keep"))
    assert vault.by_original("host") == old
    assert [r.getMessage() for r in caplog.records] == [
        f"vault entry {old.id} keeps a fake that shows its value: no other could be drawn"
    ]


def test_without_a_way_to_keep_a_new_fake_the_vault_loads_its_entries_as_they_are():
    old = _host()
    assert Vault(ROOT_KEY, [old]).by_original("host") == old


def test_a_rule_another_process_learned_is_the_one_the_next_sighting_finds():
    # The same value: kept before rules were kept, and with the rule another process learned for it since.
    old = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "emily@gmail.com")
    new = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "emily@gmail.com", rule="pii:email")
    assert (old.id, old.surrogate) == (new.id, new.surrogate) and (old.rule, new.rule) == (None, "pii:email")
    saved = []
    here = Vault(ROOT_KEY, [old], on_insert=saved.append, on_rule=saved.append)
    version = here.version
    here.learn(new)
    found = here.by_original("emily@gmail.com")
    assert found == new and found.rule == "pii:email"
    assert here.get(old.id) == new and here.by_surrogate(old.surrogate) == new
    assert here.version > version  # what is cached under the old version is made again
    assert here.intern("EMAIL", EntityClass.PII, "emily@gmail.com", rule="pii:email") == new  # the next sighting
    assert saved == []  # learned in memory only: neither the new value nor its rule is written again


def test_a_value_kept_again_elsewhere_with_another_fake_is_masked_and_restored_with_that_one():
    old = Vault(ROOT_KEY).intern("PROJECT", EntityClass.CUSTOM, "Falcon", generate=lambda original, rng: "Vega-7")
    new = Vault(ROOT_KEY).intern("PROJECT", EntityClass.CUSTOM, "Falcon", generate=lambda original, rng: "Altair-3")
    assert old.id == new.id  # the same value, forgotten and kept again by another process with another fake
    saved = []
    here = Vault(ROOT_KEY, [old], on_insert=saved.append, on_rule=saved.append)
    version = here.version
    here.learn(new)
    assert here.get(old.id) == new and here.by_original("Falcon") == new and here.by_surrogate("Altair-3") == new
    assert here.by_surrogate("Vega-7") is None and not here.is_surrogate("Vega-7")  # the old fake restores nothing
    assert here.version > version
    assert here.intern("PROJECT", EntityClass.CUSTOM, "Falcon") == new  # the next sighting gives the new fake
    assert [e.surrogate for e in here.entities()] == ["Altair-3"]
    assert saved == []  # in memory only: neither the new value nor a rule is written to the store


def test_a_value_the_vault_is_not_let_learn_stops_every_text_masked_or_checked_through_it():
    """RP4: on a Latet server, a value another instance kept is learnt through ``Vault.learn``, and a space past its
    share learns none (``on_learn`` refuses it, with words that name no value). It then lacks a value that only its own
    detector would find, as one the local reviewer taught: every text masked or checked through it is refused with
    those words from then on, a text checked clean before included, and none leaves with the value in clear."""
    entity = Vault(ROOT_KEY).intern("CLIENT", EntityClass.CUSTOM, "Orion Holdings")  # taught on another instance
    words = "this identity's space holds its share of the memory this instance of the server gives the spaces"

    def refused(entity):
        raise MaskingError(words)

    here = Vault(ROOT_KEY, on_learn=refused)
    masker, text = Masker(here, []), "the Orion Holdings renewal is due on Friday"
    verifier = Verifier(masker)
    assert masker.mask(text).text == text and verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "s1").checked
    with pytest.raises(MaskingError, match="share of the memory") as stopped:
        here.learn(entity)
    assert "Orion" not in str(stopped.value)
    assert here.get(entity.id) is None and here.by_original(entity.original) is None  # not learnt
    for stop in (
        lambda: masker.mask(text),
        lambda: masker.known_in(text),
        lambda: verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "s1"),  # clean before: checked again
    ):
        with pytest.raises(MaskingError, match="share of the memory"):
            stop()


class Watched(dict):
    """One of a vault's indexes, which says so after each change made to it."""

    def __init__(self, held, changed):
        super().__init__(held)
        self.changed = changed

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.changed()

    def pop(self, *args):
        found = super().pop(*args)
        self.changed()
        return found

    def setdefault(self, key, default=None):
        found = super().setdefault(key, default)
        self.changed()
        return found


@pytest.mark.parametrize("fake", ["Altair-3", "Vega-7"], ids=["another-fake", "the-same-fake-and-a-rule"])
def test_while_a_value_is_learned_again_a_lookup_finds_the_old_entity_or_the_new_one_never_neither(fake):
    """The lookups take no lock: a response is restored and another thread masks while a value another process kept
    again takes the place of what this vault knew. At every step of that, the value is known (not knowing it, a masking
    would take it for a new one), its id answers an entity, and one of its fakes is restored."""
    old = Vault(ROOT_KEY).intern("PROJECT", EntityClass.CUSTOM, "Falcon", generate=lambda original, rng: "Vega-7")
    new = Vault(ROOT_KEY).intern("PROJECT", EntityClass.CUSTOM, "Falcon", "", "llm:confidential", lambda o, rng: fake)
    here, seen = Vault(ROOT_KEY, [old]), []

    def looked_up():
        seen.append(
            (here.by_original("Falcon"), here.get(old.id), here.by_surrogate("Vega-7") or here.by_surrogate(fake))
        )

    for index in ("_by_key", "_by_surrogate", "_by_original", "_by_id"):
        setattr(here, index, Watched(getattr(here, index), looked_up))
    here.learn(new)
    assert seen and [found for step in seen for found in step if found not in (old, new)] == []
    assert seen[-1] == (new, new, new) and [e.rule for e in here.entities()] == ["llm:confidential"]


@pytest.mark.parametrize(
    ("etype", "eclass", "original"),
    [("PERSON", EntityClass.NER, "Emily Carter"), ("ACCOUNT", EntityClass.STRUCT, "AB-1234")],
)
def test_a_store_that_refuses_every_fake_stops_the_masking_and_the_vault_knows_nothing(etype, eclass, original):
    def refuse(entity):
        raise FakeTaken(f"the fake made for a {entity.etype} is another value's")

    vault = Vault(ROOT_KEY, on_insert=refuse)
    with pytest.raises(MaskingError, match=f"no collision-free surrogate for a {etype}") as stopped:
        vault.intern(etype, eclass, original)
    assert original not in str(stopped.value)  # the error names the type, never the value
    assert vault.entities() == [] and vault.by_original(original) is None and vault.version == 0


def test_a_store_whose_save_fails_leaves_the_value_unknown_to_the_vault():
    disk = {"full": True}

    def save(entity):
        if disk["full"]:
            raise sqlite3.OperationalError("database or disk is full")

    vault = Vault(ROOT_KEY, on_insert=save)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        vault.intern("PERSON", EntityClass.NER, "Emily Carter")
    assert vault.entities() == [] and vault.by_original("Emily Carter") is None and vault.version == 0
    disk["full"] = False  # room again: the next request masks the value, and the vault knows it from then on
    entity = vault.intern("PERSON", EntityClass.NER, "Emily Carter")
    assert vault.by_original("Emily Carter") == entity and vault.get(entity.id) == entity


def test_a_value_kept_while_the_masker_reads_the_vault_is_known_to_its_next_reading(monkeypatch):
    """The engine's masker and the final check read the vault's values through detectors made again where the vault
    changed. A value another thread keeps while they are made (the plugin's thread, at your decision in the Live
    Reviewer) is not in them: they must count as older than the vault, so that the next reading has it."""
    vault = Vault(ROOT_KEY)
    vault.intern("PROJECT", EntityClass.CUSTOM, "Atlas")
    masker = Masker(vault, [])
    kept, made = [], known_module.KnownValuesDetector

    def kept_while_made(*args, **kwargs):
        if not kept:  # once, right after the reading took what the vault changed
            kept.append(vault.intern("PROJECT", EntityClass.CUSTOM, "Orion", rule="human:confidential"))
        return made(*args, **kwargs)

    monkeypatch.setattr(known_module, "KnownValuesDetector", kept_while_made)
    text = "ask about the Orion contract"
    assert masker.known_in(text) == []  # this reading's detectors were made of what it took before the value was kept
    [found] = masker.known_in(text)  # the next one has it: the vault changed since
    assert text[found.start : found.end] == "Orion"
    assert masker.mask(text).text == f"ask about the {kept[0].surrogate} contract"
    assert [s.rule for s in masker.surrogates_in(f"about {kept[0].surrogate}")] == ["echo"]  # its fake is known too


def test_two_threads_reading_the_vault_at_once_each_read_it_whole_as_it_was_when_they_began(monkeypatch):
    """The engine's thread and the plugin's each read the vault's values, one begun before a value was kept and one
    after: the later one waits for the earlier one, never makes its own beside it, then reads what the vault changed
    meanwhile. What the masker holds in the end is every value of the vault at its version, never older values under a
    newer version."""
    vault = Vault(ROOT_KEY)
    vault.intern("PROJECT", EntityClass.CUSTOM, "Atlas")
    masker = Masker(vault, [])
    text, made = "ask about the Orion contract", known_module.KnownValuesDetector
    older_has_read, go = threading.Event(), threading.Event()
    waited, found = [], {}

    def held(*args, **kwargs):
        if threading.current_thread().name == "older" and not older_has_read.is_set():
            older_has_read.set()  # it took what the vault changed, before the value was kept, and waits
            waited.append(go.wait(5))
        return made(*args, **kwargs)

    def reads():
        found[threading.current_thread().name] = masker.known_in(text)

    monkeypatch.setattr(known_module, "KnownValuesDetector", held)
    older, newer = threading.Thread(target=reads, name="older"), threading.Thread(target=reads, name="newer")
    older.start()
    assert older_has_read.wait(5)
    vault.intern("PROJECT", EntityClass.CUSTOM, "Orion", rule="human:confidential")  # kept after the older one read
    newer.start()
    newer.join(0.3)
    assert newer.is_alive()  # it waits for the older reading
    go.set()
    older.join(5)
    newer.join(5)
    assert waited == [True] and not older.is_alive() and not newer.is_alive()
    assert found["older"] == [] and len(found["newer"]) == 1  # each read the vault as it was when it began
    assert vault.matchers.current().version == vault.version and len(masker.known_in(text)) == 1
