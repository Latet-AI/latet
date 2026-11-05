"""Known secrets inside longer runs must reach masking after working-memory eviction."""

import contextlib
import json
from dataclasses import replace

import pytest

from latet.engine.detectors.pii import PiiDetector
from latet.engine.masker import Masker
from latet.engine.pipeline import Engine, Session
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass, Policy, SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_stored_vault import _Store

TOKEN = "Kq7mZ2xW9pL4vR8tN3bY"
MINIMAL = f"Niccol{TOKEN}D%27Angelo@acme.it"
EXACT = "pw5Kz\\ud83eabglvqhv\\'" + MINIMAL + "-mario.rossipjlhi5"
DATA = SlotKind.TOOL_RESULT


def seeded(store=None, *, value=TOKEN, working=0, glued_all=False):
    source = Vault(ROOT_KEY)
    entity = source.intern("API_KEY", EntityClass.SECRET, value)
    if store is None:
        store = _Store()
    store.save_entity(entity)
    vault = StoredVault(ROOT_KEY, store, working=working, held_at_most=0, glued_all=glued_all)
    return source, vault, entity


def masker(vault, policy=Policy.ALLOW):
    return Masker(vault, [PiiDetector()], policies={"EMAIL": policy})


def positions(done):
    return {i for r in done.replacements if r.policy != Policy.ALLOW for i in range(r.start, r.end)}


def round_trips(vault, original, masked):
    for text, expected, json_mode in (
        (masked, original, False),
        (json.dumps(masked), json.dumps(original), True),
    ):
        with vault.holding():
            vault.prefetch_fakes([text])
            back, _ = restore_text(
                text, restores_for(vault.candidates_in(text), parts=False, whole_vault=True, json_mode=json_mode)
            )
        assert back == expected


@pytest.mark.parametrize(("text", "start"), [(MINIMAL, 0), (EXACT, 21)])
@pytest.mark.parametrize("path", ["masker", "engine"])
@pytest.mark.parametrize("scoped", [False, True], ids=["gateway", "apps"])
def test_an_evicted_secret_in_an_allowed_email_masks_the_whole_overlap(text, start, path, scoped):
    """Missing interior edges previously let all 44 characters of the allowed email through."""
    plain, stored, _ = seeded()
    results = []
    for vault in (plain, stored):
        reader = masker(vault)
        with vault.holding(scoped=scoped):
            vault.prefetch([text])
            done = (
                reader.mask(text, DATA)
                if path == "masker"
                else Engine(vault, reader).mask_text(text, DATA, Session("interior"))
            )
        assert positions(done) == set(range(start, start + 44))
        round_trips(vault, text, done.text)
        results.append(done.text)
    assert results[0] == results[1]


@pytest.mark.parametrize("size", [7, 16, 20, 40])
@pytest.mark.parametrize("joined", [False, True])
@pytest.mark.parametrize("policy", [Policy.ALLOW, Policy.MASK])
def test_interior_edges_cover_secret_lengths_and_joined_calls(size, joined, policy):
    value = (TOKEN * 2)[:size]
    text = f"Niccol{value}D%27Angelo@acme.it"
    plain, stored, _ = seeded(value=value)
    expected = None
    for vault in (plain, stored):
        reader = masker(vault, policy)
        texts = ["ordinary text", text, "another ordinary line"]
        with vault.holding(scoped=True):
            vault.prefetch(texts)
            with reader.joined(texts, DATA) if joined else contextlib.nullcontext():
                done = [reader.mask(part, DATA) for part in texts]
        assert positions(done[1]) == set(range(len(text)))
        assert done[0].text == texts[0] and done[2].text == texts[2]
        round_trips(vault, text, done[1].text)
        if expected is None:
            expected = done[1].text
        assert done[1].text == expected


@pytest.mark.parametrize("working", [0, 1])
@pytest.mark.parametrize("glued_all", [False, True])
def test_hot_engine_cache_and_eviction_keep_the_interior_secret(working, glued_all):
    plain, vault, entity = seeded(working=working, glued_all=glued_all)
    reader = masker(vault)
    engine = Engine(vault, reader)
    expected = masker(plain).mask(MINIMAL, DATA).text
    assert engine.mask_text(MINIMAL, DATA, Session("first")).text == expected
    assert engine.mask_text(MINIMAL, DATA, Session("hot")).text == expected
    for value in ("LaterValueOne1234567890", "LaterValueTwo1234567890"):
        vault.intern("API_KEY", EntityClass.SECRET, value)
    if not glued_all:
        assert vault.in_memory(entity.id) is None
    assert engine.mask_text(MINIMAL, DATA, Session("after-eviction")).text == expected


@pytest.mark.parametrize(
    "text",
    [
        f"éx{TOKEN}y中",
        f"before.x{TOKEN}y-after",
        f"x{TOKEN[:6]}\\u0078{TOKEN[7:]}y",
        "x" + "".join(f"%{ord(c):02X}" for c in TOKEN) + "y",
        "x" + "".join(f"&#{ord(c)};" for c in TOKEN) + "y",
    ],
)
def test_interior_lookup_uses_the_maskers_escape_and_unicode_readings(text):
    plain, vault, _ = seeded()
    answers = []
    for current in (plain, vault):
        reader = masker(current)
        with current.holding():
            current.prefetch([text])
            done = reader.mask(text, DATA)
        assert positions(done)
        round_trips(current, text, done.text)
        answers.append((done.text, positions(done)))
    assert answers[0] == answers[1]


def test_unrelated_allowed_email_and_colliding_edges_are_unchanged():
    _, vault, _ = seeded(value="AAAAAA1UnrelatedSecretZZZZZZ")
    text = "contact xAAAAAA2OtherValueZZZZZZy@example.org"
    with vault.holding(scoped=True):
        vault.prefetch([text])
        done = masker(vault).mask(text, DATA)
    assert done.text == text and not positions(done)


def test_a_later_text_selects_its_interior_candidates_after_prefetch():
    """Candidates fetched by read_for must survive its per-text word selection, before joining memory."""
    _, vault, _ = seeded()
    with vault.holding():
        vault.prefetch(["ordinary text"])
        done = masker(vault).mask(MINIMAL, DATA)
    assert positions(done) == set(range(44))


def test_a_found_old_short_fake_keeps_its_whole_vault_restore_boundary():
    source = Vault(ROOT_KEY)
    old = replace(source.intern("API_KEY", EntityClass.SECRET, "hunter2"), surrogate="Ab7cD9eF")
    store = _Store()
    store.save_entity(old)
    vault = StoredVault(ROOT_KEY, store, working=0, held_at_most=0)
    text = f"x{old.surrogate}y"
    with vault.holding():
        vault.prefetch_fakes([text])
        candidates = vault.candidates_in(text)
        assert old in candidates
        assert restore_text(text, restores_for(candidates, parts=False, whole_vault=True))[0] == text
        assert restore_text(text, restores_for(candidates, parts=False, whole_vault=False))[0] == "xhunter2y"


def test_lookup_budget_is_cumulative_across_nested_calls_and_fakes(monkeypatch):
    """One budget for the outer call and every nested one, values and fakes alike. Past it the nested fake lookup reads
    as 0.10.2 asked (Task 8's fix round 6: never a refusal), and finds the fake."""
    from latet.engine import lookup

    _, vault, entity = seeded()
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 2)
    with vault.holding():
        vault.prefetch([TOKEN])  # one returned candidate and its weighing exactly fit
        outer = vault._call().lookup
        assert not outer.past
        with vault.holding():
            assert vault._call().lookup is outer
            vault.prefetch_fakes([entity.surrogate])  # another role shares the same budget: past it
            assert outer.past
            assert entity.id in {e.id for e in vault.candidates_in(entity.surrogate)}
    # The next request has its own budget, and the secret is still there.
    with vault.holding():
        assert not vault._call().lookup.past
        vault.prefetch([TOKEN])


def test_interior_window_budget_counts_repeated_positions_before_building_them(monkeypatch):
    """Positions are counted before windows are made, repeated ones too; past the bound a run has no windows, and its
    words are asked all the same (never a refusal)."""
    from latet.engine import lookup

    _, vault, _ = seeded()
    monkeypatch.setattr(lookup, "INTERIOR_WINDOWS", 2)
    with vault.holding():
        vault.prefetch(["aaaaaaaaa"])  # two interior positions, one distinct six-character word
        budget = vault._call().lookup
        assert budget.windows == 2 and not budget.windows_off
        vault.prefetch(["aaaaaaaaa"])  # cached reading costs no new windows
        assert budget.windows == 2
        vault.prefetch(["bbbbbbbb"])  # one more position: past the bound
        assert budget.windows == 2 and budget.windows_off and not budget.past
        assert {"bbbbbbbb", "bbbbbb"} <= vault._call().asked


def test_lookup_word_roles_share_a_call_budget_and_past_it_read_as_0_10_2_did(monkeypatch):
    """The word roles of every lookup of a call share one bound; past it the call asks its words as 0.10.2 asked them
    (``with_words``), never refused."""
    from latet.engine import lookup

    store = _Store()
    _, vault, _ = seeded(store)
    monkeypatch.setattr(lookup, "LOOKUP_WORD_ROLES", 2)
    with vault.holding(scoped=True):
        vault.prefetch(["cat"])  # one word, two roles: the bound, exactly
        budget = vault._call().lookup
        assert budget.words == 2 and not budget.past
        before = len(store.calls)
        vault.prefetch(["dog"])
        assert budget.past and store.calls[before:] == ["with_words:original+surrogate"]


def test_candidate_payload_budget_never_matches_a_collision_past_it(monkeypatch):
    """Past the characters' bound the call reads on (never refused, never truncated), and a colliding edge still masks
    nothing."""
    from latet.engine import lookup

    _, vault, entity = seeded(value="AAAAAA1UnrelatedSecretZZZZZZ")
    size = sum(len(s) for s in (entity.id, entity.etype, entity.original, entity.surrogate, entity.rule or ""))
    monkeypatch.setattr(lookup, "LOOKUP_CHARACTERS", size * 2)
    with vault.holding():
        vault.prefetch(["xAAAAAA2OtherValuey"])  # returned collision and weighing fit, but must not match
        done = masker(vault).mask("xAAAAAA2OtherValuey", DATA)  # the per-text selection is past the bound
        assert vault._call().lookup.past
    assert done.text == "xAAAAAA2OtherValuey" and not positions(done)


def test_memory_store_reproduces_the_same_mask_and_restoration():
    from scripts.vault_fuzz import MemoryStore

    plain, vault, _ = seeded(MemoryStore())
    expected = masker(plain).mask(MINIMAL, DATA)
    engine = Engine(vault, masker(vault))
    done = engine.mask_text(MINIMAL, DATA, Session("memory-store"))
    assert done.text == expected.text and positions(done) == set(range(44))
    round_trips(vault, MINIMAL, done.text)


def test_a_thousand_log_lines_with_a_thousand_held_tokens_match_the_plain_vault():
    from scripts.bench_apps import lines
    from scripts.vault_fuzz import MemoryStore

    from latet.engine.detectors.base import DetectContext
    from latet.engine.escapes import read_escaped
    from latet.engine.merge import merge_spans
    from latet.engine.stored import held_glued

    texts = lines(1000, 7301, 0.5, tokens=True)
    plain = Vault(ROOT_KEY)
    reader = Masker(plain, [PiiDetector()])
    for text in texts:
        reader.mask(text, DATA)
        plain.intern("API_KEY", EntityClass.SECRET, text.rsplit("session_token=", 1)[1])
    store = MemoryStore()
    for entity in plain._by_id.values():
        store.save_entity(entity)
    vault = StoredVault(ROOT_KEY, store, working=1000, held_at_most=1000)
    assert sum(held_glued(entity) for entity in vault._by_id.values()) == 1000
    ctx = DetectContext()

    def spans(text, reading):
        return set(read_escaped(text, lambda part: list(reading.known.detect(part, ctx)))) | set(
            read_escaped(text, lambda part: list(reading.echo.detect(part, ctx)))
        )

    # Cold and repeated calls must complete with the same masking groups. The full vault can also report a
    # contained domain that the per-text selector omits; source-pinned real masking/restore comparisons cover it.
    for _ in range(2):
        with vault.holding(scoped=True):
            vault.prefetch(texts)
            for text in texts:
                reading = vault.matchers.for_text(text)
                found = spans(text, reading)
                assert merge_spans(found, text) == merge_spans(spans(text, plain.matchers.for_text(text)), text)
                assert any(span.etype == "API_KEY" and span.of(text) == text[-16:] for span in found)
                reading.seen()


def test_fake_lookup_without_an_outer_holding_reads_past_its_payload_bound(monkeypatch):
    from latet.engine import lookup

    _, vault, entity = seeded()
    monkeypatch.setattr(lookup, "LOOKUP_CHARACTERS", 1)
    assert entity.id in {e.id for e in vault.candidates_in(f"see {entity.surrogate}.")}


def test_standalone_prefetch_shares_its_window_budget_across_all_texts(monkeypatch):
    from latet.engine import lookup, stored

    _, vault, _ = seeded()
    monkeypatch.setattr(lookup, "INTERIOR_WINDOWS", 2)
    seen = []
    real = stored.words_and_windows

    def spied(text, **kwargs):
        plain, windows = real(text, **kwargs)
        seen.append((text, windows))
        return plain, windows

    monkeypatch.setattr(stored, "words_and_windows", spied)
    vault.prefetch(["abcdefghi", "jklmnopqr"])  # no outer call: one budget for both texts
    assert seen == [("abcdefghi", {"bcdefg", "cdefgh"}), ("jklmnopqr", set())]


def test_standalone_fake_fetch_and_selection_share_one_budget(monkeypatch):
    from latet.engine import lookup

    _, vault, entity = seeded()
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
    assert entity.id in {e.id for e in vault.candidates_in(f"see {entity.surrogate}.")}  # past it: read on


def test_new_text_selections_share_the_budget_even_when_candidates_are_cached(monkeypatch):
    from latet.engine import lookup

    _, vault, _ = seeded(working=1)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 3)
    with vault.holding():
        vault.prefetch([MINIMAL])  # fetch and weighing
        budget = vault._call().lookup
        vault.matchers.for_text(MINIMAL)  # one retained text matcher
        charged = budget.candidates
        vault.matchers.for_text(MINIMAL)  # the same cached matcher adds no work or retention
        assert budget.candidates == charged and not budget.past
        vault.matchers.for_text(MINIMAL + " ")  # a different matcher is charged: past the bound, read on
        assert budget.candidates > charged and budget.past


def test_a_call_past_its_bounds_finishes_with_its_cached_reads_and_restores(monkeypatch):
    """No bound refuses a call (Task 8's fix round 6): past one, the call keeps its cached readings, reads on as the
    release before read, and ends without an error."""
    from latet.engine import lookup

    _, vault, entity = seeded(working=1)
    monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 3)
    with vault.holding():
        vault.prefetch([MINIMAL])
        cached = vault.matchers.for_text(MINIMAL)
        vault.prefetch_fakes([entity.surrogate])
        assert vault._call().lookup.past
        assert vault.matchers.for_text(MINIMAL) is cached
        assert entity.id in {e.id for e in vault.candidates_in(entity.surrogate)}


def test_multiple_overlapping_known_secrets_keep_the_same_allowed_email_group():
    store = _Store()
    plain, vault, _ = seeded(store)
    second = plain.intern("API_KEY", EntityClass.SECRET, TOKEN[8:] + "D")
    store.save_entity(second)
    expected = masker(plain).mask(MINIMAL, DATA)
    with vault.holding():
        vault.prefetch([MINIMAL])
        done = masker(vault).mask(MINIMAL, DATA)
    assert positions(done) == set(range(44)) and done.text == expected.text
    round_trips(vault, MINIMAL, done.text)
