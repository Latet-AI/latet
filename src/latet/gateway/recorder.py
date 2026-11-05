"""Turn each exchange into a dashboard record: what was sent (block by block), what came back,
how long Latet took, and the token usage the provider reported.

Recording runs on its own thread once an exchange is over: it never delays the traffic it observes,
and a failure there is logged, never raised.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import logging
import re
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, NamedTuple

from latet import observe
from latet.engine import wellformed
from latet.engine.modules import kept, module_of, origin
from latet.engine.pipeline import LRU, Engine
from latet.engine.rehydrate import Annotator, Restore, restores_for
from latet.engine.response_guard import safe_text, safe_tree
from latet.engine.types import Replacement, SlotKind
from latet.engine.vault import Vault
from latet.gateway.cloud import shape_of
from latet.gateway.headers import redacted
from latet.gateway.launch_authority import LAUNCH_CATEGORIES
from latet.gateway.outcome import BLOCK_CODES, Observation
from latet.gateway.response_encoding import (
    ResponseEncodingError,
    brotli_parts,
    inflate,
    prove_brotli,
    prove_zlib,
    prove_zstd,
    zstd_parts,
)
from latet.protocols import codec
from latet.protocols.base import tool_source
from latet.protocols.eventstream import EventStreamDecoder
from latet.protocols.sse import MAX_EVENTS, SSEParser, StreamDecoder
from latet.state.local import LocalJobLock, LocalSentBlocks
from latet.storage.exchanges import (
    MAX_IMAGE,
    BlockRecord,
    ExchangeRecord,
    ImageRecord,
    configured_destination,
    history_coverage_summary,
    history_project,
    new_exchange_id,
    safe_leaks,
    valid_project_id,
)

if TYPE_CHECKING:
    from latet.engine.verifier import Leak
    from latet.gateway.mediate import Prepared
    from latet.state.protocols import Feed, HistoryStore, JobLock, SentBlocks

log = logging.getLogger(__name__)
_MAX_RESPONSE = 2_000_000  # characters of response text kept for the dashboard
_MAX_JSON_BODY = 8_000_000  # bytes of a non-streamed response body parsed for the dashboard
_PRUNE_EVERY = 6 * 3600
# Refused the pruning's lock, a process prunes all the same what is this much past the retention: whoever holds the lock
# would have pruned it (someone who writes a server's Valkey can hold it for ever), and pruning twice is harmless. Read
# from the history's own dates, so that a recorder made a moment ago (a space built again) does it at its first try.
_PRUNE_OVERDUE = 86400.0
_PRUNE_RETRY = 60.0  # seconds after a failure of its own that the clock asks for the history's prune again
_MAX_QUEUED = 500  # exchanges waiting to be recorded; beyond this the dashboard skips some


def agent_of(headers: dict[str, str], path: str = "") -> tuple[str, str | None]:
    ua = headers.get("user-agent", "")
    originator = headers.get("originator", "")

    def version() -> str | None:
        return ua.split("/", 1)[1].split(" ", 1)[0] if "/" in ua else None

    if ua.startswith("claude-cli/") or "x-claude-code-session-id" in headers:
        return "claude-code", version()
    if ua.startswith("Bun/") and path.endswith("/api/hello"):
        return "claude-code", None  # its connectivity check, sent with the runtime's default user agent
    if ua.startswith("aws-sdk-js/") and "/inference-profiles" in path:
        return "claude-code", None  # the model list it asks Bedrock for at start, with the AWS SDK's user agent
    if "codex" in originator.lower() or ua.lower().startswith("codex"):
        return "codex", headers.get("version") or version()
    if ua.lower().startswith("opencode"):
        return "opencode", version()
    if ua.startswith("GeminiCLI"):  # GeminiCLI/<version>, or GeminiCLI-<mode>/<version>/<model> (...)
        found = re.match(r"GeminiCLI(?:-[\w-]+)?/([^/\s]+)", ua)
        return "gemini-cli", found.group(1) if found else None
    if ua.startswith("tutorial-agent/"):
        return "tutorial-agent", version()  # the tutorial's pretend agent
    return "other", None


def replacement_dicts(replacements: Iterable[Replacement], vault: Vault | None = None) -> list[dict[str, Any]]:
    """Types, rules and offsets in the masked text; never the values themselves.

    A value found again (already in the vault, or repeated in the text) shows the rule that found it
    first, from ``vault``.
    """
    out, delta = [], 0
    for r in replacements:
        masked_start = r.start + delta
        entity = vault.get(r.entity_id) if vault is not None and r.entity_id else None
        out.append(
            {
                "masked_start": masked_start,
                "masked_end": masked_start + len(r.replacement),
                "type": r.etype,
                "class": r.eclass.name,
                "entity": r.entity_id,
            }
            | origin(r.rule, entity.rule if entity else None, r.etype)
        )
        delta += len(r.replacement) - (r.end - r.start)
    return out


def json_paths(
    value: Any, path: str = "$", out: dict[tuple[int, Any], str] | None = None
) -> dict[tuple[int, Any], str]:
    """JSON path of every (container, key) in a document, to label the blocks the dashboard shows."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out[(id(value), k)] = f"{path}.{k}"
            if isinstance(v, dict | list):
                json_paths(v, f"{path}.{k}", out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out[(id(value), i)] = f"{path}[{i}]"
            if isinstance(v, dict | list):
                json_paths(v, f"{path}[{i}]", out)
    return out


def record_positions(value: Any, needed: set[tuple[int, Any]]) -> dict[tuple[int, Any], tuple[int, ...]]:
    """Worker-only occurrence identities for selected references, without paths or payload copies. Each integer is
    a child's position in its object/list, so a repeated reference reuses its occurrence and equal values cannot
    impersonate it. Detached references stay absent, as they do in ``json_paths``."""
    out: dict[tuple[int, Any], tuple[int, ...]] = {}

    def walk(value: Any, parent: tuple[int, ...]) -> None:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for n, (key, item) in enumerate(items):
            position = (*parent, n)
            identity = (id(value), key)
            if identity in needed:
                out[identity] = position
            if isinstance(item, dict | list):
                walk(item, position)

    if needed and isinstance(value, dict | list):
        walk(value, ())
    return out


def _looks_json(text: str) -> bool:
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def reconstruct(masked: str, replacements: list[dict[str, Any]], vault: Vault) -> tuple[str, list[dict[str, Any]]]:
    """The original of a masked text, from the vault as it is now.

    A value the vault does not give back stays as it was sent, its replacement marked ``known: false``:
    one forgotten since, and one redacted one way, whose replacement names no entry. The vault holds
    none for a value the detectors redacted; one the local reviewer or the Live Reviewer found under
    ``redact`` it keeps all the same, sealed, with a fake that never leaves, and that is not put back
    here either; nor is a live launch key, kept sealed in the vault for the agent alone, never shown to a person, in
    the Live Reviewer or the history (P2-6a's re-review, R-1). Returned replacements carry offsets on both sides.
    """
    out: list[str] = []
    spans: list[dict[str, Any]] = []
    pos = size = 0
    forms: dict[tuple[str, bool], dict[str, str]] = {}
    for r in sorted(replacements, key=lambda r: r["masked_start"]):
        start, end = r["masked_start"], r["masked_end"]
        if start < pos or end > len(masked):
            continue
        out.append(masked[pos:start])
        size += start - pos
        piece = masked[start:end]
        entity = vault.get(r["entity"]) if r.get("entity") else None
        if entity is not None and not safe_text(entity.original):
            entity = None  # a live launch key: its stand-in stays
        if "via" not in r:  # recorded before modules and first rules were kept
            r = r | origin(r.get("rule"), entity.rule if entity else None, r.get("type"))
        if entity is None:
            original = piece
        else:
            key = (entity.id, bool(r.get("json")))
            if key not in forms:
                forms[key] = {x.pattern: x.replacement for x in restores_for([entity], json_mode=key[1])}
            original = forms[key].get(piece, entity.original)
        spans.append({**r, "start": size, "end": size + len(original), "known": entity is not None})
        out.append(original)
        size += len(original)
        pos = end
    out.append(masked[pos:])
    return "".join(out), spans


def kept_source(source: str) -> str | None:
    """Where a tool's output came from, as the history keeps it: a file's path whole, a web address by its scheme and
    host alone (``https://crm.example.com/``), what a rule of `data_hosts` reads. Its user, port, path, query and
    fragment are left out: an address can carry a person's email or a token in them. None: an address with no host,
    which no rule names."""
    try:
        parts = urllib.parse.urlsplit(source)
        if not parts.scheme or not parts.netloc:
            return source  # a file's path (``C:\\work`` reads as the scheme ``c``, and has no host)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return f"{parts.scheme}://{f'[{host}]' if ':' in host else host}/"


def restored_source(kept: Any, vault: Vault) -> str | None:
    """Where a tool's output came from, as the history keeps it (``block_ref``: its text, or its text and where values
    are masked in it), with those values from the vault as it is now: one forgotten since stays as its fake."""
    if isinstance(kept, str):
        return kept  # nothing masked in it, or kept by a Latet that kept it in clear
    if isinstance(kept, list) and len(kept) == 2 and isinstance(kept[0], str) and isinstance(kept[1], list):
        return reconstruct(kept[0], kept[1], vault)[0]
    return None


def original_range(spans: list[dict[str, Any]], start: int, end: int) -> tuple[int, int]:
    """Where a range of a masked text is in the original ``reconstruct`` rebuilt from it.

    ``spans`` are the replacements ``reconstruct`` returned, in order. The values masked before the
    range move it; a range that cuts into a masked value takes the whole value in.
    """

    def moved(offset: int, edge: str) -> int:
        shift = 0
        for span in spans:
            if offset >= span["masked_end"]:
                shift = span["end"] - span["masked_end"]
            elif offset > span["masked_start"]:
                return span[edge]  # inside a value: its start, or its end
            else:
                break
        return offset + shift

    return moved(start, "start"), moved(end, "end")


class Broadcaster:
    """Fan-out of new exchange summaries to the dashboard's live feeds (event-loop thread only)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Any]] = set()

    def subscribe(self) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        self._queues.discard(queue)

    def publish(self, payload: Any, event: str = "exchange") -> None:
        """Send ``payload`` to every viewer: an exchange's summary, or another ``event`` (a ``review``)."""
        item = payload if event == "exchange" else (event, payload)
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):  # a stalled viewer misses events, nothing more
                queue.put_nowait(item)


@dataclass(slots=True, repr=False)
class _ResponsePiece:
    text: str
    starts_lane: bool = False


@dataclass(slots=True, repr=False)
class _ResponseLane:
    pieces: list[_ResponsePiece] = field(default_factory=list)


@dataclass(repr=False)
class ResponseCapture:
    """One bounded, private response owner. Only ``screen`` admits its payload.

    HTTP is coalesced into at most 123 blocks of 64 KiB; WS keeps original
    message boundaries. Descriptor reservations precede retention: 128 bytes
    per raw block, 256 per WS boundary/piece, 512 plus four bytes per key
    character per lane/tool/header. Shared semantic pieces preserve ordering
    without another full raw buffer. The 64-lane index includes ended lanes.
    """

    protocol: str | None
    content_type: str = ""
    content_encoding: str | None = None
    text: list[str] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    size: int = 0
    done: bool = False
    failed: bool = False
    broken: bool = False
    omission: str | None = None
    safe: bool = False
    headers: tuple[tuple[str, str], ...] = ()
    _raw: list[bytes | str] = field(default_factory=list)
    _raw_bytes: int = 0
    _metadata: int = 2048  # owner, bounded scalar/parser/container roots
    _ended: bool = False
    _settled: bool = False
    _websocket: bool = False
    _events: SSEParser = field(default_factory=lambda: SSEParser(limit=_MAX_JSON_BODY))
    _body: bytearray = field(default_factory=bytearray)
    _zlib_bits: int | None = None
    _frames: EventStreamDecoder | None = None
    _lanes: dict[tuple[str, ...], _ResponseLane] = field(default_factory=dict)
    _rendered: list[_ResponsePiece] = field(default_factory=list)
    _render_lane: _ResponseLane | None = None
    _history_transitions: tuple[bool, ...] = ()
    _calls: dict[tuple[str, ...], tuple[tuple[str, ...], tuple[str, ...], bool]] = field(default_factory=dict)
    _blocks: dict[str, bool] = field(default_factory=dict)
    _event_count: int = 0

    def __post_init__(self) -> None:
        if not self._charge(4 * (len(self.content_type) + len(self.content_encoding or ""))):
            self.content_type = ""
            self.content_encoding = None

    def _charge(self, amount: int) -> bool:
        if self._metadata + amount > 65536:
            self.omit("response-budget")
            return False
        self._metadata += amount
        return True

    def metadata(self, headers: Iterable[tuple[str, str]]) -> None:
        kept: list[tuple[str, str]] = []
        for name, value in headers:
            if not self._charge(256 + 4 * (len(name) + len(value))):
                break
            kept.append((name, value))
        self.headers = tuple(kept)

    def omit(self, category: str) -> None:
        self.omission = self.omission or category
        self.broken = True
        self.safe = False

    def feed(self, chunk: bytes) -> None:
        if not chunk or self.broken or self._ended or self._settled:
            return
        if self._raw_bytes + len(chunk) > _MAX_JSON_BODY:
            self.omit("response-budget")
            self._raw.clear()
            return
        self._raw_bytes += len(chunk)
        at = 0
        while at < len(chunk):
            last = self._raw[-1] if self._raw else b""
            if isinstance(last, bytes) and last and len(last) < 65536:
                take = min(65536 - len(last), len(chunk) - at)
                self._raw[-1] = last + chunk[at : at + take]
            else:
                if not self._charge(128):
                    self._raw.clear()
                    return
                take = min(65536, len(chunk) - at)
                self._raw.append(chunk[at : at + take])
            at += take

    def frame(self, data: str | bytes) -> None:
        if self.broken or self._ended or self._settled:
            return
        self._websocket = True
        if not isinstance(data, str):
            self.omit("response-unsupported")
            return
        if len(data) > _MAX_JSON_BODY - self._raw_bytes:
            self.omit("response-budget")
            return
        try:
            size = len(data.encode("utf-8"))
        except UnicodeError:
            self.omit("response-malformed")
            return
        if self._raw_bytes + size > _MAX_JSON_BODY:
            self.omit("response-budget")
            return
        if len(self._raw) >= MAX_EVENTS or not self._charge(256):
            self.omit("response-budget")
            return
        self._raw_bytes += size
        self._raw.append(data)

    def end(self) -> None:
        self._ended = True

    def _lane(self, key: tuple[str, ...], value: str, *, render: bool = False, repeat: bool = True) -> None:
        if not isinstance(value, str):
            self.omit("response-malformed")
            return
        lane = self._lanes.get(key)
        if lane is None:
            if len(self._lanes) >= 64 or not self._charge(512 + 4 * sum(map(len, key))):
                self.omit("response-budget")
                return
            lane = self._lanes[key] = _ResponseLane()
        elif not repeat:
            if "".join(piece.text for piece in lane.pieces) != value:
                self.omit("response-ambiguous")
            return
        if self.size + len(value) > _MAX_RESPONSE:
            self.omit("response-budget")
            return
        self.size += len(value)
        while value:
            last = lane.pieces[-1] if lane.pieces else None
            if last is not None and len(last.text) < 65536 and (not render or self._rendered[-1] is last):
                take = min(65536 - len(last.text), len(value))
                last.text += value[:take]
            else:
                # The piece charge includes staging list growth and the screened text/transition references.
                if not self._charge(256):
                    return
                take = min(65536, len(value))
                starts_lane = render and self._render_lane is not None and self._render_lane is not lane
                piece = _ResponsePiece(value[:take], starts_lane)
                lane.pieces.append(piece)
                if render:
                    self._render_lane = lane
                    self._rendered.append(piece)
            value = value[take:]

    def _tool(
        self,
        key: tuple[str, ...],
        name: str = "",
        args: str = "",
        *,
        delta_name: bool = False,
        json_args: bool = True,
    ) -> None:
        if key not in self._calls:
            if not self._charge(512 + 4 * sum(map(len, key))):
                return
            self._calls[key] = ((*key, "name"), (*key, "args"), json_args)
        named, arguments, structured = self._calls[key]
        if structured != json_args:
            self.omit("response-ambiguous")
        if name:
            self._lane(named, name, repeat=delta_name)
        self._lane(arguments, args)

    def event(self, data: str) -> None:
        """Read one complete event only on the settlement worker (direct readers may also use it)."""
        if not safe_text(data):
            self.omission = self.omission or "response-credential"
        self._event_count += 1
        if self._event_count > MAX_EVENTS:
            self.omit("response-budget")
            return
        read = wellformed.read(data)
        if read.repeated:  # a key given twice: the agent's parser may have read the other copy
            self.omit("response-malformed")
            return
        obj = read.doc  # a half of a character (a lone surrogate) is U+FFFD in the copy, as UTF-8 stores it
        if not isinstance(obj, dict):
            self.omit("response-malformed")
            return
        if not safe_tree(obj):
            self.omission = self.omission or "response-credential"
        self._event(obj)

    def _event(self, obj: dict[str, Any]) -> None:
        kind = obj.get("type", "")
        if self.protocol == "anthropic":
            index = str(obj.get("index", ""))
            if kind == "content_block_start":
                if not index or index in self._blocks:
                    self.omit("response-ambiguous")
                    return
                if len(self._blocks) >= 64 or not self._charge(256 + 4 * len(index)):
                    self.omit("response-budget")
                    return
                block = obj.get("content_block") or {}
                block_kind = block.get("type")
                if block_kind not in ("tool_use", "text", "thinking"):
                    self.omit("response-unsupported")
                    return
                self._blocks[index] = True
                if block_kind == "tool_use":
                    self._tool(("anthropic", index), block.get("name", ""))
                    given = block.get("input")
                    if given:
                        self._tool(
                            ("anthropic", index), args=json.dumps(given, ensure_ascii=False, separators=(",", ":"))
                        )
                else:
                    field_name = "text" if block_kind == "text" else "thinking"
                    self._lane(("anthropic", index, field_name), block.get(field_name, ""), render=field_name == "text")
            elif kind == "content_block_delta":
                if not self._blocks.get(index):
                    self.omit("response-ambiguous")
                    return
                delta = obj.get("delta") or {}
                part = delta.get("type")
                if part == "text_delta":
                    self._lane(("anthropic", index, "text"), delta.get("text", ""), render=True)
                elif part == "input_json_delta":
                    if ("anthropic", index) not in self._calls:
                        self.omit("response-ambiguous")
                    self._tool(("anthropic", index), args=delta.get("partial_json", ""))
                elif part in ("thinking_delta", "signature_delta"):
                    field_name = "thinking" if part == "thinking_delta" else "signature"
                    self._lane(("anthropic", index, field_name), delta.get(field_name, ""))
                else:
                    self.omit("response-unsupported")
            elif kind == "content_block_stop":
                if not self._blocks.get(index):
                    self.omit("response-ambiguous")
                else:
                    self._blocks[index] = False
            elif kind in ("message_start", "message_delta"):
                self._anthropic_usage((obj.get("message") or {}).get("usage") or obj.get("usage") or {})
            elif kind in ("message_stop", "error"):
                self.done = True
                self.failed = kind == "error" or any(self._blocks.values())
        elif isinstance(kind, str) and (kind.startswith("response.") or kind == "error"):
            item = str(obj.get("item_id", obj.get("output_index", "")))
            if kind.endswith(".delta") and not obj.get("item_id"):
                self.omit("response-ambiguous")
                return
            part = str(obj.get("content_index", ""))
            if kind in (
                "response.output_text.delta",
                "response.refusal.delta",
                "response.reasoning_summary_text.delta",
            ):
                self._lane(
                    ("responses", item, part, kind), obj.get("delta", ""), render=kind == "response.output_text.delta"
                )
            elif kind in ("response.function_call_arguments.delta", "response.custom_tool_call_input.delta"):
                self._tool(
                    ("responses", item),
                    args=obj.get("delta", ""),
                    json_args=kind == "response.function_call_arguments.delta",
                )
            elif kind == "response.output_item.added":
                added = obj.get("item") or {}
                if added.get("type") in ("function_call", "custom_tool_call"):
                    self._tool(
                        ("responses", str(added.get("id", obj.get("output_index", "")))),
                        added.get("name", ""),
                        json_args=added.get("type") == "function_call",
                    )
            elif kind.endswith(".delta"):
                self.omit("response-unsupported")
            if kind in ("response.completed", "response.done", "response.incomplete", "response.failed", "error"):
                self.done = True
                self.failed = kind in ("response.incomplete", "response.failed", "error")
                self._responses_usage((obj.get("response") or {}).get("usage") or {})
        elif "choices" in obj:
            for choice in obj.get("choices") or []:
                index = str(choice.get("index", 0))
                delta = choice.get("delta") or choice.get("message") or {}
                for field_name in ("content", "refusal", "reasoning_content"):
                    value = delta.get(field_name)
                    if value is not None:
                        self._lane(("chat", index, field_name), value, render=field_name == "content")
                for call in delta.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    if "index" not in call and "id" not in call:
                        self.omit("response-ambiguous")
                    self._tool(
                        ("chat", index, str(call.get("index", call.get("id")))),
                        fn.get("name") or "",
                        fn.get("arguments") or "",
                        delta_name="delta" in choice,
                    )
                if delta.get("function_call"):
                    fn = delta["function_call"]
                    self._tool(
                        ("chat", index, "legacy"),
                        fn.get("name") or "",
                        fn.get("arguments") or "",
                        delta_name="delta" in choice,
                    )
                if choice.get("finish_reason"):
                    self.done = True
            usage = obj.get("usage") or {}
            if usage:
                cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                self.usage = {
                    "input": int(usage.get("prompt_tokens") or 0) - cached,
                    "output": int(usage.get("completion_tokens") or 0),
                    "cache_read": cached,
                    "cache_write": 0,
                }
        elif self.protocol == "gemini":
            self._gemini(obj)
        elif "delta" in obj:
            self.omit("response-unsupported")

    def feed_json(self, body: bytes) -> None:
        raw = body.decode("utf-8")
        if not safe_text(raw):
            self.omission = self.omission or "response-credential"
        read = wellformed.read(raw)
        if read.repeated:  # a key given twice: the agent's parser may have read the other copy
            self.omit("response-malformed")
            return
        obj = read.doc  # a half of a character (a lone surrogate) is U+FFFD in the copy, as UTF-8 stores it
        if self.protocol in ("anthropic", "responses", "chat") and not isinstance(obj, dict):
            self.omit("response-malformed")
            return
        if not safe_tree(obj):
            self.omission = self.omission or "response-credential"
        if isinstance(obj, list) and self.protocol == "gemini":
            for answer in obj:
                self._gemini(answer)
        elif isinstance(obj, dict):
            if self.protocol == "anthropic":
                for index, block in enumerate(obj.get("content") or []):
                    if block.get("type") == "text":
                        self._lane(("anthropic", str(index), "text"), block.get("text", ""), render=True)
                    elif block.get("type") == "tool_use":
                        self._tool(
                            ("anthropic", str(index)),
                            block.get("name", ""),
                            json.dumps(block.get("input"), ensure_ascii=False, separators=(",", ":")),
                        )
                self._anthropic_usage(obj.get("usage") or {})
            elif self.protocol == "responses":
                for index, item in enumerate(obj.get("output") or []):
                    if item.get("type") == "message":
                        for number, part in enumerate(item.get("content") or []):
                            if part.get("type") == "output_text":
                                self._lane(("responses", str(index), str(number)), part.get("text", ""), render=True)
                    elif item.get("type") in ("function_call", "custom_tool_call"):
                        self._tool(
                            ("responses", str(index)),
                            item.get("name", ""),
                            item.get("arguments") or item.get("input") or "",
                            json_args=item.get("type") == "function_call",
                        )
                self._responses_usage(obj.get("usage") or {})
            else:
                self._event(obj)
        self.done = True

    def _gemini(self, obj: dict[str, Any]) -> None:
        if "error" in obj:
            self.done = self.failed = True
        for index, candidate in enumerate(obj.get("candidates") or []):
            index = candidate.get("index", index)
            for number, part in enumerate((candidate.get("content") or {}).get("parts") or []):
                if isinstance(part.get("text"), str):
                    self._lane(
                        ("gemini", str(index), str(number), "thought" if part.get("thought") else "text"),
                        part["text"],
                        render=not part.get("thought"),
                    )
                call = part.get("functionCall")
                if isinstance(call, dict):
                    self._tool(
                        ("gemini", str(index), str(number)),
                        call.get("name", ""),
                        json.dumps(call.get("args"), ensure_ascii=False, separators=(",", ":")),
                    )
            if candidate.get("finishReason"):
                self.done = True
        usage = obj.get("usageMetadata") or {}
        if usage:
            cached = int(usage.get("cachedContentTokenCount") or 0)
            self.usage = {
                "input": int(usage.get("promptTokenCount") or 0) - cached,
                "output": int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
                "cache_read": cached,
                "cache_write": 0,
            }

    def _decoded(self, data: bytes) -> None:
        if "text/event-stream" in self.content_type:
            for event in self._events.feed(data):
                if not safe_text(event.raw.decode("utf-8")):
                    self.omission = self.omission or "response-credential"
                if event.data == "[DONE]":
                    self.done = True
                elif event.data:
                    self.event(event.data)
        elif "vnd.amazon.eventstream" in self.content_type:
            if self._frames is None:
                self._frames = EventStreamDecoder(strict_text_headers=True)
            for message in self._frames.feed(data):
                if not safe_tree(message.headers):
                    self.omission = self.omission or "response-credential"
                if message.headers.get(":message-type") == "event" and message.headers.get(":event-type") == "chunk":
                    envelope = wellformed.read(message.payload.decode("utf-8"))
                    if envelope.mended or envelope.repeated or not isinstance(envelope.doc, dict):
                        self.omit("response-malformed")
                        return
                    if not safe_tree(envelope.doc):
                        self.omission = self.omission or "response-credential"
                    self.event(base64.b64decode(envelope.doc["bytes"], validate=True).decode("utf-8"))
                elif message.headers.get(":message-type") in ("exception", "error"):
                    self.done = self.failed = True  # Bedrock's failed answer, as before: its words are never kept
                else:
                    self.omit("response-unsupported")
        else:
            if len(self._body) + len(data) > _MAX_JSON_BODY:
                self.omit("response-budget")
            elif not self.broken:
                self._body.extend(data)

    def _decoded_parts(self) -> Iterator[bytes]:
        if self._zlib_bits is not None:
            yield from inflate(self._raw, self._zlib_bits, _MAX_JSON_BODY)
            return
        coding = (self.content_encoding or "").strip().lower()
        if coding == "br":
            yield from brotli_parts(self._raw, _MAX_JSON_BODY)
            return
        if coding == "zstd":
            yield from zstd_parts(self._raw, _MAX_JSON_BODY)
            return
        decoder = StreamDecoder(self.content_encoding, limit=_MAX_JSON_BODY)
        for chunk in self._raw:
            if not isinstance(chunk, bytes):
                raise ResponseEncodingError("response-malformed")
            yield from decoder.pieces(chunk)
        yield decoder.flush()

    def settle(self) -> None:
        """Compatibility for private readers; publication still requires ``safe``."""
        self.screen()

    def screen(self) -> bool:
        if self._settled:
            return self.safe
        self._settled = True
        try:
            if not safe_text(self.content_type) or not safe_text(self.content_encoding or ""):
                self.content_type = ""
                self.content_encoding = None
                self.omit("response-credential")
            self.headers = tuple((k, v) for k, v in self.headers if safe_text(k) and safe_text(v))
            if not self._ended:
                self.omit("response-incomplete")
            if not self.broken:
                if self._websocket:
                    for message in self._raw:
                        if not isinstance(message, str):
                            self.omit("response-unsupported")
                            break
                        self.event(message)
                        if self.broken:
                            break
                else:
                    coding = (self.content_encoding or "").strip().lower()
                    if coding in ("gzip", "x-gzip", "deflate"):
                        self._zlib_bits = prove_zlib(self._raw, coding, _MAX_JSON_BODY)
                    elif coding in ("br", "zstd"):
                        if not self._charge(256):
                            return False
                        prove = prove_brotli if coding == "br" else prove_zstd
                        prove(self._raw, _MAX_JSON_BODY)
                    for part in self._decoded_parts():
                        self._decoded(part)
                        if self.broken:
                            break
                    if not self.broken:
                        if "text/event-stream" in self.content_type:
                            if self._events.held or not self.done:
                                self.omit("response-incomplete")
                        elif self._frames is not None:
                            if self._frames.held or not self.done:
                                self.omit("response-incomplete")
                        elif "json" in self.content_type:
                            self.feed_json(bytes(self._body))
                        elif self.content_type.startswith("text/"):
                            value = self._body.decode("utf-8")
                            self._lane(("text",), value, render=True)
                            self.done = True
                        else:
                            self.omit("response-unsupported")
                if not self.done or self.failed:
                    self.omit("response-incomplete")
            for lane in self._lanes.values():
                if not safe_text("".join(piece.text for piece in lane.pieces)):
                    self.omission = self.omission or "response-credential"
            for _, args, structured in self._calls.values():
                lane = self._lanes.get(args)
                if structured and lane is not None:
                    value = "".join(piece.text for piece in lane.pieces)
                    if not value and self.protocol == "anthropic":
                        continue
                    read = wellformed.read(value)
                    if read.repeated:  # a half of a character is the copy's U+FFFD, as the agent's text
                        self.omit("response-malformed")
                    elif not safe_tree(read.doc):
                        self.omission = self.omission or "response-credential"
            if self.omission is None:
                self.text = [piece.text for piece in self._rendered]
                if any(piece.starts_lane for piece in self._rendered):
                    self._history_transitions = tuple(piece.starts_lane for piece in self._rendered)
                for index, (name, args, _) in enumerate(self._calls.values()):
                    named = self._lanes.get(name)
                    arguments = self._lanes.get(args)
                    self.tools[str(index)] = {
                        "name": "".join(p.text for p in named.pieces) if named else "",
                        "args": "".join(p.text for p in arguments.pieces) if arguments else "",
                    }
                self.safe = True
        except ResponseEncodingError as exc:
            self.omit(str(exc))
        except Exception:
            self.headers = ()
            self.omit("response-malformed")
        finally:
            self._frames = None
            self._body.clear()
            self._events = SSEParser(limit=_MAX_JSON_BODY)
            self._lanes.clear()
            self._rendered.clear()
            self._render_lane = None
            self._calls.clear()
            self._blocks.clear()
            if not self.safe:
                self.clear_payload()
        return self.safe

    def clear_payload(self) -> None:
        self._raw.clear()
        self.text.clear()
        self.tools.clear()
        self._body.clear()
        self._lanes.clear()
        self._rendered.clear()
        self._render_lane = None
        self._history_transitions = ()
        self._calls.clear()

    def history(self) -> _ScreenedResponse:
        """Copy only screened observations, with no link to the staging graph."""
        return _ScreenedResponse(
            tuple(self.text) if self.safe else (),
            tuple((tool["name"], tool["args"]) for tool in self.tools.values()) if self.safe else (),
            tuple(self.usage.items()),
            self.omission,
            self._history_transitions if self.safe else (),
        )

    def publish(self, exchange: Any, status: int, *, terminal: bool) -> None:
        """Second sequential worker operation; safe bytes alone reach the raw writer."""
        try:
            exchange.response_start(status, list(self.headers))
            if self.safe:
                if self._websocket:
                    for message in self._raw:
                        exchange.ws("down", message)
                elif self.content_encoding:
                    body = bytearray()
                    for piece in self._decoded_parts():
                        body.extend(piece)
                    exchange.response_body(bytes(body))
                elif "vnd.amazon.eventstream" in self.content_type:
                    for chunk in self._raw:
                        if isinstance(chunk, bytes):
                            exchange.response_chunk(chunk)
                else:
                    text_decoder = codecs.getincrementaldecoder("utf-8")()
                    for chunk in self._raw:
                        if isinstance(chunk, bytes):
                            value = text_decoder.decode(chunk)
                            if value:
                                exchange.response_text(value)
                    tail = text_decoder.decode(b"", final=True)
                    if tail:
                        exchange.response_text(tail)
            if self.omission:
                exchange.response_omitted(self.omission)
            if terminal:
                exchange.response_end(error=self.omission)
        finally:
            self._raw.clear()

    def _anthropic_usage(self, usage: dict[str, Any]) -> None:
        for src, dst in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_write"),
        ):
            if usage.get(src) is not None:
                self.usage[dst] = int(usage[src])

    def _responses_usage(self, usage: dict[str, Any]) -> None:
        if not usage:
            return
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        self.usage = {
            "input": int(usage.get("input_tokens") or 0) - cached,
            "output": int(usage.get("output_tokens") or 0),
            "cache_read": cached,
            "cache_write": 0,
        }


@dataclass
class Outcome:
    """Everything known about one exchange once it is over."""

    headers: dict[str, str]  # the client's request headers, lower-cased
    upstream: str
    path: str
    transport: str  # "http" | "ws"
    started: float
    prepared: Prepared | None = None
    status: int | None = None
    forwarded: float | None = None  # when the request left for the upstream
    ended: float | None = None
    capture: ResponseCapture | None = None
    request_headers: list[tuple[str, str]] = field(default_factory=list)  # as forwarded
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    body_bytes: int = 0
    error: str | None = None
    host: str | None = None  # where it went
    signed: str | None = None  # Latet signed it: `aws-profile:<name>` or `aws-session`
    # What the plugin's bridge read of a request it never sends (in place of ``prepared``): recorded, never sent.
    observed: Observation | None = None
    # A WebSocket frame as its decision sent it (or would have): where its texts were is found in it. None for a request
    # over HTTP, whose arena's body is what its decision leaves with.
    payload: bytes | str | None = None
    # Private producer handoff, never reconstructed from summary/request/JSON.
    history_code: int = 0
    # The code of the refusal that stopped it (``outcome.BLOCK_CODES``), as its decision says: what the history's
    # summary shows of why, its words kept in the full history's private view. None: its arena's words alone say it
    # was stopped (0.10's preparation, the plugin's checks), shown as ``refused``.
    block_code: str | None = None
    # What its decision rests on, by fixed categories alone (``exchanges.HISTORY_PASSES``): the built-in rules a
    # ``Pass`` named and whether a request exception let something through; empty for a ``Forward``. Its envelope's
    # profile (``request_profiles.Profile.id``) and the headers its profile removed (D4): ids and counts, never values.
    passes: tuple[str, ...] = ()
    profile: str | None = None
    removed_headers: int = 0


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Taken where the exchange ends (the event loop, where sessions change): its conversation's key, what its fakes
    restore to (one ``Engine.restores`` pair a record), the state they were read in, and its project."""

    session: str
    text: tuple[Restore, ...]
    json: tuple[Restore, ...]
    version: tuple[int, int] = (0, 0)  # (entities in the session, vault version): same restores
    project: tuple[tuple[str, str], ...] | None = None  # (("id", ...), ("name", ...)) of the session's project


class _Block(NamedTuple):
    """One text of an exchange as it left (or as the plugin's bridge read it), frozen as the exchange ended: its words
    (shared, never copied), where it was in the request, what it is, whether the model wrote it, the tool whose output
    it is and where that output came from as the request carried it (``SlotRef.sent_source``), and the values masked in
    it, their offsets exact or not."""

    text: str
    # ("at", path): a bridge observation; ("slot", child occurrence ordinals): a request's text or JsonRef;
    # ("image", n): the text read in its n-th image. None for a detached request slot.
    place: tuple[Any, ...]
    kind: SlotKind
    from_model: bool
    tool: str | None
    source_input: tuple[int, ...] | None  # its tool call's input occurrence, read in the recorder's thread
    replacements: tuple[Replacement, ...]
    exact: bool


@dataclass(frozen=True, slots=True, repr=False)
class EncodedImage:
    """An immutable shared carrier whose canonical base64 is needed only when the recorder worker persists it.

    No live reference or additional image-sized string survives a pending review here: only the already owned raw
    string and its encoding. The normalized result belongs to the recorder's current job, never to Prepared.
    """

    raw: str
    form: str

    def canonical(self) -> str:
        if self.form == "data_url":
            return self.raw.partition(",")[2]
        if self.form == "base64url":
            value = self.raw.replace("-", "+").replace("_", "/")
            return value + "=" * (-len(value) % 4)
        return self.raw


class _Image(NamedTuple):
    """One image of a request as it was sent, frozen as the exchange ended: where it was (found in the recorder's thread
    by its occurrence in what was sent), what became of it, the text read in it (a block of
    the request; None without one), and its base64 (None when it was withheld)."""

    place: tuple[int, ...] | None  # its data's occurrence; None when the image's holder was detached
    state: str
    text: str | None
    data: str | EncodedImage | None
    media_type: str


@dataclass(frozen=True, slots=True, weakref_slot=True, kw_only=True)
class _RequestSnapshot:
    """An exchange as the recorder's thread reads it: taken as it ends (``Recorder.finish``), on the thread that ends
    it, from its outcome and its arena (or the plugin's bridge's observation), and immutable all the way down, holding
    nothing of either (its texts shared, never copied): what the history is given is what the exchange was then,
    whatever becomes of its arena or its outcome afterwards. The provider's answer as the model wrote it (``capture``)
    is handed over with it, to be read in the recorder's thread: nothing feeds it once its exchange has ended."""

    headers: tuple[tuple[str, str], ...]  # the client's request headers, lower-cased
    upstream: str
    path: str
    transport: str
    started: float
    status: int | None
    forwarded: float | None
    ended: float | None
    host: str | None
    signed: str | None
    body_bytes: int
    request_headers: tuple[tuple[str, str], ...]  # as forwarded
    restores: _Snapshot
    # What its arena (or the bridge's observation) said of it, as it ended.
    protocol: str | None
    mode: str
    model: str | None
    block_reason: str | None
    leaks: tuple[Leak, ...]
    uninspected: tuple[str, ...]
    degraded: tuple[str, ...]
    review: bool
    off: bool
    withheld: int
    history: bool
    outbound: tuple[str, ...]  # tool names
    bypass: bool
    blocks: tuple[_Block, ...]
    images: tuple[_Image, ...]
    # What left, as the decision left with it (an HTTP request's body, a WebSocket frame), and its content-encoding:
    # where each text and image was is found there, in the recorder's thread (``occurrence_paths``). An edited refusal
    # may use its worker-frozen logical document: its unsent original body does not describe that document.
    sent: bytes | str | None = None
    encoding: str | None = None
    counted: bool = False  # Bedrock token count: paths are relative to the decoded inner request, as before
    history_code: int = 0  # owned immutable scalar, independent of Audit and the mutable Outcome
    # Why it was stopped, by its category code alone (``Outcome.block_code``, or ``refused``); None when it was not.
    block_code: str | None = None
    # What its launch was decided as, by its fixed category (``launch_authority.LAUNCH_CATEGORIES``); None: not asked
    # (the plugin's own calls, an exchange refused before its launch was decided).
    launch: str | None = None
    # What its decision rests on (``Outcome.passes``), its envelope's profile and what was removed of it (D4): fixed
    # categories, a profile's id and counts, never a value.
    passes: tuple[str, ...] = ()
    profile: str | None = None
    removed_headers: int = 0
    removed_members: int = 0


@dataclass(frozen=True, slots=True)
class _ScreenedResponse:
    text: tuple[str, ...]
    tools: tuple[tuple[str, str], ...]
    usage: tuple[tuple[str, int], ...]
    omission: str | None
    transitions: tuple[bool, ...]

    def history_text(self) -> str:
        text = "".join(self.text)
        if safe_text(text):
            return text
        size = len(text)
        del text
        separators = self.transitions.count(True)
        if not separators or size + separators > _MAX_RESPONSE:
            return ""

        # The pieces are already screened. Only the exact newly composed string is admitted here.
        parts = [""] * (len(self.text) + separators)
        position = 0
        for piece, starts_lane in zip(self.text, self.transitions, strict=True):
            if starts_lane:
                parts[position] = "\n"
                position += 1
            parts[position] = piece
            position += 1
        text = "".join(parts)
        del parts
        return text if safe_text(text) else ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordSnapshot(_RequestSnapshot):
    """Normal recorder work contains only positively screened response payload."""

    response_headers: tuple[tuple[str, str], ...] = ()
    capture: _ScreenedResponse | None = None
    error: str | None = None


_OBSERVED_MODE = "verify"  # mediate.VERIFY: what the plugin's bridge observed was masked by the plugin, never rewritten


def build_record_blocks(read: Prepared) -> tuple[_Block, ...]:
    """The texts of an arena, with the tools' inputs that ran outside this computer as blocks of their own; where each
    was is left to the recorder's thread (``_Block.place``)."""
    pending = [*read.pending, *((slot, [], False) for slot in read.outbound_slots)]
    images = {id(work): n for n, work in enumerate(read.images)}
    blocks = []
    for (slot, replacements, exact), text in zip(pending, (*read.frozen_texts, *read.frozen_outbound), strict=True):
        n = images.get(id(slot.container))  # an image's text: where the image was
        place = ("image", n) if n is not None else ("slot", read.record_positions.get((id(slot.container), slot.key)))
        tool = getattr(slot, "tool", None) if slot.kind == SlotKind.TOOL_RESULT else None
        given = getattr(slot, "source_input", None) if slot.kind == SlotKind.TOOL_RESULT else None
        source = read.record_positions.get((id(given[0]), given[1])) if given and isinstance(given[0], dict) else None
        blocks.append(_Block(text, place, slot.kind, slot.from_model, tool, source, tuple(replacements), exact))
    return tuple(blocks)


def build_record_images(read: Prepared) -> tuple[_Image, ...]:
    images = []
    for work, (_, _, data) in zip(read.images, read.frozen_image_data, strict=True):
        text = work.text if work.slot is not None and work.text else None
        place = read.record_positions.get((id(work.ref.holder), work.ref.key))
        images.append(_Image(place, work.state, text, data, work.ref.media_type))
    return tuple(images)


def _frozen_blocks(read: Prepared) -> tuple[_Block, ...]:
    """The worker's immutable metadata, shared without per-slot allocation on the finishing event loop."""
    return read.record_blocks


def _frozen_images(read: Prepared) -> tuple[_Image, ...]:
    return read.record_images


def occurrence_paths(document: Any, needed: set[tuple[int, ...] | None]) -> dict[tuple[int, ...], tuple[str, Any]]:
    """Resolve selected immutable occurrence identities in frozen JSON, in the recorder's thread. Key names come
    from that JSON alone; equal strings/objects and repeated references never consume another occurrence's path."""
    out: dict[tuple[int, ...], tuple[str, Any]] = {}

    def walk(value: Any, path: str, parent: tuple[int, ...]) -> None:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for n, (key, item) in enumerate(items):
            position = (*parent, n)
            at = f"{path}.{key}" if isinstance(value, dict) else f"{path}[{key}]"
            if position in needed:
                out[position] = (at, item)
            if isinstance(item, dict | list):
                walk(item, at, position)

    if isinstance(document, dict | list):
        walk(document, "$", ())
    return out


def _sent_document(sent: bytes | str, encoding: str | None, counted: bool = False) -> Any:
    """What was sent, read again as the masking read it (``wellformed``), its content-encoding undone; None when it
    does not read (nothing is then found in it)."""
    try:
        raw = codec.decode(sent, encoding) if isinstance(sent, bytes) and encoding else sent
        document = wellformed.loads(raw)
        if counted:
            document = wellformed.loads(base64.b64decode(document["input"]["invokeModel"]["body"], validate=True))
        return document
    except Exception:
        return None


class Recorder:
    def __init__(
        self,
        store: HistoryStore | None,
        engine: Engine | None = None,
        broadcaster: Feed | None = None,
        history: str = "full",
        sent: SentBlocks | None = None,
        jobs: JobLock | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.broadcaster: Feed = broadcaster if broadcaster is not None else Broadcaster()
        self.history = history  # "full", "metadata" (no text kept) or "off" (nothing recorded)
        self.sent: SentBlocks = sent if sent is not None else LocalSentBlocks()  # (session, block): recorded before
        self.jobs: JobLock = jobs if jobs is not None else LocalJobLock()  # the history's pruning, once at a time
        # block id → values masked in it, as (type, class, module, count)
        self._types: LRU[str, tuple[tuple[str, str, str, int], ...]] = LRU(200_000)
        self._annotators: LRU[tuple[str, tuple[int, int], bool], Annotator] = LRU(64)
        # A history is one whatever it says of its length (a store may count its rows elsewhere, and answer 0).
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="latet-recorder") if store is not None else None
        self._last_prune = time.time()
        self._pending = 0
        self._idle = threading.Condition()

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def freeze_request(self, outcome: Outcome) -> _RequestSnapshot | None:
        """Synchronously freeze request observations before any response teardown/await. Never raises: an outcome
        that cannot be read is said by the error's type, and nothing of it is kept."""
        try:
            outcome.ended = outcome.ended or time.time()
        except Exception as exc:  # the dashboard must never break the traffic it observes
            log.error("could not freeze an exchange: %s", type(exc).__name__)
            return None
        observe.exchange(outcome)
        if self._executor is None or self.history == "off":
            return None
        try:
            frozen = self._request_snapshot(outcome)
        except Exception as exc:
            log.error("could not freeze an exchange: %s", type(exc).__name__)
            return None
        if frozen is None:  # its arena could not make what the record needs (``Prepared.record_error``): said so
            failure = getattr(outcome.prepared, "record_error", None)
            log.error("could not queue an exchange for the dashboard: %s", failure or "unknown")
        return frozen

    def finish(self, outcome: Outcome) -> None:
        """Compatibility for request-only refusals and bridge observations. Never raises: the dashboard must never
        break the traffic it observes."""
        request = self.freeze_request(outcome)
        if request is None:  # nothing frozen (history off, or an outcome that could not be read): nothing to queue
            return
        try:
            capture = outcome.capture if outcome.capture is not None and outcome.capture.safe else None
            error = "response-incomplete" if outcome.capture and capture is None else outcome.error
        except Exception as exc:
            log.error("could not queue an exchange for the dashboard: %s", type(exc).__name__)
            return
        self.submit_safe(request, capture, (), error)

    def submit_safe(
        self,
        request: _RequestSnapshot | None,
        capture: ResponseCapture | None,
        headers: tuple[tuple[str, str], ...],
        error: str | None,
    ) -> None:
        """Admit frozen request plus screened response to the existing 500-job gate."""
        if request is None or self._executor is None or self.history == "off":
            return
        if capture is not None and not capture.safe and (capture.text or capture.tools or capture._raw):
            return
        try:
            snapshot = RecordSnapshot(
                **{item.name: getattr(request, item.name) for item in fields(_RequestSnapshot)},
                capture=capture.history() if capture is not None else None,
                response_headers=headers,
                error=error if error is None or (len(error) <= 128 and safe_text(error)) else "response-error",
            )
            try:
                loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            with self._idle:
                if self._pending >= _MAX_QUEUED:
                    log.warning("dashboard recording is behind: an exchange was not recorded")
                    return
                self._pending += 1
            try:
                future = self._executor.submit(self._record, snapshot)
            except BaseException:
                self._left()
                raise
            future.add_done_callback(lambda f: self._done(f, loop))
        except Exception as exc:
            log.error("could not queue an exchange for the dashboard: %s", type(exc).__name__)

    def snapshot(self, outcome: Outcome) -> RecordSnapshot | None:
        """Synchronous request-only snapshot; response capture requires finalizer admission."""
        request = self._request_snapshot(outcome)
        if request is None:
            return None
        capture = outcome.capture if outcome.capture is not None and outcome.capture.safe else None
        return RecordSnapshot(
            **{item.name: getattr(request, item.name) for item in fields(_RequestSnapshot)},
            capture=capture.history() if capture is not None else None,
            response_headers=capture.headers if capture is not None else (),
            error="response-error" if outcome.error else None,
        )

    def prune_now(self) -> None:
        """Prune the history now, not at the next interval: its retention was made shorter, or the interval passed with
        nothing recorded to carry it. Queued on the recorder's thread, where the interval's own prune runs, so the two
        take turns and the event loop waits for neither; it restarts the interval. Returns at once; ``drain`` waits for
        it."""
        executor = self._executor
        if executor is None:
            return
        with self._idle:
            self._pending += 1
        try:
            future = executor.submit(self._prune)
        except Exception as exc:
            # The thread ended meanwhile (a settings change racing ``close``): never queued, not pending.
            self._left()
            log.error("could not queue the pruning of the history: %s", type(exc).__name__)
            return
        future.add_done_callback(lambda f: self._ended(f, "could not prune the history"))
        self._last_prune = time.time()  # asked for: the interval starts from now, not from when the thread gets to it

    async def prune_when_due(self) -> None:
        """Ask for the history's prune each time its interval has passed, whether or not anything is recorded: the
        interval's own prune rides on a recorded exchange, and a daemon with no traffic, or with the history off,
        records none. The daemon runs this as a task for as long as it serves, and cancels it at stop; a failure of its
        own ends it never: said by its type, it asks again ``_PRUNE_RETRY`` seconds later."""
        while True:
            try:
                until_due = self._prune_if_due()
            except Exception as exc:  # by its type: its words could quote what the history holds
                log.error("could not ask for the pruning of the history: %s", type(exc).__name__)
                until_due = _PRUNE_RETRY
            await asyncio.sleep(until_due)

    def _prune_if_due(self) -> float:
        """Ask for the prune if its interval has passed since the last prune, whoever made it (an exchange recorded
        meanwhile may have); the seconds until it falls due next."""
        left = self._last_prune + _PRUNE_EVERY - time.time()
        if left > 0:
            return left
        self.prune_now()  # restarts the interval
        return _PRUNE_EVERY

    def _request_snapshot(self, outcome: Outcome) -> _RequestSnapshot | None:
        """``outcome`` as the recorder's thread will read it (``RecordSnapshot``), taken now: what its arena (or the
        bridge's observation) held, its texts and where they were, its images as they were sent, and what its
        conversation's fakes restore to (``_snapshot``); nothing of the arena or the outcome kept, nothing of either
        changed. Its large parts are shared, never copied: the texts and the images' base64 are strings already."""
        read: Prepared | Observation | None = outcome.prepared if outcome.prepared is not None else outcome.observed
        restores = self._snapshot(read)
        blocks: tuple[_Block, ...] = ()
        images: tuple[_Image, ...] = ()
        said: dict[str, Any] = {"protocol": None, "mode": "passthrough", "model": None, "block_reason": None}
        said |= {"leaks": (), "uninspected": (), "degraded": (), "review": False, "off": False, "withheld": 0}
        said |= {"history": False, "outbound": (), "bypass": False, "launch": None}
        if isinstance(read, Observation):
            said |= {"protocol": read.protocol, "mode": _OBSERVED_MODE, "block_reason": read.reason}
            said |= {"leaks": read.leaks, "uninspected": read.uninspected, "history": read.history}
            said |= {"outbound": read.outbound, "bypass": read.bypass}
            blocks = tuple(
                _Block(text.text, ("at", f"$[{i}]"), text.kind, text.from_model, None, None, (), False)
                for i, text in enumerate(read.blocks)
            )
        elif read is not None:
            if read.record_error is not None:
                return None
            said |= {"protocol": read.protocol, "mode": read.mode, "model": read.model}
            said |= {"block_reason": read.block_reason, "leaks": tuple(read.leaks)}
            said |= {"uninspected": tuple(read.uninspected), "degraded": tuple(read.degraded), "review": read.review}
            said |= {"off": read.off, "withheld": read.withheld, "history": read.history}
            said |= {"outbound": tuple(read.outbound), "bypass": read.bypass}
            said["launch"] = read.launch if read.launch in LAUNCH_CATEGORIES else None
            said["removed_members"] = len(getattr(read, "body_removed", ()) or ())
            if read.pending or read.outbound_slots or read.images:
                blocks, images = _frozen_blocks(read), _frozen_images(read)
                # Where they were is found in what left (immutable: the decision's bytes, or the frame as it was sent),
                # in the recorder's thread; a request's in its content-encoding, a frame's as it is.
                logical = read.record_body
                said["sent"] = (
                    logical if logical is not None else (outcome.payload if outcome.payload is not None else read.body)
                )
                said["encoding"] = (
                    outcome.headers.get("content-encoding") if logical is None and outcome.payload is None else None
                )
                said["counted"] = logical is None and (
                    read.shape == "bedrock-count" or shape_of("POST", outcome.path) == "bedrock-count"
                )
        # Stopped, by its decision's code (a refusal its arena has no words for included: a proof that leaves a part
        # unaccounted for, evidence its rules do not admit, the coverage limit); words with no code are the generic one.
        code = outcome.block_code
        block_code = code if type(code) is str and code in BLOCK_CODES else "refused" if said["block_reason"] else None
        return _RequestSnapshot(
            headers=tuple(outcome.headers.items()),
            upstream=outcome.upstream,
            path=outcome.path,
            transport=outcome.transport,
            started=outcome.started,
            status=outcome.status,
            forwarded=outcome.forwarded,
            ended=outcome.ended,
            host=outcome.host,
            signed=outcome.signed,
            body_bytes=outcome.body_bytes,
            request_headers=tuple(outcome.request_headers),
            restores=restores,
            blocks=blocks,
            images=images,
            history_code=outcome.history_code,
            block_code=block_code,
            passes=tuple(outcome.passes),
            profile=outcome.profile,
            removed_headers=outcome.removed_headers,
            **said,
        )

    def _snapshot(self, read: Prepared | Observation | None) -> _Snapshot:
        session = getattr(read, "session", None)
        project = self._project(read, session)
        if session is None or self.engine is None or not session.entity_ids:
            return _Snapshot(getattr(session, "key", None) or "unscoped", (), (), project=project)
        return _Snapshot(
            session.key,
            tuple(self.engine.restores(session, json_mode=False)),
            tuple(self.engine.restores(session, json_mode=True)),
            (len(session.entity_ids), self.engine.vault.version),
            project,
        )

    def _project(self, read: object, session: Any) -> tuple[tuple[str, str], ...] | None:
        """The project whose rules the exchange was decided by, and how it came (D7): a request's own (``trusted``
        through a binding Latet made, ``claimed`` by what it said, which binds no conversation); else its
        conversation's, which only a trusted binding attaches (the plugin's bridge, whose observations these are)."""
        pid = getattr(read, "project", None)  # an observation (the bridge's) has none of its own
        binding = getattr(read, "binding", None) if pid else None
        if pid is None:
            pid, binding = getattr(session, "project_id", None), "trusted"
        peek = getattr(getattr(self.engine, "scopes", None), "peek", None)
        project = peek(pid) if pid and peek is not None else None
        if project is None:
            return None
        return (("id", project.id), ("name", project.name), ("binding", binding if binding == "claimed" else "trusted"))

    def _annotator(self, snap: _Snapshot, json_mode: bool) -> Annotator | None:
        restores = snap.json if json_mode else snap.text
        if not restores:
            return None
        key = (snap.session, snap.version, json_mode)
        annotator = self._annotators.get(key)
        if annotator is None:
            annotator = Annotator(restores)  # a trie of every surrogate form: built once per session state
            self._annotators.put(key, annotator)
        return annotator

    def _left(self) -> None:
        """A queued job is over (or was never queued): it no longer counts as pending."""
        with self._idle:
            self._pending -= 1
            self._idle.notify_all()

    @staticmethod
    def _failed(future: Future[Any], failure: str) -> bool:
        """Whether a queued job failed: then ``failure`` is logged with the type of what it raised (never its words nor
        its traceback, which can quote what it was recording)."""
        error = future.exception()
        if error is not None:
            log.error("%s: %s", failure, type(error).__name__)
        return error is not None

    def _ended(self, future: Future[Any], failure: str) -> None:
        """A queued job is over: its failure said, if it failed, and only then not pending any more, so that whoever
        waits for the recorder (``drain``) finds it said."""
        try:
            self._failed(future, failure)
        finally:
            self._left()

    def _done(self, future: Future[dict[str, Any]], loop: asyncio.AbstractEventLoop | None) -> None:
        try:
            if self._failed(future, "could not record an exchange"):
                return
            summary = future.result()
            if loop is None:
                self.broadcaster.publish(summary)
                return
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (shutdown): no viewer left
                loop.call_soon_threadsafe(self.broadcaster.publish, summary)
        finally:
            self._left()

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until every queued job is over: each exchange recorded and each pruning done (tests, shutdown)."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        if self._executor is not None:
            self.drain()
            self._executor.shutdown(wait=True)
            self._executor = None

    # --- the recorder thread ------------------------------------------------------------------------

    def _prune(self) -> None:
        store = self.store
        assert store is not None
        now = self._last_prune = time.time()
        with self.jobs.hold("history-prune", 600) as mine:
            if mine:
                store.prune()
                return
        # Another process (a server's other instance) prunes it now: once is enough. But what is a day past the
        # retention is pruned here all the same: whoever holds the lock would have pruned it, so nobody is pruning.
        if store.prune(now=now - _PRUNE_OVERDUE):
            log.warning(
                "the history's pruning was refused its lock: what was a day past its retention was pruned without it"
            )

    def _record(self, exchange: RecordSnapshot) -> dict[str, Any]:
        """``exchange`` given to the history (in the recorder's thread): read from its snapshot alone, never from its
        arena or its outcome."""
        store = self.store
        assert store is not None
        if time.time() - self._last_prune > _PRUNE_EVERY:
            self.prune_now()  # a job of its own, after this one: its failure never loses this exchange
        snap = exchange.restores

        def found(
            text: str, json_mode: bool, rules: dict[str, str] | None = None, rule: str = "vault"
        ) -> list[dict[str, Any]]:
            annotator = self._annotator(snap, json_mode) if text else None
            if annotator is None:
                return []
            spans = []
            for f in annotator.annotate(text)[1]:
                entity = self.engine.vault.get(f.restore.entity_id) if self.engine else None
                etype = entity.etype if entity else "?"
                span = {
                    "masked_start": f.start,
                    "masked_end": f.end,
                    "type": etype,
                    "class": entity.eclass.name if entity else "?",
                    "entity": f.restore.entity_id,
                } | origin((rules or {}).get(f.restore.entity_id, rule), entity.rule if entity else None, etype)
                if json_mode:
                    span["json"] = True  # found in its JSON-escaped form
                spans.append(span)
            return spans

        # Request blocks.
        blocks: list[BlockRecord] = []
        by_type: Counter[str] = Counter()
        new_by_type: Counter[str] = Counter()
        by_class: Counter[str] = Counter()
        by_module: Counter[str] = Counter()
        totals = [0, 0]  # values masked in this request, of which in blocks new to the session

        def count(types: Iterable[tuple[str, str, str, int]], new: bool) -> None:
            for etype, eclass, module, n in types:
                by_type[etype] += n
                by_class[eclass] += n
                by_module[module] += n
                totals[0] += n
                if new:
                    new_by_type[etype] += n
                    totals[1] += n

        # Its texts, the inputs of tools that ran outside this computer with stand-ins in them among them, as blocks.
        unresolved: list[BlockRecord] = []  # stored earlier, counts not cached (after a restart)
        ids: list[str] = []  # the block of each slot, empty ones included: a leak counts its slot among them
        # Resolve occurrence identities, never key/text equality. Images and repeated outbound references share
        # this index with texts and tool inputs; no lookup consumes an occurrence another reference also needs.
        located: dict[tuple[int, ...], tuple[str, Any]] = {}
        if exchange.sent is not None:
            needed = {block.place[1] for block in exchange.blocks if block.place[0] == "slot"}
            needed.update(block.source_input for block in exchange.blocks)
            needed.update(image.place for image in exchange.images)
            located = occurrence_paths(_sent_document(exchange.sent, exchange.encoding, exchange.counted), needed)

        def path_of(place: tuple[int, ...] | None) -> str:
            return located[place][0] if place is not None and place in located else "$"

        image_paths = [path_of(image.place) for image in exchange.images]
        if exchange.blocks:
            ids = [store.block_id(block.text) for block in exchange.blocks]
            known = store.known_blocks(ids)
            for block, block_id in zip(exchange.blocks, ids, strict=True):
                text, replacements, exact = block.text, block.replacements, block.exact
                if not text:
                    continue
                if block.place[0] == "at":
                    path = block.place[1]
                elif block.place[0] == "image":
                    path = image_paths[block.place[1]] + "#text"
                else:
                    path = path_of(block.place[1])
                new = self.sent.first(snap.session, block_id)
                # A tool output's tool, and the file or the address it came from: what strict data mode covers depends
                # on them (the dashboard's columns answer). Where it came from is kept as the request carried it, its
                # call's input masked as every other text it kept, with where its values are masked: shown with them
                # from the vault, so that a value forgotten there is gone from it too.
                tool = block.tool
                source, source_found = None, []
                given = located.get(block.source_input) if block.source_input is not None else None
                left = tool_source(given[1]) if given is not None else None
                if left is not None:
                    source = kept_source(left)
                    source_found = found(source, False) if source else []
                types = self._types.get(block_id)
                # Seen before: stored already, or (metadata history, nothing stored) its counts cached.
                if block_id in known or (types is not None and self.history == "metadata"):
                    record = BlockRecord(
                        block_id, path, block.kind.value, block.from_model, new, tool=tool, source=source,
                        source_found=source_found,
                    )  # fmt: skip
                    if types is None:
                        unresolved.append(record)
                    else:
                        count(types, new)
                else:
                    if replacements and exact:
                        repl = replacement_dicts(replacements, self.engine.vault if self.engine else None)
                    elif replacements:  # masked as a JSON document: find the surrogates in its text
                        repl = found(text, True, {r.entity_id: r.rule for r in replacements if r.entity_id})
                    else:  # verify mode: masked by the Claude Code plugin before Latet saw it
                        repl = found(text, _looks_json(text), rule="plugin")
                    known.add(block_id)
                    record = BlockRecord(
                        block_id, path, block.kind.value, block.from_model, new, text, repl, tool=tool, source=source,
                        source_found=source_found,
                    )  # fmt: skip
                    kinds = Counter(
                        (r["type"], r["class"], r.get("module") or module_of(r.get("rule"), r["type"])) for r in repl
                    )
                    types = tuple((t, c, m, n) for (t, c, m), n in kinds.items())
                    self._types.put(block_id, types)
                    count(types, new)
                blocks.append(record)
        if unresolved:
            stored = store.block_types([b.id for b in unresolved])
            for record in unresolved:
                types = tuple(stored.get(record.id, ()))
                self._types.put(record.id, types)
                count(types, record.new)

        # The response, as the model wrote it.
        capture = exchange.capture
        response = None
        restored = 0
        if capture is not None:
            text = capture.history_text()
            text_spans = found(text, False, rule="restore") if text else []
            tool_calls = []
            for name, args in capture.tools:
                spans = found(args, True, rule="restore")
                restored += len(spans)
                tool_calls.append({"name": name, "args": args, "spans": spans})
            restored += len(text_spans)
            response = {
                "headers": redacted(exchange.response_headers),
                "text": text,
                "text_spans": text_spans,
                "tool_calls": tool_calls,
                **({"omitted": capture.omission} if capture.omission else {}),
            }

        flags: list[str] = []
        if exchange.block_reason or exchange.block_code:  # every refusal, whether its arena has words for it or not
            flags.append("blocked")
        leaks = [
            {"type": leak.etype, "kind": leak.kind.value, "preview": leak.preview, "new": leak.new}
            | ({"detail": leak.detail} if leak.detail else {})
            | ({"allowed": True} if leak.allowed else {})  # let through to auto mode's reviewer
            | self._place(leak, ids)
            for leak in exchange.leaks
        ]
        if any(not leak.get("allowed") for leak in leaks):  # details the user lets through are no leak
            flags.append("leak")
        flags += [f"uninspected:{u}" for u in sorted(set(exchange.uninspected))]
        states = {image.state for image in exchange.images}
        flags += [f"image:{s}" for s in ("masked", "withheld") if s in states]
        flags += [f"degraded:{d}" for d in sorted(set(exchange.degraded))]
        mode = exchange.mode
        # A bodyless call its request profile admitted (a model list, D1) is no unknown route (P2-8a, ruling 6).
        if exchange.protocol is None and mode == "passthrough" and exchange.profile is None:
            flags.append("unknown-route")
        if exchange.review:
            flags.append("auto-mode-review")
        if exchange.off:
            flags.append("project-off")
        if snap.project is not None and dict(snap.project).get("binding") == "claimed":
            flags.append("project-claimed")  # its project's rules that add protection alone applied (D7)
        if exchange.withheld:
            flags.append("withheld")  # the dump guard kept a tool output from the model
        if exchange.history:
            flags.append("history")  # the values in clear came from history Latet never masked
        # A tool outside this computer got stand-ins: stopped by the Claude Code plugin, or run by the provider.
        flags += [f"outbound:{tool}" for tool in sorted(set(exchange.outbound))]
        if exchange.bypass:
            flags.append("route-bypass")  # the plugin found the session's requests going around Latet
        if exchange.error:
            flags.append("error")
        agent, version = agent_of(dict(exchange.headers), exchange.path)
        ended = exchange.ended or time.time()
        summary = {
            "id": new_exchange_id(),
            "ts": exchange.started,
            "session": snap.session,
            "agent": agent,
            "agent_version": version,
            "upstream": exchange.upstream,
            "host": exchange.host,
            "signed": exchange.signed,
            "path": exchange.path,
            "protocol": exchange.protocol,
            "mode": mode,
            "transport": exchange.transport,
            "model": exchange.model,
            "status": exchange.status,
            "latency_ms": round((ended - exchange.started) * 1000),
            "overhead_ms": round((exchange.forwarded - exchange.started) * 1000) if exchange.forwarded else None,
            "tokens": dict(capture.usage) if capture and capture.usage else None,
            "masked": totals[0],
            "new_masked": totals[1],
            "by_type": dict(by_type),
            "new_by_type": dict(new_by_type),
            "by_class": dict(by_class),
            "by_module": dict(by_module),
            "restored": restored,
            "flags": flags,
            "block_reason": exchange.block_code,  # its category code: the words stay in the private view below
            "launch": exchange.launch,  # what its launch was decided as, by its fixed category (P2-6a, root's I-5)
            "passes": list(exchange.passes),  # the rules and exceptions it was let through by, by category (P2-7)
            "profile": exchange.profile,  # its envelope's request profile, by its id
            "removed": {"headers": exchange.removed_headers, "members": exchange.removed_members},
            "verified": mode != "passthrough" and not leaks and "blocked" not in flags and "project-off" not in flags,
            "project": dict(snap.project) if snap.project is not None else None,
        }
        source = snap.session
        project_id = valid_project_id(dict(snap.project).get("id")) if snap.project is not None else None
        summary["session"] = store.history_group(source)
        destination = (
            exchange.upstream
            if exchange.transport in ("http", "ws") and configured_destination(exchange.upstream)
            else None
        )
        public = history_coverage_summary(summary, exchange.history_code, summary["id"])
        public["session"] = summary["session"]
        public["project"] = history_project(project_id)
        if destination is not None:
            public["upstream"] = "custom"
        if self.history == "metadata":  # counts, timings and flags only: no text, no headers
            store.record(
                ExchangeRecord(
                    summary=summary,
                    leaks=safe_leaks(leaks),
                    body_bytes=exchange.body_bytes,
                    destination_name=destination,
                    session_source=source,
                    project_id=project_id,
                    history_code=exchange.history_code,
                )
            )
            return public
        private = {
            key: summary[key]
            for key in (
                "agent_version",
                "upstream",
                "host",
                "path",
                "model",
                "block_reason",
                "session",
                "project",
            )
        }
        private["session"] = source
        private["block_reason"] = exchange.block_reason  # the words told the agent: previews only, never values
        private["outbound"] = list(exchange.outbound)
        private["leaks"] = leaks
        store.record(
            ExchangeRecord(
                summary=summary,
                blocks=blocks,
                images=self._images(store, exchange.images, image_paths),
                request_headers=redacted(exchange.request_headers),
                response=response,
                leaks=leaks,
                body_bytes=exchange.body_bytes,
                sensitive=private,
                destination_name=destination,
                session_source=source,
                project_id=project_id,
                history_code=exchange.history_code,
            )
        )
        return public

    def _place(self, leak: Leak, ids: list[str]) -> dict[str, Any]:
        """Where a leak's value is, so the dashboard can mark it: the block it is in and its offsets there.
        The value is not repeated: it is in the block's text, which the history keeps. A history that keeps
        no texts has nothing to point at."""
        if self.history != "full" or not 0 <= leak.slot < len(ids) or leak.start < 0:
            return {}
        return {
            "block": ids[leak.slot],
            "masked_start": leak.start,
            "masked_end": leak.end,
            "class": leak.eclass.name,
            "rule": kept(leak.rule),  # a computer's history keeps it in clear: a field's name in it by its kind
        }

    @staticmethod
    def _images(store: HistoryStore, images: tuple[_Image, ...], paths: list[str]) -> list[ImageRecord]:
        """The request's images as they were sent, each where it was (``paths``): kept once each, by their bytes; a
        withheld one only listed."""
        out = []
        for image, path in zip(images, paths, strict=True):
            block = store.block_id(image.text) if image.text is not None else None
            if image.data is None:  # withheld
                out.append(ImageRecord(path, image.state, block=block))
                continue
            data = image.data.canonical() if isinstance(image.data, EncodedImage) else image.data
            kept = len(data) * 3 // 4 <= MAX_IMAGE
            image_id = store.image_id(data) if kept else None
            out.append(ImageRecord(path, image.state, image_id, image.media_type, data if kept else None, block))
        return out
