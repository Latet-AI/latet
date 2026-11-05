"""What a masker reads each text for (``Matchers.for_text``, the server performance plan's Task 7): a vault that holds
every value in memory gives its detectors whole, whatever the text; an API key's vault gives those of the few values its
detectors hold (held for good, and those no word of a text finds glued on both sides), and those the text's own words
find, its words read once, its lookups moving no version."""

import pytest

from latet.config import Config
from latet.engine import known as known_module
from latet.engine import stored
from latet.engine.anchors import EDGE, anchor
from latet.engine.build import build_masker
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import WORD_INDEX_FROM
from latet.engine.known import value_chars
from latet.engine.pipeline import Engine, Session
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_stored_vault import _email, _held, _many, _Store, _vault


def _engine(vault: Vault) -> Engine:
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    return Engine(vault, masker)


def _token(n: int, size: int = 24) -> str:
    """A token of ``size`` hexadecimal digits: one run, longer than a vault holds glued for good (16), so that no word
    of a text finds it glued on both sides."""
    return f"{(n * 0x9E3779B97F4A7C15 + 7) % (1 << 96):024x}"[:size]


def test_a_computers_vault_matches_as_before():
    """A vault that holds every value in memory (a computer's, a person's space) reads every text with its detectors
    whole: what it answers for a text is what it answers for any (``current``)."""
    vault = Vault(ROOT_KEY)
    vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@nordest.it")
    held = vault.matchers.current()
    assert vault.matchers.for_text("laura.bianchi@nordest.it wrote") is held
    assert vault.matchers.for_text("nothing of it here", pieces=["nothing", "of it here"]) is held


def test_a_stored_vault_matches_held_and_glued_values_alone_in_memory():
    """Of 500 values in a key's memory, its detectors hold the four no word of a text finds glued: three tokens of one
    run longer than ``EDGE`` (``glued_both``, past what a vault holds glued for good), and one without a word (held for
    good). The others are found by the words of each text that holds them."""
    store = _Store()
    vault = _vault(store, working=1_000)
    emails = [vault.intern("EMAIL", EntityClass.PII, _email(n)) for n in range(495)]
    tokens = [vault.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(n)) for n in range(3)]
    badge = vault.intern("BADGE", EntityClass.CUSTOM, "x9-k2")  # no anchor: held for good
    # An IBAN's fake is one run no word finds glued on both sides, its forms held for its restores; but no detector
    # finds a fake of personal data inside a word, and its word finds it where nothing is glued to it (review M3).
    iban = vault.intern("IBAN", EntityClass.PII, "IT60X0542811101000000123456", rule="pii:iban")
    assert stored.glued_both(iban, "surrogate") and iban.id in vault._held_forms._forms
    assert vault.count == 500 and all(_held(vault, e) for e in [*emails, *tokens, badge, iban])
    assert all(stored.glued_both(t, "original") and not stored.held_glued(t) for t in tokens)
    assert len(anchor(tokens[0].original) or "") > EDGE
    matched = {e.id for e in vault.entities() if vault.matched(e)}
    assert matched == {*(t.id for t in tokens), badge.id}
    vault.matchers.current()
    assert vault.matchers.chars == sum(value_chars(e) for e in (*tokens, badge))
    parts = vault.matchers.current().known
    glued = f"ref x{tokens[1].original}y and {emails[7].original}"
    assert {glued[s.start : s.end] for s in parts.detect(glued, DetectContext())} == {tokens[1].original}
    # The text's own reading finds the address too, by its words.
    found = vault.matchers.for_text(glued).known
    assert {glued[s.start : s.end] for s in found.detect(glued, DetectContext())} == {
        tokens[1].original,
        emails[7].original,
    }


def test_a_texts_words_are_read_once(monkeypatch):
    """A call reads each of its texts' words once (``anchors.words_and_windows``): to ask the store for its values, to
    read its values, its fakes sent back and what is left in clear in it, each text of the call once."""
    store = _Store()
    vault = _vault(store, working=1_000)
    engine = _engine(vault)
    _many(vault, 50)
    texts = [f"line {n}: {_email(n)} wrote to {_email(n + 1)}" for n in range(20)]
    read: list[str] = []
    real = stored.words_and_windows

    def counted(text: str, **kwargs):
        read.append(text)
        return real(text, **kwargs)

    monkeypatch.setattr(stored, "words_and_windows", counted)
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        masked = [engine.mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)).text for t in texts]
    assert sorted(read) == sorted(texts)
    assert not any(_email(n) in text for n, text in enumerate(masked))


def test_a_lookup_does_not_move_the_version():
    """What a lookup brings into memory (a value by itself, by its fake, by its id), what a call's words find, and what
    a text's reading does, moves no version: memory no longer vouches for a text, each check reads its words. A value
    its detectors hold (a token no word finds glued) moves it, as a value kept does; and a conversation's values held
    for its answer, brought back, move it, as its restores made without them are made again."""
    store = _Store()
    vault = _vault(store, working=100)
    old = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@nordest.it")
    other = vault.intern("EMAIL", EntityClass.PII, "ugo.neri84@nordest.it")
    third = vault.intern("EMAIL", EntityClass.PII, "anna.ferri@nordest.it")
    fourth = vault.intern("EMAIL", EntityClass.PII, "marco.ferretti@nordest.it")
    fifth = vault.intern("EMAIL", EntityClass.PII, "lucia.neri@nordest.it")
    token = vault.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(1))
    _many(vault, 300)
    assert not any(_held(vault, e) for e in (old, other, third, fourth, fifth, token))
    version = vault.version
    assert vault.by_original(old.original) is not None and _held(vault, old)
    assert vault.by_surrogate(other.surrogate) is not None and _held(vault, other)
    assert vault.get(third.id) is not None and _held(vault, third)
    assert vault.holds_since(0)
    with vault.holding():
        vault.prefetch([f"seen {fourth.original}"])
        text = f"seen {fifth.original}"
        reading = vault.matchers.for_text(text)
        assert [text[s.start : s.end] for s in reading.known.detect(text, DetectContext())] == [fifth.original]
        reading.seen()
    assert _held(vault, fourth) and _held(vault, fifth) and vault.version == version
    assert vault.get(token.id) is not None and vault.version == version + 1
    _many(vault, 300, start=1_000)
    version = vault.version
    vault.hold([old.id])
    assert _held(vault, old) and vault.version == version + 1


def test_a_call_asks_the_store_first_whether_values_kept_elsewhere_were_taken_in():
    """``KeyVaultStore.current``: a call (the outermost ``holding``) asks it once as it begins, and the version moves
    when the store took in values kept elsewhere (the plan's Task 8, its index caught up): a text masked before is
    checked again. A nested holding and a store that took in nothing move nothing."""
    store = _Store()
    vault = _vault(store)
    asked: list[bool] = []
    taken = [False, True]
    store.current = lambda: asked.append(True) or taken.pop(0)  # type: ignore[method-assign]
    version = vault.version
    with vault.holding(), vault.holding():
        pass
    assert asked == [True] and vault.version == version
    with vault.holding(scoped=True):
        pass
    assert asked == [True, True] and vault.version == version + 1


def test_a_text_of_more_values_than_an_alternation_takes_is_read_by_the_detector_compiled_for_them(monkeypatch):
    """Ruling R3-2: a text whose words find more values than ``WORD_INDEX_FROM`` (2,100 namesakes) is read by the
    detector of known values compiled for them, not with ``str.find`` (no bound on its cost): masked as a vault that
    holds every value masks it."""
    store = _Store()
    keeper = stored.StoredVault(ROOT_KEY, store, working=0, on_insert=store.save_entity)
    people = [keeper.intern("PERSON", EntityClass.NER, f"Nome{n} Lanzavecchia", rule="ner") for n in range(2_100)]
    made: list[str] = []
    real = known_module.TextValues.__init__

    def recorded(self, values, text, **kwargs):
        real(self, values, text, **kwargs)
        made.append("detector" if self._detector is not None else "plain" if self._plain is not None else "none")

    monkeypatch.setattr(known_module.TextValues, "__init__", recorded)
    vault = stored.StoredVault(ROOT_KEY, store, working=0, on_insert=store.save_entity)
    text = "ticket closed by Nome7 Lanzavecchia and Nome2077 Lanzavecchia"
    done = _engine(vault).mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert len(people) > WORD_INDEX_FROM and "detector" in made
    plain = Vault(ROOT_KEY, people)
    expected = _engine(plain).mask_text(text, SlotKind.TOOL_RESULT, Session("p", kept=False)).text
    assert done.text == expected == f"ticket closed by {people[7].surrogate} and {people[2077].surrogate}"


@pytest.mark.parametrize("pieces", [True, False], ids=["joined", "alone"])
def test_a_text_joined_of_texts_read_before_is_read_for_their_words(monkeypatch, pieces):
    """A part of an applications call read as one text (``Masker.joined``) is read for its texts' words, read once
    each, never for the part's again."""
    store = _Store()
    vault = _vault(store, working=1_000)
    _many(vault, 50)
    texts = [f"line {n}: {_email(n)} logged in" for n in range(10)]
    read: list[str] = []
    real = stored.words_and_windows
    monkeypatch.setattr(stored, "words_and_windows", lambda text, **kw: read.append(text) or real(text, **kw))
    joined = "\n\n".join(texts)
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        found = vault.matchers.for_text(joined, pieces=texts if pieces else None).known
        spans = list(found.detect(joined, DetectContext(joined=True)))
    assert {joined[s.start : s.end] for s in spans} == {_email(n) for n in range(10)}
    assert sorted(read) == sorted(texts if pieces else [*texts, joined])
