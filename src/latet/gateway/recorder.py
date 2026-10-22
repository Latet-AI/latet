"""Turn each exchange into a dashboard record: what was sent (block by block), what came back,
how long Latet took, and the token usage the provider reported.

Recording runs on its own thread once an exchange is over: it never delays the traffic it observes,
and a failure there is logged, never raised.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from latet import observe
from latet.engine import wellformed
from latet.engine.modules import kept, module_of, origin
from latet.engine.pipeline import LRU, Engine
from latet.engine.rehydrate import Annotator, Restore, restores_for
from latet.engine.types import Replacement, SlotKind
from latet.engine.vault import Vault
from latet.gateway.headers import redacted
from latet.protocols.eventstream import EventStreamDecoder
from latet.protocols.sse import SSEParser, StreamDecoder
from latet.state.local import LocalJobLock, LocalSentBlocks
from latet.storage.exchanges import (
    MAX_IMAGE,
    BlockRecord,
    ExchangeRecord,
    ImageRecord,
    new_exchange_id,
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
    here either. Returned replacements carry offsets on both sides.
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


@dataclass
class ResponseCapture:
    """The response as the model wrote it (before any restore): text, tool calls, token usage. What the forwarder hands
    it on the event loop is only kept, ``_MAX_JSON_BODY`` of the wire at most, and read by ``settle`` in the recorder's
    thread (a chunk of tens of thousands of tiny events, read where it was handed over, held the loop). It decodes at
    most ``_MAX_JSON_BODY`` of an encoded answer, and reads one event or body of at most as much, and at most
    ``MAX_EVENTS`` events: past that it is ``broken``, and decodes nothing more. A WebSocket turn's events are read as
    they come (``event``), one message each."""

    protocol: str | None
    content_type: str = ""
    content_encoding: str | None = None
    text: list[str] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    size: int = 0
    done: bool = False  # a terminal event was seen (WebSocket turns)
    failed: bool = False  # ... and it was an error
    broken: bool = False
    _events: SSEParser = field(default_factory=lambda: SSEParser(limit=_MAX_JSON_BODY))
    _body: bytearray = field(default_factory=bytearray)
    _decoder: StreamDecoder | None = None
    _frames: EventStreamDecoder | None = None  # Bedrock's answers
    _raw: list[bytes] = field(default_factory=list)  # the chunks handed over, still encoded: read by ``settle``
    _raw_bytes: int = 0
    _ended: bool = False
    _settled: bool = False

    def __post_init__(self) -> None:
        if self.content_type:
            self._decoder = StreamDecoder(self.content_encoding, limit=_MAX_JSON_BODY)

    @property
    def _sse(self) -> bool:
        return "text/event-stream" in self.content_type

    @property
    def _eventstream(self) -> bool:
        return "vnd.amazon.eventstream" in self.content_type

    def feed(self, chunk: bytes) -> None:
        """A raw upstream chunk (still content-encoded), kept for ``settle``: on the event loop, where the forwarder
        hands it over, it is neither decoded nor read. Past ``_MAX_JSON_BODY`` kept, the capture is ``broken`` and keeps
        nothing."""
        if self.broken or self._decoder is None or self._settled:
            return
        if self._raw_bytes + len(chunk) > _MAX_JSON_BODY:
            self.broken = True
            self._raw.clear()
            return
        self._raw.append(bytes(chunk))
        self._raw_bytes += len(chunk)

    def end(self) -> None:
        """The answer is over: what was kept is read by ``settle``."""
        self._ended = True

    def settle(self) -> None:
        """What was kept, decoded and read, once (the recorder's thread): its text, its tool calls, its usage."""
        if self._settled:
            return
        self._settled = True
        kept, self._raw = self._raw, []
        for chunk in kept:
            self._read(chunk)
        if self._ended:
            self._read_end()

    def _read(self, chunk: bytes) -> None:
        if self.broken or self._decoder is None:
            return
        try:
            data = self._decoder.feed(chunk)  # past its cap it raises, and nothing more is decoded
            if self._sse:
                self._feed_sse(data)
            elif self._eventstream:
                self._feed_frames(data)
            elif len(self._body) + len(data) > _MAX_JSON_BODY:  # not parsed: not decoded any further either
                self.broken = True
            else:
                self._body += data
        except Exception as exc:
            self.broken = True
            log.debug("response capture failed: %s", type(exc).__name__)  # by its type: its words can quote the answer

    def _read_end(self) -> None:
        if self.broken or self._decoder is None:
            return
        try:
            data = self._decoder.flush()
            if self._sse:
                self._feed_sse(data + b"\n\n")
            elif self._eventstream:
                self._feed_frames(data)
            elif "json" in self.content_type:
                self._body += data
                self.feed_json(bytes(self._body))
        except Exception as exc:
            self.broken = True
            log.debug("response capture failed: %s", type(exc).__name__)

    def _feed_sse(self, data: bytes) -> None:
        for event in self._events.feed(data):  # an event that never ends past the cap raises: broken
            if event.data and event.data != "[DONE]":
                self.event(event.data)

    def _feed_frames(self, data: bytes) -> None:
        """Bedrock's frames: each ``chunk`` event carries one of Anthropic's stream events, in base64."""
        if self._frames is None:
            self._frames = EventStreamDecoder()
        for message in self._frames.feed(data):
            kind = message.headers.get(":message-type")
            if kind == "event" and message.headers.get(":event-type") == "chunk":
                self.event(base64.b64decode(json.loads(message.payload)["bytes"]).decode("utf-8", "replace"))
            elif kind in ("exception", "error"):
                self.done = self.failed = True

    def event(self, data: str) -> None:
        """One event's JSON (an SSE ``data:`` payload or a WebSocket text frame)."""
        try:
            obj = wellformed.loads(data)
        except ValueError:
            return
        if isinstance(obj, dict):
            self._event(obj)

    def _event(self, obj: dict[str, Any]) -> None:
        etype = str(obj.get("type", ""))
        if self.protocol == "gemini":
            self._gemini(obj)
            return
        if self.protocol == "anthropic":
            if etype == "content_block_start" and (obj.get("content_block") or {}).get("type") == "tool_use":
                block = obj["content_block"]
                self.tools[str(obj.get("index"))] = {"name": block.get("name", ""), "args": ""}
            elif etype == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self._add_text(delta.get("text", ""))
                elif delta.get("type") == "input_json_delta" and str(obj.get("index")) in self.tools:
                    self._add_args(self.tools[str(obj.get("index"))], delta.get("partial_json", ""))
            elif etype in ("message_start", "message_delta"):
                self._anthropic_usage((obj.get("message") or {}).get("usage") or obj.get("usage") or {})
            elif etype in ("message_stop", "error"):
                self.done = True
                self.failed = etype == "error"
        elif etype.startswith("response.") or etype == "error":
            if etype == "response.output_text.delta":
                self._add_text(obj.get("delta", ""))
            elif etype in ("response.function_call_arguments.delta", "response.custom_tool_call_input.delta"):
                tool = self.tools.setdefault(str(obj.get("item_id")), {"name": "", "args": ""})
                self._add_args(tool, obj.get("delta", ""))
            elif etype == "response.output_item.added":
                item = obj.get("item") or {}
                if item.get("type") in ("function_call", "custom_tool_call"):
                    self.tools[str(item.get("id"))] = {"name": item.get("name", ""), "args": ""}
            if etype in ("response.completed", "response.done", "response.incomplete", "response.failed", "error"):
                self.done = True
                self.failed = etype in ("response.failed", "error")
                self._responses_usage((obj.get("response") or {}).get("usage") or {})
        elif "choices" in obj:
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or choice.get("message") or {}
                if isinstance(delta.get("content"), str):
                    self._add_text(delta["content"])
                for call in delta.get("tool_calls") or []:
                    tool = self.tools.setdefault(str(call.get("index", len(self.tools))), {"name": "", "args": ""})
                    fn = call.get("function") or {}
                    tool["name"] = tool["name"] or fn.get("name", "")
                    self._add_args(tool, fn.get("arguments") or "")
            usage = obj.get("usage") or {}
            if usage:
                cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                self.usage = {
                    "input": int(usage.get("prompt_tokens") or 0) - cached,
                    "output": int(usage.get("completion_tokens") or 0),
                    "cache_read": cached,
                    "cache_write": 0,
                }

    def feed_json(self, body: bytes) -> None:
        """A whole non-streamed response body."""
        try:
            obj = wellformed.loads(body)
        except ValueError:
            return
        if self.protocol == "gemini" and isinstance(obj, list):  # a stream sent as one array of answers
            for answer in obj:
                if isinstance(answer, dict):
                    self._gemini(answer)
            self.done = True
            return
        if not isinstance(obj, dict):
            return
        if self.protocol == "anthropic":
            for block in obj.get("content") or []:
                if block.get("type") == "text":
                    self._add_text(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input"), ensure_ascii=False, separators=(",", ":"))
                    self.tools[str(len(self.tools))] = {"name": block.get("name", ""), "args": args}
            self._anthropic_usage(obj.get("usage") or {})
        elif self.protocol == "responses":
            for item in obj.get("output") or []:
                if item.get("type") == "message":
                    for part in item.get("content") or []:
                        if part.get("type") == "output_text":
                            self._add_text(part.get("text", ""))
                elif item.get("type") in ("function_call", "custom_tool_call"):
                    args = item.get("arguments") or item.get("input") or ""
                    self.tools[str(item.get("id"))] = {"name": item.get("name", ""), "args": args}
            self._responses_usage(obj.get("usage") or {})
        else:
            self._event(obj)
        self.done = True

    def _gemini(self, obj: dict[str, Any]) -> None:
        """One of Gemini's answers, whole or an event of a stream: its candidates' text (not their thoughts), their
        function calls, the token counts, and whether it finished or failed."""
        if "error" in obj:
            self.done = self.failed = True
            return
        candidates = obj.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and not part.get("thought"):
                    self._add_text(part["text"])
                call = part.get("functionCall")
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    args = call.get("args")
                    text = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args is not None else ""
                    tool = self.tools[str(len(self.tools))] = {"name": call["name"], "args": ""}
                    self._add_args(tool, text)
            if isinstance(candidate, dict) and candidate.get("finishReason"):
                self.done = True
        usage = obj.get("usageMetadata")
        if isinstance(usage, dict):
            cached = int(usage.get("cachedContentTokenCount") or 0)
            self.usage = {
                "input": int(usage.get("promptTokenCount") or 0) - cached,
                "output": int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
                "cache_read": cached,
                "cache_write": 0,
            }

    def _add_text(self, text: str) -> None:
        if self.size < _MAX_RESPONSE:
            self.text.append(text)
            self.size += len(text)

    def _add_args(self, tool: dict[str, Any], text: str) -> None:
        if self.size < _MAX_RESPONSE:  # a Write of a huge file must not grow the record without bound
            tool["args"] += text
            self.size += len(text)

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


@dataclass(slots=True)
class _Snapshot:
    """Taken on the event loop, where sessions change: what the recorder thread may read."""

    session: str
    text: list[Restore]
    json: list[Restore]
    version: tuple[int, int] = (0, 0)  # (entities in the session, vault version): same restores
    project: dict[str, str] | None = None  # {id, name} of the session's project


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

    def finish(self, outcome: Outcome) -> None:
        """Record an exchange (call on the event loop). Returns at once; the work is queued."""
        observe.exchange(outcome)  # a server's metrics count every exchange, whatever the history keeps
        if self._executor is None or self.history == "off":
            return
        try:
            outcome.ended = outcome.ended or time.time()
            snapshot = self._snapshot(outcome.prepared)
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
                future = self._executor.submit(self._record, outcome, snapshot)
            except BaseException:
                self._left()  # never queued (its thread ended meanwhile): not pending
                raise
            future.add_done_callback(lambda f: self._done(f, loop))
        except Exception as exc:  # the dashboard must never break the traffic it observes
            log.error("could not queue an exchange for the dashboard: %s", type(exc).__name__)  # its words can quote it

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

    def _snapshot(self, prepared: Prepared | None) -> _Snapshot:
        session = getattr(prepared, "session", None)
        project = self._project(session)
        if session is None or self.engine is None or not session.entity_ids:
            return _Snapshot(getattr(session, "key", None) or "unscoped", [], [], project=project)
        return _Snapshot(
            session.key,
            self.engine.restores(session, json_mode=False),
            self.engine.restores(session, json_mode=True),
            (len(session.entity_ids), self.engine.vault.version),
            project,
        )

    def _project(self, session: Any) -> dict[str, str] | None:
        pid = getattr(session, "project_id", None)
        peek = getattr(getattr(self.engine, "scopes", None), "peek", None)
        project = peek(pid) if pid and peek is not None else None
        return {"id": project.id, "name": project.name} if project is not None else None

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

    def _record(self, outcome: Outcome, snap: _Snapshot) -> dict[str, Any]:
        store = self.store
        assert store is not None
        if time.time() - self._last_prune > _PRUNE_EVERY:
            self.prune_now()  # a job of its own, after this one: its failure never loses this exchange
        prepared = outcome.prepared

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

        pending = list(getattr(prepared, "pending", None) or [])
        # The inputs of tools that ran outside this computer with stand-ins in them, as blocks of their own.
        pending += [(slot, [], False) for slot in getattr(prepared, "outbound_slots", None) or []]
        unresolved: list[BlockRecord] = []  # stored earlier, counts not cached (after a restart)
        ids: list[str] = []  # the block of each slot, empty ones included: a leak counts its slot among them
        if pending:
            paths = json_paths(prepared.document) if prepared is not None and prepared.document is not None else {}
            texts = [slot.get() for slot, _, _ in pending]
            ids = [store.block_id(t) for t in texts]
            known = store.known_blocks(ids)
            for (slot, replacements, exact), text, block_id in zip(pending, texts, ids, strict=True):
                if not text:
                    continue
                path = paths.get((id(slot.container), slot.key), "$")
                where = getattr(slot.container, "path_key", None)  # an image's text: the image's path
                if where is not None:
                    path = paths.get(where, "$") + "#text"
                new = self.sent.first(snap.session, block_id)
                # A tool output's tool, and the file or the address it came from: what strict data mode covers depends
                # on them (the dashboard's columns answer). Where it came from is kept as the request carried it, its
                # call's input masked as every other text it kept, with where its values are masked: shown with them
                # from the vault, so that a value forgotten there is gone from it too.
                tool = getattr(slot, "tool", None) if slot.kind == SlotKind.TOOL_RESULT else None
                source, source_found = None, []
                sent = getattr(slot, "sent_source", None)
                if slot.kind == SlotKind.TOOL_RESULT and callable(sent) and isinstance(left := sent(), str):
                    source = kept_source(left)
                    source_found = found(source, False) if source else []
                types = self._types.get(block_id)
                # Seen before: stored already, or (metadata history, nothing stored) its counts cached.
                if block_id in known or (types is not None and self.history == "metadata"):
                    record = BlockRecord(
                        block_id, path, slot.kind.value, slot.from_model, new, tool=tool, source=source,
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
                        block_id, path, slot.kind.value, slot.from_model, new, text, repl, tool=tool, source=source,
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
        capture = outcome.capture
        response = None
        restored = 0
        if capture is not None:
            capture.settle()  # read here, off the event loop that handed it over
            text = "".join(capture.text)
            text_spans = found(text, False, rule="restore")
            tool_calls = []
            for tool in capture.tools.values():
                spans = found(tool["args"], True, rule="restore")
                restored += len(spans)
                tool_calls.append({"name": tool["name"], "args": tool["args"], "spans": spans})
            restored += len(text_spans)
            response = {
                "headers": redacted(outcome.response_headers),
                "text": text,
                "text_spans": text_spans,
                "tool_calls": tool_calls,
            }

        flags: list[str] = []
        if getattr(prepared, "block_reason", None):
            flags.append("blocked")
        leaks = [
            {"type": leak.etype, "kind": leak.kind.value, "preview": leak.preview, "new": leak.new}
            | ({"detail": leak.detail} if leak.detail else {})
            | ({"allowed": True} if leak.allowed else {})  # let through to auto mode's reviewer
            | self._place(leak, ids)
            for leak in getattr(prepared, "leaks", None) or []
        ]
        if any(not leak.get("allowed") for leak in leaks):  # details the user lets through are no leak
            flags.append("leak")
        flags += [f"uninspected:{u}" for u in sorted(set(getattr(prepared, "uninspected", None) or []))]
        states = {getattr(w, "state", None) for w in getattr(prepared, "images", None) or []}
        flags += [f"image:{s}" for s in ("masked", "withheld") if s in states]
        flags += [f"degraded:{d}" for d in sorted(set(getattr(prepared, "degraded", None) or []))]
        mode = getattr(prepared, "mode", "passthrough")
        if getattr(prepared, "protocol", None) is None and mode == "passthrough":
            flags.append("unknown-route")
        if getattr(prepared, "review", False):
            flags.append("auto-mode-review")
        if getattr(prepared, "off", False):
            flags.append("project-off")
        if getattr(prepared, "withheld", 0):
            flags.append("withheld")  # the dump guard kept a tool output from the model
        if getattr(prepared, "history", False):
            flags.append("history")  # the values in clear came from history Latet never masked
        # A tool outside this computer got stand-ins: stopped by the Claude Code plugin, or run by the provider.
        flags += [f"outbound:{tool}" for tool in sorted(set(getattr(prepared, "outbound", None) or []))]
        if getattr(prepared, "bypass", False):
            flags.append("route-bypass")  # the plugin found the session's requests going around Latet
        if outcome.error:
            flags.append("error")
        agent, version = agent_of(outcome.headers, outcome.path)
        ended = outcome.ended or time.time()
        summary = {
            "id": new_exchange_id(),
            "ts": outcome.started,
            "session": snap.session,
            "agent": agent,
            "agent_version": version,
            "upstream": outcome.upstream,
            "host": outcome.host,
            "signed": outcome.signed,
            "path": outcome.path,
            "protocol": getattr(prepared, "protocol", None),
            "mode": mode,
            "transport": outcome.transport,
            "model": getattr(prepared, "model", None),
            "status": outcome.status,
            "latency_ms": round((ended - outcome.started) * 1000),
            "overhead_ms": round((outcome.forwarded - outcome.started) * 1000) if outcome.forwarded else None,
            "tokens": (capture.usage or None) if capture else None,
            "masked": totals[0],
            "new_masked": totals[1],
            "by_type": dict(by_type),
            "new_by_type": dict(new_by_type),
            "by_class": dict(by_class),
            "by_module": dict(by_module),
            "restored": restored,
            "flags": flags,
            "block_reason": getattr(prepared, "block_reason", None),  # previews only, never values
            "verified": mode != "passthrough" and not leaks and "blocked" not in flags and "project-off" not in flags,
            "project": snap.project,
        }
        if self.history == "metadata":  # counts, timings and flags only: no text, no headers
            store.record(ExchangeRecord(summary=summary, leaks=leaks, body_bytes=outcome.body_bytes))
            return summary
        store.record(
            ExchangeRecord(
                summary=summary,
                blocks=blocks,
                images=self._images(store, prepared),
                request_headers=redacted(outcome.request_headers),
                response=response,
                leaks=leaks,
                body_bytes=outcome.body_bytes,
            )
        )
        return summary

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
    def _images(store: HistoryStore, prepared: Any) -> list[ImageRecord]:
        """The request's images as they were sent: kept once each, by their bytes; a withheld one only listed."""
        works = getattr(prepared, "images", None) or []
        if not works:
            return []
        paths = json_paths(prepared.document) if prepared.document is not None else {}
        out = []
        for work in works:
            path = paths.get(work.path_key, "$")
            block = store.block_id(work.text) if work.slot is not None and work.text else None
            if work.state == "withheld":
                out.append(ImageRecord(path, work.state, block=block))
                continue
            data = work.ref.data()
            kept = len(data) * 3 // 4 <= MAX_IMAGE
            image_id = store.image_id(data) if kept else None
            out.append(ImageRecord(path, work.state, image_id, work.ref.media_type, data if kept else None, block))
        return out
