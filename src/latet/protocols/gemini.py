"""Google's Gemini API: ``generateContent``, ``streamGenerateContent``, ``countTokens``, ``embedContent``,
``batchEmbedContents`` and the creation of an explicit cache (``cachedContents``), on the Gemini API
(``/v1beta/models/...``) and on Vertex AI (``.../publishers/google/models/...``).

Google reads every field under its camelCase name and under its snake_case one (proto3 JSON), and its Python SDK sends
both kinds in one body (camelCase at the top, snake_case inside): Latet reads both, everywhere."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

from latet.engine.types import SlotKind
from latet.protocols.base import (
    BodyScan,
    ImageRef,
    JsonRef,
    SlotRef,
    came_from,
    string_leaves,
)

METHODS = frozenset({"generateContent", "streamGenerateContent", "countTokens", "embedContent", "batchEmbedContents"})
# A model's method ends its path after a colon, which a client may write escaped (Google decodes it, and so must Latet).
_METHOD = re.compile(r"(?::|%3[Aa])([A-Za-z]+)/?$")
_CACHES = re.compile(r"/cachedContents(?:/[^/]+)?/?$")
# The model a call names in its path, before its method: the body names none.
_MODEL = re.compile(r"/models/([^/]+?)(?::|%3[Aa])[A-Za-z]+/?$")

TWO_NAMES = "a field given under two names"
# The fields that make a part's kind: a part holds one of them.
_DATA = (
    "text",
    "inlineData",
    "fileData",
    "functionCall",
    "functionResponse",
    "executableCode",
    "codeExecutionResult",
    "toolCall",
    "toolResponse",
)
# Code and tools the provider ran, given back as it made them.
_PROVIDERS = ("executableCode", "codeExecutionResult", "toolCall", "toolResponse")
# What a part may carry beside its content that holds no text Latet reads.
_METADATA = ("thought", "thoughtSignature", "mediaResolution", "mediaProcessing", "videoMetadata", "speechMetadata")
_AUDIO = ("audioTranscription", "audio_transcription")  # what was heard in audio, which Latet does not read
# The fields of a function call and of its response: any other is in a form Latet does not read.
_CALLED = ("id", "name", "args", "partialArgs", "willContinue")
_ANSWERED_BY = ("id", "name", "response", "parts", "willContinue", "scheduling")
_ROLE_KIND = {"model": SlotKind.ASSISTANT_TEXT, "function": SlotKind.TOOL_RESULT}
# Where a function declaration and a request's answer carry a schema: JSON documents, read as a tool's parameters are.
_DECLARED = ("parameters", "parametersJsonSchema", "response", "responseJsonSchema")
_ANSWERED = ("responseSchema", "responseJsonSchema", "_responseJsonSchema")


def snake(name: str) -> str:
    """A field's snake_case name (``inlineData``: ``inline_data``)."""
    return re.sub(r"([A-Z])", r"_\1", name).lower()


# Every field a part may hold, under both its names: any other is a kind of content Latet does not read.
_KNOWN = frozenset(name for field in (*_DATA, *_METADATA, "partMetadata") for name in (field, snake(field)))


def method_of(path: str) -> str | None:
    """The Gemini method ``path`` calls (``generateContent``...), ``cachedContents`` for an explicit cache, or None."""
    found = _METHOD.search(path)
    if found is not None and found.group(1) in METHODS:
        return found.group(1)
    return "cachedContents" if _CACHES.search(path) else None


def model_of(path: str) -> str | None:
    """The model a Gemini call names in its path (``models/gemini-3-pro:generateContent``), or None."""
    found = _MODEL.search(path)
    return unquote(found.group(1)) if found is not None and method_of(path) in METHODS else None


def _each(obj: dict[str, Any], name: str, scan: BodyScan | None = None) -> list[tuple[str, Any]]:
    """``obj``'s field ``name`` under each of its names it is given under (a key, its value). Given under both, it is
    named unreadable in ``scan``, and both are read: whichever Google reads is masked."""
    found = [(key, obj[key]) for key in dict.fromkeys((name, snake(name))) if key in obj]
    if len(found) > 1 and scan is not None:
        scan.unreadable.append(TWO_NAMES)
    return found


def _one(obj: dict[str, Any], name: str) -> Any:
    """``obj``'s field ``name`` under whichever name it is given (its camelCase one first), or None."""
    found = _each(obj, name)
    return found[0][1] if found else None


def _listed(value: Any) -> list[Any] | None:
    """A list as it is, an object as a list of one (as Google reads ``"parts": {...}``), anything else None."""
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else None


def _uninspected(media: str) -> str:
    """What a medium Latet does not read is named as, in the words the dashboard explains."""
    if media.startswith("image/"):
        return "image"
    if media == "application/pdf":
        return "document"
    return "audio" if media.startswith("audio/") else "file"


class _Reader:
    """One request's parts, read into ``scan``: each function call's name and input kept by its id (else its name), so
    that its response is known as that tool's output and as coming from the file or address the call names."""

    def __init__(self, scan: BodyScan) -> None:
        self.scan = scan
        self.names: dict[str, str] = {}
        self.sources: dict[str, tuple[Any, Any]] = {}

    def content(self, holder: Any, key: Any, kind: SlotKind, from_model: bool) -> None:
        """A ``Content`` (its ``parts``), or a text where a client gives one (a system instruction)."""
        value = holder[key]
        if isinstance(value, str):
            self.scan.slots.append(SlotRef(holder, key, kind, from_model))
            return
        if not isinstance(value, dict):
            if value is not None:
                self.scan.unreadable.append("content that is neither text nor parts")
            return
        for _, parts in _each(value, "parts", self.scan):
            self.parts(parts, kind, from_model)

    def contents(self, value: Any) -> None:
        listed = _listed(value)
        if listed is None:
            if value is not None:
                self.scan.unreadable.append("contents that are not a list")
            return
        for content in listed:
            if not isinstance(content, dict):
                self.scan.unreadable.append("a content that is not an object")
                continue
            role = content.get("role")
            kind = _ROLE_KIND.get(role, SlotKind.USER_TEXT) if isinstance(role, str) else SlotKind.USER_TEXT
            for _, parts in _each(content, "parts", self.scan):
                self.parts(parts, kind, role == "model")

    def parts(self, value: Any, kind: SlotKind, from_model: bool, tool: str | None = None) -> None:
        listed = _listed(value)
        if listed is None:
            if value is not None:
                self.scan.unreadable.append("content that is neither text nor parts")
            return
        blocks = value if isinstance(value, list) else None
        for index, part in enumerate(listed):
            if not isinstance(part, dict):
                self.scan.unreadable.append("a part of content that is not an object")
                continue
            self.part(part, kind, from_model, tool, blocks, index)

    def part(
        self,
        part: dict[str, Any],
        kind: SlotKind,
        from_model: bool,
        tool: str | None,
        blocks: list[Any] | None,
        index: int,
    ) -> None:
        if any(key in part for key in _AUDIO):
            self.scan.unreadable.append("audio, which Latet does not read")
        elif any(key not in _KNOWN for key in part):
            self.scan.unreadable.append("a part of content of a kind Latet does not read")
        data = [field for field in _DATA if _each(part, field)]
        if len(data) > 1:
            self.scan.unreadable.append("a part of content of a kind Latet does not read")
        for _, value in _each(part, "partMetadata", self.scan):  # a client's own notes, as other protocols' metadata
            self.scan.slots.extend(string_leaves(value, SlotKind.OTHER, False))
        for field in data:
            for key, value in _each(part, field, self.scan):
                self.data(part, field, key, value, kind, from_model, tool, blocks, index)

    def data(
        self,
        part: dict[str, Any],
        field: str,
        key: str,
        value: Any,
        kind: SlotKind,
        from_model: bool,
        tool: str | None,
        blocks: list[Any] | None,
        index: int,
    ) -> None:
        if field == "text":
            if isinstance(value, str):
                self.scan.slots.append(SlotRef(part, key, kind, from_model, tool=tool))
            else:
                self.scan.unreadable.append("a part's text that is not text")
        elif field in ("inlineData", "fileData"):
            self.media(field, value, kind, from_model, tool, blocks, index)
        elif field == "functionCall":
            self.call(value)
        elif field == "functionResponse":
            self.response(value)
        elif field not in _PROVIDERS:  # pragma: no cover - every field of _DATA is handled
            self.scan.unreadable.append("a part of content of a kind Latet does not read")

    def media(
        self,
        field: str,
        value: Any,
        kind: SlotKind,
        from_model: bool,
        tool: str | None,
        blocks: list[Any] | None,
        index: int,
    ) -> None:
        """An image carried as bytes, read; any other medium, and one by its address, named as not inspected. Its
        display name is a text of its part's kind."""
        if not isinstance(value, dict):
            self.scan.unreadable.append("a medium that is not an object")
            return
        types = _each(value, "mimeType", self.scan)
        media = types[0][1].strip().lower() if types and isinstance(types[0][1], str) else ""
        data = _each(value, "data", self.scan)
        if field == "inlineData" and media.startswith("image/") and data and isinstance(data[0][1], str):
            notice = None if blocks is None or tool is not None else (lambda text: {"text": text})
            self.scan.images.append(
                ImageRef(
                    value,
                    data[0][0],
                    "base64url",
                    media,
                    kind,
                    len(self.scan.slots),
                    tool,
                    blocks if notice is not None else None,
                    index,
                    notice,
                    type_key=types[0][0],
                )
            )
        else:
            self.scan.uninspected.append(_uninspected(media))
        for key, name in _each(value, "displayName", self.scan):
            if isinstance(name, str):
                self.scan.slots.append(SlotRef(value, key, kind, from_model, tool=tool))
            elif name is not None:
                self.scan.unreadable.append("a medium's name that is not text")

    def call(self, value: Any) -> None:
        """A function call the model made: the string leaves of its arguments, and its keys, as the model wrote them."""
        if not isinstance(value, dict):
            self.scan.unreadable.append("a tool call in a form Latet does not read")
            return
        if any(key not in {n for field in _CALLED for n in (field, snake(field))} for key in value):
            self.scan.unreadable.append("a tool call in a form Latet does not read")
        name, ident = _one(value, "name"), _one(value, "id")
        called = ident if isinstance(ident, str) else name
        if isinstance(name, str) and isinstance(called, str):
            self.names[called] = name
        for key, args in _each(value, "args", self.scan):
            if not isinstance(args, dict):
                if args is not None:
                    self.scan.unreadable.append("a tool call's input that is not an object")
                continue
            if isinstance(called, str):
                self.sources[called] = (value, key)
            self.scan.slots.extend(string_leaves(args, SlotKind.TOOL_INPUT, True))
            self.scan.keyed.append((args, SlotKind.TOOL_INPUT, True))
        for _, pieces in _each(value, "partialArgs", self.scan):  # Vertex's streamed arguments, sent back as they came
            listed = _listed(pieces)
            if listed is None:
                self.scan.unreadable.append("a tool call's input that is not an object")
                continue
            for piece in listed:
                for key, text in _each(piece, "stringValue", self.scan) if isinstance(piece, dict) else []:
                    if isinstance(text, str):
                        self.scan.slots.append(SlotRef(piece, key, SlotKind.TOOL_INPUT, True))
                    elif text is not None:
                        self.scan.unreadable.append("a tool call's input that is not text")

    def response(self, value: Any) -> None:
        """A function's response: the string leaves of its ``response`` and its keys, a tool's output; its ``parts``
        (images it gives back) read as parts of a tool's output."""
        if not isinstance(value, dict):
            self.scan.unreadable.append("a tool's output in a form Latet does not read")
            return
        if any(key not in {n for field in _ANSWERED_BY for n in (field, snake(field))} for key in value):
            self.scan.unreadable.append("a tool's output in a form Latet does not read")
        name, ident = _one(value, "name"), _one(value, "id")
        by = ident if isinstance(ident, str) and ident in self.names else name
        tool = self.names.get(by) if isinstance(by, str) else None
        tool = tool or (name if isinstance(name, str) else None)
        first = len(self.scan.slots)
        for _, response in _each(value, "response", self.scan):
            if not isinstance(response, dict):
                if response is not None:
                    self.scan.unreadable.append("a tool's output that is not an object")
                continue
            for slot in string_leaves(response, SlotKind.TOOL_RESULT, False):
                slot.tool = tool
                self.scan.slots.append(slot)
            self.scan.keyed.append((response, SlotKind.TOOL_RESULT, False))
        for _, parts in _each(value, "parts", self.scan):
            self.parts(parts, SlotKind.TOOL_RESULT, False, tool or "function")
        came_from(self.scan.slots[first:], self.sources.get(by) if isinstance(by, str) else None)


def _request(body: dict[str, Any], reader: _Reader) -> None:
    """What a model reads in a request to generate, count or cache: its system instruction and its contents."""
    for key, _ in _each(body, "systemInstruction", reader.scan):
        reader.content(body, key, SlotKind.SYSTEM, False)
    for _, contents in _each(body, "contents", reader.scan):
        reader.contents(contents)


def _embedded(body: dict[str, Any], reader: _Reader) -> None:
    """What an embedding's request carries: its content and its title (also under ``embedContentConfig``)."""
    for key, _ in _each(body, "content", reader.scan):
        reader.content(body, key, SlotKind.USER_TEXT, False)
    configs = [config for _, config in _each(body, "embedContentConfig", reader.scan) if isinstance(config, dict)]
    titled = [body, *configs]
    for holder in titled:
        for key, title in _each(holder, "title", reader.scan):
            if isinstance(title, str):
                reader.scan.slots.append(SlotRef(holder, key, SlotKind.USER_TEXT))
            elif title is not None:
                reader.scan.unreadable.append("a title that is not text")


def scan_request(body: dict[str, Any]) -> BodyScan:
    """A Gemini request's texts, but its tools and the schema of its answer (``definitions``, read first)."""
    scan = BodyScan()
    reader = _Reader(scan)
    _request(body, reader)
    for _, inner in _each(body, "generateContentRequest", scan):  # a token count of a whole request
        if isinstance(inner, dict):
            _request(inner, reader)
        elif inner is not None:
            scan.unreadable.append("a request that is not an object")
    _embedded(body, reader)
    for _, requests in _each(body, "requests", scan):  # a batch of embeddings
        listed = _listed(requests)
        if listed is None and requests is not None:
            scan.unreadable.append("requests that are not a list")
        for request in listed or []:
            if isinstance(request, dict):
                _embedded(request, reader)
            else:
                scan.unreadable.append("a request that is not an object")
    return scan


def _schema(holder: dict[str, Any], name: str, tools: BodyScan, what: str) -> None:
    for key, value in _each(holder, name, tools):
        if isinstance(value, dict | list):
            tools.slots.append(JsonRef(holder, key, SlotKind.TOOL_DESCRIPTION, mode="json"))
        elif value is not None and not isinstance(value, bool):  # a schema of true or false holds no text
            tools.unreadable.append(what)


def _declarations(holder: dict[str, Any], tools: BodyScan) -> None:
    for _, given in _each(holder, "tools", tools):
        listed = _listed(given)
        if listed is None:
            if given is not None:
                tools.unreadable.append("tool definitions not given as a list")
            continue
        for tool in listed:
            if not isinstance(tool, dict):
                tools.unreadable.append("a tool definition that is not an object")
                continue
            for _, declared in _each(tool, "functionDeclarations", tools):
                listed = _listed(declared)
                if listed is None and declared is not None:
                    tools.unreadable.append("tool definitions not given as a list")
                for function in listed or []:
                    if not isinstance(function, dict):
                        tools.unreadable.append("a tool definition that is not an object")
                        continue
                    for key, text in _each(function, "description", tools):
                        if isinstance(text, str):
                            tools.slots.append(SlotRef(function, key, SlotKind.TOOL_DESCRIPTION))
                        elif text is not None:
                            tools.unreadable.append("a tool description that is not text")
                    for name in _DECLARED:
                        _schema(function, name, tools, "a tool's parameters that are not an object")


def definitions(body: dict[str, Any], tools: BodyScan) -> None:
    """The functions a request declares (their descriptions as texts, their schemas as JSON documents) and the schema it
    asks the answer in, of the body and of a token count's whole request, read into ``tools``."""
    holders = [body] + [r for _, r in _each(body, "generateContentRequest") if isinstance(r, dict)]
    for holder in holders:
        _declarations(holder, tools)
        for _, config in _each(holder, "generationConfig", tools):
            if isinstance(config, dict):
                for name in _ANSWERED:
                    _schema(config, name, tools, "an answer's format whose schema is not an object")


def opening(body: dict[str, Any]) -> tuple[Any, Any]:
    """A request's system instruction and contents, under whichever names it gives them: what names its conversation.
    A token count of a whole request is of that request's conversation."""
    inner = _one(body, "generateContentRequest")
    if _one(body, "contents") is None and isinstance(inner, dict):
        body = inner
    return _one(body, "systemInstruction"), _one(body, "contents")


def _parts_of(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for content in _listed(_one(body, "contents")) or []:
        if isinstance(content, dict):
            for part in _listed(_one(content, "parts")) or []:
                if isinstance(part, dict):
                    yield part


def server_calls(body: dict[str, Any]) -> Iterator[tuple[str, list[SlotRef]]]:
    """The tools the provider ran in this conversation (``toolCall`` parts), each named, with the strings of its
    input."""
    for part in _parts_of(body):
        call = _one(part, "toolCall")
        if not isinstance(call, dict):
            continue
        name = next((n for n in (_one(call, "toolName"), _one(call, "toolType")) if isinstance(n, str)), "tool")
        args = _one(call, "args")
        yield name, list(string_leaves(args if isinstance(args, dict) else {}, SlotKind.OUTBOUND_TOOL, True))
