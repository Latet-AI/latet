"""Between the forwarder and the engine: decide pass-through / verify / rewrite for each request,
mask request bodies, and build the response transform that restores originals."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import copy
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import InitVar, dataclass, field, replace
from functools import partial
from itertools import chain
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from latet.engine import document, wellformed
from latet.engine.detectors.fields import shown_rule
from latet.engine.masker import ExtraValue, Masker, MaskReport
from latet.engine.modules import origin
from latet.engine.pipeline import (
    LRU,
    Engine,
    RequestScope,
    Scope,
    Session,
    SlotResult,
    dump_notice,
    personal_values,
    request_scope,
    shows_known,
    unread_notice,
)
from latet.engine.response_guard import safe_text
from latet.engine.review import ReviewPolicy, block_reason
from latet.engine.reviewer import Answer, Finding, Picture, Reviewer, View, readable, view_of
from latet.engine.reviewer.reviewer import CATEGORY_TYPES
from latet.engine.types import EntityClass, Policy, Replacement, SlotKind, Span
from latet.engine.vault import MaskingError
from latet.engine.verifier import Leak, Verifier
from latet.gateway import body_selectors, outbound
from latet.gateway.approvals import Approvals, Batch, Decision
from latet.gateway.body_coverage import (
    M3,
    PROTECTED_LAUNCH,
    BodyFreeze,
    BodyInspection,
    BodyProof,
    BodyRefused,
    ContentEpoch,
    Ledger,
    declared_body_safe,
    declared_string_safe,
    freeze_body,
    inspect_body,
)
from latet.gateway.cloud import model_of, shape_of
from latet.gateway.coverage import EnvelopeRefused
from latet.gateway.frozen import FrozenText, freeze_text
from latet.gateway.image_coverage import NO_OCR, UnreadImage, inspect_image
from latet.gateway.launch_authority import RequestLaunch
from latet.gateway.lease import ExchangeLease
from latet.gateway.provenance import (
    _BATCH_BYTES,
    _NAME_BYTES,
    _PAIR_BYTES,
    ECHO_ENTRY_BYTES,
    ECHO_RULE,
    EchoCheck,
    EchoScope,
    LocalEchoes,
    OpaqueCapture,
    _AcceptedProvenance,
    _Records,
)
from latet.gateway.recorder import (
    EncodedImage,
    _Block,
    _Image,
    build_record_blocks,
    build_record_images,
    reconstruct,
    record_positions,
)
from latet.gateway.request_profiles import opaque_scope
from latet.gateway.routes import Upstream
from latet.gateway.written import Written
from latet.protocols import anthropic, codec, gemini, openai_chat, openai_responses, sse
from latet.protocols.base import (
    BodyScan,
    ImageRef,
    JsonRef,
    SlotRef,
    definitions,
    dumps_compact,
    map_json_values,
    objects,
    output_format,
    rename,
)
from latet.protocols.rewrite import (
    AnthropicStream,
    ChatStream,
    GeminiStream,
    ResponsesStream,
    RestoreContext,
    _json_bound,
    _json_size,
    _utf8_size,
    restore_anthropic_message,
    restore_chat_completion,
    restore_gemini_answer,
    restore_response_body,
)
from latet.protocols.sse import ResponseError, SSEEvent, SSEParser, StreamDecoder, serialize, too_large
from latet.state import Left, LocalGiven
from latet.state.protocols import Echoes
from latet.vision.masking import NOT_AVAILABLE, UNREAD, WITHHELD, ImageMasking, ImageWork
from latet.vision.policy import ALLOW, refusal

if TYPE_CHECKING:
    from latet.config import LiveReview, LlmReview
    from latet.gateway.request_policy import ProfileGrants
    from latet.state import Given

log = logging.getLogger(__name__)

PASSTHROUGH, VERIFY, REWRITE = "passthrough", "verify", "rewrite"
_NO_RECORD_POSITIONS: Mapping[tuple[int, Any], tuple[int, ...]] = MappingProxyType({})
# What the local reviewer reads besides the kinds it reads inline: what the user and the tools bring.
_REVIEWED = frozenset({SlotKind.USER_TEXT, SlotKind.ATTACHMENT, SlotKind.TOOL_RESULT})
# How many texts approved in the Live Reviewer one conversation keeps (a key of 64 characters each): past that,
# another one is forgotten, and a text forgotten only waits for you again.
APPROVED_KEPT = 2_000
# What one entry of the memory of texts found fit to leave as they were kept takes, at most (a key of 32 bytes, a vault
# version, the memory's own links: measured between 170 and 200 bytes).
_CHECK_BYTES = 320
# The time the local name model has for the strings and keys of one JSON document (``Mediator._read_ahead``), checked
# before each of its windows (a second or so on a desktop CPU): with what else masking takes, within the 10 seconds
# Claude Code gives a plugin's hook. A document it could not read in that time is withheld.
DOCUMENT_READ_SECONDS = 5.0
# Texts of what the model wrote recorded in one turn of the event loop, at most (``Mediator.read_written``): a few
# milliseconds, a server's sealing included.
WRITTEN_AT_ONCE = 1000


def protocol_for(upstream: Upstream, method: str, path: str) -> str | None:
    p = path.split("?", 1)[0].rstrip("/")
    called = gemini.method_of(p)
    if method == "PATCH" and called == "cachedContents" and not p.endswith("/cachedContents"):
        return "gemini"  # an explicit cache's update: read as its creation is, whatever it carries
    if method != "POST":
        return None
    if shape_of(method, path) is not None:  # Bedrock's or Vertex's call: Anthropic's body, no model in it
        return "anthropic"
    if p.endswith("/v1/messages") or p.endswith("/v1/messages/count_tokens"):
        return "anthropic"
    if p.endswith("/chat/completions") or p.endswith("/v1/completions"):
        return "chat"
    if p.endswith("/responses"):
        return "responses"
    if p.endswith("/embeddings"):
        return "embeddings"
    if called in gemini.METHODS or (called == "cachedContents" and p.endswith("/cachedContents")):
        return "gemini"
    return None


def _remove_declared(inspection: BodyInspection) -> tuple[str, ...]:
    """Take the declared removable members out of the document before it is masked, encoded and checked again: the
    final check reads the document without them, and the ledger records their occurrences as removed."""
    names = []
    for holder, key in inspection.removed:
        if holder.pop(key, None) is not None:
            names.append(key)
    return tuple(names)


def _scan(protocol: str, body: dict[str, Any]) -> BodyScan:
    if protocol == "anthropic":
        return anthropic.scan_request(body)
    if protocol == "chat":
        return openai_chat.scan_request(body)
    if protocol == "responses":
        return openai_responses.scan_request(body)
    if protocol == "gemini":
        return gemini.scan_request(body)
    scan = BodyScan()
    if protocol == "embeddings":
        value = body.get("input")
        if isinstance(value, str):
            scan.slots.append(SlotRef(body, "input", SlotKind.USER_TEXT))
        elif isinstance(value, list):
            scan.slots.extend(SlotRef(value, i, SlotKind.USER_TEXT) for i, v in enumerate(value) if isinstance(v, str))
    return scan


_CWD_TAG = re.compile(r"<cwd>\s*([^<>\r\n]{1,4096}?)\s*</cwd>")
_WORKING_DIRECTORY = re.compile(
    r"(?m)^[ \t>*-]*(?:Primary working directory|Working directory):[ \t]*(\S[^\r\n]{0,4095}?)[ \t]*$"
)
_GEMINI_CLI_DIRECTORY = re.compile(r"(?m)I'm currently working in the directory:[ \t]*(\S[^\r\n]{0,4095}?)[ \t]*$")


def cwd_from(headers: dict[str, str], scan: BodyScan) -> str | None:
    """The working directory a client reports: a header of its own (``x-latet-project``), Codex's
    ``<cwd>`` in its environment context, "Working directory:" in OpenCode's system prompt, or "I'm currently working
    in the directory:" in Gemini CLI's first message."""
    given = headers.get("x-latet-project", "").strip()
    if given:
        return given
    for slot in scan.slots[:12]:
        if slot.kind not in (SlotKind.SYSTEM, SlotKind.USER_TEXT, SlotKind.OTHER):
            continue
        text = slot.get()
        if not isinstance(text, str) or (
            "cwd>" not in text and "orking directory:" not in text and "working in the directory:" not in text
        ):
            continue
        found = _CWD_TAG.search(text) or _WORKING_DIRECTORY.search(text) or _GEMINI_CLI_DIRECTORY.search(text)
        if found:
            return found.group(1).strip()
    return None


def verify_only(protocol: str | None, shape: str | None, headers: dict[str, str], body: Any) -> bool:
    """Whether Latet only checks this request, never rewrites it: Claude Code's, a subscription's (``sk-ant-oat``), and
    Bedrock's and Vertex's calls (``shape``), whose answers come in formats Latet does not restore (Bedrock's
    event-stream). Asked once for a request: it decides both how the request is prepared and whether a live launch key
    in its texts is screened before masking (P2-6a's re-review, R-4)."""
    return protocol == "anthropic" and (
        shape is not None or anthropic.is_claude_code(headers, body) or "sk-ant-oat" in headers.get("authorization", "")
    )


def _bodyless_checked_only(family: str | None, headers: dict[str, str]) -> bool:
    """Whether a call with no body (a model list) is one Latet only checks: ``verify_only`` asked of its headers alone,
    its profile's family as its protocol, the same test as a call with a body is decided by."""
    return verify_only(family, None, headers, None)


def _open_refused(
    envelope_open: Sequence[tuple[str, EnvelopeRefused]], protocol: str | None, shape: str | None
) -> Prepared:
    """D4 (b), option B: what the open list let through at the envelope (``request_profiles.Admission.open``) passes on
    a request Latet only checks alone. Any other is refused, before anything of it is masked or learnt, as its admission
    refused it before the open list: the first of them, by name to its agent alone (``open_refusal``), by category in
    its decision, its log line and its history."""
    refusal = envelope_open[0][1]
    reason = f"request envelope refused ({refusal.code})"
    prepared = Prepared(VERIFY, protocol, b"", shape=shape, block_reason=reason, envelope_refusal=refusal.code)
    prepared.open_refusal = refusal
    return prepared


def _json_document(value: str) -> bool:
    stripped = value.lstrip()
    if stripped[:1] not in ("{", "["):
        return False
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


# The JSON agents wrap a tool's own text in (Codex: {"output": ..., "metadata": {...}}; MCP:
# {"content": [{"type": "text", "text": ...}]}): a text there is the tool's output, read for
# structures, not the value of a field; the wrapper's own values are not data.
_WRAPPER = frozenset({"output", "stdout", "stderr", "content[].text", "content[].type", "isError"})


def _field(path: str) -> str | None:
    """The field name a JSON path gives a value: ``items[].sku``; ``[].id`` for a list is ``id``."""
    path = path.removeprefix("[].")
    return None if not path or path in _WRAPPER or path.startswith("metadata.") else path


@dataclass(slots=True)
class Masked:
    """One text masked for a request: what is sent instead, and what was replaced in it."""

    text: str
    replacements: list[Replacement]
    exact: bool = True  # the offsets fit the text (False: they are inside decoded JSON values)
    fresh: bool = False  # masked now, not found in the cache
    withheld: bool = False  # the dump guard replaced it: too many personal values in one output
    original: str = ""  # the text as the client sent it
    history_allowed: bool = False


@dataclass(slots=True)
class Call:
    """The texts of one request that are new to their conversation, each with all it was masked into
    and with how to mask it again: one masked before a later text taught the vault a value in it.
    And those it sends as they were kept, by key: the conversation, how to mask the text anew, and
    each sight with the record it is sent as, asked again once the vault has changed."""

    version: int  # the vault's, before the first text was masked
    new: dict[bytes, tuple[Callable[[], Masked], list[Masked]]] = field(default_factory=dict)
    kept: dict[bytes, tuple[Session, Callable[[], Masked], list[tuple[Left, Masked]]]] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewItem:
    """A text for the local reviewer: inline, before its request leaves, or in the background."""

    index: int  # which slot of the request (or item of the plugin's call)
    original: str
    kind: SlotKind
    tool: str | None
    path: str | None
    json_mode: bool
    view: View  # what the reviewer reads: the text as sent, Latet's replacements as placeholders
    inline: bool
    sent: str  # the text as sent (after the review, when it masked more)
    answer: Answer | None = None  # reviewed before (resent after a restart): what it found then
    image: ImageWork | None = None  # the image the text was read in, when the reviewer sees images
    picture: Picture | None = None  # that image as the reviewer is shown it (not reviewed before)


@dataclass(slots=True)
class LiveItem:
    """A new text for the Live Reviewer: it waits for you before it leaves."""

    index: int  # which slot of the request (or item of the plugin's call)
    original: str
    kind: SlotKind
    tool: str | None
    path: str | None
    json_mode: bool
    sent: str  # as Latet and the local reviewer masked it
    answer: Answer | None = None  # what the local reviewer said of it
    extra: list[ExtraValue] = field(default_factory=list)  # what the reviewer masked: kept when masked again
    image: ImageWork | None = None  # the image this text was read in


@dataclass(slots=True)
class _HeldLive:
    """A batch opened here and waiting: what its previews are made of, and what they were made with."""

    session: Session
    items: list[LiveItem]
    keep: bool  # the plugin's texts keep their surrogates (``apply_live``)
    views: list[dict[str, Any]]  # the previews the dashboards show now
    made_at: tuple[int, int]  # the rules' version and the vault's, when they were made


# How long a refresh of the previews waits for the changes that come with the first (``Mediator.refresh_live``).
_REFRESH_AFTER = 0.25


def _refresh_ended(task: asyncio.Task[None]) -> None:
    """A refresh of the previews that failed, said by the type of its error alone (its message could quote a text)."""
    if not task.cancelled() and (error := task.exception()) is not None:
        log.warning(
            "the Live Reviewer could not make the previews of the requests waiting again (%s)", type(error).__name__
        )


@dataclass(slots=True)
class ReviewRecord:
    """What the reviewer said of one text, for the dashboard: never a value, only where it is."""

    session: str
    project: str | None
    kind: str
    inline: bool
    sent: str  # the block as it left
    answer: Answer
    masked: list[Finding]  # the findings masked (inline: in this block; background: from now on)


def _as_word(value: str, text: str) -> bool:
    """Whether ``value`` appears in ``text`` as a whole word."""
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text) is not None


def still_in_clear(found: Sequence[Finding], original: str, sent: str, masker: Masker) -> bool:
    """Whether a value the local reviewer found in ``original``, masked again, is in clear in what is ``sent``: as a
    whole word outside every fake in it (masked again, it is masked in a JSON document's keys too; what is found here
    is a value masking did not place). One whose type your policy lets through is left as it is, as the detectors'
    finds are."""
    fakes: list[Span] | None = None
    for finding in found:
        if masker.policies.get(finding.extra().etype) == Policy.ALLOW or not _as_word(finding.value, original):
            continue
        fakes = masker.surrogates_in(sent) if fakes is None else fakes
        for at in re.finditer(rf"(?<!\w){re.escape(finding.value)}(?!\w)", sent):
            if not any(f.start <= at.start() and at.end() <= f.end for f in fakes):
                return True
    return False


def _shown(replacements: Sequence[Replacement], path: str, sent: str) -> list[Replacement]:
    """``replacements`` made in a value at ``path``, each rule that names the path naming it as it is sent (``sent``: a
    key on it masked), so that what an exchange shows and keeps of the value holds no key that left masked."""
    out = []
    for r in replacements:
        rule = shown_rule(r.rule, path, sent)
        out.append(r if rule == r.rule else replace(r, rule=rule))
    return out


def _log_failure(future: asyncio.Future[Any]) -> None:
    """A background review whose findings could not be learned: logged by the error's type alone (its message and its
    traceback could quote a value the reviewer found)."""
    if not future.cancelled() and (error := future.exception()) is not None:
        log.error("a background review could not be applied: %s", type(error).__name__)


def _left_failure(future: asyncio.Future[Any]) -> None:
    """What a request carried could not be recorded as left (its store did not answer): said by the error's type alone.
    Its texts are masked anew when they are sent again, the safe side."""
    if not future.cancelled() and (error := future.exception()) is not None:
        log.warning("what a request carried could not be recorded as left: %s", type(error).__name__)


def _session_key(
    protocol: str, headers: dict[str, str], body: dict[str, Any], named: Callable[[str], str | None]
) -> str:
    if headers.get("x-claude-code-session-id"):
        return f"cc:{headers['x-claude-code-session-id']}"  # the plugin bridge's key for the same session
    for name in ("session-id", "session_id", "thread-id", "x-session-id"):
        if headers.get(name):
            return f"{name}:{headers[name]}"
    if protocol == "responses":
        previous = body.get("previous_response_id")
        if isinstance(previous, str) and (known := named(previous)):
            return known
        if isinstance(body.get("prompt_cache_key"), str):
            return f"pck:{body['prompt_cache_key']}"
    # Stateless clients resend the whole history: its opening (system prompt and first user message)
    # stays the same on every turn, so it names the conversation.
    if protocol == "gemini":  # its system instruction and contents, under whichever names they come
        system, history = gemini.opening(body)
        head = _opening(history) if isinstance(history, list) else history
        opening = dumps_compact([system, None, head or ""])[:8192]
        return f"conv:{hashlib.sha256(opening.encode()).hexdigest()[:24]}"
    history = body.get("messages") if isinstance(body.get("messages"), list) else body.get("input")
    head = _opening(history) if isinstance(history, list) else history
    opening = dumps_compact([body.get("system"), body.get("instructions"), head or ""])[:8192]
    return f"conv:{hashlib.sha256(opening.encode()).hexdigest()[:24]}"


def _opening(items: list[Any]) -> list[Any]:
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get("role") == "user":
            return items[: i + 1]
    return items[:1]


@dataclass(slots=True)
class Prepared:
    mode: str
    protocol: str | None
    body: bytes
    session: Session | None = None
    leaks: list[Leak] = field(default_factory=list)
    uninspected: list[str] = field(default_factory=list)
    masked_count: int = 0
    degraded: list[str] = field(default_factory=list)
    block_reason: str | None = None
    stream: bool = False
    model: str | None = None
    # For the dashboard: the parsed body and its slots, each with the replacements made in it and
    # whether their offsets are exact (False: found again in the masked text when recording).
    document: Any = None
    pending: list[tuple[SlotRef, list[Replacement], bool]] = field(default_factory=list)
    review: bool = False  # Claude Code's auto mode reviewer
    off: bool = False  # Latet is off in its project: refused (D5), never sent
    # The project whose rules it was decided by and how it came: ``trusted`` (a binding Latet made) or ``claimed`` (what
    # the request said, whose rules only add protection; it binds no conversation). For the dashboard (D7).
    project: str | None = None
    binding: str | None = None
    # The folders of its route's project exceptions that name the project Latet trusts it to be in
    # (``request_policy.trusted_folders``), compared as it was prepared, in its worker, never on the event loop.
    excepted: frozenset[str] = frozenset()
    # The body selectors of the exceptions its project allows (``request_policy.ProfileGrants.chosen``), read again by
    # its final check (``_check``) as by its first: what an undeclared member of its body may be admitted by.
    body_grants: Any = field(default=None, repr=False)
    withheld: int = 0  # tool outputs the dump guard withheld, the first time they were sent
    history: bool = False  # Claude Code: the values in clear are in history Latet never masked
    reviews: list[ReviewItem] = field(default_factory=list)  # texts for the local reviewer
    masked: list[Masked] = field(default_factory=list)  # each slot as masked, with its original
    # Tools that ran outside this computer with stand-ins in their input (first seen in this request),
    # and those inputs, for the dashboard.
    outbound: list[str] = field(default_factory=list)
    outbound_slots: list[SlotRef] = field(default_factory=list)
    encoding: str | None = None  # the body's content-encoding, to encode it again after a review
    shape: str | None = None  # a cloud's model call (Bedrock, Vertex): checked, never rewritten
    bypass: bool = False  # a Claude Code session whose requests do not go through Latet
    images: list[ImageWork] = field(default_factory=list)  # the images read: masked, clean or withheld
    # What its conversation's vault holds for its answer (``Engine.hold``, which ``_mask_slots`` asks once it has
    # masked, through the lease that owns the handle it returns): released once, by whoever ends the exchange, and only
    # for a request that held (RC-REC-11).
    lease: ExchangeLease | None = None
    # The local reviewer or you changed it after it was masked (a text masked again, an image withheld or covered): it
    # leaves written anew from ``document``, a WebSocket turn as a body is.
    amended: bool = False
    # Claude Code's: what its answer's model writes is read as the answer passes (``Mediator.read_written``).
    claude_code: bool = False
    # What it was when its request's decision was made (``outcome.finalize``, once, after its last change): set there
    # alone. Any change made to it afterwards (a reviewer's, a late writer's) means that decision sends nothing.
    sealed: object | None = None
    authority: Any = field(default=None, repr=False)
    route_authority: Any = field(default=None, repr=False)
    control_target: str | None = field(default=None, repr=False)
    control_current: Callable[[], bool] | None = field(default=None, repr=False)
    protected_unavailable: bool = False
    protected_current: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)
    # What its launch was decided as (``launch_authority.LAUNCH_CATEGORIES``), for the history and P2-7's display.
    launch: str | None = None
    # Immutable, checked-only envelope values. Never pending/body slots and never recorded as conversation text.
    envelope_checked: tuple[str, ...] = field(default=(), repr=False)
    body_checked: tuple[str, ...] = field(default=(), repr=False)
    body_semantic: tuple[str, ...] = field(default=(), repr=False)
    body_metadata: tuple[str, ...] = field(default=(), repr=False)
    body_rules: tuple[str, ...] = ()
    opaque_required: bool = field(default=False, init=False, repr=False)
    body_authority: tuple[str, ...] = ()
    body_input: bytes | str = field(default=b"", repr=False)
    body_ledger: Ledger | None = field(default=None, repr=False)
    body_upstream: Upstream | None = field(default=None, repr=False)
    # A Gemini cache's creation, whose model the GenAI SDK names as models/{id} (P2-8a, root's ruling 4).
    body_cache_create: bool = field(default=False, repr=False)
    opaque_check: EchoCheck | None = field(default=None, repr=False)
    body_scan: BodyScan | None = field(default=None, repr=False)
    body_structure: BodyFreeze | None = field(default=None, repr=False)
    # The shape its ledger last accounted (``inspect_body``'s ``accounted``): the final check accounts again only a body
    # whose shape changed since. Worker-owned, as the ledger is.
    body_shape: BodyFreeze | None = field(default=None, repr=False, compare=False)
    body_epoch: ContentEpoch | None = field(default=None, repr=False)
    body_verified_token: object | None = field(default=None, repr=False)
    body_verified: bool = False
    body_verified_payload: bytes | None = field(default=None, repr=False)
    frame_verified_payload: str | None = field(default=None, repr=False)
    body_proof: BodyProof | None = field(default=None, init=False, repr=False)
    envelope_refusal: str | None = None  # explicit safe category, independent of agent-facing wording
    # D4 (b)'s open list (``request_profiles.Admission.open``): the headers it let through at the envelope that this
    # request was prepared with, by name; and, on a request Latet rewrites, the refusal the first of them had without
    # it, which its forwarder answers (its name to the agent alone), nothing of the request masked or learnt.
    envelope_open: tuple[str, ...] = field(default=(), repr=False)
    open_refusal: EnvelopeRefused | None = field(default=None, repr=False)
    # Declared removable body members taken out before the send (Codex's client_metadata), by name.
    body_removed: tuple[str, ...] = ()
    # A worker construction index for selected occurrence ordinals. Discarded once immutable blocks/images own
    # their selected ordinals; the finishing event loop shares those tuples directly.
    record_positions: Mapping[tuple[int, Any], tuple[int, ...]] = field(init=False, repr=False, compare=False)
    # An edited refusal's logical JSON, only when the unsent original bytes no longer describe its arena.
    record_body: bytes | str | None = field(default=None, init=False, repr=False, compare=False)
    record_error: str | None = field(default=None, init=False, repr=False, compare=False)

    # Worker-owned representations. Only JsonRef subtrees retain container checks, private to this arena;
    # decisions and snapshots share their immutable strings alone.
    freeze_generation: object = field(default_factory=object, init=False, repr=False, compare=False)
    frozen_slots: tuple[SlotRef, ...] = field(default=(), init=False, repr=False, compare=False)
    frozen_texts: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)
    frozen_outbound: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)
    frozen_json: dict[int, FrozenText] = field(default_factory=dict, init=False, repr=False, compare=False)
    frozen_image_data: tuple[tuple[object, str, str | EncodedImage | None], ...] = field(
        default=(), init=False, repr=False, compare=False
    )
    record_blocks: tuple[_Block, ...] = field(default=(), init=False, repr=False, compare=False)
    record_images: tuple[_Image, ...] = field(default=(), init=False, repr=False, compare=False)
    # A WS frame's exact final representation, set/refreshed in the worker. None for an HTTP arena.
    frame_payload: str | None = field(default=None, init=False, repr=False, compare=False)
    # Whether a request exception may apply to its body at all: never a WebSocket frame's (F-2), which an unknown
    # member's refusal then says (``BodyRefused``, P2-7 review I-8).
    body_excepted: bool = field(default=True, init=False, repr=False, compare=False)
    frame_body: bytes | None = field(default=None, init=False, repr=False, compare=False)
    frame_count: int | None = field(default=None, init=False, repr=False, compare=False)
    frame_compact: bool = field(default=False, init=False, repr=False, compare=False)
    # Built by a worker that refreshes its recording itself once its final check is done, on every way out
    # (``_mark_outbound``, ``mask_ws_frame``, ``discard_unadmitted_content``): not refreshed as it is built too (P2-8).
    recorded_later: InitVar[bool] = False

    def discard_unadmitted_content(self, reason: str, code: str | None = None) -> None:
        """Keep the exchange's categorical refusal and its release owner."""
        self.block_reason = reason
        self.envelope_refusal = code
        self.body = self.body_input = b""
        self.document = None
        self.model = None
        self.pending = []
        self.masked = []
        self.reviews = []
        self.outbound = []
        self.outbound_slots = []
        self.images = []
        self.body_scan = self.body_ledger = self.body_structure = self.body_shape = None
        self.body_checked = self.body_semantic = self.body_metadata = ()
        self.body_rules = self.body_authority = ()
        self.leaks = []
        self.close_receipt()
        self.opaque_check = None
        self.body_proof = None
        self.body_verified = False
        self.body_verified_payload = None
        self.frame_payload = self.frame_verified_payload = None
        self.frame_body = None
        self.refresh_recording()

    def __post_init__(self, recorded_later: bool) -> None:
        if not recorded_later:
            self.refresh_recording(logical=bool(self.block_reason and any(w.state == WITHHELD for w in self.images)))

    def refresh_recording(self, *, logical: bool = False, serialized: bytes | None = None) -> None:
        """Refresh selected occurrence identities after a worker changes the document or its references. Production
        document constructors and all calls here run in workers; recording an exchange never walks its arena.
        Failure loses the record alone, latched by its type without retaining an exception or its traceback."""
        self.freeze_inputs()  # transport correctness is independent of recording allocation/failure
        if self.record_error is not None:
            return
        try:
            self._refresh_recording(logical=logical, serialized=serialized)
        except Exception as exc:
            self.record_positions = _NO_RECORD_POSITIONS
            self.record_body = None
            self.record_blocks = self.record_images = ()
            self.frozen_image_data = ()
            self.record_error = type(exc).__name__

    def _refresh_recording(self, *, logical: bool, serialized: bytes | None) -> None:
        needed = set()
        for slot in chain((slot for slot, _, _ in self.pending), self.outbound_slots):
            needed.add((id(slot.container), slot.key))
            given = slot.source_input if slot.kind == SlotKind.TOOL_RESULT else None
            if given is not None and isinstance(given[0], dict):
                needed.add((id(given[0]), given[1]))
        needed.update((id(work.ref.holder), work.ref.key) for work in self.images)
        self.record_positions = MappingProxyType(record_positions(self.document, needed))
        self.record_body = None
        if logical and self.document is not None:
            self.record_body = serialized if serialized is not None else dumps_compact(self.document)
        self._freeze_images()  # image recording allocation failures drop only the record
        self.record_blocks = build_record_blocks(self)
        self.record_images = build_record_images(self)
        # The immutable blocks/images now own the selected ordinal tuples; their construction map is redundant.
        self.record_positions = _NO_RECORD_POSITIONS

    def freeze_inputs(self) -> None:
        """Called only in preparation workers, after every edit, including exceptional reviewer exits."""
        if self.body_verified and (
            self.body is not self.body_verified_payload or self.frame_payload is not self.frame_verified_payload
        ):
            self.body_verified = False
            self.block_reason = (
                self.block_reason or "request bytes changed after the final check; send the request again"
            )
        previous = self.frozen_json
        frozen: dict[int, FrozenText] = {}

        def text_of(slot: SlotRef) -> str:
            if isinstance(slot, JsonRef):
                held = frozen.get(id(slot)) or freeze_text(slot, previous.get(id(slot)))
                frozen[id(slot)] = held
                return held.text
            value = slot.get()
            return value if isinstance(value, str) else ""

        self.frozen_slots = tuple(slot for slot, _, _ in self.pending)
        self.frozen_texts = tuple(text_of(slot) for slot in self.frozen_slots)
        self.frozen_outbound = tuple(text_of(slot) for slot in self.outbound_slots)
        self.frozen_json = frozen
        if self.frame_payload is not None and not self.block_reason:
            if self.amended and self.body is not self.frame_body:
                # Reviews already wrote the final body here. Decode only a newly written body, off the event loop.
                self.frame_payload = self.body.decode()
                self.frame_body, self.frame_compact = self.body, True
            elif self.frame_count != self.masked_count and not self.frame_compact:
                # The baseline relay rewrites on a count-only review too, even with no changed slot text.
                self.frame_payload = dumps_compact(self.document)
                self.frame_compact = True
            self.frame_count = self.masked_count
            self.frame_verified_payload = self.frame_payload
        self.freeze_generation = object()  # any later worker refresh invalidates an already minted seal
        self.body_proof = None
        self.opaque_required = ECHO_RULE in self.body_rules
        if self.body_structure is not None and not self.body_structure.unchanged():
            self.body_verified = False
            self.block_reason = (
                self.block_reason or "request body changed after the final check; send the request again"
            )
        if (
            self.body_epoch is not None
            and self.body_verified_token is not None
            and self.body_verified_token is not self.body_epoch.token
        ):
            self.body_verified = False
            self.block_reason = (
                self.block_reason or "request values changed after the final check; send the request again"
            )
        # The body revision its protocol is read by (``body_selectors.revision``): a protocol with none mints no proof.
        read_by = self.protocol
        known = read_by is not None and read_by in body_selectors.HISTORY
        profile = body_selectors.revision(read_by) if known and read_by is not None else None
        if self.body_verified and not self.block_reason and self.body_ledger is not None and profile is not None:
            self.body_proof = BodyProof(
                self.body_input,
                self.frame_payload if self.frame_payload is not None else self.body,
                self.freeze_generation,
                len(self.body_ledger.raw),
                len(self.body_ledger.active),
                self.body_ledger.retained,
                self.body_rules,
                self.body_checked,
                profile,
                self.body_verified_token,
                self.body_ledger.receipts,
                tuple(self.body_ledger.final[path][0] for path in self.body_ledger.active),
                self.body_authority,
            )

    def _freeze_images(self) -> None:
        """Freeze recording-only image carriers without making an extra canonical payload before review."""
        images = []
        for n, work in enumerate(self.images):
            raw = work.ref.holder.get(work.ref.key)
            old = self.frozen_image_data[n] if n < len(self.frozen_image_data) else None
            if work.state == WITHHELD:
                data = None
            elif (canonical := work.current_original) is not None:
                data = canonical
            elif work.ref.form == "base64":
                data = raw
            elif old is not None and old[0] is raw and old[1] == work.ref.form and old[2] is not None:
                data = old[2]
            else:
                if not isinstance(raw, str):
                    raise TypeError("an image carrier is not text")
                data = EncodedImage(raw, work.ref.form)
            images.append((raw, work.ref.form, data))
        self.frozen_image_data = tuple(images)

    def opaque_current(self) -> bool:
        """Constant-time receipt gate after any await, before the sealed request/frame is authorized."""
        check = self.opaque_check
        return not self.opaque_required or (
            check is not None and bool(check.found) and time.monotonic() < check.expires
        )

    def inputs_unchanged(self) -> bool:
        if self.body_structure is not None and not self.body_structure.unchanged():
            return False
        return self.slots_unchanged()

    def slots_unchanged(self) -> bool:
        """Its selected texts as frozen: the very slots, each holding the text frozen (a JSON document's nested objects
        too, never serialized again); its body's whole shape is not walked here (``freeze_inputs`` walks it, in the
        worker). What the event loop checks before a decision is sent (``outcome.sendable``), in the slots' count."""
        if len(self.pending) != len(self.frozen_slots):
            return False
        try:
            for (slot, _, _), held, text in zip(self.pending, self.frozen_slots, self.frozen_texts, strict=True):
                if slot is not held:
                    return False
                json_text = self.frozen_json.get(id(slot))
                if json_text is not None:
                    if not json_text.unchanged(slot):
                        return False
                elif slot.get() is not text:
                    return False
        except Exception:
            return False
        return True

    @property
    def held(self) -> bool:
        """Whether its conversation's vault held for its answer (a hold taken, released since or not)."""
        return self.lease is not None and self.lease.taken

    @property
    def hold(self) -> object | None:
        """The handle its lease holds (what the vault counted its hold in), None without one."""
        return self.lease.handle if self.lease is not None else None

    @property
    def released(self) -> bool:
        return self.lease is not None and self.lease.released

    def close_receipt(self) -> None:
        """Release the input evidence after its final send/refusal; capture keeps only the scope/hash interface."""
        if self.opaque_check is not None:
            self.opaque_check.close()


def _release_given_up(future: Future[Any]) -> None:
    """``Mediator.run_held``'s step ended after its request was given up: the lease of what it returned released, once
    (in the thread it ended in, or at once when it had ended already)."""
    if future.cancelled() or future.exception() is not None:
        return
    found = future.result()
    for item in found if isinstance(found, tuple) else (found,):
        if isinstance(item, Prepared):
            item.close_receipt()
            if item.lease is not None:
                item.lease.release()


def _counted(body: dict[str, Any], ledger: Ledger | None = None, *, projection: bool = False) -> dict[str, Any] | None:
    """The request a Bedrock token count carries, in base64 (``input.invokeModel.body``); None when
    it holds anything else, beside it too (a ``converse`` request Latet would not read), or gives a key
    twice in an object (it leaves as it came, the copy Latet did not read in it)."""
    try:
        wrapper = body["input"]["invokeModel"]
        if set(body) != {"input"} or set(body["input"]) != {"invokeModel"} or set(wrapper) != {"body"}:
            return None
        decoded = base64.b64decode(wrapper["body"], validate=True)
        if ledger is not None:
            if projection:
                ledger.projected_embedded(decoded)
            else:
                ledger.embedded(decoded)
        inner = wellformed.read(decoded)
    except BodyRefused:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    return inner.doc if isinstance(inner.doc, dict) and not inner.repeated and not inner.mended else None


# D5: a request of a project where Latet is off is refused (0.11 sends no request unread), whether its project came from
# a binding Latet made (trusted) or from what the request says (a claim, R-83), with how to go on without Latet there.
_OFF_ADVICE = (
    "Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the "
    "project's switch in the dashboard), or reach the provider directly for this folder by starting the agent "
    "without Latet's address"
)
OFF_THERE = f"Latet is off in this project: {_OFF_ADVICE}"
CLAIMED_OFF = f"request claimed a project where Latet is off: {_OFF_ADVICE}"
# Bedrock and Vertex carry many models' requests: Latet reads Claude's (its messages), and sends no other.
_NOT_MESSAGES = (
    "Latet reads Claude's messages on Bedrock and Vertex, and this request is not one, so it does not send it"
)
# A request Latet only checks leaves as it came: one whose body gives a key twice would leave with the copy Latet did
# not read (it reads the last, as Python's json does). One it rewrites leaves written anew from what it read.
_MENDED = "Latet cannot send a verify-only request containing an incomplete character; repair the text and try again"
# What a request carries that Latet does not read, as a refusal made before its content is read keeps it: these fixed
# kinds alone (the dashboard's uninspected flags), never a type name the client wrote.
_UNREAD_KINDS = frozenset({"image", "audio", "video", "file", "document"})


def _unread_kinds(unread: list[str]) -> list[str]:
    return [kind if kind in _UNREAD_KINDS else "other" for kind in unread]


_TWICE = (
    "this request's body gives a key twice in one of its objects, and Latet reads one copy of it: as it came, the "
    "request would leave with the other unread, so Latet does not send it"
)
# A coded body leaves as Latet read it, never as the container it came in (G2 review I-1): a coding can carry bytes its
# decoder skips. One Latet rewrites leaves encoded again; one it only checks would leave as it came, so it is refused,
# under the code a content-encoding outside its profile's values has. No client Latet supports compresses those.
_CODED_CODE = "envelope-header-value"
_CODED = (
    f"request envelope refused ({_CODED_CODE}): header content-encoding: Latet checks this request and sends it as it "
    "came, never rewritten, so it does not send a compressed body; send the body uncompressed, without a "
    "content-encoding header"
)
# What Latet reads and nothing else: a body it does not read is refused, saying why, on a computer as on a server (one
# code path, so its words are true on both). A request with no body has nothing to read: a GET of the models list
# passes.
_READ_BODIES = "messages, chat completions, completions, responses, embeddings or Gemini's generateContent"
_NOT_AN_OBJECT = (
    "Latet reads a model's call as a JSON object, and this request's body is not one, so it does not send it"
)


def _defined(protocol: str | None, body: dict[str, Any], tools: BodyScan) -> None:
    """Select tool descriptions and schemas without imposing masking or off policy."""
    if protocol in ("anthropic", "chat", "responses"):
        definitions(body.get("tools"), tools)
    if protocol == "anthropic":
        anthropic.added_tools(body, tools)
    if protocol == "gemini":
        gemini.definitions(body, tools)
    if protocol == "chat":
        definitions(body.get("functions"), tools)
        given = body.get("response_format")
        if isinstance(given, dict) and given.get("type") == "json_schema":
            output_format(given.get("json_schema"), tools)
    if protocol == "responses" and isinstance(body.get("text"), dict):
        output_format(body["text"].get("format"), tools)


def _projected_request_safe(
    body: dict[str, Any], protocol: str, upstream: Upstream, ledger: Ledger, shape: str | None, grants: Any = None
) -> bool:
    if shape == "bedrock-count":
        decoded = _counted(body, ledger, projection=True)
        if decoded is None:
            raise BodyRefused("shadow wrapper")
        body = decoded
    if shape is not None and not isinstance(body.get("messages"), list):
        raise BodyRefused("shadow shape")
    try:
        scan = _scan(protocol, body)
        _defined(protocol, body, scan)
    except (TypeError, KeyError) as error:
        raise BodyRefused("shadow shape") from error
    if scan.unreadable:
        raise BodyRefused("shadow shape")
    return ledger.declared_scan_safe(scan, projection=True) and declared_body_safe(
        body, protocol, upstream, scan, ledger, projection=True, grants=grants
    )


def _unread_fields(fields: Sequence[str]) -> str:
    """Why Latet refuses a request that holds content in a form it does not read (``BodyScan.unreadable``), in words
    that name no content, on a computer as on a server."""
    what = ", ".join(sorted(set(fields)))
    return f"Latet reads the content of a model's call, and this request holds {what}, so it does not send it"


def _unread_body(method: str) -> str:
    """Why Latet refuses a request with a body it does not read, on a computer as on a server: its method, or its
    endpoint."""
    if method != "POST":
        return (
            f"Latet reads the body of a model's call, made by POST, and this is a {method} request with a body, so it "
            "does not send it"
        )
    return (
        f"Latet reads the body of a model's call ({_READ_BODIES}), and this request is to another endpoint, one it "
        "does not read yet (Google's Interactions API, the Batch API, file uploads and image generation among them), "
        "so it does not send it"
    )


# Why Latet refuses a client's WebSocket frame: what it is (never its content, which it did not read).
_UNREAD_FRAME = (
    "Latet reads the turns of a WebSocket (response.create frames), and this is {what}, so it does not send it"
)
# The frames a client may send that the protocol defines with no content, and the fields they hold: a server passes
# them as they came (cancelling a turn), and refuses one that holds anything else.
_CONTENTLESS = frozenset({"response.cancel"})
_CONTENTLESS_FIELDS = frozenset({"type", "event_id", "response_id"})


def _contentless(frame: Any, repeated: bool) -> bool:
    """Whether a client's WebSocket frame is one the protocol defines with no content (``_CONTENTLESS``), holding no
    field but its own (an id), each given once."""
    return (
        isinstance(frame, dict)
        and frame.get("type") in _CONTENTLESS
        and not repeated
        and frame.keys() <= _CONTENTLESS_FIELDS
        and all(isinstance(value, str) for value in frame.values())
    )


def _model(body: dict[str, Any]) -> str | None:
    model = body.get("model")
    return model if isinstance(model, str) else None


P = ParamSpec("P")
R = TypeVar("R")


class Mediator:
    def __init__(
        self,
        engine: Engine,
        verifier: Verifier,
        approvals: Approvals | None = None,
        given: Given | None = None,
        echoes: Echoes | None = None,
    ) -> None:
        self.engine = engine
        self.verifier = verifier
        self.echoes: Echoes = echoes if echoes is not None else LocalEchoes()
        self.images = ALLOW  # whether requests that carry images and documents are sent (the global rules)
        self.ocr = True  # the text in images is read (the global rules), when the models are installed
        self.vision: ImageMasking | None = None  # reads and paints images, once the models are installed
        # At most this many personal values in one tool output (the global setting; projects have theirs).
        self.dump_guard: int | None = None
        # The local reviewer, when it is on, and its global settings (projects may read more or less).
        self.reviewer: Reviewer | None = None
        self.llm_review: LlmReview | None = None
        self.on_review: Callable[[ReviewRecord], None] | None = None  # the dashboard's record of reviews
        self.retired: list[Reviewer] = []  # reviewers of earlier settings, closed on the event loop
        # The Live Reviewer's global settings (projects may turn it on or off) and its queue.
        self.live_review: LiveReview | None = None
        self.approvals = approvals if approvals is not None else Approvals()
        self.approvals.is_surrogate = engine.vault.is_surrogate
        self.approvals.is_allowed = lambda batch, value: self._masker(self.engine.session(batch.session)).allow.allows(
            value
        )
        # Each conversation's texts as they were masked for it: sent again there, a text leaves as it left before,
        # whatever the vault has learnt since, once it is known to have left so (``_leaves_as_it_left``). This
        # process's memory on a computer, Valkey on a Latet server (any instance serves a conversation's next request).
        self.given: Given = given if given is not None else LocalGiven()
        # Each text found fit to leave again as it was kept (by a keyed hash of its key and of that text), with the
        # vault's version then: checked again once the vault has changed.
        self._as_it_left: LRU[bytes, tuple[int, bool]] = LRU(50_000)
        self._outbound_seen: LRU[bytes, bool] = LRU(50_000)  # tool calls said already (history repeats them)
        self.loop: asyncio.AbstractEventLoop | None = None  # the daemon's, for what threads publish
        # The Live Reviewer's batches opened here and waiting (``hold_live``): their texts are here alone, so their
        # previews are made again here when the rules or the vault change before you decide (``live_changed``), and the
        # dashboards shown what will leave. One refresh at a time, and one more when they change meanwhile.
        self._held_live: dict[str, _HeldLive] = {}
        self._refreshing: asyncio.Task[None] | None = None
        self._refresh_again = False
        self._refresh_asked = False  # by a thread, and not taken by the loop yet
        self._content_epoch = ContentEpoch()
        engine.vault.on_change = self.live_changed
        # Claude Code's auto mode reviewer is sent the OS user name and paths, which no plugin can
        # mask: the details the user lets through to it (auto_mode_review), the rest blocks the review
        # (auto mode then asks the user instead).
        self.review = ReviewPolicy()
        # Masking and verification never run on the event loop, nor the restore of a large answer
        # (RESTORED_ON_LOOP): a long block (or the local name model) must not stall the other agents'
        # streams. The Claude Code plugin has a thread of its own, so its hooks (10 s budget) never
        # wait behind a large proxied request.
        self.executor = ThreadPoolExecutor(1, thread_name_prefix="latet-engine")
        self.bridge_executor = ThreadPoolExecutor(1, thread_name_prefix="latet-bridge")
        self.provenance = _AcceptedProvenance(self.executor)
        # On a Latet server, the bytes a request's body may decode to (its max_request_mb, which bounds it on the wire
        # too): a few KiB that would inflate past it stop the request (DecodedTooLarge), never fill the memory every
        # identity's space shares. None on a computer: decoded as it always was.
        self.body_limit: int = 32 << 20
        # An identity's space on a Latet server (its app sets it: ``create_app``). The server masks and never trusts a
        # computer: what a computer sends as it came because Latet does not read it, the server refuses, saying why (an
        # image it could not read on a request it only checks, a WebSocket frame or a body it does not read).
        # Payload-free saturating D4 count. P2-5/P2-7 project runtime status into the dashboard.
        self.envelope_removed_headers = 0
        # Payload-free saturating count of declared removable body members that left removed (P2-5/P2-7 show it).
        self.body_removed_members = 0
        # This identity's answers restored on the event loop, one step of one answer at a time (``RESTORING_AT_ONCE``):
        # however many streams it has, the others wait their turn, and the loop serves every other identity between
        # two of its steps.
        self.restoring = asyncio.Semaphore(RESTORING_AT_ONCE)

    @staticmethod
    def _request_work(fn: Callable[[], R]) -> Callable[[], R]:
        selected = request_scope.get()
        if selected is None:
            return fn

        def scoped() -> R:
            token = request_scope.set(selected)
            try:
                return fn()
            finally:
                request_scope.reset(token)

        return scoped

    async def run(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        loop = self.loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._request_work(lambda: fn(*args, **kwargs)))

    async def run_held(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """``fn`` in the engine's thread, as ``run``, for a step that may take a hold (``prepare``, ``mask_ws_frame``):
        given up before it ends (its request cancelled), the lease of what it returns (a ``Prepared``, alone or in a
        tuple) is released as it ends, since no one else will own it (RC-REC-11 (b)); a step never started takes
        none."""
        self.loop = asyncio.get_running_loop()
        future = self.executor.submit(self._request_work(lambda: fn(*args, **kwargs)))
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.add_done_callback(_release_given_up)
            raise

    async def run_bridge(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        loop = self.loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bridge_executor, lambda: fn(*args, **kwargs))

    def submit_launch(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        self.loop = asyncio.get_running_loop()
        return self.bridge_executor.submit(fn, *args, **kwargs)

    def close(self) -> None:
        """No more work for the two worker threads: each ends once what it holds is done (not waited for)."""
        self._held_live.clear()
        task = self._refreshing
        if task is not None and not task.done():
            with contextlib.suppress(RuntimeError):  # its loop closed already
                task.get_loop().call_soon_threadsafe(task.cancel)
        self.provenance.close()
        self.echoes.close()
        self.executor.shutdown(wait=False)
        self.bridge_executor.shutdown(wait=False)

    @property
    def opaque_provenance_bytes(self) -> int:
        return self.echoes.retained_bytes

    @property
    def checked_bytes(self) -> int:
        """What the memory of texts found fit to leave as they were kept takes, at most, and the final check's of the
        texts it found clean (a server's budget of spaces counts both, beside ``Engine.cached_bytes``): their entries
        are all of one size."""
        return len(self._as_it_left) * _CHECK_BYTES + self.verifier.cached_bytes

    # --- requests --------------------------------------------------------------------------------

    def prepare_handshake(
        self,
        upstream: Upstream,
        path: str,
        headers: dict[str, str],
        checked: tuple[str, ...],
        session: Session,
        launch: RequestLaunch | None = None,
    ) -> Prepared:
        from latet.gateway.request_policy import LocalAuthority, session_facts

        if launch is None:  # isolated preparation seam; the actual relay always supplies typed input
            prepared = self.prepare_http(upstream, "GET", path, headers, b"", checked)
            prepared.session = session
            prepared.authority = replace(prepared.authority, session=session, facts=session_facts(session))
            return prepared
        selection = request_scope.get()
        if selection is not None:
            selection.project = selection.binding = selection.launch = selection.compared = None
        reason, unavailable = self._ws_launch_scope(session, headers, BodyScan(), launch)
        scope = self.engine.scope(session)
        off = reason == CLAIMED_OFF or (scope is not None and not scope.enabled)
        if reason is None and off:  # D5: a socket of a project where Latet is off is refused before it opens
            reason = OFF_THERE
        prepared = Prepared(
            PASSTHROUGH,
            None,
            b"",
            session,
            block_reason=reason,
            protected_unavailable=unavailable,
            off=off,
        )
        if selection is not None:
            prepared.project, prepared.binding, prepared.launch = selection.project, selection.binding, selection.launch
        prepared.envelope_checked = checked
        if reason is None:
            with self.engine.vault.holding():
                result = self.verifier.verify(
                    ((text, SlotKind.OTHER, False) for text in checked),
                    session.key,
                    scope=scope,
                    record=False,
                )
            if result.blocking:
                prepared.envelope_refusal = "envelope-sensitive"
                prepared.block_reason = "request envelope refused (envelope-sensitive)"
        prepared.protected_current = selection.protected_current if selection is not None else None
        prepared.authority = replace(
            LocalAuthority.capture(self.engine, upstream),
            session=session,
            facts=session_facts(session),
            off=prepared.off,
            empty=True,
        )
        return prepared

    def prepare_http(
        self,
        upstream: Upstream,
        method: str,
        path: str,
        headers: dict[str, str],
        raw: bytes,
        envelope_checked: tuple[str, ...],
        envelope_occurrences: int = 0,
        query: str = "",
        launch: RequestLaunch | None = None,
        body_grants: ProfileGrants | None = None,
        *,
        envelope_open: Sequence[tuple[str, EnvelopeRefused]] = (),
        envelope_family: str | None = None,
    ) -> Prepared:
        """One HTTP worker call and lookup allowance for body preparation and envelope verification together.
        ``body_grants``: the body selectors of the exceptions of the request's profile (``request_policy.body_grants``),
        of which it takes those its project allows. A request with neither a body nor an envelope text to check (a
        bodyless GET) has nothing a value could be found in: it holds no vault call, whose outermost hold asks an API
        key's store what other instances learnt since (P2-8); any text to check takes it, on both machines.
        ``envelope_open``: what D4 (b)'s open list let through at the envelope, its profile's ``envelope_family``,
        refused unless the request is one Latet only checks (``_prepare``)."""
        if not raw and not envelope_checked:
            prepared = self.prepare(
                upstream, method, path, headers, raw, envelope_occurrences=envelope_occurrences, query=query,
                launch=launch, body_grants=body_grants, envelope_open=envelope_open, envelope_family=envelope_family,
            )  # fmt: skip
            prepared.envelope_checked = envelope_checked
            prepared.envelope_open = tuple(name for name, _ in envelope_open)
            if prepared.block_reason:
                prepared.close_receipt()
            return prepared
        with self.engine.vault.holding():
            prepared = self.prepare(
                upstream,
                method,
                path,
                headers,
                raw,
                envelope_occurrences=envelope_occurrences,
                query=query,
                launch=launch,
                body_grants=body_grants,
                envelope_open=envelope_open,
                envelope_family=envelope_family,
            )
            try:
                prepared.envelope_checked = envelope_checked
                prepared.envelope_open = tuple(name for name, _ in envelope_open)
                if not prepared.block_reason:
                    key = prepared.session.key if prepared.session is not None else f"route|{upstream.name}|{method}"
                    scope = self.engine.scope(prepared.session) if prepared.session is not None else None
                    result = self.verifier.verify(
                        ((text, SlotKind.OTHER, False) for text in envelope_checked),
                        key,
                        scope=scope,
                        record=False,
                    )
                    if result.blocking:
                        prepared.envelope_refusal = "envelope-sensitive"
                        prepared.block_reason = "request envelope refused (envelope-sensitive)"
                if prepared.block_reason:
                    prepared.close_receipt()
                return prepared
            except BaseException:
                prepared.close_receipt()
                if prepared.lease is not None:
                    prepared.lease.release()
                raise

    def prepare(
        self,
        upstream: Upstream,
        method: str,
        path: str,
        headers: dict[str, str],
        raw: bytes,
        *,
        envelope_occurrences: int = 0,
        query: str = "",
        launch: RequestLaunch | None = None,
        body_grants: ProfileGrants | None = None,
        envelope_open: Sequence[tuple[str, EnvelopeRefused]] = (),
        envelope_family: str | None = None,
    ) -> Prepared:
        from latet.gateway.request_policy import LocalAuthority, session_facts

        authority = LocalAuthority.capture(self.engine, upstream)
        receipts: list[EchoCheck] = []
        prepared: Prepared | None = None
        selection = request_scope.get()
        if selection is not None:  # this preparation's own project: never one an earlier preparation selected
            selection.project = selection.binding = selection.launch = selection.compared = None
        try:
            prepared = self._prepare(
                upstream,
                method,
                path,
                headers,
                raw,
                envelope_occurrences=envelope_occurrences,
                query=query,
                receipts=receipts,
                launch=launch,
                body_grants=body_grants,
                envelope_open=envelope_open,
                envelope_family=envelope_family,
            )
            if selection is not None and selection.protected_current is not None:
                prepared.protected_current = selection.protected_current
            if selection is not None:
                prepared.project, prepared.binding, prepared.launch = (
                    selection.project,
                    selection.binding,
                    selection.launch,
                )
                prepared.excepted = self._excepted(upstream, selection)
            prepared.authority = replace(
                authority,
                session=prepared.session,
                facts=session_facts(prepared.session) if prepared.session is not None else (),
                off=prepared.off,
                empty=not raw and prepared.protocol is None,
            )
            return prepared
        finally:
            for check in receipts:
                if prepared is None or prepared.block_reason or prepared.opaque_check is not check:
                    check.close()

    def _prepare(
        self,
        upstream: Upstream,
        method: str,
        path: str,
        headers: dict[str, str],
        raw: bytes,
        *,
        envelope_occurrences: int = 0,
        query: str = "",
        receipts: list[EchoCheck],
        launch: RequestLaunch | None = None,
        body_grants: ProfileGrants | None = None,
        envelope_open: Sequence[tuple[str, EnvelopeRefused]] = (),
        envelope_family: str | None = None,
    ) -> Prepared:
        protocol = protocol_for(upstream, method, path)
        shape = shape_of(method, path)
        if len(raw) > self.body_limit:
            raise codec.DecodedTooLarge(self.body_limit)
        if not raw:
            reason = "a model request requires a JSON body" if protocol is not None else None
            if reason is None and envelope_open and not _bodyless_checked_only(envelope_family, headers):
                return _open_refused(envelope_open, protocol, shape)  # D4 (b): a call with no body Latet rewrites
            prepared = Prepared(PASSTHROUGH, protocol, raw, block_reason=reason)
            if launch is not None and launch.presented is not None and reason is None:
                prepared.block_reason, prepared.protected_unavailable = self._launch_scope(launch, None, None)
            return prepared
        if protocol is None:  # a body Latet does not read: refused, on a computer as on a server
            refused = _unread_body(method)
            return Prepared(PASSTHROUGH, protocol, b"", shape=shape, block_reason=refused)
        encoding = headers.get("content-encoding")
        ledger = Ledger(envelope=envelope_occurrences)
        try:
            # Half of a character is read as U+FFFD, and a key given twice by its last copy: a request Latet rewrites
            # leaves written anew from what was read (``changed``), one it only checks with neither (``_TWICE``).
            decoded = codec.decode(raw, encoding, limit=self.body_limit)
            ledger.preflight(decoded)
            outer_stop = len(ledger.raw)
            read = wellformed.read(decoded)
        except BodyRefused as error:
            return Prepared(
                VERIFY,
                protocol,
                b"",
                shape=shape,
                block_reason=str(error),
                envelope_refusal=error.safe_code,
            )
        except codec.DecodedTooLarge:  # the forwarder answers it (413): never passed through as unreadable
            raise
        except (ValueError, codec.UnsupportedEncodingError) as exc:
            return Prepared(
                PASSTHROUGH, None, b"", shape=shape, block_reason=f"unreadable request body ({type(exc).__name__})"
            )
        body = read.doc
        if not isinstance(body, dict):
            if shape is not None:
                return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=_NOT_MESSAGES)
            refused = _NOT_AN_OBJECT
            return Prepared(PASSTHROUGH, protocol, b"", block_reason=refused)
        model = _model(body) or model_of(path) or (gemini.model_of(path) if protocol == "gemini" else None)
        if shape == "bedrock-count":  # the whole request whose tokens Bedrock counts, sent in base64
            try:
                counted = _counted(body, ledger)
            except BodyRefused as error:
                return Prepared(
                    VERIFY,
                    protocol,
                    b"",
                    shape=shape,
                    block_reason=str(error),
                    envelope_refusal=error.safe_code,
                )
            if counted is None:
                reason = "Latet cannot read this token count (not a request it knows), so it does not send it"
                return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=reason)
            body = counted
        if shape is not None and not isinstance(body.get("messages"), list):
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=_NOT_MESSAGES)
        tools = BodyScan()
        try:
            scan = _scan(protocol, body)
            _defined(protocol, body, tools)
        except (TypeError, KeyError):
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason="request body refused (shape)")
        # What the exceptions of its profile could admit is screened too, whichever of them its project takes later.
        screened = body_grants.every if body_grants is not None else None
        # Refuse before any shortcut can send or retain the original text: a live launch key where nothing masks it.
        # A request Latet rewrites masks its texts, a key in them as a known secret (R-103), screened again as it
        # leaves; one it only checks (Claude Code's, Bedrock's and Vertex's, a subscription's) masks none of them.
        checked_only = verify_only(protocol, shape, headers, body)
        if envelope_open and not checked_only:  # D4 (b): what the open list alone let through, on a request rewritten
            return _open_refused(envelope_open, protocol, shape)
        if checked_only and codec.coded(encoding):
            return Prepared(
                VERIFY, protocol, b"", shape=shape, block_reason=_CODED, envelope_refusal=_CODED_CODE
            )  # fmt: skip
        try:
            protected = not (
                ledger.declared_scan_safe(scan, masked=not checked_only)
                and ledger.declared_scan_safe(tools)
                and declared_body_safe(body, protocol, upstream, scan, ledger, grants=screened)
            )
            if not protected:
                for alternate in ledger.shadow_projections(decoded, outer_stop):
                    protected = not _projected_request_safe(alternate, protocol, upstream, ledger, shape, screened)
                    del alternate
                    if protected:
                        break
        except BodyRefused as error:
            return Prepared(
                VERIFY, protocol, b"", shape=shape, block_reason=str(error),
                envelope_refusal=error.safe_code,
            )  # fmt: skip
        if protected:
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=PROTECTED_LAUNCH)
        session = self.engine.session(_session_key(protocol, headers, body, self.engine.conversations.named))
        echo_scope = opaque_scope(upstream, method, path, protocol, query)
        echo_check = EchoCheck(
            self.engine.vault.cache_key, self.echoes, echo_scope or EchoScope((), protocol), session.key, body
        )
        receipts.append(echo_check)
        stream = bool(body.get("stream"))
        if launch is None:  # isolated preparation seam; the admitted relay always hands a launch
            self._attach_project(session, headers, scan)
        else:
            claude = protocol == "anthropic" and anthropic.is_claude_code(headers, body)
            declared = headers.get("x-claude-code-session-id")
            eligible = claude and bool(declared) and session.key == f"cc:{declared}"
            reason, unavailable = self._launch_scope(launch, session, cwd_from(headers, scan), legacy_eligible=eligible)
            if reason is not None:
                return Prepared(
                    VERIFY, protocol, b"", shape=shape, block_reason=reason, protected_unavailable=unavailable,
                    off=reason == CLAIMED_OFF,
                )  # fmt: skip
        scope = self.engine.scope(session)
        if scope is not None and not scope.enabled:  # D5: 0.11 sends no request of a project where Latet is off
            return Prepared(VERIFY, protocol, b"", session, shape=shape, block_reason=OFF_THERE, off=True)
        unread = scan.unread()
        if (refused := self._refuses(scope, unread)) is not None:
            # Its category alone, and what it did not read by fixed kinds: the dashboard flags what was not sent.
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=refused, uninspected=_unread_kinds(unread))
        claude_code = anthropic.is_claude_code(headers, body)
        if checked_only and (read.repeated or read.mended):
            return Prepared(
                VERIFY, protocol, raw, session, uninspected=unread, model=model, document=body,
                pending=[(s, [], False) for s in scan.slots],
                block_reason=_TWICE if read.repeated else _MENDED, shape=shape,
            )  # fmt: skip
        # Its tools, and the format it asks the answer in. Claude Code's plugin masks the descriptions of its tools,
        # which a computer sends as they came; a server, which never trusts a computer, reads them with the schemas of
        # their parameters and the examples of their input, and checks them, as it checks the rest of its requests.
        scan.first(tools)
        if scan.unreadable:
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=_unread_fields(scan.unreadable))
        selected = request_scope.get()
        grants = body_grants.chosen(self._excepted(upstream, selected)) if body_grants is not None else None
        # A Gemini cache's creation (POST .../cachedContents), whose model the GenAI SDK names as models/{id}.
        cache_create = protocol == "gemini" and method == "POST" and path.rstrip("/").endswith("/cachedContents")
        try:
            inspected = inspect_body(
                body,
                protocol,
                upstream,
                scan,
                checked_only,
                ledger,
                echo_check.rules(body) if echo_scope else {},
                grants=grants,
                cache_create=cache_create,
            )
        except BodyRefused as error:
            return Prepared(
                VERIFY,
                protocol,
                b"",
                shape=shape,
                block_reason=str(error),
                envelope_refusal=error.safe_code,
            )
        if any(not safe_text(text) for text in inspected.checked):
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=PROTECTED_LAUNCH)
        body_metadata: tuple[str, ...] = ()
        if scan.uninspected:
            return Prepared(
                VERIFY,
                protocol,
                raw,
                session,
                document=body,
                pending=[(s, [], False) for s in scan.slots],
                block_reason="Latet cannot read this document, audio or remote media, so it does not send it",
            )
        reads = scope.ocr if scope is not None else self.ocr
        if scan.images and (not reads or self.vision is None or not self.vision.available):
            if reads and self.vision is not None:  # the text recognition down, not images the settings leave unread
                self.vision.refused_unavailable()
            return Prepared(
                VERIFY,
                protocol,
                raw,
                session,
                document=body,
                pending=[(s, [], False) for s in scan.slots],
                block_reason=NO_OCR,
            )
        if checked_only:
            try:
                body_metadata = tuple(
                    text
                    for index, image in enumerate(scan.images)
                    for text in inspect_image(image.data(), partial(ledger.metadata, index)).texts
                )
                ledger.finish()
            except BodyRefused as error:
                return Prepared(
                    VERIFY,
                    protocol,
                    raw,
                    session,
                    document=body,
                    block_reason=str(error),
                    envelope_refusal="coverage-limit",
                )
            except UnreadImage as error:
                return Prepared(
                    VERIFY,
                    protocol,
                    raw,
                    session,
                    document=body,
                    pending=[(s, [], False) for s in scan.slots],
                    block_reason=str(error),
                )
        works = self._read_images(scan, scope, modify=not checked_only)
        if any(not safe_text(text) for text in chain(body_metadata, (work.text for work in works))):
            return Prepared(VERIFY, protocol, b"", shape=shape, block_reason=PROTECTED_LAUNCH)
        unread = scan.uninspected + ["image"] * len(self._unread(scan, works))
        refused = self._refuses_unmasked(scope, unread)
        if refused is None and checked_only:
            refused = self._not_read_here(scan, works, scope)
        if refused is not None:
            return Prepared(
                VERIFY, protocol, raw, session, uninspected=unread, model=model, document=body,
                pending=[(s, [], False) for s in scan.slots], block_reason=refused, images=works, shape=shape,
            )  # fmt: skip
        if checked_only:
            body_checked = tuple(inspected.checked) + body_metadata
            review = anthropic.is_auto_mode_review(body)
            policy = self.review if review else None
            # Its own verifier scope: the plugin's final check (same session) must not mark blocks sent.
            # Reviews have one per policy: a detail blocked again after being let through blocks again.
            sent_scope = f"gw|{session.key}"
            if policy is not None:
                sent_scope = f"review|{','.join(sorted(policy.allowed))}|{session.key}"
            slots = ((s.get(), s.kind, s.from_model) for s in scan.slots)
            # What the model wrote, exactly so, leaves: known only of Claude Code's conversations (``read_written``, and
            # the plugin's tool calls). Another client's assistant turns are its own texts.
            written = partial(self.verifier.written_source, session.key) if claude_code else None
            # A clean text check cannot publish sent-history authority for a refused body.
            # Keep the same check/budget path, including its blocking and source evidence.
            result = self.verifier.verify(
                slots,
                sent_scope,
                policy,
                scope,
                written=written,
                checked_only=body_checked,
                record=inspected.problem is None,
            )
            prepared = Prepared(
                VERIFY, protocol, raw, session, result.leaks, unread, stream=stream, model=model,
                document=body, pending=[(s, [], False) for s in scan.slots], review=review,
                withheld=result.withheld, images=works, claude_code=claude_code, body_checked=body_checked,
                body_semantic=tuple(inspected.semantic), body_metadata=body_metadata,
                body_rules=tuple(sorted(inspected.rules | result.rules | ({M3} if works else set()))),
                body_input=raw, body_ledger=ledger, body_upstream=upstream, body_scan=scan, body_grants=grants,
                body_cache_create=cache_create, opaque_check=echo_check,
                body_verified=not result.blocking, body_verified_payload=raw,
                body_authority=tuple(sorted(result.authority)),
                body_epoch=self._content_epoch, body_verified_token=self._content_epoch.token,
                body_structure=freeze_body(body),
            )  # fmt: skip
            if result.blocking:
                what = ", ".join(sorted({leak.preview for leak in result.blocking}))
                prepared.block_reason = f"sensitive values would leave in clear: {what}"
                if policy is not None:
                    prepared.block_reason = block_reason(policy, [(x.detail, x.preview) for x in result.blocking])
                elif shape is not None and not claude_code:
                    prepared.block_reason += (
                        "; Bedrock and Vertex calls are checked, never rewritten: make them from Claude Code with "
                        "Latet's plugin"
                    )
            if inspected.problem is not None and prepared.block_reason is None:
                prepared.block_reason = inspected.problem
            prepared.shape = shape
            return self._mark_outbound(prepared)
        removed = _remove_declared(inspected)
        report = MaskReport()
        lease = None
        try:
            masked, done_slots, lease = self._mask_slots(scan.slots, session, report, scan.keyed)
            # its conversation's vault holds for its answer, whatever stops it from here
            withheld = sum(done.withheld and done.fresh for done in done_slots)
            pending = [(slot, done.replacements, done.exact) for slot, done in zip(scan.slots, done_slots, strict=True)]
            # A coded body leaves encoded again from what was read, never as the container it came in (G2 review I-1).
            changed = (
                masked > 0 or read.mended or read.repeated or bool(works) or bool(removed) or codec.coded(encoding)
            )
            out = self._serialize_body(body, encoding) if changed else raw
            prepared = Prepared(
                REWRITE, protocol, out, session, [], unread, masked, report.degraded, stream=stream,
                model=model, document=body, pending=pending, withheld=withheld, masked=done_slots,
                encoding=encoding, images=works, lease=lease,
                body_semantic=tuple(inspected.semantic), body_metadata=body_metadata,
                body_input=raw, body_ledger=ledger, body_shape=inspected.shape,
                body_upstream=upstream, body_scan=scan, body_epoch=self._content_epoch, body_grants=grants,
                body_cache_create=cache_create, opaque_check=echo_check, body_removed=removed, recorded_later=True,
            )  # fmt: skip
            self._check(prepared)  # one stopped here is shown to no reviewer, nor to you
            if prepared.block_reason is None:
                prepared.reviews = self._review_items(session, scan.slots, done_slots)
        except MaskingError as exc:  # a vault at its limit says so: a MaskingError's words name no value
            log.error("masking failed: %s", exc)
            return Prepared(REWRITE, protocol, raw, session, block_reason=f"masking failed: {exc}", lease=lease)
        except BaseException:  # anything else once it held: no request will own the hold, so it goes now
            if lease is not None:
                lease.release()
            raise
        try:
            return self._mark_outbound(prepared)
        except BaseException:
            if lease is not None:
                lease.release()
            raise

    def _serialize_body(self, document: Any, encoding: str | None = None, *, frame: bool = False) -> bytes:
        limit = min(self.body_limit, sse.MAX_DECODED) if frame else self.body_limit
        # Its size before the encoder makes it: an upper bound first, counted character by character only past it.
        if _json_bound(document) > limit and _json_size(document) > limit:
            raise codec.DecodedTooLarge(limit)
        raw = dumps_compact(document).encode()
        encoded = codec.encode(raw, encoding)
        if len(encoded) > limit:
            raise codec.DecodedTooLarge(limit)
        return encoded

    def _check(self, prepared: Prepared) -> None:
        """A request this gateway rewrote, checked before it leaves as one it may only check is (``Verifier.verify``):
        whatever its masking left in clear (a card number written as a JSON number, which ``balanced`` does not mask; a
        value a reviewer found, in a text it did not read). A value of a class the check stops, in clear in a text new
        to the conversation, stops the request (``block_reason``); one in a text recorded as left (``left``) is said
        (``leaks``), never stopped, so that a running conversation is not stopped by its past. Nothing is recorded as
        left here: ``left`` does it once the request has reached its provider. The name model is not run, as the final
        check never runs it. Called again once the local reviewer or you masked more, on what leaves then. In the
        engine's thread."""
        session = prepared.session
        if session is None or prepared.mode != REWRITE or prepared.off:
            return
        ledger, scan = prepared.body_ledger, prepared.body_scan
        if ledger is not None and scan is not None and prepared.body_upstream is not None:
            # What leaves: its slots, as masked, screened here; every other text the declaration reads is screened as
            # the inspection below reads it (``body_checked``), or refused by it (a fixed word, a member it does not
            # read). The declaration's own walk, which read them all once more, is made before masking alone (R-103):
            # tests/unit/test_request_coverage_speed.py holds the two to the same refusals.
            try:
                if not ledger.declared_scan_safe(scan):
                    prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
                    return
            except BodyRefused as error:
                prepared.discard_unadmitted_content(str(error), error.safe_code)
                return
        if prepared.block_reason:
            return
        frozen = None  # the body's shape as accounted here, when it is accounted again
        if prepared.body_ledger is not None and prepared.body_upstream is not None and prepared.body_scan is not None:
            prepared.body_verified = False
            try:
                inspection = inspect_body(
                    prepared.document,
                    prepared.protocol or "",
                    prepared.body_upstream,
                    prepared.body_scan,
                    False,
                    prepared.body_ledger,
                    prepared.opaque_check.rules(prepared.document) if prepared.opaque_check is not None else {},
                    grants=prepared.body_grants,
                    excepted=prepared.body_excepted,
                    # Masking changed its leaf strings alone: its final paths are those of its last account. Images
                    # re-encoded or withheld are accounted again, with their metadata.
                    accounted=None if prepared.images else prepared.body_shape,
                    cache_create=prepared.body_cache_create,
                )
                frozen = inspection.shape
                # Re-encoded/repainted carriers have a new container shape. Its
                # generated parts share this exchange ledger and this same final
                # verifier call; removed source metadata receives no allowance.
                if prepared.images:
                    prepared.body_metadata = tuple(
                        text
                        for index, work in enumerate(prepared.images)
                        if work.state != WITHHELD
                        for text in inspect_image(work.ref.data(), partial(prepared.body_ledger.metadata, index)).texts
                    )
                    prepared.body_ledger.finish()
                prepared.body_checked = tuple(inspection.checked) + prepared.body_semantic + prepared.body_metadata
                if any(not safe_text(text) for text in prepared.body_checked):
                    prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
                    return
                prepared.body_rules = tuple(sorted(inspection.rules))
                if inspection.problem is not None:
                    prepared.discard_unadmitted_content(inspection.problem)
                    return
            except UnreadImage as error:
                prepared.discard_unadmitted_content(str(error))
                return
            except BodyRefused as error:
                code = error.safe_code
                prepared.discard_unadmitted_content(str(error), code)
                return
        slots = [(text if isinstance(text := slot.get(), str) else "", slot.kind, slot.from_model)
                 for slot, _, _ in prepared.pending]  # fmt: skip
        result = self.verifier.verify(
            slots,
            session.key,
            scope=self.engine.scope(session),
            record=False,
            rewritten=True,
            checked_only=(*prepared.body_checked, *prepared.envelope_checked),
        )
        prepared.leaks = result.leaks
        prepared.body_authority = tuple(sorted(result.authority))
        if prepared.body_ledger is not None:
            # The freeze of what leaves: the account's own when it was made here (nothing changed the body since).
            frozen = frozen if frozen is not None else freeze_body(prepared.document)
            prepared.body_structure = prepared.body_shape = frozen
        else:
            prepared.body_structure = None
        prepared.body_verified = not result.blocking
        prepared.body_verified_payload = prepared.body
        prepared.body_verified_token = self._content_epoch.token
        prepared.body_rules = tuple(
            sorted(
                set(prepared.body_rules)
                | result.rules
                | ({M3} if prepared.images else set())
                | (
                    {"sent before in this conversation"}
                    if any(done.history_allowed for done in prepared.masked)
                    else set()
                )
            )
        )
        if result.blocking:
            what = ", ".join(sorted({leak.preview for leak in result.blocking}))
            prepared.block_reason = f"sensitive values would leave in clear: {what}"

    def left(self, prepared: Prepared) -> None:
        """A request (or a WebSocket turn) this gateway rewrote has reached its provider: what each of its texts was as
        it left is recorded in its conversation (``record_left``), so that sent again there it may leave as it left
        (``_leaves_as_it_left``): nothing else stands between the gateway and the provider to record it. Handed to the
        engine's thread, after what it was given before (the conversation's next request is masked after it); called on
        the event loop. A failure is said in the log by its type, and the texts are then masked anew when sent again."""
        if prepared.session is None or prepared.mode != REWRITE or prepared.off:
            return
        if prepared.body_removed:
            self.body_removed_members = min((1 << 63) - 1, self.body_removed_members + len(prepared.body_removed))
        loop = self.loop = asyncio.get_running_loop()
        with contextlib.suppress(RuntimeError):  # the engine's thread has ended (the space closes): nothing to record
            loop.run_in_executor(self.executor, self.record_left, prepared).add_done_callback(_left_failure)

    def record_left(self, prepared: Prepared) -> None:
        """The texts of ``prepared``, as its body carried them when it left, recorded as left in its conversation (the
        verifier's record, which the final check keeps for what it lets out). In a worker thread: on a server it waits
        on Valkey."""
        if prepared.session is not None:
            texts = [text for slot, _, _ in prepared.pending if isinstance(text := slot.get(), str) and text]
            self.verifier.left(prepared.session.key, texts)

    def written(
        self, prepared: Prepared, content_type: str, content_encoding: str | None, *, observe: bool = True
    ) -> Written | None:
        """What the model writes in the answer to ``prepared``, to read as it passes (``read_written``): a request of
        Claude Code's that Latet only checks, its answer passed on as it came. None for any other."""
        if prepared.session is None or prepared.off:
            return None
        record_text = prepared.mode == VERIFY and prepared.claude_code
        opaque = self.opaque_capture(prepared) if observe else None
        return (
            Written(content_type, content_encoding, opaque=opaque, record_text=record_text)
            if record_text or opaque
            else None
        )

    def opaque_capture(self, prepared: Prepared) -> OpaqueCapture | None:
        check = prepared.opaque_check
        return (
            OpaqueCapture(check, self.echoes)
            if check is not None and check.scope.binding and not prepared.off
            else None
        )

    async def commit_opaque(self, capture: OpaqueCapture | None, held: int = 0) -> None:
        """The terminal owner holds publication while the existing engine worker commits observation.

        Extra terminal bytes are charged only for this await; the worker retains digest reservations separately.
        Cancellation releases queued work, while a running capture releases its reservation when it actually ends.
        """
        if capture is None or not capture.ready or capture.closed:
            return
        if not capture.pending:
            capture.close()
            return
        if not capture.backend.reserve(held):
            capture.close()
            return
        try:
            await self.run(capture.commit)
        finally:
            capture.close()
            capture.backend.release(held)

    async def read_written(self, capture: Written, session: Session, chunk: bytes | None) -> None:
        """What the model wrote that ``chunk`` of its answer ends (None: the answer's end), known in its conversation
        before the chunk is passed on (``Verifier.wrote``): the request the client makes after it finds it known. A step
        at a time (``Written.pieces``), each decoded where it came, as a restore's. A step that may parse more than a
        little at once (``Written.load``: a whole body, an event of megabytes, a tool call's input joined from its
        pieces) is read and recorded on the engine's thread, as a large restore is: the event loop carries every other
        identity's streams. A small one is read on the event loop in this identity's turn (``restoring``), one step of
        one of its answers at a time however many it has, waiting behind no masking, and what it ends recorded
        ``WRITTEN_AT_ONCE`` texts at a time, each once, the loop serving the others between two. Never waits on a
        network."""
        for piece in capture.pieces(chunk):
            await self._read_written_step(capture, session, piece)
        if chunk is None:
            await self._read_written_step(capture, session, None)
        await self.commit_opaque(
            capture.opaque, len(chunk) + 64 if chunk is not None and not capture.holds_answer else 0
        )

    async def _read_written_step(self, capture: Written, session: Session, piece: bytes | None) -> None:
        """One step of ``read_written``: ``piece``, or the answer's end (None)."""
        if capture.broken:
            return
        if capture.load(piece) > RESTORED_ON_LOOP:
            loop = self.loop = asyncio.get_running_loop()
            try:
                done = loop.run_in_executor(self.executor, self._write_down, capture, session.key, piece)
            except RuntimeError:  # the engine's thread has ended (the space closes): nothing more is taken
                capture.broken = True
                return
            await done
            return
        async with self.restoring:
            texts = capture.read(piece) if piece is not None else capture.end()
            distinct = list(dict.fromkeys(text for text in texts if safe_text(text)))
            for start in range(0, len(distinct), WRITTEN_AT_ONCE):
                self.verifier.wrote(session.key, distinct[start : start + WRITTEN_AT_ONCE])
                await asyncio.sleep(0)
            await asyncio.sleep(0)

    def _write_down(self, capture: Written, session_key: str, piece: bytes | None) -> None:
        """A step of ``read_written`` too large for the event loop, read and recorded in the engine's thread: each text
        once, found so a text at a time (Python's own pass over hundreds of thousands of them would hold its lock, and
        so the event loop, all along)."""
        texts = capture.read(piece) if piece is not None else capture.end()
        seen: set[str] = set()
        distinct = [text for text in texts if safe_text(text) and not (text in seen or seen.add(text))]
        self.verifier.wrote(session_key, distinct)

    def _mark_outbound(self, prepared: Prepared) -> Prepared:
        """Tools the provider ran with stand-ins in their input: too late to stop, so said (once)."""
        session = prepared.session
        if session is None or prepared.off or prepared.document is None:
            return prepared
        masker = self._masker(session)
        for tool, refs in outbound.server_calls(prepared.protocol, prepared.document):
            values = [ref.get() for ref in refs]
            types = outbound.stand_ins(masker, values)
            if not types:
                continue
            message = "\x00".join((session.key, tool, *values)).encode()
            key = hmac.new(self.engine.vault.cache_key, message, hashlib.sha256).digest()
            if self._outbound_seen.get(key):
                continue
            self._outbound_seen.put(key, True)
            prepared.outbound.append(tool)
            prepared.outbound_slots.extend(refs)
            log.warning(
                "provider outbound tool used stand-ins: category=%s occurrences=%d", outbound.category(tool), len(types)
            )
        if prepared.outbound_slots or prepared.body_ledger is not None:
            prepared.refresh_recording()
        return prepared

    def _excepted(self, upstream: Upstream, selection: RequestScope | None) -> frozenset[str]:
        """The folders of ``upstream``'s project exceptions that name the project Latet trusts this request's to be
        (``request_policy.trusted_folders``), compared here, in the worker that prepares it (I-6)."""
        from latet.gateway.request_policy import trusted_folders

        if selection is None:
            return frozenset()
        return trusted_folders(
            upstream.envelope_exceptions,
            binding=selection.binding,
            compared=selection.compared,
            projects=self.engine.scopes,
        )

    def _launch_scope(
        self,
        launch: RequestLaunch,
        session: Session | None,
        claimed: str | None,
        *,
        legacy_eligible: bool = False,
    ) -> tuple[str | None, bool]:
        """The one launch decision of a request, on both machines (Revision 9): the authority construction injected
        (``RequestLaunch.decide``) says whether its launch is verified, bound by the plugin, a claim, refused or
        unavailable; a verified or bound project gives its rules and its one freshness check (awaited where the request
        is sent), a claim only tightens (D7) and a claimed project that is off is refused (D5).

        Returns (reason, unavailable)."""
        from latet.projects import Projects, _a_folder

        refused = "request launch authority refused"
        request = request_scope.get()
        if request is None:
            return refused, False
        scopes = self.engine.scopes
        chosen = launch.decide(session, claimed, legacy_eligible=legacy_eligible, projects=scopes)
        if chosen.status == "unavailable":
            return "request launch authority unavailable", True
        if chosen.status == "refused":
            return refused, False
        request.launch = chosen.category  # recorded whatever follows: a claimed project that is off is refused below
        if chosen.project is not None and chosen.current is not None:
            request.scope = chosen.project.scope
            request.protected_current = chosen.current
            request.project, request.binding = chosen.project.id, "trusted"
            # Its project as projects are compared (R-105): the id a project exception's folder is compared with, the
            # same on both machines, whichever folder of the project it was bound in (I-4).
            compare = getattr(scopes, "compared_id", None)
            request.compared = compare(chosen.project.root) if compare is not None else None
        else:
            folder = claimed or (session.cwd if session is not None else None)
            project = scopes.resolve(folder) if isinstance(scopes, Projects) and _a_folder(folder) else None
            if project is not None:
                request.project, request.binding = project.id, "claimed"
            if project is not None and project.scope is not None and not project.scope.enabled:
                return CLAIMED_OFF, False
            request.scope = None if project is None else project.safe if project.lowers else project.scope
        request.session = session
        return None, False

    def _ws_launch_scope(
        self,
        session: Session,
        headers: dict[str, str],
        scan: BodyScan,
        launch: RequestLaunch | None,
    ) -> tuple[str | None, bool]:
        if launch is None:
            return None, False  # direct preparation tests; never the admitted relay
        # A WebSocket turn has no admitted Claude plugin binding (Responses only).
        return self._launch_scope(launch, session, cwd_from(headers, scan))

    def _attach_project(self, session: Session, headers: dict[str, str], scan: BodyScan) -> None:
        """Give a session its project, from what the request says of its working directory. Claude
        Code's sessions get theirs from the plugin, which knows the folder before any request."""
        scopes = self.engine.scopes
        if scopes is None or session.key.startswith("cc:"):
            return
        # Also while no folder was reported to this process: on a Latet server the project may have come from another
        # instance, and a request that says its folder reports it here (``Projects.scope_for``).
        if session.project_id is None or session.reported is None or headers.get("x-latet-project"):
            self.engine.attach(session, cwd_from(headers, scan))

    def _mask_slots(
        self,
        slots: Sequence[SlotRef],
        session: Session,
        report: MaskReport,
        keyed: Sequence[tuple[Any, SlotKind, bool]] = (),
    ) -> tuple[int, list[Masked], ExchangeLease]:
        """Mask the slots of a request in place: (values masked, what was done to each, the lease of what its
        conversation's vault holds for its answer: ``Engine.hold``'s handle, registered as it is taken). And the keys
        of the ``keyed`` objects (a tool call's input), each masked as a text and written back in its place, the slots
        of its object's values told its new name: a key the model wrote with a fake in it is restored in the answer, and
        so leaves masked again when the agent sends it back. Two keys of an object that would be one stop the request
        (``KeysCollide``)."""
        texts = [s.get() for s in slots]
        named = [(obj, kind, from_model) for root, kind, from_model in keyed for obj in objects(root)]
        vault = self.engine.vault
        # The values the request's texts hold, in memory at once, in one call to the store where the vault keeps most of
        # its values there (an API key's: ``Vault.prefetch``), and held until the request is masked.
        with vault.holding():
            vault.prefetch([*texts, *(key for obj, _, _ in named for key in obj)])
            call = self.call()
            done_slots = [
                self.mask_value(
                    text, s.kind, session, report, from_model=s.from_model, path=s.source, tool=s.tool,
                    json_mode=s.mode == "json",
                    call=call,
                )
                for s, text in zip(slots, texts, strict=True)
            ]  # fmt: skip
            keys = [  # a fake drawn for a key is none of the other keys of its object (``context``)
                (obj, key, self.mask_value(key, kind, session, report, from_model=from_model, call=call, context=ctx))
                for obj, kind, from_model in named
                for ctx in ("\n".join(obj),)
                for key in obj
            ]
            self.settle(call)  # a key too: masked again when a later text of the request taught the vault a value in it
        masked = 0
        for slot, done in zip(slots, done_slots, strict=True):
            if done.text != done.original:
                slot.put(done.text, done.replacements)
                masked += max(len(done.replacements), 1)
        names: dict[int, tuple[dict[str, Any], dict[str, str]]] = {}
        for obj, key, done in keys:
            if done.text != key:
                names.setdefault(id(obj), (obj, {}))[1][key] = done.text
        # Each object renamed tells the slots of its own values alone, found once by their object: a request of tens of
        # thousands of such objects would otherwise walk every slot for each.
        held: dict[int, list[SlotRef]] = {}
        if names:
            for slot in slots:
                if id(slot.container) in names:
                    held.setdefault(id(slot.container), []).append(slot)
        for at, (obj, given) in names.items():
            rename(obj, given, held.get(at, []))
            masked += len(given)
        lease = ExchangeLease(self.engine, session)  # what its answer restores, in memory (``Engine.hold``)
        lease.acquire()
        return masked, done_slots, lease

    def call(self) -> Call:
        """What ``mask_value`` notes of a request's texts, for ``settle`` once they are all masked."""
        return Call(self.engine.vault.version)

    def settle(self, call: Call) -> None:
        """Mask again the texts of a request that a later text of it made stale: the vault learnt a
        value from the later one (an address's domain, a secret found by its key) that an earlier one
        had left in clear, whole or in part (an IBAN around a secret it masked). The texts new to the conversation are
        masked again, in place. One sent as it was kept is asked again whether it may leave so
        (``_leaves_as_it_left``): known to have left so in its conversation, it leaves as it left; otherwise, holding
        in clear a value the vault knows now, it is masked anew, with every sight of it in the request."""
        refresh = getattr(self.engine.vault, "refresh", None)
        if refresh is not None:
            refresh()
        if self.engine.vault.version == call.version:
            return
        originals = dict.fromkeys(done.original for _, dones in call.new.values() for done in dones)
        originals.update(dict.fromkeys(done.original for _, _, sights in call.kept.values() for _, done in sights))
        # A refresh revoked the call's word answers. Restore their batching before even classifying kept texts:
        # each individual prefetch otherwise rebuilds a matcher over a progressively growing candidate set.
        self.engine.vault.prefetch(list(originals))
        groups = dict(call.new)
        for key, (session, again, sights) in call.kept.items():
            entries = []
            for left, done in sights:
                entry = self._leaves_as_it_left(session, key, left, done.original)
                if entry is None:
                    break
                entries.append(entry)
            else:
                for (_, done), entry in zip(sights, entries, strict=True):
                    done.history_allowed = self._history_allowed(entry)
                continue
            masking, dones = groups.get(key, (again, []))  # also masked anew in the request: one group
            groups[key] = (masking, [*(done for _, done in sights), *dones])
        for again, dones in groups.values():
            new = again()  # the engine masks again what holds a value learnt since, and only that
            for done in dones:
                if not new.fresh and done.text == new.text:
                    continue
                # Masked again now, or already by a later sight of the same text in the request (masked anew there: it
                # held a value learnt since): either way this one leaves as the text masks now.
                done.text, done.replacements, done.exact = new.text, list(new.replacements), new.exact
                done.fresh, done.withheld = True, new.withheld
                done.history_allowed = new.history_allowed

    def mask_value(
        self,
        value: str,
        kind: SlotKind,
        session: Session,
        report: MaskReport,
        *,
        from_model: bool = False,
        path: str | None = None,
        tool: str | None = None,
        json_mode: bool = False,
        keep_surrogates: bool = False,
        extra: Sequence[ExtraValue] = (),
        call: Call | None = None,
        context: str = "",
        as_text: bool = False,
    ) -> Masked:
        """Mask one text of a request (the proxy's slots, the plugin's items alike).

        A text its conversation sent before is sent again as it left, so that the provider's cache
        still holds it, even when the vault has learnt since a value it left in clear, once it is
        known to have left so (``_leaves_as_it_left``); a text new to the conversation is masked with
        what the vault knows now. A tool output holding more personal values than the dump guard
        allows is withheld. ``call`` notes the texts new to the conversation, which ``settle`` masks
        again when a later text of the request teaches the vault a value in them. ``context``: the text
        around it, which a fake drawn for a value in it must not hold (a key's, the other keys of its object).
        ``as_text``: masked as one text, a tool output that is a JSON document too, never value by value (an
        application's, whose positions are in the text it gets back). A session not ``kept`` (an application's call,
        which is no conversation) sends nothing again: none of its texts is kept as it left, and each is masked with
        what the vault knows now.
        """
        # A text masked as one text is kept apart from the same text masked value by value (the keys of the texts kept
        # before stay as they were).
        flags = f"{int(from_model)}{int(keep_surrogates)}{int(json_mode)}{'t' if as_text else ''}"
        key = self._given_key(session, kind, value, path, tool, flags)
        given = None if extra or not session.kept else self.given.masked(key)
        entry = self._leaves_as_it_left(session, key, given, value) if given is not None else None
        if entry is None:
            given = None  # a value the vault knows is in clear in it, whole or in part, and it never left so: anew

        def new() -> Masked:
            done = self._mask_new(
                value, kind, session, report, from_model, path, tool, json_mode, keep_surrogates, extra, context,
                as_text,
            )  # fmt: skip
            if session.kept:
                self.given.keep(key, Left(done.text, tuple(done.replacements), done.exact))
            return self._guarded(done, kind, session)

        if given is None or entry is None:
            done = new()
        else:
            masked, replacements, exact = given.text, given.replacements, given.exact
            if keep_surrogates:
                self.engine.adopt(session, value, self._masker(session))
            self.engine.show(session, (r.entity_id for r in replacements if r.entity_id))
            done = self._guarded(
                Masked(masked, list(replacements), exact, original=value, history_allowed=self._history_allowed(entry)),
                kind,
                session,
            )
        if call is not None and (given is None or key in call.new):  # the same text twice in a request too
            call.new.setdefault(key, (new, []))[1].append(done)
        elif call is not None and given is not None:  # sent as it was kept: asked again in ``settle``
            call.kept.setdefault(key, (session, new, []))[2].append((given, done))
        return done

    def _guarded(self, done: Masked, kind: SlotKind, session: Session) -> Masked:
        """``done``, or the dump guard's notice in its place: a tool output with too many personal values."""
        if kind == SlotKind.TOOL_RESULT:
            scope = self.engine.scope(session)
            limit = scope.dump_guard if scope is not None else self.dump_guard
            count = personal_values(done.replacements)
            if limit and count > limit:  # not set or 0: no limit
                return Masked(dump_notice(count, limit), [], True, done.fresh, withheld=True, original=done.original)
        return done

    def _given_key(
        self, session: Session, kind: SlotKind, text: str, path: str | None, tool: str | None, flags: str
    ) -> bytes:
        """Where ``text``, as masked for ``session`` by the rules in use, is kept (new rules mask it again): the same
        on every instance of a Latet server, which name the rules by what they are (``rules_tag``, ``Scope.tag``)."""
        scope = self.engine.scope(session)
        rules = f"{self.engine.rules_tag}|{scope.tag if scope is not None else ''}"
        message = "\x00".join((session.key, rules, kind.value, flags, path or "", tool or "", text)).encode()
        return hmac.new(self.engine.vault.cache_key, message, hashlib.sha256).digest()

    def _leaves_as_it_left(self, session: Session, key: bytes, left: Left, original: str) -> tuple[int, bool] | None:
        """Whether a text its conversation sent before (``original``, as its client sends it) leaves again as it was
        kept (``left``): while it shows in clear no value the vault knows, whole or in part (``shows_known``: one learnt
        since around a value masked in it too, 0.10.2), or once the final check saw that very text leave in the
        conversation. A JSON document masked value by value has its replacements' places inside its values, none in
        ``original``: any value the vault knows in it has that asked. A text is kept as it is masked, before its request
        leaves (it may never leave), and another process may have kept it again since, or someone who writes the store
        written an older version back: one that never left must not bring a value masked since back in clear, nor leave
        a value learnt since around one masked in it in clear in part. Nor, in an API key's vault, one that names an
        entry the vault no longer holds (``Vault.forgot_any``): its value was forgotten by itself since (Task 11b), and
        the text is masked anew, never sent with the old fake, which restores nothing, nor that entry shown to the
        conversation again, which would name it in the conversation's record (Ruling 11b-R1); asked before the check of
        the version, which such a forget heard elsewhere does not move. A person's Forget leaves the texts as they left
        (review 11b's N2). Checked once per vault version, for the text read now. The values the kept text holds among
        those its request's words found (an API key's vault, which keeps most of its values in its store) are brought
        into memory first, in whatever form the text was kept (``Vault.weigh``), and those it names, by their ids.

        Returns its entry when it leaves as it left (the vault version it was checked at, whether it shows a known
        value in clear), None when it does not: what ``_history_allowed`` reads, its keyed mark made once (P2-8)."""
        vault = self.engine.vault
        vault.weigh([left.text])  # first: what it brings into memory moves the version
        named = [r.entity_id for r in left.replacements if r.entity_id]
        if named and vault.forgot_any(named):  # those out of memory brought back first: it moves the version
            return None
        version = vault.version  # first: a value learnt while it is checked has it checked again
        mark = hmac.new(vault.cache_key, b"L\x00" + key + left.text.encode(), hashlib.sha256).digest()
        cached = self._as_it_left.get(mark)
        if cached is not None and cached[0] == version:
            return cached
        masker = self._masker(session)
        if left.exact:
            shown = shows_known(masker, original, left.text, left.replacements)
        else:  # a JSON document masked value by value: its replacements place no value in ``original``
            shown = masker.known_outside(original, ())
        # Secret values newly learned from a later turn must not be replayed
        # in clear. Retain the existing history contract for non-secret data.
        from latet.engine.detectors.base import DetectContext
        from latet.engine.types import EntityClass
        secret_shown = shown and any(
            span.eclass == EntityClass.SECRET and not masker.fake_or_let_through(left.text, span)
            for span in masker.detect(left.text, DetectContext())
        )
        if shown and (secret_shown or not self.verifier.sent(session.key, left.text)):
            return None
        entry = (version, bool(shown))
        self._as_it_left.put(mark, entry)
        return entry

    def _history_allowed(self, entry: tuple[int, bool]) -> bool:
        """Whether a text that leaves as it left (its entry, ``_leaves_as_it_left``'s) does so holding a value in clear
        the conversation sent before (``HISTORY``): its check made at the vault's version still."""
        return entry[0] == self.engine.vault.version and entry[1]

    def _mask_new(
        self,
        value: str,
        kind: SlotKind,
        session: Session,
        report: MaskReport,
        from_model: bool,
        path: str | None,
        tool: str | None,
        json_mode: bool,
        keep_surrogates: bool,
        extra: Sequence[ExtraValue],
        context: str = "",
        as_text: bool = False,
    ) -> Masked:
        """Mask a text new to its conversation, with what the vault knows now.

        A tool output that is a JSON document is masked value by value (not ``as_text``), each with the path of its
        key, so that line-based detectors see real lines and field rules see their fields. A value
        the vault learns from one of them (an address's domain, a secret found by its key) is masked
        in the values before it too, as in every later text. Its strings and keys are read ahead by the
        local name model, at once (``_read_ahead``): one it cannot read in ``DOCUMENT_READ_SECONDS`` is
        withheld, never sent with what the model would have found there in clear.
        """
        replacements: list[Replacement] = []
        fresh = False
        results: dict[tuple[str, str | None], SlotResult] = {}  # the texts masked here, with their keys
        around: dict[tuple[str, str | None], str] = {}  # a key's context, the other keys of its object

        def masking(text: str, key: str | None, again: bool = False) -> SlotResult:
            return self.engine.mask_text(
                text, kind, session, from_model, path, report, keep_surrogates, tool=tool, field=key, extra=extra,
                again=again, context=around.get((text, key), context),
            )  # fmt: skip

        def mask(text: str, key: str | None = None, shown: str | None = None, others: str = "") -> str:
            nonlocal fresh
            result = results.get((text, key))
            if result is None:
                if others:
                    around[text, key] = others
                result = results[text, key] = masking(text, key)
                fresh = fresh or not result.cached
            if key is None or shown is None or shown == key:
                replacements.extend(result.replacements)
            else:  # its field holds a key masked: each rule that names the field names it as it is sent
                replacements.extend(_shown(result.replacements, key, shown))
            return result.text

        exact = True
        if not as_text and (json_mode or (kind == SlotKind.TOOL_RESULT and _json_document(value))):
            numbers = self.engine.json_numbers(session, kind, tool, path)
            sent: dict[str, str] = {}  # a path with a key masked in it, as it is sent
            # Each path asked, as it is sent: read once for every value under it (a path is the same string for each),
            # as a path is made once, never at a cost of its length for each value.
            asked: dict[str, str] = {}

            def sent_path(key_path: str) -> str:
                found = sent.get(key_path)
                if found is None:
                    found = asked.get(key_path)
                if found is None:
                    above = f"{sent_path(key_path[:-2])}[]" if key_path.endswith("[]") else key_path
                    found = asked[key_path] = above
                return found

            def name(key: str, inside: str, others: str) -> str:
                # A key is masked as a text of its own, of no field: a field rule or strict data mode finds a value by
                # its key, and a key that names a field (`id`, `email`) changes only where a detector finds a value in
                # it (an address, a name, a term), with the fake that value has everywhere. A fake drawn for it is none
                # of the object's other keys, so that two keys are never one (``others``).
                masked = mask(key, others=others)
                above = sent_path(inside)
                if masked != key or above != inside:
                    sent[f"{inside}.{key}" if inside else key] = f"{above}.{masked}" if above else masked
                return masked

            def leaf(text: str, key_path: str, number: bool) -> str:
                # Its field, cut from the path once for every value under it (the same string each time).
                key = document.once(("field", key_path), lambda: _field(key_path))
                if number and key is None:
                    return text  # the numbers of a wrapper (exit code, duration) are not data
                if key is None and kind == SlotKind.TOOL_RESULT and _json_document(text):
                    return map_json_values(text, leaf, numbers, name)  # JSON in the tool's own text (MCP)
                at = sent_path(key_path) if key is not None and sent else key_path
                if at is key_path:
                    return mask(text, key)
                return mask(text, key, document.once(("sent field", at), lambda: _field(at)))

            # What a path costs is read once for the document: each value costs what it holds, not its key's length.
            masker = self._masker(session)
            with masker.reading_ahead(), document.reading():
                if masker.reads_ahead(kind):
                    count = self._read_ahead(
                        value, kind, session, numbers, from_model, path, tool, keep_surrogates, extra
                    )
                    if count is not None:  # the name model could not read its texts in time: nothing of it leaves
                        report.degraded.append("ner")
                        return Masked(unread_notice(count, DOCUMENT_READ_SECONDS), [], True, True, True, value)
                version = self.engine.vault.version
                masked = map_json_values(value, leaf, numbers, name)
                if self.engine.vault.version != version:
                    # A value masked before a later one taught the vault a value in it, or around a value masked in it,
                    # is masked again, one from the cache too, a key as a value: an output sent before leaves again as
                    # it left (``given``).
                    stale = [
                        pair
                        for pair, result in results.items()
                        if shows_known(masker, pair[0], result.text, result.replacements)
                    ]
                    for text, key in stale:
                        results[text, key] = masking(text, key, again=True)
                    if stale:
                        fresh = True
                        replacements.clear()
                        sent.clear()
                        asked.clear()
                        masked = map_json_values(value, leaf, numbers, name)
            exact = masked == value  # offsets are inside the decoded values
        else:
            masked = mask(value)
        return Masked(masked, replacements, exact, fresh, original=value)

    def _read_ahead(
        self,
        value: str,
        kind: SlotKind,
        session: Session,
        numbers: bool,
        from_model: bool,
        path: str | None,
        tool: str | None,
        keep_surrogates: bool,
        extra: Sequence[ExtraValue],
    ) -> int | None:
        """The strings and keys of the JSON document ``value`` that its masking reads, read ahead at once by the local
        name model (``Masker.read_ahead``), in as few runs as hold them, within ``DOCUMENT_READ_SECONDS`` (the engine's
        thread, in ``Masker.reading_ahead``): run once for each, it cost a document of a thousand short values minutes,
        on a computer past Claude Code's hooks' time, on a server beside every other identity. A text masked before, as
        its masking will find it, is not read again. Every other is, a value strict data mode masks whole too: what the
        model finds there is what the vault learns. None when all were read; else how many there were to read."""
        wanted: dict[tuple[str, str | None], None] = {}  # (a text, its field), as the masking asks for each

        def leaf(text: str, key_path: str, number: bool) -> str:
            key = document.once(("field", key_path), lambda: _field(key_path))
            if number and key is None:
                return text
            if key is None and kind == SlotKind.TOOL_RESULT and _json_document(text):
                return map_json_values(text, leaf, numbers, named)
            wanted.setdefault((text, key))
            return text

        def named(key: str, _inside: str, _others: str) -> str:
            wanted.setdefault((key, None))
            return key

        map_json_values(value, leaf, numbers, named)
        cached = self.engine.cached
        texts = list(
            dict.fromkeys(
                text
                for text, field in wanted
                if extra or not cached(text, kind, session, from_model, path, keep_surrogates, tool, field)
            )
        )
        deadline = time.monotonic() + DOCUMENT_READ_SECONDS
        return None if self._masker(session).read_ahead(texts, kind, deadline) else len(texts)

    # --- the local reviewer ----------------------------------------------------------------------

    def review_config(self, session: Session) -> LlmReview | None:
        """The reviewer's settings for this session's project, or None when it reads nothing there."""
        if self.reviewer is None:
            return None
        scope = self.engine.scope(session)
        if scope is not None and not scope.enabled:
            return None
        config = scope.review if scope is not None and scope.review is not None else self.llm_review
        return config if config is not None and config.mode() != "off" else None

    def _masker(self, session: Session) -> Masker:
        scope = self.engine.scope(session)
        return scope.masker if scope is not None else self.engine.masker

    def review_view(self, text: str, masker: Masker) -> View:
        """``text`` as the reviewer reads it: every surrogate in it as a typed placeholder."""
        return view_of(text, [(s.start, s.end, s.etype) for s in masker.surrogates_in(text)])

    def review_item(
        self,
        config: LlmReview,
        masker: Masker,
        index: int,
        kind: SlotKind,
        done: Masked,
        *,
        tool: str | None = None,
        path: str | None = None,
        json_mode: bool = False,
        from_model: bool = False,
        current: bool = True,
        image: ImageWork | None = None,
        left: Callable[[str], bool] | None = None,
    ) -> ReviewItem | None:
        """What the reviewer is to read of one masked text, never the model's own text: a text masked now, and one that
        is not (kept from an earlier request, or found in the engine's cache) when the reviewer holds no answer for it,
        since the request that brought it may have been stopped before the reviewer read it (its server off or slow,
        or a review that could not be trusted). ``current``: it belongs to this turn, so it may be reviewed inline.
        ``image``: the image the text was read in, shown to the reviewer with it when it sees images
        (``llm_review.images``).

        A text that is not masked now, whose answer the reviewer holds, is given that answer again unless it left as
        it is (``left``: ``Verifier.sent`` in its conversation): the request that first brought it may have been stopped
        for what the reviewer found there, a value masking could not place (a number of a JSON document), and sent
        again it must stop again, not leave as it was kept."""
        if from_model or done.withheld:
            return None
        if kind not in _REVIEWED and kind.value not in config.inline:
            return None
        inline = current and kind.value in config.inline
        if not inline and not config.background:
            return None
        reads_json = json_mode or kind == SlotKind.TOOL_RESULT
        for selected in (done.original, done.text):
            screen = declared_string_safe if reads_json and selected.lstrip()[:1] in ("{", "[") else safe_text
            if not screen(selected):
                return None
        text = readable(done.text) if reads_json else done.text
        if not text.strip() or not safe_text(text):
            return None
        work = image if config.images and kind == SlotKind.IMAGE_TEXT and image is not None else None
        key = work.key if work is not None else None
        known = self.reviewer.known(done.original, key) if self.reviewer is not None else None
        if not done.fresh and known is not None and (left is None or left(done.text)):
            return None  # reviewed when it was first sent, and it left so: what it found is masked already
        view = self.review_view(text, masker)
        item = ReviewItem(index, done.original, kind, tool, path, json_mode, view, inline, done.text, known)
        if work is not None:
            item.image = work
            if known is None and self.vision is not None:
                try:
                    url = self.vision.review_picture(work)
                except Exception as exc:  # the reviewer still reads the text; by its type: its words can quote it
                    log.error("could not prepare an image for the local reviewer: %s", type(exc).__name__)
                    url = None
                item.picture = Picture(work.key, url) if url else None
        return item

    def _review_items(self, session: Session, slots: list[SlotRef], done: list[Masked]) -> list[ReviewItem]:
        config = self.review_config(session)
        if config is None:
            return []
        masker = self._masker(session)
        # With no time limit, whatever was not reviewed before is reviewed before it leaves, history
        # included. With one, only this turn (what comes after the model's last words) waits for it,
        # and history not reviewed before goes to the background.
        last_model = (
            -1 if config.budget_ms is None else max((i for i, s in enumerate(slots) if s.from_model), default=-1)
        )
        items = []
        left = partial(self.verifier.sent, session.key)
        for i, (slot, masked) in enumerate(zip(slots, done, strict=True)):
            image = slot.container if isinstance(slot.container, ImageWork) else None
            item = self.review_item(
                config, masker, i, slot.kind, masked, tool=slot.tool, path=slot.source, json_mode=slot.mode == "json",
                from_model=slot.from_model, current=i > last_model, image=image, left=left,
            )  # fmt: skip
            if item is not None:
                items.append(item)
        return items

    async def review_inline(
        self, config: LlmReview, items: Sequence[ReviewItem], budget_s: float | None = None
    ) -> dict[int, Answer]:
        """Review the inline items not reviewed before, side by side, within the budget (event loop):
        ``llm_review.budget_ms`` when it is set, else ``budget_s`` (None: as long as it takes)."""
        assert self.reviewer is not None
        todo = [i for i in items if i.inline and i.answer is None]
        timeout = config.budget_ms / 1000 if config.budget_ms else budget_s
        found = await self.reviewer.review_all(
            [(i.view, i.original) for i in todo], timeout, config.guidance, [i.picture for i in todo]
        )
        return {item.index: answer for item, answer in zip(todo, found, strict=True)}

    def kept(self, config: LlmReview, masker: Masker, answer: Answer) -> list[Finding]:
        """The findings masked by themselves: of the categories set to, and not an exception or a fake."""
        vault = self.engine.vault
        return [
            f
            for f in answer.findings
            if f.category in config.auto_mask and not masker.allow.allows(f.value) and not vault.is_surrogate(f.value)
        ]

    def withhold_unseen(
        self, config: LlmReview, masker: Masker, item: ReviewItem, answer: Answer, now: bool = True
    ) -> bool:
        """An image in which the reviewer saw a value Latet could not find in its text is withheld:
        Latet cannot cover what it cannot place. It stays withheld when sent again (``now`` False:
        it left already, after a review in the background; from the next request on). True when it
        was withheld now."""
        work = item.image
        if work is None or work.state == WITHHELD or not answer.unlocated:
            return False
        lost = self.kept(config, masker, Answer(list(answer.unlocated)))
        if not lost:
            return False
        what = ", ".join(sorted({f.category for f in lost}))
        reason = f"the local reviewer saw a value in it ({what}) that Latet could not find in its text"
        if not now:
            work.service.remember(work.key, [], True, reason)
            return False
        work.decide([], True, reason)
        return True

    def remask(
        self,
        session: Session,
        item: ReviewItem,
        findings: Sequence[Finding],
        report: MaskReport,
        keep: bool,
        as_text: bool = False,
    ) -> Masked:
        """``item`` masked again with what the reviewer found in it; later turns send it the same way. ``as_text``: as
        ``mask_value`` takes it, the way the item was first masked."""
        return self.mask_value(
            item.original, item.kind, session, report, path=item.path, tool=item.tool, json_mode=item.json_mode,
            keep_surrogates=keep, extra=[f.extra() for f in findings], as_text=as_text,
        )  # fmt: skip

    def failure(self, config: LlmReview, kinds: Sequence[str], why: str, limit: str | None = None) -> str | None:
        """Why a request is stopped when its review failed (None: it is sent as masked). ``limit``: the time the review
        was given, in words, when it was not ``llm_review.budget_ms`` (an application's call)."""
        if config.on_failure != "stop":
            return None
        what = ", ".join(sorted({k.replace("_", " ") for k in kinds}))
        if why == "suspect":
            return (
                f"the local reviewer could not trust its review of the {what}: the text gives it instructions. It was "
                "not sent; look at it in the dashboard"
            )
        hint = {
            "timeout": f"it took longer than {limit or 'llm_review.budget_ms'}",
            "unreachable": "its server did not answer: start it, or run `latet doctor`",
            "unreadable": "its answer could not be read",
            "busy": "too much was waiting for it",
        }.get(why, why)
        return f"the local reviewer could not read the {what} ({hint}), so it was not sent"

    def learn(self, session: Session, config: LlmReview, item: ReviewItem, answer: Answer) -> None:
        """A background answer: what it found is masked wherever it appears from now on (in the engine
        thread). The block it was found in was sent already: it is sent the same way again once it is
        known to have left so, and masked anew otherwise (``_leaves_as_it_left``)."""
        if any(not safe_text(finding.value) for finding in chain(answer.findings, answer.unlocated)):
            return
        masker = self._masker(session)
        found = self.kept(config, masker, answer)
        for finding in found:
            etype, eclass = finding.extra().etype, finding.extra().eclass
            self.engine.vault.intern(etype, eclass, finding.value, rule=f"llm:{finding.category}")
        self.withhold_unseen(config, masker, item, answer, now=False)
        self._record(session, item, answer, found, inline=False)

    def _record(self, session: Session, item: ReviewItem, answer: Answer, masked: list[Finding], inline: bool) -> None:
        if self.on_review is None or not (answer.findings or answer.unlocated or answer.failed or answer.block):
            return
        try:
            self.on_review(
                ReviewRecord(session.key, session.project_id, item.kind.value, inline, item.sent, answer, masked)
            )
        except Exception as exc:  # the dashboard must never break the traffic it observes
            log.error("a review could not be recorded: %s", type(exc).__name__)  # its words can quote the text

    def submit_background(self, session: Session, config: LlmReview, items: Sequence[ReviewItem]) -> None:
        """Queue the items for a background review (on the event loop)."""
        if self.reviewer is None or not config.background:
            return
        loop = asyncio.get_running_loop()
        for item in items:

            def done(answer: Answer, item: ReviewItem = item) -> None:
                future = loop.run_in_executor(self.executor, self.learn, session, config, item, answer)
                future.add_done_callback(_log_failure)

            self.reviewer.submit(item.view, item.original, done, config.guidance, item.picture)

    async def close_retired(self) -> None:
        while self.retired:
            await self.retired.pop().aclose()

    async def review_prepared(self, prepared: Prepared) -> Prepared:
        """The local reviewer on a masked request: inline items before it leaves, the rest after."""
        await self.close_retired()
        session = prepared.session
        config = self.review_config(session) if session is not None else None
        if config is None or session is None or not prepared.reviews:
            return prepared
        answers = {i.index: i.answer for i in prepared.reviews if i.answer is not None}
        answers |= await self.review_inline(config, prepared.reviews)
        for item in prepared.reviews:
            item.answer = answers.get(item.index, item.answer)
        if answers:
            try:
                await self.run(self._apply_review, prepared, config, answers)
            except MaskingError as exc:  # what the reviewer found could not be masked (a vault at its limit): stopped
                log.error("masking failed: %s", exc)
                prepared.block_reason = f"masking failed: {exc}"
        if prepared.block_reason:
            return prepared
        later = [
            i
            for i in prepared.reviews
            if i.answer is None and (not i.inline or answers.get(i.index, Answer()).failed == "timeout")
        ]
        self.submit_background(session, config, later)
        return prepared

    def _apply_review(self, prepared: Prepared, config: LlmReview, answers: dict[int, Answer]) -> None:
        try:
            self._apply_review_changes(prepared, config, answers)
        except BaseException:
            prepared.refresh_recording(logical=True)
            raise
        prepared.refresh_recording(
            logical=bool(prepared.block_reason and prepared.amended),
            serialized=prepared.body if prepared.encoding is None else None,
        )

    def _apply_review_changes(self, prepared: Prepared, config: LlmReview, answers: dict[int, Answer]) -> None:
        session = prepared.session
        assert session is not None
        masker = self._masker(session)
        report = MaskReport()
        changed = lost = False  # lost: a value the reviewer found is still in clear once masked again
        stops: list[str] = []
        failed: dict[str, list[str]] = {}
        for item in prepared.reviews:
            answer = answers.get(item.index)
            if answer is None:
                continue
            if any(not safe_text(finding.value) for finding in chain(answer.findings, answer.unlocated)):
                prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
                return
            if answer.failed:
                report.degraded.append("llm-review")
                failed.setdefault(answer.failed, []).append(item.kind.value)
            found = self.kept(config, masker, answer)
            if found:
                done = self.remask(session, item, found, report, keep=False)
                lost = lost or still_in_clear(found, item.original, done.text, masker)
                slot = prepared.pending[item.index][0]
                if not (declared_string_safe(done.text) if slot.mode == "json" else safe_text(done.text)):
                    prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
                    return
                if done.text != slot.get():
                    slot.put(done.text, done.replacements)
                    changed = True
                prepared.pending[item.index] = (slot, done.replacements, done.exact)
                prepared.masked_count += len(found)
                item.sent = done.text
            if self.withhold_unseen(config, masker, item, answer):
                changed = True
            if answer.block and config.block:
                stops.append(item.kind.value.replace("_", " "))
            self._record(session, item, answer, found, inline=True)
        prepared.degraded = sorted({*prepared.degraded, *report.degraded})
        for why, kinds in failed.items():
            if self.live_config(session) is None:  # with the Live Reviewer on, you are shown it and decide
                prepared.block_reason = prepared.block_reason or self.failure(config, kinds, why)
        if stops:
            prepared.block_reason = (
                f"the local reviewer stopped this request: the {', '.join(sorted(set(stops)))} must not leave this "
                "machine even masked"
            )
        if changed:
            prepared.amended = True
            if isinstance(prepared.document, dict):
                prepared.body = self._serialize_body(
                    prepared.document, prepared.encoding, frame=prepared.frame_payload is not None
                )
        if lost and prepared.block_reason is None:  # never sent with a value it found in clear, as the Live Reviewer's
            raise MaskingError("a value the local reviewer found would have left in clear")
        self._check(prepared)  # what leaves now: a secret it found, in clear in a text it did not read, stops it

    # --- the Live Reviewer -----------------------------------------------------------------------

    def live_config(self, session: Session) -> LiveReview | None:
        """The Live Reviewer's settings in this session's project, or None when nothing waits for you."""
        scope = self.engine.scope(session)
        if scope is not None and not scope.enabled:
            return None
        config = scope.live if scope is not None and scope.live is not None else self.live_review
        return config if config is not None and config.enabled else None

    def _approved_key(self, session: Session, kind: SlotKind, text: str) -> bytes:
        message = "\x00".join((session.key, kind.value, text)).encode()
        return hmac.new(self.engine.vault.cache_key, message, hashlib.sha256).digest()

    def live_item(
        self,
        config: LiveReview,
        session: Session,
        index: int,
        kind: SlotKind,
        done: Masked,
        *,
        tool: str | None = None,
        path: str | None = None,
        json_mode: bool = False,
        from_model: bool = False,
    ) -> LiveItem | None:
        """A text to wait for you: one the model did not write, of the kinds you review, not approved
        before in this conversation."""
        if from_model or done.withheld or kind.value not in config.kinds or not done.original.strip():
            return None
        if self._approved_key(session, kind, done.original).hex() in session.approved:
            return None
        return LiveItem(index, done.original, kind, tool, path, json_mode, done.text)

    def with_answers(self, session: Session, items: Sequence[LiveItem], reviews: Sequence[ReviewItem]) -> None:
        """What the local reviewer said of each text, and what it masked in it (kept if masked again)."""
        config = self.review_config(session)
        answers = {r.index: r.answer for r in reviews if r.answer is not None}
        masker = self._masker(session)
        for item in items:
            item.answer = answers.get(item.index)
            if config is not None and item.answer is not None:
                item.extra = [f.extra() for f in self.kept(config, masker, item.answer)]

    def live_batch(
        self, session: Session, config: LiveReview, items: Sequence[LiveItem], agent: str, polled: bool
    ) -> Batch | None:
        """The batch to show you (engine thread); None when it may leave without asking you."""
        made_at = self._live_stamp()  # before its previews: what changes while they are made shows again
        views = [self._live_view(session, item) for item in items]
        if config.auto_approve_clean and not any(v["replacements"] or v["suggestions"] or v["failed"] for v in views):
            self._approve(session, items)
            return None
        batch = Batch(session.key, session.project_id, agent, views, polled=polled, timeout_s=config.timeout_s)
        batch.images = {n: item.image for n, item in enumerate(items) if item.image is not None}
        batch.made_at = made_at
        return batch

    def _live_view(self, session: Session, item: LiveItem) -> dict[str, Any]:
        """One text as the dashboard shows it: the original and the masked text, with the place of every
        value on both sides; what the reviewer found and did not mask, as suggestions."""
        masker = self._masker(session)
        vault = self.engine.vault
        text = item.sent
        found = []
        for span in masker.surrogates_in(text):
            entity = vault.by_surrogate(span.of(text))
            found.append(
                {
                    "masked_start": span.start,
                    "masked_end": span.end,
                    "type": entity.etype if entity is not None else span.etype,
                    "class": entity.eclass.name if entity is not None else "?",
                    "entity": entity.id if entity is not None else None,
                }
                | origin(entity.rule if entity is not None else None, None, span.etype)
            )
        original, replacements = reconstruct(text, found, vault)
        masked = {e.value for e in item.extra}
        suggestions: list[dict[str, Any]] = []
        for finding in item.answer.findings if item.answer is not None else []:
            if finding.value in masked or not finding.value:
                continue
            at = original.find(finding.value)
            while at >= 0:
                end = at + len(finding.value)
                if not any(r["start"] < end and at < r["end"] for r in replacements):
                    suggestions.append({"start": at, "end": end, "category": finding.category})
                at = original.find(finding.value, end)
        view = {
            "kind": item.kind.value,
            "path": item.path,
            "tool": item.tool,
            "original": original,
            "masked": text,
            "replacements": replacements,
            "suggestions": suggestions,
            "failed": item.answer.failed if item.answer is not None else None,
        }
        if item.image is not None and item.image.result is not None:
            view["image"] = item.image.view(replacements)
        return view

    def decide_images(self, items: Sequence[LiveItem], decision: Decision) -> bool:
        """What you did to the images (rectangles drawn, withheld): applied now, and kept for the same
        image sent again. True when an image changed."""
        changed = False
        for choice in decision.images:
            work = items[choice.item].image if 0 <= choice.item < len(items) else None
            if work is None:
                continue
            work.decide(list(choice.rects), choice.withhold)
            changed = True
        return changed

    def _approve(self, session: Session, items: Sequence[LiveItem], sent: Sequence[str] = ()) -> None:
        """The texts do not wait again in this conversation, whichever process serves it, for as long as they are
        among the ``APPROVED_KEPT`` it keeps; nor do they as Latet sent them (``item.sent``, or ``sent`` once masked
        again with your masks): Claude Code keeps the prompt it sent, with its stand-ins, and that copy comes back
        (the plugin's session.append), which a second approval of the first's stand-ins would only repeat."""
        texts = [(item.kind, item.original) for item in items]
        texts += [(item.kind, item.sent) for item in items if item.sent and item.sent != item.original]
        texts += [(item.kind, text) for item, text in zip(items, sent, strict=False) if text]
        keys = {self._approved_key(session, kind, text).hex() for kind, text in texts}
        if not keys <= session.approved:
            session.approved |= keys
            over = len(session.approved) - APPROVED_KEPT
            if over > 0:
                # Any of the others (a set keeps no order), and one approved now only when no other is left.
                session.approved.difference_update([*(session.approved - keys), *keys][:over])
            if session.kept:  # a session that is no conversation keeps what it approved in its own memory alone
                self.engine.conversations.update(session)

    def apply_live(self, session: Session, items: Sequence[LiveItem], decision: Decision, keep: bool) -> list[Masked]:
        """Your decision on approved texts (engine thread), masked again with the rules of now: a value you let through
        while they waited (Not sensitive: always allow) leaves in clear, and the values you masked are masked in them.
        The vault keeps those, so they are masked wherever they appear from now on, like the values the detectors and
        the local reviewer found."""
        if any(not safe_text(mask.value) for mask in decision.masks):
            raise MaskingError(PROTECTED_LAUNCH)
        self._approve(session, items)
        human: dict[str, ExtraValue] = {}
        for mask in decision.masks:
            etype, eclass = CATEGORY_TYPES[mask.category]
            # A class the final check enforces: the value never leaves in clear again.
            eclass = EntityClass.SECRET if eclass == EntityClass.SECRET else EntityClass.CUSTOM
            human.setdefault(mask.value, ExtraValue(mask.value, etype, eclass, f"human:{mask.category}"))
        report = MaskReport()
        out = []
        for item in items:
            done = self.mask_value(
                item.original, item.kind, session, report, path=item.path, tool=item.tool,
                json_mode=item.json_mode, keep_surrogates=keep, extra=[*item.extra, *human.values()],
            )  # fmt: skip
            for value in human:
                if _as_word(value, item.original) and _as_word(value, done.text):
                    # Never sent with a value you masked still in clear (the text is withheld instead).
                    raise MaskingError("a value masked in the Live Reviewer would have left in clear")
            out.append(done)
        if any(
            not (declared_string_safe(done.text) if item.json_mode else safe_text(done.text))
            for item, done in zip(items, out, strict=True)
        ):
            raise MaskingError(PROTECTED_LAUNCH)
        self._approve(session, items, [done.text for done in out])  # as sent, with your masks
        return out

    # --- the previews of what still waits ---------------------------------------------------------

    def hold_live(self, batch: Batch, session: Session, items: Sequence[LiveItem], *, keep: bool) -> None:
        """``batch`` waits, opened here (on the event loop): its previews are made again here when the rules or the
        vault change before you decide on it, until it no longer waits (``release_live``); at once if they changed since
        its previews were made."""
        made_at = batch.made_at or self._live_stamp()
        self._held_live[batch.id] = _HeldLive(session, list(items), keep, list(batch.items), made_at)
        if made_at != self._live_stamp():
            self._refresh_soon()

    def release_live(self, batch_id: str) -> None:
        self._held_live.pop(batch_id, None)

    def _live_stamp(self) -> tuple[int, int]:
        return self.engine.rules_version, self.engine.vault.version

    def live_changed(self) -> None:
        """The rules or the vault changed, from any thread (the vault tells it under its lock: this returns at once):
        the previews of the batches waiting here are made again on the event loop. Once asked, not again until the loop
        took it: a large output keeps thousands of values."""
        self._content_epoch.token = object()
        loop = self.loop
        if loop is None or not self._held_live or self._refresh_asked:
            return
        self._refresh_asked = True
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (shutdown)
            loop.call_soon_threadsafe(self._refresh_soon)

    def _refresh_soon(self) -> None:
        """On the event loop: a refresh started, or one more asked of the one running."""
        self._refresh_asked = False
        if self._refreshing is not None and not self._refreshing.done():
            self._refresh_again = True
            return
        self._refreshing = asyncio.get_running_loop().create_task(self.refresh_live(), name="latet-live-refresh")
        self._refreshing.add_done_callback(_refresh_ended)

    async def refresh_live(self) -> None:
        """The previews of the batches waiting here, made again where the rules or the vault changed since (in the
        engine's thread, one batch at a time: a request's masking goes between them), each stored and shown to every
        dashboard where it changed ("updated"); again while they keep changing. A batch decided meanwhile is left as it
        is (``Approvals.update``), and a preview the queue did not keep is made again at the next change."""
        while True:
            await asyncio.sleep(_REFRESH_AFTER)  # changes come together (a request keeps its values one by one)
            self._refresh_again = False
            for batch_id in list(self._held_live):
                renewed = await self.run(self._renewed_view, batch_id)
                held = self._held_live.get(batch_id)
                if renewed is None or held is None:
                    continue
                views, made_at = renewed
                if views is not None:
                    try:
                        if not await self.approvals.update(batch_id, views):
                            continue  # decided meanwhile, or not kept: as it was
                    except Exception as exc:  # said by its type alone: its message could quote a text
                        log.warning("the Live Reviewer could not show a request masked again (%s)", type(exc).__name__)
                        continue
                    held.views = views
                held.made_at = made_at
            if not self._refresh_again:
                return

    def _renewed_view(self, batch_id: str) -> tuple[list[dict[str, Any]] | None, tuple[int, int]] | None:
        """The previews of ``batch_id`` made again (engine thread): each text masked as approving it now masks it
        (``apply_live``), with what the local reviewer found; None for them when they did not change. None: no longer
        held here, or made as of now already. A change of the vault alone makes them again only where it reaches them
        (``_reached``)."""
        held = self._held_live.get(batch_id)
        made_at = self._live_stamp()  # before masking: what changes meanwhile shows at the next pass
        if held is None or held.made_at == made_at:
            return None
        try:
            if held.made_at[0] == made_at[0] and not self._reached(held):
                return None, made_at
            views = []
            for item in held.items:
                done = self.mask_value(
                    item.original, item.kind, held.session, MaskReport(), path=item.path, tool=item.tool,
                    json_mode=item.json_mode, keep_surrogates=held.keep, extra=list(item.extra),
                )  # fmt: skip
                views.append(self._live_view(held.session, replace(item, sent=done.text)))
        except Exception as exc:  # this batch keeps its previews; its decision masks it again all the same
            log.warning("the Live Reviewer could not mask a waiting request again (%s)", type(exc).__name__)
            return None, made_at
        return (views if views != held.views else None), made_at

    def _reached(self, held: _HeldLive) -> bool:
        """Whether the vault as it is now reaches the previews of ``held``: a value it keeps is in clear in one, or a
        fake in one is no longer the vault's for its value (forgotten, drawn anew)."""
        masker, vault = self._masker(held.session), self.engine.vault
        for view in held.views:
            masked = view["masked"]
            if masker.known_in(masked):
                return True
            for r in view["replacements"]:
                if r.get("entity") is not None:
                    entity = vault.get(r["entity"])
                    if entity is None or entity.surrogate != masked[r["masked_start"] : r["masked_end"]]:
                        return True
        return False

    def _live_items(self, prepared: Prepared, config: LiveReview) -> list[LiveItem]:
        """The texts of a request that wait for you: those after the model's last words (what came
        before was sent with an earlier request), not approved before."""
        session = prepared.session
        assert session is not None
        slots = [slot for slot, _, _ in prepared.pending]
        last_model = max((i for i, slot in enumerate(slots) if slot.from_model), default=-1)
        items = []
        for i, (slot, done) in enumerate(zip(slots, prepared.masked, strict=True)):
            if i <= last_model:
                continue
            item = self.live_item(
                config, session, i, slot.kind, done, tool=slot.tool, path=slot.source, json_mode=slot.mode == "json",
                from_model=slot.from_model,
            )  # fmt: skip
            if item is not None:
                item.sent = slot.get()
                item.image = slot.container if isinstance(slot.container, ImageWork) else None
                items.append(item)
        self.with_answers(session, items, prepared.reviews)
        return items

    async def live_prepared(
        self, prepared: Prepared, agent: str, gone: Callable[[], Awaitable[bool]] | None = None
    ) -> Prepared:
        """The Live Reviewer on a masked request: its new texts wait for you, then it leaves as you said."""
        session = prepared.session
        if session is None or prepared.mode != REWRITE or prepared.off or prepared.block_reason:
            return prepared
        config = self.live_config(session)
        if config is None or not prepared.masked:
            return prepared
        items = await self.run(self._live_items, prepared, config)
        if not items:
            return prepared
        batch = await self.run(self.live_batch, session, config, items, agent, False)
        if batch is None:
            return prepared
        try:
            decision = await self.await_decision(batch, gone, held=(session, items))
        except MaskingError as exc:  # a batch the queue does not keep (too heavy, too many): why, and what to do
            log.error("the Live Reviewer could not keep a request: %s", exc)
            prepared.block_reason = f"Latet did not send this request: {exc}"
            return prepared
        if not decision.approved:
            prepared.block_reason = f"Latet did not send this request: {decision.reason}"
            return prepared
        try:
            await self.run(self._apply_live_prepared, prepared, items, decision)
        except MaskingError as exc:
            log.error("the Live Reviewer's masks could not be applied: %s", exc)
            prepared.block_reason = f"Latet did not send this request: {exc}"
        return prepared

    async def await_decision(
        self,
        batch: Batch,
        gone: Callable[[], Awaitable[bool]] | None,
        held: tuple[Session, Sequence[LiveItem]] | None = None,
    ) -> Decision:
        """Wait for your decision on ``batch``; the client going away gives it up. With ``held`` (its session and
        texts), its previews are made again here while it waits (``hold_live``)."""
        await self.approvals.open(batch)
        if held is not None:
            self.hold_live(batch, held[0], held[1], keep=False)
        try:
            while True:
                decision = await self.approvals.wait(batch, 1.0 if gone is not None else None)
                if decision is not None:
                    return decision
                if gone is not None and await gone():
                    self.approvals.abandon(batch)
        finally:
            self.release_live(batch.id)

    def _apply_live_prepared(self, prepared: Prepared, items: Sequence[LiveItem], decision: Decision) -> None:
        try:
            self._apply_live_changes(prepared, items, decision)
        except BaseException:
            prepared.refresh_recording(logical=True)
            raise
        prepared.refresh_recording(
            logical=bool(prepared.block_reason and prepared.amended),
            serialized=prepared.body if prepared.encoding is None else None,
        )

    def _apply_live_changes(self, prepared: Prepared, items: Sequence[LiveItem], decision: Decision) -> None:
        session = prepared.session
        assert session is not None
        if any(not safe_text(mask.value) for mask in decision.masks):
            prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
            return
        done = self.apply_live(session, items, decision, keep=False)
        changed = self.decide_images(items, decision)
        for item, masked in zip(items, done, strict=True):
            slot, before, _ = prepared.pending[item.index]
            if masked.text != slot.get():
                slot.put(masked.text, masked.replacements)
                prepared.masked_count += max(len(masked.replacements) - len(before), 1)
                changed = True
            prepared.pending[item.index] = (slot, masked.replacements, masked.exact)
        if changed:
            prepared.amended = True
            if isinstance(prepared.document, dict):
                prepared.body = self._serialize_body(
                    prepared.document, prepared.encoding, frame=prepared.frame_payload is not None
                )
        self._check(prepared)  # what leaves now: a value you masked, in clear in a text that did not wait, stops it
        # Its answer restores what the conversation was shown: held again after the wait (its hold may have lapsed),
        # the handle ``Engine.reacquire`` returns owned by its lease from then on (released at once when the exchange
        # was given up meanwhile: RC-REC-11 (b)).
        if prepared.lease is not None:
            prepared.lease.reacquire()

    def read_image(self, ref: ImageRef, session: Session, modify: bool) -> ImageWork | None:
        """One image the Claude Code plugin sends: read, when Latet reads images in its project. On a Latet server whose
        text recognition is not available it is one whose text could not be read (withheld with ``modify``)."""
        scope = self.engine.scope(session)
        reads = scope.ocr if scope is not None else self.ocr
        if (scope is not None and not scope.enabled) or not reads or self.vision is None:
            return None
        if not self.vision.available:
            return self.vision.unread(ref, NOT_AVAILABLE, modify)
        return self.vision.work(ref, modify)

    def _read_images(self, scan: BodyScan, scope: Scope | None, modify: bool) -> list[ImageWork]:
        """Read the request's images, when Latet reads them here, and put each one's text among the
        slots, where the image is. ``modify``: an image that cannot be read is withheld. On a Latet server whose text
        recognition is not available (its models could not load), each image is one whose text could not be read: a
        request it rewrites leaves without it, one it only checks is stopped (``_not_read_here``). A computer sends
        them as ``images`` says, as it always did."""
        reads = scope.ocr if scope is not None else self.ocr
        if not reads or self.vision is None or not scan.images:
            return []
        if not self.vision.available:
            return [self.vision.unread(ref, NOT_AVAILABLE, modify) for ref in scan.images]
        works = self.vision.works(scan.images, modify, strip_metadata=modify)
        if modify:
            for work in works:
                work.strip_metadata = True
                if work.decoded is not None and work.state != WITHHELD:
                    work.masked("text", work.text, work.replacements)
        for at, _, work in sorted(((w.ref.at, i, w) for i, w in enumerate(works)), reverse=True):
            if work.slot is not None:
                scan.slots.insert(at, work.slot)
        return works

    @staticmethod
    def _unread(scan: BodyScan, works: list[ImageWork]) -> list[ImageRef]:
        """The request's images Latet did not read: none read at all, or each that failed (and could
        not be withheld, as in a request that is only checked)."""
        if not works:
            return list(scan.images)
        return [w.ref for w in works if w.state == UNREAD]

    def _not_read_here(self, scan: BodyScan, works: list[ImageWork], scope: Scope | None) -> str | None:
        """Why a request Latet only checks is stopped, whatever ``images`` says, on a computer as on a server: it
        carries an image whose text Latet reads there and could not read. A request it rewrites has such an image
        withheld; one it only checks would leave with it, unread (on a server: another identity's images holding the
        text recognition every space shares, a read's time budget, a server's pixels), and the final check its text.
        None where Latet reads no image (``ocr: off``, the models not installed): ``images`` says then, as it always
        did. Why an image could not be read is its own reason (a load that failed is said by its type), else that the
        text recognition is not available."""
        reads = scope.ocr if scope is not None else self.ocr
        if not reads or self.vision is None or not scan.images:
            return None
        why = next((work.reason or "it could not be read" for work in works if work.state == UNREAD), "")
        if not why and not self.vision.available:
            why = NOT_AVAILABLE
        if not why:
            return None
        return f"Latet sends no image it could not read, and this request carries one: {why}"

    def _refuses_unmasked(self, scope: Scope | None, unread: list[str]) -> str | None:
        """``images: mask``: a request that carries what Latet did not read and mask is not sent."""
        policy = scope.images if scope is not None else self.images
        if not unread or policy.send != "mask":
            return None
        return refusal(policy, unread)

    def _refuses(self, scope: Scope | None, unread: list[str]) -> str | None:
        """Why a request that carries what Latet does not read is not sent; None when it is."""
        policy = scope.images if scope is not None else self.images
        if not unread or not policy.blocks:
            return None
        return refusal(policy, unread)

    def unread_frame(self, what: str, session: Session) -> Prepared | None:
        """A client's WebSocket frame Latet does not read (``what`` it is): refused, with an error frame that says why,
        as a request body it cannot read is, on a computer as on a server."""
        return Prepared(PASSTHROUGH, None, b"", session, block_reason=_UNREAD_FRAME.format(what=what))

    def mask_ws_frame(
        self,
        text: str,
        session: Session,
        upstream: Upstream | None = None,
        path: str = "",
        query: str = "",
        launch: RequestLaunch | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, Prepared | None]:
        from latet.gateway.request_policy import LocalAuthority, session_facts

        authority = LocalAuthority.capture(self.engine, upstream)
        receipts: list[EchoCheck] = []
        prepared: Prepared | None = None
        complete = False
        selection = request_scope.get()
        if selection is not None:
            selection.project = selection.binding = selection.launch = selection.compared = None
        try:
            sent, prepared = self._mask_ws_frame(
                text,
                session,
                upstream,
                path,
                query,
                receipts=receipts,
                launch=launch,
                headers=headers or {},
            )
            if prepared is not None:
                prepared.protected_current = selection.protected_current if selection is not None else None
                if selection is not None:
                    prepared.project, prepared.binding, prepared.launch = (
                        selection.project,
                        selection.binding,
                        selection.launch,
                    )
                prepared.authority = replace(
                    authority,
                    session=session,
                    facts=session_facts(session),
                    off=prepared.off,
                )
                prepared.frame_payload = sent
                prepared.frame_verified_payload = sent
                prepared.frame_count = prepared.masked_count
                prepared.frame_compact = sent is not text
                edited = prepared.masked_count or any(work.state == WITHHELD for work in prepared.images)
                failed = prepared.block_reason and prepared.block_reason.startswith("masking failed:")
                prepared.refresh_recording(logical=bool(prepared.block_reason and (edited or failed)))
            complete = True
            return sent, prepared
        except BaseException:
            if prepared is not None and prepared.lease is not None:
                prepared.lease.release()
            raise
        finally:
            for check in receipts:
                if not complete or prepared is None or prepared.block_reason or prepared.opaque_check is not check:
                    check.close()

    def _mask_ws_frame(
        self,
        text: str,
        session: Session,
        upstream: Upstream | None = None,
        path: str = "",
        query: str = "",
        *,
        receipts: list[EchoCheck],
        launch: RequestLaunch | None = None,
        headers: dict[str, str],
    ) -> tuple[str, Prepared | None]:
        """A client WebSocket frame: a ``response.create`` is masked like a Responses request body (one that gives a key
        twice leaves written anew from what was read, as it does). Another frame is sent as it came on a computer, and
        refused on a server (``unread_frame``), but a frame the protocol defines with no content (``_contentless``)."""
        ledger = Ledger()
        try:
            limit = min(self.body_limit, sse.MAX_DECODED)
            if len(text) > limit or _utf8_size(text) > limit:
                raise codec.DecodedTooLarge(limit)
            ledger.preflight(text.encode())
            read = wellformed.read(text)
        except BodyRefused as error:
            return text, Prepared(
                PASSTHROUGH,
                None,
                b"",
                session,
                block_reason=str(error),
                envelope_refusal=error.safe_code,
            )
        except ValueError:
            return text, self.unread_frame("a text frame that is not JSON", session)
        frame = read.doc
        # One code path with HTTP: a turn that gives a key twice is read with its last copy and leaves written anew
        # from what was read (``changed`` below), so the provider never gets the copy Latet did not read. Any other
        # frame that does is refused (``_contentless`` takes no repeated frame).
        if not isinstance(frame, dict) or frame.get("type") != "response.create":
            if _contentless(frame, read.repeated) and not read.mended:
                from latet.gateway.body_coverage import cancel_fields

                prepared = Prepared(PASSTHROUGH, "ws-cancel", b"", session, document=frame)
                try:
                    checked = cancel_fields(frame, ledger)
                    if any(not safe_text(value) for value in checked):
                        return "", Prepared(VERIFY, "ws-cancel", b"", session, block_reason=PROTECTED_LAUNCH)
                    reason, unavailable = self._ws_launch_scope(session, headers, BodyScan(), launch)
                    if reason is not None:
                        return "", Prepared(
                            VERIFY, "ws-cancel", b"", session, block_reason=reason, protected_unavailable=unavailable
                        )
                    scope = self.engine.scope(session)
                    if scope is not None and not scope.enabled:
                        scope = replace(scope, enabled=True)  # controls retain the project's rules even when off
                    with self.engine.vault.holding():
                        result = self.verifier.verify(
                            (),
                            session.key,
                            scope=scope,
                            record=False,
                            checked_only=checked,
                        )
                    prepared.leaks = result.leaks
                    prepared.body_rules = tuple(sorted(result.rules))
                    prepared.body_authority = tuple(sorted(result.authority))
                    if result.blocking:
                        raise BodyRefused("control sensitive")
                    prepared.control_target = frame.get("response_id")
                    prepared.body_input = text
                    prepared.body_ledger = ledger
                    prepared.body_checked = checked
                    prepared.body_epoch = self._content_epoch
                    prepared.body_verified_token = self._content_epoch.token
                    prepared.body_structure = freeze_body(frame)
                    prepared.body_verified = True
                    prepared.body_verified_payload = prepared.body
                except BodyRefused as error:
                    prepared.block_reason = str(error)
                return text, prepared
            return text, self.unread_frame("a frame of another type", session)
        scan = openai_responses.scan_turn(frame)
        tools = BodyScan()
        for level in (frame, frame.get("response")):
            if isinstance(level, dict):
                _defined("responses", level, tools)
        source = upstream or Upstream("openai", "/openai", "https://api.openai.com")
        try:
            # A turn Latet rewrites: a live launch key in its texts is masked (R-103), screened again as it leaves.
            protected = not (
                ledger.declared_scan_safe(scan, masked=True)
                and ledger.declared_scan_safe(tools)
                and declared_body_safe(frame, "responses", source, scan, ledger)
            )
        except BodyRefused as error:
            return "", Prepared(
                VERIFY, "responses", b"", session, block_reason=str(error),
                envelope_refusal=error.safe_code,
            )  # fmt: skip
        if protected:
            return "", Prepared(VERIFY, "responses", b"", session, block_reason=PROTECTED_LAUNCH)
        if launch is None:
            self._attach_project(session, {}, scan)
        else:
            reason, unavailable = self._ws_launch_scope(session, headers, scan, launch)
            if reason is not None:
                return "", Prepared(
                    VERIFY, "responses", b"", session, block_reason=reason, protected_unavailable=unavailable,
                    off=reason == CLAIMED_OFF,
                )  # fmt: skip
        scope = self.engine.scope(session)
        unread = scan.unread()
        prepared = Prepared(
            REWRITE, "responses", b"", session, uninspected=unread, model=_model(frame), document=frame,
            recorded_later=True,  # mask_ws_frame refreshes what it returns
        )  # fmt: skip
        if scope is not None and not scope.enabled:  # D5: refused, as over HTTP
            prepared.off = True
            prepared.block_reason = OFF_THERE
            prepared.pending = [(s, [], False) for s in scan.slots]
            return text, prepared
        scan.first(tools)
        if scan.unreadable:
            prepared.discard_unadmitted_content(_unread_fields(scan.unreadable))
            return "", prepared
        if (refused := self._refuses(scope, unread)) is not None:
            prepared.block_reason = refused
            prepared.pending = [(s, [], False) for s in scan.slots]
            return text, prepared
        try:
            prepared.body_ledger = ledger
            prepared.body_input = text
            prepared.body_upstream = source
            prepared.body_scan = scan
            prepared.body_excepted = False  # a frame takes no request exception (F-2)
            prepared.body_epoch = self._content_epoch
            route = path or prepared.body_upstream.prefix + (
                "/responses" if prepared.body_upstream.name == "chatgpt" else "/v1/responses"
            )
            echo_scope = opaque_scope(prepared.body_upstream, "POST", route, "responses", query)
            if echo_scope is not None:
                echo_scope = EchoScope((*echo_scope.binding, "ws"), echo_scope.protocol, echo_scope.fallback)
                prepared.opaque_check = EchoCheck(
                    self.engine.vault.cache_key, self.echoes, echo_scope, session.key, frame
                )
                receipts.append(prepared.opaque_check)
            inspection = inspect_body(
                frame,
                "responses",
                prepared.body_upstream,
                scan,
                False,
                ledger,
                prepared.opaque_check.rules(frame) if prepared.opaque_check else {},
                excepted=False,
            )
            if any(not safe_text(value) for value in inspection.checked):
                return "", Prepared(VERIFY, "responses", b"", session, block_reason=PROTECTED_LAUNCH)
            prepared.body_shape = inspection.shape
            prepared.body_semantic = tuple(inspection.semantic)
            prepared.body_removed = _remove_declared(inspection)
        except BodyRefused as error:
            prepared.block_reason = str(error)
            if error.safe_code is not None:
                prepared.envelope_refusal = error.safe_code
            return text, prepared
        if scan.uninspected:
            prepared.block_reason = "Latet cannot read this document, audio or remote media, so it does not send it"
            return text, prepared
        reads = scope.ocr if scope is not None else self.ocr
        if scan.images and (not reads or self.vision is None or not self.vision.available):
            if reads and self.vision is not None:  # the text recognition down, counted once for the turn
                self.vision.refused_unavailable()
            prepared.block_reason = NO_OCR
            return text, prepared
        prepared.images = self._read_images(scan, scope, modify=True)
        if any(not safe_text(work.text) for work in prepared.images):
            prepared.discard_unadmitted_content(PROTECTED_LAUNCH)
            return "", prepared
        prepared.uninspected = scan.uninspected + ["image"] * len(self._unread(scan, prepared.images))
        if (refused := self._refuses_unmasked(scope, prepared.uninspected)) is not None:
            prepared.block_reason = refused
            prepared.pending = [(s, [], False) for s in scan.slots]
            return text, prepared
        report = MaskReport()
        try:
            masked_count, done_slots, prepared.lease = self._mask_slots(scan.slots, session, report, scan.keyed)
            prepared.masked_count = masked_count
            prepared.withheld = sum(done.withheld and done.fresh for done in done_slots)
            prepared.pending = [
                (s, done.replacements, done.exact) for s, done in zip(scan.slots, done_slots, strict=True)
            ]
            prepared.masked = done_slots
            prepared.degraded = report.degraded
            self._check(prepared)  # as a request over HTTP is, before anyone is asked about it
            if prepared.block_reason is None:
                prepared.reviews = self._review_items(session, scan.slots, done_slots)
        except MaskingError as exc:  # as a request over HTTP: never sent unmasked, and the agent is told why
            log.error("masking failed: %s", exc)
            prepared.block_reason = f"masking failed: {exc}"
            prepared.pending = [(s, [], False) for s in scan.slots]
            return text, prepared
        except BaseException:  # anything else once it held: no turn will own the hold, so it goes now
            if prepared.lease is not None:
                prepared.lease.release()
            raise
        if prepared.block_reason is not None:
            return text, prepared
        changed = (
            prepared.masked_count
            or read.mended
            or read.repeated
            or bool(prepared.images)
            or bool(prepared.body_removed)
        )
        try:
            return (self._serialize_body(frame, frame=True).decode() if changed else text), prepared
        except BaseException:
            if prepared.lease is not None:
                prepared.lease.release()
            raise

    # --- responses -------------------------------------------------------------------------------

    def restore_context(self, session: Session) -> RestoreContext:
        return RestoreContext(
            text_restores=self.engine.restores(session, json_mode=False),
            json_restores=self.engine.restores(session, json_mode=True),
            remember=self.engine.remember_model_output,
        )

    def response_transform(
        self, prepared: Prepared, content_type: str, content_encoding: str | None
    ) -> ResponseTransform | None:
        if prepared.mode != REWRITE or prepared.session is None or prepared.protocol in (None, "embeddings"):
            return None
        if not prepared.session.entity_ids:
            return None  # nothing was masked in this conversation: nothing to restore
        ctx = self.restore_context(prepared.session)
        protocol = prepared.protocol or ""
        if "text/event-stream" in content_type:
            named = self._on_response_id(prepared.session)
            return SSETransform(
                protocol, ctx, content_encoding, named, mediator=self, opaque=self.opaque_capture(prepared)
            )
        if "json" in content_type:
            return JSONTransform(protocol, ctx, content_encoding, mediator=self, opaque=self.opaque_capture(prepared))
        return None

    def _on_response_id(self, session: Session) -> Callable[[str], None]:
        name, key = self.engine.conversations.name, session.key

        def seen(response_id: str) -> None:
            name(response_id, key)

        return seen


# The most the event loop restores at once, decoded: an answer, an event of a stream or a WebSocket message larger than
# this is restored on the engine's thread, in turn with masking (``Mediator.run``). Restoring one as large as Latet
# reads takes seconds, and the event loop carries every other agent's streams (on a Latet server, every identity's); a
# little is restored where it is read, waiting behind no masking.
RESTORED_ON_LOOP = 64 * 1024
# Events of a stream restored on the event loop in one step at most: a piece of 64 KiB can end a thousand tiny ones,
# each read, restored and written again; past that many, the rest are restored in further steps, others served between.
RESTORED_EVENTS_AT_ONCE = 256
# One identity's answers restored on the event loop at once, at most (``Mediator.restoring``): its other answers wait
# their turn, so that its share of the loop is a step at a time whatever it sends (16 streams of tiny events held every
# other identity's requests seconds).
RESTORING_AT_ONCE = 1


class ResponseTransform:
    drops_encoding = True
    streams = True  # what it restores goes out as it comes (False: once the answer is whole)
    stopped: str | None = None  # a stream Latet stopped: why (an error's type); nothing goes out after

    def feed(self, chunk: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def pieces(self, chunk: bytes) -> Iterator[bytes]:
        """What goes out for ``chunk``, in pieces when it decodes to many."""
        yield self.feed(chunk)

    def end(self) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    async def restored(self, chunk: bytes) -> AsyncIterator[bytes]:
        """``pieces``, for the event loop: what is too large to restore there is restored on the engine's thread."""
        for out in self.pieces(chunk):
            yield out

    async def restored_end(self) -> bytes:
        """``end``, for the event loop, as ``restored``."""
        return self.end()

    def close(self) -> None:
        """Release terminal transport state. Whole-body transforms need no stream lifecycle action."""


# What ends a stream Latet stopped: the events it read before are passed on, then this, in the shape of an error of the
# client's SDK. And what answers an answer stopped whole.
_STOPPED_STREAM = "{}: the rest of it was not passed on"
STOPPED_ANSWER = "{}: it was not passed on"


@dataclass(slots=True)
class _RewrittenEvents:
    data: bytes
    remembered: list[tuple[str, str]]
    named: list[str] = field(default_factory=list)
    charge: int = -1  # detached from worker staging only when its owner accepts the validated batch
    json_charge: int = 0


def _json_work(value: Any) -> bool:
    """Whether an event carries structured tool work, including a mixed text/tool completion."""
    if isinstance(value, list):
        return any(_json_work(part) for part in value)
    if not isinstance(value, dict):
        return False
    if value.get("type") in (
        "tool_use",
        "input_json_delta",
        "function_call",
        "mcp_call",
        "shell_call",
        "local_shell_call",
    ):
        return True
    if str(value.get("type", "")).startswith("response.function_call_arguments."):
        return True
    return "arguments" in value or any(_json_work(part) for part in value.values())


def _detached(stream: Any) -> Any:
    """An empty terminal view. The engine's unfinished work retains exclusive ownership of the old dictionaries."""
    terminal = copy.copy(stream)
    terminal.ctx = copy.copy(stream.ctx)
    terminal.budget = copy.copy(stream.budget)
    terminal.budget.held = 0
    terminal.budget.reconstructed = 0
    terminal.ctx.budget = terminal.budget
    terminal.ctx.restored_ids = []
    terminal.ctx.remember = lambda masked, restored: None
    terminal.channels = {}
    if isinstance(stream, ResponsesStream):
        terminal.held = None
        terminal.held_charge = 0
    if isinstance(stream, ChatStream):
        terminal.last = None
        terminal.last_charge = 0
    return terminal


class _StreamWork:
    """Bounded results owned by one existing engine task until its awaiting owner accepts them."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.remember = stream.ctx.remember
        self.pending: list[tuple[str, str]] = []
        self.named: list[str] = []
        self.charge = 0
        self.json_charge = 0
        self.json_batch = False
        stream.ctx.remember = self.stage

    def stage(self, masked: str, restored: str) -> None:
        size = _utf8_size(masked) + _utf8_size(restored) + _PAIR_BYTES
        if self.charge + size > self.stream.budget.limit:
            raise too_large(self.stream.budget.limit)
        if self.stream.ctx._json_scope:
            self.stream.budget.keep(size)
            self.json_charge += size
        self.charge += size
        self.pending.append((masked, restored))

    def name(self, response_id: str) -> None:
        size = _utf8_size(response_id) + _NAME_BYTES
        if self.charge + size > self.stream.budget.limit:
            raise too_large(self.stream.budget.limit)
        self.stream.budget.keep(size)
        self.charge += size
        self.json_charge += size
        self.named.append(response_id)

    def clear(self) -> None:
        self.pending.clear()
        self.named.clear()
        self.stream.budget.release(self.json_charge)
        self.charge = 0
        self.json_charge = 0
        self.json_batch = False

    def close(self) -> None:
        self.clear()
        self.stream.close()
        self.stream.budget.held = 0
        self.stream.budget.reconstructed = 0
        self.stream.ctx.restored_ids.clear()


class SSETransform(ResponseTransform):
    """A stream restored event by event. One that does not decode, is larger once decoded than Latet reads, or holds an
    event that never ends, is stopped (``stopped``): the events read before are passed on, then an error in the shape
    the client's SDK reads, and nothing after."""

    def __init__(
        self,
        protocol: str,
        ctx: RestoreContext,
        content_encoding: str | None,
        on_response_id: Callable[[str], None],
        *,
        mediator: Mediator | None = None,
        opaque: OpaqueCapture | None = None,
    ) -> None:
        self.protocol = protocol
        self.opaque = opaque
        self.decoder = StreamDecoder(content_encoding)
        self.parser = SSEParser()
        self.on_response_id = on_response_id
        self.mediator = mediator  # its engine's thread restores events too large for the event loop
        self.stopped: str | None = None  # why it was stopped: an error's type
        if protocol == "anthropic":
            self.rewriter: Any = AnthropicStream(ctx)
        elif protocol == "chat":
            self.rewriter = ChatStream(ctx)
        elif protocol == "gemini":
            self.rewriter = GeminiStream(ctx)
        else:
            self.rewriter = ResponsesStream(ctx)
        self.ctx = ctx
        self._work = _StreamWork(self.rewriter)
        self._busy = False

    def _events(self, events: list) -> _RewrittenEvents:
        work = self._work
        self._busy = True
        try:
            data = self._rewrite_events(events, work)
            return _RewrittenEvents(data, work.pending, work.named)
        finally:
            work.stream.budget.release_reconstructed()
            if self.stopped is not None:
                work.close()
            self._busy = False

    def _commit(self, batch: _RewrittenEvents, work: _StreamWork | None = None) -> bytes:
        work = work or self._work
        if batch.charge < 0:
            self._accept(batch, work)
        try:
            for masked, restored in batch.remembered:
                work.remember(masked, restored)
            for response_id in batch.named:
                self.on_response_id(response_id)
            return batch.data
        finally:
            batch.remembered.clear()
            batch.named.clear()
            if self.stopped is None:
                work.stream.budget.release(batch.json_charge)
                work.stream.ctx.restored_ids.clear()
                work.json_batch = False

    def _accept(self, batch: _RewrittenEvents, work: _StreamWork) -> None:
        batch.charge = work.charge
        batch.json_charge = work.json_charge
        work.pending = []
        work.named = []
        work.charge = 0
        work.json_charge = 0

    def _encoded(
        self, work: _StreamWork, item: dict[str, Any], name: str | None, newline: bytes, already: int
    ) -> bytes:
        if work.json_batch:
            work.stream.budget.check(2 * already + 2 * _json_size(item) + 256)
        return serialize(name, dumps_compact(item), newline)

    def _rewrite_events(self, events: list, work: _StreamWork) -> bytes:
        out = bytearray()
        rewriter = work.stream
        work.json_batch = any(channel.json_mode for channel in rewriter.channels.values())
        for ev in events:
            if self.stopped is not None:
                return b""
            if ev.data == "[DONE]" and isinstance(rewriter, ChatStream):
                if work.json_batch:
                    rewriter.budget.check(2 * (len(out) + len(ev.raw)))
                extra = rewriter.on_done()
                if extra is not None:
                    out += self._encoded(work, extra, None, ev.newline, len(out))
                out += ev.raw
                continue
            obj = ev.json()
            if self.opaque is not None:
                self.opaque.event(obj)
            work.json_batch |= _json_work(obj)
            if work.json_batch:
                rewriter.budget.check(2 * (len(out) + len(ev.raw)))
            if not isinstance(obj, dict):
                out += ev.raw
                continue
            if self.protocol == "responses" and obj.get("type") == "response.created":
                rid = (obj.get("response") or {}).get("id")
                if isinstance(rid, str):
                    work.name(rid)
            if isinstance(rewriter, ChatStream):
                new = rewriter.on_chunk(obj)
                result = None if new is None else [new]
            else:
                result = rewriter.on_event(obj)
            if result is None:
                out += ev.raw
                continue
            for item in result:
                name = item.get("type") if ev.event is not None else None
                out += self._encoded(work, item, name, ev.newline, len(out))
        if work.json_batch:
            rewriter.budget.check(2 * len(out))
        return bytes(out)

    def _read(self, chunk: bytes) -> Iterator[list[SSEEvent] | bytes]:
        """For each piece ``chunk`` decodes to, the events it ends, to restore; the error that ends the stream when it
        is stopped (bytes, passed on as they are), and nothing once it was."""
        if self.stopped is not None:
            return
        try:
            for piece in self.decoder.pieces(chunk):
                yield self.parser.feed(piece)
                if self.stopped is not None:
                    return
        except ResponseError as exc:
            yield self._stop(exc)

    def _last(self) -> list[SSEEvent] | bytes:
        """The events the decoder's last bytes end, to restore; or, as bytes, the error that ends the stream when it is
        stopped (nothing once it was)."""
        if self.stopped is not None:
            return b""
        try:
            return self.parser.feed(self.decoder.flush())
        except ResponseError as exc:
            return self._stop(exc)

    async def _restore(self, events: list[SSEEvent]) -> bytes:
        """``events`` restored: on the engine's thread when they are too large to restore on the event loop (an event
        that a piece ends may have begun megabytes before); else on the loop, in this identity's turn
        (``Mediator.restoring``), the loop serving others before its next step."""
        if self.stopped is not None:
            return b""
        work = self._work
        try:
            if self.mediator is None:
                return self._commit(self._events(events), work)
            if sum(len(event.raw) for event in events) + self.rewriter.budget.held > RESTORED_ON_LOOP:
                self._busy = True  # ownership transfers before queuing, not after the worker starts
                batch = await self.mediator.run(self._events, events)
            else:
                async with self.mediator.restoring:
                    batch = self._events(events)
                    await asyncio.sleep(0)
            # Acceptance follows normal completion of validation/serialization. Provenance is model output,
            # not proof of transport delivery; its existing store calls stay on the engine's thread.
            if self.stopped is not None:
                work.close()
                return b""
            self._accept(batch, work)
            records = _Records(
                batch.remembered, batch.named, work.remember, self.on_response_id, batch.charge + _BATCH_BYTES
            )
            try:
                # No await between reservation/submission and output. Only detached records reach the executor.
                self.mediator.provenance.submit(records)
            finally:
                work.stream.budget.release(batch.json_charge)
                work.stream.ctx.restored_ids.clear()
                work.json_batch = False
            await self.mediator.commit_opaque(self.opaque, len(batch.data) + 64)
            return batch.data
        except ResponseError as exc:
            return self._stop(exc)

    def pieces(self, chunk: bytes) -> Iterator[bytes]:
        """What goes out for ``chunk``, a piece for each piece it decodes to; the error that ends the stream when it is
        stopped (and nothing once it was)."""
        for read in self._read(chunk):
            try:
                yield read if isinstance(read, bytes) else self._commit(self._events(read))
            except ResponseError as exc:
                yield self._stop(exc)

    async def restored(self, chunk: bytes) -> AsyncIterator[bytes]:
        """``pieces``, for the event loop: the events a piece ends restored ``RESTORED_EVENTS_AT_ONCE`` at a time, each
        step in this identity's turn."""
        for read in self._read(chunk):
            if isinstance(read, bytes):
                yield read
                continue
            if not read:
                yield b""  # a piece that ends no event (one of megabytes, being read): the loop's turn all the same
                continue
            for start in range(0, len(read), RESTORED_EVENTS_AT_ONCE):
                yield await self._restore(read[start : start + RESTORED_EVENTS_AT_ONCE])

    def feed(self, chunk: bytes) -> bytes:
        return b"".join(self.pieces(chunk))

    def end(self) -> bytes:
        read = self._last()
        try:
            return read if isinstance(read, bytes) else self._ended(self._commit(self._events(read)))
        except ResponseError as exc:
            return self._stop(exc)

    async def restored_end(self) -> bytes:
        read = self._last()
        if isinstance(read, bytes):
            return read
        steps = range(0, len(read), RESTORED_EVENTS_AT_ONCE)
        try:
            return self._ended(
                b"".join([await self._restore(read[start : start + RESTORED_EVENTS_AT_ONCE]) for start in steps])
            )
        except ResponseError as exc:
            return self._stop(exc)

    def _ended(self, out: bytes) -> bytes:
        """The stream's end, after ``out``: what the rewriter still holds, then what never ended, as it came."""
        out += b"".join(serialize(e.get("type"), dumps_compact(e)) for e in self.rewriter.on_end())
        return out + self.parser.rest()

    def close(self) -> None:
        self.stopped = self.stopped or "CancelledError"
        if self.opaque is not None:
            self.opaque.close()
        if self._busy:
            self.rewriter = _detached(self._work.stream)
            self.ctx = self.rewriter.ctx
            self._work = _StreamWork(self.rewriter)
        else:
            self._work.close()
        self.parser.rest()

    def _stop(self, error: ResponseError) -> bytes:
        """The event that ends a stream Latet stopped, in the shape the client's SDK reads as an error."""
        if self.stopped is not None:
            return b""
        self.stopped = type(error).__name__
        self.close()
        text = f"Latet: {_STOPPED_STREAM.format(error)}"
        if self.protocol == "anthropic":
            return serialize("error", dumps_compact({"type": "error", "error": {"type": "api_error", "message": text}}))
        if self.protocol == "chat":
            said = {"error": {"message": text, "type": "latet_error", "code": "latet_blocked"}}
            return serialize(None, dumps_compact(said))
        if self.protocol == "gemini":  # Google's shape, ``error`` its first key: its Python SDK knows an error by it
            said = {"error": {"code": 500, "message": text, "status": "INTERNAL"}}
            return serialize(None, dumps_compact(said))
        return serialize("error", dumps_compact({"type": "error", "code": "latet_blocked", "message": text}))


class JSONTransform(ResponseTransform):
    """An answer restored once it is whole: held decoded meanwhile, never more than Latet reads (``MAX_DECODED``),
    whatever its encoding. Past that, or when it does not decode, ``ResponseError``: nothing of it is passed on."""

    streams = False

    def __init__(
        self,
        protocol: str,
        ctx: RestoreContext,
        content_encoding: str | None,
        *,
        mediator: Mediator | None = None,
        opaque: OpaqueCapture | None = None,
    ) -> None:
        self.protocol = protocol
        self.opaque = opaque
        self.ctx = ctx
        self.decoder = StreamDecoder(content_encoding)
        self.mediator = mediator  # its engine's thread restores an answer too large for the event loop
        self.buf = bytearray()

    def _held(self, data: bytes) -> None:
        if len(self.buf) + len(data) > self.decoder.limit:
            raise too_large(self.decoder.limit)
        self.buf += data

    def pieces(self, chunk: bytes) -> Iterator[bytes]:
        """Nothing goes out before the answer is whole: what ``chunk`` decodes to is held, a piece at a time."""
        for piece in self.decoder.pieces(chunk):
            self._held(piece)
            yield b""

    def feed(self, chunk: bytes) -> bytes:
        for _ in self.pieces(chunk):
            pass
        return b""

    async def restored_end(self) -> bytes:
        if self.mediator is None or len(self.buf) <= RESTORED_ON_LOOP:
            result = self.end()
        else:
            result = await self.mediator.run(self.end)
        if self.mediator is not None:
            await self.mediator.commit_opaque(self.opaque, len(result) + 64)
        return result

    def close(self) -> None:
        if self.opaque is not None:
            self.opaque.close()

    def end(self) -> bytes:
        self._held(self.decoder.flush())
        try:
            body = wellformed.loads(self.buf)
        except ValueError:
            return bytes(self.buf)
        if self.opaque is not None:
            self.opaque.body(body)
        if self.protocol == "gemini" and isinstance(body, dict | list):  # an array: a stream not sent as events
            return dumps_compact(restore_gemini_answer(self.ctx, body)).encode()
        if not isinstance(body, dict):
            return bytes(self.buf)
        if self.protocol == "anthropic":
            body = restore_anthropic_message(self.ctx, body)
        elif self.protocol == "chat":
            body = restore_chat_completion(self.ctx, body)
        elif self.protocol == "responses":
            body = restore_response_body(self.ctx, body)
        return dumps_compact(body).encode()


@dataclass(slots=True, repr=False)
class _ResponseState:
    """One precharged socket state; no ID history and no authority after ambiguous overlap."""

    lock: Any = field(default_factory=threading.RLock)
    generation: int = 0
    phase: str = "idle"
    digest: bytes | None = None
    token: object | None = None
    closed: bool = False


class WSResponseRewriter:
    """Server → client frames of a Codex WebSocket session."""

    def __init__(self, mediator: Mediator, session: Session) -> None:
        self.mediator = mediator
        self.session = session
        self.stream: ResponsesStream | None = None
        self.version = (-1, -1)
        self.stopped = False
        self._busy = False
        self._deferred = False
        self._work: _StreamWork | None = None
        self._name = mediator._on_response_id(session)
        self.opaque: OpaqueCapture | None = None
        # None: never sent; False: stateless quarantine after reservation failure or closure.
        self._target: _ResponseState | bool | None = None

    def _clear_target(self) -> None:
        state = self._target
        self._target = False
        if isinstance(state, _ResponseState):
            with state.lock:
                if not state.closed:
                    state.closed = True
                    state.digest = state.token = None
                    self.mediator.echoes.release(ECHO_ENTRY_BYTES)

    def begin(self) -> None:
        """Called synchronously by S4 immediately before invoking a create send."""
        if self.stopped:
            raise ValueError("WebSocket response state closed")
        state = self._target
        if state is False:
            return
        if state is None:
            if not self.mediator.echoes.reserve(ECHO_ENTRY_BYTES):
                self._target = False  # prior untracked generations can never become explicit-ID authority
                return
            try:
                state = _ResponseState()
            except BaseException:
                self.mediator.echoes.release(ECHO_ENTRY_BYTES)
                raise
            self._target = state
            if self.stopped:
                self._clear_target()
                raise ValueError("WebSocket response state closed")
        assert isinstance(state, _ResponseState)
        with state.lock:
            if state.closed:
                raise ValueError("WebSocket response state closed")
            if state.generation == 2**63 - 1:
                raise ValueError("WebSocket generation limit")
            state.generation += 1
            state.digest = state.token = None
            state.phase = "awaiting" if state.phase in {"idle", "terminal"} else "disabled"

    def target_check(self, value: str) -> Callable[[], bool]:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        token = None
        state = self._target
        if isinstance(state, _ResponseState):
            with state.lock:
                if not state.closed and state.phase == "active" and state.digest == digest:
                    token = state.token

        def current_target() -> bool:
            found = self._target
            if not isinstance(found, _ResponseState):
                return False
            with found.lock:
                return (
                    not self.stopped
                    and not found.closed
                    and found.phase == "active"
                    and token is not None
                    and found.token is token
                )

        return current_target

    def _observe_target(self, event: dict[str, Any], generation: int) -> None:
        from latet.gateway.body_coverage import _ID

        state = self._target
        if not isinstance(state, _ResponseState):
            return
        with state.lock:
            if self.stopped or state.closed or generation != state.generation or state.phase == "disabled":
                return
            kind = event.get("type")
            if kind in {"response.completed", "response.done", "response.incomplete", "response.failed", "error"}:
                state.digest = state.token = None
                state.phase = "terminal"
            elif kind == "response.created":
                state.digest = state.token = None
                awaiting = state.phase == "awaiting"
                state.phase = "disabled"
                response = event.get("response")
                value = response.get("id") if isinstance(response, dict) else None
                if (
                    not awaiting
                    or generation == 0
                    or not isinstance(value, str)
                    or len(value) > 512
                    or not _ID.fullmatch(value)
                ):
                    return
                state.digest = hashlib.sha256(value.encode("utf-8")).digest()
                state.token = object()
                state.phase = "active"

    def _target_generation(self) -> int:
        state = self._target
        if not isinstance(state, _ResponseState):
            return 0
        with state.lock:
            return state.generation

    def observe(self, prepared: Prepared) -> None:
        if self.opaque is not None:
            self.opaque.close()
        self.opaque = self.mediator.opaque_capture(prepared)

    def _ctx(self) -> ResponsesStream:
        # Restores are rebuilt when the session learnt new entities (a new response.create).
        version = (len(self.session.entity_ids), self.mediator.engine.vault.version)
        if self.stream is None or version != self.version:
            self.stream = ResponsesStream(self.mediator.restore_context(self.session))
            self._work = _StreamWork(self.stream)
            self.version = version
        return self.stream

    async def restored(self, text: str) -> list[str]:
        """``on_frame``, for the event loop: a message too large to restore there is restored on the engine's thread."""
        if self.stopped:
            return []
        self._deferred = True
        try:
            held = self.stream.budget.held if self.stream is not None else 0
            # Character length is a lower bound: only bounded small strings need a byte-size allocation here.
            large = len(text) + held > RESTORED_ON_LOOP or len(text.encode("utf-8")) + held > RESTORED_ON_LOOP
            if large:
                self._busy = True
                out, held = await self.mediator.run(self._observed_frame, text)
                self._busy = False
            else:
                out, held = self._observed_frame(text)
            await self.mediator.commit_opaque(self.opaque, held)
            if self.stopped:
                return []
            work = self._work
            if work is None:
                return out
            batch = self._accept(work)
            records = _Records(batch.remembered, batch.named, work.remember, self._name, batch.charge + _BATCH_BYTES)
            try:
                self.mediator.provenance.submit(records)
            finally:
                work.stream.budget.release(batch.json_charge)
                work.stream.ctx.restored_ids.clear()
                work.json_batch = False
            return out
        except ResponseError:
            self.close()
            raise
        finally:
            self._deferred = False

    def _observed_frame(self, text: str) -> tuple[list[str], int]:
        out = self.on_frame(text)
        terminal = self.opaque is not None and self.opaque.ready and not self.opaque.closed
        held = sum(_utf8_size(part) + 64 for part in out) if terminal else 0
        return out, held

    def _accept(self, work: _StreamWork) -> _RewrittenEvents:
        batch = _RewrittenEvents(b"", work.pending, work.named, work.charge, work.json_charge)
        work.pending, work.named, work.charge, work.json_charge = [], [], 0, 0
        return batch

    def _commit(self, work: _StreamWork, batch: _RewrittenEvents) -> None:
        try:
            for masked, restored in batch.remembered:
                work.remember(masked, restored)
            for response_id in batch.named:
                self._name(response_id)
        finally:
            batch.remembered.clear()
            batch.named.clear()
            if not self.stopped:
                work.stream.budget.release(batch.json_charge)
                work.stream.ctx.restored_ids.clear()
                work.json_batch = False

    def close(self) -> None:
        self.stopped = True
        self._clear_target()
        if self.opaque is not None:
            self.opaque.close()
        if self._work is not None:
            if self._busy:
                self.stream = _detached(self._work.stream)
                self._work = _StreamWork(self.stream)
            else:
                self._work.close()

    def on_frame(self, text: str) -> list[str]:
        if self.stopped:
            return []
        generation = self._target_generation()
        try:
            event = wellformed.loads(text)
        except ValueError:
            return [text]
        if not isinstance(event, dict):
            return [text]
        self._observe_target(event, generation)
        if self.opaque is not None:
            self.opaque.event(event)
        if not self.session.entity_ids:
            return [text]
        etype = event.get("type", "")
        stream = self.stream if self.stream is not None and etype != "response.created" else self._ctx()
        work = self._work
        assert work is not None
        deferred = self._deferred
        self._busy = True
        try:
            work.json_batch = _json_work(event) or any(channel.json_mode for channel in stream.channels.values())
            if work.json_batch:
                stream.budget.check(2 * _utf8_size(text))
            if etype == "response.created":
                rid = (event.get("response") or {}).get("id")
                if isinstance(rid, str):
                    work.name(rid)
            result = stream.on_event(event)
            if etype in ("response.completed", "response.done", "response.incomplete", "error"):
                tail = stream.on_end()
                result = (result or [event]) + tail if tail else result
            out = []
            if result is None:
                out = [text]
            else:
                size = 0
                for item in result:
                    size += _json_size(item)
                    if work.json_batch:
                        stream.budget.check(2 * size)
                    out.append(dumps_compact(item))
            if self.stopped:
                return []
            if not deferred:
                self._commit(work, self._accept(work))
            return out
        finally:
            stream.budget.release_reconstructed()
            if self.stopped:
                work.close()
            self._busy = False
