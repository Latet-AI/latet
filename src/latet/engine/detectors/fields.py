"""Values by the field they sit in: a column of a table, a JSON key or path, the key of ``key=value``.

A tool output that is data (a query's rows, an API's JSON, a log's ``key=value`` pairs) says what
each value is by its column or key. Three rules use that:

- **Names that say secret** (``password_hash``, ``api_key``, ``reset_token``): their values are
  secrets, even when the name is on a line of its own, as in a CSV header. Always on.
- **Your field rules** (``fields:``): the values of a field masked with its type, or kept in clear.
- **Strict data mode** (``data: strict``): every other value of the structure is masked, as STRUCT.

A value found by its field stays in its structure (class STRUCT): it never becomes a value masked
everywhere, so ``admin`` in a table leaves ``admin`` in code alone. Values of one or two characters
stay in clear: they say little, and a fake that short could not be told apart in an answer.

The structures found in a text are psql, CSV, TSV, markdown and HTML tables, the tables of a
browser's accessibility snapshot, lines of ``key=value`` pairs and ``"key": value`` fragments. A
JSON document's values are masked one by one, each with its path (``items[].sku``).
"""

from __future__ import annotations

import bisect
import fnmatch
import re
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import re2

from latet.config import FieldRule
from latet.engine import document
from latet.engine.detectors.base import DetectContext, quiet_options
from latet.engine.detectors.pii import DEFAULT_PHONE_REGIONS, phone_end
from latet.engine.detectors.structural import (
    _PLACEHOLDER,
    _STATUS,
    _key_kind,
    _status_name,
    _status_shaped,
    connection_secrets,
)
from latet.engine.types import EntityClass, Policy, SlotKind, Span

# Tools that read files: what they return is code or documents, never "data" for strict mode.
FILE_TOOLS = frozenset(
    {"read", "notebookread", "grep", "glob", "ls", "read_file", "view", "list_dir", "list_directory", "search_files"}
)
# Tools that bring public web content: search results and pages are not your data, and masking every
# title and address in them would leave the model nothing to read or cite. Left alone unless
# `data_tools` names them; the other detectors still apply.
WEB_TOOLS = frozenset({"websearch", "webfetch", "web_search", "web_fetch"})
# The file tools that return a file's text: what a rule of `data_files` can make data (a search's lines and a listing
# are not a file).
READ_TOOLS = frozenset({"read", "notebookread", "read_file", "view"})
# The tools that change a file and echo its lines (Claude Code's Edit, MultiEdit and Write print the lines around the
# change, numbered as a read prints them, lines the agent may never have read): a file a rule of `data_files` names is
# read there as a read of it is.
EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "notebookedit", "edit_file", "write_file"})
_MARKDOWN = (".md", ".markdown", ".mdx")
_MAX_TEXT = 2_000_000  # characters looked through for structures
_SHORTEST = 3  # a value shorter than this stays in clear (secret names have their own lengths)
# The longest field a rule can name (``FieldRule.name``): a value's rule names its field up to this length.
_NAMED = next(m.max_length for m in FieldRule.model_fields["name"].metadata if hasattr(m, "max_length"))
_NOTHING = re.compile(r"(?i)^(?:|null|none|nil|n/a|-+|true|false|\(null\))$")
# What Latet writes in place of a value it redacts (``Masker._apply``: ``[REDACTED:<its type>]``, the type in capitals,
# a field rule's own type in whatever characters it is written): no value of yours, and the one placeholder a secret
# field rule of your own leaves alone. The built-in names leave every placeholder (``_PLACEHOLDER``); a rule of yours
# masks the rest (`$Pa55word`, `<secret>` are passwords too).
_REDACTED = re.compile(r"^\[REDACTED:[^\]]+\]$")
# The words a name that says secret holds, which a text must hold somewhere for there to be anything to look for
# (``_secretish``), in any case. Read by RE2, in time in proportion to the text and letting the other threads run:
# Python's re read 4 MiB in a third of a second, holding the interpreter, and every other thread waited (an event
# loop's too). Case-insensitively as Python's re read them, which takes a dotted capital I and a dotless i for an i:
# RE2 takes neither, so they are named. Its other cases are Python's (a long s for an s, the Kelvin sign for a k).
_SECRETISH = re2.compile(
    "(?i)pass|pwd|secret|token|key|cred|auth|salt|"
    "cook[i\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}\N{LATIN SMALL LETTER DOTLESS I}]e|dsn|bearer",
    quiet_options(),
)
# A value that names another rather than holding one, which strict data mode leaves in clear: a template's or a
# shell's interpolation of a variable (`${from}`, `{{ subject }}`, `$MAIL_FROM`, `%APPDATA%`, `%(name)s`,
# `{recipient}`), as code a command prints holds it. Narrower than the secret names' placeholders (``_PLACEHOLDER``):
# `<ann@acme.com>` or `[Ann Ross]` is a value of yours, and so are `$Secr3tPass` and `{marta.galli}` (a shell's
# variable is named in capitals, a format's field has no dots).
_INTERPOLATION = re2.compile(
    r"^(?:\$\{\s*[A-Za-z_$][\w$.]*\s*\}|\{\{\s*[A-Za-z_][\w.]*\s*\}\}|\$[A-Z_][A-Z0-9_]*|%[A-Za-z_]\w*%|"
    r"%\([A-Za-z_]\w*\)[sdifr]|\{[A-Za-z_]\w*\})$",
    quiet_options(),
)
# The attributes of markup that say how a page looks (an email template a command prints): `style="display: block"`,
# `class="btn"`. Strict data mode leaves their values in clear inside a tag alone, and a style that loads an address
# (`url(...)`) masked; a data attribute beside them is data, and a column, a key or a log field named class holds a
# school's or a clinic's class.
_MARKUP_FIELDS = frozenset({"style", "class", "classname"})
# A column name of a CSV header: an identifier, or up to three words (`First Name`), never prose.
_COLUMN = re.compile(r"^[A-Za-z_][\w.\-]{0,47}(?: [\w.\-]{1,24}){0,2}$")


@dataclass(frozen=True, slots=True)
class Cell:
    start: int
    end: int
    field: str
    markup: bool = False  # an attribute inside a tag of markup (``<a class="btn">``)


def _lines(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append((pos, line.rstrip("\r\n")))
        pos += len(line)
    return out


def _strip(line: str, start: int, end: int) -> tuple[int, int]:
    while start < end and line[start] in " \t":
        start += 1
    while end > start and line[end - 1] in " \t":
        end -= 1
    return start, end


def _split(line: str, sep: str) -> list[tuple[int, int]]:
    """Fields of a separated line as (start, end), outside double quotes; quotes are left out."""
    fields, start, quoted, i = [], 0, False, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
        elif c == sep and not quoted:
            fields.append((start, i))
            start = i + 1
        i += 1
    fields.append((start, len(line)))
    out = []
    for a, b in fields:
        a, b = _strip(line, a, b)
        if b - a >= 2 and line[a] == '"' and line[b - 1] == '"':
            a, b = a + 1, b - 1
        out.append((a, b))
    return out


# --- tables --------------------------------------------------------------------------------------

_PSQL_RULE = re.compile(r"^[ \t]*-+(?:\+-+)+[ \t]*$")
# Each run of blanks, dashes or colons is taken whole and never given back (``*+``), which no line that is a rule needs:
# the same lines, each read once, where the blanks beside an optional ``|`` were shared out between the two runs in
# every way first, and a line of twenty thousand blanks held the interpreter for three seconds and a half.
_MD_RULE = re.compile(r"^[ \t]*+\|?[ \t]*+:?+-{3,}+:?+[ \t]*+(?:\|[ \t]*+:?+-{3,}+:?+[ \t]*+)+\|?[ \t]*+$")


# Tables drawn with box characters (Node's console.table, psql's unicode style, the CLIs that draw so): their vertical
# bars read as ``|`` (one character for one, so every place stays), the rule under their header as a rule.
_BOX_BARS = str.maketrans({"│": "|", "┃": "|", "║": "|"})
_BOX_RULE = re.compile(r"^[ \t]*+[├┝┠┣╞╟╠]?+[─━═]++(?:[┼┿╂╋╪╫╬][─━═]++)++[┤┥┨┫╡╢╣]?+[ \t]*+$")
_QUOTES = ("'", '"')
# The field of a table's row that does not fit its columns, masked whole: no column's name, so that no rule keeping a
# column in clear (`row`) keeps it.
_ROW = "(row)"


def _box_tables(text: str, lines: list[tuple[int, str]], used: set[int]) -> tuple[list[Cell], set[int]]:
    """The cells of the tables drawn with box characters, without the quotes console.table writes around a string."""
    plain = [(b, line.translate(_BOX_BARS)) if i not in used else (b, "") for i, (b, line) in enumerate(lines)]
    found, rows = _pipe_tables(plain, _BOX_RULE, outer=True)
    out = []
    for cell in found:
        start, end = cell.start, cell.end
        if end - start >= 2 and text[start] in _QUOTES and text[end - 1] == text[start]:
            start, end = start + 1, end - 1
        out.append(Cell(start, end, cell.field))
    return out, rows


def _pipes(line: str, left: bool, right: bool) -> list[tuple[int, int]]:
    """Segments between ``|``; with ``left`` (``right``), the empty one before the first (after the last) goes: the
    table's outer border."""
    bounds = [-1, *[i for i, c in enumerate(line) if c == "|"], len(line)]
    segments = [_strip(line, bounds[i] + 1, bounds[i + 1]) for i in range(len(bounds) - 1)]
    if left and segments and line[: bounds[1]].strip() == "":
        segments = segments[1:]
    if right and segments and line[bounds[-2] + 1 :].strip() == "":
        segments = segments[:-1]
    return segments


def _pipe_tables(lines: list[tuple[int, str]], rule: re.Pattern[str], outer: bool) -> tuple[list[Cell], set[int]]:
    """The cells of the tables whose columns ``|`` parts. With ``outer`` a table may have an outer border, as its
    header says: a table without one (psql's) has an empty first or last cell where a value is NULL. A row that does not
    fit its columns (a bar inside a value) is one cell, the row's (``_ROW``): strict data mode masks it whole."""
    cells: list[Cell] = []
    used: set[int] = set()
    i = 0
    while i + 1 < len(lines):
        header, separator = lines[i][1], lines[i + 1][1]
        if "|" not in header or not rule.match(separator):
            i += 1
            continue
        left, right = outer and header.lstrip().startswith("|"), outer and header.rstrip().endswith("|")
        names = [header[a:b] for a, b in _pipes(header, left, right)]
        used.update((i, i + 1))
        j = i + 2
        while j < len(lines) and "|" in lines[j][1]:
            base, line = lines[j]
            segments = _pipes(line, left, right)
            if len(segments) == len(names):
                cells += [Cell(base + a, base + b, name) for (a, b), name in zip(segments, names, strict=True) if b > a]
            elif segments:
                cells.append(Cell(base + segments[0][0], base + segments[-1][1], _ROW))
            used.add(j)
            j += 1
        i = j
    return cells, used


def _delimited(lines: list[tuple[int, str]], used: set[int]) -> list[Cell]:
    """CSV, TSV and semicolon tables: a header of names, then rows with as many fields."""
    cells: list[Cell] = []
    i = 0
    while i + 1 < len(lines):
        if i in used:
            i += 1
            continue
        header = lines[i][1]
        sep = next((s for s in ("\t", ",", ";") if s in header), None)
        names = [header[a:b] for a, b in _split(header, sep)] if sep else []
        if len(names) < 2 or not all(_COLUMN.match(n) for n in names):
            i += 1
            continue
        rows: list[Cell] = []
        j = i + 1
        while j < len(lines) and j not in used:
            base, line = lines[j]
            fields = _split(line, sep or ",")
            if len(fields) != len(names):
                break
            rows += [Cell(base + a, base + b, name) for (a, b), name in zip(fields, names, strict=True) if b > a]
            j += 1
        # Names of more words could be a sentence with commas: such a header needs two rows under it.
        if j - i - 1 >= (2 if any(" " in n for n in names) else 1):
            cells += rows
        i = j if j > i + 1 else i + 1
    return cells


# An HTML table's rows and cells are what ``<tr\b[^>]*>(.*?)</tr>`` and ``<(t[hd])\b[^>]*>([^<]*)</t[hd]>`` find, read
# once from start to end: those patterns read the text again to its end from every row or cell left open, and a text
# of rows opened twenty thousand times held the interpreter for seven seconds (an hour at the detector's two million
# characters).
_ROW_OPEN = re.compile(r"<tr\b", re.I)
_ROW_CLOSE = re.compile(r"</tr>", re.I)
_CELL_OPEN = re.compile(r"<(t[hd])\b", re.I)
_CELL_CLOSE = re.compile(r"</t[hd]>", re.I)


def _rows(text: str) -> Iterator[tuple[int, int]]:
    """Where each row of an HTML table holds its cells: from the end of its tag (the first ``>`` after ``<tr``) to the
    first ``</tr>`` after it. A row whose tag or whose end is not there is none, nor is any row after it, whose tag and
    end would come later still."""
    pos = 0
    while (opened := _ROW_OPEN.search(text, pos)) is not None:
        tag_end = text.find(">", opened.end())
        closed = _ROW_CLOSE.search(text, tag_end + 1) if tag_end >= 0 else None
        if closed is None:
            return
        yield tag_end + 1, closed.start()
        pos = closed.end()


def _row_cells(row: str) -> Iterator[tuple[str, int, int]]:
    """Each cell of a row: its tag's name (``td``, ``th``) and where its text is, from the end of its tag to the next
    ``<``, which must close it (``</td>``, ``</th>``). A cell that is not closed there is none, nor is any other cell
    begun in its tag, which ends where its tag ends: the next one begins at that ``<`` at the earliest."""
    pos = 0
    while (opened := _CELL_OPEN.search(row, pos)) is not None:
        tag_end = row.find(">", opened.end())
        text_end = row.find("<", tag_end + 1) if tag_end >= 0 else -1
        if text_end < 0:
            return
        closed = _CELL_CLOSE.match(row, text_end)
        if closed is None:
            pos = text_end
            continue
        yield opened.group(1), tag_end + 1, text_end
        pos = closed.end()


def _html(text: str) -> list[Cell]:
    cells: list[Cell] = []
    names: list[str] = []
    for base, stop in _rows(text):
        row = text[base:stop]
        found = list(_row_cells(row))
        if found and all(name.lower() == "th" for name, _, _ in found):
            names = [row[a:b].strip() for _, a, b in found]
            continue
        for index, (_, a, b) in enumerate(found):
            start, end = base + a, base + b
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                cells.append(Cell(start, end, names[index] if index < len(names) else f"column {index + 1}"))
    return cells


_SNAP_HEADER = re.compile(r'\bcolumnheader "((?:[^"\\]|\\.)*)"')
_SNAP_CELL = re.compile(r'\b(?:grid)?cell "((?:[^"\\]|\\.)*)"')
_SNAP_ROW = re.compile(r"(?:^|[\s-])row\b")
_SNAP_TABLE = re.compile(r"(?:^|[\s-])(?:table|grid)\b")


def _snapshot(lines: list[tuple[int, str]]) -> list[Cell]:
    """Tables in a browser's accessibility snapshot: ``columnheader "Name"``, then ``cell "Marta"``."""
    cells: list[Cell] = []
    names: list[str] = []
    index = 0
    for base, line in lines:
        if _SNAP_TABLE.search(line) and "cell" not in line and "columnheader" not in line:
            names, index = [], 0
        header = _SNAP_HEADER.search(line)
        if header:
            names.append(header.group(1))
            continue
        cell = _SNAP_CELL.search(line)
        if cell:
            if cell.end(1) > cell.start(1):
                field = names[index] if index < len(names) else f"column {index + 1}"
                cells.append(Cell(base + cell.start(1), base + cell.end(1), field))
            index += 1
        elif _SNAP_ROW.search(line):
            index = 0
    return cells


# --- key=value and "key": value -----------------------------------------------------------------

_KV = re.compile(r'(?<![\w.\-/])([A-Za-z_][\w.\-]{0,40})=("([^"\n]{0,2000})"|[^\s&,;"\'<>()\[\]{}]{1,2000})')
_JSONISH = re.compile(r'"([A-Za-z_][\w.\-]{0,64})"\s*:\s*(?:"((?:[^"\\\n]|\\.){0,4000})"|(-?\d+(?:\.\d+)?)(?![\w.]))')


def _kv_pairs(line: str, regions: Sequence[str]) -> list[tuple[re.Match[str], int]]:
    """The ``key=value`` pairs of a line, each with where its value ends: at the first space or delimiter, but over the
    groups of a phone number that begins it (``phone=+39 347 123 4567``), whose spaces are no end."""
    pairs: list[tuple[re.Match[str], int]] = []
    for m in _KV.finditer(line):
        end = m.end()
        if m.group(3) is None:  # not quoted
            stop = phone_end(line, m.start(2), regions, limit=end)
            if stop is not None and stop > end:
                end = stop
        pairs.append((m, end))
    return pairs


def _key_values(lines: list[tuple[int, str]], regions: Sequence[str] = DEFAULT_PHONE_REGIONS) -> list[Cell]:
    """``key=value`` pairs on log lines (two or more, apart by spaces only) and ``"key": value``."""
    cells: list[Cell] = []
    for base, line in lines:
        if "=" in line:
            angles = _angles(line)
            run: list[tuple[re.Match[str], int]] = []
            for pair in _kv_pairs(line, regions):
                if run and line[run[-1][1] : pair[0].start()].strip(" \t"):
                    if len(run) >= 2:
                        cells += [_kv_cell(base, line, r, end, angles) for r, end in run]
                    run = []
                run.append(pair)
            if len(run) >= 2:
                cells += [_kv_cell(base, line, r, end, angles) for r, end in run]
        if '":' in line or '" :' in line:
            for m in _JSONISH.finditer(line):
                group = 2 if m.group(2) is not None else 3
                if m.end(group) > m.start(group):
                    cells.append(Cell(base + m.start(group), base + m.end(group), m.group(1)))
    return cells


_ANGLE = re.compile(r"[<>]")


def _angles(line: str) -> tuple[list[int], list[int]]:
    """Where the ``<`` and the ``>`` of a line are, in order: a pair asks for the last of each before it once, not by
    reading back through the line, which costs the square of its length on a long line of pairs."""
    opens: list[int] = []
    closes: list[int] = []
    if "<" in line:
        for m in _ANGLE.finditer(line):
            (opens if m.group() == "<" else closes).append(m.start())
    return opens, closes


def _last_before(places: list[int], at: int) -> int:
    """The last of ``places`` (in order) before ``at``, or -1."""
    i = bisect.bisect_left(places, at)
    return places[i - 1] if i else -1


def _kv_cell(base: int, line: str, m: re.Match[str], end: int, angles: tuple[list[int], list[int]]) -> Cell:
    """``end``: where the pair ends (``_kv_pairs``): a quoted value ends inside it, before its quote. ``angles``: where
    the line's ``<`` and ``>`` are (``_angles``)."""
    group = 3 if m.group(3) is not None else 2
    # An attribute of markup: quoted, inside a tag (``<`` and a name before it, no ``>`` between), not after one.
    markup = False
    if group == 3 and angles[0]:
        tag = _last_before(angles[0], m.start())
        markup = tag > _last_before(angles[1], m.start()) and line[tag + 1 : tag + 2].isalpha()
    return Cell(base + m.start(group), base + (m.end(group) if group == 3 else end), m.group(1), markup)


def cells(text: str, regions: Sequence[str] = DEFAULT_PHONE_REGIONS) -> list[Cell]:
    """Every value of a structure found in ``text``, with the name of its field. ``regions``: those of the phone numbers
    a ``key=value`` value takes in whole (``phone_regions``)."""
    if not text or len(text) > _MAX_TEXT:
        return []
    lines = _lines(text)
    out: list[Cell] = []
    used: set[int] = set()
    if "-+-" in text:
        found, rows = _pipe_tables(lines, _PSQL_RULE, outer=False)
        out += found
        used |= rows
    if "|" in text and "---" in text:
        found, rows = _pipe_tables(
            [(b, line) if i not in used else (b, "") for i, (b, line) in enumerate(lines)], _MD_RULE, outer=True
        )
        out += found
        used |= rows
    if "│" in text or "┃" in text or "║" in text:
        found, rows = _box_tables(text, lines, used)
        out += found
        used |= rows
    if "," in text or "\t" in text or ";" in text:
        out += _delimited(lines, used)
    if "<t" in text.lower():
        out += _html(text)
    if 'cell "' in text:
        out += _snapshot(lines)
    # On every line, a table's rows too: a cell can hold key=value pairs or JSON (psql's jsonb, a string console.table
    # prints), whose secret names say what they hold where the cell's column says nothing (the masking merges both).
    out += _key_values(lines, regions)
    return out


# --- the detector --------------------------------------------------------------------------------


def _utf8(text: str) -> bytes:
    """``text`` as RE2 reads it here: in UTF-8, a half of a character (a lone surrogate) passed through as it is,
    where a strict encoding would fail on it (Python's re read such a text)."""
    return text.encode("utf-8", "surrogatepass")


def _secretish(text: str) -> bool:
    """Whether ``text`` holds a word of a name that says secret (``_SECRETISH``), none of which holds a half of a
    character."""
    return _SECRETISH.search(_utf8(text)) is not None


def _last(field: str) -> str:
    return field.rsplit(".", 1)[-1].replace("[]", "")


def shown_rule(rule: str, field: str, shown: str) -> str:
    """The rule a value of ``field`` was found by, naming the field as it is sent (``shown``: its keys masked where they
    are values) where it names it, as ``FieldDetector`` names it: what an exchange shows and keeps of a value left
    holds no key in clear that left masked. The field is found by its own name all the same."""
    if shown == field:
        return rule
    if rule == f"strict:{field}":
        return f"strict:{shown}" if len(shown) <= _NAMED else "strict"
    last, sent = _last(field), _last(shown)
    if last != sent and rule == f"field-secret:{last}":
        return f"field-secret:{sent}" if len(sent) <= _NAMED else f"field-secret:{_key_kind(last) or 'secret'}"
    return rule


def _literal_secret(value: str, kind: str, status: bool = False) -> bool:
    """Whether ``value``, under a field whose name says it holds a secret of ``kind``, is one. Not a placeholder, a
    status word, nor, under a name that says what a check found (``status``: ``auth_status``, ``token_error``), a
    value of the shape of a status."""
    value = value.strip()
    if _NOTHING.match(value) or _PLACEHOLDER.match(value) or _STATUS.match(value):
        return False
    if status and _status_shaped(value):
        return False
    return len(value) >= (3 if kind == "password" else 6)


def _pattern(name: str) -> Any:
    """A field rule's name as a pattern: `*` matches any characters, everything else is itself (a JSON
    path's `[]` too, which fnmatch read as a set of characters), and case does not matter.

    RE2 reads it, in time in proportion to the field however many stars it has: Python's re tried each way of sharing
    the field out among them first, and a rule of a dozen stars (a repository's latet.yaml may set one) held the
    interpreter for good on a long field. It reads the UTF-8 of the name and of a field (``_utf8``), each byte as itself
    (Latin-1): a character's bytes begin where no other character's are inside, so the stars between the name's
    characters take whole characters of the field, a half of one (a lone surrogate) too, as Python's re read them."""
    pattern = b"".join(b".*" if c == "*" else re2.escape(_utf8(c)) for c in name.lower())
    return re2.compile(pattern, quiet_options(encoding=re2.Options.Encoding.LATIN1, dot_nl=True))


def strict_covers(
    tool: str | None,
    tools: Sequence[str] = (),
    path: str | None = None,
    files: Sequence[str] = (),
    hosts: Sequence[str] = (),
) -> bool:
    """Strict data mode covers the output of ``tool``, which came from ``path`` (the file a read tool read, the address
    a web tool fetched) when known: a file a read tool returns when a rule of ``files`` (`data_files`) names it, and a
    tool's search or listing never; a web tool's result when its address's host matches one of ``hosts``
    (`data_hosts`); else, with ``tools`` (`data_tools`, ``*`` for any characters), only the tools it names, and without
    it any but the web tools. An edit of a file a rule of ``files`` names is covered as a read of it is, whatever
    ``tools`` names. The dashboard asks it of a recorded output too, so that what it says strict data mode would mask
    is what it masks."""
    return _covered(tool, tools, named_source(tool, path, files, hosts))


def _covered(tool: str | None, tools: Sequence[str], named: bool) -> bool:
    """``strict_covers``, knowing whether the output of ``tool`` came from a file or an address a rule names."""
    name = (tool or "").lower()
    if name in FILE_TOOLS:
        return named
    if named:  # a web tool's page from a host a rule names, an edit of a file a rule names
        return True
    if tools:
        return bool(name) and any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in tools)
    return name not in WEB_TOOLS


def named_source(tool: str | None, source: str | None, files: Sequence[str], hosts: Sequence[str]) -> bool:
    """The output of ``tool`` came from a file or a web address a rule names: a file a read tool returned, or an edit
    tool changed, whose path a rule of ``files`` (`data_files`) names, or a web tool's result from a host one of
    ``hosts`` (`data_hosts`) names. Such an output is data with strict data mode on or off: naming it is asking for
    it."""
    name = (tool or "").lower()
    if not source:
        return False
    if name in READ_TOOLS or name in EDIT_TOOLS:
        return file_named(source, files)
    return name in WEB_TOOLS and bool(hosts) and host_named(source, hosts)


def file_named(path: str, patterns: Sequence[str]) -> bool:
    """A rule of ``patterns`` (`data_files`) names the file at ``path``: a rule without a slash names a file by its name
    (``*.json``, ``.env*``), one with a slash by the end of its path (``exports/**``, ``**/*.csv``), ``*`` for any
    characters, in any case. A markdown file only by a rule that names markdown (``*.md``): an agent's instructions
    and documents are markdown, and a rule for every file (``*``) does not make them data.

    A rule with a slash names the path when it names the path whole, or what follows one of its slashes: what ``*/``
    and the rule name, ``*`` taking whatever comes before that slash. Two matches for each rule, each in time in
    proportion to the path (fnmatch's patterns take each run between two stars once): the end of the path after each
    of its slashes, joined again for each rule, cost the square of a path of thousands of folders."""
    parts = [part for part in path.replace("\\", "/").lower().split("/") if part]
    if not parts:
        return False
    whole = "/".join(parts)
    markdown = parts[-1].endswith(_MARKDOWN)
    for pattern in patterns:
        rule = pattern.replace("\\", "/").lower().strip().strip("/")
        if not rule or (markdown and not rule.endswith(_MARKDOWN)):
            continue
        if "/" not in rule:
            if fnmatch.fnmatchcase(parts[-1], rule):
                return True
        elif fnmatch.fnmatchcase(whole, rule) or fnmatch.fnmatchcase(whole, f"*/{rule}"):
            return True
    return False


def host_named(address: str, patterns: Sequence[str]) -> bool:
    """A rule of ``patterns`` (`data_hosts`, ``*`` for any characters, in any case) names the host of the web address
    ``address`` (its port aside). A host with a final dot (``example.com.``, written from the root of the names) is the
    same host without it, in an address as in a rule; and a rule for every subdomain (``*.salesforce.com``) names the
    domain itself too (``salesforce.com``): protection first."""
    try:
        host = urllib.parse.urlsplit(address).hostname
    except ValueError:
        return False
    host = (host or "").rstrip(".")
    if not host:
        return False
    for pattern in patterns:
        rule = pattern.strip().lower().rstrip(".")
        below = rule.lstrip("*")  # ``.salesforce.com`` for ``*.salesforce.com``
        if fnmatch.fnmatchcase(host, rule) or (below.startswith(".") and fnmatch.fnmatchcase(host, below[1:])):
            return True
    return False


class FieldDetector:
    """Values by the field they sit in: secret names, your field rules and strict data mode."""

    id = "fields"
    mandatory = True

    def __init__(
        self,
        rules: Sequence[FieldRule] = (),
        strict: bool = False,
        tools: Sequence[str] = (),
        files: Sequence[str] = (),
        hosts: Sequence[str] = (),
        policies: Mapping[str, Policy] | None = None,
        phone_regions: Sequence[str] = DEFAULT_PHONE_REGIONS,
    ) -> None:
        """``policies``: each type's policy where the rules apply, which says how strict a rule is (``_rank``).
        ``phone_regions``: those of the phone numbers an unquoted ``key=value`` value takes in whole."""
        self.phone_regions = tuple(phone_regions)
        self.rules = [(_pattern(r.name), r) for r in rules]
        self.layered = any(r._layer for r in rules)
        self.policies = dict(policies or {})
        self.strict = strict
        self.tools = [t.lower() for t in tools]
        self.files = list(files)
        self.hosts = list(hosts)

    def _rule(self, field: str) -> FieldRule | None:
        """The rule a field's values follow: the first that matches it, of yours; and in a project whose rules come from
        layers that may only add protection (a repository file: ``FieldRule._layer``), the stricter of the first that
        matches in each layer, yours when as strict (Ruling 098-R2), so that no layer loosens another's rule for the
        field, whatever their order."""
        if not self.rules:
            return None
        name, last = _utf8(field.lower()), _utf8(_last(field).lower())
        if not self.layered:
            for regex, rule in self.rules:
                if regex.fullmatch(name) is not None or regex.fullmatch(last) is not None:
                    return rule
            return None
        firsts: dict[int, FieldRule] = {}
        for regex, rule in self.rules:
            if rule._layer not in firsts and (regex.fullmatch(name) is not None or regex.fullmatch(last) is not None):
                firsts[rule._layer] = rule
        # By layer, yours first: max keeps the first of the strictest.
        return max((firsts[layer] for layer in sorted(firsts)), key=self._rank, default=None)

    def _rank(self, rule: FieldRule) -> tuple[int, bool]:
        """How strict a rule is: its values in clear (`mask: false`, or of a type let through), masked, or redacted,
        as its type's policy says; then a secret's (one in clear stops the request) over another's."""
        if not rule.mask:
            return 0, rule.secret
        policy = self.policies.get((rule.type or "FIELD").upper(), Policy.MASK)
        return {Policy.ALLOW: 0, Policy.MASK: 1, Policy.REDACT: 2}[Policy(policy)], rule.secret

    def strict_for(self, ctx: DetectContext) -> bool:
        """This text is masked as data: in strict data mode, a tool's output, not a file it read nor a web page unless
        a rule names them (`data_files`, `data_hosts`; a web tool also when `data_tools` names it); with the mode off,
        a file or a web page a rule names all the same."""
        return self._covers(ctx)[0]

    def _covers(self, ctx: DetectContext) -> tuple[bool, bool]:
        """Whether this text is masked as data (``strict_for``), and whether as the file a rule of `data_files` names
        (a read of it, an edit's lines of it). Asked of its tool and its source once in a document whose values are
        masked one by one (``document.once``): each value asked it again, and a rule of up to two hundred, from a
        repository's file, was read again for each."""
        if ctx.kind != SlotKind.TOOL_RESULT:
            return False, False
        return document.once((self, "covers", ctx.tool, ctx.source), lambda: self._read_covers(ctx.tool, ctx.source))

    def _read_covers(self, tool: str | None, source: str | None) -> tuple[bool, bool]:
        named = named_source(tool, source, self.files, self.hosts)
        strict = _covered(tool, self.tools, named) if self.strict else named
        name = (tool or "").lower()
        return strict, named and (name in READ_TOOLS or name in EDIT_TOOLS)

    def numbers_for(self, ctx: DetectContext) -> bool:
        """The numbers of a JSON document can be masked here: strict data mode, or field rules."""
        return bool(self.rules) or self.strict_for(ctx)

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        strict, as_file = self._covers(ctx)
        known: dict[str, _Field] = {}  # what each field's name says, read once for every value of the field here
        if ctx.field is not None:
            yield from self._leaf(text, ctx.field, strict, known)
            return
        if as_file and ctx.source:
            yield from self._file(text, ctx.source, known, (ctx.tool or "").lower() in EDIT_TOOLS)
            return
        if not strict and not self.rules and not _secretish(text):
            return  # no structure here can hold a secret name: nothing to look for
        for cell in cells(text, self.phone_regions):
            yield from self._value(text, cell.start, cell.end, cell.field, strict, known, cell.markup)

    def _file(self, text: str, path: str, known: dict[str, _Field], edited: bool = False) -> Iterator[Span]:
        """A file a rule of `data_files` names, as a read tool returned it, or an edit tool its lines (``edited``, after
        the line that says which file it changed): its values found by its format, without the numbers of its lines,
        each where it is in ``text``."""
        from latet.engine.detectors.files import file_cells, numbered_view  # it reads tables with this module's own

        view = numbered_view(text, header=edited)
        found = [
            span
            for cell in file_cells(view.text, path, view.first)
            for span in self._value(view.text, cell.start, cell.end, cell.field, True, known)
        ]
        # Secret names and your field rules, as in a text no rule names: a markdown file's prose, a form a reader passes
        # over (the masking merges what both find).
        if self.rules or _secretish(view.text):
            found += [
                span
                for cell in cells(view.text, self.phone_regions)
                for span in self._value(view.text, cell.start, cell.end, cell.field, False, known, cell.markup)
            ]
        for span in found:
            # Each line of a value apart: a cell across lines must not take the line numbers between them.
            start = span.start
            while start < span.end:
                stop = view.text.find("\n", start, span.end)
                stop = span.end if stop < 0 else stop
                if stop > start:
                    yield replace(span, start=view.original(start), end=view.original(stop - 1) + 1)
                start = stop + 1

    def _about(self, field: str, known: dict[str, _Field]) -> _Field:
        """What ``field``'s name says, read once in a text: a table's thousand rows carried a long column name each,
        and reading it again for each held the detector for seconds (for a day at its two million characters). And
        once in a document whose values are masked one by one with their paths (``document.once``)."""
        about = known.get(field)
        if about is None:
            about = known[field] = document.once((self, field), lambda: self._read(field))
        return about

    def _read(self, field: str) -> _Field:
        last = _last(field)
        kind = _key_kind(last)
        # A value's rule names its field, as long as a rule could name it (the dashboard proposes a column by it): a
        # longer one, which no rule can name, is named by its kind, and costs each value no more than a field's name.
        secret = f"field-secret:{last}" if len(last) <= _NAMED else f"field-secret:{kind or 'secret'}"
        strict = f"strict:{field}" if len(field) <= _NAMED else "strict"
        return _Field(self._rule(field), kind, secret, strict, _status_name(last))

    def _leaf(self, text: str, field: str, strict: bool, known: dict[str, _Field]) -> Iterator[Span]:
        """A decoded JSON value and the path of its key.

        The value is masked whole when a field rule or a secret name says so, or in strict mode when
        it is one line. A longer text (a command's output, a note) is read for structures instead.
        """
        about = self._about(field, known)
        if about.rule is not None or about.kind is not None:
            yield from self._value(text, 0, len(text), field, strict, known)
            return
        found = cells(text, self.phone_regions) if strict or self.rules or _secretish(text) else []
        if found:
            for cell in found:
                yield from self._value(text, cell.start, cell.end, cell.field, strict, known, cell.markup)
        elif strict and "\n" not in text.strip():
            yield from self._value(text, 0, len(text), field, strict, known)

    def _value(
        self, text: str, start: int, end: int, field: str, strict: bool, known: dict[str, _Field], markup: bool = False
    ) -> Iterator[Span]:
        start, end = _strip_space(text, start, end)
        if end <= start:
            return
        value = text[start:end]
        about = self._about(field, known)
        rule = about.rule
        if rule is not None and rule.mask:
            if rule.secret and _REDACTED.match(value):
                # Latet's own marker, in place of a value too long for a vault to keep (``[REDACTED:FIELD]``): taken for
                # a secret of yours left in clear, it stopped the request that carries it and every later one of its
                # conversation. Nothing else is skipped: whatever else a value of yours looks like, it is yours.
                return
            if (rule.secret or len(value) >= _SHORTEST) and not _NOTHING.match(value):
                eclass = EntityClass.SECRET if rule.secret else EntityClass.STRUCT
                yield Span(start, end, (rule.type or "FIELD").upper(), eclass, f"field:{rule.name}")
            return
        kind = about.kind
        if kind is not None and _literal_secret(value, kind, about.status):  # a field in clear still keeps its secrets
            parts = connection_secrets(value, start, about.secret) if kind == "connection" else None
            if parts is None:
                etype = "PASSWORD" if kind == "password" else "ASSIGNED_SECRET"
                yield Span(start, end, etype, EntityClass.SECRET, about.secret)
                return
            yield from parts  # a connection string: its credentials, and strict mode masks the rest below
        if (
            strict
            and rule is None
            and len(value) >= _SHORTEST
            and not _NOTHING.match(value)
            and not _INTERPOLATION.match(value)
            and not (markup and field.lower() in _MARKUP_FIELDS and "url(" not in value.lower())
        ):
            yield Span(start, end, "DATA", EntityClass.STRUCT, about.strict)


@dataclass(frozen=True, slots=True)
class _Field:
    """What a field's name says: the rule of yours it falls under, the kind of secret its name says it holds, and the
    names of the rules that find its values."""

    rule: FieldRule | None
    kind: str | None
    secret: str  # a secret found by the field's name
    strict: str  # a value strict data mode masks
    status: bool = (
        False  # its last word says what a check found about a secret (`auth_status`): see ``_literal_secret``
    )


def _strip_space(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
