"""Claude Code 2.1.323's request to Anthropic's Messages API, through P2-2's body coverage. The body's shape was captured
on 2026-11-13 by pointing Claude Code at a local stand-in for the API that kept member names and the values of a few
closed members, never a text; the texts here are invented.

- A ``role: "system"`` message in the conversation carries ``output_config: {"effort": ...}``: the effort from that
  turn on (the beta ``per-turn-control-2026-07-01``). Its content is text blocks and ``tool_addition`` blocks.
- ``context_management``: ``{"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}`` (the beta
  ``context-management-2025-06-27``).
- ``output_config.effort`` is ``xhigh`` unless ``--effort`` says otherwise (low, medium, high, xhigh, max).

Before 0.15.0 took them, each of the three refused every request of Claude Code's: nothing was sent.
"""

from __future__ import annotations

import copy
import json

import pytest

from latet.gateway.body_coverage import NEW_MEMBER_RULE, BodyRefused, Ledger, UnknownMembers, inspect_body
from latet.gateway.routes import DEFAULT_UPSTREAMS
from latet.protocols.base import BodyScan

from .test_what_leaves import EMAIL, _mediator

ANTHROPIC = next(route for route in DEFAULT_UPSTREAMS if route.name == "anthropic")
TOOL = {
    "name": "Read",
    "description": "Reads a file from the local filesystem.",
    "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
}


def claude_code_body(effort: str = "xhigh") -> dict:
    return {
        "model": "claude-opus-4-5",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "write the release notes"}]},
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "The tools changed.", "cache_control": {"type": "ephemeral"}},
                    {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": TOOL}},
                ],
                "output_config": {"effort": effort},
            },
        ],
        "system": [
            {"type": "text", "text": "You are Claude Code."},
            {"type": "text", "text": "Answer briefly.", "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [TOOL],
        "metadata": {"user_id": "user_9f2c4e1a_account__session_0b7d"},
        "max_tokens": 32000,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": effort},
        "stream": True,
    }


def _said(body: dict, *, verify: bool = True, unknown_members: UnknownMembers = "check") -> str | None:
    try:
        return inspect_body(
            body, "anthropic", ANTHROPIC, BodyScan(), verify, Ledger(), unknown_members=unknown_members
        ).problem
    except BodyRefused as error:
        return str(error)


def _read_by_the_new_rule(body: dict, *, verify: bool) -> bool:
    inspection = inspect_body(body, "anthropic", ANTHROPIC, BodyScan(), verify, Ledger())
    return inspection.problem is None and NEW_MEMBER_RULE in inspection.rules


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_claude_code_2_1_323_s_body_passes_at_every_effort(effort, verify):
    """Checked only, as Claude Code's is, and masked, as another agent's would be with the same members. Where Latet
    masks, ``thinking.display`` is not declared (Claude Code's own request reads it as text, checked)."""
    body = claude_code_body(effort)
    if not verify:
        del body["thinking"]["display"]
    assert _said(body, verify=verify) is None


@pytest.mark.parametrize("verify", [True, False])
def test_the_fallback_model_takes_the_effort_xhigh_too(verify):
    body = claude_code_body()
    del body["thinking"]["display"]
    body["fallbacks"] = [{"model": "claude-sonnet-4-5", "output_config": {"effort": "xhigh"}}]
    assert _said(body, verify=verify) is None


NOTE = "Write to mario.rossi@example.com"
EXTRA_TEXT = [
    ("context_management", "edits", 0, "note"),
    ("context_management", "note"),
    ("messages", 1, "output_config", "note"),
]


@pytest.mark.parametrize("path", EXTRA_TEXT)
def test_a_text_beside_the_new_members_is_checked_where_latet_only_checks(path):
    """On Claude Code's own request, a string member no profile declares is read as text and checked (D6), here as
    anywhere else: the final check sees it, so a value in it stops the request."""
    body = _with(path, NOTE)
    inspection = inspect_body(body, "anthropic", ANTHROPIC, BodyScan(), True, Ledger())
    assert inspection.problem is None and NOTE in inspection.checked


@pytest.mark.parametrize("path", EXTRA_TEXT)
def test_a_text_beside_the_new_members_is_refused_where_latet_masks(path):
    """Where Latet masks, the new rule reads the member whole (never masked) and the final check reads it as masking
    reads a person's text: an address in it stops the request (one of a reserved domain, as ``NOTE``'s, is no one's,
    here as in any text), and its refusal names neither the member nor what it holds."""
    body = _with(path, NOTE)
    del body["thinking"]["display"]
    inspection = inspect_body(body, "anthropic", ANTHROPIC, BodyScan(), False, Ledger())
    assert inspection.problem is None and NOTE in inspection.new_members and NEW_MEMBER_RULE in inspection.rules
    body = _with(path, f"Write to {EMAIL}")
    del body["thinking"]["display"]
    raw = json.dumps(body).encode()
    prepared = _mediator().prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", {"x-api-key": "sk"}, raw)
    assert prepared.block_reason == f"sensitive values would leave in clear: EMAIL[{len(EMAIL)}]"


@pytest.mark.parametrize("path", EXTRA_TEXT)
def test_strict_a_text_beside_the_new_members_is_refused_where_latet_masks(path):
    body = _with(path, NOTE)
    del body["thinking"]["display"]
    said = _said(body, verify=False, unknown_members="refuse")
    assert said is not None and "mario.rossi" not in said


def _with(path: tuple, value) -> dict:
    body = claude_code_body()
    holder = body
    for step in path[:-1]:
        holder = holder[step]
    holder[path[-1]] = copy.deepcopy(value)
    return body


# A declared member's value outside what Claude Code sends: refused by its profile, whatever reads new members.
OUTSIDE = [
    (("output_config", "effort"), "ultra"),
    (("messages", 1, "output_config", "effort"), "ultra"),
    (("messages", 1, "output_config", "effort"), "mario.rossi@example.com"),
    (("messages", 1, "output_config"), "high"),
    (("context_management",), {"edits": [{"type": "clear_tool_uses_20250919", "keep": "all"}]}),
    (("context_management",), {"edits": [{"type": "clear_thinking_20251015", "keep": "mario.rossi@example.com"}]}),
    (("context_management",), {"edits": [{"type": "clear_thinking_20251015", "keep": {"type": "thinking_turns"}}]}),
    (("context_management",), {"edits": {"type": "clear_thinking_20251015", "keep": "all"}}),
    (("context_management",), [{"type": "clear_thinking_20251015", "keep": "all"}]),
]
# A member no profile declares beside them, of control values: read by the new rule, refused as before it when it is
# not read (``unknown_members: refuse``).
ADDED = [
    (("messages", 1, "output_config"), {"effort": "high", "format": {"type": "json_object"}}),
    (("messages", 1, "output_config"), {"effort": "high", "budget": 4}),
    (("messages", 0, "output_config"), {"effort": "high"}),  # only a system message sets the effort
    (("context_management",), {"edits": [{"type": "clear_thinking_20251015", "keep": "all", "at": 3}]}),
    (("context_management",), {"edits": [], "trigger": {"type": "input_tokens"}}),
]


@pytest.mark.parametrize(("path", "value"), OUTSIDE)
@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("mode", ["check", "refuse"])
def test_a_member_outside_what_claude_code_sends_is_refused(path, value, verify, mode):
    body = _with(path, value)
    del body["thinking"]["display"]
    said = _said(body, verify=verify, unknown_members=mode)
    assert said is not None and "mario.rossi" not in said


@pytest.mark.parametrize(("path", "value"), ADDED)
@pytest.mark.parametrize("verify", [True, False])
def test_a_control_member_beside_what_claude_code_sends_is_read_by_the_new_rule(path, value, verify):
    body = _with(path, value)
    del body["thinking"]["display"]
    assert _read_by_the_new_rule(body, verify=verify)


@pytest.mark.parametrize(("path", "value"), ADDED)
@pytest.mark.parametrize("verify", [True, False])
def test_strict_a_member_beside_what_claude_code_sends_is_refused(path, value, verify):
    body = _with(path, value)
    del body["thinking"]["display"]
    said = _said(body, verify=verify, unknown_members="refuse")
    assert said is not None and said.startswith("request body refused (unknown member in")


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("role", ["user", "assistant", "developer", "tool"])
def test_only_a_system_message_sets_the_effort(role, verify):
    """On another message the effort is no member its profile declares: read by the new rule, never as the effort."""
    body = claude_code_body()
    del body["thinking"]["display"]
    body["messages"][1]["role"] = role
    assert _read_by_the_new_rule(body, verify=verify)
    assert _said(body, verify=verify, unknown_members="refuse") == "request body refused (unknown member in message)"


def test_the_effort_inside_a_message_is_anthropic_s_alone():
    """OpenAI's Chat Completions has no effort on a message: a system message there reads the member by the new rule,
    and refuses it where new members are refused."""
    openai = next(route for route in DEFAULT_UPSTREAMS if route.name == "openai")
    body = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": "Be brief.", "output_config": {}}]}
    inspection = inspect_body(body, "chat", openai, BodyScan(), False, Ledger())
    assert NEW_MEMBER_RULE in inspection.rules and inspection.new_members == ["output_config"]
    with pytest.raises(BodyRefused):
        inspect_body(body, "chat", openai, BodyScan(), False, Ledger(), unknown_members="refuse")
