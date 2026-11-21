"""The daily check of the agents' requests (`.github/workflows/agent-drift.yml`), its report
(`scripts/agent_drift/report.py`): each member no profile declares named by its JSON path, read as the gateway reads
the request (its route, its protocol, Claude Code's checked only); a body the inspection refuses all the same listed by
its category; and a run that crashed, hung, sent no request or left its scripted turn unfinished failed, saying so.

The bodies are synthetic, shaped as the drift fixtures' are (`tests/request_qualification.py`): never a person's."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "agent_drift_report", Path(__file__).resolve().parents[2] / "scripts" / "agent_drift" / "report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
report = importlib.util.module_from_spec(_SPEC)
sys.modules["agent_drift_report"] = report  # its dataclasses look their module up there
_SPEC.loader.exec_module(report)

SESSION = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
CLAUDE_CODE = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.330 (external, sdk-cli)",
    "x-claude-code-session-id": SESSION,
    "x-app": "cli",
}
CODEX = {
    "accept": "text/event-stream",
    "content-type": "application/json",
    "originator": "codex_exec",
    "user-agent": "codex_exec/0.170.0 (Ubuntu 24.04; x86_64) dumb",
    "session-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f",
}
TOOLU = "toolu_01" + "Ab3" * 7
CALL = "call_" + "x7Q" * 8
BASH = {
    "name": "Bash",
    "description": "Runs a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
        "required": ["command"],
    },
}
EXEC = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in a shell.",
    "strict": False,
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string", "description": "The command to run."}},
        "required": ["cmd"],
        "additionalProperties": False,
    },
}


def claude_code_body(*, caller: bool = False) -> dict[str, Any]:
    """Claude Code's second request of the scripted turn: its call of Bash, and the result."""
    call: dict[str, Any] = {
        "type": "tool_use",
        "id": TOOLU,
        "name": "Bash",
        "input": {"command": "echo latet-canary", "description": "Print a word"},
    }
    if caller:
        call["caller"] = {"type": "direct"}
    return {
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Run the command, then say done."}]},
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": TOOLU, "content": "latet-canary"}]},
        ],
        "system": [
            {"type": "text", "text": "You are a coding agent."},
            {"type": "text", "text": "Answer briefly.", "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [BASH],
        "metadata": {"user_id": "user_9f2c4e1a_account__session_0b7d"},
        "max_tokens": 32000,
        "thinking": {"type": "adaptive"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "xhigh"},
        "stream": True,
    }


def codex_body(**extra: Any) -> dict[str, Any]:
    """Codex's second request of the scripted turn: its call of exec_command, and the output."""
    return {
        "model": "gpt-5.2-codex",
        "instructions": "You are a coding agent.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run the command."}]},
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "echo latet-canary"}),
                "call_id": CALL,
            },
            {"type": "function_call_output", "call_id": CALL, "output": "latet-canary"},
        ],
        "tools": [EXEC],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"summary": "auto"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f",
        **extra,
    }


def test_a_member_no_profile_declares_is_named_by_its_json_path():
    assert report.undeclared(claude_code_body(caller=True), "anthropic", headers=CLAUDE_CODE) == [
        "$.messages[1].content[0].caller"
    ]


def test_a_body_of_declared_members_alone_names_none():
    assert report.undeclared(claude_code_body(), "anthropic", headers=CLAUDE_CODE) == []
    assert report.undeclared(codex_body(), "responses", headers=CODEX) == []


def test_a_request_is_read_as_the_gateway_reads_it():
    """Its route and protocol from its path, Claude Code's checked only (a string member is D6's, read as text, and
    named all the same), Codex's rewritten (the same member read by the new rule)."""
    claude = claude_code_body() | {"note": "a text"}
    reading = report.read(claude, "anthropic", headers=CLAUDE_CODE)
    assert reading.paths == ("$.note",) and reading.checked_only and reading.refused is None
    codex = codex_body(drift={"on": True})
    codex["input"][0]["kind-of"] = "x"
    reading = report.read(codex, "responses", path="/openai/v1/responses", headers=CODEX)
    assert reading.paths == ('$.input[0]["kind-of"]', "$.drift") and not reading.checked_only
    assert reading.refused is None


def test_a_body_the_inspection_refuses_is_listed_by_its_category():
    """A member a dedicated reader refuses never reaches the walk's new rule: its body is refused all the same."""
    reading = report.read(codex_body(stream_options={"include_usage": "yes"}), "responses", headers=CODEX)
    assert reading.refused == "body-stream_options"


def _request(body: Any, *, answered: str, path: str = "/anthropic/v1/messages", headers=CLAUDE_CODE) -> dict:
    request = {"method": "POST", "path": path, "query": "beta=true", "headers": headers, "credentials": ["x-api-key"]}
    return {"request": request | {"body": body}, "answered": answered}


def _record(folder: Path, name: str, *entries: dict) -> Path:
    path = folder / f"{name}.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _first() -> dict[str, Any]:
    body = claude_code_body()
    body["messages"] = body["messages"][:1]
    return body


def _turn(second: dict[str, Any]) -> tuple[dict, ...]:
    return (
        _request(_first(), answered="tool call"),
        _request(second, answered="final text"),
        {"agent": {"exit": 0}},
    )


def test_the_cli_fails_and_writes_the_paths_to_the_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    record = _record(tmp_path, "claude-latest", *_turn(claude_code_body(caller=True)))
    assert report.main([str(record)]) == 1
    written = summary.read_text(encoding="utf-8")
    assert "`caller` at `$.messages[1].content[0].caller`, in 1 of 2 requests" in written
    assert "claude-latest" in written and "claude-cli/2.1.330" in written


def test_the_cli_passes_a_run_of_declared_members_alone(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    record = _record(tmp_path, "claude-latest", *_turn(claude_code_body()))
    assert report.main([str(record)]) == 0
    written = summary.read_text(encoding="utf-8")
    assert "2 requests, each read with no member no profile declares" in written


def test_the_cli_lists_a_refused_request_by_its_category(tmp_path):
    summary = tmp_path / "summary.md"
    first = codex_body()
    first["input"] = first["input"][:1]
    entries = (
        _request(first, answered="tool call", path="/openai/v1/responses", headers=CODEX),
        _request(
            codex_body(stream_options={"include_usage": "yes"}),
            answered="final text",
            path="/openai/v1/responses",
            headers=CODEX,
        ),
        {"agent": {"exit": 0}},
    )
    record = _record(tmp_path, "codex-latest", *entries)
    assert report.main(["--summary", str(summary), str(record)]) == 1
    assert "request 2, POST /openai/v1/responses: refused (body-stream_options)" in summary.read_text(encoding="utf-8")


SILENT = [
    pytest.param((), "no record of it: the run did not start", id="no-record"),
    pytest.param(({"agent": {"exit": 0}},), "it sent no request", id="no-request"),
    pytest.param(({"agent": {"timeout": 300}},), "it hung: stopped after 300 s", id="hung"),
    pytest.param((*_turn(claude_code_body())[:2], {"agent": {"exit": 3}}), "it exited with status 3", id="crash"),
    pytest.param(
        ({"agent": {"error": "FileNotFoundError"}},), "it did not start (FileNotFoundError)", id="not-started"
    ),
    pytest.param(_turn(claude_code_body())[:2], "its end was not recorded", id="no-end"),
    pytest.param(
        (_request(_first(), answered="text"), {"agent": {"exit": 0}}),
        "it called no shell tool: the stand-in found none it knows in its requests",
        id="no-tool-call",
    ),
    pytest.param(
        (_request(_first(), answered="tool call"), {"agent": {"exit": 0}}),
        "no request brought the tool's result back",
        id="no-result",
    ),
]


@pytest.mark.parametrize(("entries", "words"), SILENT)
def test_a_run_that_saw_nothing_fails_saying_why(tmp_path, entries, words):
    """A canary that silently sees nothing is worse than none: a run that crashed, hung or sent no request fails."""
    summary = tmp_path / "summary.md"
    record = _record(tmp_path, "codex-latest", *entries) if entries else tmp_path / "codex-latest.jsonl"
    assert report.main(["--summary", str(summary), str(record)]) == 1
    written = summary.read_text(encoding="utf-8")
    assert words in written, written


def test_a_record_cut_short_fails_saying_so(tmp_path):
    summary = tmp_path / "summary.md"
    record = _record(tmp_path, "claude-latest", *_turn(claude_code_body()))
    record.write_text(record.read_text(encoding="utf-8") + '{"request": {"meth', encoding="utf-8")
    assert report.main(["--summary", str(summary), str(record)]) == 1
    assert "its record holds a line that is no entry of the stand-in's" in summary.read_text(encoding="utf-8")


def test_no_record_named_fails(tmp_path):
    summary = tmp_path / "summary.md"
    assert report.main(["--summary", str(summary)]) == 1
    assert "no run was recorded" in summary.read_text(encoding="utf-8")


def test_an_odd_member_name_is_quoted_in_its_path():
    assert report.json_path(("messages", 0, "a`b", "c d", "ok_1")) == '$.messages[0]["a\\u0060b"]["c d"].ok_1'


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "agent-drift.yml"


def test_the_workflow_runs_daily_and_by_hand_reads_only_and_holds_no_secret():
    """Never on a pull request, its token read-only, no secret; each agent run through its stand-in, at the port its
    base URL names, with a timeout of its own; the agents' nonessential traffic off."""
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}  # YAML 1.1 reads the key `on` as True
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "pull_request" not in text
    # The records stay on the runner: they hold the agents' own prompts and tools, never uploaded.
    assert "upload-artifact" not in text
    job = workflow["jobs"]["agent-drift"]
    assert job["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    runs = [step for step in job["steps"] if "stand_in.py" in step.get("run", "")]
    assert len(runs) == 3
    for step in runs:
        found = re.search(r"--port (\d+)", step["run"])
        assert found is not None and step["timeout-minutes"] and "--timeout 300" in step["run"], step["name"]
        # Never unless every step before the agents succeeded, the providers' hosts shut off among them.
        assert "steps.ready.outcome == 'success'" in step["if"], step["name"]
        base = step["env"].get("ANTHROPIC_BASE_URL", "")
        assert base == f"http://127.0.0.1:{found[1]}/anthropic" or (
            "CODEX_HOME" in step["env"] and f'base_url = "http://127.0.0.1:{found[1]}/openai/v1"' in text
        ), step["name"]
    assert job["steps"][0] == {"uses": "actions/checkout@v4", "with": {"persist-credentials": False}}
    # No cache of uv's: the job runs the agents' newest code from npm on the default branch, and other workflows of the
    # repository would restore what it saved.
    [setup] = [step for step in job["steps"] if step.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup.get("with", {}).get("enable-cache") is False
    # The step the agents wait for runs only when every step before it succeeded (no `if` of its own), and the step
    # that shuts the providers' hosts off is among them.
    names = [step.get("name") for step in job["steps"]]
    ready = next(index for index, step in enumerate(job["steps"]) if step.get("id") == "ready")
    assert "if" not in job["steps"][ready] and names.index("No provider reachable from this runner") < ready
