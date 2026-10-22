"""Exact-term detectors: the user's own deny-list, the local identity (OS user, host, git author),
and every value already in the vault (so a value detected once is masked everywhere)."""

from __future__ import annotations

import re
import unicodedata
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
    """Offsets in a text from offsets in its UTF-8, asked for in increasing order: each piece is decoded once."""

    def __init__(self, text: str, data: bytes) -> None:
        self._data = data
        self._ascii = len(data) == len(text)  # the same offsets
        self._byte = self._char = 0

    def __call__(self, byte: int) -> int:
        if self._ascii:
            return byte
        self._char += len(self._data[self._byte : byte].decode())
        self._byte = byte
        return self._char


class _Alternation:
    """Up to WORD_INDEX_FROM terms in one RE2 alternation: at the leftmost place, the longest term that stands as a
    whole word. No \\b in the pattern (RE2's knows ASCII letters only): word edges are checked with `_whole`, and where
    the longest term at a place runs on into a letter, a shorter one there may still stand on its own.

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
        """Where its terms are in ``text``; ``data``: the text's UTF-8, when the caller has it already (the vault's
        values are matched by many alternations, each over the same text)."""
        data = text.encode() if data is None else data
        offset = _Offsets(text, data)
        if not self._whole_word:
            for m in self._pattern.finditer(data):
                term = self._lookup.get(self._key(data[m.start() : m.end()].decode()))
                if term is not None:
                    yield offset(m.start()), offset(m.end()), term
            return
        pos = 0
        while (m := self._pattern.search(data, pos)) is not None:
            start, end = m.span()
            term: _Plain | None = None
            while True:
                candidate = self._lookup.get(self._key(data[start:end].decode()))
                if candidate is not None and _whole_utf8(data, start, end):
                    term = candidate
                    break
                shorter = self._pattern.match(data, start, end - 1) if end - 1 > start else None
                if shorter is None:
                    break
                end = shorter.end()
            if term is not None:
                yield offset(start), offset(end), term
                pos = end
            else:
                pos = start + 1  # in the middle of a character, no term starts: RE2 goes on to the next one


# A term in the word index, a plain tuple as a term is (``_Plain``): its words as compared, the characters before its
# first word (`@acme`), those after its last word (`C++ Guild`), the whole term as compared, and the term.
_Entry = tuple[tuple[str, ...], int, int, str, _Plain]


class _WordIndex:
    """Whole-word terms looked up by their words: the cost follows the length of the text, not the number of terms."""

    def __init__(self, terms: list[Term], case_sensitive: bool) -> None:
        self._case = case_sensitive
        starts: dict[str, list[_Entry]] = {}  # a first word → the terms that start with it
        for term in terms:
            found = list(_WORDS.finditer(term.value))
            if not found:
                continue
            words = tuple(self._key(w.group()) for w in found)
            lead, trail = found[0].start(), len(term.value) - found[-1].end()
            starts.setdefault(words[0], []).append((words, lead, trail, self._key(term.value), _plain(term)))
        # The one that starts first, then the longest; in tuples, which the garbage collector stops following.
        self._starts: dict[str, tuple[_Entry, ...]] = {
            word: tuple(sorted(entries, key=lambda e: (-e[1], -len(e[3])))) for word, entries in starts.items()
        }
        self._first = frozenset(self._starts)

    def _key(self, value: str) -> str:
        return value if self._case else value.casefold()

    def finditer(self, text: str, data: bytes | None = None) -> Iterator[tuple[int, int, _Plain]]:
        words = _WORDS.findall(text)
        if self._first.isdisjoint(words if self._case else map(str.casefold, words)):
            return  # most texts hold none of the terms: one look at their words says so
        tokens = [(m.start(), m.end(), self._key(m.group())) for m in _WORDS.finditer(text)]
        done = i = 0  # done: where the last term found ends; the next one starts there or after
        while i < len(tokens):
            found = self._at(text, tokens, i, done)
            if found is None:
                i += 1
                continue
            yield found
            done = found[1]
            while i < len(tokens) and tokens[i][0] < done:
                i += 1

    def _at(self, text: str, tokens: list[tuple[int, int, str]], i: int, done: int) -> tuple[int, int, _Plain] | None:
        """The term that starts with the text's i-th word, if one does."""
        for words, lead, trail, whole, term in self._starts.get(tokens[i][2], ()):
            last = i + len(words) - 1
            if last >= len(tokens) or any(tokens[i + j][2] != words[j] for j in range(1, len(words))):
                continue
            start, end = tokens[i][0] - lead, tokens[last][1] + trail
            # The words are \w (no combining accent): `_whole` says whether a mark next to the term joins its word.
            if start >= done and end <= len(text) and self._key(text[start:end]) == whole and _whole(text, start, end):
                return start, end, term
        return None


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
