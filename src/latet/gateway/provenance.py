"""Bounded, detached model-output records submitted to the mediator's existing engine executor."""

import hashlib
import hmac
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any

from latet.protocols.rewrite import _utf8_size
from latet.protocols.sse import ResponseError

log = logging.getLogger(__name__)

ECHO_RULE = "observed opaque provider echo (1)"
ENCRYPTED_RULE = "provider encrypted field (1)"
ECHO_VALUE_CHARS = 1 << 20
ECHO_ENTRIES = 1024
ECHO_CONVERSATION_LIMIT = 1 << 20
ECHO_LOCAL_LIMIT = 16 << 20
ECHO_FRONT_LIMIT = 4 << 20
# Rounded conservative owned-container charges, qualified against actual containers in P2-3 tests.
ECHO_ENTRY_BYTES = 1024
ECHO_CONVERSATION_BYTES = 1024
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}\Z", re.ASCII)


def encrypted_value(value: str) -> bool:
    if not value or len(value) > ECHO_VALUE_CHARS or not _BASE64.fullmatch(value):
        return False
    if any(c in value for c in "+/") and any(c in value for c in "-_"):
        return False
    bare = len(value.rstrip("="))
    padding = len(value) - bare
    return bare % 4 != 1 and (not padding or (len(value) % 4 == 0 and padding == (-bare % 4)))


@dataclass(frozen=True, slots=True, repr=False)
class EchoScope:
    binding: tuple[str, ...]
    protocol: str
    fallback: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class OpaqueField:
    holder: dict[str, Any]
    key: str
    position: tuple[str | int, ...]
    model: bool
    fallback: bool


def opaque_fields(body: Any, protocol: str, *, answer: bool = False) -> list[OpaqueField]:
    """Only declared opaque leaves; history ordinals are absent, internal sibling positions are retained."""
    out: list[OpaqueField] = []

    def leaf(holder: dict, key: str, path: tuple, model: bool, fallback: bool = False) -> None:
        if key in holder:
            out.append(OpaqueField(holder, key, (*path, key), model, fallback))

    def blocks(items: Any, path: tuple, model: bool, *, depth: int = 0) -> None:
        if depth > 64 or not isinstance(items, list):
            return
        for index, block in enumerate(items):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if not isinstance(kind, str):
                continue
            at = (*path, index, kind)
            if kind == "thinking":
                leaf(block, "signature", at, model, protocol == "anthropic")
            elif kind == "redacted_thinking":
                leaf(block, "data", at, model, protocol == "anthropic")
            elif kind == "compaction":
                leaf(block, "encrypted_content", at, model)
            if kind in {"text", "output_text"}:
                key = "citations" if kind == "text" else "annotations"
                citations = block.get(key)
                for ci, citation in enumerate(citations if isinstance(citations, list) else []):
                    if isinstance(citation, dict) and citation.get("type") in {
                        "char_location",
                        "page_location",
                        "content_block_location",
                        "web_search_result_location",
                        "url_citation",
                    }:
                        leaf(
                            citation,
                            "encrypted_index",
                            (*at, key, ci, citation["type"]),
                            model,
                            protocol == "anthropic" and citation["type"] == "web_search_result_location",
                        )
            if kind in {
                "web_search_tool_result",
                "web_fetch_tool_result",
                "code_execution_tool_result",
                "bash_code_execution_tool_result",
                "text_editor_code_execution_tool_result",
                "tool_search_tool_result",
                "advisor_tool_result",
            }:
                results(block.get("content"), (*at, "content"), model, kind, depth + 1)
            elif kind in {"tool_result", "mcp_tool_result"}:
                # A tool result is not a model-authored position, even inside an assistant message.
                blocks(block.get("content"), (*at, "content"), False, depth=depth + 1)

    def results(value: Any, path: tuple, model: bool, wrapper: str, depth: int) -> None:
        if depth > 64:
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                results(item, (*path, index), model, wrapper, depth + 1)
        elif isinstance(value, dict) and value.get("type") in {
            "web_search_result",
            "web_fetch_result",
            "code_execution_result",
            "bash_code_execution_result",
            "text_editor_code_execution_view_result",
            "text_editor_code_execution_create_result",
            "text_editor_code_execution_str_replace_result",
            "tool_search_tool_search_result",
            "advisor_result",
            "web_search_tool_result_error",
            "web_fetch_tool_result_error",
            "code_execution_tool_result_error",
        }:
            kind = value["type"]
            leaf(
                value,
                "encrypted_content",
                (*path, kind),
                model,
                protocol == "anthropic" and wrapper == "web_search_tool_result" and kind == "web_search_result",
            )

    def message(message: Any) -> None:
        if isinstance(message, dict):
            blocks(message.get("content"), ("model", "content"), message.get("role") == "assistant")

    def response_item(item: Any) -> None:
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "message":
            message(item)
        elif kind in {"reasoning", "compaction", "image_generation_call"}:
            model = item.get("role", "assistant") == "assistant"
            leaf(
                item,
                "result" if kind == "image_generation_call" else "encrypted_content",
                ("model", "item", kind),
                model,
                kind in {"reasoning", "compaction"},
            )
            blocks(item.get("summary"), ("model", "item", kind, "summary"), model)
            blocks(item.get("content"), ("model", "item", kind, "content"), model)

    def gcontent(content: Any) -> None:
        if not isinstance(content, dict):
            return
        parts = content.get("parts")
        for index, part in enumerate(parts if isinstance(parts, list) else []):
            if isinstance(part, dict):
                for key in ("thoughtSignature", "thought_signature"):
                    if key in part:
                        out.append(
                            OpaqueField(
                                part,
                                key,
                                ("model", "parts", index, "thoughtSignature"),
                                content.get("role") == "model",
                                True,
                            )
                        )

    if protocol == "gemini":
        if answer and isinstance(body, list):
            for item in body:
                out.extend(opaque_fields(item, protocol, answer=True))
        elif isinstance(body, dict):
            if answer:
                for candidate in body.get("candidates", []):
                    if isinstance(candidate, dict):
                        gcontent(candidate.get("content"))
            else:
                for content in body.get("contents", []):
                    gcontent(content)
    elif isinstance(body, dict):
        if protocol == "responses":
            if not answer and isinstance(body.get("response"), dict):
                out.extend(opaque_fields(body["response"], protocol))
            items = body.get("output" if answer else "input")
            if isinstance(items, list):
                for item in items:
                    response_item(item)
        elif answer and protocol == "anthropic":
            message(body)
        elif answer and protocol == "chat":
            for choice in body.get("choices", []):
                if isinstance(choice, dict):
                    message(choice.get("message"))
        else:
            for item in body.get("messages", []):
                message(item)
    return out


class LocalEchoes:
    """Process-owned permission metadata. No value, session string or mutable payload is retained."""

    def __init__(
        self,
        *,
        limit: int = ECHO_LOCAL_LIMIT,
        entries: int = ECHO_ENTRIES,
        conversation_limit: int = ECHO_CONVERSATION_LIMIT,
    ) -> None:
        self.limit, self.entries, self.conversation_limit = limit, entries, conversation_limit
        self._items: OrderedDict[bytes, tuple[bytes, float]] = OrderedDict()
        self._conversations: dict[bytes, int] = {}
        self._reserved = 0
        self._lock = RLock()
        self.ceiling: Callable[[int], str | None] | None = None
        self.closed = False

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return (
                len(self._items) * ECHO_ENTRY_BYTES
                + len(self._conversations) * ECHO_CONVERSATION_BYTES
                + self._reserved
            )

    def reserve(self, size: int) -> bool:
        with self._lock:
            if self.closed or size < 0 or self.retained_bytes + size > self.limit:
                return False
            if self.ceiling is not None and self.ceiling(size) is not None:
                return False
            self._reserved += size
            return True

    def release(self, size: int) -> None:
        with self._lock:
            assert 0 <= size <= self._reserved
            self._reserved -= size

    def _remove(self, member: bytes) -> None:
        conversation, _ = self._items.pop(member)
        remaining = self._conversations[conversation] - 1
        if remaining:
            self._conversations[conversation] = remaining
        else:
            del self._conversations[conversation]
        if not self._items:
            self._items = OrderedDict()
            self._conversations = {}

    def read(self, members: tuple[bytes, ...]) -> tuple[frozenset[bytes], float]:
        with self._lock:
            now = time.monotonic()
            found = set()
            expiry = float("inf")
            for member in members:
                item = self._items.get(member)
                if item is not None:
                    if item[1] <= now:
                        self._remove(member)
                    else:
                        found.add(member)
                        expiry = min(expiry, item[1])
                        self._items.move_to_end(member)
            return frozenset(found), expiry

    def keep(self, conversation: bytes, members: tuple[bytes, ...], *, expires: float = float("inf")) -> bool:
        unique = tuple(dict.fromkeys(members))
        if not unique:
            return True
        if (
            len(unique) > self.entries
            or len(unique) * ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES > self.conversation_limit
        ):
            return False
        with self._lock:
            if self.closed:
                return False
            added = sum(member not in self._items for member in unique)
            wanted = added * ECHO_ENTRY_BYTES + (0 if conversation in self._conversations else ECHO_CONVERSATION_BYTES)
            if wanted + self._reserved > self.limit:
                return False
            while self._items and (
                self.retained_bytes + wanted > self.limit
                or self._conversations.get(conversation, 0) + added > self.entries
                or (self._conversations.get(conversation, 0) + added) * ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES
                > self.conversation_limit
            ):
                self._remove(next(iter(self._items)))
                added = sum(member not in self._items for member in unique)
                wanted = added * ECHO_ENTRY_BYTES + (
                    0 if conversation in self._conversations else ECHO_CONVERSATION_BYTES
                )
            if not self.reserve(wanted):
                return False
            try:
                for member in unique:
                    if member not in self._items:
                        self._conversations[conversation] = self._conversations.get(conversation, 0) + 1
                    self._items[member] = (conversation, expires)
                    self._items.move_to_end(member)
            finally:
                self.release(wanted)
            return True

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self._items = OrderedDict()
            self._conversations = {}


class NoEchoes(LocalEchoes):
    """An explicitly absent backend cannot invent local observation authority."""

    def __init__(self) -> None:
        super().__init__(limit=0)


def _framed(key: bytes, values: tuple[str | int, ...]) -> Any:
    digest = hmac.new(key, b"latet-opaque-1\0", hashlib.sha256)
    for value in values:
        data = str(value).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest


class EchoCheck:
    """One preparation's exact opaque value set and one authoritative lookup, reused by final inspection."""

    def __init__(self, key: bytes, backend: Any, scope: EchoScope, session: str, body: Any) -> None:
        self.key, self.scope, self.session = key, scope, session
        self.backend = backend
        self.found: frozenset[bytes] = frozenset()
        self.expires = 0.0
        self._charge = 0
        self._lock = RLock()
        try:
            fields = opaque_fields(body, scope.protocol)
        except (TypeError, ValueError):
            fields = []  # malformed typed containers are refused by the body inspector
        count = sum(self.valid(field) for field in fields)
        # The same owner covers both temporary lookup containers and the exact receipt retained across awaits.
        # The returned subset alone binds final checks to the originally requested, scoped values.
        charge = (count + 1) * ECHO_ENTRY_BYTES if count else 0
        if not charge or not backend.reserve(charge):
            return
        self._charge = charge
        try:
            members = frozenset(self.member(field) for field in fields if self.valid(field))
            found, expires = backend.read(tuple(members))
            if not isinstance(found, frozenset) or not found <= members:
                return
            self.found, self.expires = found, expires
        except Exception:
            self.found, self.expires = frozenset(), 0.0
        finally:
            kept = (len(self.found) + 1) * ECHO_ENTRY_BYTES if self.found else 0
            backend.release(self._charge - kept)
            self._charge = kept

    def close(self) -> None:
        with self._lock:
            self.found, self.expires = frozenset(), 0.0
            charge, self._charge = self._charge, 0
            self.backend.release(charge)

    def __del__(self) -> None:
        # Defensive for standalone callers. Transport/worker owners explicitly close at their end boundary.
        self.close()

    @staticmethod
    def valid(field: OpaqueField) -> bool:
        value = field.holder[field.key]
        return field.model and isinstance(value, str) and len(value) <= ECHO_VALUE_CHARS

    def member(self, field: OpaqueField) -> bytes:
        digest = _framed(self.key, (*self.scope.binding, self.session, self.scope.protocol, *field.position))
        digest.update(b"\0value\0")
        value = field.holder[field.key]
        for start in range(0, len(value), 4096):
            digest.update(value[start : start + 4096].encode("utf-8"))
        return digest.digest()

    def rules(self, body: Any) -> dict[tuple[int, str], str]:
        if self.found and time.monotonic() >= self.expires:
            self.close()
        rules = {}
        try:
            fields = opaque_fields(body, self.scope.protocol)
        except (TypeError, ValueError):
            return rules
        for field in fields:
            if not self.valid(field):
                continue
            member = self.member(field)
            if member in self.found and time.monotonic() < self.expires:
                rules[id(field.holder), field.key] = ECHO_RULE
            elif self.scope.fallback and field.fallback and encrypted_value(field.holder[field.key]):
                rules[id(field.holder), field.key] = ENCRYPTED_RULE
        return rules


class OpaqueCapture:
    """Digest-only response assembly. The async transport owner awaits commit before releasing completion."""

    def __init__(self, check: EchoCheck, backend: Any) -> None:
        self.check, self.backend = check, backend
        self.conversation = _framed(check.key, ("conversation", check.session)).digest()
        self.pending: dict[bytes, None] = {}
        self.signatures: dict[int, tuple[Any, int]] = {}
        self.citations: dict[int, int] = {}
        self.blocks: dict[int, bool] = {}
        self.charge = 0
        self.ready = False
        self.closed = False
        self.running = False
        self._working = False
        self._lock = RLock()

    def _reserve(self, size: int) -> bool:
        if self.closed or not self.backend.reserve(size):
            self.close()
            return False
        self.charge += size
        return True

    def _member(self, member: bytes) -> None:
        if member not in self.pending and self._reserve(ECHO_ENTRY_BYTES):
            self.pending[member] = None

    def _fields(self, body: Any, *, block_index: int | None = None) -> None:
        for field in opaque_fields(body, self.check.scope.protocol, answer=True):
            if not self.check.valid(field):
                continue
            if block_index is not None:
                field = OpaqueField(
                    field.holder,
                    field.key,
                    (*field.position[:2], block_index, *field.position[3:]),
                    field.model,
                    field.fallback,
                )
            self._member(self.check.member(field))

    def _begin(self) -> bool:
        with self._lock:
            if self.closed or self.running:
                return False
            if self._working:
                # A capture has one parsing owner. Unexpected overlapping input loses evidence conservatively.
                self.closed = True
                return False
            self._working = True
            return True

    def _end(self) -> None:
        with self._lock:
            self._working = False
            if self.closed and not self.running:
                self._discard()

    def body(self, body: Any) -> None:
        if not self._begin():
            return
        try:
            self._body(body)
        except (TypeError, ValueError, UnicodeError):
            self.close()
        finally:
            self._end()

    def _body(self, body: Any) -> None:
        if isinstance(body, dict) and (
            body.get("error")
            or not isinstance(body.get("status", "completed"), str)
            or body.get("status") in {"failed", "incomplete", "cancelled"}
        ):
            self.close()
            return
        self._fields(body)
        self.ready = True

    def event(self, event: Any) -> None:
        if not isinstance(event, dict) or not self._begin():
            return
        try:
            if not self.ready:
                self._event(event)
        except (TypeError, ValueError, UnicodeError):
            self.close()
        finally:
            self._end()

    def _event(self, event: dict[str, Any]) -> None:
        protocol, kind = self.check.scope.protocol, event.get("type")
        if protocol == "anthropic":
            index = event.get("index")
            if kind == "message_stop":
                if self.signatures or self.citations or not all(self.blocks.values()):
                    self.close()
                else:
                    self.ready = True
                return
            if kind == "error":
                self.close()
                return
            if type(index) is not int or not 0 <= index < 100_000:
                return
            if kind == "content_block_start":
                block = event.get("content_block")
                if not isinstance(block, dict):
                    return
                if index in self.blocks or not self._reserve(ECHO_ENTRY_BYTES):
                    self.close()
                    return
                self.blocks[index] = False
                if block.get("type") == "thinking":
                    value = block.get("signature", "")
                    if (
                        not isinstance(value, str)
                        or len(value) > ECHO_VALUE_CHARS
                        or not self._reserve(ECHO_ENTRY_BYTES)
                    ):
                        self.close()
                        return
                    digest = _framed(
                        self.check.key,
                        (
                            *self.check.scope.binding,
                            self.check.session,
                            protocol,
                            "model",
                            "content",
                            index,
                            "thinking",
                            "signature",
                        ),
                    )
                    digest.update(b"\0value\0")
                    digest.update(value.encode("utf-8"))
                    self.signatures[index] = (digest, len(value))
                else:
                    self._fields({"role": "assistant", "content": [block]}, block_index=index)
                    if block.get("type") == "text" and self._reserve(ECHO_ENTRY_BYTES):
                        citations = block.get("citations", [])
                        self.citations[index] = len(citations) if isinstance(citations, list) else 0
            elif kind == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "signature_delta":
                    found = self.signatures.get(index)
                    value = delta.get("signature")
                    if found is None or not isinstance(value, str) or found[1] + len(value) > ECHO_VALUE_CHARS:
                        self.close()
                        return
                    digest, size = found
                    for start in range(0, len(value), 4096):
                        digest.update(value[start : start + 4096].encode("utf-8"))
                    self.signatures[index] = digest, size + len(value)
                elif isinstance(delta, dict) and delta.get("type") == "citations_delta":
                    citation = delta.get("citation")
                    if index not in self.citations or not isinstance(citation, dict):
                        self.close()
                        return
                    ci = self.citations[index]
                    body = {"role": "assistant", "content": [{"type": "text", "citations": [citation]}]}
                    for field in opaque_fields(body, protocol, answer=True):
                        at = ("model", "content", index, "text", "citations", ci, citation["type"], "encrypted_index")
                        field = OpaqueField(field.holder, field.key, at, field.model, field.fallback)
                        if self.check.valid(field):
                            self._member(self.check.member(field))
                    if not self.closed:
                        self.citations[index] += 1
            elif kind == "content_block_stop":
                if index not in self.blocks or self.blocks[index]:
                    self.close()
                    return
                self.blocks[index] = True
                self._finished_block(index)
        elif protocol == "responses":
            if kind in {"response.failed", "response.incomplete", "error"}:
                self.close()
            elif kind in {"response.output_item.done", "response.output_item.added"}:
                # Added items can still be partial; only done items are observations.
                if kind == "response.output_item.done":
                    self._fields({"output": [event.get("item")]})
            elif kind in {"response.completed", "response.done"}:
                response = event.get("response")
                self._body(response if isinstance(response, dict) else {})
        elif protocol == "gemini":
            self._fields(event)
            candidates = event.get("candidates")
            if isinstance(candidates, list) and any(isinstance(c, dict) and c.get("finishReason") for c in candidates):
                self.ready = True

    def _finished_block(self, index: int) -> None:
        if index in self.signatures:
            digest, _ = self.signatures.pop(index)
            # Transfer the assembly reservation to its finished digest; do not reserve twice.
            member = digest.digest()
            if member not in self.pending:
                self.pending[member] = None
                return
            self.backend.release(ECHO_ENTRY_BYTES)
            self.charge -= ECHO_ENTRY_BYTES
        elif index in self.citations:
            del self.citations[index]
            self.backend.release(ECHO_ENTRY_BYTES)
            self.charge -= ECHO_ENTRY_BYTES

    def commit(self) -> bool:
        """Called on the engine worker only; never enqueue/wait on that worker from inside this method."""
        with self._lock:
            if self.closed or self._working or not self.ready:
                return False
            self.running = True
            members = tuple(self.pending)
        try:
            return self.backend.keep(self.conversation, members) if members else True
        except Exception:
            return False
        finally:
            with self._lock:
                self.running = False
                self._discard()

    def _discard(self) -> None:
        self.closed = True
        self.pending = {}
        self.signatures = {}
        self.citations = {}
        self.blocks = {}
        charge, self.charge = self.charge, 0
        self.backend.release(charge)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            if not self.running and not self._working:
                self._discard()


MAX_ACCEPTED_BYTES = 32 * 1024 * 1024
MAX_ACCEPTED_BATCHES = 256
_BATCH_BYTES = 128
_PAIR_BYTES = 128
_NAME_BYTES = 64


@dataclass(slots=True)
class _Records:
    remembered: list[tuple[str, str]]
    named: list[str]
    remember: Callable[[str, str], None]
    name: Callable[[str], None]
    _size: int | None = None

    def __post_init__(self) -> None:
        if self._size is None:
            self._size = (
                _BATCH_BYTES
                + sum(_utf8_size(masked) + _utf8_size(restored) + _PAIR_BYTES for masked, restored in self.remembered)
                + sum(_utf8_size(response_id) + _NAME_BYTES for response_id in self.named)
            )

    @property
    def byte_size(self) -> int:
        assert self._size is not None
        return self._size

    def discard(self) -> None:
        self.remembered.clear()
        self.named.clear()


def _record(records: _Records) -> None:
    try:
        for masked, restored in records.remembered:
            records.remember(masked, restored)
        for response_id in records.named:
            records.name(response_id)
    finally:
        records.discard()


class _AcceptedProvenance:
    """One mediator's accepted records, including running work. Only its existing engine executor does store I/O.

    Production supplies Mediator's ThreadPoolExecutor; an executor that runs submitted callbacks inline is unsupported.
    """

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.closed = False
        self.held_bytes = 0
        self.pending_batches = 0
        self.pending: dict[Future[None], tuple[_Records, int]] = {}
        # Only bounded metadata and submit/rollback are protected. No payload, callback, Future operation or wait.
        self._metadata = Lock()
        self._reported = False

    def submit(self, records: _Records) -> Future[None] | None:
        if not records.remembered and not records.named:
            return None
        charge = records.byte_size
        future = None
        with self._metadata:
            if (
                not self.closed
                and self.held_bytes + charge <= MAX_ACCEPTED_BYTES
                and self.pending_batches < MAX_ACCEPTED_BATCHES
            ):
                self.held_bytes += charge
                self.pending_batches += 1
                try:
                    future = self.executor.submit(_record, records)
                except Exception:
                    self.held_bytes -= charge
                    self.pending_batches -= 1
                else:
                    self.pending[future] = (records, charge)
        if future is None:
            records.discard()
            raise ResponseError("the provider's output could not be recorded within Latet's limits")
        # Registration is outside the mutex: a Future already finished/cancelled invokes this inline.
        future.add_done_callback(self._finished)
        return future

    def cancel(self, future: Future[None]) -> bool:
        if not future.cancel():
            return False  # a running worker owns its records through its own finally
        self._finished(future)  # cleanup also works if the completion observer is not registered yet
        return True

    def _finished(self, future: Future[None]) -> None:
        with self._metadata:
            entry = self.pending.pop(future, None)
        if entry is None:
            return
        records, charge = entry
        error = None
        try:
            if future.cancelled():
                records.discard()  # confirmed pre-start cancellation; the executor may retain its work item
            else:
                error = future.exception()  # the worker's finally already cleared the payload
        finally:
            # Keep the reservation until payload ownership has ended, even during concurrent cancellation/close.
            with self._metadata:
                self.held_bytes -= charge
                self.pending_batches -= 1
        if error is not None:
            with self._metadata:
                report = not self._reported
                self._reported = True
            if report:
                log.warning("accepted model-output recording failed: %s", type(error).__name__)

    def close(self) -> None:
        with self._metadata:
            self.closed = True
            pending = tuple(self.pending)
        for future in pending:
            self.cancel(future)
