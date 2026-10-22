"""A Latet server's logs: one line per record on the standard output, as text or JSON lines, which hold no route key,
one-time code, token, key, password, value or text, and in which nobody writes a line of his own.

Every line passes through the redaction (``latet.logline.redacted``), which reads at most twice ``LINE_LIMIT`` of any
text, so that no line costs the event loop more than a bounded time whoever chose its length. A record of Latet's own
keeps its words, but an exception given to it, which is written by its type; a record of a library (uvicorn, psycopg
and its pool, redis, websockets, httpx, asyncio...) keeps the first line of its message's fixed text and the types of
its arguments, numbers as they are, since a connection's repr names its host and its user, a driver's words quote what
it refused (a unique violation quotes the value) and asyncio writes the reprs of what failed after its first line (and
before its first parenthesis, where its debug mode writes a callback's arguments). An exception is shown by its types
and its frames, for it and for each one of its chain and of its group, never by its words or its notes (but for a few
of Latet's own errors, whose words are fixed). A line escapes whatever would end it, act on a terminal or reorder what
follows it, and is cut at ``LINE_LIMIT``.

The level asked applies to Latet's own loggers (``latet`` and below), and to no other: uvicorn's are never open below
INFO, nor any library's below WARNING, whatever is asked, since their DEBUG lines hold headers, the texts of frames and
every request's scope. One line per request (``RequestLog``) replaces uvicorn's access lines, which are off, and its
WebSocket lines, which are dropped; it reads at most ``PATH_LIMIT`` of a path. What anyone can make happen at will is
said once a minute with how many times it happened, each kind on its own, and what was counted and not said yet is said
when ``flush`` is called (the beat, once a minute). Warnings, an exception that ends a thread or the process and one
that cannot be raised go to the same lines. A thread of its own writes them: no thread that logs, the event loop least
of all, waits on the output; what waits for it is bounded, and what it could not take in time is counted and said by
``flush``.

`latet server run` (E0c) calls ``configure_logging`` and starts uvicorn with ``log_config=None`` and ``log_level=None``,
so that uvicorn keeps these."""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import re
import sys
import threading
import time
import traceback
import warnings
import weakref
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, TextIO
from urllib.parse import quote, unquote_to_bytes

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from latet.logline import Paced, redacted

log = logging.getLogger("latet.server.requests")
_log = logging.getLogger(__name__)  # what the hooks say, and a record that could not be written

LEVELS = ("debug", "info", "warning", "error")  # the levels a server's log takes
# The libraries a server runs, held at WARNING whatever is asked: below it psycopg and its pool name hosts and users,
# websockets writes each header of a handshake and the text of each frame, httpcore the headers of each answer, httpx
# each upstream address with its query, uvicorn.asgi each request's scope.
LIBRARIES = ("asyncio", "httpcore", "httpx", "PIL", "psycopg", "psycopg_pool", "redis", "uvicorn.asgi", "websockets")
_UVICORN = ("uvicorn", "uvicorn.error")  # held at INFO: its start, its stop, its warnings, an application's error
LINE_LIMIT = 8192  # bytes of a line at most: a record's in JSON, each of a record's lines in text
# What waits for the output, written by a thread of its own, at most: past either, a record is dropped and counted. A
# burst of a few hundred lines waits whole; an output that takes nothing for minutes holds a few MiB, never the loop.
QUEUE_LINES = 1000
QUEUE_BYTES = 4 * 1024 * 1024
CLOSE_SECONDS = 5.0  # what the writer is waited for when it is closed (another configure_logging, the process's end)
# Bytes of a request's path a line reads at most, as it was sent: Latet's longest route, a Bedrock call naming a model
# by its ARN, is a few hundred. Past it, the path is cut, before it is decoded or redacted.
PATH_LIMIT = 1024
_METHOD_LIMIT = 32  # characters of a request's method a line writes at most: an HTTP method is a short word
# Where Task 14's gate marks, in a request's scope, a request it answered itself (its limits, its refusals): what it
# gives is paced, whatever its status (a sign-in link that opens nothing is a 303), and apart from what an upstream gave
# an identity's agent.
REFUSED = "latet.refused"
_REQUEST_FIELDS = ("method", "path", "status", "ms")
_CUT = " [cut]"
_MIDDLE = ("exception", "stack")  # what a JSON line cuts in its middle: where it started, and where it ended
_MARKER = "  | "  # what each line of a record's traceback starts with, in text: no record's own line does
_BACKSLASH = chr(0x5C)
# What a line holds escaped, never as it is: the C0 and C1 controls and DEL (a line break, an escape sequence that a
# terminal acts on), the line and paragraph separators (which split a line for some readers), the bidirectional controls
# (a right-to-left override, the isolates, the marks: they reorder what follows them where a terminal or a viewer
# applies them), and the lone surrogates (no text: an output that writes UTF-8 strictly cannot write them).
_UNSAFE_RANGES = (
    (0x00, 0x1F),
    (0x7F, 0x9F),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x2028, 0x2029),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0xD800, 0xDFFF),
)
_UNSAFE = re.compile("[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _UNSAFE_RANGES) + "]")
# uvicorn's own line for each WebSocket, with the client's address and the raw query: RequestLog writes one instead.
_WEBSOCKET_LINES = frozenset({'%s - "WebSocket %s" [accepted]', '%s - "WebSocket %s" 403', '%s - "WebSocket %s" %d'})
# uvicorn's warnings that a stranger makes at will, one a request: said once a minute with how many.
_REPEATED = ("Invalid HTTP request received.", "Unsupported upgrade request.", "Exceeded concurrency limit.")
# Latet's own errors whose words are fixed, written by Latet and naming no value: a traceback keeps their words, and so
# does a line given one (each raise site read for it: a schema's name, an encoding, a setting's name, a count, an HTTP
# status, an error's type and its number's fixed text, a certificate refused, a space past its share: ``PAST_SHARE``).
_WORDS_KEPT = frozenset(
    {
        "latet.engine.detectors.base.DetectorSkipped",
        "latet.engine.reviewer.client.ReviewError",
        "latet.engine.vault.MaskingError",
        "latet.runtime.SpaceFull",
        "latet.server.db.DatabaseError",
        "latet.server.db.SchemaMismatch",
        "latet.server.vault.VaultFull",
    }
)
_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"
_GROUP_WIDTH, _GROUP_DEPTH = 15, 10  # as Python shows a group: its first 15 members, 10 groups deep
# A dashboard project's id, in the dashboard's paths: a hash of the project's folder, which a list of usual folders
# gives back.
_PROJECT = re.compile(r"(/_latet/api/v1/projects/)[^/?#]+")
# A route key wherever a path places it (``//u//<key>``, ``/u;x/<key>``, right after an escape, a byte that is no text,
# a letter or a digit): every run of 26 characters or more of its alphabet, in either case, read in the path as the
# router reads it (its escapes decoded), before what a line cannot hold is escaped again, whose hex digits would sit
# against the key. A line loses no word it needs: none of Latet's paths holds such a run.
_KEY_RUN = re.compile(rb"(?i)[a-z2-7]{26,}")
# A secret of Latet's written in a path (a base URL pasted wrong, say), taken out whole before the route keys' runs are:
# a run of 26 letters of their alphabet inside its body would cut it there, and the rest of it be written.
_SECRET_IN_PATH = re.compile(rb"(latet_(?:pat|key|adm|ck)_)[A-Za-z0-9_-]+")
# What a path, as a line writes it, keeps as it is: printable ASCII, but the sign of an escape and those of a query and
# of a fragment. Anything else (a control, a space, what is not ASCII) is written escaped, as a URL writes it.
_PLAIN_IN_PATH = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) not in "%?#")
# An address's password that runs to the end of a text cut before it was redacted: its last @, by which the redaction
# knows where it ends, was cut away with the rest (an @ the password holds may be left before the cut).
_UNENDED = re.compile(r"(://[^\s:/@]*:)[^\s/]*\Z")
# A request's line as RequestLog makes it (its path cut, read and redacted, its method cut and redacted): the formatter
# does not redact those twice.
_MADE = object()


# --- what a line may show --------------------------------------------------------------------------------------------


def _own(name: str) -> bool:
    """Whether a logger is Latet's own."""
    return name == "latet" or name.startswith("latet.")


def _kind(value: object) -> str:
    return f"<{type(value).__name__}>"


class _Kind:
    """A library's argument in its place in the message: its type, however the message formats it."""

    __slots__ = ("_text",)

    def __init__(self, value: object) -> None:
        self._text = _kind(value)

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._text


def _stand_in(value: object) -> object:
    """What a library's argument is shown as: a number as itself, anything else by its type."""
    return value if isinstance(value, int | float) else _Kind(value)


def _shape(record: logging.LogRecord) -> str:
    """A library's message as a log may show it: its fixed text, with each argument shown by its type (a number as it
    is). A message that is no text is shown by its type too (``logger.error(exc)``). A mapping that logging made the
    record's arguments (one given alone) fills named fields only where the message has one (``%(host)s``); anywhere
    else it is one argument like any other, and its keys are never written."""
    if not isinstance(record.msg, str):
        return _kind(record.msg)
    args = record.args
    if not args:
        return record.msg
    if isinstance(args, Mapping) and "%(" not in record.msg:
        args = (args,)
    try:
        if isinstance(args, Mapping):
            return record.msg % {key: _stand_in(value) for key, value in args.items()}
        return record.msg % tuple(_stand_in(value) for value in args)
    except (TypeError, ValueError, KeyError):  # a format the stand-ins do not fit: the text, and the types after it
        values = args.values() if isinstance(args, Mapping) else args
        return f"{record.msg} ({', '.join(str(_stand_in(value)) for value in values)})"


def _message(record: logging.LogRecord) -> str:
    """What a log may show of a record's message: Latet's own as it is, but an exception it is given (``_own_message``);
    a library's fixed text and the types of its arguments, its first line alone (what asyncio writes after it is the
    reprs of the task or the future that failed, which hold what they raised; what Starlette hands uvicorn when its
    start fails, a whole traceback), and asyncio's up to its first parenthesis (its debug mode names a callback there
    with the reprs of its arguments). Without what ends it."""
    if _own(record.name):
        return _own_message(record).rstrip()
    first = _shape(record).partition("\n")[0]
    if record.name == "asyncio" or record.name.startswith("asyncio."):
        first = first.partition("(")[0]
    return first.rstrip()


def _named(kind: type[BaseException]) -> str:
    """An exception's type as a traceback names it."""
    module = kind.__module__
    return kind.__qualname__ if module in ("builtins", "__main__") else f"{module}.{kind.__qualname__}"


def _last(error: BaseException) -> str:
    """The last line of an exception's traceback: its type, with its words for those of ``_WORDS_KEPT``."""
    name = _named(type(error))
    if name in _WORDS_KEPT and (words := str(error)):
        return f"{name}: {words}"
    return name


def _typed(value: object) -> object:
    return _last(value) if isinstance(value, BaseException) else value


def _own_message(record: logging.LogRecord) -> str:
    """A record of Latet's own with its words, but an exception it is given, as its message or as one of its arguments,
    which is written as a traceback's last line names it (``_last``): the net under the reading of every logging call
    of Latet's (tests/unit/test_logging_calls.py), for a call it could not follow. Made beside the record, which other
    handlers see as it was."""
    text = str(_typed(record.msg))
    args = record.args
    if not args:
        return text
    if isinstance(args, Mapping):
        return text % {key: _typed(value) for key, value in args.items()}
    return text % tuple(_typed(value) for value in args)


def _shown(error: BaseException, seen: set[int], depth: int) -> list[str]:
    """``error`` as Python shows an exception, by its types and frames alone: its chain first (what caused it, or what
    was being handled when it was raised), the members of a group after the group."""
    chain: list[tuple[str | None, BaseException]] = []
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__cause__ is not None:
            link, following = _CAUSE, current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            link, following = _CONTEXT, current.__context__
        else:
            link, following = None, None
        chain.append((link, current))
        current = following
    lines: list[str] = []
    for link, exc in reversed(chain):
        if link is not None:
            lines += ["", link, ""]
        group = isinstance(exc, BaseExceptionGroup)
        if exc.__traceback__ is not None:
            lines.append(f"{'Exception Group Traceback' if group else 'Traceback'} (most recent call last):")
            lines += [
                line for frame in traceback.format_tb(exc.__traceback__) for line in frame.rstrip("\n").split("\n")
            ]
        lines.append(_last(exc))
        if isinstance(exc, BaseExceptionGroup):
            if depth >= _GROUP_DEPTH:
                lines.append(f"... (max_group_depth is {_GROUP_DEPTH})")
                continue
            for index, member in enumerate(exc.exceptions[:_GROUP_WIDTH], 1):
                lines.append(f"+---------------- {index} ----------------")
                lines += [f"| {line}" for line in _shown(member, seen, depth + 1)]
            if len(exc.exceptions) > _GROUP_WIDTH:
                lines.append(f"and {len(exc.exceptions) - _GROUP_WIDTH} more exceptions")
    return lines


def _exception(record: logging.LogRecord) -> list[str]:
    """The lines of a record's exception, by its types and frames, made from the exception itself every time: never a
    traceback another formatter made and left in the record (``exc_text``), which holds its words."""
    info = record.exc_info
    if not info or info[0] is None:
        return []
    if info[1] is None:
        return [_named(info[0])]
    return _shown(info[1], set(), 0)


def frames_only() -> None:
    """From now on in this process, a record made with an exception carries that exception's types and frames as its
    text (``exc_text``), as a server's log shows them (``_exception``), never its words nor its notes: a plain
    formatter, which writes that text in place of the traceback it would make, writes them so (a computer's daemon.log,
    a console). A server's formatters make the same from the exception itself, and ignore that text. A runtime calls it
    as it is made; once is enough."""
    made = logging.getLogRecordFactory()
    if getattr(made, "frames_only", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = made(*args, **kwargs)
        if record.exc_info and record.exc_info[0] is not None:
            try:
                record.exc_text = "\n".join(_exception(record))
            except Exception:  # never in the way of the record itself: its type alone
                record.exc_text = _named(record.exc_info[0])
        return record

    factory.frames_only = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def _escape(found: re.Match[str]) -> str:
    code = ord(found.group())
    return f"{_BACKSLASH}x{code:02x}" if code < 0x100 else f"{_BACKSLASH}u{code:04x}"


def _escaped(text: str) -> str:
    """``text`` on one line: what ``_UNSAFE`` holds written as its code (``\\x1b``, ``\\u2028``, ``\\u202e``)."""
    return _UNSAFE.sub(_escape, text)


def _redacted(text: str) -> str:
    """``text`` redacted, read up to twice LINE_LIMIT characters at most: a text longer than that is cut first, so that
    the redaction of any text costs a bounded time, whoever chose its length. Safe, because what this cut could split
    lies past the line's own cut, at LINE_LIMIT bytes, but for an address's password, which the redaction knows by the
    ``@`` after it, cut away too: one left running to the end is taken out here."""
    if len(text) > 2 * LINE_LIMIT:
        text = _UNENDED.sub(r"\1***", text[: 2 * LINE_LIMIT])
    return redacted(text)


def _traceback(lines: list[str]) -> str:
    """A traceback's lines redacted as one text, read up to twice LINE_LIMIT characters at most: past that, its first
    lines and its last ones (where it started and what was raised), whole lines either side of the cut, each of them cut
    as any text is first."""
    lines = [line if len(line) <= 2 * LINE_LIMIT else _UNENDED.sub(r"\1***", line[: 2 * LINE_LIMIT]) for line in lines]
    if sum(len(line) + 1 for line in lines) <= 2 * LINE_LIMIT:
        return redacted("\n".join(lines))
    room = LINE_LIMIT - len(_CUT)
    head: list[str] = []
    for line in lines:
        if len(line) + 1 > room:
            break
        head.append(line)
        room -= len(line) + 1
    room, tail = LINE_LIMIT - len(_CUT), []
    for line in reversed(lines[len(head) :]):
        if len(line) + 1 > room:
            break
        tail.append(line)
        room -= len(line) + 1
    return redacted("\n".join([*head, _CUT.strip(), *reversed(tail)]))


def _cut(line: str) -> str:
    """A text line, or its start and ``_CUT``, in LINE_LIMIT bytes of UTF-8 at most (always cut after the redaction: a
    secret cut first could lose what the redaction knows it by, an address its ``@``)."""
    if len(line) * 4 <= LINE_LIMIT:
        return line
    data = line.encode("utf-8", "surrogatepass")
    if len(data) <= LINE_LIMIT:
        return line
    return data[: LINE_LIMIT - len(_CUT)].decode("utf-8", "ignore") + _CUT


def _line(text: str) -> str:
    return _cut(_escaped(_redacted(text)))


def _width(text: str) -> int:
    """How many characters ``text`` takes in a JSON line written in ASCII (an escape takes two to twelve)."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def _shortened(value: str, keep: int, middle: bool) -> str:
    """``value`` cut to ``keep`` characters and ``_CUT``: its start, or its start and its end (``middle``)."""
    keep = max(keep, 0)
    if middle:
        head = keep // 2
        return value[:head] + _CUT + value[len(value) - (keep - head) :]
    return value[:keep] + _CUT


def _within(value: str, width: int, middle: bool) -> str:
    """``value`` cut so that it takes ``width`` characters at most in a JSON line."""
    cut = _shortened(value, width - len(_CUT), middle)
    over = _width(cut) - width
    if over > 0:  # escapes: what it is over by goes with as many characters, each taking one at least
        cut = _shortened(value, width - len(_CUT) - over, middle)
    return cut


def _shares(widths: dict[str, int], room: int) -> dict[str, int]:
    """How much of ``room`` each text may take: what it needs, and the longest share evenly what the others leave."""
    shares: dict[str, int] = {}
    waiting = sorted(widths, key=widths.__getitem__)
    while waiting:
        even = room // len(waiting)
        if widths[waiting[0]] > even:
            shares.update(dict.fromkeys(waiting, even))
            break
        name = waiting.pop(0)
        shares[name] = widths[name]
        room -= widths[name]
    return shares


def _fit(line: dict[str, Any]) -> str:
    """``line`` as JSON on one line of LINE_LIMIT characters at most, in ASCII (whatever would end a line or act on a
    terminal is written escaped; what JSON cannot hold is written by its type). Over the limit, its longest texts are
    cut to share what the others leave: a traceback or a stack in its middle, which keeps where it started and what was
    raised."""
    text = json.dumps(line, ensure_ascii=True, default=_kind)
    over = len(text) - LINE_LIMIT
    if over <= 0:
        return text
    widths = {name: _width(value) for name, value in line.items() if isinstance(value, str)}
    for name, share in _shares(widths, sum(widths.values()) - over).items():
        if share < widths[name]:
            line[name] = _within(line[name], share, middle=name in _MIDDLE)
    return json.dumps(line, ensure_ascii=True, default=_kind)


def _when(record: logging.LogRecord, sep: str = "T") -> str:
    """When a record was made, in UTC with its zone: instances in different zones say the same time. ``sep`` goes
    between the date and the time: a text line keeps them two fields, as it had them before it said its zone."""
    return datetime.fromtimestamp(record.created, UTC).isoformat(sep=sep, timespec="milliseconds")


class TextFormatter(logging.Formatter):
    """A record as text: one line with the date and the time (UTC, with its zone), the level, the logger and the
    message, each where a line had it before it said its zone, then its exception's traceback (types and frames) and
    its stack, each of their lines starting with ``_MARKER``, which no record's own line does. Every line redacted
    once, what would end it, act on a terminal or reorder it escaped, and cut at LINE_LIMIT."""

    def format(self, record: logging.LogRecord) -> str:
        lines = [_line(f"{_when(record, ' ')} {record.levelname} {record.name}: {_message(record)}")]
        more = _exception(record)
        if record.stack_info:
            more += record.stack_info.split("\n")
        lines += [_line(_MARKER + line) for line in more]
        return "\n".join(lines)


class JsonFormatter(logging.Formatter):
    """A record as one JSON object on one line: ``ts``, ``level``, ``logger``, ``message``; the request's ``method``,
    ``path``, ``status`` and ``ms`` when a record of Latet's has them; ``exception`` (its types and frames) when it has
    one, and ``stack`` (its frames) when it was asked for. Every text redacted once (a request's path and method, which
    RequestLog made so, are not again), the line in ASCII and at most LINE_LIMIT long."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": _when(record),
            "level": record.levelname,
            "logger": record.name,
            "message": _redacted(_message(record)),
        }
        if _own(record.name):
            made = getattr(record, "latet_request", None) is _MADE
            for name in _REQUEST_FIELDS:
                value = getattr(record, name, None)
                if value is not None:
                    line[name] = _redacted(value) if isinstance(value, str) and not made else value
        if exception := _exception(record):
            line["exception"] = _traceback(exception)
        if record.stack_info:
            line["stack"] = _traceback(record.stack_info.split(chr(10)))
        return _fit(line)


# --- which records, and how often ------------------------------------------------------------------------------------


class _Gate(logging.Filter):
    """Which records reach the lines: Latet's at the level asked; uvicorn's at INFO or above (never its access lines nor
    its WebSocket lines, and its warnings a stranger makes at will once a minute); every other one at WARNING or
    above, whatever level a library's logger was left at."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level
        self._repeated = {message: Paced() for message in _REPEATED}
        self._from: dict[str, tuple[str, int]] = {}  # the logger and the level each of them came with, for ``flush``

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        name = record.name
        if _own(name):
            return True
        if name != "uvicorn" and not name.startswith("uvicorn."):
            return record.levelno >= max(logging.WARNING, self._level)
        if name == "uvicorn.access" or record.levelno < max(logging.INFO, self._level):
            return False
        if not isinstance(record.msg, str):
            return True
        if name == "uvicorn.error" and record.msg in _WEBSOCKET_LINES:
            return False
        paced = self._repeated.get(record.msg)
        if paced is None:
            return True
        self._from[record.msg] = (name, record.levelno)
        count = paced.due()
        if count < 2:
            return count == 1
        said = logging.makeLogRecord(record.__dict__)  # the record other handlers see stays as it was
        said.msg, said.args = f"{record.msg} ({count} since the line before)", None
        return said

    def flush(self) -> None:
        """Each of those warnings that came since its line before and was not said yet, said now with how many."""
        for message, paced in self._repeated.items():
            if count := paced.flush():
                name, level = self._from.get(message, ("uvicorn.error", logging.WARNING))
                logging.getLogger(name).log(level, f"{message} ({count} since the line before)")


class _Dropped:
    """The records the writers dropped since the process began, never reset: what the metrics say
    (``latet_log_dropped_total``), read from outside the log while its output takes nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def add(self) -> None:
        with self._lock:
            self._count += 1

    def total(self) -> int:
        with self._lock:
            return self._count


_DROPPED = _Dropped()


def dropped() -> int:
    """How many log records the writers dropped since the process began, their output not taking them in time."""
    return _DROPPED.total()


class _Writer(logging.Handler):
    """The standard output, written by a thread of its own: each record is formatted where it is logged (redacted,
    escaped and cut, a line's cost bounded) and handed to that thread, so that no thread that logs, the event loop least
    of all, ever waits on the output (a log driver that blocks while its endpoint is away, a pipe nobody reads). What
    waits for the output is bounded (``QUEUE_LINES``, ``QUEUE_BYTES``): past it a record is dropped and counted, never
    waited for, and ``say_dropped`` (the beat's ``flush``, once a minute) says how many once there is room. A record
    that cannot be formatted, or a line the output refuses, leaves a line that says so by the error's type: logging's
    own would write the record's message and arguments to the standard error, past the redaction."""

    def __init__(self, stream: TextIO) -> None:
        super().__init__()
        self.stream = stream
        self._lines: collections.deque[str] = collections.deque()
        self._bytes = 0  # what the lines waiting hold
        self._dropped = 0  # records dropped since the line that said so
        self._writing = False  # a line taken by the thread and not written yet
        self._closed = False
        self._ready = threading.Condition(threading.Lock())
        self._thread = threading.Thread(target=self._write, name="latet-log-writer", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception as exc:  # a format that does not fit its arguments, say
            line = self._failed(exc)
        self._put(line + "\n")

    def _failed(self, error: BaseException) -> str:
        said = logging.LogRecord(
            _log.name, logging.ERROR, __file__, 0, "a log record could not be written: %s", (_named(type(error)),), None
        )
        try:
            return self.format(said)
        except Exception:
            return "a log record could not be written"

    def _put(self, line: str) -> bool:
        """``line`` handed to the thread, or dropped and counted when what waits is at its bound: never waited for."""
        with self._ready:
            if self._closed or len(self._lines) >= QUEUE_LINES or self._bytes + len(line) > QUEUE_BYTES:
                self._dropped += 1
                _DROPPED.add()
                return False
            self._lines.append(line)
            self._bytes += len(line)
            self._ready.notify_all()
        return True

    def _write(self) -> None:
        while True:
            with self._ready:
                while not self._lines and not self._closed:
                    self._ready.wait()
                if not self._lines:
                    return  # closed, and everything written
                line = self._lines.popleft()
                self._writing = True
            try:
                self.stream.write(line)
                self.stream.flush()
            except Exception as exc:  # an output that refuses a character, or fails: said by the error's type
                with contextlib.suppress(Exception):  # the output itself fails: nothing is left to tell it
                    self.stream.write(self._failed(exc) + "\n")
                    self.stream.flush()
            finally:
                with self._ready:
                    self._bytes -= len(line)
                    self._writing = False
                    self._ready.notify_all()

    def say_dropped(self) -> None:
        """How many records were dropped since the line before, said now, when there is room for the line; else they
        are counted on and said by a later call."""
        with self._ready:
            count = self._dropped
            if not count or self._closed or len(self._lines) >= QUEUE_LINES:
                return
            self._dropped = 0
        many = "record was" if count == 1 else "records were"
        said = logging.LogRecord(
            _log.name, logging.WARNING, __file__, 0,
            "%d log %s dropped since the line before: the output did not take them in time", (count, many), None,
        )  # fmt: skip
        if not self._put(self.format(said) + "\n"):
            with self._ready:
                self._dropped += count  # no room after all: said by a later call, the notice counted with them

    def flush(self, timeout: float = 5.0) -> None:
        """Wait, ``timeout`` seconds at most, for what waits to be written (the tests; the process's end)."""
        with self._ready:
            self._ready.wait_for(lambda: not self._lines and not self._writing, timeout)

    def close(self) -> None:
        """No more lines: what waits is written first (``CLOSE_SECONDS`` at most), then the thread ends."""
        with self._ready:
            self._closed = True
            self._ready.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(CLOSE_SECONDS)
        super().close()


# --- warnings and exceptions that nothing catches ---------------------------------------------------------------------


def _warned(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    """A warning, in the log: its category and where it was raised, never its words (``warnings.showwarning``)."""
    said = f"{category.__name__} at {filename}, line {lineno}"
    logging.getLogger("py.warnings").warning(said)


def _uncaught(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
    """``sys.excepthook``: an exception that ends the process, by its types and frames."""
    _log.critical("the process ends on an uncaught %s", _named(kind), exc_info=(kind, error, trace))


def _thread_ended(args: threading.ExceptHookArgs) -> None:
    """``threading.excepthook``: an exception that ends a thread, by its types and frames (a thread that exits is
    none)."""
    if issubclass(args.exc_type, SystemExit):
        return
    _log.error("a thread ended on an uncaught %s", _named(args.exc_type), exc_info=args.exc_value)


def _unraisable(unraisable: sys.UnraisableHookArgs) -> None:
    """``sys.unraisablehook``: an exception that could not be raised (in a finalizer, say), by its types and frames,
    never the object it was raised in."""
    _log.error("an exception could not be raised: %s", _named(unraisable.exc_type), exc_info=unraisable.exc_value)


# --- the whole of it -------------------------------------------------------------------------------------------------


def _level(level: str) -> int:
    if not isinstance(level, str) or level.lower() not in LEVELS:
        raise ValueError(f"the log level is {', '.join(LEVELS[:-1])} or {LEVELS[-1]}")
    return logging.getLevelNamesMapping()[level.upper()]


def configure_logging(format: Literal["text", "json"], level: str, *, stream: TextIO | None = None) -> None:
    """Every record of the process to ``stream`` (the standard output), one line each, as ``format`` says (text or json:
    anything else is a ValueError): Latet's at ``level`` (debug, info, warning or error: anything else is a ValueError),
    uvicorn's from INFO up (its access lines off, its WebSocket lines dropped: ``RequestLog`` writes theirs), every
    other logger's from WARNING up; warnings, and an exception that ends a thread or the process or that cannot be
    raised, to the same lines, written by a thread of their own (``_Writer``)."""
    if format not in ("text", "json"):
        raise ValueError("the log format is text or json")
    asked = _level(level)
    handler = _Writer(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter() if format == "json" else TextFormatter())
    handler.addFilter(_Gate(asked))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        if isinstance(old, _Writer):  # what it holds is written first
            old.close()
    root.addHandler(handler)
    root.setLevel(max(logging.WARNING, asked))
    logging.getLogger("latet").setLevel(asked)
    for name in _UVICORN:  # whatever level an earlier start of uvicorn left them at
        found = logging.getLogger(name)
        found.handlers.clear()
        found.propagate = True
        found.setLevel(max(logging.INFO, asked))
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in LIBRARIES:
        logging.getLogger(name).setLevel(max(logging.WARNING, asked))
    warnings.showwarning = _warned
    sys.excepthook, threading.excepthook, sys.unraisablehook = _uncaught, _thread_ended, _unraisable


_KEPT = ("", "latet", *_UVICORN, "uvicorn.access", "py.warnings", *LIBRARIES)


@contextlib.contextmanager
def saved() -> Iterator[None]:
    """While it lasts ``configure_logging`` may be called: after it, each logger it changes, how warnings are shown and
    the three hooks are as they were (the tests, which share one process)."""
    loggers = {name: logging.getLogger(name) for name in _KEPT}
    kept = {
        name: (list(found.handlers), found.level, found.propagate, found.disabled) for name, found in loggers.items()
    }
    hooks = (warnings.showwarning, sys.excepthook, threading.excepthook, sys.unraisablehook)
    try:
        yield
    finally:
        for name, (handlers, level, propagate, disabled) in kept.items():
            found = loggers[name]
            for writer in [h for h in found.handlers if isinstance(h, _Writer) and h not in handlers]:
                writer.close()  # what it holds is written first, and its thread ends
            found.handlers[:] = handlers
            found.setLevel(level)
            found.propagate, found.disabled = propagate, disabled
        warnings.showwarning, sys.excepthook, threading.excepthook, sys.unraisablehook = hooks


# --- one line per request --------------------------------------------------------------------------------------------


def _within_limit(path: bytes) -> tuple[bytes, bool]:
    """At most PATH_LIMIT bytes of a path, and whether it was cut: cut after its last ``/`` within them, so that no
    segment is left in part (part of a route key is too short for the redaction to know it)."""
    if len(path) <= PATH_LIMIT:
        return path, False
    return path[: path.rfind(b"/", 0, PATH_LIMIT) + 1], True


def _path(scope: Scope) -> str:
    """A request's path as a log may show it: PATH_LIMIT bytes of it at most as it was sent, read before anything else
    and said cut when it was longer, without its query, which may hold anything; read as the router reads it (its
    escapes decoded), so that a route key or a session's id shows as itself however it was written, a secret of Latet's
    taken out whole (``_SECRET_IN_PATH``) and then a route key wherever it stands (``_KEY_RUN``); written again with
    only what a line cannot hold escaped; redacted, and a dashboard project's id taken out."""
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        sent, cut = _within_limit(raw[: PATH_LIMIT + 1].partition(b"?")[0])
        read = unquote_to_bytes(sent)
    else:  # a server that gives no raw path: the path it decoded
        read, cut = _within_limit(str(scope.get("path", ""))[: PATH_LIMIT + 1].encode("utf-8", "surrogatepass"))
    path = quote(_KEY_RUN.sub(b"***", _SECRET_IN_PATH.sub(rb"\1***", read)), safe=_PLAIN_IN_PATH)
    if len(path) > PATH_LIMIT:  # longer again with its escapes
        path, cut = path[: path.rfind("/", 0, PATH_LIMIT) + 1], True
    path = _PROJECT.sub(r"\1***", redacted(path))
    return path + _CUT if cut else path


# The areas of a server's paths, each paced answer counted with its own kind: an identity's address, the
# administration, the dashboard, and the server's own (anything else).
_AREAS = (
    ("/u/", "an identity's address"),
    ("/_latet/server/v1/admin/", "the administration"),
    ("/_latet/api/", "the dashboard"),
    ("/_latet/ui/", "the dashboard"),
    ("/_latet/admin/", "the dashboard"),
)
# What anyone can make happen at will, one a request: said once a minute with how many, each status and kind apart. A
# request with no credential (401), with a method its route does not take (405), or that says a body too large (413)
# costs its sender nothing more than one that is refused.
_PACED = frozenset({401, 403, 404, 405, 413, 429})


def _how(status: int, refused: bool) -> str:
    """How the requests of a paced kind were answered: by the gate itself (``REFUSED``), or by what is behind it."""
    return f"refused {status} by the gate" if refused else f"answered {status}"


def _area(scope: Scope) -> str:
    """Where a request went, as the router read its path."""
    path = str(scope.get("path", ""))
    return next((area for start, area in _AREAS if path.startswith(start) or path == start[:-1]), "the server's own")


# Every RequestLog of the process, for ``flush``.
_REQUEST_LOGS: weakref.WeakSet[RequestLog] = weakref.WeakSet()


class RequestLog:
    """One line for each HTTP request and each WebSocket, when it ends: its method (``WS`` for a WebSocket), its path
    (``_path``), its status and how long it took in milliseconds. A WebSocket's status is 101 once accepted, the status
    of its refused handshake (403 for one closed before it was accepted), else the code of the close that ended it.
    Requests answered 401, 403, 404, 405, 413 or 429 (``_PACED``), and any answer the server marked its own refusal
    (``REFUSED``: a sign-in link that opens nothing, a store that did not answer), which anyone can make at will, are
    said once a minute with how many, each status and kind on its own (the path's area, and whether the gate refused it
    itself), so that a flood of one kind never hides the first line of another; what was counted since is said by
    ``flush``. It never writes the client's address, its headers, its query or its body, and reads no more of a path
    than its line holds (``PATH_LIMIT``)."""

    def __init__(self, app: ASGIApp, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self._clock = clock
        self._refused: dict[tuple[int, str, bool], Paced] = {}
        _REQUEST_LOGS.add(self)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        started, status, answered = self._clock(), 500, False

        async def sending(message: Message) -> None:
            nonlocal status, answered
            kind = message["type"]
            if kind == "http.response.start":
                status = int(message["status"])
            elif answered:
                pass
            elif kind == "websocket.accept":
                status, answered = 101, True
            elif kind == "websocket.close":  # before it was accepted: refused (uvicorn answers 403)
                status, answered = 403, True
            elif kind == "websocket.http.response.start":
                status, answered = int(message["status"]), True
            await send(message)

        async def receiving() -> Message:
            nonlocal status
            message = await receive()
            if message["type"] == "websocket.disconnect" and not answered:
                status = int(message.get("code", 1005))
            return message

        try:
            await self.app(scope, receiving if scope["type"] == "websocket" else receive, sending)
        finally:
            self._say(scope, status, round((self._clock() - started) * 1000))

    def _say(self, scope: Scope, status: int, ms: int) -> None:
        count, refused = 1, bool(scope.get(REFUSED))
        if status in _PACED or refused:  # decided first: a request counted and not said costs no reading of its path
            kind = (status, _area(scope), refused)
            paced = self._refused.get(kind) or self._refused.setdefault(kind, Paced(clock=self._clock))
            if not (count := paced.due()):
                return
        method = "WS" if scope["type"] == "websocket" else redacted(str(scope.get("method", ""))[:_METHOD_LIMIT])
        path = _path(scope)
        fields = {"method": method, "path": path, "status": status, "ms": ms, "latet_request": _MADE}
        if count > 1:
            line = "%s %s %d %d ms (%d %s since the line before)"
            log.info(line, method, path, status, ms, count, _how(status, refused), extra=fields)
            return
        log.info("%s %s %d %d ms", method, path, status, ms, extra=fields)

    def flush(self) -> None:
        """For each status and kind paced, how many came since its line before and were not said yet, said now."""
        for (status, area, refused), paced in list(self._refused.items()):
            if count := paced.flush():
                said, many = "%d more %s to %s %s since the line before", "request" if count == 1 else "requests"
                log.info(said, count, many, area, _how(status, refused), extra={"status": status})


def flush() -> None:
    """What was counted and not said yet, said now: each RequestLog's paced answers of each kind, uvicorn's warnings
    that a stranger makes at will, each with how many, and the records the output could not take in time. Task 14's
    beat calls it once a minute, so that a burst is told when it ends and not when the same thing next happens, which
    may be never."""
    for request_log in list(_REQUEST_LOGS):
        request_log.flush()
    for handler in logging.getLogger().handlers:
        for gate in handler.filters:
            if isinstance(gate, _Gate):
                gate.flush()
        if isinstance(handler, _Writer):
            handler.say_dropped()
