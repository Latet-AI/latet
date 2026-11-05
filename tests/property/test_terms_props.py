"""The term matcher against its meaning, written slowly: the terms found from the left, at each place the longest one,
one after the other (what 0.9.8 found), are found; and with them, what the terms that overlap them hold of more than
blanks, so that masking covers every place a term is, its word edges free. Both ways of matching must find it, in
several alphabets: one RE2 alternation (its word edges checked in Python, as RE2's \\b knows ASCII only) and the word
index."""

from hypothesis import given
from hypothesis import strategies as st

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import (
    _WORDS,
    KnownValuesDetector,
    NotPlain,
    PlainTerms,
    Term,
    _Alternation,
    _WordIndex,
)
from latet.engine.detectors.terms import _word_char as _word
from latet.engine.types import EntityClass

ASCII = "ab C-+_ .1"
WORLD = "abéòÉZ -+_.1"
# Letters and signs of one to four bytes in UTF-8, and a combining accent, none with another case: the alternation runs
# over the text's UTF-8.
WIDE = "aé中 -€" + chr(0x1D400) + chr(0x1F600) + chr(0x0301)  # a bold A, a smiling face, a combining acute accent


def _free(value: str, text: str, at: int, end: int) -> bool:
    return not (
        (_word(value[0]) and at > 0 and _word(text[at - 1]))
        or (_word(value[-1]) and end < len(text) and _word(text[end]))
    )


def places(terms: list[Term], text: str, whole_word: bool = True) -> set[tuple[int, int]]:
    """Every place a term is in ``text``, overlaps included."""
    out = set()
    for at in range(len(text)):
        for term in terms:
            value, end = term.value, at + len(term.value)
            if (
                end <= len(text)
                and text[at:end].casefold() == value.casefold()
                and (not whole_word or _free(value, text, at, end))
            ):
                out.add((at, end))
    return out


def one_after_the_other(terms: list[Term], text: str, whole_word: bool = True) -> list[tuple[int, int]]:
    """From the left, at each place the longest term found there, then on from its end."""
    out, at = [], 0
    while at < len(text):
        ends = [end for start, end in places(terms, text, whole_word) if start == at]
        best = max(ends) if ends else None
        out += [(at, best)] if best is not None else []
        at = best if best is not None else at + 1
    return out


def _covered(found: set[tuple[int, int]]) -> set[int]:
    return {i for start, end in found for i in range(start, end)}


def _check(found: list[tuple[int, int]], terms: list[Term], text: str, whole_word: bool = True) -> None:
    every = places(terms, text, whole_word)
    assert set(one_after_the_other(terms, text, whole_word)) <= set(found)  # never less than 0.9.8 found
    assert all(any(s <= start and end <= e for s, e in every) for start, end in found)  # each within a term
    letters = {i for i in _covered(every) if not text[i].isspace()}
    assert letters <= _covered(set(found))  # all together cover every term, overlaps included, but blanks between


@st.composite
def _case(draw: st.DrawFn, alphabet: str) -> tuple[list[str], str]:
    """Terms over ``alphabet``, and a text of pieces of them and of the alphabet: terms meet and overlap in it often
    (a random text alone holds few)."""
    pick = st.text(alphabet, min_size=1, max_size=5).filter(str.strip)
    values = draw(st.lists(pick, min_size=1, max_size=6, unique=True))
    pieces = st.one_of(
        st.sampled_from(values),
        st.sampled_from(values).flatmap(lambda v: st.sampled_from([v[i:] for i in range(1, len(v))] or [v])),
        st.text(alphabet, max_size=3),
    )
    return values, "".join(draw(st.lists(pieces, max_size=10)))[:40]


@given(st.one_of(_case(WORLD), st.tuples(_case(WORLD).map(lambda c: c[0]), st.text(WORLD, max_size=40))))
def test_the_word_index_finds_every_place_of_a_term(case):
    values, text = case
    terms = [Term(v) for v in values if _WORDS.search(v)]
    _check([(s, e) for s, e, _ in _WordIndex(terms, case_sensitive=False).finditer(text)], terms, text)


@given(_case(WIDE))
def test_the_word_index_finds_every_place_of_a_term_whatever_the_width_of_the_characters(case):
    values, text = case
    terms = [Term(v) for v in values if _WORDS.search(v)]
    _check([(s, e) for s, e, _ in _WordIndex(terms, case_sensitive=False).finditer(text)], terms, text)


@given(st.one_of(_case(WORLD), st.tuples(_case(WORLD).map(lambda c: c[0]), st.text(WORLD, max_size=40))))
def test_the_alternation_finds_every_place_of_a_term(case):
    values, text = case
    terms = [Term(v) for v in values]
    _check(
        [(s, e) for s, e, _ in _Alternation(terms, case_sensitive=False, whole_word=True).finditer(text)], terms, text
    )


@given(_case(WIDE), st.booleans())
def test_the_alternation_finds_every_place_of_a_term_whatever_the_width_of_the_characters(case, whole_word):
    values, text = case
    terms = [Term(v) for v in values]
    found = _Alternation(terms, case_sensitive=True, whole_word=whole_word).finditer(text)
    _check([(s, e) for s, e, _ in found], terms, text, whole_word)


# The values a text's repeats are looked for by: a person's name (whole words, from three letters), a secret (inside a
# word too), and others; the same string may come twice, with another type (the later one names it).
PLAIN = "abAB12.@-_ ò"
_KINDS = [
    ("PERSON", EntityClass.NER),
    ("API_KEY", EntityClass.SECRET),
    ("EMAIL", EntityClass.PII),
    ("DOMAIN", EntityClass.PII),
]


@st.composite
def _values_and_text(draw: st.DrawFn) -> tuple[list[tuple[str, str, EntityClass]], str]:
    strings = draw(st.lists(st.text(PLAIN, min_size=3, max_size=7).filter(str.strip), min_size=1, max_size=6))
    values = [(value, *draw(st.sampled_from(_KINDS))) for value in strings]
    pieces = st.one_of(
        st.sampled_from(strings),
        st.sampled_from(strings).flatmap(lambda v: st.sampled_from([v[i:] for i in range(1, len(v))] or [v])),
        st.sampled_from(strings).flatmap(lambda v: st.sampled_from([v[:i] for i in range(1, len(v))] or [v])),
        st.text(PLAIN, max_size=3),
    )
    return values, "".join(draw(st.lists(pieces, max_size=12)))[:60]


@given(_values_and_text())
def test_plain_terms_find_what_known_values_find(case):
    values, text = case
    known = list(KnownValuesDetector(values, rule="repeat").detect(text, DetectContext()))
    plain = PlainTerms(values, rule="repeat")
    try:
        strict = plain.strict_spans(text)
    except NotPlain:
        strict = None  # a value not ASCII found, or two that overlap: the masker asks KnownValuesDetector there
    if strict is not None:
        assert strict == known  # the same spans, in the same order
    assert _covered({(s.start, s.end) for s in known}) <= _covered({(s.start, s.end) for s in plain.spans(text)})


@given(_values_and_text(), st.data())
def test_plain_terms_find_what_known_values_find_with_strings_found_whole(case, data):
    # ``whole``: strings found only as whole words, a secret's too (a vault's short fakes, ``known._Part``).
    values, text = case
    whole = set(data.draw(st.lists(st.sampled_from([value for value, _, _ in values]), max_size=3)))
    known = list(KnownValuesDetector(values, rule="repeat", whole=whole).detect(text, DetectContext()))
    plain = PlainTerms(values, rule="repeat", whole=whole)
    try:
        strict = plain.strict_spans(text)
    except NotPlain:
        strict = None
    if strict is not None:
        assert strict == known
    assert _covered({(s.start, s.end) for s in known}) <= _covered({(s.start, s.end) for s in plain.spans(text)})
