"""The anchor word of a value or a fake: the one word by which a vault kept in a database finds it again in a text.

A vault of a hundred thousand secrets cannot be asked, for a text, whether each of them is in it. So each value and each
fake is kept with its anchor, the longest run of plain letters and digits it holds, in folded case, and a text is asked
only for the values and fakes whose anchor is among its words (``words_in``). The anchor of `Laura.Bianchi@nordest.it`
is `bianchi`.

A run is ASCII on purpose. A value or a fake does not stay as it was written when it comes back in a text: it is in
upper or lower case, in a URL it is quoted (`laura.bianchi%40nordest.it`), in JSON an accented letter is written as an
escape (a backslash, `u` and four hex digits stand for the `ò` of `Niccolò`). A run of plain letters and digits is the
part that every one of these forms keeps, whole: the folded case drops the case, the signs a quote puts in are not
letters or digits, and the accented letter, which the escape changes, is not part of any run. The anchor of
`Niccolò Ferri` is `niccol`, and it is a word of the same name with its `ò` written as an escape.

The case is folded from the upper case (``upper().casefold()``), not only lowered, because some letters change their
ASCII letters with the case: the upper case of `ß` is `SS`, and that of the dotless i of Turkish (U+0131) is `I`, so
`Hauptstraße` and `HAUPTSTRASSE` are one word, `hauptstrasse`, and a Turkish name written with its dotless i is the
same word as its upper case, `YILDIZHAN`. Folding makes the `ß` of such a word ASCII, so it is part of the run, and the
forms that write it apart (an escape, a quote) have to be read as the letter to keep the run whole: ``words_in`` reads
them so (``decoded``).

The escapes themselves glue, though. A line break written as a backslash and an `n` leaves the `n` in front of the next
word (`Anna\\nLaura` reads `nLaura`), and a quoted space (`%20`) leaves its digits in front of it. So ``words_in`` reads
a text in more than one way, and answers the words of all of them: a word it adds that is no word of the text costs a
lookup in vain, and a word it misses is a value that is never found.

- As it is written. A backslash written twice is a backslash, and the letter after it is a letter.
- With the characters of its escapes blanked out (a detector's reading, ``escapes``: a backslash and one of `n r t b f`,
  or `u` and four hex digits, to which these are added: a log's `\\x` and two hex digits, `\\v`, `\\a`, `\\e` and `\\0`,
  Python's `\\U` and eight hex digits, and a URL's `%` and two hex digits).
- With the escapes written twice blanked out as well, as in a JSON string inside a JSON string, whatever the number of
  backslashes before the letter.
- With the escapes that stand for a letter (`u` and four hex digits, `x` and two, `%` and two) read as the letter, and
  then blanked out as above.
- As the masker reads a text's escapes, where it finds a value the vault knows written in them (``escapes.decoded``):
  HTML's references too (`Zo&euml; Li`), git's octal bytes and Python's `\\U` and eight hex digits.

Both ``anchor`` and ``words_in`` read a text in Unicode's composed form (NFC) first, so a letter written with its
accent apart (`o`, then a combining grave) is the same word as the one written whole. A run of more than
``MARKS_AT_MOST`` accents on one letter, which no word has, is read as a space first: putting a run of marks in their
order takes time that grows with its square, and a text from outside can hold a run of a million.

A value with no run of three plain letters and digits may be written in another script: `Иван Петров`, `王小明`,
`محمد`. Its anchor is then the longest run of letters and digits of any script, of two characters at least (a Chinese
or Korean name is two or three), read without the accents and the marks of its letters: a Greek name loses its accents
in capitals (`ΠΑΠΑΔΟΠΟΥΛΟΣ`), Russian leaves the dots of `ё` out as often as not, and Arabic and Hebrew write their
vowel points or leave them out; so `Фёдоров` and `Федоров` are one word, `федоров`, and `Zoë` is `zoe`. A format
character (a zero-width space or non-joiner, a word joiner, a soft hyphen, a mark of direction) is the edge of a word,
as the engine reads it: `رضایی` is a word of its plural written with a zero-width non-joiner. In a script written
without spaces between its words (Chinese, Japanese, Thai, Lao, Khmer, Myanmar) and in Korean, whose words carry their
particles glued on, a name is glued to the words around it: there the anchor is the first two characters of the run
(`王小` for `王小明`), and the words of a text are every two characters in a row of such a run. Two characters say less
of a name than a whole word: more values share one (every name of one surname and the same next character), and a
reader of the database who sees which values share a keyed hash sees larger groups. ``words_in`` reads these runs in
every reading it makes, the escapes that stand for a letter read as the letter (a character beyond the first plane as
the two halves JSON writes it in), so `Иван` in a JSON written in ASCII, or quoted in a URL, is found.

A value with no such run (`ab-1`, `Ж`, a line of signs) has no anchor: it is not found by its word, and whoever keeps
the vault says how such a value is found.

The labels of the public mail domains (`gmail`, `hotmail`, `com`) are shared by thousands of addresses, and a text
that names one would have the store open every one of them: they are the anchor of a value that has no other run alone
(`ugo.neri@gmail.com` is found by `neri`).

A value or a fake written in escapes, as a link or a JSON string writes it (0.9.9 keeps a value found so as it is
written, with a fake in the same escapes: `Giulia%20Martini`), comes back as written and as it reads (`Giulia Martini`):
its anchor is a run that no escape glues letters to (`giulia`, not `20martini`), a word of both; where every run is
glued to an escape of its own (a phone's `%2B39...`, a password's signs written in escapes), a run the escapes leave
free (`393331234567`), as they read it (version 4).

What ``anchor`` and ``words_in`` give is kept: a store keeps the keyed hashes of the anchors, with ``ANCHORS_VERSION``,
and makes those of an older version again. Any change that gives a value another anchor bumps it.
"""

from __future__ import annotations

import functools
import operator
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import unquote

from latet.engine.escapes import blanked, escapes
from latet.engine.escapes import decoded as as_escapes_read
from latet.engine.surrogates.data.names import PUBLIC_MAIL_DOMAINS

# The version of what ``anchor``, ``edges``, ``dotted`` and ``words_in`` give. A store that keeps anchors keeps it
# beside each one, and makes again those of an older version: any change that gives a value another anchor bumps it.
ANCHORS_VERSION = 9
WORD = re.compile(r"[A-Za-z0-9]{3,}")
# The letters of an anchor's edge word (``edges``), and of the first and last letters of a text's longer runs
# (``words_in``): six letters and digits are a few millions of words, which few strings of a vault share.
EDGE = 6
# The longest string of one run that a vault finding it inside a word holds in memory for good (``glued_unfound``): a
# password's length. A longer one (a token) glued on both sides is found by no word of a text, and is not held: a key
# can see thousands of tokens, each read in every text it masks once held.
ONE_RUN_HELD = 16
# An address's domain's name, as a text read for fakes holds it (``address_word``): the run of letters, digits and
# hyphens after its `@`.
_NAME = re.compile(r"[A-Za-z0-9-]+")
# A dotted word (``dotted``): runs of letters and digits with the dots, hyphens and underscores between them, of at most
# ``SEGMENTS_AT_MOST`` runs (an IPv4 address has four), so that a text's dotted words grow with its length.
_DOTTED = re.compile(r"[A-Za-z0-9._-]+")
_SEGMENT = re.compile(r"[A-Za-z0-9]+")
_DOTTED_WHOLE = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*")
SEGMENTS_AT_MOST = 4
# The labels of the public mail domains (`gmail`, `hotmail`, `com`), which thousands of addresses share: never the
# anchor of a value that has another run, which a text that names them would otherwise find all of (Ruling T8a).
_MAIL_LABELS = frozenset(label for domain in PUBLIC_MAIL_DOMAINS for label in domain.casefold().split("."))
# The scripts written without spaces between their words, and Korean (``_RUN``'s second kind of run): their letters,
# and the iteration marks, numerals and phonetic letters written among them.
_UNSPACED_RANGES = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x1780, 0x17FF),  # Khmer
    (0x3005, 0x3007),  # the iteration mark of Han (佐々木), the closing mark, the ideographic zero
    (0x3021, 0x3029),  # Hangzhou numerals
    (0x3031, 0x3035),  # the iteration marks of kana, written vertically
    (0x303B, 0x303C),  # the vertical iteration mark of Han, the masu mark
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3100, 0x312F),  # Bopomofo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
    (0x31A0, 0x31BF),  # Bopomofo Extended
    (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
    (0x3400, 0x4DBF),  # Han, Extension A
    (0x4E00, 0x9FFF),  # Han
    (0xA960, 0xA97F),  # Hangul Jamo Extended-A
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xD7B0, 0xD7FF),  # Hangul Jamo Extended-B
    (0xF900, 0xFAFF),  # Han, compatibility
    (0xFF66, 0xFF9F),  # Katakana, halfwidth
    (0x1B000, 0x1B16F),  # Kana Supplement and Extended-A, Small Kana Extension
    (0x20000, 0x323AF),  # Han, beyond the first plane (Extensions B to H, compatibility supplement)
)
_UNSPACED = "".join(f"{chr(first)}-{chr(last)}" for first, last in _UNSPACED_RANGES)
# A run of letters and digits of any script but those (two at least), or a run of those (two at least).
_RUN = re.compile(f"([^\\W_{_UNSPACED}]{{2,}})|([{_UNSPACED}]{{2,}})")
_TATWEEL = 0x0640  # Arabic's stretching of a line between two letters: no letter itself
# Where the combining marks and the format characters are: the first two planes, and the fourteenth (its tags and
# variation selectors). No other plane holds one.
_MARKED_PLANES = (range(0x20000), range(0xE0000, 0xE1000))
# The combining marks a letter carries at most, as a text is read: no word has more than a few. A longer run is read as
# a space, before the marks are put in their order (NFC), which takes time that grows with the square of a run.
MARKS_AT_MOST = 32
# The escapes that `escapes.escapes` does not read, and a URL's quote (`%C3`): a log's backslash and `x` with two hex
# digits, `v`, `a`, `e` or `0`, or Python's `U` with eight. A backslash that is itself escaped starts none, as there.
_QUOTED = re.compile(r"(?<!\\)(?:\\\\)*\\(x[0-9A-Fa-f]{2}|[vae0]|U[0-9A-Fa-f]{8})|(%[0-9A-Fa-f]{2})")
# The letter of an escape written twice or more (`\\n`), after any number of backslashes. Each of these starts at the
# first backslash of a run (`(?<!\\)`): a regex that begins at every backslash of a run reads one of N in time that
# grows with N squared, and a text from outside can be a run of a million.
_DOUBLED = re.compile(r"(?<!\\)\\{2,}([nrtbfvae0]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})")
# An escape that stands for a letter: the two halves of a character beyond the first plane (`u` and four hex digits
# each, as JSON writes it in ASCII), `u` and four hex digits, or the bytes of a letter as a log writes them (a run of
# `\xc3\x9f`), any number of backslashes before each.
_LETTER = re.compile(
    r"(?<!\\)(?:\\+u([dD][89abAB][0-9A-Fa-f]{2})\\+u([dD][c-fC-F][0-9A-Fa-f]{2})"
    r"|\\+u([0-9A-Fa-f]{4})|((?:\\+x[0-9A-Fa-f]{2})+))"
)
_BYTE = re.compile(r"x([0-9A-Fa-f]{2})")


def _mark(character: str) -> bool:
    """A combining mark: an accent, a vowel sign or point. A letter of the word it is on, to the engine."""
    return unicodedata.category(character) in ("Mn", "Mc", "Me")


@functools.cache
def _marks() -> re.Pattern[str]:
    """A run of more than ``MARKS_AT_MOST`` combining marks. Made at its first use."""
    codes = [code for plane in _MARKED_PLANES for code in plane if _mark(chr(code))]
    ranges: list[tuple[int, int]] = []
    for code in codes:
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1] = (ranges[-1][0], code)
        else:
            ranges.append((code, code))
    allowed = "".join(f"{re.escape(chr(first))}-{re.escape(chr(last))}" for first, last in ranges)
    return re.compile(f"[{allowed}]{{{MARKS_AT_MOST + 1},}}")


def _composed(text: str) -> str:
    """``text`` in Unicode's composed form: a letter and its accent written apart become the one letter. A run of more
    than ``MARKS_AT_MOST`` marks is read as a space first, so that putting the marks in their order takes time that
    grows with the text, not with the square of a run; and so in a text composed already, so that a value gives one
    anchor in either of its forms (Ruling T7d)."""
    if text.isascii():
        return text
    text = _marks().sub(" ", text)
    return text if unicodedata.is_normalized("NFC", text) else unicodedata.normalize("NFC", text)


def _folded(text: str) -> str:
    """``text`` in one case for every form of a letter: the upper case first, as the letters whose upper case is another
    letter (`ß`, the dotless i) are the same word in either, then the fold."""
    return text.upper().casefold()


def _separated(text: str) -> str:
    """``text`` with the characters of its escapes blanked out, so that no escape glues a word to the one before it."""
    if "\\" in text:
        text = blanked(text, escapes(text))
    if "\\" in text or "%" in text:
        text = blanked(text, [m.span(m.lastindex or 0) for m in _QUOTED.finditer(text)])
    return text


def _doubled(text: str) -> str:
    """``text`` with the letters of its escapes written twice or more blanked out."""
    return blanked(text, [m.span(1) for m in _DOUBLED.finditer(text)]) if "\\\\" in text else text


def _letter(match: re.Match[str]) -> str:
    """The letter an escape stands for: `\\u00df` is `ß`, the two halves `\\ud842\\udfb7` are the character beyond the
    first plane they make, `\\xc3\\x9f` is the bytes of `ß` (or, when they are not UTF-8, `\\xdf` is Latin-1's). Half of
    a character alone (`\\ud83d`) is a character of its own, which is no letter."""
    if match.group(1):
        high, low = int(match.group(1), 16), int(match.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    if match.group(3):
        return chr(int(match.group(3), 16))
    raw = bytes(int(digits, 16) for digits in _BYTE.findall(match.group(4)))
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def decoded(text: str) -> str:
    """``text`` with the escapes that stand for a letter (a JSON or a log's, a URL's) written as the letter."""
    return unquote(_LETTER.sub(_letter, text))


@functools.cache
def _unmarked() -> dict[int, str | None]:
    """How a word of another script is read without the accents and the marks of its letters (``str.translate``'s
    table): a mark and the tatweel taken out, which the engine reads as parts of the word they are in, and a letter
    written with an accent (`ё`, `ώ`, `が`) read as the letter without it, as its canonical decomposition has it (NFD)
    less its marks. A letter whose decomposition holds no mark stays as it is (a Korean syllable is one letter). A
    format character (a zero-width space or non-joiner, a word joiner, a soft hyphen, a mark of direction) is read as a
    space: the engine reads it as the edge of a word, and finds a value next to one (`رضایی` before the ZWNJ of a
    plural). Made at its first use, in a few hundredths of a second; read in one pass of the text, with no
    normalization, which a long run of marks would make take time that grows with its square."""
    table: dict[int, str | None] = {_TATWEEL: None}
    for plane in _MARKED_PLANES:
        for code in plane:
            character = chr(code)
            if _mark(character):
                table[code] = None
            elif unicodedata.category(character) == "Cf":
                table[code] = " "
            elif unicodedata.decomposition(character)[:1] not in ("", "<"):  # a canonical decomposition
                parts = unicodedata.normalize("NFD", character)
                if any(_mark(part) for part in parts):
                    table[code] = "".join(part for part in parts if not _mark(part)) or None
    return table


def _bare(folded: str) -> str:
    """``folded`` read without the accents and the marks of its letters (``_unmarked``)."""
    return folded.translate(_unmarked())


def _words(text: str) -> set[str]:
    """The runs of plain letters and digits of ``text``, in folded case, and the first and last ``EDGE`` letters of
    those that are longer (``edges``); and, when it holds another script, its runs of letters and digits of two or more
    without their marks, and every two characters in a row of a run of a script written without spaces."""
    folded = _folded(text)
    words = set()
    for match in WORD.finditer(folded):
        run = match.group()
        words.add(run)
        if len(run) > EDGE:
            words.add(run[:EDGE])
            words.add(run[-EDGE:])
    if not folded.isascii():
        for match in _RUN.finditer(_bare(folded)):
            run = match.group(1)
            if run is not None:
                words.add(run)
            else:
                run = match.group(2)
                words.update(map(operator.add, run, run[1:]))
    return words


def anchor(value: str) -> str | None:
    """The longest run of plain letters and digits of ``value``, in folded case: the first one when two are as long.
    When it has none of three, the longest run of letters and digits of any script of two or more, without its marks,
    a run of a script written without spaces counting for its first two characters; None when it has neither. A label
    of a public mail domain (`gmail`, `com`) is the anchor only of a value that has no other run. A run an escape glues
    letters to (`20martini` of `Giulia%20Martini`, `u0040nordest` of an `@` written as JSON's escape) is no anchor
    (version 3): a fake written in escapes comes back written so, and as it reads (`Giulia Martini`, 0.9.9), and only a
    run no escape touches is a word of both; where every run is glued, a run the escapes leave free (version 4:
    `393331234567` of `%2B393331234567`), and only where there is none of those either, a glued one (`41ss`)."""
    best: str | None = None
    label: str | None = None  # the longest label of a public mail domain: the anchor only when there is no other
    composed = _composed(value)
    folded = _folded(composed)
    # The value with its escapes read as separators (``words_in``'s second reading): a run of it is no escape's.
    apart = _folded(_separated(composed)) if "\\" in composed or "%" in composed else None
    for word in _unglued(WORD.findall(folded), apart, WORD.findall):
        if word in _MAIL_LABELS:
            label = word if label is None or len(word) > len(label) else label
        elif best is None or len(word) > len(best):
            best = word
    if best is not None or folded.isascii():
        return best if best is not None else label
    for word in _unglued(_runs(_bare(folded)), apart, lambda text: _runs(_bare(text))):
        if word not in _MAIL_LABELS and (best is None or len(word) > len(best)):
            best = word
    return best if best is not None else label


def edges(value: str) -> tuple[str | None, str | None]:
    """The words, besides its anchor, by which ``value`` is found glued to a letter or a digit (version 5, Ruling T10h),
    its head and its tail: a secret is found inside a word (`tok_9fQ2xL7pZ4acme`), and a fake that is no word is
    restored there (`IT84W...511D'Angelo`), where the text's run is the anchor and more. Where the anchor is a run of
    plain letters and digits that ends ``value``, its head is its first ``EDGE`` letters, which begin the run of a text
    that glues something after it; where it begins ``value``, its tail is its last ``EDGE`` letters, which end the run
    of a text that glues something before it (``words_in`` reads those of every run longer than that). None for an
    anchor of ``EDGE`` letters or fewer (where it ends or begins a text's run, those are the run's first or last
    letters, which a text reads already), nor for one inside ``value``, which no glued letter touches. An anchor that is
    ``value``'s only run, glued on both sides, is found by neither (``glued_unfound``)."""
    word = anchor(value)
    if word is None or len(word) <= EDGE:
        return None, None
    at_end, at_start = _touches(value, word)
    return (word[:EDGE] if at_end else None), (word[-EDGE:] if at_start else None)


def _touches(value: str, word: str) -> tuple[bool, bool]:
    """Whether ``word``, ``value``'s anchor, is the run that ends it and the one that begins it."""
    folded = _folded(_composed(value))
    runs = WORD.findall(folded)
    return bool(runs) and runs[-1] == word and folded.endswith(word), bool(runs) and runs[0] == word and (
        folded.startswith(word)
    )


def glued_unfound(value: str) -> bool:
    """Whether ``value``, glued to a letter or a digit on the side its anchor touches, can be in a text none of whose
    words finds it (version 8, review 10b's I1), so that a vault that finds it inside a word (a secret) holds it in
    memory for good: its anchor begins or ends it and has ``EDGE`` letters or fewer (a text's run is then the anchor
    and more, and a text gives the first and last letters of its runs ``EDGE`` at a time), or ``value`` is one run of
    ``ONE_RUN_HELD`` letters or fewer (glued on both sides, it is in the middle of a text's run). A longer string of one
    run glued on both sides is found by no word: its letters are in the middle of a run of the text, whose words are
    the run and its first and last ``EDGE`` letters, and a text that gave every ``EDGE`` letters in a row of its runs
    would have many times the words."""
    word = anchor(value)
    if word is None or not WORD.fullmatch(word):
        return False
    at_end, at_start = _touches(value, word)
    if not (at_end or at_start):
        return False
    return len(word) <= EDGE or (at_end and at_start and len(word) <= ONE_RUN_HELD)


def address_word(address: str) -> str | None:
    """The word an address's fake is found by (version 8): its local part as a dotted word with its domain's name
    (`lily.harris@liberty-d05e` for `lily.harris@liberty-d05e.example`), which few fakes share, where thousands of fakes
    at as many companies share a local part in a vault of a million. None where the local part is no dotted word, or
    the domain has no name of letters, digits and hyphens."""
    local, at, domain = _folded(address).rpartition("@")
    name = domain.split(".", 1)[0].strip("-")
    word = dotted(local) if at else None
    if word is None or not name or not re.fullmatch(r"[a-z0-9-]+", name):
        return None
    return f"{word}@{name}"


def _runs(bare: str) -> list[str]:
    """The runs of letters and digits of any script of ``bare`` (``_RUN``), a run of a script written without spaces
    for its first two characters."""
    return [match.group(1) or match.group(2)[:2] for match in _RUN.finditer(bare)]


def _unglued(runs: list[str], apart: str | None, runs_of: Callable[[str], list[str]]) -> list[str]:
    """``runs`` but those an escape glues letters to: those that are no run of ``apart`` (the value with its escapes
    read as separators, ``runs_of`` reading it). Where every one is glued (each starts at an escape of the string's
    own: `%2B393331234567`, `s%40fy%2Ft1lb`), the runs of ``apart``, which the escapes leave free (version 4): words of
    the string written as it is (``words_in``'s second reading) and as it reads; where it has none, ``runs``."""
    if apart is None:
        return runs
    free = runs_of(apart)
    whole = set(free)
    return [run for run in runs if run in whole] or free or runs


def words_in(text: str, *, dotted: bool = False) -> set[str]:
    """Every run of plain letters and digits of ``text`` that could be an anchor, in folded case: as the text is
    written, with its escapes read as separators (written once or twice), with the escapes that stand for a letter
    read as the letter, and as the masker reads its escapes (``escapes.decoded``: HTML's references, git's octal bytes,
    Python's long escape too), where a value the vault knows is found written in them (0.9.9). ``dotted``: its dotted
    words too (``dotted``: an address's local part, an IPv4 address, a domain), by which a store finds the values and
    fakes of those shapes (``stored.word_of``)."""
    text = _composed(text)
    readings = {text}
    if "\\" in text or "%" in text:
        once = _separated(text)
        readings |= {once, _doubled(once), _doubled(_separated(_composed(decoded(text))))}
    if "\\" in text or "%" in text or "&" in text:
        reading = as_escapes_read(text)
        if reading is not None:
            readings.add(_composed(reading.text))
    words: set[str] = set()
    for reading in readings:  # each read once, though two readings are often one text
        words |= _words(reading)
        if dotted:
            words |= _dotted_words(_folded(reading))
    return words


def dotted(string: str) -> str | None:
    """``string`` as a dotted word (version 6, Task 10's F3), in folded case: runs of plain letters and digits with the
    dots, hyphens and underscores between them, those at its ends left out, of at most ``SEGMENTS_AT_MOST`` runs and
    three characters at least (`lily.harris`, `198.51.100.7`, `liberty-d05e.example`); None for one with another sign
    or letter, or too long or too short. A text read for fakes holds it wherever it holds ``string`` with no letter or
    digit glued to it (``words_in``), so that a fake Latet made in such a shape is found by it alone, where its anchor
    is shared by thousands (the domain of the fakes at one company, `example`, the first number of an IPv4 address)."""
    folded = _folded(string).strip("._-")
    if len(folded) < 3 or not _DOTTED_WHOLE.fullmatch(folded):
        return None
    return folded if len(_SEGMENT.findall(folded)) <= SEGMENTS_AT_MOST else None


def _dotted_words(folded: str) -> set[str]:
    """The dotted words of a text (``dotted``): of each run of letters, digits, dots, hyphens and underscores, every
    part of two to ``SEGMENTS_AT_MOST`` of its runs of letters and digits in a row, of three characters at least (a run
    alone is one of its words already); and the words of its addresses (``address_word``): each part of one to
    ``SEGMENTS_AT_MOST`` runs that ends the local part, with the domain's name, so that a fake address with a letter
    glued before it by a hyphen or a dot is found as its local part is. Each `@` is read with the run that ends at it
    and the name after it, whatever the address before took: `ops@gktk.fzy8n@liberty-6a4e.example` gives the second
    address's word too."""
    words: set[str] = set()
    local_at: dict[int, str] = {}  # each run, by where it ends: the local part of an `@` there
    for match in _DOTTED.finditer(folded):
        run = match.group()
        local_at[match.end()] = run
        segments = [(m.start(), m.end()) for m in _SEGMENT.finditer(run)]
        for i, (start, _) in enumerate(segments):
            for j in range(i + 1, min(len(segments), i + SEGMENTS_AT_MOST)):
                word = run[start : segments[j][1]]
                if len(word) >= 3:
                    words.add(word)
    at = folded.find("@")
    while at != -1:
        local, name = local_at.get(at), _NAME.match(folded, at + 1)
        domain = name.group().strip("-") if name is not None else ""
        if local is not None and domain:
            for start in [m.start() for m in _SEGMENT.finditer(local)][-SEGMENTS_AT_MOST:]:
                word = dotted(local[start:])
                if word is not None:
                    words.add(f"{word}@{domain}")
        at = folded.find("@", at + 1)
    return words
