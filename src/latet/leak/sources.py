"""The coding agents' saved sessions as texts, for ``latet leak``.

One reader of JSON for every agent: it takes each string of each saved record, minus the keys that carry no
conversation (ids, signatures, times) and those an agent keeps of what it never sent (``LOCAL_ONLY_KEYS``: Claude
Code's copy of a tool's result for its own screen), and needs per agent only where the sessions are and which keys
name the session, the project folder and the time. A new format of an agent's still reads, as long as it is JSON.

Where the sessions are, as confirmed against each agent's source:

* Claude Code: ``<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/*.jsonl`` and ``projects/*/*/subagents/*.jsonl``
  (its documentation, "Explore the .claude directory" and the environment variables, names ``~/.claude`` on every
  system, and no other place).
* Codex: ``<CODEX_HOME or ~/.codex>/sessions/**/rollout-*.jsonl`` and ``archived_sessions/**``.
* OpenCode: ``<XDG_DATA_HOME or ~/.local/share>/opencode/opencode.db`` (SQLite) and the older
  ``opencode/storage/{session,message,part}/**/*.json``.
* Copilot CLI (v1.0.112; its repository holds documentation only, ``COPILOT_HOME`` is named in its container
  guide): ``<COPILOT_HOME or ~/.copilot>/session-state/*/events.jsonl``. Its configuration directory's reference says
  ``COPILOT_HOME`` replaces the whole ``~/.copilot``, and that earlier XDG locations are moved into ``~/.copilot`` at
  its start: ``XDG_CONFIG_HOME`` names none of its folders.
* Gemini CLI (v0.68.0, ``chatRecordingService.ts``):
  ``<GEMINI_CLI_HOME or ~>/.gemini/tmp/<project>/chats/session-*.jsonl``
  (a metadata line, then message lines), and the older whole-file ``session-*.json``.
* Qwen Code (v0.27.3, ``chatRecordingService.ts``): ``<QWEN_HOME or ~/.qwen>/projects/<cwd>/chats/<session>.jsonl``;
  the Gemini-derived ``tmp/*/chats/*.json`` of its older releases.
* Hermes Agent (v0.23.1, ``hermes_state_common.py``): ``<HERMES_HOME or the platform default>/state.db``
  (``%LOCALAPPDATA%/hermes`` on Windows, ``~/.hermes`` elsewhere and as the older Windows place), SQLite tables
  ``sessions`` (``id``, ``cwd``, ``title``, ``started_at``) and ``messages`` (``session_id``, ``content``,
  ``tool_calls``, ``reasoning``, ``reasoning_content``, ``timestamp``).

The texts of ``read(extra=...)`` (the ``--path`` files and folders) carry the agent name ``"path"``; their records drop
every agent's ``LOCAL_ONLY_KEYS``, as a copy of Claude Code's sessions needs.

Everything is opened for reading only; the files are read line by line. A SQLite database in WAL mode (opening it,
even read-only, creates its ``-wal`` and ``-shm`` files) is read from a private copy in a temporary folder, deleted
after, made again when the database or its ``-wal`` changed while they were copied, and made only where the disk keeps
room to spare (``COPY_FREE_FLOOR``); any other database through a read-only URI. A copy that a run stopped hard left
behind, the next run removes (``remove_old_copies``).
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

AGENTS: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "copilot": "Copilot CLI",
    "gemini": "Gemini CLI",
    "qwen": "Qwen Code",
    "hermes": "Hermes Agent",
}

# Keys of a record's envelope (and of the wrapper object holding its payload) that are bookkeeping, not
# conversation: ids, times, the folder. They are dropped there and nowhere deeper: in a tool's input or output
# the same words are what the tool was given or printed.
ID_KEYS = frozenset(
    {
        "id",
        "type",
        "role",
        "call_id",
        "callId",
        "callID",
        "toolCallId",
        "messageId",
        "interactionId",
        "tool_use_id",
        "toolUseId",
        "parentId",
        "status",
    }
)
# Opaque blobs of reasoning and thinking blocks.
OPAQUE_KEYS = frozenset({"signature", "encrypted_content", "encryptedContent", "reasoningOpaque", "thoughtSignature"})
ENVELOPE_KEYS = (
    ID_KEYS
    | OPAQUE_KEYS
    | frozenset(
        {
            "uuid",
            "parentUuid",
            "requestId",
            "sessionId",
            "session_id",
            "sessionID",
            "messageID",
            "parentID",
            "projectID",
            "timestamp",
            "startTime",
            "lastUpdated",
            "projectHash",
            "hash",
            "snapshot",
            "cwd",
            "directory",
            "directories",
            "model",
            "stop_reason",
            "stop_sequence",
        }
    )
)
# What holds a record's payload, and the lists of content blocks: the bookkeeping keys go from these, the rest
# (tool inputs, outputs, results, text) is walked whole.
WRAPPERS = frozenset({"message", "payload", "data", "functionCall", "functionResponse"})
CONTAINERS = frozenset({"content", "parts", "toolCalls", "toolRequests", "tool_calls", "summary", "thoughts", "output"})
BLOCK_KEYS = ID_KEYS | OPAQUE_KEYS | frozenset({"timestamp"})
# Keys of an agent's record envelope that hold what never reached the provider, dropped there for that agent alone.
# Claude Code's ``toolUseResult`` is a tool's result as its own screen shows it (an edit's whole ``originalFile`` and
# its patch, a read's file, a command's output again), beside the ``tool_result`` block of ``message.content`` that the
# request carries: read, it would count a text never sent as one that may have reached the provider. An attachment's
# ``rendered`` (the ``{"content": ...}`` messages it becomes in the request) is sent, and read.
LOCAL_ONLY_KEYS: dict[str, frozenset[str]] = {"claude": frozenset({"toolUseResult"})}
_ENVELOPES = {agent: ENVELOPE_KEYS | keys for agent, keys in LOCAL_ONLY_KEYS.items()}
# A ``--path`` file may be any agent's, a copy of Claude Code's sessions among them: no other format sends a key of
# those names, so its records drop every agent's.
_ENVELOPES["path"] = ENVELOPE_KEYS.union(*LOCAL_ONLY_KEYS.values())

DB_TIMEOUT = 2.0
# A private copy of a database is made only where this much of the disk stays free after it, on top of a tenth of its
# size: a scan never takes the last of a disk (Windows pages to C:, and a full one stops the machine).
COPY_FREE_FLOOR = 1024**3
# A database and its -wal are copied one after the other: a write between the two leaves a copy of two moments. Seen by
# their sizes and modification times, the copy is made again, this many times at most, before the database is skipped.
COPY_TRIES = 3
# The temporary folder each private copy is made in begins with this, in the system's temporary folder. A run stopped
# hard (killed, the computer switched off) leaves it there, a plain copy of OpenCode's or Hermes Agent's database:
# the next run removes those older than ``COPY_KEPT`` seconds (``remove_old_copies``), so that one a run under way in
# another console reads stays.
COPY_PREFIX = "latet-leak-"
COPY_KEPT = 12 * 3600
# The name tempfile gives such a folder: the prefix and eight of its letters, digits and `_`. The cleanup removes no
# other, so that a folder of the user's own named alike stays (`latet-leak-mine`, `latet-leak-backup-2026`).
COPY_NAME = re.compile(re.escape(COPY_PREFIX) + r"[a-z0-9_]{8}\Z")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_IN_STRING = 16 * 1024 * 1024
MAX_NESTING = 8
_LINE_SUFFIXES = frozenset({".jsonl", ".ndjson"})
_EXTRA_SUFFIXES = frozenset({".json", ".jsonl", ".ndjson"})
_FIRST = re.compile(r"\S")  # a string's first character that is not white space, as ``str.strip`` reads it


class Text(NamedTuple):
    """One string of a saved session, with the agent, the session, its project folder and the record's time. A tuple:
    a read of every session on a computer can make tens of millions, each at less than half a frozen dataclass's
    cost."""

    agent: str
    session: str
    folder: str | None
    time: datetime | None
    text: str


@dataclass(frozen=True, slots=True)
class Skipped:
    """Something that could not be read, and why (never a piece of its content)."""

    where: str
    why: str


class _Unreadable(Exception):
    pass


def json_strings(value: object, skip: frozenset[str]) -> Iterator[str]:
    """Every string leaf of ``value``, skipping the values of the keys in ``skip``.

    A string that parses as a JSON object or array is walked too (without ``skip``: a key such as ``signature``
    in the JSON a tool printed is conversation, not a record's own bookkeeping).
    """
    stack: list[tuple[object, frozenset[str], int]] = [(value, skip, 0)]
    while stack:
        item, keys, depth = stack.pop()
        if isinstance(item, str):
            # Its first character that is not white space, found without a copy of the string (``strip`` makes one).
            first = _FIRST.search(item)
            if first is None:
                continue
            yield item
            if first.group() in "{[" and depth < MAX_NESTING and len(item) <= MAX_JSON_IN_STRING:
                try:
                    parsed = json.loads(item)
                except (ValueError, RecursionError):
                    continue
                if isinstance(parsed, dict | list):
                    stack.append((parsed, frozenset(), depth + 1))
        elif isinstance(item, dict):
            children = [v for k, v in item.items() if k not in keys]
            stack.extend((child, keys, depth) for child in reversed(children))
        elif isinstance(item, list):
            stack.extend((child, keys, depth) for child in reversed(item))


def _strip(obj: dict[str, object], drop: frozenset[str], depth: int = 0) -> dict[str, object]:
    """``obj`` without its bookkeeping keys, and so are its wrapper objects and content blocks (not their insides)."""
    out: dict[str, object] = {}
    for key, value in obj.items():
        if key in drop:
            continue
        if depth < MAX_NESTING and key in WRAPPERS and isinstance(value, dict):
            out[key] = _strip(value, ENVELOPE_KEYS, depth + 1)
        elif depth < MAX_NESTING and key in CONTAINERS and isinstance(value, list):
            out[key] = [_strip(v, BLOCK_KEYS, depth + 1) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def _when(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(value, dict):
        for key in ("created", "start", "updated"):
            found = _when(value.get(key))
            if found:
                return found
    return None


@dataclass(slots=True)
class _State:
    session: str
    folder: str | None = None


def _meta(record: object, state: _State) -> datetime | None:
    """Update the session and folder from a record, and return the record's own time."""
    if not isinstance(record, dict):
        return None
    for key in ("sessionId", "session_id"):
        if isinstance(record.get(key), str) and record[key]:
            state.session = record[key]
    if isinstance(record.get("cwd"), str) and record["cwd"]:
        state.folder = record["cwd"]
    directories = record.get("directories")
    if state.folder is None and isinstance(directories, list) and directories and isinstance(directories[0], str):
        state.folder = directories[0]
    payload = record.get("payload")
    if record.get("type") == "session_meta" and isinstance(payload, dict):
        if isinstance(payload.get("id"), str):
            state.session = payload["id"]
        if isinstance(payload.get("cwd"), str):
            state.folder = payload["cwd"]
    data = record.get("data")
    if record.get("type") == "session.start" and isinstance(data, dict):
        if isinstance(data.get("sessionId"), str):
            state.session = data["sessionId"]
        context = data.get("context")
        if isinstance(context, dict) and isinstance(context.get("cwd"), str):
            state.folder = context["cwd"]
    for key in ("timestamp", "startTime", "time"):
        found = _when(record.get(key))
        if found:
            return found
    if isinstance(payload, dict):
        return _when(payload.get("timestamp"))
    return None


def _emit(
    agent: str,
    state: _State,
    record: object,
    when: datetime | None,
    since: datetime | None,
    mtime: datetime | None,
) -> Iterator[Text]:
    if since is not None:
        moment = when or mtime
        if moment is not None and moment < since:
            return
    if isinstance(record, dict):
        record = _strip(record, _ENVELOPES.get(agent, ENVELOPE_KEYS))
    for text in json_strings(record, frozenset()):
        yield Text(agent, state.session, state.folder, when, text)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _lines(path: Path) -> Iterator[object]:
    try:
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, RecursionError):
                    continue
    except OSError as exc:
        raise _Unreadable(f"could not be read ({exc.strerror or 'error'})") from exc


def _expand(document: object) -> Iterator[object]:
    """A whole-file document as records: a list's items, a chat's metadata then its messages, else itself."""
    if isinstance(document, list):
        yield from document
    elif isinstance(document, dict) and isinstance(document.get("messages"), list):
        yield {k: v for k, v in document.items() if k != "messages"}
        yield from document["messages"]
    else:
        yield document


def _records(path: Path) -> Iterator[object]:
    if path.suffix.lower() in _LINE_SUFFIXES:
        yield from _lines(path)
        return
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            raise _Unreadable(f"is {size} bytes, beyond the {MAX_JSON_BYTES} read from a whole JSON file")
        with path.open("rb") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise _Unreadable(f"could not be read ({exc.strerror or 'error'})") from exc
    except (ValueError, RecursionError):
        yield from _lines(path)
        return
    yield from _expand(document)


def _read_file(agent: str, path: Path, since: datetime | None, session: str | None = None) -> Iterator[Text | Skipped]:
    mtime = _mtime(path)
    if since is not None and mtime is not None and mtime < since:
        return
    state = _State(session=session or path.stem)
    try:
        for record in _records(path):
            when = _meta(record, state)
            yield from _emit(agent, state, record, when, since, mtime)
    except _Unreadable as exc:
        yield Skipped(str(path), str(exc))


def _files(base: Path, *patterns: str) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        try:
            found.update(p for p in base.glob(pattern) if p.is_file())
        except OSError:
            continue
    return sorted(found)


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def _read_claude(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = _env_dir("CLAUDE_CONFIG_DIR") or home / ".claude"
    for path in _files(base, "projects/*/*.jsonl", "projects/*/*/subagents/*.jsonl"):
        yield from _read_file("claude", path, since)


def _read_codex(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = _env_dir("CODEX_HOME") or home / ".codex"
    for path in _files(base, "sessions/**/*.jsonl", "archived_sessions/**/*.jsonl"):
        yield from _read_file("codex", path, since)


def _read_copilot(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = _env_dir("COPILOT_HOME") or home / ".copilot"
    for path in _files(base, "session-state/*/events.jsonl"):
        yield from _read_file("copilot", path, since, session=path.parent.name)


def _read_gemini(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = (_env_dir("GEMINI_CLI_HOME") or home) / ".gemini"
    for path in _files(base, "tmp/*/chats/**/*.jsonl", "tmp/*/chats/**/*.json"):
        yield from _read_file("gemini", path, since)


def _read_qwen(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = _env_dir("QWEN_HOME") or home / ".qwen"
    for path in _files(base, "projects/*/chats/**/*.jsonl", "tmp/*/chats/**/*.jsonl", "tmp/*/chats/**/*.json"):
        yield from _read_file("qwen", path, since)


def _is_wal(db: Path) -> bool:
    if db.with_name(db.name + "-wal").exists():
        return True
    try:
        with db.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def _states(*paths: Path) -> tuple[tuple[int, int] | None, ...]:
    """Each file's size and modification time, None where there is none."""
    found: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            found.append(None)
            continue
        found.append((stat.st_size, stat.st_mtime_ns))
    return tuple(found)


@contextmanager
def connect_read_only(db: Path) -> Iterator[sqlite3.Connection]:
    """A connection that reads ``db`` and writes nothing beside it (see the module docstring)."""
    if not _is_wal(db):
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=DB_TIMEOUT)
        try:
            yield connection
        finally:
            connection.close()
        return
    wal = db.with_name(db.name + "-wal")
    with tempfile.TemporaryDirectory(prefix=COPY_PREFIX) as folder:
        copy, copied_wal = Path(folder) / "snapshot.db", Path(folder) / "snapshot.db-wal"
        for _ in range(COPY_TRIES):
            before = _states(db, wal)
            needed = sum(state[0] for state in before if state is not None)
            if shutil.disk_usage(folder).free < needed * 1.1 + COPY_FREE_FLOOR:
                raise OSError(errno.ENOSPC, "not enough free space for a private copy")
            shutil.copyfile(db, copy)
            if before[1] is not None:
                try:
                    shutil.copyfile(wal, copied_wal)
                except FileNotFoundError:  # the last connection to it closed meanwhile: copied again
                    continue
            else:
                copied_wal.unlink(missing_ok=True)  # one a try before copied, the -wal gone since
            if _states(db, wal) == before:
                break
        else:
            raise OSError(errno.EBUSY, "it changed while it was copied, each time")
        connection = sqlite3.connect(copy, timeout=DB_TIMEOUT)
        try:
            yield connection
        finally:
            connection.close()


def remove_old_copies(folder: Path | None = None) -> None:
    """The private copies a run stopped hard left behind, removed: in ``folder`` (the system's temporary folder, where
    ``connect_read_only`` makes them), each folder of the name tempfile gave it (``COPY_NAME``) that was last changed
    more than ``COPY_KEPT`` seconds ago. Nothing else: no file, no other name, no link or junction, and nothing a link
    inside one leads to (``shutil.rmtree`` removes a link, never what it leads to). What cannot be removed (a copy in
    use) stays, without an error. Where tempfile finds no temporary folder it can write, it falls back on the current
    folder, the user's own: nothing is removed there; where it finds none at all, nothing is either."""
    if folder is not None:
        base = folder
    else:
        try:
            base = Path(tempfile.gettempdir())
            if base.resolve() == Path.cwd().resolve():
                return
        except OSError:
            return
    cutoff = time.time() - COPY_KEPT
    try:
        with os.scandir(base) as entries:
            found = [entry for entry in entries if COPY_NAME.match(entry.name)]
    except OSError:
        return
    for entry in found:
        try:
            if entry.is_symlink() or entry.is_junction() or not entry.is_dir(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _rows(connection: sqlite3.Connection, table: str, wanted: Sequence[str]) -> Iterator[dict[str, object]]:
    """The rows of ``table`` with those of the ``wanted`` columns it has."""
    present = _columns(connection, table)
    names = [c for c in wanted if c in present]
    if not names:
        return
    cursor = connection.execute(f"SELECT {', '.join(names)} FROM {table}")  # noqa: S608 - fixed names
    for row in cursor:
        yield dict(zip(names, row, strict=True))


def _decode(raw: object) -> object:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _database_error(db: Path, exc: Exception) -> Skipped:
    why = (
        exc.strerror
        if isinstance(exc, OSError) and exc.strerror
        else str(exc)
        if isinstance(exc, sqlite3.Error)
        else "error"
    )
    return Skipped(str(db), f"could not be read ({why})")


def _hermes_dbs(home: Path) -> list[Path]:
    explicit = _env_dir("HERMES_HOME")
    if explicit:
        return [explicit / "state.db"]
    candidates = []
    if sys.platform == "win32":
        local = _env_dir("LOCALAPPDATA") if home == Path.home() else None
        candidates.append((local or home / "AppData" / "Local") / "hermes" / "state.db")
    candidates.append(home / ".hermes" / "state.db")
    return [p for p in dict.fromkeys(candidates) if p.is_file()]


def _read_hermes(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    for db in _hermes_dbs(home):
        if not db.is_file():
            continue
        mtime = _mtime(db)
        try:
            with connect_read_only(db) as connection:
                connection.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
                states: dict[str, _State] = {}
                for row in _rows(connection, "sessions", ("id", "cwd", "title", "started_at")):
                    state = _State(str(row["id"]), _str_or_none(row.get("cwd")))
                    states[state.session] = state
                    if row.get("title"):
                        yield from _emit("hermes", state, str(row["title"]), _when(row.get("started_at")), since, mtime)
                wanted = ("session_id", "content", "tool_calls", "reasoning", "reasoning_content", "timestamp")
                for row in _rows(connection, "messages", wanted):
                    session = str(row["session_id"])
                    state = states.get(session) or _State(session)
                    record = {k: _decode(v) for k, v in row.items() if k not in ("session_id", "timestamp") and v}
                    calls = record.get("tool_calls")
                    if isinstance(calls, str):
                        with suppress(ValueError, RecursionError):
                            record["tool_calls"] = json.loads(calls)
                    yield from _emit("hermes", state, record, _when(row.get("timestamp")), since, mtime)
        except (sqlite3.Error, OSError) as exc:
            yield _database_error(db, exc)


def _opencode_base(home: Path) -> Path:
    return (_env_dir("XDG_DATA_HOME") or home / ".local" / "share") / "opencode"


def _read_opencode_db(db: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    mtime = _mtime(db)
    try:
        with connect_read_only(db) as connection:
            connection.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
            folders: dict[str, str | None] = {}
            for row in _rows(connection, "session", ("id", "directory", "title", "time_updated", "time_created")):
                directory = _str_or_none(row.get("directory"))
                state = _State(str(row["id"]), directory)
                folders[state.session] = directory
                if row.get("title"):
                    when = _when(row.get("time_updated")) or _when(row.get("time_created"))
                    yield from _emit("opencode", state, str(row["title"]), when, since, mtime)
            for table in ("message", "part"):
                for row in _rows(connection, table, ("session_id", "time_created", "data")):
                    data = row.get("data")
                    try:
                        record: object = json.loads(data) if isinstance(data, str) else data
                    except (ValueError, RecursionError):
                        record = data
                    session = str(row.get("session_id"))
                    state = _State(session, folders.get(session))
                    when = _when(row.get("time_created")) or (
                        _when(record.get("time")) if isinstance(record, dict) else None
                    )
                    yield from _emit("opencode", state, record, when, since, mtime)
    except (sqlite3.Error, OSError) as exc:
        yield _database_error(db, exc)


def _read_opencode_storage(storage: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    folders: dict[str, str | None] = {}
    for path in _files(storage, "session/**/*.json"):
        try:
            with path.open("rb") as handle:
                doc = json.load(handle)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("id"), str):
            directory = doc.get("directory")
            folders[doc["id"]] = directory if isinstance(directory, str) else None
    for path in _files(storage, "session/**/*.json", "message/**/*.json", "part/**/*.json"):
        mtime = _mtime(path)
        if since is not None and mtime is not None and mtime < since:
            continue
        try:
            if path.stat().st_size > MAX_JSON_BYTES:
                raise _Unreadable(
                    f"is {path.stat().st_size} bytes, beyond the {MAX_JSON_BYTES} read from a whole JSON file"
                )
            with path.open("rb") as handle:
                doc = json.load(handle)
        except _Unreadable as exc:
            yield Skipped(str(path), str(exc))
            continue
        except OSError as exc:
            yield Skipped(str(path), f"could not be read ({exc.strerror or 'error'})")
            continue
        except (ValueError, RecursionError):
            continue
        if not isinstance(doc, dict):
            continue
        in_session = path.relative_to(storage).parts[0] == "session"
        session = doc.get("sessionID") or (doc.get("id") if in_session else None) or path.stem
        state = _State(str(session), folders.get(str(session)))
        yield from _emit("opencode", state, doc, _when(doc.get("time")), since, mtime)


def _read_opencode(home: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    base = _opencode_base(home)
    for db in _files(base, "opencode*.db"):
        yield from _read_opencode_db(db, since)
    if (base / "storage").is_dir():
        yield from _read_opencode_storage(base / "storage", since)


_READERS = {
    "claude": _read_claude,
    "codex": _read_codex,
    "opencode": _read_opencode,
    "copilot": _read_copilot,
    "gemini": _read_gemini,
    "qwen": _read_qwen,
    "hermes": _read_hermes,
}


def _read_extra(path: Path, since: datetime | None) -> Iterator[Text | Skipped]:
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in _EXTRA_SUFFIXES)
    elif path.is_file():
        files = [path]
    else:
        yield Skipped(str(path), "does not exist")
        return
    for file in files:
        yield from _read_file("path", file, since)


def read(
    agents: Sequence[str] | None = None,
    extra: Sequence[Path] = (),
    since: datetime | None = None,
    home: Path | None = None,
) -> Iterator[Text | Skipped]:
    """Every string of the saved sessions of ``agents`` (default: all) and of the ``extra`` files or folders.

    ``since`` leaves out the records older than it (by the record's time, else the file's modification time).
    Something unreadable is yielded as a ``Skipped``, never raised; an unknown agent is a ``ValueError`` at once.
    """
    chosen = list(AGENTS if agents is None else agents)
    unknown = [a for a in chosen if a not in _READERS]
    if unknown:
        raise ValueError(f"unknown agent: {unknown[0]}")
    if since is not None and since.tzinfo is None:
        since = since.astimezone()
    return _read(chosen, list(extra), since, home if home is not None else Path.home())


def _read(agents: list[str], extra: list[Path], since: datetime | None, home: Path) -> Iterator[Text | Skipped]:
    for agent in agents:
        yield from _READERS[agent](home, since)
    for path in extra:
        yield from _read_extra(path, since)
