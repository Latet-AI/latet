r"""Escapes as an agent reads them, in a file or a tool's output: they glue a value to a letter, and write a value in
other characters than its own.

In `"Anna\nluca.neri@acme.it"` the line break is written as an escape, a backslash and an `n`. The `n` is a word
character, so as it is written the address has no word boundary before it: a detector that wants one misses the
value (an IBAN, a card number, a term) or takes the `n` with it (`nluca.neri@…`), and a restore that wants whole
words leaves a fake name at the start of a line. Read again with the escapes' letters blanked out (the same length,
so every place holds), the value stands on its own.

Where no JSON document is read value by value, a value is also written in escapes of its own characters: JSON text in
a code fence or in JSON lines writes `ò` as a backslash, a `u` and four hex digits, and a document held in a string of
another doubles that backslash; a log writes the bytes of its UTF-8 as `\xc3\xb2`, and git a path's as `\303\262`; a
URL writes `@` as `%40` and a space as `%20`, or as `+` in a query or a form; HTML writes `&#242;` or `&ograve;`. Read
as written, `Niccolò Ferri` is not there. So the text is read once more as its escapes read (``decoded``), each escape
the character it stands for, and a value found there stands at the escapes it is written with (``Decoded.span``). A
text whose escapes are of white space alone (line breaks, tabs) is not read so: blanked out, it is read already.
"""

from __future__ import annotations

import json
import re
from array import array
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from html.entities import name2codepoint
from itertools import accumulate
from urllib.parse import quote, quote_plus, unquote, unquote_plus

from latet.engine.types import EntityClass, Span

# A backslash that is not itself escaped (`\\`), then the letter of an escape, or `u` and four hex digits.
_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\([nrtbf]|u[0-9A-Fa-f]{4})")
# An escape at a text's end that ends in a letter or a digit as written, of every kind ``decoded`` reads (behind any
# run of backslashes, a nested document's too; `%XX`; a reference by number with no `;`): what follows it begins a word
# where the decoded reading found a value, and a restore must see it so (Ruling 099-R2).
_AT_END = re.compile(
    r"(?:\\+(?:[nrtbf]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-3][0-7]{2})"
    r"|%[0-9A-Fa-f]{2}|&#(?:[xX][0-9A-Fa-f]{1,6}|[0-9]{1,7}))\Z"
)
TAIL = 16  # enough of a text's end to see an escape there, ten characters at most, and the backslashes before it

# An escape of a character, as ``decoded`` reads it. Backslashes, then: `u` and four hex digits (half of a character
# past the first 65,536, the two halves read as one), `U` and eight, `x` and two (a byte), three octal digits (a byte,
# as git writes a path's bytes past ASCII), the letter of a white space, a quote or a slash. A byte as `%` and two hex
# digits. An HTML reference, by number or by name. An even run of backslashes is one escape, as a document held in a
# string of another doubles each; an odd one is backslashes written as two each, then the escape (`C:\\` and then `É`).
# The backslashes are taken at once and only where they begin (`++` and the look behind): a run of a million of them
# is read once. One group: the text splits into what is written as it reads and its escapes (``re.split``).
_BODIES = r"u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-3][0-7]{2}|[\"'/]"
_OTHERS = r"|%[0-9A-Fa-f]{2}|&#(?:[xX][0-9A-Fa-f]{1,6}|[0-9]{1,7});?|&[A-Za-z][A-Za-z0-9]{1,31};"
_UNIT = re.compile(r"((?<!\\)\\++(?:" + _BODIES + r"|[nrtbf])" + _OTHERS + ")")
# An escape of a character or of a quote. A text with none, whose escapes are of white space alone (line breaks, tabs),
# is not read again: blanked out, it is read already (``read_escaped``).
_CHARACTER = re.compile(r"(?<!\\)\\++(?:" + _BODIES + ")" + _OTHERS)
# How an escape reads (``_unit``): as written (a name HTML has not), a character, a byte written with `\x`, with `%`
# or in octal (read with the bytes next to it, as UTF-8; octal ones only so, as git writes them), the first or the
# second half of a character.
_WRITTEN, _CHAR, _BYTE, _URL_BYTE, _OCTAL_BYTE, _FIRST_HALF, _SECOND_HALF = range(7)
_LETTERS = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
_NAMES = {**name2codepoint, "apos": 0x27}
# A `+` in the value of a query or a form is a space (`application/x-www-form-urlencoded`): after a `=`, up to what
# ends the value. Elsewhere it is a sign (`C++`, `a + b`, a URL's path).
_FORM = re.compile(r"=[^\s&#\"'<>=+]*\+[^\s&#\"'<>=]*")
_UNREADABLE = "\N{REPLACEMENT CHARACTER}"  # half of a character with no other half, as ``wellformed`` reads one


def escapes(text: str) -> list[tuple[int, int]]:
    """Where the letters of ``text``'s escapes are."""
    return [m.span(1) for m in _ESCAPE.finditer(text)] if "\\" in text else []


def breaks(text: str) -> list[tuple[int, int]]:
    """Where ``text`` writes whitespace as an escape (a line break, a tab...): its backslash and letter."""
    return [(start - 1, end) for start, end in escapes(text) if end - start == 1]


def blanked(text: str, places: list[tuple[int, int]]) -> str:
    """``text`` with the characters at ``places`` replaced by spaces."""
    parts: list[str] = []
    last = 0
    for start, end in places:
        parts += [text[last:start], " " * (end - start)]
        last = end
    parts.append(text[last:])
    return "".join(parts)


def ends_with_escape(text: str) -> bool:
    """``text`` ends with an escape: what follows it starts a word."""
    tail = text[-TAIL:]
    return ("\\" in tail or "%" in tail or "&#" in tail) and _AT_END.search(tail) is not None


@dataclass(frozen=True, slots=True, eq=False)
class Decoded:
    """A text as its escapes read (``decoded``): each escape one character, the one it stands for. ``characters``: an
    escape in it writes a character (a letter, a byte, a slash), not only a white space, a quote or a form's space:
    the detectors of personal data read such a reading too, the detectors of whole values alone another
    (``Masker.detect``)."""

    text: str = field(repr=False)  # what the text holds, never in a log line
    characters: bool
    _at: array[int] = field(repr=False)  # where each escape read stands in ``text``, in order
    _starts: array[int] = field(repr=False)  # where it is written in the text read: its first character
    _ends: array[int] = field(repr=False)  # the character after its last

    def written(self, start: int, end: int) -> tuple[int, int]:
        """Where ``self.text[start:end]`` is written in the text read: with each escape it holds, whole."""
        return self._written(start, False), self._written(end - 1, True)

    def _written(self, at: int, after: bool) -> int:
        k = bisect_right(self._at, at) - 1
        if k >= 0 and self._at[k] == at:
            return self._ends[k] if after else self._starts[k]
        read, written = (self._at[k] + 1, self._ends[k]) if k >= 0 else (0, 0)
        return written + at - read + after

    def read(self, start: int, end: int) -> str | None:
        """What ``[start:end]`` of the text read reads, or None where one of its ends falls inside an escape."""
        first, last = self._read(start), self._read(end)
        return None if first is None or last is None else self.text[first:last]

    def _read(self, place: int) -> int | None:
        k = bisect_right(self._starts, place) - 1
        if k >= 0 and place < self._ends[k]:
            return self._at[k] if place == self._starts[k] else None
        read, written = (self._at[k] + 1, self._ends[k]) if k >= 0 else (0, 0)
        return read + place - written

    def span(self, found: Span, text: str) -> Span:
        """``found`` in ``self.text``, at its place in ``text``: with the value it reads (``Span.value``) where ``text``
        writes it otherwise."""
        start, end = self.written(found.start, found.end)
        reads = self.text[found.start : found.end]
        return Span(
            start, end, found.etype, found.eclass, found.rule, found.score, None if reads == text[start:end] else reads
        )


def _character(code: int) -> str:
    """The character of ``code``, or U+FFFD where it is none (zero, half of one, past Unicode's last)."""
    return _UNREADABLE if code == 0 or code > 0x10FFFF or 0xD800 <= code < 0xE000 else chr(code)


def _unit(unit: str) -> tuple[int, str, int, bool]:
    """How the escape ``unit`` (``_UNIT``'s) reads (``_CHAR``...), the character it reads or the byte or the half of
    one, and whether it writes a character, not only a white space or a quote (``Decoded.characters``)."""
    if unit[0] == "%":
        return _URL_BYTE, "", int(unit[1:], 16), True
    if unit[0] == "&":
        if unit[1] != "#":
            code = _NAMES.get(unit[1:-1])
            return (_WRITTEN, unit, 0, False) if code is None else (_CHAR, chr(code), 0, True)
        number = unit[2:].rstrip(";")
        return _CHAR, _character(int(number[1:], 16) if number[0] in "xX" else int(number)), 0, True
    body = unit.lstrip("\\")
    letter = body[0]
    if letter == "u":
        code = int(body[1:], 16)
        if 0xD800 <= code < 0xE000:
            return (_FIRST_HALF if code < 0xDC00 else _SECOND_HALF), "", code, True
        return _CHAR, chr(code), 0, True
    if letter == "U":
        return _CHAR, _character(int(body[1:], 16)), 0, True
    if letter == "x":
        return _BYTE, "", int(body[1:], 16), True
    if letter.isdigit():
        return _OCTAL_BYTE, "", int(body, 8), True
    return _CHAR, _LETTERS.get(letter, letter), 0, letter == "/"


def _width(lead: int) -> int:
    """How many bytes a character of UTF-8 takes that starts with ``lead`` (1: none starts so, or an ASCII one)."""
    return 2 if 0xC2 <= lead <= 0xDF else 3 if 0xE0 <= lead <= 0xEF else 4 if 0xF0 <= lead <= 0xF4 else 1


def _spaced(found: re.Match[str]) -> str:
    return found.group().replace("+", " ")


def _decode(text: str) -> Decoded | None:
    source = text
    if "+" in text and "=" in text:
        source = _FORM.sub(_spaced, text)  # one for one: every place holds
    written = ("\\" in source or "%" in source or "&" in source) and _CHARACTER.search(source) is not None
    parts = _UNIT.split(source) if written else [source]
    if len(parts) == 1:
        return None if source == text else Decoded(source, False, array("q"), array("q"), array("q"))
    pieces: list[str] = []
    at, starts, ends = array("q"), array("q"), array("q")  # each escape read: in the reading, and as written
    place = size = 0  # where the next part is written, and where it reads
    characters = False
    # Escapes that wait for the ones right after them, all of one kind (``waits``): bytes, read as UTF-8 together, or
    # the first half of a character. Each as (where it is written, where it ends, the byte or the half).
    waiting: list[tuple[int, int, int]] = []
    waits = _WRITTEN
    known: dict[str, tuple[int, str, int, bool]] = {}  # each escape as ``_unit`` reads it: a text repeats its escapes

    def put(start: int, end: int, char: str) -> None:
        nonlocal size
        pieces.append(char)
        at.append(size)
        starts.append(start)
        ends.append(end)
        size += 1

    def keep(start: int, end: int) -> None:
        """``source[start:end]`` read as it is written."""
        nonlocal size
        pieces.append(source[start:end])
        size += end - start

    def flush(last: bool = True) -> None:
        """What waits is read: a first half alone as U+FFFD; bytes as UTF-8 where they are (`\\xc3\\xb2`), each byte one
        character elsewhere (`\\xf2`, Latin-1), but octal ones, read only where they write a character past ASCII (git
        writes no other so: `\\100` in a Windows path is a folder, no `@`). Not ``last``: the bytes of a character not
        whole yet wait on."""
        if waits == _FIRST_HALF:
            put(waiting[0][0], waiting[0][1], _UNREADABLE)
            waiting.clear()
            return
        data = bytes([byte for _, _, byte in waiting])
        i = 0
        while i < len(data):
            width = _width(data[i])
            if width > 1 and i + width > len(data) and not last:
                break
            char = None
            if width > 1 and i + width <= len(data):
                try:
                    char = data[i : i + width].decode()
                except UnicodeDecodeError:
                    char = None
            if char is None and waits == _OCTAL_BYTE:
                keep(waiting[i][0], waiting[i][1])
                i += 1
                continue
            if char is None:
                width, char = 1, chr(data[i])
            put(waiting[i][0], waiting[i + width - 1][1], char)
            i += width
        del waiting[:i]

    for i in range(1, len(parts), 2):
        before, unit = parts[i - 1], parts[i]
        if before:
            if waiting:
                flush()
            pieces.append(before)
            size += len(before)
            place += len(before)
        run = len(unit) - len(unit.lstrip("\\"))
        if run > 1 and run % 2:  # an odd run: backslashes written as two each, then the escape with the last one
            if waiting:
                flush()
            keep(place, place + run - 1)
            place += run - 1
            unit = unit[run - 1 :]
        found = known.get(unit)
        if found is None:
            found = known[unit] = _unit(unit)
        how, char, number, deep = found
        end = place + len(unit)
        if how == _CHAR:
            if waiting:
                flush()
            put(place, end, char)
            characters = characters or deep
        elif how in (_BYTE, _URL_BYTE, _OCTAL_BYTE):
            if waiting and waits != how:
                flush()
            waits = how
            waiting.append((place, end, number))
            if len(waiting) >= _width(waiting[0][2]):
                flush(last=False)
            characters = True
        elif how == _SECOND_HALF and waiting and waits == _FIRST_HALF:
            first = waiting.pop()
            put(first[0], end, chr(0x10000 + ((first[2] - 0xD800) << 10) + number - 0xDC00))
        elif how == _WRITTEN:  # a name that is not HTML's
            if waiting:
                flush()
            pieces.append(unit)
            size += len(unit)
        else:  # half of a character: the first waits for its second
            if waiting:
                flush()
            if how == _FIRST_HALF:
                waits = how
                waiting.append((place, end, number))
            else:
                put(place, end, _UNREADABLE)
            characters = True
        place = end
    if waiting:
        flush()
    if not at:  # names alone, none of them HTML's
        return None if source == text else Decoded(source, False, at, starts, ends)
    pieces.append(parts[-1])
    return Decoded("".join(pieces), characters, at, starts, ends)


_URL_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _spaces_as_plus(written: str, reads: str) -> bool:
    """``written`` writes a space as `+` (a query's or a form's value): it holds more `+` than ``reads`` holds of its
    own, those written `%2B`."""
    return "+" in written and written.count("+") > reads.count("+") - len(re.findall("%2[Bb]", written))


def written_like(fake: str, written: str, reads: str) -> str:
    """``fake``, drawn for what ``written`` reads (``reads``), written as ``written`` writes it where its escapes are
    part of the syntax. In a URL (`%XX`, or `+` for a space), each character ``written`` does not write as it is,
    quoted (a space as `+` where it writes one so): the URL stays one, its fields as they were. Elsewhere, each sign
    ``written`` writes as an escape (a quote as `\\'`, `@` as `&#64;`) the same way, so a string or a page stays as it
    was; a letter as it is drawn (an ASCII fake needs no escape)."""
    if _URL_ESCAPE.search(written) or _spaces_as_plus(written, reads):
        plus = _spaces_as_plus(written, reads)
        raw = set(written) - ({"%", "+"} if plus else {"%"})
        out = []
        for char in fake:
            if char == " " and plus:
                out.append("+")
            elif char in _UNRESERVED or char in raw:
                out.append(char)
            else:
                out.append(quote(char, safe=""))
        return "".join(out)
    reading = _decode(written)
    if reading is None:
        return fake
    signs: dict[str, str] = {}
    for k, at in enumerate(reading._at):
        char = reading.text[at]
        if not char.isalnum():
            signs.setdefault(char, written[reading._starts[k] : reading._ends[k]])
    return "".join(signs.get(char, char) for char in fake)


def _in_a_link(written: str, fake: str, words: bool) -> bool:
    """``written`` is kept as a link or a form writes it: it holds a `%XX`, or, a value written in words (``words``: a
    name, a term), a `+` for a space that its fake writes so too (`Mario+Rossi`, `Nicola+Greco`). A `+` in any other
    value is a sign, as it is written (a phone's `+39`, a sub-address's `+crm`, a token's): it reads as it is written.
    The vault keeps no mark of how a value was found: this is read from how it is written."""
    return _URL_ESCAPE.search(written) is not None or (words and "+" in written and "+" in fake)


def _escaped(written: str) -> Decoded | None:
    """``written`` as its escapes read, where it holds one that the decoded reading reads (`\\u00f2`, `&#242;`, `\\'`):
    a form's `+` alone is left to ``_in_a_link``."""
    value = _decode(written)
    return value if value is not None and len(value._at) > 0 else None


def written_forms(written: str, fake: str, *, words: bool) -> list[str]:
    """Other forms a model may write ``fake`` in, the fake of a value kept as ``written``, written in escapes: in a URL,
    its fake quoted again (``quote``, ``quote_plus``); elsewhere, the fake as JSON writes it with ASCII alone. Each
    stands for ``written`` as it is (``restores_for``). ``words``: the value is written in words (``_in_a_link``)."""
    if _in_a_link(written, fake, words):
        forms = []
        for plain in dict.fromkeys((unquote(fake), unquote_plus(fake))):
            forms += [quote(plain, safe=""), quote_plus(plain, safe="")]
        return [form for form in dict.fromkeys(forms) if form != fake]
    if _escaped(written) is None:
        return []
    reading = _decode(fake)
    form = json.dumps(reading.text if reading is not None else fake)[1:-1]
    return [form] if form != fake else []


def readings(written: str, fake: str, *, words: bool) -> tuple[str, str] | None:
    """How ``fake`` and the value it stands for read, where that value is kept as ``written`` in escapes
    (`Nicola Greco` and `Mario Rossi` for `Nicola%20Greco` and `Mario%20Rossi`); None where it is not written in
    escapes, or reads as no text (bytes that are not UTF-8). ``words``: the value is written in words, where a `+` can
    be a form's space (``_in_a_link``)."""
    if _in_a_link(written, fake, words):
        plus = "+" in written and "%20" not in written.upper() and "%20" not in fake.upper()
        try:
            if plus:
                return unquote_plus(fake, errors="strict"), unquote_plus(written, errors="strict")
            return unquote(fake, errors="strict"), unquote(written, errors="strict")
        except UnicodeDecodeError:
            return None
    value = _escaped(written)
    if value is None:
        return None
    reading = _decode(fake)
    return (reading.text if reading is not None else fake), value.text


def cased(text: str, upper: bool) -> str:
    """``text`` in capitals (``upper``) or in small letters, but its escapes, written as they are: `\\U00F2` or
    `&OGRAVE;` is none (``restores_for``)."""
    if "\\" not in text and "%" not in text and "&" not in text:
        return text.upper() if upper else text.lower()
    parts = _UNIT.split(text)
    return "".join((part.upper() if upper else part.lower()) if k % 2 == 0 else part for k, part in enumerate(parts))


_last: tuple[str, Decoded | None] = ("", None)  # the text read last, and its reading


def decoded(text: str) -> Decoded | None:
    """``text`` as its escapes read (the module's words), or None where it holds none. A masking reads one text many
    times (each of its detectors, the values found in it again, the fakes), and the final check reads it after: the last
    text read is kept with its reading, and read once."""
    global _last
    last = _last
    if last[0] is text:
        return last[1]
    found = _decode(text)
    _last = (text, found)
    return found


def _covering(spans: list[Span]) -> Callable[[Span, int], bool]:
    """Whether a span lies inside one of ``spans`` of a class at least as strong, from ``start`` on."""
    ordered = sorted(spans, key=lambda f: f.start)
    # By class: a finding gives way only to one at least as strong (a key, not the table cell around it).
    tables: dict[EntityClass, tuple[list[int], list[int]]] = {}

    def covered(span: Span, start: int) -> bool:
        if span.eclass not in tables:
            strong = [f for f in ordered if f.eclass >= span.eclass]
            # Their starts, and the furthest end of those starting so far.
            tables[span.eclass] = ([f.start for f in strong], list(accumulate((f.end for f in strong), max)))
        starts, reach = tables[span.eclass]
        i = bisect_right(starts, start)
        return i > 0 and reach[i - 1] >= span.end

    return covered


def read_escaped(
    text: str,
    read: Callable[[str], list[Span]],
    again: Callable[[str], list[Span]] | None = None,
    deep: Callable[[str, bool], list[Span]] | None = None,
) -> list[Span]:
    """What ``read`` finds in ``text``, with what it finds (or ``again``, when given) where the letters of the
    text's escapes are blanked out, and where the escapes read as what they stand for (``decoded``; ``deep``, when
    given, is told whether an escape there writes a character, ``Decoded.characters``).

    A value found blanked out stands for its place: a finding of no stronger class that lies inside it, or only adds
    escape letters to it (`nluca.neri@…`), gives way. A value found where the escapes read stands at the escapes it is
    written with, with the value it reads (``Span.value``), and is added to what the text as written holds: never less
    is found. One that reads as it is written gives way to a finding of no weaker class around it."""
    spans = read(text)
    places = escapes(text)
    if places:
        found = sorted((again or read)(blanked(text, places)), key=lambda f: f.start)
        after = {p: end for start, end in places for p in range(start, end)}
        inside = _covering(found)
        spans = list(dict.fromkeys([*(s for s in spans if not inside(s, after.get(s.start, s.start))), *found]))
    reading = decoded(text)
    if reading is None:
        return spans
    more = deep(reading.text, reading.characters) if deep is not None else (again or read)(reading.text)
    placed = [reading.span(found, text) for found in more]
    inside = _covering(spans)
    return list(dict.fromkeys([*spans, *(p for p in placed if p.value is not None or not inside(p, p.start))]))
