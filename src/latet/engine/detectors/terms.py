"""Exact-term detectors: the user's own deny-list, the local identity (OS user, host, git author),
and every value already in the vault (so a value detected once is masked everywhere)."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace

import re2

from latet.engine.detectors.base import DetectContext, quiet_options
from latet.engine.surrogates.data.names import PUBLIC_MAIL_DOMAINS
from latet.engine.types import EntityClass, Span


@dataclass(frozen=True, slots=True)
class Term:
    value: str
    etype: str = "CUSTOM"
    case_sensitive: bool = False
    whole_word: bool = True


# A term as its matchers hold it: its value, type, case and whole words, a tuple of texts and flags. The garbage
# collector stops following such a tuple once it has seen it (nothing in it refers to an object), so that a server's
# every masker of tens of thousands of terms costs its collections no visit; an object for each term (a Term, the word
# index's record of it) would be visited at every full collection, in every masker of every space.
_Plain = tuple[str, str, bool, bool]
_VALUE, _ETYPE = 0, 1


def _plain(term: Term) -> _Plain:
    return (term.value, term.etype, term.case_sensitive, term.whole_word)


def has_letter_or_digit(value: str) -> bool:
    """Whether ``value`` has a letter or a digit, of any script. A fake keeps the signs of a value, so signs alone
    ("--", "###", "+") would be their own fake: a term of them is never looked for (every way of adding one refuses
    it), and such values are normally not masked. Latet route keys have a fully random fake instead:
    their type is explicitly retained by callers of this character test."""
    return any(c.isalpha() or c.isdecimal() for c in value)


def _word_char(ch: str) -> bool:
    """A letter or digit of any script, an underscore, or a mark on a letter. RE2's ``\\b`` knows ASCII letters only:
    with it, "Nicolò" was never found, and "Zoë" was found inside "Zoëlla"."""
    return ch == "_" or ch.isalnum() or unicodedata.category(ch).startswith("M")


def _whole(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` stands as a word of its own: no letter or digit runs on into an edge of it that is
    one (a value that begins or ends with a sign, such as ``#ops`` or ``acme.``, has no boundary there)."""
    if _word_char(text[start]) and start > 0 and _word_char(text[start - 1]):
        return False
    return not (_word_char(text[end - 1]) and end < len(text) and _word_char(text[end]))


# More terms than this in one list are looked up word by word (a dictionary lookup for each word of the text) instead
# of one RE2 alternation: past a few thousand terms the alternation outgrows RE2's memory, a scan then takes seconds per
# 100 KB, and 50,000 terms do not compile at all (scripts/terms_bench.py).
WORD_INDEX_FROM = 2000
# The memory one RE2 alternation of terms may use: RE2's default, 8 MB, is outgrown from about a thousand terms.
RE2_MEMORY = 64 << 20
# A bounded alternation's (a project's masker in an identity's space on a server, one of many in its process): RE2 may
# use this much for each byte of its pattern, at least BOUNDED_SMALLEST, at most RE2_MEMORY. Its program takes 14 to 28
# bytes for each byte of the pattern: what is left is the cache of its automaton, which grows as it scans and is kept
# within the rest (a scan whose automaton outgrows it goes on more slowly, never with a term less). One that does not
# compile within it is given twice as much, up to RE2_MEMORY.
BOUNDED_PER_BYTE = 64
BOUNDED_SMALLEST = 1 << 20
# A list of terms larger than this, in bytes of its pattern, is matched in parts of at most as much, each an alternation
# compiled on its own under a budget sized to it (as a vault's values are, ``latet.engine.known``): RE2 holds the GIL
# for the whole of a compile, and one alternation of 30,000 terms of 60 characters held it 1.3 s, on a server every
# identity's event loop with it, at each change of a person's settings and for each of her folders. A part compiles in
# a few tens of milliseconds, the GIL let go between two; what the parts find in one text is merged as any detectors'
# findings are (``merge_spans``: the longest at a place names it). A term longer than a part is one of its own, compiled
# alone: a server keeps none longer than ``TERM_LONGEST``.
PART_CHARS = 32 * 1024
# The most characters a term holds where a server keeps settings (the shared ones, an identity's, a project's): one so
# long fits a part whatever its characters (four bytes each at most). A longer one is compiled alone, holding the GIL
# longer than a part, and one of about a million characters does not compile within RE2_MEMORY at all.
TERM_LONGEST = PART_CHARS // 4
# The longest value a vault keeps (``Masker``): a PEM key of RSA 16,384 bits (about 12,600 characters), a certificate, a
# token stay under it. A longer one is redacted whatever its type's policy (but one let through), never kept nor
# restored: kept, it would weigh 40 bytes a character in every space of a Latet server that knows it, and be kept,
# sealed, in PostgreSQL. The local reviewer reads a text in pieces no longer (``LlmReview.chunk_chars``), so that
# whatever it finds in one is kept.
VALUE_LONGEST = 16_384

_WORDS = re.compile(r"\w+")  # letters and digits of every alphabet, and _: the words of a whole-word term


def _whole_utf8(data: bytes, start: int, end: int) -> bool:
    """`_whole` for byte offsets in a text's UTF-8. A character takes four bytes at most: the four on each side of the
    match hold its neighbours."""
    found = data[start:end].decode()
    before = data[max(0, start - 4) : start].decode(errors="ignore")[-1:]
    after = data[end : end + 4].decode(errors="ignore")[:1]
    return _whole(before + found + after, len(before), len(before) + len(found))


class _Offsets:
    """Offsets in a text from offsets in its UTF-8, asked for mostly in increasing order: each piece is decoded once,
    and again where a term found starts before the end of the one before it (an overlap, as long as a term at most)."""

    def __init__(self, text: str, data: bytes) -> None:
        self._data = data
        self._ascii = len(data) == len(text)  # the same offsets
        self._byte = self._char = 0

    def __call__(self, byte: int) -> int:
        if self._ascii:
            return byte
        if byte < self._byte:
            self._char -= len(self._data[byte : self._byte].decode())
        else:
            self._char += len(self._data[self._byte : byte].decode())
        self._byte = byte
        return self._char


# Terms that overlap (`Anna Maria`, `Maria Rossi` in `Anna Maria Rossi`) are each found, so that masking covers the
# whole of them: an alternation is searched again from the place after each term's start, not from its end. A search
# that starts inside a term found already looks for an overlap, which the terms one after the other would not need: what
# such searches cost is bounded, as many as a quarter of the text's bytes and reading twice its bytes (a text of a value
# that overlaps itself at each character, `aaaa`, would search at each one). Past the bound, the rest of the text is
# matched as before, the leftmost-longest terms one after the other: never less than them is found.
_OVERLAP_SEARCHES = 4  # a search for every this many bytes of the text
_OVERLAP_READ = 2  # what those searches read: as many times the text's bytes
# The word index looks for overlaps where they can be alone (a term that begins inside one of its terms one after the
# other, or whose signs before its first word lie in the term before, or that starts at a term's word and ends past it),
# within a share of what those terms cost: a fifth (Ruling 099-R2), past which its terms one after the other alone.
_WORD_OVERLAP_SHARE = 5

_Found = tuple[int, int, _Plain]  # a term found: where it starts and ends, and the term


def _with_overlaps(text: str, chain: list[_Found], others: list[_Found]) -> list[_Found]:
    """The terms found one after the other (``chain``: 0.9.8's, apart and in order), and of the terms that overlap them
    (``others``) what holds more than blanks beside them, each cut where the next term of the chain begins: so a term
    across two of the chain merges with the first alone, and what is masked as one value never runs on past the next
    term found. A value repeated whose repeats overlap others (`Mario Rossi Mario Rossi`, with `Rossi Mario` known too)
    is masked as 0.9.8 masked it, each occurrence with its value's fake; `Anna Maria Rossi` as a whole. What a term
    that spans many of the chain costs to cut is bounded by the chain's and the others' count: past it, the rest are
    left to the chain, as 0.9.8 left them."""
    starts = [start for start, _, _ in chain]
    out = list(chain)
    steps = len(chain) + len(others) + 256
    for start, end, term in others:
        j = bisect_right(starts, start) - 1  # the term of the chain it starts in or after
        at = start
        while at < end:
            steps -= 1
            if steps < 0:
                return sorted(dict.fromkeys(out), key=lambda found: (found[0], -found[1]))
            following = starts[j + 1] if j + 1 < len(starts) else len(text)
            cut = min(end, following)
            free = max(at, chain[j][1]) if j >= 0 else at
            if free < cut and not text[free:cut].isspace():
                while text[cut - 1].isspace():
                    cut -= 1
                out.append((at, cut, term))
            at, j = following, j + 1
    return sorted(dict.fromkeys(out), key=lambda found: (found[0], -found[1]))


class _Alternation:
    """Up to WORD_INDEX_FROM terms in one RE2 alternation: at each place, the longest term that stands as a whole word,
    those that overlap included (``finditer``). No \\b in the pattern (RE2's knows ASCII letters only): word edges are
    checked with `_whole`, and where the longest term at a place runs on into a letter, a shorter one there may still
    stand on its own.

    The pattern runs over the text's UTF-8, encoded once: RE2's binding encodes a text again at every call, which a text
    full of terms (a git log naming its author in every commit) would pay for each term found.

    ``bounded``: RE2 may use memory in proportion to the pattern (``BOUNDED_PER_BYTE``), not ``RE2_MEMORY``; ``memory``
    is what it may use, program and automaton, which C++ holds where Python's own count does not see it. ``escaped``:
    each value as the pattern writes it, when the caller has it already."""

    def __init__(
        self,
        terms: list[Term],
        case_sensitive: bool,
        whole_word: bool,
        *,
        bounded: bool = False,
        escaped: Mapping[str, str] | None = None,
    ) -> None:
        self._case = case_sensitive
        self._whole_word = whole_word
        self._lookup: dict[str, _Plain] = {self._key(t.value): _plain(t) for t in terms}
        values = sorted({t.value for t in terms})
        parts = [escaped[value] for value in values] if escaped is not None else [re2.escape(v) for v in values]
        pattern = "|".join(parts).encode()
        self.memory = RE2_MEMORY
        if bounded:
            self.memory = min(RE2_MEMORY, max(BOUNDED_SMALLEST, BOUNDED_PER_BYTE * len(pattern)))
        while True:
            # Quiet: an alternation too large for RE2 would be written to the standard error, its first values with it.
            options = quiet_options(case_sensitive=case_sensitive, longest_match=True, max_mem=self.memory)
            try:
                # Not re2.compile: it keeps the last 128 patterns alive, with the memory their automata grew, and the
                # vault's detectors are built again at each new value.
                self._pattern = re2._Regexp(pattern, options)
                break
            except re2.error:
                if self.memory >= RE2_MEMORY:
                    raise  # too large for any budget: the build fails, as it did before budgets were sized
                self.memory = min(RE2_MEMORY, self.memory * 2)  # never a term left out: more room

    def _key(self, value: str) -> str:
        return value if self._case else value.casefold()

    def finditer(self, text: str, data: bytes | None = None) -> Iterator[tuple[int, int, _Plain]]:
        """Where its terms are in ``text``: from the left, the longest term at a place (that stands as a whole word,
        when terms must), one after the other, and what the terms that overlap them add (``_with_overlaps``).
        ``data``: the text's UTF-8, when the caller has it already (the vault's values are matched by many
        alternations, each over the same text)."""
        data = text.encode() if data is None else data
        offset = _Offsets(text, data)
        searches = len(data) // _OVERLAP_SEARCHES + 64  # what the searches for overlaps may cost (the bound above)
        read = _OVERLAP_READ * len(data) + 4096
        overlaps = True
        chain: list[_Found] = []  # the terms one after the other, 0.9.8's, and those that overlap them, in bytes
        others: list[_Found] = []
        pos = reach = 0  # reach: where the terms one after the other end
        while (m := self._pattern.search(data, pos)) is not None:
            start, end = m.span()
            if start < reach:  # inside a term found already: an overlap is looked for
                searches -= 1
                read -= end - pos
                if searches < 0 or read < 0:
                    overlaps = False  # bound reached: the rest as before, one after the other from the chain's end
                    pos = reach
                    continue
            found = self._longest_at(data, start, end)
            if start >= reach:  # where 0.9.8 went on: after the term, or past the match (a term's edge on a letter)
                if found is not None:
                    chain.append((start, found[0], found[1]))
                reach = found[0] if found is not None else start + 1 if self._whole_word else end
            elif found is not None:
                others.append((start, found[0], found[1]))
            # In the middle of a character, no term starts: RE2 goes on to the next one.
            pos = start + 1 if overlaps else reach
        if not others:
            yield from ((offset(start), offset(end), term) for start, end, term in chain)
            return
        terms = sorted([*chain, *others])
        places = {place: offset(place) for found in terms for place in found[:2]}  # in order: each piece decoded once
        in_text = [(places[start], places[end], term) for start, end, term in chain]
        overlapping = [(places[start], places[end], term) for start, end, term in others]
        yield from _with_overlaps(text, in_text, overlapping)

    def _longest_at(self, data: bytes, start: int, end: int) -> tuple[int, _Plain] | None:
        """The longest term at ``start`` that stands as a whole word when terms must (``end``: where the longest one
        ends), and where it ends."""
        while True:
            term = self._lookup.get(self._key(data[start:end].decode()))
            if term is not None and (not self._whole_word or _whole_utf8(data, start, end)):
                return end, term
            if not self._whole_word:
                return None
            shorter = self._pattern.match(data, start, end - 1) if end - 1 > start else None
            if shorter is None:
                return None
            end = shorter.end()


# A term in the word index, a plain tuple as a term is (``_Plain``): its words as compared, the characters before its
# first word (`@acme`), those after its last word (`C++ Guild`), the whole term as compared, the term, and what looking
# at the terms of its first word costs up to it, in order: a unit for each term and for each of its words. Counted once
# here, never as the text is read: the terms one after the other are found at 0.9.8's cost.
_Entry = tuple[tuple[str, ...], int, int, str, _Plain, int]


class _WordIndex:
    """Whole-word terms looked up by their words: the cost follows the length of the text, not the number of terms."""

    def __init__(self, terms: list[Term], case_sensitive: bool) -> None:
        self._case = case_sensitive
        starts: dict[str, list[tuple[tuple[str, ...], int, int, str, _Plain]]] = {}  # a first word → its terms
        for term in terms:
            found = list(_WORDS.finditer(term.value))
            if not found:
                continue
            words = tuple(self._key(w.group()) for w in found)
            lead, trail = found[0].start(), len(term.value) - found[-1].end()
            starts.setdefault(words[0], []).append((words, lead, trail, self._key(term.value), _plain(term)))
        # The one that starts first, then the longest; in tuples, which the garbage collector stops following.
        self._starts: dict[str, tuple[_Entry, ...]] = {}
        for word, entries in starts.items():
            cost = 0
            ordered = []
            for entry in sorted(entries, key=lambda e: (-e[1], -len(e[3]))):
                cost += 1 + len(entry[0])
                ordered.append((*entry, cost))
            self._starts[word] = tuple(ordered)
        self._first = frozenset(self._starts)

    def _key(self, value: str) -> str:
        return value if self._case else value.casefold()

    def finditer(self, text: str, data: bytes | None = None) -> Iterator[tuple[int, int, _Plain]]:
        words = _WORDS.findall(text)
        if self._first.isdisjoint(words if self._case else map(str.casefold, words)):
            return  # most texts hold none of the terms: one look at their words says so
        tokens = [(m.start(), m.end(), self._key(m.group())) for m in _WORDS.finditer(text)]
        chain: list[_Found] = []  # the terms one after the other, 0.9.8's
        others: list[_Found] = []  # the terms that overlap them
        inside: list[int] = []  # the words inside a term of the chain that begin a term: one may run on past it
        cost = spent = 0  # what the terms one after the other cost (``_Entry``'s units), and what the overlaps did
        done = i = 0  # done: where the last term found ends; the next one starts there or after
        while i < len(tokens):
            found, looked, extra = self._at(text, tokens, i, done, others, cost, spent)
            cost += looked
            spent += extra
            if found is None:
                i += 1
                continue
            chain.append(found)
            done = found[1]
            i += 1
            while i < len(tokens) and tokens[i][0] < done:
                if tokens[i][2] in self._first:
                    inside.append(i)
                i += 1
        others += self._overlapping(text, tokens, inside, cost // _WORD_OVERLAP_SHARE + 256 - spent)
        yield from _with_overlaps(text, chain, others) if others else chain

    def _overlapping(
        self, text: str, tokens: list[tuple[int, int, str]], inside: list[int], budget: int
    ) -> list[_Found]:
        """The terms that begin with the words ``inside`` the terms one after the other (``_each``), within ``budget``,
        in ``_Entry``'s units: past it, none more, and the terms one after the other alone are found from there
        (0.9.8's)."""
        out: list[_Found] = []
        for i in inside:
            if budget < 0:
                break
            found, spent = self._each(text, tokens, i)
            out += found
            budget -= spent
        return out

    def _at(
        self,
        text: str,
        tokens: list[tuple[int, int, str]],
        i: int,
        done: int,
        others: list[_Found],
        cost: int,
        spent: int,
    ) -> tuple[_Found | None, int, int]:
        """The term that starts with the text's i-th word, if one does: of those that start at ``done`` or after, the
        one that starts first, the longest there, as 0.9.8 found it, and what looking cost (``_Entry``'s count). And,
        where the terms one after the other have cost five times more than the overlaps looked for so far (``cost``,
        ``spent``), the terms there that overlap others, added to ``others``, and what looking for them cost: one whose
        signs before its first word lie in the term before (`@team` right after `ops@`), and, where the term found has
        signs before its word, the one that ends last past it, which only one with fewer signs can (those with as many
        start where it does and end no later: the terms are in that order). Found as the term is, the word never read
        again."""
        entries = self._starts.get(tokens[i][2], ())
        extra = 0
        for words, lead, trail, whole, term, looked in entries:
            last = i + len(words) - 1
            if last >= len(tokens) or any(tokens[i + j][2] != words[j] for j in range(1, len(words))):
                continue
            start, end = tokens[i][0] - lead, tokens[last][1] + trail
            # The words are \w (no combining accent): `_whole` says whether a mark next to the term joins its word.
            if start >= done and end <= len(text) and self._key(text[start:end]) == whole and _whole(text, start, end):
                if lead and spent + extra <= cost // _WORD_OVERLAP_SHARE + 256:
                    extra += self._past(text, tokens, i, entries, (start, end, term), lead, others)
                return (start, end, term), looked, extra
            if start < done and spent + extra <= cost // _WORD_OVERLAP_SHARE + 256:
                extra += 1 + len(words)  # its first signs in the term before
                if start >= 0 and end <= len(text) and self._key(text[start:end]) == whole and _whole(text, start, end):
                    others.append((start, end, term))
        return None, entries[-1][5] if entries else 0, extra

    def _past(
        self,
        text: str,
        tokens: list[tuple[int, int, str]],
        i: int,
        entries: tuple[_Entry, ...],
        found: _Found,
        lead: int,
        others: list[_Found],
    ) -> int:
        """Of the terms that start with the text's i-th word with fewer signs before it than ``found`` (``lead``), the
        one that ends last, where it ends past ``found``, added to ``others``; and what looking cost."""
        furthest, spent = found, 0
        for words, before, trail, whole, term, _ in entries:
            if before >= lead:
                continue
            spent += 1 + len(words)
            last = i + len(words) - 1
            if last >= len(tokens) or any(tokens[i + j][2] != words[j] for j in range(1, len(words))):
                continue
            start, end = tokens[i][0] - before, tokens[last][1] + trail
            if (
                end > furthest[1]
                and end <= len(text)
                and self._key(text[start:end]) == whole
                and _whole(text, start, end)
            ):
                furthest = (start, end, term)
        if furthest is not found:
            others.append(furthest)
        return spent

    def _each(self, text: str, tokens: list[tuple[int, int, str]], i: int) -> tuple[list[_Found], int]:
        """The terms that start with the text's i-th word, wherever the terms before it end: the one that starts first,
        the longest there; and the one that ends last, where it ends later (each other one there lies within the two).
        And what looking cost, as ``_Entry`` counts it."""
        first: _Found | None = None
        furthest: _Found | None = None
        entries = self._starts.get(tokens[i][2], ())
        spent = entries[-1][5] if entries else 0
        for words, lead, trail, whole, term, _ in entries:
            last = i + len(words) - 1
            if last >= len(tokens) or any(tokens[i + j][2] != words[j] for j in range(1, len(words))):
                continue
            start, end = tokens[i][0] - lead, tokens[last][1] + trail
            if start >= 0 and end <= len(text) and self._key(text[start:end]) == whole and _whole(text, start, end):
                first = first or (start, end, term)
                if furthest is None or end > furthest[1]:
                    furthest = (start, end, term)
        if first is None or furthest is None:
            return [], spent
        return ([first, furthest] if furthest[1] > first[1] else [first]), spent


def _forms(value: str) -> list[str]:
    """A term as given, composed and decomposed: a keyboard composes "ë", macOS writes file names decomposed (an "e"
    and a combining mark), and the two never compare equal."""
    if value.isascii():
        return [value]
    return list(dict.fromkeys((value, unicodedata.normalize("NFC", value), unicodedata.normalize("NFD", value))))


def _in_parts(terms: list[Term], case_sensitive: bool) -> tuple[list[list[Term]], dict[str, str]]:
    """``terms`` in parts whose patterns hold ``PART_CHARS`` bytes at most each (a term longer than that alone in a part
    of its own); one part when they fit in one. And each value as a pattern writes it.

    Cut from the terms in order (as compared: without case, unless they match case), a part half full ending where
    their first character changes: so the terms of a part share their first character, and RE2 skips, as it scans, to
    where that character stands. A part of terms that start every way is read at every place of a text, which a few
    dozen parts made tens of times slower than one alternation; parts cut so scan about as fast as it does."""
    escaped = {t.value: re2.escape(t.value) for t in terms}
    sizes = {value: len(pattern.encode()) + 1 for value, pattern in escaped.items()}  # with the bar after it
    if sum(sizes[t.value] for t in terms) <= PART_CHARS:
        return [terms], escaped
    compared = (lambda value: value) if case_sensitive else str.casefold
    parts: list[list[Term]] = [[]]
    size, first = 0, ""
    for term in sorted(terms, key=lambda t: compared(t.value)):
        more, starts = sizes[term.value], compared(term.value[:1])
        if parts[-1] and (size + more > PART_CHARS or (size >= PART_CHARS // 2 and starts != first)):
            parts.append([])
            size = 0
        if not parts[-1]:
            first = starts
        parts[-1].append(term)
        size += more
    return parts, escaped


class _TermSet:
    """Terms grouped by how they match (case, whole words), each group matched the fastest way for its size, each
    term in every form it can be written in. ``bounded``: each alternation's RE2 memory in proportion to it.
    ``parted``: an alternation larger than ``PART_CHARS`` is matched in parts, each compiled on its own under a budget
    sized to it (the user's terms; a vault's values come in parts of their own, ``latet.engine.known``)."""

    def __init__(self, terms: Iterable[Term], *, bounded: bool = False, parted: bool = False) -> None:
        groups: dict[tuple[bool, bool], list[Term]] = {}
        self._given: dict[_Plain, _Plain] = {}  # a term written in another form → the term as given
        for t in terms:
            if t.etype != "LATET_ROUTE_KEY" and not has_letter_or_digit(t.value):
                continue
            for form in _forms(t.value):
                written = t if form == t.value else replace(t, value=form)
                if written is not t:
                    self._given[_plain(written)] = _plain(t)
                groups.setdefault((t.case_sensitive, t.whole_word), []).append(written)
        self._matchers: list[_Alternation | _WordIndex] = []
        for (case_sensitive, whole_word), group in groups.items():
            # One matcher for a list wherever it can be: two would each pick their own term at a place ("Zoë" inside
            # "Zoë Smith"), which the masker then merges, the longest naming the value. The alternation checks word
            # edges in every alphabet, so only the size decides (and past PART_CHARS, what one compile may take).
            big = len(group) > WORD_INDEX_FROM
            indexed: list[Term] = []
            rest: list[Term] = []
            for t in group:
                wordy = whole_word and _WORDS.search(t.value) is not None
                (indexed if wordy and big else rest).append(t)
            if indexed:
                self._matchers.append(_WordIndex(indexed, case_sensitive))
            if rest and parted:
                parts, escaped = _in_parts(rest, case_sensitive)
                for part in parts:  # each compiled on its own: the GIL is let go between two
                    sized = bounded or len(parts) > 1  # one of many: a budget sized to it
                    self._matchers.append(
                        _Alternation(part, case_sensitive, whole_word, bounded=sized, escaped=escaped)
                    )
            elif rest:
                self._matchers.append(_Alternation(rest, case_sensitive, whole_word, bounded=bounded))

    def __bool__(self) -> bool:
        return bool(self._matchers)

    @property
    def memory(self) -> int:
        """What RE2 may use for its alternations, in bytes (the word indexes are Python's, counted with their terms)."""
        return sum(m.memory for m in self._matchers if isinstance(m, _Alternation))

    def finditer(self, text: str, data: bytes | None = None) -> Iterator[tuple[int, int, _Plain]]:
        for matcher in self._matchers:
            if not self._given:
                yield from matcher.finditer(text, data)
                continue
            for start, end, term in matcher.finditer(text, data):
                yield start, end, self._given.get(term, term)


class TermsDetector:
    """The user's deny-list (client names, project code names, internal domains...)."""

    # ``bounded``: the RE2 memory in proportion to the terms (``memory``), as each project's masker in an identity's
    # space on a server has it. (Kept out of the class's docstring: the detectors' reference page is made from it.)

    id = "terms"
    mandatory = True

    def __init__(
        self,
        terms: Iterable[Term],
        eclass: EntityClass = EntityClass.CUSTOM,
        rule: str = "terms",
        *,
        bounded: bool = False,
    ) -> None:
        self._set = _TermSet(terms, bounded=bounded, parted=True)  # in parts past PART_CHARS, as a vault's values are
        self._eclass = eclass
        self._rule = rule

    @property
    def memory(self) -> int:
        """What RE2 may use to match its terms, in bytes: C++ memory, which Python's own count does not see."""
        return self._set.memory

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if not self._set:
            return
        for start, end, term in self._set.finditer(text):
            etype = term[_ETYPE]
            yield Span(start, end, etype, self._eclass, f"{self._rule}:{etype.lower()}")


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    usernames: tuple[str, ...] = ()
    username: str | None = None  # a single name: same as usernames=(name,)
    hostname: str | None = None
    full_name: str | None = None
    emails: tuple[str, ...] = ()
    hostnames: tuple[str, ...] = ()  # more host names, as hostname (a person's computers on a Latet server)


def own_domain(email: str) -> str | None:
    """The domain of your own address when it is your company's: not a mail provider's, nor a
    forge's no-reply one (``users.noreply.github.com``)."""
    domain = email.rpartition("@")[2].lower().strip(".")
    if len(domain) < 4 or "." not in domain or domain in PUBLIC_MAIL_DOMAINS or "noreply" in domain.split("."):
        return None
    return domain


class IdentityDetector:
    """This machine's identity: the OS user inside home paths, the host name, the git author and
    its company's domain."""

    id = "identity"
    mandatory = True

    def __init__(self, identity: LocalIdentity) -> None:
        terms: list[Term] = []
        if identity.full_name:
            terms.append(Term(identity.full_name, "PERSON"))
        terms += [Term(e, "EMAIL") for e in identity.emails]
        hosts = dict.fromkeys(h for h in (identity.hostname, *identity.hostnames) if h and len(h) >= 4)
        terms += [Term(host, "HOSTNAME") for host in hosts]
        self._terms = TermsDetector(terms, EntityClass.IDENTITY, "identity")
        # Your company's domain wherever it appears (api.studio-rossi.it, a wiki link), not only next to
        # your address. As personal data: an address at it found by the detectors stays an address.
        domains = dict.fromkeys(d for d in map(own_domain, identity.emails) if d)
        self._domains = TermsDetector([Term(d, "DOMAIN") for d in domains], EntityClass.PII, "identity")
        self._home: re2._Regexp | None = None
        self._user_line: re2._Regexp | None = None
        names = [*identity.usernames, *([identity.username] if identity.username else [])]
        names = list(dict.fromkeys(n for n in names if n))
        if names:
            # Inside home-directory paths only: a short user name is a common word elsewhere. Long,
            # distinctive login names are also masked as plain terms (they show up in metadata).
            alternation = "|".join(re2.escape(n) for n in sorted(names, key=len, reverse=True))
            home = r"(?i)(?:[\\/]|\\\\)(?:users|home)(?:[\\/]|\\\\)(" + alternation + r")\b"
            self._home = re2.compile(home, quiet_options())
            # Claude Code tells its auto mode reviewer "User identity: `<user name>`": any length there.
            self._user_line = re2.compile(r"(?i)\buser identity:[ \t]*`?(" + alternation + r")\b", quiet_options())
            terms += [Term(n, "USERNAME") for n in names if len(n) >= 6]
            self._terms = TermsDetector(terms, EntityClass.IDENTITY, "identity")

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        yield from self._terms.detect(text, ctx)
        yield from self._domains.detect(text, ctx)
        if self._home is not None:
            for m in self._home.finditer(text):
                start, end = m.span(1)
                yield Span(start, end, "USERNAME", EntityClass.IDENTITY, "identity:home-path")
        if self._user_line is not None:
            for m in self._user_line.finditer(text):
                start, end = m.span(1)
                yield Span(start, end, "USERNAME", EntityClass.IDENTITY, "identity:user-identity")


SHORTEST_NAME = 3


def long_enough(value: str, etype: str, min_length: int = 4) -> bool:
    """A value is matched again from ``min_length`` characters: a shorter one is too often a common
    word. A person's name from three ("Ugo", "Ada"), as names are matched whole and case-sensitive."""
    return len(value) >= (min(min_length, SHORTEST_NAME) if etype == "PERSON" else min_length)


class KnownValuesDetector:
    """Every original already in the vault, matched exactly wherever it appears again."""

    # ``bounded``: the RE2 memory in proportion to the values, as each part of a vault's values has it
    # (``latet.engine.known``): one of many, and compiled faster so. (Kept out of the class's docstring: the detectors'
    # reference page is made from it.)

    id = "known-values"
    mandatory = True

    def __init__(
        self,
        values: Iterable[tuple[str, str, EntityClass]],
        min_length: int = 4,
        rule: str = "known-values",
        *,
        bounded: bool = False,
    ) -> None:
        self._rule = rule
        self._classes: dict[str, tuple[str, EntityClass]] = {}
        terms = []
        for original, etype, eclass in values:
            if long_enough(original, etype, min_length):
                self._classes[original] = (etype, eclass)
                terms.append(Term(original, etype, case_sensitive=True, whole_word=eclass != EntityClass.SECRET))
        self._set = _TermSet(terms, bounded=bounded)

    @property
    def empty(self) -> bool:
        """It matches nothing (no value long enough to be matched again)."""
        return not self._set

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        yield from self.spans(text)

    def spans(self, text: str, data: bytes | None = None) -> Iterator[Span]:
        """``detect``'s, ``data`` the text's UTF-8 when the caller has it already."""
        if not self._set:
            return
        for start, end, term in self._set.finditer(text, data):
            etype, eclass = self._classes[term[_VALUE]]
            yield Span(start, end, etype, eclass, self._rule)
