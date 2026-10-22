"""OpenAI Responses API (``/v1/responses``, Codex's ``/backend-api/codex/responses``), over HTTP
or as ``response.create`` WebSocket frames."""

from __future__ import annotations

from typing import Any

from latet.engine.types import SlotKind
from latet.protocols.base import (
    BodyScan,
    ImageRef,
    SlotRef,
    came_from,
    data_url,
    definitions,
    metadata,
    string_leaves,
)

_ROLE_KIND = {
    "system": SlotKind.SYSTEM,
    "developer": SlotKind.SYSTEM,
    "user": SlotKind.USER_TEXT,
    "assistant": SlotKind.ASSISTANT_TEXT,
}
_TEXT_PARTS = {"input_text", "output_text", "text", "summary_text"}
_MEDIA_PARTS = {"input_file", "input_audio"}
_IMAGE_PARTS = {"input_image", "computer_screenshot"}
# Audio, and what was heard in it (a Realtime part's transcript): neither is read. A computer sends it as it came; a
# server, which reads what it sends, refuses it.
_AUDIO_PARTS = {"audio", "output_audio"}
# Items the provider made, given back as it made them and never restored (reasoning, encrypted; a compaction; its own
# tools' calls; a call of a tool the model made and Latet does not restore), and items that hold no content (a reference
# to another): passed as they came, on a server too.
_PROVIDERS_ITEMS = {
    "reasoning",
    "compaction",
    "item_reference",
    "web_search_call",
    "file_search_call",
    "image_generation_call",
    "code_interpreter_call",
    "computer_call",
    "apply_patch_call",
    "mcp_list_tools",
    "mcp_approval_request",
    "tool_search_call",
    "tool_search_output",
}
# Items Latet reads. Any other kind is named unreadable: a server refuses it, a computer sends it as it came.
_READ_ITEMS = {
    "message",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "mcp_call",
    "mcp_approval_response",
    "local_shell_call",
    "local_shell_call_output",
    "shell_call",
    "shell_call_output",
    "computer_call_output",
    "apply_patch_call_output",
    "additional_tools",
}


def _parts(
    container: dict[str, Any], key: str, kind: SlotKind, from_model: bool, scan: BodyScan, tool: str | None = None
) -> None:
    value = container.get(key)
    if isinstance(value, str):
        scan.slots.append(SlotRef(container, key, kind, from_model, tool=tool))
    elif isinstance(value, list):
        for index, part in enumerate(value):
            if not isinstance(part, dict):
                scan.unreadable.append("a part of content that is not an object")
                continue
            ptype = part.get("type")
            if not isinstance(ptype, str):
                scan.unreadable.append("a part of content of a kind Latet does not read")
            elif ptype in _TEXT_PARTS and isinstance(part.get("text"), str):
                scan.slots.append(SlotRef(part, "text", kind, from_model, tool=tool))
            elif ptype == "refusal" and isinstance(part.get("refusal"), str):  # the model's refusal, its text
                scan.slots.append(SlotRef(part, "refusal", kind, from_model, tool=tool))
            elif ptype in _IMAGE_PARTS:
                _image(part, kind, scan, tool, value, index)
            elif ptype in _MEDIA_PARTS:
                scan.uninspected.append(ptype)
                if part.get("transcript") is not None:  # what was heard, beside the audio (the Realtime shape)
                    scan.unreadable.append("audio, which Latet does not read")
            elif ptype in _AUDIO_PARTS:
                scan.unreadable.append("audio, which Latet does not read")
            else:
                scan.unreadable.append("a part of content of a kind Latet does not read")
    elif isinstance(value, dict):  # a computer call's screenshot: the image is the whole output
        ptype = value.get("type")
        if ptype in _IMAGE_PARTS:
            _image(value, kind, scan, tool)
        elif ptype in _MEDIA_PARTS:
            scan.uninspected.append(str(ptype))
        else:
            scan.unreadable.append("a part of content of a kind Latet does not read")
    elif value is not None:
        scan.unreadable.append("content that is neither text nor parts")


def _text(
    item: dict[str, Any],
    key: str,
    kind: SlotKind,
    from_model: bool,
    scan: BodyScan,
    *,
    tool: str | None = None,
    mode: str = "text",
) -> None:
    """An item's field that is a text when it is given: read, or named unreadable when it is something else."""
    value = item.get(key)
    if isinstance(value, str):
        scan.slots.append(SlotRef(item, key, kind, from_model, tool=tool, mode=mode))
    elif value is not None:
        scan.unreadable.append("a field of an input item that is not in the form Latet reads")


def _printed(item: dict[str, Any], scan: BodyScan, tool: str) -> None:
    """A shell call's output (``shell_call_output``): what each command printed, its standard output and its error,
    each a tool's output; how it ended holds no text. Given as a text or as parts, it is read as any output is."""
    output = item.get("output")
    if not isinstance(output, list) or not all(isinstance(c, dict) and "type" not in c for c in output):
        _parts(item, "output", SlotKind.TOOL_RESULT, False, scan, tool)
        return
    for chunk in output:
        for stream in ("stdout", "stderr"):
            if isinstance(chunk.get(stream), str):
                scan.slots.append(SlotRef(chunk, stream, SlotKind.TOOL_RESULT, tool=tool))
            elif chunk.get(stream) is not None:
                scan.unreadable.append("a command's output that is not text")


def _image(
    part: dict[str, Any],
    kind: SlotKind,
    scan: BodyScan,
    tool: str | None,
    blocks: list[Any] | None = None,
    index: int = -1,
) -> None:
    found = data_url(part.get("image_url"))
    if found is None:
        scan.uninspected.append("image")  # by URL or file id: the provider fetches it
        return
    notice = None if blocks is None else (lambda text: {"type": "input_text", "text": text})
    scan.images.append(
        ImageRef(part, "image_url", "data_url", found[0], kind, len(scan.slots), tool, blocks, index, notice)
    )


def scan_items(items: list[Any], scan: BodyScan) -> None:
    """A request's input items, each read by its kind (``_READ_ITEMS``), or passed as the provider made it
    (``_PROVIDERS_ITEMS``); any other kind, and an item that is not an object, is named in ``scan.unreadable``."""
    names: dict[str, str] = {}  # call id -> tool name
    sources: dict[str, tuple[Any, Any]] = {}  # call id -> its input, which names where its output came from
    for item in items:
        if not isinstance(item, dict):
            scan.unreadable.append("an input item that is not an object")
            continue
        itype = item.get("type", "message")
        if not isinstance(itype, str) or itype not in _READ_ITEMS | _PROVIDERS_ITEMS:
            scan.unreadable.append("an input item of a kind Latet does not read")
            continue
        if itype in _PROVIDERS_ITEMS:
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and not itype.endswith("_output"):
            names[call_id] = str(item.get("name") or itype.removesuffix("_call"))
            sources[call_id] = (item, "arguments" if "arguments" in item else "action")
        if itype == "message":
            role = item.get("role", "user")
            _parts(item, "content", _ROLE_KIND.get(role, SlotKind.USER_TEXT), role == "assistant", scan)
        elif itype in ("function_call", "mcp_call"):
            _text(item, "arguments", SlotKind.TOOL_INPUT, True, scan, mode="json")
            if itype == "mcp_call":  # what the provider's MCP server gave back
                _text(item, "output", SlotKind.TOOL_RESULT, False, scan, tool=str(item.get("name") or "mcp"))
        elif itype == "custom_tool_call":
            _text(item, "input", SlotKind.TOOL_INPUT, True, scan)
        elif itype in ("local_shell_call", "shell_call"):
            if isinstance(item.get("action"), dict):
                scan.slots.extend(string_leaves(item["action"], SlotKind.TOOL_INPUT, True))
                scan.keyed.append((item["action"], SlotKind.TOOL_INPUT, True))
            elif item.get("action") is not None:
                scan.unreadable.append("a field of an input item that is not in the form Latet reads")
        elif itype == "mcp_approval_response":  # why, in the client's words
            _text(item, "reason", SlotKind.USER_TEXT, False, scan)
        elif itype == "additional_tools":  # tools given as an item: read as a request's tools are
            definitions(item.get("tools"), scan)
        elif itype.endswith("_output"):  # function_call_output, custom_tool_call_output, shell outputs
            tool = names.get(str(item.get("call_id"))) or itype.removesuffix("_call_output").removesuffix("_output")
            first = len(scan.slots)
            if itype == "shell_call_output":
                _printed(item, scan, tool)
            else:
                _parts(item, "output", SlotKind.TOOL_RESULT, False, scan, tool)
            came_from(scan.slots[first:], sources.get(str(item.get("call_id"))))


def _variables(prompt: Any, scan: BodyScan) -> None:
    """The variables of a prompt kept on the provider's side (``prompt: {id, variables}``): each a text, or a part of
    content as an item's are (a text, an image, a file); a number holds no text. One Latet does not read is named in
    ``scan.unreadable``, as a prompt that is not an object is."""
    if prompt is not None and not isinstance(prompt, dict):
        scan.unreadable.append("a prompt that is not an object")
        return
    if not isinstance(prompt, dict) or prompt.get("variables") is None:
        return
    variables = prompt["variables"]
    if not isinstance(variables, dict):
        scan.unreadable.append("prompt variables that are not an object")
        return
    for name, value in variables.items():
        ptype = value.get("type") if isinstance(value, dict) else None
        if isinstance(value, str):
            scan.slots.append(SlotRef(variables, name, SlotKind.USER_TEXT))
        elif ptype in _TEXT_PARTS and isinstance(value.get("text"), str):
            scan.slots.append(SlotRef(value, "text", SlotKind.USER_TEXT))
        elif ptype in _IMAGE_PARTS:
            _image(value, SlotKind.USER_TEXT, scan, None)
        elif ptype in _MEDIA_PARTS:
            scan.uninspected.append(str(ptype))
        elif not isinstance(value, int | float | bool) and value is not None:
            scan.unreadable.append("a prompt variable of a kind Latet does not read")


def scan_request(body: dict[str, Any]) -> BodyScan:
    scan = BodyScan()
    metadata(body, scan)  # whom the call is for, and the client's notes
    instructions = body.get("instructions")
    if isinstance(instructions, str):
        scan.slots.append(SlotRef(body, "instructions", SlotKind.SYSTEM))
    elif instructions is not None:
        scan.unreadable.append("instructions that are not text")
    _variables(body.get("prompt"), scan)
    value = body.get("input")
    if isinstance(value, str):
        scan.slots.append(SlotRef(body, "input", SlotKind.USER_TEXT))
    elif isinstance(value, list):
        scan_items(value, scan)
    elif value is not None:
        scan.unreadable.append("an input that is neither text nor items")
    return scan


def scan_turn(frame: dict[str, Any]) -> BodyScan:
    """A ``response.create`` frame of a WebSocket: a Responses request's fields at its top (Codex's), or under
    ``response`` (the Realtime shape: its ``instructions`` and ``input``), read alike."""
    scan = scan_request(frame)
    response = frame.get("response")
    if isinstance(response, dict):
        inner = scan_request(response)
        for image in inner.images:  # its place among the slots, which come after the frame's own
            image.at += len(scan.slots)
        scan.slots += inner.slots
        scan.uninspected += inner.uninspected
        scan.images += inner.images
        scan.keyed += inner.keyed
        scan.unreadable += inner.unreadable
    elif response is not None:
        scan.unreadable.append("a turn's response that is not an object")
    return scan
