"""The bounded interior lookup uses the existing PostgreSQL edge rows and transaction scope."""

import pytest

from latet.engine import lookup
from latet.engine.anchors import words_in
from latet.engine.lookup import LookupBudget
from latet.engine.pipeline import Engine, Session
from latet.engine.types import EntityClass
from latet.engine.vault import MaskingError, Vault
from latet.server import vault as pg

from ..conftest import ROOT_KEY
from ..unit.test_interior_lookup import DATA, MINIMAL, TOKEN, masker, positions, round_trips, seeded
from .test_key_vault import APPS_API, _bearer, _calling, _masked

pytestmark = pytest.mark.server


@pytest.fixture
def keys(server):
    mine, _ = server.identities.add_key("Interior lookup")
    theirs, _ = server.identities.add_key("Other lookup")
    return pg.PgVaultStore(server.db, mine.id, ROOT_KEY), pg.PgVaultStore(server.db, theirs.id, ROOT_KEY)


@pytest.mark.parametrize("scoped", [False, True], ids=["gateway", "apps"])
def test_postgresql_masks_the_allowed_email_after_eviction(keys, scoped):
    store, _ = keys
    plain, vault, _ = seeded(store)
    expected = masker(plain).mask(MINIMAL, DATA).text
    engine = Engine(vault, masker(vault))
    for label in ("first", "hot"):
        with vault.holding(scoped=scoped):
            result = engine.mask_text(MINIMAL, DATA, Session(label))
        assert positions(result) == set(range(44)) and result.text == expected
        round_trips(vault, MINIMAL, result.text)


def test_interior_queries_preserve_identity_and_original_fake_roles(keys):
    mine, theirs = keys
    _, _, entity = seeded(mine)
    words = words_in(f"x{TOKEN}y", interior=LookupBudget().interior)
    assert mine.bounded_words("original", words, LookupBudget()) == [entity]
    assert mine.bounded_words("surrogate", words, LookupBudget()) == []
    assert theirs.bounded_words(("original", "surrogate"), words, LookupBudget()) == []
    fakes = words_in(f"x{entity.surrogate}y", interior=LookupBudget().interior)
    assert mine.bounded_words("surrogate", fakes, LookupBudget()) == [entity]
    assert mine.bounded_words("original", fakes, LookupBudget()) == []


def test_interior_lookup_never_falls_back_to_reading_every_vault_word(keys, monkeypatch):
    mine, _ = keys
    _, vault, _ = seeded(mine)
    monkeypatch.setattr(pg, "_ALL_WORDS", "SELECT 1 / 0")
    monkeypatch.setattr(pg, "WORDS_ASKED_AT_MOST", 1)
    with vault.holding():
        vault.prefetch([MINIMAL])
        result = masker(vault).mask(MINIMAL, DATA)
    assert positions(result) == set(range(44))


def test_sql_limits_payload_before_transfer_and_before_decryption(keys, monkeypatch):
    mine, _ = keys
    _, _, entity = seeded(mine, value="AAAAAA" + "x" * 4096 + "ZZZZZZ")
    words = {"aaaaaa"}
    hashed = sorted(mine._hashed(("original",), words))
    roles = ["original", "original-head", "original-tail"]
    allowance = 128
    with mine.db.connection() as conn:
        rows = conn.execute(pg._BOUNDED_WORDS, (mine.identity, mine.identity, roles, hashed, 10, allowance)).fetchall()
    assert len(rows) == 1
    # The real SQL response contains neither ciphertext nor unbounded text metadata, only a size sentinel.
    assert all(rows[0][column] is None for column in (0, 1, 3, 4, 7))
    assert rows[0][9] > allowance
    monkeypatch.setattr(lookup, "LOOKUP_BYTES", allowance)
    opened = []
    real = mine._opened
    monkeypatch.setattr(mine, "_opened", lambda row: opened.append(row) or real(row))
    with pytest.raises(MaskingError, match="safe work limit") as refused:
        mine.bounded_words("original", words, LookupBudget())
    assert not opened
    assert entity.original not in str(refused.value) and entity.surrogate not in str(refused.value)


def test_unreadable_colliding_rows_count_toward_the_cumulative_candidate_bound(keys, monkeypatch):
    mine, _ = keys
    source = Vault(ROOT_KEY)
    entities = [source.intern("API_KEY", EntityClass.SECRET, f"AAAAAA{n}LongCollisionZZZZZZ") for n in range(3)]
    for entity in entities:
        mine.save_entity(entity)
    with mine.db.connection() as conn:
        conn.execute("UPDATE vault_entities SET original = surrogate WHERE identity_id = %s", (mine.identity,))
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 2)
    with pytest.raises(MaskingError, match="safe work limit"):
        mine.bounded_words("original", {"aaaaaa"}, LookupBudget())
    assert mine.unreadable == 2


def test_a_call_past_its_bound_keeps_the_values_it_created(keys, monkeypatch):
    """Task 8's fix round 6: no bound refuses an applications call. Past one it reads as 0.10.2 did, and keeps (commits)
    the values it created."""
    mine, _ = keys
    _, vault, _ = seeded(mine)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    with vault.holding(scoped=True):
        created = vault.intern("API_KEY", EntityClass.SECRET, "AnotherNewSecret123456789")
        vault.prefetch([MINIMAL])
        masker(vault).mask(MINIMAL, DATA)
        assert vault._call().lookup.past
    assert mine.by_original(created.original) == created


def test_sealed_payload_budget_is_shared_across_separate_sql_lookups(keys, monkeypatch):
    mine, _ = keys
    _, _, first = seeded(mine)
    _, _, second = seeded(mine, value="AnotherCandidate9876543210")
    with mine.db.connection() as conn:
        size = conn.execute(
            "SELECT octet_length(original)::bigint + octet_length(surrogate) + octet_length(id)"
            " + octet_length(etype) + coalesce(octet_length(rule), 0)"
            " FROM vault_entities WHERE identity_id = %s AND id = %s",
            (mine.identity, first.id),
        ).fetchone()[0]
    monkeypatch.setattr(lookup, "LOOKUP_BYTES", size)
    budget = LookupBudget()
    assert mine.bounded_words("original", {first.original.lower()}, budget) == [first]
    with pytest.raises(MaskingError, match="safe work limit"):
        mine.bounded_words("original", {second.original.lower()}, budget)


def test_a_nested_lookup_past_its_bound_lets_the_outer_application_call_commit(keys, monkeypatch):
    mine, _ = keys
    _, vault, _ = seeded(mine)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    with vault.holding(scoped=True):
        created = vault.intern("API_KEY", EntityClass.SECRET, "AnotherNewSecret123456789")
        with vault.holding(scoped=True):
            vault.prefetch([MINIMAL])
    assert mine.by_original(created.original) == created  # kept: memory of none lets it go once the call ends


@pytest.mark.parametrize("ceiling", ["INTERIOR_WINDOWS", "LOOKUP_WORD_ROLES", "LOOKUP_CHARACTERS", "LOOKUP_BYTES"])
async def test_lookup_bounds_refuse_no_api_mask_scan_or_unmask(server, monkeypatch, ceiling):
    """Past each bound of Task 8's lookup an applications call is answered as 0.10.2 answered it (the owner's ruling of
    2026-10-29): masked, scanned and restored, never refused with 422."""
    key, secret = server.identities.add_key("Bounded API lookup")
    async with _calling(server) as client:
        await _masked(client, secret, [f"password={TOKEN}"])
        store = (await server.spaces.runtime(key)).store
        entity = store.by_original(TOKEN)
        assert entity is not None
        monkeypatch.setattr(lookup, ceiling, 1)
        for operation, text in (
            ("mask", f"{TOKEN}y"),
            ("scan", f"{TOKEN}y"),
            ("unmask", f"{entity.surrogate}y"),
        ):
            answer = await client.post(f"{APPS_API}/{operation}", json={"texts": [text]}, headers=_bearer(secret))
            assert answer.status_code == 200, (ceiling, operation, answer.text)
            if operation == "mask":
                assert TOKEN not in answer.text and entity.surrogate in answer.text
            elif operation == "unmask":
                assert answer.json()["results"][0]["text"] == f"{TOKEN}y"
