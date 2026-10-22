"""A list of terms is matched in parts, as the vault's values are: RE2 holds the GIL for the whole of a compile, and one
alternation of tens of thousands of long terms held every identity's event loop on a server for more than a second, at
each change of a person's settings and for each of her folders. Each part is compiled on its own, the GIL let go between
two, and every term is found as one alternation found it."""

import asyncio
import random
import string
import time

import pytest
import re2

from latet.engine.detectors import terms as terms_module
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import PART_CHARS, RE2_MEMORY, Term, TermsDetector
from latet.engine.masker import Masker
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_known_values import _Ticks


def _long_terms(count: int, seed: int) -> list[str]:
    """Terms of 60 characters that are not whole words (a word index cannot take them): the round-3 report's case."""
    rng = random.Random(seed)
    alphabet = string.ascii_lowercase + string.digits + "-"
    return list(dict.fromkeys("".join(rng.choice(alphabet) for _ in range(60)) for _ in range(count)))


def _found(detector: TermsDetector, text: str) -> set[str]:
    return {text[s.start : s.end] for s in detector.detect(text, DetectContext())}


@pytest.mark.timing
async def test_thirty_thousand_long_terms_hold_the_event_loop_for_tens_of_milliseconds_at_most():
    values = _long_terms(30_000, 1)
    terms = [Term(v, "CLIENT", whole_word=False) for v in values]
    async with _Ticks() as ticks:
        detector = await asyncio.to_thread(TermsDetector, terms, bounded=True)
    assert ticks.worst < 0.1, f"compiling the terms held the loop {ticks.worst * 1000:.0f} ms"  # 1.3 s as one
    sample = random.Random(2).sample(values, 400)
    text = " | ".join(sample)
    assert _found(detector, text) == set(sample)  # each found, whatever part it is in


def test_each_part_of_a_long_list_is_compiled_on_its_own_within_its_bound(monkeypatch):
    compiled: list[int] = []  # the bytes of each pattern RE2 compiles
    real = re2._Regexp

    class Counted(real):
        def __init__(self, pattern, options):
            compiled.append(len(pattern))
            super().__init__(pattern, options)

    monkeypatch.setattr(re2, "_Regexp", Counted)
    values = _long_terms(30_000, 3)
    TermsDetector([Term(v, "CLIENT", whole_word=False) for v in values], bounded=True)
    assert max(compiled) <= PART_CHARS  # every value's pattern is 1.8 MB
    assert sum(compiled) >= sum(len(v) for v in values)  # every term, in one part or another
    compiled.clear()
    short = values[:400]  # about 24 KiB, past half a part: never cut where a first character changes
    TermsDetector([Term(v, "CLIENT") for v in short])
    assert len(compiled) == 1 and compiled[0] > PART_CHARS // 2  # one alternation, matched as it always was


def test_a_long_list_in_parts_scans_about_as_fast_as_one_alternation(monkeypatch):
    """Parts cut in order share a first character, which RE2 skips to: cut in the order given, the same parts read a
    text at every place each, thirty times slower than one alternation."""
    values = _long_terms(30_000, 4)
    terms = [Term(v, "CLIENT", whole_word=False) for v in values]
    parted = TermsDetector(terms, bounded=True)
    monkeypatch.setattr(terms_module, "PART_CHARS", 1 << 30)
    whole = TermsDetector(terms, bounded=True)
    rng = random.Random(5)
    words = [rng.choice(values) if rng.random() < 0.02 else "".join(rng.choices(string.ascii_lowercase, k=7))
             for _ in range(12_500)]  # fmt: skip
    text = " ".join(words)  # about 100 KB

    def fastest(detector):
        took = []
        for _ in range(3):
            started = time.perf_counter()
            found = _found(detector, text)
            took.append(time.perf_counter() - started)
        return found, min(took)

    (in_parts, parts_took), (in_one, one_took) = fastest(parted), fastest(whole)
    assert in_parts == in_one and len(parted._set._matchers) > 30
    assert parts_took < 3 * one_took + 0.05, f"{parts_took:.3f} s in parts, {one_took:.3f} s as one"
    for matcher in parted._set._matchers:  # each part half full or more shares its first character
        values = [plain[0] for plain in matcher._lookup.values()]
        if sum(len(v) + 1 for v in values) >= PART_CHARS // 2:
            assert len({v[:1].casefold() for v in values}) == 1


def test_a_computers_list_in_parts_gives_each_part_a_budget_sized_to_it():
    """One alternation on a computer may use RE2_MEMORY; parts, many of them, each a budget sized to it."""
    detector = TermsDetector([Term(v, "CLIENT", whole_word=False) for v in _long_terms(30_000, 6)])
    assert len(detector._set._matchers) > 30
    assert all(matcher.memory < RE2_MEMORY for matcher in detector._set._matchers)
    assert detector.memory < 4 * RE2_MEMORY


def test_a_term_longer_than_a_part_is_a_part_of_its_own_and_is_found():
    long = "x" * (PART_CHARS + 10) + " Falcon"
    detector = TermsDetector([Term("Project Falcon"), Term(long, whole_word=False), Term("Heron Labs")], bounded=True)
    text = f"Project Falcon met Heron Labs, then {long}."
    assert _found(detector, text) == {"Project Falcon", "Heron Labs", long}


def test_terms_in_two_parts_overlapping_in_a_text_are_masked_as_one_alternation_masks_them(monkeypatch):
    monkeypatch.setattr(terms_module, "PART_CHARS", 1_024)  # small parts: the two terms land in different ones
    filler = [f"Zoë filler {n:05d}" for n in range(200)]  # in order between the two
    terms = [Term("Zoë Smith", "CLIENT"), *(Term(f) for f in filler), Term("Zoë", "PERSON")]
    detector = TermsDetector(terms)
    parts = [{plain[0] for plain in matcher._lookup.values()} for matcher in detector._set._matchers]
    assert len(parts) > 2 and not any({"Zoë", "Zoë Smith"} <= part for part in parts)  # in parts of their own
    masker = Masker(Vault(ROOT_KEY), [detector])
    masked = masker.mask("ask Zoë Smith, and Zoë Smithson too", SlotKind.USER_TEXT)
    assert [r.original for r in masked.replacements] == ["Zoë Smith", "Zoë"]  # the longest at a place, whole words
    assert [r.etype for r in masked.replacements] == ["CLIENT", "PERSON"]
