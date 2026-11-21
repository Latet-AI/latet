"""A request Latet only checks (Claude Code's), stopped by a value in what a tool's definition says of its input: the
refusal names the tool and the part, and says what to do, since Latet's plugin masks a tool's description in Claude
Code and cannot reach its parameters' schema. Claude Code sends an MCP tool's schema once the tool is loaded, and every
tool's after an error, so an address in it stops every such request until it is allowed or the server is off."""

from __future__ import annotations

import copy
import json

from latet.gateway.mediate import TOOL_INPUT_WORDS

from .test_request_claude_code_body import ANTHROPIC, TOOL, claude_code_body
from .test_what_leaves import CLAUDE_CODE, EMAIL, PHONE, _mediator

HEADERS = {**CLAUDE_CODE, "x-api-key": "sk"}
PATH = "/anthropic/v1/messages"
BASE = f"sensitive values would leave in clear: EMAIL[{len(EMAIL)}]"


def _tool(name: str, *, schema_text: str | None = None, example: str | None = None, description: str = "") -> dict:
    tool = copy.deepcopy(TOOL)
    tool["name"] = name
    tool["description"] = description or "Finds a contact."
    if schema_text is not None:
        tool["input_schema"]["properties"]["file_path"]["description"] = schema_text
    if example is not None:
        tool["input_examples"] = [{"file_path": example}]
    return tool


def _reason(
    *tools: dict, user: str = "write the release notes", headers: dict | None = None, **members: object
) -> str | None:
    body = claude_code_body()
    body["tools"] = [*body["tools"], *tools]
    body["messages"][0]["content"][0]["text"] = user
    body.update(members)
    prepared = _mediator().prepare(ANTHROPIC, "POST", PATH, headers or HEADERS, json.dumps(body).encode())
    return prepared.block_reason


def _hint(it: str, where: str, gives: str) -> str:
    verb = "is" if it == "it" else "are"
    return (
        f"; {it} {verb} in {where}, {TOOL_INPUT_WORDS}: if {it} {verb} not sensitive, choose Not sensitive in the "
        f"dashboard (latet dashboard); otherwise turn off {gives}"
    )


ONE_TOOL = "the MCP server or plugin that gives this tool"


def test_a_value_in_a_tool_s_parameter_schema_names_the_tool_and_what_to_do():
    reason = _reason(_tool("mcp__crm__find", schema_text=f"For example {EMAIL}"))
    assert reason == BASE + _hint("it", "the parameter schema of the tool mcp__crm__find", ONE_TOOL)


def test_a_value_in_a_tool_s_input_examples_names_them():
    reason = _reason(_tool("mcp__crm__find", example=EMAIL))
    assert reason == BASE + _hint("it", "the input examples of the tool mcp__crm__find", ONE_TOOL)


def test_values_in_two_tools_name_both():
    reason = _reason(
        _tool("mcp__crm__find", schema_text=f"For example {EMAIL}"),
        _tool("mcp__crm__call", schema_text=f"Dial {PHONE}"),
    )
    assert reason is not None
    assert reason.endswith(
        _hint(
            "they",
            "the parameter schema of the tool mcp__crm__call and the parameter schema of the tool mcp__crm__find",
            "the MCP servers or plugins that give these tools",
        )
    )


def test_a_name_in_another_form_is_not_repeated():
    """A refusal repeats a tool's name only in the form providers take one: anything else is "a tool"."""
    reason = _reason(_tool("find contacts <b>", schema_text=f"For example {EMAIL}"))
    assert reason == BASE + _hint("it", "the parameter schema of a tool", ONE_TOOL)


def test_a_value_in_a_tool_s_description_keeps_the_plain_refusal():
    """The plugin masks a tool's description in Claude Code: a value in clear there has the plain refusal's remedy."""
    assert _reason(_tool("mcp__crm__find", description=f"Mails {EMAIL}")) == BASE


def test_a_value_elsewhere_too_keeps_the_plain_refusal():
    """With a value outside every tool's input, the remedy the hint gives would not be enough: none is added."""
    reason = _reason(_tool("mcp__crm__find", schema_text=f"For example {EMAIL}"), user=f"call {PHONE}")
    assert reason is not None
    assert reason.startswith("sensitive values would leave in clear: ")
    assert TOOL_INPUT_WORDS not in reason


def test_one_value_written_twice_in_a_schema_is_one():
    tool = _tool("mcp__crm__find", schema_text=f"For example {EMAIL}")
    tool["input_schema"]["properties"]["file_path"]["examples"] = [EMAIL]
    assert _reason(tool) == BASE + _hint("it", "the parameter schema of the tool mcp__crm__find", ONE_TOOL)


def test_the_same_text_elsewhere_in_the_body_keeps_the_plain_refusal():
    """Each text of a tool's schema stands for one place: the same address in a member of the request no profile names
    is somewhere else, which turning the server off would not take away (the review's probe)."""
    tool = _tool("mcp__crm__find")
    tool["input_schema"]["properties"]["file_path"]["examples"] = [EMAIL]
    assert _reason(tool) == BASE + _hint("it", "the parameter schema of the tool mcp__crm__find", ONE_TOOL)
    reason = _reason(tool, x_note=EMAIL)
    assert reason is not None
    assert reason.startswith(BASE)
    assert TOOL_INPUT_WORDS not in reason


def test_a_number_in_a_schema_names_the_schema():
    """The body's coverage checks a schema's numbers as written: one that stops the request is in the schema too."""
    tool = _tool("mcp__pay__charge")
    tool["input_schema"]["properties"]["file_path"]["default"] = 4111111111111111
    reason = _reason(tool)
    assert reason is not None
    assert reason.endswith(_hint("it", "the parameter schema of the tool mcp__pay__charge", ONE_TOOL))


def test_a_tool_added_in_the_conversation_is_named_from_its_definition():
    body_tool = _tool("mcp__crm__lookup", schema_text=f"For example {EMAIL}")
    body = claude_code_body()
    body["messages"][1]["content"][1]["tool"]["definition"] = body_tool
    prepared = _mediator().prepare(ANTHROPIC, "POST", PATH, HEADERS, json.dumps(body).encode())
    assert prepared.block_reason == BASE + _hint("it", "the parameter schema of the tool mcp__crm__lookup", ONE_TOOL)


def test_an_answer_format_s_schema_keeps_the_plain_refusal():
    """The schema of the answer's format is no tool's: no MCP server or plugin gives it."""
    reason = _reason(
        output_config={"effort": "xhigh", "format": {"type": "json_schema", "schema": {"examples": [EMAIL]}}}
    )
    assert reason is not None
    assert reason.startswith(BASE)
    assert TOOL_INPUT_WORDS not in reason


def test_a_client_other_than_claude_code_keeps_the_plain_refusal():
    """A subscription's sign-in from another client is only checked too, but it has no MCP servers of Claude Code's to
    turn off: the plain refusal."""
    headers = {"content-type": "application/json", "authorization": "Bearer sk-ant-oat01-not-a-token"}
    body = claude_code_body()
    body["system"] = [{"type": "text", "text": "You are a helpful assistant."}]
    body["tools"].append(_tool("mcp__crm__find", schema_text=f"For example {EMAIL}"))
    prepared = _mediator().prepare(ANTHROPIC, "POST", PATH, headers, json.dumps(body).encode())
    assert prepared.block_reason == BASE


def test_the_dashboard_reads_these_words_and_these_parts():
    """The dashboard tells this refusal by its words (``blocks.ts``: TOOL_INPUT) and names a block by its key
    (``labels.ts``: TOOL_SCHEMA, TOOL_EXAMPLES): the same as the gateway's."""
    from pathlib import Path

    from latet.gateway.mediate import _TOOL_INPUT_PARTS

    lib = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "lib"
    assert f"const TOOL_INPUT = '{TOOL_INPUT_WORDS}'" in (lib / "blocks.ts").read_text(encoding="utf-8")
    labels = (lib / "labels.ts").read_text(encoding="utf-8")
    schema = sorted(key for key, part in _TOOL_INPUT_PARTS.items() if part == "parameter schema")
    assert schema == ["format", "input_schema", "parameters"]
    assert r"const TOOL_SCHEMA = /\.(?:input_schema|parameters|format)$/" in labels
    assert [key for key, part in _TOOL_INPUT_PARTS.items() if part == "input examples"] == ["input_examples"]
    assert r"const TOOL_EXAMPLES = /\.input_examples$/" in labels
