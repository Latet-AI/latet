"""Anthropic Messages API (``/v1/messages``, ``/v1/messages/count_tokens``)."""

from __future__ import annotations

from typing import Any

from latet.engine.types import SlotKind
from latet.protocols.base import (
    BodyScan,
    ImageRef,
    SlotRef,
    came_from,
    definitions,
    metadata,
    output_format,
    string_leaves,
)

# System blocks Claude Code places first and the API reads by position: never touched.
_PINNED_SYSTEM_PREFIXES = ("x-anthropic-billing-header:", "You are Claude Code", "You are a Claude agent")
# Blocks the provider made, given back as it made them, and blocks that hold no content: passed as they came, on a
# server too. Each is one Claude Code knows and sends back (its own list of block kinds): the model's thinking, whole or
# redacted; the calls of the provider's own tools and their results, web search and fetch, code execution, tool search,
# the advisor, an MCP server the provider reaches; a compaction of the conversation and a model's fallback, which the
# provider writes; the model's words between its tool calls (connector text); a file's id (a container upload); a
# tool's name (a reference to it, a tool removed).
_PROVIDERS_BLOCKS = {
    "thinking",
    "redacted_thinking",
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "code_execution_tool_result",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
    "tool_search_tool_result",
    "advisor_tool_result",
    "mcp_tool_use",
    "mcp_tool_result",
    "mcp_tool_listing",
    "compaction",
    "connector_text",
    "fallback",
    "container_upload",
    "tool_reference",
    "tool_removal",
}
_AUTO_MODE_REVIEW = "You are a security monitor for autonomous AI coding agents"


def is_auto_mode_review(body: dict[str, Any]) -> bool:
    """Claude Code's auto mode reviewer: it is sent the user's identity, CLAUDE.md and the action to judge."""
    system = body.get("system")
    texts = [system] if isinstance(system, str) else [b.get("text", "") for b in system or [] if isinstance(b, dict)]
    return any(isinstance(t, str) and t.lstrip().startswith(_AUTO_MODE_REVIEW) for t in texts)


def is_claude_code(headers: dict[str, str], body: dict[str, Any] | None = None) -> bool:
    """Claude Code requests are signed by the client: Latet never rewrites them. Claude Code is known by what it adds
    to each request, its session id and its attribution block; not by its user agent, which other agents send too (Qwen
    Code, on its Anthropic provider, sends `claude-cli/` with its own version)."""
    if "x-claude-code-session-id" in headers:
        return True
    system = (body or {}).get("system")
    if isinstance(system, list) and system and isinstance(system[0], dict):
        return str(system[0].get("text", "")).startswith("x-anthropic-billing-header:")
    return False


def _blocks(
    content: Any,
    kind: SlotKind,
    from_model: bool,
    scan: BodyScan,
    names: dict[str, str] | None = None,
    tool: str | None = None,
    sources: dict[str, tuple[Any, Any]] | None = None,
) -> None:
    """The blocks of one message's content (or of a tool's result), each read by its kind, or passed as the provider
    made it (``_PROVIDERS_BLOCKS``); a block of any other kind, one that is not an object, and a read block's field in
    another form are named in ``scan.unreadable``: a server refuses them, a computer sends them as they came."""
    if not isinstance(content, list):
        if content is not None and not isinstance(content, str):
            scan.unreadable.append("content that is neither text nor blocks")
        return
    names = {} if names is None else names
    sources = {} if sources is None else sources
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            scan.unreadable.append("a block of content that is not an object")
            continue
        btype = block.get("type")
        if not isinstance(btype, str):
            scan.unreadable.append("a block of content of a kind Latet does not read")
        elif btype == "text":
            if isinstance(block.get("text"), str):
                scan.slots.append(SlotRef(block, "text", kind, from_model, tool=tool))
            else:
                scan.unreadable.append("a block's text that is not text")
        elif btype == "tool_use":
            given = block.get("input")
            if isinstance(given, dict | list):
                if isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                    names[block["id"]] = block["name"]  # its result, later, says which tool made it
                    sources[block["id"]] = (block, "input")  # and which file or address it came from
                scan.slots.extend(string_leaves(given, SlotKind.TOOL_INPUT, True))
                scan.keyed.append((given, SlotKind.TOOL_INPUT, True))
            elif given is not None:
                scan.unreadable.append("a tool call's input that is not an object")
        elif btype == "tool_result":
            name = names.get(str(block.get("tool_use_id")))
            inner = block.get("content")
            first = len(scan.slots)
            if isinstance(inner, str):
                scan.slots.append(SlotRef(block, "content", SlotKind.TOOL_RESULT, False, tool=name))
            else:
                _blocks(inner, SlotKind.TOOL_RESULT, False, scan, names, name, sources)
            came_from(scan.slots[first:], sources.get(str(block.get("tool_use_id"))))
        elif btype == "tool_addition":
            pass  # a tool the conversation adds: read with the request's tools (``added_tools``)
        elif btype == "image":
            source = block.get("source")
            if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
                media = str(source.get("media_type") or "application/octet-stream")
                image = ImageRef(source, "data", "base64", media, kind, len(scan.slots), tool, content, index, _notice)
                scan.images.append(image)
            else:
                scan.uninspected.append("image")  # by URL or file id: the provider fetches it
        elif btype == "search_result":
            scan.unreadable.append("a search result whose client-authored content Latet does not read")
            scan.uninspected.append(btype)
        elif btype == "document":
            if "title" in block or "context" in block:
                scan.unreadable.append("a document with a title or context Latet does not read")
            scan.uninspected.append(btype)
        elif btype not in _PROVIDERS_BLOCKS:
            scan.unreadable.append("a block of content of a kind Latet does not read")


def added_tools(body: dict[str, Any], tools: BodyScan) -> None:
    """The tools a conversation adds as it goes (Claude Code's ``tool_addition`` blocks, in a message of the system's
    role), read where and as the request's own tools are: each definition (``definitions``); a tool named alone (a
    reference to it) holds nothing to read, and one added in another form is named in ``tools.unreadable``."""
    messages = body.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_addition":
                continue
            tool = block.get("tool")
            if isinstance(tool, dict) and tool.get("type") == "tool_definition":
                definitions([tool.get("definition")], tools)
            elif not (isinstance(tool, dict) and tool.get("type") == "tool_reference"):
                tools.unreadable.append("a tool added in a form Latet does not read")


def _notice(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _answers_format(body: dict[str, Any], scan: BodyScan) -> None:
    """The format the answer is asked in (``output_config.format``, the older ``output_format``): its schema read as a
    tool's parameters are (``output_format``); one Latet cannot read named in ``scan.unreadable``."""
    output_format(body.get("output_format"), scan)
    settings = body.get("output_config")
    if isinstance(settings, dict):
        output_format(settings.get("format"), scan)
    elif settings is not None:
        scan.unreadable.append("an answer's settings that are not an object")


def scan_request(body: dict[str, Any]) -> BodyScan:
    scan = BodyScan()
    metadata(body, scan)  # whom the call is for: Claude Code's metadata.user_id
    _answers_format(body, scan)  # read where Latet masks the request, checked where it only checks
    if "fallbacks" in body:
        fallbacks = body["fallbacks"]
        if not isinstance(fallbacks, list):
            scan.unreadable.append("fallbacks that are not a list")
        else:
            for fallback in fallbacks:
                if isinstance(fallback, dict):
                    _answers_format(fallback, scan)
                else:
                    scan.unreadable.append("a fallback that is not an object")
    system = body.get("system")
    if isinstance(system, str):
        if not system.startswith(_PINNED_SYSTEM_PREFIXES):
            scan.slots.append(SlotRef(body, "system", SlotKind.SYSTEM))
    elif isinstance(system, list):
        for block in system:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                scan.unreadable.append("a block of the system prompt that is not text")
            elif not block["text"].startswith(_PINNED_SYSTEM_PREFIXES):
                scan.slots.append(SlotRef(block, "text", SlotKind.SYSTEM))
    elif system is not None:
        scan.unreadable.append("a system prompt that is neither text nor blocks")
    names: dict[str, str] = {}  # tool_use id -> tool name
    sources: dict[str, tuple[Any, Any]] = {}  # tool_use id -> its input, which names where its result came from
    messages = body.get("messages")
    if messages is not None and not isinstance(messages, list):
        scan.unreadable.append("messages that are not a list")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            scan.unreadable.append("a message that is not an object")
            continue
        role = message.get("role")
        from_model = role == "assistant"
        kind = {"assistant": SlotKind.ASSISTANT_TEXT, "system": SlotKind.SYSTEM}.get(role or "", SlotKind.USER_TEXT)
        content = message.get("content")
        if isinstance(content, str):
            scan.slots.append(SlotRef(message, "content", kind, from_model))
        else:
            _blocks(content, kind, from_model, scan, names, sources=sources)
    return scan
