"""OpenAI Chat Completions (``/v1/chat/completions``), also spoken by OpenRouter and most gateways."""

from __future__ import annotations

from typing import Any

from latet.engine.types import SlotKind
from latet.protocols.base import BodyScan, ImageRef, SlotRef, came_from, data_url, metadata

_ROLE_KIND = {
    "system": SlotKind.SYSTEM,
    "developer": SlotKind.SYSTEM,
    "user": SlotKind.USER_TEXT,
    "assistant": SlotKind.ASSISTANT_TEXT,
    "tool": SlotKind.TOOL_RESULT,
    "function": SlotKind.TOOL_RESULT,
}


_TEXT_PARTS = ("text", "input_text", "output_text")
_IMAGE_PARTS = ("image_url", "input_image")
_MEDIA_PARTS = ("input_audio", "file", "input_file")  # not read: ``images`` decides whether they are sent


def _content(
    message: dict[str, Any], kind: SlotKind, from_model: bool, scan: BodyScan, tool: str | None = None
) -> None:
    """A message's content, a text or parts, each part read by its kind (a model's refusal is its text); a part of any
    other kind, one that is not an object, and content in another form are named in ``scan.unreadable``: a server
    refuses them, a computer sends them as they came."""
    content = message.get("content")
    if isinstance(content, str):
        scan.slots.append(SlotRef(message, "content", kind, from_model, tool=tool))
    elif isinstance(content, list):
        for index, part in enumerate(content):
            ptype = part.get("type") if isinstance(part, dict) else None
            if not isinstance(part, dict):
                scan.unreadable.append("a part of content that is not an object")
            elif not isinstance(ptype, str):
                scan.unreadable.append("a part of content of a kind Latet does not read")
            elif ptype in _TEXT_PARTS or ptype == "refusal":
                key = "refusal" if ptype == "refusal" else "text"
                if isinstance(part.get(key), str):
                    scan.slots.append(SlotRef(part, key, kind, from_model, tool=tool))
                else:
                    scan.unreadable.append("a part's text that is not text")
            elif ptype in _IMAGE_PARTS:
                image = image_ref(part, kind, len(scan.slots), tool, content, index)
                if image is not None:
                    scan.images.append(image)
                else:
                    scan.uninspected.append("image")  # by URL: the provider fetches it
            elif ptype in _MEDIA_PARTS:
                scan.uninspected.append(ptype)
            else:
                scan.unreadable.append("a part of content of a kind Latet does not read")
    elif content is not None:
        scan.unreadable.append("content that is neither text nor parts")


def _calls(message: dict[str, Any], scan: BodyScan, names: dict[str, str], sources: dict[str, tuple[Any, Any]]) -> None:
    """An assistant's tool calls: a function's arguments, a JSON document, and a custom tool's input, a text, each as
    the model wrote it; a call in another form, or of another kind, is named in ``scan.unreadable``."""
    calls = message.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        scan.unreadable.append("tool calls that are not a list")
        return
    for call in calls or []:
        kind = call.get("type", "function") if isinstance(call, dict) else None
        body = call.get(kind) if isinstance(call, dict) and isinstance(kind, str) else None
        if kind not in ("function", "custom") or not isinstance(body, dict):
            scan.unreadable.append("a tool call in a form Latet does not read")
            continue
        if isinstance(call.get("id"), str) and isinstance(body.get("name"), str):
            names[call["id"]] = body["name"]  # its result, later, says which tool made it
            if kind == "function":  # and which file or address it came from
                sources[call["id"]] = (body, "arguments")
        field, mode = ("arguments", "json") if kind == "function" else ("input", "text")
        if isinstance(body.get(field), str):
            scan.slots.append(SlotRef(body, field, SlotKind.TOOL_INPUT, True, mode=mode))
        elif body.get(field) is not None:
            scan.unreadable.append("a tool call in a form Latet does not read")


def image_ref(
    part: dict[str, Any], kind: SlotKind, at: int, tool: str | None, blocks: list[Any], index: int
) -> ImageRef | None:
    """An image part whose URL is a ``data:`` URL (``{"image_url": {"url": ...}}`` or ``{"image_url": ...}``)."""
    url = part.get("image_url")
    holder, key = (url, "url") if isinstance(url, dict) else (part, "image_url")
    found = data_url(holder.get(key))
    if found is None:
        return None
    notice = {"type": "text"} if part.get("type") == "image_url" else {"type": "input_text"}
    return ImageRef(holder, key, "data_url", found[0], kind, at, tool, blocks, index, lambda t: {**notice, "text": t})


def scan_request(body: dict[str, Any]) -> BodyScan:
    scan = BodyScan()
    metadata(body, scan)  # whom the call is for, and the client's notes
    names: dict[str, str] = {}  # tool call id -> function name
    sources: dict[str, tuple[Any, Any]] = {}  # tool call id -> its input, which names where its result came from
    messages = body.get("messages")
    if messages is not None and not isinstance(messages, list):
        scan.unreadable.append("messages that are not a list")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            scan.unreadable.append("a message that is not an object")
            continue
        role = message.get("role", "user")
        kind = _ROLE_KIND.get(role, SlotKind.USER_TEXT)
        from_model = role == "assistant"
        if "name" in message:
            scan.unreadable.append("a chat message name Latet does not read")
        tool = None
        first = len(scan.slots)
        if kind == SlotKind.TOOL_RESULT:
            tool = names.get(str(message.get("tool_call_id"))) or (message.get("name") if role == "function" else None)
        _content(message, kind, from_model, scan, tool if isinstance(tool, str) else None)
        if kind == SlotKind.TOOL_RESULT:
            came_from(scan.slots[first:], sources.get(str(message.get("tool_call_id"))))
        refusal = message.get("refusal")  # the model's refusal, beside its content
        if isinstance(refusal, str):
            scan.slots.append(SlotRef(message, "refusal", kind, from_model))
        elif refusal is not None:
            scan.unreadable.append("a refusal that is not text")
        _calls(message, scan, names, sources)
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and isinstance(legacy.get("arguments"), str):
            scan.slots.append(SlotRef(legacy, "arguments", SlotKind.TOOL_INPUT, True, mode="json"))
        elif legacy is not None:
            scan.unreadable.append("a tool call in a form Latet does not read")
    # Legacy completions endpoint: a prompt, or a list of prompts, and the text that follows the completion.
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        scan.slots.append(SlotRef(body, "prompt", SlotKind.USER_TEXT))
    elif isinstance(prompt, list):
        scan.slots.extend(
            SlotRef(prompt, i, SlotKind.USER_TEXT) for i, part in enumerate(prompt) if isinstance(part, str)
        )
        if not all(isinstance(part, str) for part in prompt):
            scan.unreadable.append("a prompt given as tokens")  # numbers, or lists of them, a tokenizer's
    elif prompt is not None:
        scan.unreadable.append("a prompt that is not text")
    if isinstance(body.get("suffix"), str):
        scan.slots.append(SlotRef(body, "suffix", SlotKind.USER_TEXT))
    # A predicted output (``prediction: {type: content, content}``): what the client expects the answer to hold, most
    # often a file of its own being edited, a text or text parts.
    prediction = body.get("prediction")
    if isinstance(prediction, dict):
        _content(prediction, SlotKind.ATTACHMENT, False, scan)
    elif prediction is not None:
        scan.unreadable.append("a predicted output that is not an object")
    return scan
