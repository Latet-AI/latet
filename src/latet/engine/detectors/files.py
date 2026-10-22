"""A file as strict data mode reads it, when a rule of `data_files` names it: without the line numbers a read tool puts
before each of its lines, and its values found by the file's format.

- JSON (``.json``, ``.jsonl``, a file of another kind that is JSON): every string and number, with the path of its key
  (``orders[].sku``), never a key. A document read in part (a read tool's first two thousand lines, a long line cut) is
  read token by token instead: every string that is not a key, every number.
- Tables (``.csv``, ``.tsv``): each cell of each line, by its column; the first line is the header when the file was
  read from its first line, and a column with no name there is named by its place.
- ``.env``, properties and ini files: the value of each ``KEY=value`` (or ``key: value``) line.
- YAML: each scalar, with its path (``servers[].host``), never a key nor the structure.
- Markdown, which a rule names by its extension alone: its tables, never its prose (an agent's instructions and
  documents are written in markdown).
- A file of any other kind: the structures any tool's output is read for (``cells``).
- A file longer than the detector reads for structures: each of its lines, whole.

Each value is found where it is in the file's text, so that it is replaced there and the file keeps its lines, its
numbers and its keys: an agent edits the file it read by the text it read. Every reader takes time and memory in
proportion to the file: the file's content is the agent's, which a repository can write.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from latet.engine.detectors.fields import _MAX_TEXT, _MD_RULE, _NAMED, Cell, _lines, _pipe_tables, _split, cells

# The line-number prefix a read tool puts in front of every line it returns: Claude Code's "     12\t" or "12→",
# OpenCode's "00012| ".
_NUMBERED = re.compile(r"[ \t]*(\d+)(?:\t|→|\| )")
_MARKDOWN = frozenset({"md", "markdown", "mdx"})
_TABLES = {"csv": ",", "tsv": "\t", "psv": "|"}
_ASSIGNMENTS = frozenset({"env", "properties", "ini", "cfg"})
_YAML = frozenset({"yaml", "yml"})
_JSON = frozenset({"json", "jsonl", "ndjson", "geojson"})


@dataclass(frozen=True, slots=True)
class FileView:
    """A file's text as a read tool returned it, without its line numbers: ``original`` gives where a character of
    ``text`` is in what the tool returned; ``first``, the number of its first line (1 when it was read from the top)."""

    text: str
    starts: tuple[int, ...]  # where each line of ``text`` starts
    origins: tuple[int, ...]  # where that line's first character is in what the tool returned
    first: int = 1

    def original(self, offset: int) -> int:
        line = bisect.bisect_right(self.starts, offset) - 1
        return self.origins[line] + offset - self.starts[line]


def numbered_view(text: str, header: bool = False) -> FileView:
    """``text`` without the numbers of its lines, when its lines are numbered one after another (after OpenCode's
    ``<file>`` line) up to its end, a blank line or a line of ``<`` (the note an agent adds after the file, which is
    not the file). A text of other lines, a lone numbered first line among them, is seen as it is. With ``header``, the
    numbered lines may follow a first line that says what they are (Claude Code's Edit: ``The file ... has been
    updated. Here's the result of running `cat -n` on a snippet of the edited file:``), which is not the file."""
    whole = FileView(text, (0,), (0,))
    lines: list[str] = []
    starts: list[int] = []
    origins: list[int] = []
    here = len("<file>\n") if text.startswith("<file>\n") else 0
    if header and _NUMBERED.match(text, here) is None:
        after = text.find("\n", here) + 1
        if after and _NUMBERED.match(text, after) is not None:
            here = after
    at = 0
    first = number = None
    rest: str | None = None
    for line in text[here:].split("\n"):
        found = _NUMBERED.match(line)
        if found is None or (number is not None and int(found.group(1)) != number + 1):
            rest = line
            break
        number = int(found.group(1))
        first = number if first is None else first
        lines.append(line[found.end() :])
        starts.append(at)
        origins.append(here + found.end())
        at += len(line) - found.end() + 1
        here += len(line) + 1
    if not lines or (rest is not None and rest.strip() and not rest.lstrip().startswith("<")):
        return whole
    return FileView("\n".join(lines), tuple(starts), tuple(origins), first or 1)


def file_cells(text: str, path: str, first: int = 1) -> list[Cell]:
    """Every value of the file at ``path`` whose text is ``text`` (from its line ``first``), found by its format, with
    the name of its field."""
    if len(text) > _MAX_TEXT:
        return _whole_lines(text)
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    extension = name.rsplit(".", 1)[-1] if "." in name.lstrip(".") else ""
    if name.startswith(".env") or extension in _ASSIGNMENTS:
        return _assignments(text)
    if extension in _YAML:
        return _yaml(text)
    if extension in _MARKDOWN:
        return _pipe_tables(_lines(text), _MD_RULE, outer=True)[0]
    if extension in _TABLES:
        return _table(text, _TABLES[extension], header=first == 1)
    found = _json(text)
    if found is not None:
        return found
    if extension in _JSON:
        return _tokens(text) or cells(text)
    return cells(text)


def _whole_lines(text: str) -> list[Cell]:
    """Each line of ``text`` with something on it, whole, without the blanks around it."""
    found: list[Cell] = []
    for base, line in _lines(text):
        start, end = len(line) - len(line.lstrip()), len(line.rstrip())
        if end > start:
            found.append(Cell(base + start, base + end, "line"))
    return found


# --- tables --------------------------------------------------------------------------------------------------


def _table(text: str, sep: str, header: bool) -> list[Cell]:
    """Each cell of each line of a table file, outside double quotes, by its column: the first line names the columns
    when ``header`` (the file was read from its top), and a column it does not name, or every column of a file read
    from a later line, is named by its place (``column 3``). A line of another number of cells is read all the same."""
    lines = _lines(text)
    names: list[str] = []
    if header and lines:
        names = [lines[0][1][a:b] for a, b in _split(lines[0][1], sep)]
        lines = lines[1:]
    found: list[Cell] = []
    for base, line in lines:
        if not line.strip():
            continue
        for place, (a, b) in enumerate(_split(line, sep)):
            if b > a:
                named = names[place] if place < len(names) and names[place] else f"column {place + 1}"
                found.append(Cell(base + a, base + b, named))
    return found


# --- JSON ----------------------------------------------------------------------------------------------------

_SPACE = re.compile(r"[ \t\r\n]*")
_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\.)*"')
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_LITERAL = re.compile(r"true|false|null")


def _skip(text: str, at: int) -> int:
    """Where the blanks from ``at`` end."""
    blanks = _SPACE.match(text, at)
    return blanks.end() if blanks is not None else at


@dataclass(slots=True)
class _Open:
    """An object or an array the reading is inside: its path, what comes next in it, and the path of its key read
    last (an object's)."""

    array: bool
    path: str
    next: str  # "first" (a key, an item or the end), "key", ":", "value", "," (a comma or the end)
    key: str = ""


def _under(path: str, key: str | None) -> str:
    """The path of a key (or, None, an array's items) under ``path``. A path past the length a rule can name stays as
    it is: a deeper field is named by its kind anyway, and a document nested a hundred thousand deep holds one path
    for its depths, never a longer one at each level."""
    if len(path) >= _NAMED:
        return path
    if key is None:
        return f"{path}[]"
    return f"{path}.{key}" if path else key


def _json(text: str) -> list[Cell] | None:
    """The strings and numbers of a JSON document, or of JSON lines (one document after another), each where it is in
    ``text`` (a string without its quotes, its escapes as written), with the path of its key; None when ``text`` is not
    whole JSON whose documents are objects or arrays. Read in one pass, never by recursion."""
    found: list[Cell] = []
    stack: list[_Open] = []
    i, n, documents = 0, len(text), 0

    def done() -> None:  # a value ended: its container waits for a comma or its end
        if stack:
            stack[-1].next = ","

    while True:
        i = _skip(text, i)
        if i >= n:
            break
        c = text[i]
        top = stack[-1] if stack else None
        if top is None and c not in "{[":
            return None  # a document is an object or an array
        if top is not None and not top.array and top.next in ("first", "key"):
            if c == "}" and top.next == "first":
                stack.pop()
                done()
                i += 1
                continue
            key = _STRING.match(text, i)
            if key is None:
                return None
            top.key = _under(top.path, text[i + 1 : key.end() - 1])
            top.next = ":"
            i = key.end()
            continue
        if top is not None and top.next == ":":
            if c != ":":
                return None
            top.next = "value"
            i += 1
            continue
        if top is not None and top.next == ",":
            if c == ",":
                top.next = "value" if top.array else "key"
                i += 1
                continue
            if c != ("]" if top.array else "}"):
                return None
            stack.pop()
            done()
            i += 1
            continue
        if top is not None and top.array and top.next == "first" and c == "]":
            stack.pop()
            done()
            i += 1
            continue
        where = "" if top is None else _under(top.path, None) if top.array else top.key
        if c in "{[":
            if top is None:
                documents += 1
            stack.append(_Open(c == "[", where, "first"))
            i += 1
            continue
        if c == '"':
            string = _STRING.match(text, i)
            if string is None:
                return None
            if where and string.end() - 1 > i + 1:
                found.append(Cell(i + 1, string.end() - 1, where))
            i = string.end()
        elif (number := _NUMBER.match(text, i)) is not None:
            if where:
                found.append(Cell(i, number.end(), where))
            i = number.end()
        elif (literal := _LITERAL.match(text, i)) is not None:
            i = literal.end()
        else:
            return None
        done()
    return found if documents and not stack else None


# A string to its closing quote or, cut, to the end of its line; a number.
_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*+"?|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')


def _tokens(text: str) -> list[Cell]:
    """The values of JSON read in part, token by token: every string not followed by ``:`` (a key), to its end or, cut,
    to the end of its line, and every number after ``:``, ``[`` or ``,``, each named by the key read last."""
    found: list[Cell] = []
    key = "value"
    for base, line in _lines(text):
        for token in _TOKEN.finditer(line):
            start, end = token.span()
            if line[start] == '"':
                closed = end - start >= 2 and line[end - 1] == '"'
                if closed and line[end:].lstrip().startswith(":"):
                    key = line[start + 1 : end - 1] or key
                    continue
                start, end = start + 1, end - 1 if closed else end
            elif line[:start].rstrip()[-1:] not in ("", ":", "[", ","):
                continue  # digits in a word, not a number
            if end > start:
                found.append(Cell(base + start, base + end, key))
    return found


# --- .env, properties, ini -----------------------------------------------------------------------------------

_ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][\w.\-]*)[ \t]*[=:][ \t]*")


def _assignments(text: str) -> list[Cell]:
    """The value of each ``KEY=value`` or ``key: value`` line (``export`` before it too), without the quotes around
    it or a comment after it (`` #``, when it is not quoted); a comment line and an ini section's name are not."""
    found: list[Cell] = []
    for base, line in _lines(text):
        first = line.lstrip()[:1]
        if not first or first in "#;![":
            continue
        assignment = _ASSIGNMENT.match(line)
        if assignment is None:
            continue
        start, end = _unquoted(line, assignment.end(), len(line))
        if end > start:
            found.append(Cell(base + start, base + end, assignment.group(1)))
    return found


def _closing(line: str, start: int, end: int) -> int:
    """Where the quote opened at ``line[start]`` closes before ``end``: after ``\\`` escapes in double quotes, after a
    doubled ``''`` in single quotes; -1 when it does not."""
    quote, i = line[start], start + 1
    while i < end:
        if quote == '"' and line[i] == "\\":
            i += 2
            continue
        if line[i] == quote:
            if quote == "'" and i + 1 < end and line[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def _unquoted(line: str, start: int, end: int) -> tuple[int, int]:
    """``line[start:end]`` as a scalar value: without the blanks around it, the quotes around it (its escapes as
    written), or a comment after it when it is not quoted. A quote that does not close takes the rest."""
    while start < end and line[start] in " \t":
        start += 1
    while end > start and line[end - 1] in " \t\r":
        end -= 1
    if start < end and line[start] in "\"'":
        close = _closing(line, start, end)
        return (start + 1, close) if close > start else (start + 1, end)
    comment = line.find(" #", start, end)
    if comment >= 0:
        end = comment
    elif line.startswith("#", start):
        return start, start
    while end > start and line[end - 1] in " \t":
        end -= 1
    return start, end


def _parts(line: str, start: int, end: int) -> list[tuple[int, int]]:
    """The items of a flow collection written on one line (``line[start:end]``, inside its brackets), split at the
    commas outside quotes."""
    parts, at, i = [], start, start
    while i < end:
        if line[i] in "\"'":
            close = _closing(line, i, end)
            i = close + 1 if close > i else end
            continue
        if line[i] == ",":
            parts.append((at, i))
            at = i + 1
        i += 1
    parts.append((at, end))
    return parts


# --- YAML ----------------------------------------------------------------------------------------------------

_COLON = re.compile(r":(?=[ \t]|$)")  # a key's colon: one followed by a blank or the end of the line
_BLOCK = re.compile(r"[|>][0-9+\-]*$")
_INDICATORS = "-?:,[]{}#&*!|>%@`"


def _key(line: str, at: int) -> tuple[str, int] | None:
    """The key a YAML line holds from ``at`` and where its colon ends, or None: a quoted key, or the text before the
    first colon followed by a blank, which no comment comes before. Found by looking, in time in proportion to the
    line: a pattern that shared the blanks between the key and its colon out in every way took seconds on a long line
    of blanks."""
    if at >= len(line):
        return None
    if line[at] in "\"'":
        close = _closing(line, at, len(line))
        if close < 0:
            return None
        colon = _skip(line, close + 1)
        if line[colon : colon + 1] == ":" and _COLON.match(line, colon):
            return line[at + 1 : close], colon + 1
        return None
    if line[at] in _INDICATORS:
        return None
    colon = _COLON.search(line, at)
    if colon is None or line.find(" #", at, colon.start()) >= 0:
        return None
    name = line[at : colon.start()].rstrip()
    return (name, colon.end()) if name else None


def _yaml(text: str) -> list[Cell]:
    """Each scalar of a YAML file, with the path of its key (``servers[].host``, an item of a list ``servers[]``):
    after an anchor or a tag, in a flow collection written on one line, each line of a block scalar (``notes: |``) and
    of a plain value continued on the lines under it; never a key, an anchor, an alias or a tag."""
    found: list[Cell] = []
    stack: list[tuple[int, str]] = []  # (the indentation it is at, the path of what is under it)
    block: tuple[int, str] | None = None  # a block scalar: the indentation of its key, its path
    last: tuple[int, str] | None = None  # the plain value read last: the indentation of its line, its path

    def value(base: int, line: str, start: int, path: str) -> bool:
        """The value from ``start``; False when there is none (an anchor or a tag alone: what is under it is)."""
        start, end = _unquoted(line, start, len(line))
        if end > start and line[start] in "&!" and line[start - 1 : start] not in ("'", '"'):
            space = line.find(" ", start, end)
            if space < 0:
                return False
            start, end = _unquoted(line, space, end)
        if end <= start or line[start] == "*":
            return True  # nothing, or an alias
        if line[start] == "{" and line[end - 1] == "}":
            for a, b in _parts(line, start + 1, end - 1):
                named = _key(line, _skip(line, a))
                if named is not None and named[1] <= b:
                    first, after = _unquoted(line, named[1], b)
                    if after > first:
                        found.append(Cell(base + first, base + after, f"{path}.{named[0]}" if path else named[0]))
        elif line[start] == "[" and line[end - 1] == "]":
            for a, b in _parts(line, start + 1, end - 1):
                first, after = _unquoted(line, a, b)
                if after > first and line[first] not in "[{":
                    found.append(Cell(base + first, base + after, f"{path}[]"))
        else:
            found.append(Cell(base + start, base + end, path))
        return True

    def key(base: int, line: str, at: int, indent: int, parent: str) -> bool:
        nonlocal block, last
        named = _key(line, at)
        if named is None:
            return False
        path = f"{parent}.{named[0]}" if parent else named[0]
        rest = _skip(line, named[1])
        given = line[rest:].rstrip()
        if _BLOCK.match(given):
            block = (indent, path)
        elif not given or given.startswith("#") or not value(base, line, rest, path):
            stack.append((indent, path))  # what is under it is its value
        else:
            last = (indent, path)
        return True

    for base, line in _lines(text):
        content = line.lstrip(" ")
        indent = len(line) - len(content)
        if block is not None:
            if not content.strip():
                continue
            if indent > block[0]:
                found.append(Cell(base + indent, base + len(line.rstrip()), block[1]))
                continue
            block = None
        if not content.strip() or content.startswith("#"):
            continue
        if content.rstrip() in ("---", "...") or content.startswith(("--- ", "%")):
            stack.clear()
            last = None
            continue
        if last is not None and indent > last[0] and _key(line, indent) is None and not content.startswith("- "):
            found.append(Cell(base + indent, base + len(line.rstrip()), last[1]))  # a plain value continued
            continue
        last = None
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        if content == "-" or content.startswith("- "):
            item = f"{parent}[]"
            stack.append((indent, item))
            rest = _skip(line, indent + 1)
            if rest < len(line) and not key(base, line, rest, rest, item) and value(base, line, rest, item):
                last = (indent, item)
            continue
        if content.startswith(("? ", ": ")):
            if content.startswith(": "):  # the value of a complex key
                value(base, line, indent + 2, parent)
            continue
        if not key(base, line, indent, indent, parent):
            start, end = indent, len(line.rstrip())  # a line no form explains: whole, as a value
            found.append(Cell(base + start, base + end, parent or "value"))
    return found
