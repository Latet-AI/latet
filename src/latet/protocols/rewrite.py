"""Restoring originals in responses: streaming event rewriters and static body restorers.

Events that need no change are forwarded as their exact bytes by the transport. Thinking and
reasoning content is never touched. Every completed model block is remembered (restored text →
masked bytes) so the history the client sends back maps to exactly what the model produced.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from latet.engine import wellformed
from latet.engine.rehydrate import Restore, StreamRehydrator, restore_text
from latet.protocols import sse


class _BufferBudget:
    """One stream's held JSON input and metadata, with headroom for reconstruction and wire serialization."""

    def __init__(self) -> None:
        self.limit = sse.MAX_DECODED
        self.held = 0
        self.reconstructed = 0

    def check(self, extra: int = 0) -> None:
        if self.held + extra > self.limit:
            raise sse.too_large(self.limit)

    def keep(self, size: int) -> None:
        self.check(size)
        self.held += size

    def release(self, size: int) -> None:
        self.held -= size

    def keep_reconstructed(self, size: int) -> None:
        self.keep(size)
        self.reconstructed += size

    def release_reconstructed(self) -> None:
        self.release(self.reconstructed)
        self.reconstructed = 0


def _quoted_size(text: str) -> int:
    """UTF-8 bytes of a compact JSON string, without constructing its escaped representation."""
    total = 2
    for char in text:
        number = ord(char)
        total += (
            2
            if char in '\\"\b\f\n\r\t'
            else 6
            if number < 32
            else 1
            if number < 128
            else 2
            if number < 2048
            else 3
            if number < 65536
            else 4
        )
    return total


def _utf8_size(text: str) -> int:
    """A candidate replacement can itself exceed the budget: do not encode a copy before admission."""
    if text.isascii():
        return len(text)
    return sum(1 if ord(char) < 128 else 2 if ord(char) < 2048 else 3 if ord(char) < 65536 else 4 for char in text)


def _unfinished() -> sse.ResponseError:
    return sse.ResponseError("the provider's tool arguments did not finish as a JSON document")


def _json_size(value: Any) -> int:
    """Size of compact UTF-8 JSON before asking the encoder to construct it."""
    if isinstance(value, str):
        return _quoted_size(value)
    if isinstance(value, dict):
        return 2 + max(0, len(value) - 1) + sum(_quoted_size(key) + 1 + _json_size(part) for key, part in value.items())
    if isinstance(value, list):
        return 2 + max(0, len(value) - 1) + sum(_json_size(part) for part in value)
    return len(json.dumps(value).encode("utf-8"))


@dataclass(slots=True)
class RestoreContext:
    text_restores: list[Restore]
    json_restores: list[Restore]
    remember: Callable[[str, str], None] = lambda masked, restored: None
    restored_ids: list[str] = field(default_factory=list)
    budget: _BufferBudget | None = None
    _admit: Callable[[str], None] | None = None
    _json_scope: bool = False

    def _admission(self, value: str) -> Callable[[str], None] | None:
        if self.budget is None:
            return None
        budget = self.budget
        size = 3 * len(value.encode("utf-8"))
        budget.check(size)

        def admit(piece: str) -> None:
            nonlocal size
            # Restored text, JSON escaping and the outer event's serialization coexist. Check before the
            # rehydrator retains a replacement or joins its output; the replacement itself is borrowed.
            size += 3 * _utf8_size(piece) + 4 * (_quoted_size(piece) - 2)
            budget.check(size)

        return admit

    def text(self, value: str) -> str:
        admit = self._admit or (self._admission(value) if self._json_scope else None)
        out, ids = restore_text(value, self.text_restores, _admit=admit)
        self.restored_ids.extend(ids)
        return out

    def json_text(self, value: str) -> str:
        """A JSON document the model wrote as a text (a tool call's arguments), restored where it is written, its keys
        too. Two keys of an object that would be one so (a fake beside the value it stands for) are each left as the
        model wrote them, as ``restore_leaves`` leaves them: the document is then restored string by string and written
        anew, compactly (an object holds a key once, and an agent would read the last of the two)."""
        admit = self._admission(value)
        out, ids = restore_text(value, self.json_restores, _admit=admit)
        if ids and _repeats(out):
            try:
                found = wellformed.read(value)
            except ValueError:
                found = None
            if found is not None and not found.repeated:
                previous = self._admit
                self._admit = admit
                try:
                    restored = restore_leaves(self, found.doc)
                    if self.budget is not None:
                        self.budget.check(3 * len(value.encode("utf-8")) + 15 * _json_size(restored))
                    out = json.dumps(restored, ensure_ascii=False, separators=(",", ":"))
                finally:
                    self._admit = previous
                ids = []
        if self.budget is not None:
            result_charge = 3 * _utf8_size(out) + 2 * _quoted_size(out)
            self.budget.check(3 * len(value.encode("utf-8")) + result_charge)
            self.budget.keep_reconstructed(result_charge)
        self.restored_ids.extend(ids)
        return out

    def stream(self, json_mode: bool) -> StreamRehydrator:
        return StreamRehydrator(self.json_restores if json_mode else self.text_restores)


def _repeats(raw: str) -> bool:
    """Whether an object of the JSON text ``raw`` gives a key twice (a text that is not JSON gives none)."""
    try:
        return wellformed.read(raw).repeated
    except ValueError:
        return False


def _remember_json_leaves(ctx: RestoreContext, masked_raw: str, restored_raw: str) -> None:
    """Pair the string leaves of a masked and a restored JSON document and remember each, and each key: a key is
    restored as a value is, so that sent back it leaves as the model wrote it too."""
    try:
        masked, restored = wellformed.loads(masked_raw), wellformed.loads(restored_raw)
    except ValueError:
        return

    def walk(m: Any, r: Any) -> None:
        if isinstance(m, str) and isinstance(r, str):
            ctx.remember(m, r)
        elif isinstance(m, dict) and isinstance(r, dict):
            if len(m) == len(r):  # restored key by key, in order: each key and its value paired with their own
                for (mk, mv), (rk, rv) in zip(m.items(), r.items(), strict=True):
                    ctx.remember(mk, rk)
                    walk(mv, rv)
                return
            for k in m:  # two keys restored into one: the others paired by their names
                if k in r:
                    walk(m[k], r[k])
        elif isinstance(m, list) and isinstance(r, list):
            for a, b in zip(m, r, strict=False):
                walk(a, b)

    previous = ctx._json_scope
    ctx._json_scope = True
    try:
        walk(masked, restored)
    finally:
        ctx._json_scope = previous


def restore_leaves(ctx: RestoreContext, value: Any) -> Any:
    """Restore every string inside a JSON value (tool_use.input objects), its keys too: a key the model wrote with a
    fake in it gets its value back, as a string does. Two keys that would be one so (a fake beside the value it stands
    for) are each left as the model wrote them: an object holds a key once, and a value would be lost."""
    if isinstance(value, str):
        return ctx.text(value)
    if isinstance(value, dict):
        keys = list(value)
        names = [ctx.text(k) for k in keys]
        while len(set(names)) != len(names):  # what one key restores to, another is: those keep their own
            counts = Counter(names)
            names = [key if counts[name] > 1 else name for key, name in zip(keys, names, strict=True)]
        return {name: restore_leaves(ctx, value[key]) for key, name in zip(keys, names, strict=True)}
    if isinstance(value, list):
        return [restore_leaves(ctx, v) for v in value]
    return value


class _Channel:
    __slots__ = ("charge", "ctx", "initial", "json_mode", "masked", "restored", "stream")

    def __init__(self, ctx: RestoreContext, json_mode: bool, identity: tuple = ()) -> None:
        self.ctx = ctx
        self.stream = ctx.stream(json_mode)
        self.json_mode = json_mode
        self.masked: list[str] = []
        self.restored: list[str] = []
        self.charge = 0
        self.initial = False
        if json_mode and ctx.budget is not None:
            self.charge = 128 + len(json.dumps(identity, ensure_ascii=False).encode("utf-8"))
            ctx.budget.keep(self.charge)

    def feed(self, piece: str) -> str:
        if self.json_mode:
            if self.initial and piece:
                raise sse.ResponseError("the provider supplied two sources for one tool's arguments")
            if piece:
                size = len(piece.encode("utf-8")) + 64
                if self.ctx.budget is not None:
                    self.ctx.budget.keep(size)
                self.charge += size
                self.masked.append(piece)
            return ""
        self.masked.append(piece)
        out = self.stream.feed(piece)
        self.restored.append(out)
        return out

    def flush(self) -> str:
        if self.json_mode:
            if self.ctx.budget is not None:
                self.ctx.budget.check(sum(len(piece.encode("utf-8")) for piece in self.masked))
            raw = "".join(self.masked)
            try:
                wellformed.read(raw)
            except (ValueError, RecursionError):
                raise _unfinished() from None
            out = self.ctx.json_text(raw)
            self.restored = [out]
            return out
        out = self.stream.flush()
        self.restored.append(out)
        return out

    def finish(self, ctx: RestoreContext) -> None:
        if not self.json_mode:
            ctx.restored_ids.extend(self.stream.restored)
        masked, restored = "".join(self.masked), "".join(self.restored)
        if self.json_mode:
            _remember_json_leaves(ctx, masked, restored)
        else:
            ctx.remember(masked, restored)
        if self.json_mode and ctx.budget is not None:
            ctx.budget.release(self.charge)
        self.masked.clear()
        self.restored.clear()


class _Stream:
    def __init__(self, ctx: RestoreContext) -> None:
        self.ctx = ctx
        self.budget = ctx.budget = _BufferBudget()
        self.channels: dict[Any, _Channel] = {}

    def close(self) -> None:
        self.channels.clear()

    def on_end(self) -> list[dict[str, Any]]:
        if any(channel.json_mode for channel in self.channels.values()):
            raise _unfinished()
        self.close()
        return []


# --- Anthropic Messages ----------------------------------------------------------------------------


class AnthropicStream(_Stream):
    """``content_block_*`` events: text and tool_use blocks restored; thinking untouched."""

    def __init__(self, ctx: RestoreContext) -> None:
        super().__init__(ctx)

    def on_event(self, event: dict[str, Any]) -> list[dict[str, Any]] | None:
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            index = event.get("index", 0)
            if block.get("type") == "text":
                self.channels[index] = _Channel(self.ctx, json_mode=False)
                if block.get("text"):
                    out = copy.deepcopy(event)
                    out["content_block"]["text"] = self.channels[index].feed(block["text"])
                    return [out]
            elif block.get("type") == "tool_use":
                self.channels[index] = _Channel(self.ctx, json_mode=True, identity=(index,))
                if block.get("input"):
                    channel = self.channels[index]
                    channel.feed(json.dumps(block["input"], ensure_ascii=False, separators=(",", ":")))
                    channel.initial = True
                    out = copy.deepcopy(event)
                    out["content_block"]["input"] = {}
                    return [out]
            return None
        if etype == "content_block_delta":
            channel = self.channels.get(event.get("index", 0))
            delta = event.get("delta") or {}
            if channel is None:
                return None
            key = {"text_delta": "text", "input_json_delta": "partial_json"}.get(delta.get("type", ""))
            if key is None or not isinstance(delta.get(key), str):
                return None
            restored = channel.feed(delta[key])
            if restored == delta[key]:
                return None
            if not restored:
                return []
            out = copy.deepcopy(event)
            out["delta"][key] = restored
            return [out]
        if etype == "content_block_stop":
            index = event.get("index", 0)
            channel = self.channels.pop(index, None)
            if channel is None:
                return None
            tail = channel.flush()
            channel.finish(self.ctx)
            if not tail:
                return None
            key, dtype = ("partial_json", "input_json_delta") if channel.json_mode else ("text", "text_delta")
            synthetic = {"type": "content_block_delta", "index": index, "delta": {"type": dtype, key: tail}}
            return [synthetic, event]
        if etype in ("message_stop", "error"):
            self.on_end()
        return None


def restore_anthropic_message(ctx: RestoreContext, body: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(body)
    for block in out.get("content") or []:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            original = block["text"]
            block["text"] = ctx.text(original)
            ctx.remember(original, block["text"])
        elif block.get("type") == "tool_use" and "input" in block:
            masked = block["input"]
            block["input"] = restore_leaves(ctx, masked)
            _remember_json_leaves(ctx, json.dumps(masked), json.dumps(block["input"]))
    return out


# --- OpenAI Chat Completions ---------------------------------------------------------------------------


class ChatStream(_Stream):
    def __init__(self, ctx: RestoreContext) -> None:
        super().__init__(ctx)
        self.last: dict[str, Any] | None = None
        self.last_charge = 0

    def _last(self, chunk: dict[str, Any] | None) -> None:
        self.budget.release(self.last_charge)
        self.last_charge = 0
        self.last = None
        if chunk is not None:
            metadata = {k: v for k, v in chunk.items() if k in ("id", "object", "created", "model")}
            charge = _json_size(metadata) + 128
            self.budget.keep(charge)
            self.last, self.last_charge = metadata, charge

    def close(self) -> None:
        super().close()
        self._last(None)

    def _channel(self, key: tuple, json_mode: bool) -> _Channel:
        if key not in self.channels:
            self.channels[key] = _Channel(self.ctx, json_mode, identity=key)
        return self.channels[key]

    def on_chunk(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        self._last(chunk)
        if "error" in chunk:
            self.on_end()
        changed = False
        out = copy.deepcopy(chunk)
        for choice in out.get("choices") or []:
            ci = choice.get("index", 0)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                restored = self._channel((ci, "content"), False).feed(delta["content"])
                changed |= restored != delta["content"]
                delta["content"] = restored
            for call in delta.get("tool_calls") or []:
                fn = call.get("function") or {}
                if isinstance(fn.get("arguments"), str):
                    channel = self._channel((ci, "tool", call.get("index", 0)), True)
                    restored = channel.feed(fn["arguments"])
                    changed |= restored != fn["arguments"]
                    fn["arguments"] = restored
            if choice.get("finish_reason"):
                for key in [k for k in self.channels if k[0] == ci]:
                    channel = self.channels.pop(key)
                    tail = channel.flush()
                    channel.finish(self.ctx)
                    if not tail:
                        continue
                    changed = True
                    choice.setdefault("delta", {})
                    if key[1] == "content":
                        choice["delta"]["content"] = (choice["delta"].get("content") or "") + tail
                    else:
                        choice["delta"].setdefault("tool_calls", []).append(
                            {"index": key[2], "function": {"arguments": tail}}
                        )
        if not self.channels:
            self._last(None)
        return out if changed else None

    def on_done(self) -> dict[str, Any] | None:
        """Leftovers when no chunk carried a finish_reason: one synthetic chunk before ``[DONE]``."""
        if not self.channels:
            return None
        base = {k: v for k, v in (self.last or {}).items() if k in ("id", "object", "created", "model")}
        choices = []
        for key, channel in self.channels.items():
            tail = channel.flush()
            channel.finish(self.ctx)
            if tail and key[1] == "content":
                choices.append({"index": key[0], "delta": {"content": tail}, "finish_reason": None})
            elif tail:
                choices.append(
                    {"index": key[0], "delta": {"tool_calls": [{"index": key[2], "function": {"arguments": tail}}]}}
                )
        self.channels.clear()
        self._last(None)
        return {**base, "choices": choices} if choices else None


def restore_chat_completion(ctx: RestoreContext, body: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(body)
    for choice in out.get("choices") or []:
        message = choice.get("message") or {}
        if isinstance(message.get("content"), str):
            original = message["content"]
            message["content"] = ctx.text(original)
            ctx.remember(original, message["content"])
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if isinstance(fn.get("arguments"), str):
                masked = fn["arguments"]
                fn["arguments"] = ctx.json_text(masked)
                _remember_json_leaves(ctx, masked, fn["arguments"])
    return out


# --- OpenAI Responses ----------------------------------------------------------------------------------

_RESPONSES_DELTAS = {
    "response.output_text.delta": ("delta", False, "content_index"),
    "response.function_call_arguments.delta": ("delta", True, "args"),
    "response.custom_tool_call_input.delta": ("delta", False, "input"),
}
_RESPONSES_DONE = {
    "response.output_text.done": ("text", False, "content_index"),
    "response.function_call_arguments.done": ("arguments", True, "args"),
    "response.custom_tool_call_input.done": ("input", False, "input"),
}


def restore_response_item(ctx: RestoreContext, item: dict[str, Any], remember: bool = False) -> dict[str, Any]:
    out = copy.deepcopy(item)
    itype = out.get("type")
    if itype == "message":
        for part in out.get("content") or []:
            if part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                masked = part["text"]
                part["text"] = ctx.text(masked)
                if remember:
                    ctx.remember(masked, part["text"])
    elif itype in ("function_call", "mcp_call") and isinstance(out.get("arguments"), str):
        masked = out["arguments"]
        out["arguments"] = ctx.json_text(masked)
        if remember:
            _remember_json_leaves(ctx, masked, out["arguments"])
    elif itype == "custom_tool_call" and isinstance(out.get("input"), str):
        masked = out["input"]
        out["input"] = ctx.text(masked)
        if remember:
            ctx.remember(masked, out["input"])
    elif itype in ("local_shell_call", "shell_call") and isinstance(out.get("action"), dict):
        out["action"] = restore_leaves(ctx, out["action"])
    return out


class ResponsesStream(_Stream):
    """Responses events over SSE or WebSocket. Deltas are restored in place; when a channel ends with
    text still held back, the last delta of that channel (kept one event late) carries it, so the
    event count and ``sequence_number`` stay as the server sent them."""

    def __init__(self, ctx: RestoreContext) -> None:
        super().__init__(ctx)
        self.held: tuple[tuple, dict[str, Any], str] | None = None  # (channel, event, key)
        self.held_charge = 0
        self.seq_offset = 0

    def _emit_held(self, tail: str = "") -> list[dict[str, Any]]:
        if self.held is None:
            return []
        _, event, key = self.held
        self.held = None
        self.budget.release(self.held_charge)
        self.held_charge = 0
        if tail:
            event[key] = event[key] + tail
        return [event]

    def _renumber(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.seq_offset and isinstance(event.get("sequence_number"), int):
            event = dict(event)
            event["sequence_number"] += self.seq_offset
        return event

    def on_event(self, event: dict[str, Any]) -> list[dict[str, Any]] | None:
        etype = event.get("type", "")
        if etype in ("response.completed", "response.done", "response.incomplete", "error") and any(
            channel.json_mode for channel in self.channels.values()
        ):
            raise _unfinished()
        if etype in _RESPONSES_DELTAS:
            key, json_mode, sub = _RESPONSES_DELTAS[etype]
            chan_key = (event.get("item_id"), sub, event.get(sub) if sub == "content_index" else sub)
            if chan_key not in self.channels:
                self.channels[chan_key] = _Channel(self.ctx, json_mode, identity=chan_key)
            channel = self.channels[chan_key]
            out = dict(event)
            out[key] = channel.feed(event.get(key) or "")
            emitted = self._emit_held()
            self.held_charge = _json_size(out) + 128
            self.budget.keep(self.held_charge)
            self.held = (chan_key, self._renumber(out), key)
            return emitted
        if etype in _RESPONSES_DONE:
            key, json_mode, sub = _RESPONSES_DONE[etype]
            chan_key = (event.get("item_id"), sub, event.get(sub) if sub == "content_index" else sub)
            channel = self.channels.pop(chan_key, None)
            emitted: list[dict[str, Any]] = []
            tail = channel.flush() if channel else ""
            if channel:
                channel.finish(self.ctx)
            if self.held is not None and self.held[0] == chan_key:
                emitted += self._emit_held(tail)
            else:
                emitted += self._emit_held()
                if tail:  # rare: interleaved channels; insert one delta and shift later numbers
                    delta_type = etype.replace(".done", ".delta")
                    synthetic = {"type": delta_type, "item_id": event.get("item_id"), "delta": tail}
                    if "output_index" in event:
                        synthetic["output_index"] = event["output_index"]
                    if sub == "content_index":
                        synthetic.update(output_index=event.get("output_index"), content_index=event.get(sub))
                    if isinstance(event.get("sequence_number"), int):
                        synthetic["sequence_number"] = event["sequence_number"] + self.seq_offset
                        self.seq_offset += 1
                    emitted.append(synthetic)
            out = dict(event)
            if isinstance(event.get(key), str):
                out[key] = self.ctx.json_text(event[key]) if json_mode else self.ctx.text(event[key])
            emitted.append(self._renumber(out))
            return emitted
        emitted = self._emit_held()
        out = event
        if etype in ("response.output_item.added", "response.output_item.done") and isinstance(event.get("item"), dict):
            out = dict(event)
            out["item"] = restore_response_item(self.ctx, event["item"], remember=etype.endswith(".done"))
        elif etype in ("response.content_part.added", "response.content_part.done") and isinstance(
            event.get("part"), dict
        ):
            part = event["part"]
            if isinstance(part.get("text"), str):
                out = dict(event)
                out["part"] = {**part, "text": self.ctx.text(part["text"])}
        elif etype in ("response.completed", "response.done", "response.incomplete") and isinstance(
            event.get("response"), dict
        ):
            response = event["response"]
            if isinstance(response.get("output"), list):
                out = dict(event)
                out["response"] = {
                    **response,
                    "output": [restore_response_item(self.ctx, i) for i in response["output"]],
                }
        out = self._renumber(out)
        if out is event and not emitted:
            return None
        return [*emitted, out]

    def on_end(self) -> list[dict[str, Any]]:
        if any(channel.json_mode for channel in self.channels.values()):
            raise _unfinished()
        emitted = self._emit_held()
        super().on_end()
        return emitted

    def close(self) -> None:
        super().close()
        self.held = None
        self.held_charge = 0


def restore_response_body(ctx: RestoreContext, body: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(body)
    if isinstance(out.get("output"), list):
        out["output"] = [restore_response_item(ctx, i, remember=True) for i in out["output"]]
    return out


# --- Google's Gemini API ----------------------------------------------------------------------------------------------

# What every event of a Gemini stream repeats, and every candidate of it: an event that holds nothing else, its text
# all held back, is not sent (the next one carries the text). The rest (a prompt's feedback, grounding, citations, a
# finish) is sent, its text flushed so that no part is empty.
_GEMINI_REPEATED = frozenset(
    {
        "candidates",
        "usageMetadata",
        "usage_metadata",
        "modelVersion",
        "model_version",
        "responseId",
        "response_id",
        "createTime",
        "create_time",
        "modelStatus",
        "model_status",
    }
)
_GEMINI_CANDIDATE_REPEATED = frozenset({"index", "content", "safetyRatings", "safety_ratings"})


def _gemini_field(obj: dict[str, Any], name: str) -> tuple[str | None, Any]:
    """``obj``'s field under its camelCase name (what Google answers in) or its snake_case one: (its key, its value)."""
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name)
    for key in (name, snake):
        if key in obj:
            return key, obj[key]
    return None, None


def _gemini_content(candidate: Any) -> dict[str, Any] | None:
    """A candidate's content, when it holds a list of parts."""
    if not isinstance(candidate, dict):
        return None
    _, content = _gemini_field(candidate, "content")
    return content if isinstance(content, dict) and isinstance(content.get("parts"), list) else None


def _gemini_parts(candidate: Any) -> list[Any] | None:
    content = _gemini_content(candidate)
    return content["parts"] if content is not None else None


def _plain(part: Any) -> bool:
    """A part of text the model wrote for the reader: not a thought, and carrying no signature."""
    return (
        isinstance(part, dict)
        and isinstance(part.get("text"), str)
        and not part.get("thought")
        and _gemini_field(part, "thoughtSignature")[0] is None
    )


def _restore_call(ctx: RestoreContext, part: dict[str, Any], remember: bool) -> None:
    """A whole function call's arguments, restored in place, keys too; remembered leaf by leaf when ``remember``."""
    key, call = _gemini_field(part, "functionCall")
    if key is None or not isinstance(call, dict):
        return
    args_key, args = _gemini_field(call, "args")
    if args_key is None or not isinstance(args, dict):
        return
    restored = restore_leaves(ctx, args)
    if remember:
        _remember_json_leaves(ctx, json.dumps(args), json.dumps(restored))
    call[args_key] = restored


def restore_gemini_answer(ctx: RestoreContext, body: dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any]:
    """A whole answer (``generateContent``), or an array of them (``streamGenerateContent`` without ``alt=sse``, a
    stream sent at once, restored as one: ``GeminiStream``), restored in a copy: each candidate's text and each
    function call's arguments, remembered so that, sent back, they leave as the model wrote them; thoughts as the model
    wrote them."""
    if isinstance(body, list):
        stream = GeminiStream(ctx)
        events: list[Any] = []
        for answer in body:
            restored = stream.on_event(answer) if isinstance(answer, dict) else None
            events.extend([copy.deepcopy(answer)] if restored is None else restored)
        return events + stream.on_end()
    out = copy.deepcopy(body)
    candidates = _gemini_field(out, "candidates")[1]
    for candidate in candidates if isinstance(candidates, list) else []:
        for part in _gemini_parts(candidate) or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and not part.get("thought"):
                masked = part["text"]
                part["text"] = ctx.text(masked)
                ctx.remember(masked, part["text"])
            else:
                _restore_call(ctx, part, remember=True)
    return out


class GeminiStream(_Stream):
    """Gemini's events (``alt=sse``), each a whole answer holding only the new parts. Each candidate's text goes through
    a channel of its own, so that a stand-in cut between two events comes back whole; what is held back goes out before
    the next part of another kind, at the candidate's finish, or at the stream's end. Every event sent is one Gemini CLI
    and Google's JS SDK accept: no part that is empty, no candidate without parts, a signed part never merged with
    another, and kept where the model put it."""

    def _flush(self, index: Any) -> str:
        channel = self.channels.pop((index, "text"), None)
        if channel is None:
            return ""
        tail = channel.flush()
        channel.finish(self.ctx)
        return tail

    def _close(self, index: Any, path: str) -> str:
        """The end of one argument a call streams: what its channel held, its whole value remembered."""
        channel = self.channels.pop((index, "args", path), None)
        if channel is None:
            return ""
        tail = channel.flush()
        channel.finish(self.ctx)
        return tail

    def _streamed_args(self, index: Any, part: dict[str, Any]) -> None:
        """Vertex's streamed arguments (``partialArgs``): each argument's text pieces through a channel of their own,
        named by the argument's path, the call being the candidate's open one (its later events carry no name). A piece
        that ends its argument, and the call's end, flush what is held into that piece, or into one added for it."""
        key, call = _gemini_field(part, "functionCall")
        if key is None or not isinstance(call, dict):
            return
        pieces_key, pieces = _gemini_field(call, "partialArgs")
        for piece in pieces if isinstance(pieces, list) else []:
            if not isinstance(piece, dict) or not isinstance(piece.get("jsonPath"), str):
                continue
            value_key, value = _gemini_field(piece, "stringValue")
            if value_key is None or not isinstance(value, str):
                continue
            path = piece["jsonPath"]
            channel = self.channels.get((index, "args", path))
            if channel is None:
                channel = self.channels[(index, "args", path)] = _Channel(self.ctx, json_mode=False)
            piece[value_key] = channel.feed(value)
            if not _gemini_field(piece, "willContinue")[1]:
                piece[value_key] += self._close(index, path)
        if _gemini_field(call, "willContinue")[1]:
            return
        open_paths = [k[2] for k in self.channels if k[0] == index and k[1] == "args"]
        added = [{"jsonPath": path, "stringValue": tail} for path in open_paths if (tail := self._close(index, path))]
        if added:
            call.setdefault(pieces_key or "partialArgs", []).extend(added)

    def _text(self, index: Any, part: dict[str, Any]) -> str:
        channel = self.channels.get((index, "text"))
        if channel is None:
            channel = self.channels[(index, "text")] = _Channel(self.ctx, json_mode=False)
        return channel.feed(part["text"])

    def _candidate(self, candidate: dict[str, Any], index: Any, parts: list[Any]) -> tuple[list[Any], bool]:
        """A candidate's parts as they go out, and whether any changed."""
        out: list[Any] = []
        changed = False
        for part in parts:
            if _plain(part):
                restored = self._text(index, part)
                changed |= restored != part["text"]
                if restored:
                    out.append({**part, "text": restored})
                continue
            tail = self._flush(index)  # what was held goes out before a part of another kind
            if tail:
                out.append({"text": tail})
                changed = True
            if not isinstance(part, dict):
                out.append(part)
            elif isinstance(part.get("text"), str) and not part.get("thought"):  # signed: restored whole, in place
                restored = self.ctx.text(part["text"])
                self.ctx.remember(part["text"], restored)
                changed |= restored != part["text"]
                out.append({**part, "text": restored})
            else:
                before = json.dumps(part, sort_keys=True)
                part = copy.deepcopy(part)
                _restore_call(self.ctx, part, remember=True)
                self._streamed_args(index, part)
                changed |= json.dumps(part, sort_keys=True) != before
                out.append(part)
        if _gemini_field(candidate, "finishReason")[1]:
            tail = self._flush(index)
            if tail:
                changed = True
                if out and _plain(out[-1]):
                    out[-1] = {**out[-1], "text": out[-1]["text"] + tail}
                else:
                    out.append({"text": tail})
        return out, changed

    def on_event(self, event: dict[str, Any]) -> list[dict[str, Any]] | None:
        key, candidates = _gemini_field(event, "candidates")
        if key is None or not isinstance(candidates, list):
            return None
        out = copy.deepcopy(event)
        changed = False
        emptied: list[tuple[dict[str, Any], Any, list[Any]]] = []  # every part held back: content, index, parts
        for position, candidate in enumerate(out[key]):
            content = _gemini_content(candidate)
            if content is None:  # a finish with no content of its own carries what its candidate still holds
                finished = isinstance(candidate, dict) and _gemini_field(candidate, "finishReason")[1]
                if finished and (tail := self._flush(candidate.get("index", position))):
                    candidate["content"] = {"role": "model", "parts": [{"text": tail}]}
                    changed = True
                continue
            index = candidate.get("index", position)
            parts = content["parts"]
            restored, touched = self._candidate(candidate, index, parts)
            changed |= touched
            content["parts"] = restored
            if parts and not restored:
                emptied.append((content, index, parts))
        if not emptied:
            return [out] if changed else None
        repeated = set(out) <= _GEMINI_REPEATED and all(
            not isinstance(c, dict) or set(c) <= _GEMINI_CANDIDATE_REPEATED for c in out[key]
        )
        if repeated and all(not _gemini_parts(c) for c in out[key]):
            return []  # nothing but text held back: the next event carries it
        for content, index, parts in emptied:  # sent all the same: what is held goes out, so no candidate is empty
            tail = self._flush(index)
            content["parts"] = [{"text": tail}] if tail else parts  # nothing held: its parts as Google sent them
        return [out]

    def on_end(self) -> list[dict[str, Any]]:
        """What the candidates still hold when the stream ends, text and streamed arguments, in one event."""
        held: dict[Any, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        for key in list(self.channels):
            texts, pieces = held.setdefault(key[0], ([], []))
            if key[1] == "text":
                if tail := self._flush(key[0]):
                    texts.append({"text": tail})
            elif tail := self._close(key[0], key[2]):
                pieces.append({"jsonPath": key[2], "stringValue": tail})
        self.close()
        candidates = [
            {
                "index": index,
                "content": {
                    "role": "model",
                    "parts": texts + ([{"functionCall": {"partialArgs": pieces}}] if pieces else []),
                },
            }
            for index, (texts, pieces) in held.items()
            if texts or pieces
        ]
        return [{"candidates": candidates}] if candidates else []
