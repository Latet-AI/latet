"""What a source gives: its records (the rows of a CSV file, the objects of a JSON or YAML document, the people of a
directory), and the terms its mapping makes of them."""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import json
import logging
import math
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from latet.config import load_yaml
from latet.ee.settings import ENCODING_NAME, TEMPLATE, BaseSource
from latet.engine.detectors.terms import long_enough
from latet.fileio import read_regular_bytes

log = logging.getLogger(__name__)

MAX_BYTES: Final = 50 * 1024 * 1024  # a larger document is not read
# A YAML document's own bound (the final security review's L1): PyYAML's parser holds about 190 times a document of
# small nodes at its peak in block style and about 310 times in flow style, so about 1 to 1.5 GB at 5 MB, where JSON
# holds about 25 times its size and CSV, read a row at a time, about 5 times and the fields of its longest row.
MAX_YAML_BYTES: Final = 5 * 1024 * 1024
MAX_COLUMNS: Final = 1000  # a CSV header of more is not read (the final security review's M1)
# Terms from one source: more is refused (a filter, or fewer fields); the store's own bound too.
MOST_TERMS: Final = 50_000
# Records one document may list (M1): four times the terms one source may give, room for records that give none (the
# same customer on several rows, values left out); a document of many more is an export of something else.
MOST_RECORDS: Final = 4 * MOST_TERMS
MAX_LENGTH: Final = 200  # a longer value is a description, not a term
# Why a run failed, for the events, the metrics and the page: never a value of the source.
CODES: Final = (
    "unreachable", "refused", "unreadable", "too-large", "too-many", "empty", "no-secret", "not-installed",
    "sealed-elsewhere", "timeout", "error",
)  # fmt: skip

# The source's own secret, opened for where it goes now; None when it is not kept, or was kept for another place.
SecretOf = Callable[[], str | None]


def size_words(size: int) -> str:
    """A bound in bytes as people read it: `50 MB`, or `16 bytes`."""
    mib = 1024 * 1024
    return f"{size // mib} MB" if size >= mib and size % mib == 0 else f"{size:,} bytes"


def bound_words(fmt: str, size: int) -> str:
    """A document's bound ``size`` as a reason says it: `50 MB`, or YAML's `5 MB, the most Latet reads as YAML`."""
    return f"{size_words(size)}, the most Latet reads as YAML" if fmt == "yaml" else size_words(size)


def ago_words(seconds: float) -> str:
    """How long ago, as people read it: `12 minutes ago`, `3 hours ago`, `just now` (a run's, as `latet server status`
    and `latet server terms status` say it alike)."""
    whole = max(0, int(seconds))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if whole >= size:
            count = whole // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def read_inside(folder: Path, name: str, *, limit: int, what: str) -> bytes:
    """The bytes of the file ``name`` directly inside ``folder`` (the server's term-sync folder: a file source's, or a
    directory's certificate authority's), read as it stands now (the design security review's B-M8): resolved, and
    refused when it leads out of the folder (a link out of it), then opened without following a link put in its place
    since, a regular file alone, ``limit`` bytes at most. Refused `unreachable` in fixed words that name ``what`` and no
    path (B-M4); what the read raises (latet.fileio's ``FileReadError``, an ``OSError``) is the caller's to say."""
    root = folder.resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise SourceError("unreachable", f"the source's {what} is not inside the server's term-sync folder")
    return read_regular_bytes(target, limit=limit, follow=False)


class SocketCut:
    """Ends a read's connections from another thread (``cut_at_deadline``): a duplicate of each connection's socket is
    kept as it connects (``hold``), and ``cut`` shuts the duplicates down, which ends a read blocked on the connection,
    its TLS handshake's too (the duplicate outlives the socket TLS takes over), where a close alone does not on Linux.
    Once cut, a duplicate held after is shut down at once. ``release`` closes the duplicates. An address's reader
    (``sources._Sockets``) and a directory's (``ldap._Cut``) cut alike."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # a shutdown never meets a duplicate being closed
        self._kept: list[socket.socket] = []
        self._cut = False

    def hold(self, kept: socket.socket) -> bool:
        """Keeps ``kept``, a duplicate of a socket just connected, nothing read on it yet; whether the read was cut
        already (``kept`` is then shut down at once)."""
        with self._lock:
            self._kept.append(kept)
            if self._cut:
                shut(kept)
            return self._cut

    def cut(self) -> None:
        with self._lock:
            self._cut = True
            for kept in self._kept:
                shut(kept)

    def release(self) -> None:
        with self._lock:
            kept, self._kept = self._kept, []
            for sock in kept:
                sock.close()


def shut(sock: socket.socket) -> None:
    """``sock`` shut down both ways; one its peer closed already left as it is."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _duration(seconds: float) -> str:
    """A run's time as people read it: `15 minutes`, or `45 seconds`."""
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    whole = max(round(seconds), 1)
    return "1 second" if whole == 1 else f"{whole} seconds"


@dataclass
class Bounds:
    """What one run of a source may take (the design security review's B-I1 and B-M5): until when (its lease's turn
    less the margin to apply: ``TermSync._run``), and how many bytes in all. Every reader checks it between two reads
    (a chunk, a request, a page, an entry), so that no read outlives its turn whatever each socket's timeout. Its
    words say the time and the bytes it was given (``began``, ``budget``: when it was made, and with what)."""

    until: float = math.inf
    clock: Callable[[], float] = time.time
    left_bytes: int = field(default_factory=lambda: MAX_BYTES)
    began: float = field(init=False, default=0.0)
    budget: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.began, self.budget = self.clock(), self.left_bytes

    def left(self) -> float:
        return self.until - self.clock()

    def check(self) -> None:
        if self.left() <= 0:
            raise self.timeout()

    def timeout(self) -> SourceError:
        """The error of a read past the deadline: the time its run had, in fixed words."""
        had = _duration(self.until - self.began)
        return SourceError("timeout", f"the source took longer than the {had} its run had: narrow it")

    def take(self, size: int) -> None:
        self.left_bytes -= size
        if self.left_bytes < 0:
            raise SourceError("too-large", f"more than {size_words(self.budget)} in all: not read")


_BOUNDS: ContextVar[Bounds | None] = ContextVar("latet_term_sync_bounds", default=None)


@contextmanager
def bounded(until: float, clock: Callable[[], float]) -> Iterator[Bounds]:
    """The bounds of the run read in this thread while it is open."""
    token = _BOUNDS.set(made := Bounds(until, clock))
    try:
        yield made
    finally:
        _BOUNDS.reset(token)


def bounds() -> Bounds:
    """The bounds of the run being read here; a reader called alone (the tests) reads with no deadline and
    ``MAX_BYTES``."""
    return _BOUNDS.get() or Bounds()


@contextmanager
def cut_at_deadline(cut: Callable[[], None]) -> Iterator[None]:
    """While open, ``cut`` is called from a thread of its own when the deadline of the run read in this thread passes
    (`bounds()`, read here: the other thread does not see it), in real seconds from now; leaving cancels it. A reader
    checks the deadline between two reads, and a read whose bytes trickle in (an answer's headers, a TLS handshake, a
    directory's reply) never passes its socket's timeout: ``cut`` ends it, from outside, by shutting its connection down
    (a close alone does not end a read blocked in another thread on Linux). Once cut, the read fails `timeout` in fixed
    words, whatever it raised. Every reader that waits on a connection reads within one: an address's
    (`sources.read_url`, cut by its client's `cut`), Microsoft Graph's and a directory's alike."""
    limits = bounds()
    left = limits.left()
    if math.isinf(left):  # a reader called alone: no deadline
        yield
        return
    fired = threading.Event()

    def fire() -> None:
        fired.set()
        try:
            cut()
        except Exception as exc:  # by its type: the reader's own thread fails at its next read or check
            log.warning("term sync: a read past its deadline could not be cut (%s)", type(exc).__name__)

    timer = threading.Timer(max(left, 0.0), fire)
    timer.name, timer.daemon = "latet-term-sync-deadline", True
    timer.start()
    try:
        yield
    except Exception:
        if fired.is_set():
            raise limits.timeout() from None
        raise
    finally:
        timer.cancel()
    if fired.is_set():  # it ended as it was cut: past its deadline all the same
        raise limits.timeout()


class SourceError(Exception):
    """A source that could not be read: ``code`` (one of ``CODES``) for the events and the metrics, ``message`` for
    people; neither ever holds a value of the source."""

    def __init__(self, code: str, message: str) -> None:
        assert code in CODES, code
        super().__init__(message)
        self.code = code
        self.message = message


def no_secret(source: str) -> SourceError:
    """The error of a source whose secret is not kept, was kept for another place or another read (B-I3), or sealed
    with another master key."""
    return SourceError(
        "no-secret",
        f"its secret is not set for where it goes and what it reads now: `latet server terms secret {source}` sets it",
    )


@dataclass(frozen=True)
class Mapped:
    terms: list[tuple[str, str]]  # (value, type), each once in any case, in the order the source gave them
    skipped: dict[str, int]  # values left out, by why: excluded, unreadable, short, long


HELD: Final = "larger than the server can hold to read it: not read"  # a document past the memory left


def _too_many_records() -> SourceError:
    said = f"more than {MOST_RECORDS:,} records: narrow the source (an export of fewer records)"
    return SourceError("too-many", said)


def parse_records(
    data: bytes,
    fmt: str,
    *,
    records: str | None = None,
    delimiter: str = ",",
    encoding: str = "utf-8",
    fields: Iterable[str] | None = None,
) -> Iterable[dict[str, Any]]:
    """A CSV file's rows, by its header, each read as it is iterated and holding the ``fields`` named alone (a source's
    ``mapped_fields``; every column of the header when None); or the objects listed in a JSON or YAML document (at
    ``records``, dotted keys, when the list is inside it). The final security review's M1 and L1: a YAML document is
    read up to ``MAX_YAML_BYTES``, the others up to ``MAX_BYTES``; a CSV header of more than ``MAX_COLUMNS`` columns
    is refused, and so is a document of more than ``MOST_RECORDS`` records, a CSV file's counted as its rows are read.
    Every failure of the document is ``unreadable`` in Latet's own words (fix round 1), a CSV row's as it is read too:
    never a parser's, which may quote it, nor an exception of another kind."""
    most = MAX_BYTES if fmt in ("csv", "json") else MAX_YAML_BYTES  # whatever is read as YAML takes YAML's bound
    if len(data) > most:  # before it is decoded or parsed
        raise SourceError("too-large", f"larger than {bound_words(fmt, most)}: not read")
    named = {"csv": "CSV", "json": "JSON"}.get(fmt, "YAML")
    try:
        return _parsed(data, fmt, records, delimiter, encoding, fields)
    except SourceError:
        raise
    except MemoryError:
        raise SourceError("too-large", HELD) from None
    except RecursionError:  # lists or objects inside one another past what the parser follows
        raise SourceError("unreadable", f"not read: nested deeper than Latet reads {named}") from None
    except Exception:  # a parser's own (an integer past Python's digits, a separator the CSV reader refuses...)
        raise SourceError("unreadable", f"not valid {named}") from None


def _parsed(
    data: bytes, fmt: str, records: str | None, delimiter: str, encoding: str, fields: Iterable[str] | None
) -> Iterable[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig" if codecs.lookup(encoding).name == "utf-8" else encoding)
    except UnicodeDecodeError as exc:
        said = f"{encoding} text" if ENCODING_NAME.fullmatch(encoding) else "text in its encoding"
        raise SourceError("unreadable", f"not {said} (byte {exc.start}): set `encoding`, such as cp1252") from None
    except Exception:  # an encoding of no text (zlib, undefined), or one this Python does not know
        raise SourceError("unreadable", "not a text encoding Latet decodes: set `encoding`, such as utf-8") from None
    if fmt == "csv":
        if len(delimiter) != 1 or delimiter in '"\r\n':
            # Refused here on every Python: 3.13's CSV reader refuses a quote or a line break as the separator, 3.12's
            # takes it and reads the rows wrong (the settings refuse it before: `ee.settings._Document._separator`).
            raise SourceError("unreadable", "not valid CSV")
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)  # one it refuses: not valid CSV
        try:
            header = next(reader, [])
        except csv.Error:
            raise SourceError("unreadable", f"not valid CSV (line {reader.line_num})") from None
        if len(header) > MAX_COLUMNS:  # before any row is read
            raise SourceError("too-large", f"a header of more than {MAX_COLUMNS:,} columns: not read")
        # Each name's column, its last when the header names it twice (spaces around a name dropped), as it was read
        # when each row held every column (csv.DictReader); a field the header lacks is not made up.
        columns = {name.strip(): at for at, name in enumerate(header)}
        if fields is not None:
            columns = {name: columns[name] for name in fields if name in columns}
        return _rows(reader, columns)
    try:
        doc: Any = json.loads(text) if fmt == "json" else load_yaml(text)
    except json.JSONDecodeError as exc:
        raise SourceError("unreadable", f"not valid JSON (line {exc.lineno})") from None
    except yaml.YAMLError as exc:  # its text quotes the document: only the line is said
        mark = getattr(exc, "problem_mark", None)
        raise SourceError("unreadable", "not valid YAML" + (f" (line {mark.line + 1})" if mark else "")) from None
    for key in records.split(".") if records else ():
        doc = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(doc, list):
        place = records if records and records.isascii() and records.isprintable() else "the place set"
        raise SourceError("unreadable", f"no list of records at {place if records else 'the top'}")
    found = [r for r in doc if isinstance(r, dict)]
    if len(found) > MOST_RECORDS:
        raise _too_many_records()
    return found


def _rows(reader: Any, columns: Mapping[str, int]) -> Iterator[dict[str, Any]]:
    """The rows of a CSV file's ``reader`` (``csv.reader``, its header read), each as it is read: a record of
    ``columns`` (a name, its column), None where the row is shorter. A blank line is skipped, as csv.DictReader skips
    it; the row past ``MOST_RECORDS`` is refused. What the reader raises is said in Latet's words, as
    ``parse_records`` says it."""
    count = 0
    while True:
        try:
            row = next(reader, None)
        except csv.Error:
            raise SourceError("unreadable", f"not valid CSV (line {reader.line_num})") from None
        except MemoryError:
            raise SourceError("too-large", HELD) from None
        except Exception:
            raise SourceError("unreadable", "not valid CSV") from None
        if row is None:
            return
        if not row:
            continue
        count += 1
        if count > MOST_RECORDS:
            raise _too_many_records()
        yield {name: row[at] if at < len(row) else None for name, at in columns.items()}


def terms_of(source: BaseSource, records: Iterable[Mapping[str, Any]]) -> Mapped:
    """The terms ``source``'s mapping makes of ``records``: each once, in any case; what is not a term left out and
    counted."""
    excluded = {e.casefold() for e in source.exclude}
    found: dict[tuple[str, str], str] = {}
    skipped: Counter[str] = Counter()
    for record in records:
        for wanted, etype in source.mapping.items():
            for raw in _values(record, wanted):
                value = " ".join(raw.split())
                why = _left_out(value, etype, excluded, source.min_length) if value else None
                if why is not None:
                    skipped[why] += 1
                elif value:
                    found.setdefault((value.casefold(), etype), value)
        if len(found) > MOST_TERMS:
            raise SourceError("too-many", f"more than {MOST_TERMS:,} terms: narrow the source (a filter, fewer fields)")
    return Mapped([(value, etype) for (_, etype), value in found.items()], dict(skipped))


def _values(record: Mapping[str, Any], field: str) -> list[str]:
    """The texts ``field`` gives in ``record``: its value, each of its values, or a template filled from several
    fields when each of them has exactly one value."""
    names = TEMPLATE.findall(field)
    if names:
        parts = {name: _values(record, name) for name in names}
        if any(len(found) != 1 for found in parts.values()):
            return []
        return [TEMPLATE.sub(lambda m: parts[m.group(1)][0], field)]
    value = record.get(field)
    out = []
    for item in value if isinstance(value, list | tuple) else [value]:
        text = item.decode("utf-8", "replace") if isinstance(item, bytes) else item
        if isinstance(text, str | int | float) and not isinstance(text, bool):
            out.append(str(text))
    return out


def _unreadable(c: str) -> bool:
    """A control character, or a surrogate: half of a pair a JSON document may name alone (``\\ud83d``), which no store
    can keep (a term's id and its sealed value are UTF-8, and UTF-8 has no surrogates)."""
    code = ord(c)
    return code < 32 or code == 127 or 0xD800 <= code <= 0xDFFF


def _left_out(value: str, etype: str, excluded: set[str], min_length: int) -> str | None:
    """Why ``value`` is not a term, or None when it is one."""
    if value.casefold() in excluded:
        return "excluded"
    if not any(c.isalnum() for c in value) or any(_unreadable(c) for c in value):
        return "unreadable"
    if not long_enough(value, etype, min_length):
        return "short"
    return "long" if len(value) > MAX_LENGTH else None
