"""Shared pieces of the protocol adapters: references to maskable text inside a parsed body."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from latet.engine import wellformed
from latet.engine.types import Replacement, SlotKind
from latet.engine.vault import MaskingError


class KeysCollide(MaskingError):
    """Two keys of one JSON object would be one once masked (a key masked alone, its fake equal to a key beside it):
    an object holds a key once, so one of them would be lost, and the request is stopped instead. Its words name no
    key."""

    def __init__(self) -> None:
        super().__init__("two keys of one of its JSON objects would be the same once masked, so it was not sent")


@dataclass(slots=True)
class SlotRef:
    """A maskable string at ``container[key]``.

    ``mode`` says what the string is: ``text`` (masked as a whole) or ``json`` (a JSON document,
    such as tool-call arguments, whose string values are masked one by one).
    """

    container: Any
    key: Any
    kind: SlotKind
    from_model: bool = False
    mode: str = "text"
    tool: str | None = None  # the tool whose output this is (tool results), when the request says
    # Where that output came from, when its call says: the file a tool read, the address it fetched (``tool_source``).
    source: str | None = None
    # Where that call's input is in the request (its holder and key), which masking rewrites in place (``sent_source``).
    source_input: tuple[Any, Any] | None = None

    def sent_source(self) -> str | None:
        """Where this output came from as the request carried it: its call's input read again, once masked. The history
        keeps this form, as it keeps every text of the request as it left."""
        if self.source_input is None:
            return None
        holder, key = self.source_input
        return tool_source(holder.get(key)) if isinstance(holder, dict) else None

    def get(self) -> str:
        return self.container[self.key]

    def set(self, value: str) -> None:
        self.container[self.key] = value

    def put(self, value: str, replacements: Sequence[Replacement] = ()) -> None:
        """Write the masked text, telling a container that needs them where each value was (an image's
        text is painted over where the values were read)."""
        masked = getattr(self.container, "masked", None)
        if callable(masked):
            masked(self.key, value, replacements)
        else:
            self.container[self.key] = value


@dataclass(slots=True)
class JsonRef(SlotRef):
    """An object of the request read as a JSON document (a tool's parameters, their schema): its text is the object's,
    written compactly, its keys and values masked as a JSON text's are (``mode`` "json"), and the masked text read back
    into its place."""

    def get(self) -> str:
        return dumps_compact(self.container[self.key])

    def set(self, value: str) -> None:
        self.container[self.key] = wellformed.loads(value)

    def put(self, value: str, replacements: Sequence[Replacement] = ()) -> None:
        self.set(value)


@dataclass(slots=True)
class ImageRef:
    """An image carried in a request as bytes: ``holder[key]`` is its base64, or a ``data:`` URL.

    ``notice`` makes the block that stands in for the whole image when it is withheld, and
    ``blocks[index]`` is where that block goes; without them (a screenshot a tool call must answer
    with) only the image itself can change.
    """

    holder: dict[str, Any]
    key: str
    # "base64", "data_url", or "base64url": Google's, which reads base64 in either alphabet, padded or not (its Python
    # SDK sends the URL-safe one); written back in the standard alphabet, which it reads too.
    form: str
    media_type: str
    kind: SlotKind  # where it was: the user's message, a tool result...
    at: int  # how many text slots come before it in the request
    tool: str | None = None
    blocks: list[Any] | None = None
    index: int = -1
    notice: Callable[[str], dict[str, Any]] | None = None
    type_key: str = "media_type"  # where its holder says its media type, when it does (Google's ``mimeType``)

    def data(self) -> str:
        """The image's base64."""
        value = self.holder[self.key]
        if self.form == "data_url":
            return value.partition(",")[2]
        if self.form == "base64url":
            value = value.replace("-", "+").replace("_", "/")
            return value + "=" * (-len(value) % 4)
        return value

    def set(self, data: str, media_type: str) -> None:
        if self.form == "data_url":
            self.holder[self.key] = f"data:{media_type};base64,{data}"
        else:
            self.holder[self.key] = data
            if self.type_key in self.holder:
                self.holder[self.type_key] = media_type
        self.media_type = media_type

    def withhold(self, text: str) -> bool:
        """Put a text in place of the whole image; False when the request has no place for one."""
        if self.blocks is None or self.notice is None:
            return False
        self.blocks[self.index] = self.notice(text)
        return True


def data_url(value: Any) -> tuple[str, str] | None:
    """The media type and base64 of a ``data:<type>;base64,<data>`` URL; None for anything else."""
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    head, sep, data = value.partition(",")
    if not sep or not head.endswith(";base64"):
        return None
    return head[5:-7].strip().lower() or "application/octet-stream", data


@dataclass(slots=True)
class BodyScan:
    slots: list[SlotRef] = field(default_factory=list)
    uninspected: list[str] = field(default_factory=list)  # what is never read: "document", an image by URL...
    images: list[ImageRef] = field(default_factory=list)  # images carried as bytes
    # Objects of the request whose keys are text as their values are (a tool call's input, as the model wrote it):
    # each with the kind of text its keys are and whether the model wrote them. Their values are among ``slots``; their
    # keys are masked in place (``Mediator._mask_slots``).
    keyed: list[tuple[Any, SlotKind, bool]] = field(default_factory=list)
    # The fields that carry content in a form Latet does not read (a prompt given as tokens), each said in words that
    # name no content: a Latet server refuses the request, a computer sends it as it came.
    unreadable: list[str] = field(default_factory=list)

    def unread(self) -> list[str]:
        """What the request carries that Latet does not read: the images too, as long as nothing reads them."""
        return self.uninspected + ["image"] * len(self.images)

    def first(self, other: BodyScan) -> None:
        """``other``'s slots before this scan's own, as a model reads a request's tools before its conversation, each
        image's place among the slots moved after them; what ``other`` could not read added. ``other`` holds texts
        alone (a request's tool definitions: ``definitions``)."""
        for image in self.images:
            image.at += len(other.slots)
        self.slots[:0] = other.slots
        self.unreadable += other.unreadable


# The arguments of a tool call that say where its output comes from: the file it reads, the web address it fetches.
_SOURCES = ("file_path", "notebook_path", "path", "url")
_SOURCE_LONGEST = 4096  # a longer one is no file's path nor any address a rule is written for


def bounded_source(value: Any) -> str | None:
    """``value`` as where a tool's output came from: a text of one to ``_SOURCE_LONGEST`` characters, else None (the
    Claude Code plugin's ``path`` as a request's)."""
    return value if isinstance(value, str) and 0 < len(value) <= _SOURCE_LONGEST else None


def tool_source(given: Any) -> str | None:
    """Where the output of a tool call whose input is ``given`` (an object, or the JSON text of one) comes from: the
    file it reads (``file_path``, ``notebook_path``, ``path``) or the web address it fetches (``url``); None when its
    input names neither. Strict data mode covers a file or an address a rule names (`data_files`, `data_hosts`)."""
    if isinstance(given, str):
        if len(given) > 64 * 1024 or not given.lstrip().startswith("{"):
            return None
        try:
            given = json.loads(given)
        except ValueError:
            return None
    if not isinstance(given, dict):
        return None
    for key in _SOURCES:
        if (value := bounded_source(given.get(key))) is not None:
            return value
    return None


def came_from(slots: Sequence[SlotRef], call: tuple[Any, Any] | None) -> None:
    """``slots`` are a tool's output, whose call's input is ``call``'s holder at its key: each came from the file or the
    address that input names (``tool_source``), and keeps where the input is (``SlotRef.sent_source``)."""
    if call is None:
        return
    holder, key = call
    source = tool_source(holder.get(key)) if isinstance(holder, dict) else None
    if source is None:
        return
    for slot in slots:
        slot.source, slot.source_input = source, call


def string_leaves(value: Any, kind: SlotKind, from_model: bool) -> Iterator[SlotRef]:
    """Every string inside a JSON value (tool-call input objects), as text slots."""
    if isinstance(value, dict):
        for k in list(value.keys()):
            v = value[k]
            if isinstance(v, str):
                yield SlotRef(value, k, kind, from_model)
            else:
                yield from string_leaves(v, kind, from_model)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if isinstance(v, str):
                yield SlotRef(value, i, kind, from_model)
            else:
                yield from string_leaves(v, kind, from_model)


# What a tool's definition holds that the model reads as text: its description (a remote MCP server's too), and the
# schema of its parameters or input (their names, descriptions, examples, the values they take), or a custom tool's
# format. Its name stays as it is, the model calls the tool by it; so do what lets the provider run a tool of its own
# (an MCP server's address and headers, a web search's location).
_TOOL_TEXTS = ("description", "server_description")
# The schema of its parameters, a custom tool's grammar, and Anthropic's examples of its input: JSON documents.
_TOOL_SCHEMAS = ("parameters", "input_schema", "format", "input_examples")
_TOOL_WRAPPERS = ("function", "custom")  # chat completions: {"type": "function", "function": {...}}


def definitions(tools: Any, scan: BodyScan) -> None:
    """The tools a request defines (``tools``, a list), read into ``scan``: each one's description as a text, the
    schema of its parameters and the examples of its input as JSON documents (``JsonRef``), their keys too, all of the
    kind ``tool_description``. A definition Latet cannot read (not an object, a description that is not text, a schema
    that is not an object) is named in ``scan.unreadable``."""
    if tools is None:
        return
    if not isinstance(tools, list):
        scan.unreadable.append("tool definitions not given as a list")
        return
    for tool in tools:
        if not isinstance(tool, dict):
            scan.unreadable.append("a tool definition that is not an object")
            continue
        _definition(tool, scan)
        for wrapper in _TOOL_WRAPPERS:
            inner = tool.get(wrapper)
            if isinstance(inner, dict):
                _definition(inner, scan)
            elif inner is not None and tool.get("type") == wrapper:
                scan.unreadable.append("a tool definition that is not an object")


def _definition(tool: dict[str, Any], scan: BodyScan) -> None:
    for key in _TOOL_TEXTS:
        value = tool.get(key)
        if isinstance(value, str):
            scan.slots.append(SlotRef(tool, key, SlotKind.TOOL_DESCRIPTION))
        elif value is not None:
            scan.unreadable.append("a tool description that is not text")
    for key in _TOOL_SCHEMAS:
        value = tool.get(key)
        if isinstance(value, dict | list):
            scan.slots.append(JsonRef(tool, key, SlotKind.TOOL_DESCRIPTION, mode="json"))
        elif value is not None and not isinstance(value, bool):  # a schema of true or false holds no text
            scan.unreadable.append("a tool's parameters that are not an object")


def output_format(given: Any, scan: BodyScan) -> None:
    """The format a request asks the model's answer in (chat completions' ``response_format.json_schema``, the Responses
    API's ``text.format``), read into ``scan`` as a tool's definition is: its description as a text, its schema as a
    JSON document, its name left as it is. One Latet cannot read is named in ``scan.unreadable``."""
    if given is None:
        return
    if not isinstance(given, dict):
        scan.unreadable.append("an answer's format that is not an object")
        return
    description, schema = given.get("description"), given.get("schema")
    if isinstance(description, str):
        scan.slots.append(SlotRef(given, "description", SlotKind.TOOL_DESCRIPTION))
    elif description is not None:
        scan.unreadable.append("an answer's format whose description is not text")
    if isinstance(schema, dict | list):
        scan.slots.append(JsonRef(given, "schema", SlotKind.TOOL_DESCRIPTION, mode="json"))
    elif schema is not None and not isinstance(schema, bool):
        scan.unreadable.append("an answer's format whose schema is not an object")


_WHOSE = ("user", "safety_identifier")  # what a client says of whom a call is for (OpenAI's)


def metadata(body: dict[str, Any], scan: BodyScan) -> None:
    """What a client says of whom a call is for and its own notes: ``user`` and ``safety_identifier`` (OpenAI's),
    ``metadata``'s values (Anthropic's ``user_id``, OpenAI's pairs), each text of them read into ``scan`` of the kind
    ``other``. A value that is no text is left as it is, never named unreadable: a client always sends some (Claude
    Code its ``metadata.user_id``), and none of these is content a model reads."""
    for key in _WHOSE:
        if isinstance(body.get(key), str):
            scan.slots.append(SlotRef(body, key, SlotKind.OTHER))
    notes = body.get("metadata")
    if isinstance(notes, dict):
        scan.slots.extend(SlotRef(notes, key, SlotKind.OTHER) for key, value in notes.items() if isinstance(value, str))


def objects(value: Any) -> Iterator[dict[str, Any]]:
    """Every object inside a JSON value, an object before those inside it."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from objects(v)
    elif isinstance(value, list):
        for v in value:
            yield from objects(v)


def rename(obj: dict[str, Any], names: dict[str, str], slots: Sequence[SlotRef]) -> bool:
    """``obj``'s keys as ``names`` gives them (a key it does not name stays), in place and in their order, and each slot
    of a value of ``obj`` told its key's new name. Two keys that would be one raise ``KeysCollide`` before anything
    changes. Whether a key changed."""
    new = [names.get(key, key) for key in obj]
    if new == list(obj):
        return False
    if len(set(new)) != len(new):
        raise KeysCollide()
    moved = dict(zip(obj, new, strict=True))
    items = list(obj.values())
    obj.clear()
    obj.update(zip(new, items, strict=True))
    for slot in slots:
        if slot.container is obj and slot.key in moved:
            slot.key = moved[slot.key]
    return True


def map_json_document(raw: str, transform: Callable[[str], str]) -> str:
    """Apply ``transform`` to every string value of a JSON document, keeping it valid JSON.

    When nothing changes the original text is returned byte for byte; otherwise the document is
    serialized compactly, as model tool-call arguments usually are. A document that gives a key twice
    in an object is serialized from what was read, whatever changed (``map_json_values``).
    """
    return map_json_values(raw, lambda value, _path, _number: transform(value))


def map_json_values(
    raw: str,
    transform: Callable[[str, str, bool], str],
    numbers: bool = False,
    keys: Callable[[str, str, str], str] | None = None,
) -> str:
    """Like :func:`map_json_document`, with the path of each value: ``transform(value, path, is_number)``.

    Paths name keys and mark arrays with ``[]`` (``items[].sku``). With ``numbers``, numbers are given
    as their text too, and a new text that reads as a number stays one. A path is made once for every
    value under it, and given to each as the same string: one made again for each value of a long key
    cost each value the key's length (``latet.engine.document``).

    ``keys``: each key of an object as it is to be written, ``keys(key, path, others)``, ``path`` the object's,
    ``others`` its keys, one to a line, that a fake drawn for the key must not be: a key can be a value (an address, a
    name), masked as values are. The paths given to ``transform`` name the keys as they came, so that a value is still
    found by its key's own name. Two keys of an object that would be one so raise ``KeysCollide``: an object holds a key
    once, and one of them would be lost.

    A document that gives a key twice in an object is read with its last copy, as Python's json reads
    it, and is never returned as it came: its text holds the other copy, which ``transform`` never saw
    (a model reads every copy). It is written anew from what was read, the copy masked alone.
    """
    try:
        found = wellformed.read(raw)
    except (ValueError, TypeError):
        return transform(raw, "", False)
    doc = found.doc
    changed = False
    paths: dict[tuple[str, str | None], str] = {}  # (a path, a key under it or None for an array's items): its path

    def under(path: str, key: str | None) -> str:
        found = paths.get((path, key))
        if found is None:
            found = paths[path, key] = f"{path}[]" if key is None else f"{path}.{key}" if path else str(key)
        return found

    def walk(v: Any, path: str) -> Any:
        nonlocal changed
        if isinstance(v, str):
            out = transform(v, path, False)
            changed = changed or out != v
            return out
        if numbers and isinstance(v, int | float) and not isinstance(v, bool):
            text = json.dumps(v)
            out = transform(text, path, True)
            if out == text:
                return v
            changed = True
            try:
                parsed = json.loads(out)
            except ValueError:
                return out
            return parsed if isinstance(parsed, int | float) and not isinstance(parsed, bool) else out
        if isinstance(v, dict):
            if keys is None:
                return {k: walk(x, under(path, k)) for k, x in v.items()}
            others = "\n".join(v)  # the object's keys, once: a fake drawn for one must be none of them
            named = [keys(k, path, others) for k in v]  # each key before the values under it
            if len(set(named)) != len(named):
                raise KeysCollide()
            changed = changed or named != list(v)
            return {new: walk(x, under(path, k)) for new, (k, x) in zip(named, v.items(), strict=True)}
        if isinstance(v, list):
            items = under(path, None)
            return [walk(x, items) for x in v]
        return v

    new = walk(doc, "")
    if not changed and not found.repeated:
        return raw
    return json.dumps(new, ensure_ascii=False, separators=(",", ":"))


def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
