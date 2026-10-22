"""The term matcher against its meaning, written slowly: from the left, at each place the longest term found there
whose word edges are free. Both ways of matching must find it, in several alphabets: one RE2 alternation (its word
edges checked in Python, as RE2's \\b knows ASCII only) and the word index."""

from hypothesis import given
from hypothesis import strategies as st

from latet.engine.detectors.terms import _WORDS, Term, _Alternation, _WordIndex
from latet.engine.detectors.terms import _word_char as _word

ASCII = "ab C-+_ .1"
WORLD = "abéòÉZ -+_.1"
# Letters and signs of one to four bytes in UTF-8, and a combining accent, none with another case: the alternation runs
# over the text's UTF-8.
WIDE = "aé中 -€" + chr(0x1D400) + chr(0x1F600) + chr(0x0301)  # a bold A, a smiling face, a combining acute accent


def meaning(terms: list[Term], text: str, whole_word: bool = True) -> list[tuple[int, int]]:
    out, at = [], 0
    while at < len(text):
        best = None
        for term in terms:
            value, end = term.value, at + len(term.value)
            if end > len(text) or text[at:end].casefold() != value.casefold():
                continue
            if whole_word and (
                (_word(value[0]) and at > 0 and _word(text[at - 1]))
                or (_word(value[-1]) and end < len(text) and _word(text[end]))
            ):
                continue
            best = max(best or end, end)
        out += [(at, best)] if best is not None else []
        at = best if best is not None else at + 1
    return out


def _values(alphabet: str):
    return st.lists(st.text(alphabet, min_size=1, max_size=5).filter(str.strip), min_size=1, max_size=6, unique=True)


@given(_values(WORLD), st.text(WORLD, max_size=40))
def test_the_word_index_finds_what_the_meaning_says(values, text):
    terms = [Term(v) for v in values if _WORDS.search(v)]
    assert [(s, e) for s, e, _ in _WordIndex(terms, case_sensitive=False).finditer(text)] == meaning(terms, text)


@given(_values(WIDE), st.text(WIDE, max_size=40))
def test_the_word_index_finds_what_the_meaning_says_whatever_the_width_of_the_characters(values, text):
    terms = [Term(v) for v in values if _WORDS.search(v)]
    assert [(s, e) for s, e, _ in _WordIndex(terms, case_sensitive=False).finditer(text)] == meaning(terms, text)


@given(_values(WORLD), st.text(WORLD, max_size=40))
def test_the_alternation_finds_what_the_meaning_says(values, text):
    terms = [Term(v) for v in values]
    found = _Alternation(terms, case_sensitive=False, whole_word=True).finditer(text)
    assert [(s, e) for s, e, _ in found] == meaning(terms, text)


@given(_values(WIDE), st.text(WIDE, max_size=40), st.booleans())
def test_the_alternation_finds_what_the_meaning_says_whatever_the_width_of_the_characters(values, text, whole_word):
    terms = [Term(v) for v in values]
    found = _Alternation(terms, case_sensitive=True, whole_word=whole_word).finditer(text)
    assert [(s, e) for s, e, _ in found] == meaning(terms, text, whole_word)
