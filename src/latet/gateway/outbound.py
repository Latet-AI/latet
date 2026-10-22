"""Tools that run outside this computer, given values Latet replaced.

The model only knows the stand-ins Latet puts in place of real values. A tool that runs on this
computer gets the real values back (the Claude Code plugin restores them, the daemon restores the
model's tool calls). A tool that runs elsewhere does not: a web search, a page fetched, a claude.ai
connector, a tool the provider runs itself (its web search, a remote MCP server). Given a stand-in,
it searches for someone else, opens the wrong page, or writes to an address that does not exist.
Sending the real value instead would let it leave this computer.

So such a call is stopped where it can be (the Claude Code plugin asks before the tool runs), and
said where it cannot (the provider ran it already: the next request carries it in its history).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from latet.engine.masker import Masker
from latet.engine.types import SlotKind
from latet.protocols import gemini
from latet.protocols.base import SlotRef, string_leaves

# Anthropic content blocks and Responses items of tools the provider runs.
_SERVER_BLOCKS = {"server_tool_use", "mcp_tool_use"}
_SERVER_ITEMS = {"web_search_call": "web_search", "mcp_call": "mcp"}

# What a tool would do with a stand-in, as told to the model and shown in the dashboard.
_WOULD = {
    "WebSearch": "search for someone or something else",
    "web_search": "search for someone or something else",
    "WebFetch": "open the wrong page",
    "web_fetch": "open the wrong page",
}
_WOULD_OTHERWISE = "work on a person, address or item that does not exist: a message could go to the wrong address"


def stand_ins(masker: Masker, values: Sequence[str]) -> list[str]:
    """The types of the stand-ins in ``values``, one per occurrence."""
    return [span.etype for value in values for span in masker.surrogates_in(value)]


def reason(tool: str, types: Sequence[str]) -> str:
    """What the model is told when a call is stopped: why, and what to ask the user."""
    kinds = ", ".join(sorted(set(types)))
    what = f"a value ({kinds})" if len(types) == 1 else f"values ({kinds})"
    return (
        f"Latet stopped this {tool} call. It runs outside the user's computer, and it contains {what} that "
        f"Latet replaced with a stand-in before you saw it, so it would {_WOULD.get(tool, _WOULD_OTHERWISE)}. "
        "Tell the user, and ask whether the real value may leave their computer. If it may, they mark it "
        "Not sensitive in the Latet dashboard (this call is shown there), and you can try again."
    )


def server_calls(protocol: str | None, body: Any) -> Iterator[tuple[str, list[SlotRef]]]:
    """The tools the provider ran in this conversation, each with the strings of its input."""
    if not isinstance(body, dict):
        return
    if protocol == "anthropic":
        messages = body.get("messages")
        for message in messages if isinstance(messages, list) else []:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and isinstance(block.get("type"), str) and block["type"] in _SERVER_BLOCKS:
                    name = str(block.get("name") or block["type"])
                    yield name, list(string_leaves(block.get("input") or {}, SlotKind.OUTBOUND_TOOL, True))
    elif protocol == "responses":
        items = body.get("input")
        for item in items if isinstance(items, list) else []:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "web_search_call":
                yield "web_search", list(string_leaves(item.get("action") or {}, SlotKind.OUTBOUND_TOOL, True))
            elif kind == "mcp_call" and isinstance(item.get("arguments"), str):
                name = str(item.get("name") or _SERVER_ITEMS[kind])
                yield name, [SlotRef(item, "arguments", SlotKind.OUTBOUND_TOOL, True, mode="json")]
    elif protocol == "gemini":  # Google's own tools: its search, a page's context, its maps
        yield from gemini.server_calls(body)
