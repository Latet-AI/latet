"""The daily check's stand-in of the providers (`scripts/agent_drift/stand_in.py`): it streams a scripted turn, a call
of a harmless shell command and then a final text, on Anthropic's Messages API and OpenAI's Responses API, in the forms
the agents read; it records each request without a credential's value, in the form the report reads; and it runs the
agent, recording how it ended (its status, or that it hung or never started). The requests are synthetic."""

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_agent_drift_report import BASH, CLAUDE_CODE, CODEX, EXEC, claude_code_body, codex_body, report

_SPEC = importlib.util.spec_from_file_location(
    "agent_drift_stand_in", Path(__file__).resolve().parents[2] / "scripts" / "agent_drift" / "stand_in.py"
)
assert _SPEC is not None and _SPEC.loader is not None
stand_in = importlib.util.module_from_spec(_SPEC)
sys.modules["agent_drift_stand_in"] = stand_in
_SPEC.loader.exec_module(stand_in)

ANTHROPIC_KEY = "sk-ant-api03-drift-stand-in-not-a-key"
OPENAI_KEY = "sk-drift-stand-in-not-a-key"


@pytest.fixture
def served(tmp_path) -> Iterator[tuple[int, Path, Any]]:
    path = tmp_path / "run.jsonl"
    recorder = stand_in.Recorder(path)
    server = stand_in.serve(0, recorder)
    try:
        yield server.server_address[1], path, recorder
    finally:
        server.shutdown()
        server.server_close()
        recorder.close()


def _post(port: int, target: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, str, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", target, json.dumps(body).encode(), headers)
        answer = connection.getresponse()
        return answer.status, answer.getheader("content-type") or "", answer.read().decode()
    finally:
        connection.close()


def _events(stream: str) -> list[tuple[str, dict[str, Any]]]:
    """An SSE stream's events, each by its name and its data, which names its type again."""
    events = []
    for block in stream.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        data = json.loads(lines["data"])
        assert data["type"] == lines["event"]
        events.append((lines["event"], data))
    return events


def _first_claude() -> dict[str, Any]:
    body = claude_code_body()
    body["messages"] = body["messages"][:1]
    return body


def _first_codex() -> dict[str, Any]:
    body = codex_body()
    body["input"] = body["input"][:1]
    return body


def test_the_messages_stand_in_streams_a_call_of_bash_then_a_final_text(served):
    port, _, _ = served
    headers = CLAUDE_CODE | {"x-api-key": ANTHROPIC_KEY}
    status, kind, stream = _post(port, "/anthropic/v1/messages?beta=true", _first_claude(), headers)
    assert status == 200 and kind.startswith("text/event-stream")
    events = _events(stream)
    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    message = events[0][1]["message"]
    assert message["role"] == "assistant" and message["model"] == "claude-sonnet-4-5"
    assert message["id"].startswith("msg_") and message["usage"]["input_tokens"] > 0
    call = events[1][1]["content_block"]
    assert call["type"] == "tool_use" and call["name"] == "Bash" and call["id"].startswith("toolu_")
    delta = events[2][1]["delta"]
    assert delta["type"] == "input_json_delta"
    assert json.loads(delta["partial_json"]) == {"command": "echo latet-canary", "description": "Print a word"}
    assert events[4][1]["delta"]["stop_reason"] == "tool_use"

    _, _, stream = _post(port, "/anthropic/v1/messages?beta=true", claude_code_body(), headers)
    events = _events(stream)
    assert events[1][1]["content_block"] == {"type": "text", "text": ""}
    assert events[2][1]["delta"] == {"type": "text_delta", "text": stand_in.FINAL}
    assert events[4][1]["delta"]["stop_reason"] == "end_turn"


def test_the_messages_stand_in_answers_whole_when_not_asked_to_stream(served):
    port, _, _ = served
    status, kind, text = _post(port, "/anthropic/v1/messages", _first_claude() | {"stream": False}, CLAUDE_CODE)
    message = json.loads(text)
    assert status == 200 and kind.startswith("application/json")
    assert message["type"] == "message" and message["stop_reason"] == "tool_use"
    assert message["content"][0]["input"]["command"] == "echo latet-canary"
    status, _, text = _post(port, "/anthropic/v1/messages/count_tokens", _first_claude(), CLAUDE_CODE)
    assert status == 200 and json.loads(text)["input_tokens"] > 0


def test_the_responses_stand_in_streams_a_call_of_the_shell_then_a_final_text(served):
    port, _, _ = served
    headers = CODEX | {"authorization": f"Bearer {OPENAI_KEY}"}
    status, kind, stream = _post(port, "/openai/v1/responses", _first_codex(), headers)
    assert status == 200 and kind.startswith("text/event-stream")
    events = _events(stream)
    assert [name for name, _ in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [data["sequence_number"] for _, data in events] == list(range(7))
    assert events[2][1]["item"]["arguments"] == "" and events[2][1]["item"]["status"] == "in_progress"
    call = events[5][1]["item"]
    assert call["type"] == "function_call" and call["name"] == "exec_command" and call["status"] == "completed"
    assert call["call_id"].startswith("call_") and json.loads(call["arguments"]) == {"cmd": "echo latet-canary"}
    assert events[3][1]["delta"] == events[4][1]["arguments"] == call["arguments"]
    completed = events[6][1]["response"]
    assert completed["id"].startswith("resp_") and completed["status"] == "completed"
    assert completed["output"] == [call] and completed["usage"]["total_tokens"] > 0

    _, _, stream = _post(port, "/openai/v1/responses", codex_body(), headers)
    events = _events(stream)
    assert [name for name, _ in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    message = events[7][1]["item"]
    assert message["type"] == "message" and message["role"] == "assistant" and message["id"].startswith("msg_")
    assert message["content"] == [{"type": "output_text", "text": stand_in.FINAL, "annotations": []}]
    assert events[4][1]["delta"] == events[5][1]["text"] == stand_in.FINAL
    assert events[8][1]["response"]["output"] == [message]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (BASH, {"command": "echo latet-canary", "description": "Print a word"}),
        (EXEC, {"cmd": "echo latet-canary"}),
        (
            {"type": "function", "name": "shell", "parameters": {"properties": {"command": {"type": "array"}}}},
            {"command": ["echo", "latet-canary"]},
        ),
        (
            {
                "type": "function",
                "name": "shell_command",
                "parameters": {"properties": {"command": {"type": "string"}}},
            },
            {"command": "echo latet-canary"},
        ),
    ],
)
def test_the_command_is_given_as_the_tools_schema_asks(tool, arguments):
    found = stand_in.shell_tool([{"name": "Read", "input_schema": {}}, tool])
    assert found is not None and found[0] == tool["name"] and found[1] == arguments


def test_a_request_without_a_shell_tool_is_answered_with_text(served):
    port, path, _ = served
    body = _first_claude()
    del body["tools"]
    _, _, stream = _post(port, "/anthropic/v1/messages", body, CLAUDE_CODE)
    assert _events(stream)[1][1]["content_block"]["type"] == "text"
    assert json.loads(path.read_text(encoding="utf-8"))["answered"] == "text"


def test_each_request_is_recorded_without_a_credentials_value_as_the_report_reads_it(served):
    port, path, recorder = served
    claude = CLAUDE_CODE | {"x-api-key": ANTHROPIC_KEY}
    codex = CODEX | {"authorization": f"Bearer {OPENAI_KEY}"}
    _post(port, "/anthropic/v1/messages?beta=true", _first_claude(), claude)
    _post(port, "/anthropic/v1/messages?beta=true", claude_code_body(), claude)
    _post(port, "/openai/v1/responses", _first_codex(), codex)
    _post(port, "/openai/v1/responses", codex_body(), codex)
    recorder.write({"agent": {"exit": 0}})
    text = path.read_text(encoding="utf-8")
    assert ANTHROPIC_KEY not in text and OPENAI_KEY not in text
    entries = [json.loads(line) for line in text.splitlines()]
    assert [entry.get("answered") for entry in entries] == ["tool call", "final text", "tool call", "final text", None]
    first = entries[0]["request"]
    assert first["method"] == "POST" and first["path"] == "/anthropic/v1/messages" and first["query"] == "beta=true"
    assert first["credentials"] == ["x-api-key"] and "x-api-key" not in first["headers"]
    assert first["headers"]["x-claude-code-session-id"] == CLAUDE_CODE["x-claude-code-session-id"]
    assert first["body"] == _first_claude()
    assert entries[2]["request"]["credentials"] == ["authorization"]
    assert "authorization" not in entries[2]["request"]["headers"]
    assert report.main(["--summary", str(path.with_suffix(".md")), str(path)]) == 0


def test_another_call_is_recorded_and_answered_not_found(served):
    port, path, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/openai/v1/models")
        answer = connection.getresponse()
        assert answer.status == 404 and json.loads(answer.read())["error"]
    finally:
        connection.close()
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["request"]["method"] == "GET" and entry["request"]["body"] is None
    assert entry["answered"] == "not found"


def test_a_body_that_is_no_json_is_recorded_unreadable_and_the_report_fails_it(served):
    port, path, recorder = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", "/anthropic/v1/messages", b"{not json", CLAUDE_CODE)
        answer = connection.getresponse()
        assert answer.status == 400 and json.loads(answer.read())["error"]["type"] == "invalid_request_error"
    finally:
        connection.close()
    recorder.write({"agent": {"exit": 0}})
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["request"]["body"] is None and entry["request"]["unreadable"] == "JSONDecodeError"
    summary = path.with_suffix(".md")
    assert report.main(["--summary", str(summary), str(path)]) == 1
    assert "request 1, POST /anthropic/v1/messages: refused (unreadable body: JSONDecodeError)" in summary.read_text(
        encoding="utf-8"
    )


def test_an_agent_that_hangs_is_stopped_and_recorded(tmp_path):
    recorder = stand_in.Recorder(tmp_path / "run.jsonl")
    try:
        assert stand_in.supervise([sys.executable, "-c", "import time; time.sleep(60)"], 1, recorder) == 124
    finally:
        recorder.close()
    assert json.loads((tmp_path / "run.jsonl").read_text(encoding="utf-8")) == {"agent": {"timeout": 1}}


def test_an_agent_that_crashes_or_never_starts_is_recorded(tmp_path):
    recorder = stand_in.Recorder(tmp_path / "run.jsonl")
    try:
        assert stand_in.supervise([sys.executable, "-c", "raise SystemExit(3)"], 120, recorder) == 3
        assert stand_in.supervise([str(tmp_path / "no-such-agent")], 120, recorder) == 127
    finally:
        recorder.close()
    entries = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entries == [{"agent": {"exit": 3}}, {"agent": {"error": "FileNotFoundError"}}]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# An agent of a few lines: one request to the stand-in, at the port it is given, its answer read to the end.
AGENT = """
import http.client, json, sys
connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=30)
body = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}
connection.request("POST", "/anthropic/v1/messages", json.dumps(body), {"content-type": "application/json"})
assert connection.getresponse().read()
"""


def test_the_stand_in_serves_the_agent_it_runs_and_records_its_end(tmp_path):
    port = _free_port()
    path = tmp_path / "run.jsonl"
    command = [sys.executable, "-c", AGENT, str(port)]
    assert stand_in.main(["--port", str(port), "--record", str(path), "--timeout", "120", "--", *command]) == 0
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry.get("answered") for entry in entries] == ["text", None]
    assert entries[1] == {"agent": {"exit": 0}}
