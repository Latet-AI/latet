"""The agents' saved sessions as texts, for ``latet leak``: every agent from a fixture in its own shape."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from latet.leak import sources
from latet.leak.sources import AGENTS, Skipped, Text, json_strings, read

TOKEN = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
HOME_VARIABLES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "COPILOT_HOME",
    "QWEN_HOME",
    "HERMES_HOME",
    "GEMINI_CLI_HOME",
    "XDG_DATA_HOME",
)


@pytest.fixture(autouse=True)
def _clean_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HOME_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _jsonl(path: Path, records: list[object], tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n" + tail).encode("utf-8"))


def _texts(items: object) -> list[Text]:
    return [i for i in items if isinstance(i, Text)]  # type: ignore[attr-defined]


def _strings(items: object) -> list[str]:
    return [t.text for t in _texts(items)]


def test_the_agents_are_listed_in_order() -> None:
    assert list(AGENTS) == ["claude", "codex", "opencode", "copilot", "gemini", "qwen", "hermes"]
    assert AGENTS["claude"] == "Claude Code"
    assert AGENTS["copilot"] == "Copilot CLI"


def test_claude_code_sessions_give_their_texts_with_folder_and_time(tmp_path: Path) -> None:
    project = tmp_path / ".claude" / "projects" / "C--dev-app"
    base = {"sessionId": "sess-1", "cwd": "C:\\dev\\app", "uuid": "uuid-aaa", "requestId": "req_bbb"}
    _jsonl(
        project / "sess-1.jsonl",
        [
            {
                **base,
                "type": "user",
                "timestamp": "2026-10-02T10:00:00.000Z",
                "message": {"role": "user", "content": "my token is " + TOKEN},
            },
            {
                **base,
                "type": "assistant",
                "timestamp": "2026-10-02T10:00:05.000Z",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "hmm", "signature": "SIGNATURE-BLOB"},
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo " + AWS}},
                    ]
                },
            },
            {
                **base,
                "type": "user",
                "timestamp": "2026-10-02T10:00:09.000Z",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "out: ok"}]},
                "toolUseResult": {"stdout": "stdout-text", "stderr": ""},
            },
        ],
    )
    _jsonl(
        project / "sess-1" / "subagents" / "agent-a1.jsonl",
        [{**base, "type": "user", "timestamp": "2026-10-02T10:01:00Z", "message": {"content": "sub " + TOKEN}}],
    )
    items = list(read(["claude"], home=tmp_path))
    texts = _texts(items)
    joined = "\n".join(t.text for t in texts)
    assert TOKEN in joined and AWS in joined and "out: ok" in joined
    assert "stdout-text" not in joined  # the tool's result for Claude Code's own screen: never sent
    assert all(t.agent == "claude" and t.session == "sess-1" and t.folder == "C:\\dev\\app" for t in texts)
    first = next(t for t in texts if t.text == "my token is " + TOKEN)
    assert first.time == datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    for opaque in ("SIGNATURE-BLOB", "uuid-aaa", "req_bbb"):
        assert opaque not in joined
    assert sum(1 for t in texts if t.text == "sub " + TOKEN) == 1


def _claude_tool_records(result: str, local: dict[str, object], rendered: str) -> list[object]:
    """A command's call, its result as the request carries it (a ``tool_result`` block in ``message.content``) and as
    Claude Code keeps it for its own screen (``toolUseResult``), and an attachment with the text it becomes in the
    request (``rendered``), as Claude Code 2.1 writes them."""
    base = {"sessionId": "sess-2", "cwd": "C:\\dev\\app", "timestamp": "2026-10-02T10:00:00.000Z"}
    return [
        {
            **base,
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": "Edit", "input": {"x": "y"}}]},
        },
        {
            **base,
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": result}]},
            "toolUseResult": local,
        },
        {
            **base,
            "type": "attachment",
            "attachment": {"type": "hook_additional_context", "content": ["hook said"]},
            "rendered": [{"content": rendered}],
            "renderedRole": "system",
        },
    ]


def test_claude_codes_copy_of_a_tool_result_for_its_screen_is_not_read(tmp_path: Path) -> None:
    """``toolUseResult`` is the tool's result as Claude Code shows it (an edit's whole ``originalFile``, a read's file, a
    command's output again), beside the ``tool_result`` block the request carries: it never reached the provider, and is
    not read. ``rendered``, an attachment's text as the request carries it, is."""
    records = _claude_tool_records(
        "The file was updated with " + TOKEN,
        {"originalFile": "old file with " + AWS, "structuredPatch": [{"lines": ["-" + AWS]}], "stdout": AWS},
        "the hook's context " + TOKEN,
    )
    _jsonl(tmp_path / ".claude" / "projects" / "C--dev-app" / "sess-2.jsonl", records)
    texts = _strings(read(["claude"], home=tmp_path))
    assert "The file was updated with " + TOKEN in texts and "the hook's context " + TOKEN in texts
    assert not any(AWS in t for t in texts)
    # The same key in a tool's input is read.
    nested: list[object] = [
        {"sessionId": "s", "message": {"content": [{"type": "tool_use", "input": {"toolUseResult": AWS}}]}}
    ]
    _jsonl(tmp_path / ".claude" / "projects" / "C--dev-app" / "sess-3.jsonl", nested)
    assert any(AWS in t for t in _strings(read(["claude"], home=tmp_path)))


def test_a_path_holding_claude_codes_sessions_is_read_as_claude_code_reads_them(tmp_path: Path) -> None:
    """``--path`` reads files of any agent's: Claude Code's copy of a tool's result for its own screen (``toolUseResult``)
    is not read there either, since no other format sends a key of that name; one in a tool's input still is."""
    records = _claude_tool_records("The file was updated with " + TOKEN, {"originalFile": "old file " + AWS}, "hook")
    records.append({"sessionId": "s", "message": {"content": [{"type": "tool_use", "input": {"toolUseResult": "in"}}]}})
    _jsonl(tmp_path / "dump" / "a.jsonl", records)
    texts = _strings(read([], extra=[tmp_path / "dump"], home=tmp_path / "nohome"))
    assert "The file was updated with " + TOKEN in texts and "in" in texts
    assert not any(AWS in t for t in texts)


def test_codex_sessions_take_the_folder_from_session_meta(tmp_path: Path) -> None:
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "03"
    _jsonl(
        day / "rollout-2026-10-03T08-00-00-abc.jsonl",
        [
            {
                "timestamp": "2026-10-03T08:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": "cx-1", "cwd": "/home/u/proj", "timestamp": "2026-10-03T08:00:00Z"},
            },
            {
                "timestamp": "2026-10-03T08:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "key " + TOKEN}],
                },
            },
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "older line"}]},
        ],
    )
    _jsonl(
        tmp_path / ".codex" / "archived_sessions" / "rollout-old.jsonl",
        [
            {"timestamp": "2026-09-01T00:00:00Z", "type": "session_meta", "payload": {"id": "cx-0", "cwd": "/old"}},
            {"type": "event_msg", "payload": {"message": "archived"}},
        ],
    )
    texts = _texts(read(["codex"], home=tmp_path))
    mine = [t for t in texts if t.session == "cx-1"]
    assert any(TOKEN in t.text for t in mine)
    assert {t.folder for t in mine if TOKEN in t.text} == {"/home/u/proj"}
    assert any(t.text == "older line" and t.session == "cx-1" for t in texts)
    assert any(t.text == "archived" and t.session == "cx-0" and t.folder == "/old" for t in texts)


def _opencode_db(path: Path, wal: bool = False) -> sqlite3.Connection | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
    )
    con.execute("INSERT INTO session VALUES ('ses_1', 'p1', 'D:\\work\\site', 'A title', 1790000000000, 1790000000000)")
    con.execute(
        "INSERT INTO message VALUES ('msg_1', 'ses_1', 1790000000000, 1790000000000, ?)",
        (json.dumps({"role": "user"}),),
    )
    con.execute(
        "INSERT INTO part VALUES ('prt_1', 'msg_1', 'ses_1', 1790000001000, 1790000001000, ?)",
        (json.dumps({"type": "text", "text": "use " + TOKEN}),),
    )
    con.execute(
        "INSERT INTO part VALUES ('prt_2', 'msg_1', 'ses_1', 1790000002000, 1790000002000, ?)",
        (json.dumps({"type": "tool", "callID": "call_9", "state": {"input": {"command": "x"}, "output": "tool-out"}}),),
    )
    con.commit()
    if wal:
        return con
    con.close()
    return None


def test_opencode_reads_its_database_read_only(tmp_path: Path) -> None:
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    _opencode_db(db)
    before = db.read_bytes()
    texts = _texts(read(["opencode"], home=tmp_path))
    assert any(TOKEN in t.text for t in texts)
    assert any(t.text == "tool-out" for t in texts)
    assert "call_9" not in _strings(texts)
    assert {t.session for t in texts} == {"ses_1"}
    assert {t.folder for t in texts} == {"D:\\work\\site"}
    first = next(t for t in texts if TOKEN in t.text)
    assert first.time == datetime.fromtimestamp(1790000001, tz=UTC)
    assert db.read_bytes() == before
    assert not list(db.parent.glob("*-wal")) and not list(db.parent.glob("*-journal"))


def test_opencode_reads_its_older_json_storage(tmp_path: Path) -> None:
    storage = tmp_path / ".local" / "share" / "opencode" / "storage"
    for rel, doc in {
        "session/proj1/ses_old.json": {"id": "ses_old", "directory": "/srv/app", "time": {"created": 1780000000000}},
        "message/ses_old/msg_1.json": {
            "id": "msg_1",
            "sessionID": "ses_old",
            "role": "user",
            "time": {"created": 1780000001000},
        },
        "part/msg_1/prt_1.json": {
            "id": "prt_1",
            "sessionID": "ses_old",
            "messageID": "msg_1",
            "type": "text",
            "text": "old " + TOKEN,
        },
    }.items():
        (storage / rel).parent.mkdir(parents=True, exist_ok=True)
        (storage / rel).write_text(json.dumps(doc), encoding="utf-8")
    texts = _texts(read(["opencode"], home=tmp_path))
    hit = next(t for t in texts if TOKEN in t.text)
    assert hit.session == "ses_old" and hit.folder == "/srv/app"


def test_copilot_cli_events_take_the_folder_from_session_start(tmp_path: Path) -> None:
    _jsonl(
        tmp_path / ".copilot" / "session-state" / "11111111-aaaa" / "events.jsonl",
        [
            {
                "type": "session.start",
                "id": "e1",
                "timestamp": "2026-10-05T09:00:00.000Z",
                "parentId": None,
                "data": {"sessionId": "cop-1", "context": {"cwd": "/w/copilot"}},
            },
            {
                "type": "user.message",
                "id": "e2",
                "timestamp": "2026-10-05T09:00:01.000Z",
                "parentId": "e1",
                "data": {"content": "pw " + TOKEN},
            },
            {
                "type": "assistant.message",
                "id": "e3",
                "timestamp": "2026-10-05T09:00:02.000Z",
                "parentId": "e2",
                "data": {"content": "ok", "toolRequests": [{"name": "bash", "arguments": {"command": "echo hi"}}]},
            },
        ],
    )
    texts = _texts(read(["copilot"], home=tmp_path))
    assert {t.session for t in texts} == {"cop-1"}
    assert {t.folder for t in texts if t.text == "pw " + TOKEN} == {"/w/copilot"}
    assert any(t.text == "echo hi" for t in texts)
    assert "e2" not in _strings(texts)


def test_gemini_and_qwen_chats_are_read(tmp_path: Path) -> None:
    # Gemini CLI v0.68.0: ~/.gemini/tmp/<project>/chats/session-*.jsonl, a metadata line then messages
    chats = tmp_path / ".gemini" / "tmp" / "abc123" / "chats"
    _jsonl(
        chats / "session-2026-10-06T10-00-aaaa.jsonl",
        [
            {
                "sessionId": "gem-1",
                "projectHash": "HASH-XYZ",
                "startTime": "2026-10-06T10:00:00.000Z",
                "lastUpdated": "2026-10-06T10:00:00.000Z",
                "directories": ["/w/gem"],
            },
            {
                "id": "m1",
                "timestamp": "2026-10-06T10:00:01.000Z",
                "type": "user",
                "content": [{"text": "key " + TOKEN}],
            },
            {
                "id": "m2",
                "timestamp": "2026-10-06T10:00:02.000Z",
                "type": "gemini",
                "content": "fine",
                "toolCalls": [{"id": "t1", "name": "shell", "args": {"command": "ls"}, "result": "listing"}],
            },
        ],
    )
    # the older whole-file .json
    (chats / "session-old.json").write_text(
        json.dumps(
            {
                "sessionId": "gem-0",
                "projectHash": "H",
                "startTime": "2026-09-01T00:00:00Z",
                "messages": [
                    {"id": "x", "timestamp": "2026-09-01T00:00:01Z", "type": "user", "content": "legacy " + AWS}
                ],
            }
        ),
        encoding="utf-8",
    )
    gem = _texts(read(["gemini"], home=tmp_path))
    hit = next(t for t in gem if TOKEN in t.text)
    assert hit.agent == "gemini" and hit.session == "gem-1" and hit.folder == "/w/gem"
    assert hit.time == datetime(2026, 10, 6, 10, 0, 1, tzinfo=UTC)
    assert any(t.text == "listing" for t in gem)
    assert "HASH-XYZ" not in _strings(gem)
    assert any(AWS in t.text and t.session == "gem-0" for t in gem)

    # Qwen Code v0.27.3: ~/.qwen/projects/<cwd>/chats/<sessionId>.jsonl
    _jsonl(
        tmp_path / ".qwen" / "projects" / "-w-qwen" / "chats" / "qw-1.jsonl",
        [
            {
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "qw-1",
                "timestamp": "2026-10-07T11:00:00.000Z",
                "type": "user",
                "cwd": "/w/qwen",
                "version": "0.27.3",
                "message": {"role": "user", "parts": [{"text": "q " + TOKEN}]},
            },
            {
                "uuid": "u2",
                "parentUuid": "u1",
                "sessionId": "qw-1",
                "timestamp": "2026-10-07T11:00:03.000Z",
                "type": "tool_result",
                "cwd": "/w/qwen",
                "toolCallResult": {"resultDisplay": "shown"},
            },
        ],
    )
    qw = _texts(read(["qwen"], home=tmp_path))
    hit = next(t for t in qw if TOKEN in t.text)
    assert hit.agent == "qwen" and hit.session == "qw-1" and hit.folder == "/w/qwen"
    assert any(t.text == "shown" for t in qw) and "u1" not in _strings(qw)


def test_an_agents_folder_moved_by_its_variable_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moved = tmp_path / "elsewhere" / "claude"
    _jsonl(moved / "projects" / "p" / "s.jsonl", [{"sessionId": "mv", "message": {"content": "moved " + TOKEN}}])
    codex = tmp_path / "elsewhere" / "codex"
    _jsonl(
        codex / "sessions" / "2026" / "rollout-x.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "cx", "cwd": "/c"}},
            {"type": "event_msg", "payload": {"message": "codex moved"}},
        ],
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    texts = _texts(read(["claude", "codex"], home=home))
    assert any(t.agent == "claude" and TOKEN in t.text for t in texts)
    assert any(t.agent == "codex" and t.text == "codex moved" for t in texts)


def test_extra_paths_read_any_json_or_json_lines(tmp_path: Path) -> None:
    folder = tmp_path / "dump"
    _jsonl(folder / "a.jsonl", [{"note": "one " + TOKEN}, {"note": "two"}])
    (folder / "b.json").write_text(json.dumps({"deep": {"list": ["three", {"k": "four"}]}}), encoding="utf-8")
    (folder / "c.txt").write_text("not json " + TOKEN, encoding="utf-8")
    lone = tmp_path / "lone.json"
    lone.write_text(json.dumps([{"a": "five"}, {"a": "six"}]), encoding="utf-8")
    items = list(read([], extra=[folder, lone, tmp_path / "missing.json"], home=tmp_path / "nohome"))
    found = _strings(items)
    for want in ("one " + TOKEN, "two", "three", "four", "five", "six"):
        assert want in found
    assert not any("not json" in s for s in found)
    assert any(isinstance(i, Skipped) and "missing.json" in i.where for i in items)


def test_since_leaves_out_older_records(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "p" / "s.jsonl"
    _jsonl(
        path,
        [
            {"sessionId": "s", "timestamp": "2026-10-01T00:00:00Z", "message": {"content": "old record"}},
            {"sessionId": "s", "timestamp": "2026-11-01T00:00:00Z", "message": {"content": "new record"}},
            {"sessionId": "s", "message": {"content": "undated record"}},
        ],
    )
    since = datetime(2026, 10, 15, tzinfo=UTC)
    found = _strings(read(["claude"], home=tmp_path, since=since))
    assert "new record" in found and "old record" not in found
    # undated: the file's modification time decides
    assert "undated record" in found  # modified now

    september = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    os.utime(path, (september, september))
    assert _strings(read(["claude"], home=tmp_path, since=since)) == []


def test_a_truncated_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "p" / "s.jsonl"
    _jsonl(
        path,
        [{"sessionId": "s", "message": {"content": "whole"}}],
        tail='{"sessionId": "s", "message": {"content": "cut off ' + TOKEN,
    )
    items = list(read(["claude"], home=tmp_path))
    found = _strings(items)
    assert found == ["whole"]
    assert not [i for i in items if isinstance(i, Skipped)]


def test_a_locked_database_is_skipped_and_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    _opencode_db(db)
    monkeypatch.setattr(sources, "DB_TIMEOUT", 0.1)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        items = list(read(["opencode"], home=tmp_path))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not _texts(items)
    skipped = [i for i in items if isinstance(i, Skipped)]
    assert len(skipped) == 1 and skipped[0].where == str(db) and "locked" in skipped[0].why


def test_a_large_file_is_read_line_by_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".claude" / "projects" / "p" / "big.jsonl"
    _jsonl(path, [{"sessionId": "b", "message": {"content": f"line {i}"}} for i in range(50)])
    real_open = pathlib.Path.open

    class Guard:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def read(self, size: int = -1) -> bytes:
            if size is None or size < 0:
                raise AssertionError("read() with no size")
            return self.inner.read(size)  # type: ignore[attr-defined]

        def readlines(self, *args: object) -> list[bytes]:
            raise AssertionError("readlines()")

        def __iter__(self):
            return iter(self.inner)  # type: ignore[call-overload]

        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()  # type: ignore[attr-defined]

        def close(self) -> None:
            self.inner.close()  # type: ignore[attr-defined]

    def guarded(self: Path, *args: object, **kwargs: object):
        handle = real_open(self, *args, **kwargs)  # type: ignore[arg-type]
        return Guard(handle) if self.name == "big.jsonl" else handle

    monkeypatch.setattr(pathlib.Path, "open", guarded)
    assert len(_strings(read(["claude"], home=tmp_path))) == 50


def test_json_inside_a_string_is_walked() -> None:
    inner = json.dumps({"headers": {"Authorization": "Bearer " + TOKEN}, "list": ["deep " + AWS]})
    out = list(json_strings({"output": inner, "signature": "skip-me", "n": 3, "ok": True}, frozenset({"signature"})))
    assert inner in out
    assert "Bearer " + TOKEN in out and "deep " + AWS in out
    assert "skip-me" not in out
    # a skipped key is skipped only in the record's own structure, not in JSON a tool printed
    inside = list(json_strings({"output": json.dumps({"signature": "kept " + TOKEN})}, frozenset({"signature"})))
    assert "kept " + TOKEN in inside
    assert list(json_strings("plain {not json", frozenset())) == ["plain {not json"]


def _hermes_db(path: Path, wal: bool = False) -> sqlite3.Connection | None:
    """Hermes Agent v0.23.1's ``state.db``: the columns of ``sessions`` and ``messages`` that carry text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, cwd TEXT, title TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,"
        " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, reasoning TEXT, message_uid TEXT);"
    )
    con.execute("INSERT INTO sessions VALUES ('hs_1', 'cli', 1790000000.5, '/w/hermes', 'Hermes title')")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES ('hs_1', 'user', ?, 1790000001.5, 'UID-1')",
        ("hermes " + TOKEN,),
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, reasoning)"
        " VALUES ('hs_1', 'assistant', 'calling', ?, 'terminal', 1790000002.5, 'thinking about it')",
        (
            json.dumps(
                [
                    {
                        "id": "call_h1",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": "echo " + AWS})},
                    }
                ]
            ),
        ),
    )
    con.commit()
    if wal:
        return con
    con.close()
    return None


def test_hermes_agent_state_db_is_read(tmp_path: Path) -> None:
    _hermes_db(tmp_path / ".hermes" / "state.db")
    texts = _texts(read(["hermes"], home=tmp_path))
    found = _strings(texts)
    assert {t.session for t in texts} == {"hs_1"} and {t.folder for t in texts} == {"/w/hermes"}
    hit = next(t for t in texts if t.text == "hermes " + TOKEN)
    assert hit.agent == "hermes" and hit.time == datetime.fromtimestamp(1790000001.5, tz=UTC)
    assert any(AWS in s for s in found)  # inside the tool call's arguments, a JSON string in a JSON string
    assert "thinking about it" in found and "Hermes title" in found
    assert "UID-1" not in found and "call_h1" not in found


def test_hermes_home_variable_moves_its_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moved = tmp_path / "elsewhere"
    _hermes_db(moved / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(moved))
    assert any(TOKEN in s for s in _strings(read(["hermes"], home=tmp_path / "nohome")))


def _listing(folder: Path) -> list[tuple[str, int, int]]:
    return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir())


def test_a_wal_database_is_read_without_a_write_in_its_folder(tmp_path: Path) -> None:
    # a WAL database whose -wal and -shm are gone (the last connection checkpointed): a read-only open would
    # create them beside it
    for name, build, rel in (
        ("opencode", _opencode_db, Path(".local/share/opencode/opencode.db")),
        ("hermes", _hermes_db, Path(".hermes/state.db")),
    ):
        db = tmp_path / rel
        keeper = build(db, wal=True)
        assert keeper is not None
        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        keeper.close()
        for extra in db.parent.glob("*-wal"):
            extra.unlink(missing_ok=True)
        for extra in db.parent.glob("*-shm"):
            extra.unlink(missing_ok=True)
        before = _listing(db.parent)
        assert any(TOKEN in s for s in _strings(read([name], home=tmp_path)))
        assert _listing(db.parent) == before


def test_a_live_wal_is_read_from_a_private_copy(tmp_path: Path) -> None:
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    keeper = _opencode_db(db, wal=True)
    assert keeper is not None
    try:
        # the rows are still in the -wal file, which the database file alone does not hold
        assert (db.parent / "opencode.db-wal").stat().st_size > 0
        before = _listing(db.parent)
        assert any(TOKEN in s for s in _strings(read(["opencode"], home=tmp_path)))
        assert _listing(db.parent) == before
    finally:
        keeper.close()


def _closed_wal_db(tmp_path: Path) -> Path:
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    keeper = _opencode_db(db, wal=True)
    assert keeper is not None
    keeper.close()
    return db


def test_a_database_that_changes_while_it_is_copied_is_copied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database and its -wal are copied one after the other: a write between the two (its agent's, Latet's daemon)
    would leave a copy of two moments. Seen by their sizes and times, the copy is made again."""
    db = _closed_wal_db(tmp_path)
    real = shutil.copyfile
    copies: list[str] = []

    def copied(source, target):
        done = real(source, target)
        copies.append(Path(source).name)
        if len(copies) == 1:  # its agent writes right after the database is copied
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO part VALUES ('prt_3', 'msg_1', 'ses_1', 1790000003000, 1790000003000, ?)",
                (json.dumps({"type": "text", "text": "written meanwhile"}),),
            )
            con.commit()
            con.close()
        return done

    monkeypatch.setattr(sources.shutil, "copyfile", copied)
    items = list(read(["opencode"], home=tmp_path))
    assert "written meanwhile" in _strings(items)
    assert copies.count("opencode.db") == 2
    assert not [i for i in items if isinstance(i, Skipped)]


def test_a_database_that_keeps_changing_while_it_is_copied_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _closed_wal_db(tmp_path)
    real = shutil.copyfile

    def copied(source, target):
        done = real(source, target)
        later = db.stat().st_mtime_ns + 1_000_000_000
        os.utime(db, ns=(later, later))
        return done

    monkeypatch.setattr(sources.shutil, "copyfile", copied)
    items = list(read(["opencode"], home=tmp_path))
    assert not _texts(items)
    [skipped] = [i for i in items if isinstance(i, Skipped)]
    assert skipped.where == str(db) and "changed while it was copied" in skipped.why


def test_a_copy_leaves_room_on_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A private copy is made only where at least ``COPY_FREE_FLOOR`` stays free after it: never the last of a disk."""
    db = _closed_wal_db(tmp_path)
    usage = shutil.disk_usage(tmp_path)
    room = int(db.stat().st_size * 1.1) + sources.COPY_FREE_FLOOR // 2
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda _: usage._replace(free=room))
    items = list(read(["opencode"], home=tmp_path))
    assert not _texts(items)
    [skipped] = [i for i in items if isinstance(i, Skipped)]
    assert "not enough free space" in skipped.why


def test_bookkeeping_keys_are_read_where_they_are_conversation(tmp_path: Path) -> None:
    secret_a, secret_b, secret_c, secret_d = (f"{TOKEN}-{n}" for n in "abcd")
    _jsonl(
        tmp_path / ".claude" / "projects" / "p" / "s.jsonl",
        [
            {
                "sessionId": "s",
                "type": "user",
                "uuid": "u-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01ABC",
                            "name": "Write",
                            "input": {
                                "directory": secret_a,
                                "cwd": secret_c,
                                "result": {"snapshot": secret_b, "timestamp": secret_d, "signature": "result " + TOKEN},
                            },
                        }
                    ],
                },
            },
            {
                "sessionId": "s",
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01ABC",
                            "content": [{"type": "text", "text": "listing", "signature": "SIG-BLOB"}],
                        }
                    ]
                },
            },
        ],
    )
    found = _strings(read(["claude"], home=tmp_path))
    for secret in (secret_a, secret_b, secret_c, secret_d, "result " + TOKEN):
        assert secret in found
    assert "SIG-BLOB" not in found
    # the same keys at the envelope level are bookkeeping
    _jsonl(
        tmp_path / ".local" / "share" / "opencode" / "storage" / "part" / "m" / "p.json",
        [],
    )
    storage = tmp_path / ".local" / "share" / "opencode" / "storage" / "part" / "m" / "p.json"
    storage.write_text(
        json.dumps(
            {
                "id": "prt_9",
                "sessionID": "o",
                "snapshot": "TREE-HASH",
                "type": "tool",
                "state": {"output": {"snapshot": secret_a}},
            }
        ),
        encoding="utf-8",
    )
    found = _strings(read(["opencode"], home=tmp_path))
    assert "TREE-HASH" not in found and secret_a in found


def test_ids_and_types_of_records_and_blocks_are_not_texts(tmp_path: Path) -> None:
    _jsonl(
        tmp_path / ".claude" / "projects" / "p" / "s.jsonl",
        [
            {
                "sessionId": "s",
                "type": "assistant",
                "message": {
                    "id": "msg_01XYZ",
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash", "input": {"command": "ls"}},
                    ],
                },
            },
            {
                "sessionId": "s",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_01ABC", "content": "fine"}],
                },
            },
        ],
    )
    _jsonl(
        tmp_path / ".codex" / "sessions" / "r.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "cx"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "id": "fc_77",
                    "call_id": "call_ZZ",
                    "name": "shell",
                    "arguments": json.dumps({"command": "pwd"}),
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call_ZZ", "output": "out"},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        ],
    )
    _jsonl(
        tmp_path / ".copilot" / "session-state" / "c" / "events.jsonl",
        [
            {
                "type": "assistant.message",
                "id": "ev_1",
                "data": {
                    "messageId": "mid_5",
                    "content": "ok",
                    "toolRequests": [
                        {"toolCallId": "tc_9", "name": "bash", "type": "function", "arguments": {"command": "date"}}
                    ],
                },
            },
            {
                "type": "tool.execution_complete",
                "id": "ev_2",
                "data": {"toolCallId": "tc_9", "result": {"content": "Mon"}},
            },
        ],
    )
    _jsonl(
        tmp_path / ".gemini" / "tmp" / "h" / "chats" / "session-x.jsonl",
        [
            {"sessionId": "g", "projectHash": "H"},
            {
                "id": "gm_1",
                "type": "gemini",
                "content": "k",
                "toolCalls": [
                    {
                        "id": "gt_3",
                        "name": "shell",
                        "status": "success",
                        "args": {"command": "uname"},
                        "result": "Linux",
                    }
                ],
            },
        ],
    )
    found = set(_strings(read(["claude", "codex", "copilot", "gemini"], home=tmp_path)))
    for noise in (
        "msg_01XYZ", "toolu_01ABC", "fc_77", "call_ZZ", "tc_9", "mid_5", "ev_1", "gm_1", "gt_3", "success",
        "assistant", "user", "tool_use", "tool_result", "function_call", "function_call_output", "input_text", "function", "text", "claude-x",
    ):  # fmt: skip
        assert noise not in found
    for text in ("hello", "ls", "fine", "pwd", "out", "hi", "ok", "date", "Mon", "uname", "Linux", "bash", "Bash"):
        assert text in found or any(text in f for f in found)


def test_copilot_home_variable_moves_its_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moved = tmp_path / "elsewhere"
    _jsonl(
        moved / "session-state" / "c1" / "events.jsonl", [{"type": "user.message", "data": {"content": "cop " + TOKEN}}]
    )
    monkeypatch.setenv("COPILOT_HOME", str(moved))
    texts = _texts(read(["copilot"], home=tmp_path / "nohome"))
    assert any(t.text == "cop " + TOKEN and t.session == "c1" for t in texts)


def test_a_whole_json_file_beyond_the_cap_is_skipped_with_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"note": "x" * 5000}), encoding="utf-8")
    monkeypatch.setattr(sources, "MAX_JSON_BYTES", 1000)
    items = list(read([], extra=[big], home=tmp_path))
    assert not _texts(items)
    [skipped] = [i for i in items if isinstance(i, Skipped)]
    assert skipped.where == str(big) and str(big.stat().st_size) in skipped.why


def test_a_session_title_is_filtered_by_since_like_the_rest(tmp_path: Path) -> None:
    _opencode_db(tmp_path / ".local" / "share" / "opencode" / "opencode.db")
    since = datetime.fromtimestamp(1790000000 + 86400, tz=UTC)
    assert _strings(read(["opencode"], home=tmp_path, since=since)) == []
    assert "A title" in _strings(read(["opencode"], home=tmp_path, since=datetime.fromtimestamp(1789000000, tz=UTC)))


def test_an_unknown_agent_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nope"):
        read(["nope"], home=tmp_path)


def test_a_skipped_never_carries_content(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"note": "' + TOKEN, encoding="utf-8")
    garbage = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    garbage.parent.mkdir(parents=True)
    garbage.write_bytes(b"not a database " + TOKEN.encode() * 40)
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"k": TOKEN}) + " " * 100, encoding="utf-8")
    items = list(read(["opencode"], extra=[broken, big], home=tmp_path))
    skipped = [i for i in items if isinstance(i, Skipped)]
    assert any(s.where == str(garbage) for s in skipped)
    for item in skipped:
        assert TOKEN not in item.where and TOKEN not in item.why


def _old(path: Path, hours: float) -> None:
    then = datetime.now(UTC).timestamp() - hours * 3600
    os.utime(path, (then, then))


def _link(link: Path, target: Path) -> bool:
    """A link to a folder: a symbolic link where this computer lets one be made, else on Windows a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        if sys.platform != "win32":
            return False
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # pyright: ignore[reportAttributeAccessIssue]
    return True


def test_private_copies_a_hard_stop_left_are_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run killed hard leaves its private copy (a plain copy of OpenCode's or Hermes Agent's database): the next run
    removes the folders of leak's own prefix older than 12 hours, in the temporary folder, and nothing else: a fresh
    one (a run under way in another console), a file, another prefix, a link (and never what a link leads to)."""
    temp = tmp_path / "temp"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("the user's", encoding="utf-8")
    old = temp / "latet-leak-old_0001"  # the names tempfile gives: the prefix and eight of its characters
    old.mkdir(parents=True)
    (old / "snapshot.db").write_bytes(b"copy")
    linked = _link(old / "inside", outside)
    fresh = temp / "latet-leak-new_0001"
    fresh.mkdir()
    (fresh / "snapshot.db").write_bytes(b"copy")
    others = [
        temp / "latet-leaky",
        temp / "other-latet-leak-x",
        temp / "Latet-Leak-case",
        temp / "latet-leak-mine",  # a folder of the user's own, of another shape than tempfile's
        temp / "latet-leak-backup-2026",
    ]
    for other in others:
        other.mkdir()
    afile = temp / "latet-leak-file0001"
    afile.write_bytes(b"not a folder")
    top_link = temp / "latet-leak-lnk_0001"
    top_linked = _link(top_link, outside)
    for path in [old, *others, afile, outside]:
        _old(path, 13)
    _old(fresh, 11)
    sources.remove_old_copies(temp)
    assert not old.exists()
    assert fresh.is_dir() and (fresh / "snapshot.db").exists()
    assert all(other.is_dir() for other in others) and afile.is_file()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "the user's"  # the link inside not followed
    assert linked
    monkeypatch.setattr(sources, "COPY_KEPT", -3600)  # every folder old now: what keeps the link is that it is one
    sources.remove_old_copies(temp)
    assert not fresh.exists()
    if top_linked:
        assert os.path.lexists(top_link)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "the user's"
    sources.remove_old_copies(tmp_path / "missing")  # no such folder: nothing to do, never an error


def test_no_temporary_folder_removes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where no temporary folder can be written, tempfile falls back on the current folder, the user's own: nothing
    is removed there; and where it finds none at all, the command goes on."""
    import tempfile

    here = tmp_path / "work"
    copy = here / "latet-leak-abc_1234"
    copy.mkdir(parents=True)
    _old(copy, 13)
    monkeypatch.chdir(here)
    monkeypatch.setattr(tempfile, "gettempdir", os.getcwd)
    sources.remove_old_copies()
    assert copy.is_dir()

    def none() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(tempfile, "gettempdir", none)
    sources.remove_old_copies()


def test_a_private_copy_is_made_in_a_folder_of_its_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    db = _closed_wal_db(tmp_path)
    seen: list[str] = []
    real = shutil.copyfile

    def copyfile(src, dst, *a, **k):  # type: ignore[no-untyped-def]
        seen.append(Path(dst).parent.name)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copyfile", copyfile)
    with sources.connect_read_only(db) as connection:
        connection.execute("SELECT 1")
    assert seen and all(sources.COPY_NAME.match(name) for name in seen)  # the shape the cleanup removes
    assert sources.COPY_PREFIX == "latet-leak-"


def test_since_reads_a_naive_date_in_this_computers_zone(tmp_path: Path) -> None:
    """``--since 2026-10-03`` is midnight on this computer's clock, as the dates the command shows: a record at half
    past midnight is read, one at half past eleven the evening before is not."""
    late = datetime(2026, 10, 2, 23, 30).astimezone().astimezone(UTC).isoformat()
    early = datetime(2026, 10, 3, 0, 30).astimezone().astimezone(UTC).isoformat()
    _jsonl(
        tmp_path / ".claude" / "projects" / "p" / "s.jsonl",
        [
            {"sessionId": "s", "timestamp": late, "message": {"content": "the evening before"}},
            {"sessionId": "s", "timestamp": early, "message": {"content": "after midnight"}},
        ],
    )
    found = _strings(read(["claude"], home=tmp_path, since=datetime(2026, 10, 3)))
    assert found == ["after midnight"]
