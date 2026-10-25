"""Which module and rule made each replacement, and how a value was found again."""

import sqlite3
from dataclasses import replace

import pytest

from latet.engine.modules import module_of, origin
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.gateway.recorder import reconstruct
from latet.storage.db import Store
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore

from ..conftest import ROOT_KEY


@pytest.mark.parametrize(
    ("rule", "etype", "module"),
    [
        ("gitleaks:github-pat", "GITHUB_PAT", "secrets"),
        ("assignment:DB_PASSWORD", "PASSWORD", "secret-structure"),
        ("private-key:pem", "PRIVATE_KEY", "secret-structure"),
        ("credentials:url", "URL_PASSWORD", "secret-structure"),
        ("pii:email", "EMAIL", "personal"),
        ("pii:it-fiscal-code", "IT_FISCAL_CODE", "ids"),
        ("pii:vat-de", "VAT_NUMBER", "ids"),
        ("identity:user-name", "USERNAME", "identity"),
        ("terms:client", "CLIENT", "terms"),
        ("recognizer:badge", "BADGE", "recognizers"),
        ("ner:person", "PERSON", "names"),
        ("llm:health", "HEALTH", "reviewer"),
        ("something-else", "X", "other"),
        (None, "X", "other"),
    ],
)
def test_module_of_each_rule(rule, etype, module):
    assert module_of(rule, etype) == module


def test_a_value_found_again_shows_the_rule_that_found_it_first():
    assert origin("pii:email", None, "EMAIL") == {"rule": "pii:email", "via": "detector", "module": "personal"}
    assert origin("known-values", "pii:email", "EMAIL") == {"rule": "pii:email", "via": "known", "module": "personal"}
    assert origin("plugin", "gitleaks:github-pat", "GITHUB_PAT")["via"] == "plugin"
    assert origin("restore", "gitleaks:github-pat", "GITHUB_PAT")["module"] == "secrets"
    # A vault from before rules were kept, or a surrogate sent back: the rule is unknown.
    assert origin("plugin", None, "EMAIL") == {"rule": None, "via": "plugin", "module": "other"}
    assert origin("echo", "echo", "EMAIL") == {"rule": None, "via": "echo", "module": "other"}


def test_the_vault_keeps_the_rule_of_the_first_sighting(masker, vault):
    # The password occurs first where nothing but the repeat finds it, then where its name says what it is.
    text = "log in with hunter2-Lavender then\nDB_PASSWORD=hunter2-Lavender\nmail ops@acme-intra.it"
    masker.mask(text, SlotKind.TOOL_RESULT)
    rules = {e.etype: e.rule for e in vault.entities()}
    assert rules["PASSWORD"] == "assignment:DB_PASSWORD"
    assert rules["EMAIL"] == "pii:email"
    assert rules["DOMAIN"] == "pii:email"  # the e-mail's domain, masked along with it
    # Seen again later, by the known-values detector: the first rule stays.
    masker.mask("again hunter2-Lavender", SlotKind.USER_TEXT)
    assert {e.etype: e.rule for e in vault.entities()}["PASSWORD"] == "assignment:DB_PASSWORD"


def test_an_old_vault_gains_the_columns_and_learns_rules(tmp_path):
    path = tmp_path / "latet.db"
    store = Store(path, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    entity = vault.intern("EMAIL", EntityClass.PII, "a.b@acme.it")  # no rule: as recorded before
    store.close()
    # The table as it was before rules were kept.
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE old AS SELECT id, etype, eclass, original, surrogate, counter, created FROM entities;"
        "DROP TABLE entities; ALTER TABLE old RENAME TO entities;"
    )
    db.close()

    store = Store(path, ROOT_KEY)
    loaded = store.load_entities()
    assert {e.rule for e in loaded} == {None}
    vault = Vault(ROOT_KEY, loaded, on_insert=store.save_entity, on_rule=store.save_rule)
    version = vault.version
    again = vault.intern("EMAIL", EntityClass.PII, "a.b@acme.it", rule="pii:email")
    assert (again.surrogate, again.rule) == (entity.surrogate, "pii:email")
    assert vault.get(entity.id).rule == "pii:email" and vault.version == version  # nothing to re-mask
    store.close()
    assert {e.etype: e.rule for e in Store(path, ROOT_KEY).load_entities()}["EMAIL"] == "pii:email"


def test_an_old_vault_gains_the_column_of_how_a_value_was_found_and_its_entries_say_none(tmp_path):
    from latet.engine.vault import fake_reading

    path = tmp_path / "latet.db"
    store = Store(path, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    name = vault.intern("PERSON", EntityClass.NER, "Mario+Rossi", rule="known", reads="Mario Rossi")
    phone = vault.intern("PHONE", EntityClass.PII, "333+1234567", rule="known", reads="333 1234567")
    store.close()
    db = sqlite3.connect(path)  # the table as it was before Latet kept how a value was found
    db.executescript(
        "CREATE TABLE old AS SELECT id, etype, eclass, original, surrogate, counter, created, rule FROM entities;"
        "DROP TABLE entities; ALTER TABLE old RENAME TO entities;"
    )
    db.close()
    loaded = {e.original: e for e in Store(path, ROOT_KEY).load_entities()}
    assert loaded == {"Mario+Rossi": replace(name, decoded=None), "333+1234567": replace(phone, decoded=None)}
    assert "decoded" in {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(entities)")}
    # 0.9.9's rule, kept for them: a `+` is a form's space in a value written in words alone.
    assert fake_reading(loaded["Mario+Rossi"]) == fake_reading(name) and fake_reading(loaded["333+1234567"]) is None


# The statements Latet 0.9.9 reads and keeps a vault's values with (storage/db.py at 0.9.9): it names every column it
# uses, so a vault of a later Latet, with a column more, stays one it opens.
_SELECT_099 = "SELECT id, etype, eclass, original, surrogate, counter, created, rule FROM entities"
_INSERT_099 = (
    "INSERT OR IGNORE INTO entities (id, etype, eclass, original, surrogate, counter, created, rule)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def test_latet_0_9_9_reads_and_keeps_values_in_a_vault_of_this_latet(tmp_path):
    path = tmp_path / "latet.db"
    store = Store(path, ROOT_KEY)
    found = Vault(ROOT_KEY, on_insert=store.save_entity).intern(
        "PHONE", EntityClass.PII, "333+1234567", rule="known", reads="333 1234567"
    )
    sealed = store.seal("333 7654321")
    store.close()
    db = sqlite3.connect(path, isolation_level=None)
    [row] = db.execute(_SELECT_099).fetchall()
    assert (row[0], row[4]) == (found.id, found.surrogate) and store.open(row[3]) == found.original
    db.execute(_INSERT_099, ("k" * 20, "PHONE", int(EntityClass.PII), sealed, "345 1111111", 0, 0.0, "pii:phone"))
    db.close()
    loaded = {e.original: e for e in Store(path, ROOT_KEY).load_entities()}
    assert loaded["333 7654321"].decoded is None and loaded["333+1234567"] == found


def test_records_from_before_get_module_and_rule_when_shown(vault):
    entity = vault.intern("EMAIL", EntityClass.PII, "a.b@acme.it", rule="pii:email")
    masked = f"mail {entity.surrogate}"
    start = masked.index(entity.surrogate)
    old = [{"masked_start": start, "masked_end": len(masked), "type": "EMAIL", "rule": "plugin", "entity": entity.id}]
    _, [span] = reconstruct(masked, old, vault)
    assert (span["rule"], span["via"], span["module"]) == ("pii:email", "plugin", "personal")


def test_stored_blocks_count_values_by_module(tmp_path):
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    repl = [
        {"masked_start": 0, "masked_end": 3, "type": "EMAIL", "class": "PII", "rule": "pii:email"},
        {"masked_start": 4, "masked_end": 7, "type": "EMAIL", "class": "PII", "rule": "plugin"},  # recorded before
    ]
    block = BlockRecord(store.block_id("abc def"), "$", "user_text", False, True, "abc def", repl)
    store.record(ExchangeRecord(summary={"ts": 1.0, "session": "s", "flags": []}, blocks=[block]))
    assert store.block_types([block.id]) == {block.id: [("EMAIL", "PII", "other", 1), ("EMAIL", "PII", "personal", 1)]}
