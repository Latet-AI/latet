"""The dashboard's record of what left the machine: exchanges, the text blocks they carried
(stored once, however many turns resend them), the images as they were sent, and the response as
the model wrote it.

Only masked text is kept here, sealed with AES-GCM: originals are looked up in the vault when the
dashboard shows a block, so a value forgotten from the vault disappears from the history too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import zstandard
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from latet.engine.modules import kept, module_of
from latet.storage.db import settle
from latet.storage.folders import make_private_folder

if TYPE_CHECKING:
    from latet.storage.upgrade_copies import Follow

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA secure_delete=ON;
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,           -- keyed hash of the masked text
    kind TEXT NOT NULL,
    data BLOB NOT NULL,            -- sealed JSON [masked text, replacements]
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_seen ON blocks(last_seen);
CREATE TABLE IF NOT EXISTS exchanges (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    agent TEXT NOT NULL,
    flags TEXT NOT NULL,           -- " blocked leak uninspected:image " (padded, for LIKE)
    summary TEXT NOT NULL,         -- JSON ExchangeSummary: counts and metadata, no content
    refs BLOB,                     -- sealed JSON [[block id, path, from_model, new(, tool(, source))], ...]
    request_headers BLOB,          -- sealed JSON, credentials already redacted
    response BLOB,                 -- sealed JSON {headers, text, text_spans, tool_calls}
    leaks TEXT NOT NULL DEFAULT '[]',
    body_bytes INTEGER NOT NULL DEFAULT 0,
    project TEXT                   -- id of the session's project, when known
);
CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts);
CREATE INDEX IF NOT EXISTS exchanges_session ON exchanges(session, ts);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    project TEXT,
    block TEXT NOT NULL,           -- id of the block as it left (see blocks)
    kind TEXT NOT NULL,
    inline INTEGER NOT NULL,       -- 1: before the request left; 0: in the background
    summary TEXT NOT NULL          -- JSON: categories, what was masked, failed, block; never a value
);
CREATE INDEX IF NOT EXISTS reviews_ts ON reviews(ts);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,           -- keyed hash of the image as it was sent
    data BLOB NOT NULL,            -- sealed JSON {media_type, data (base64)}
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS images_seen ON images(last_seen);
"""
# What clearing the history deletes, and what its retention deletes past it (the table, and the column of its time):
# in this history, and in its upgrade copies (``upgrade_copies.Follow``, with these statements alone).
CLEARED = ("exchanges", "blocks", "reviews", "images")
PRUNED = (("exchanges", "ts"), ("blocks", "last_seen"), ("reviews", "ts"), ("images", "last_seen"))
MAX_IMAGE = 10 * 1024 * 1024  # larger images are listed, not kept
_CHUNK = 500  # SQLite host parameters per statement
_SUMMARIES_AT_ONCE = 1_000  # summaries read into objects at a time by ``summaries_parts``: what one part holds


@dataclass(slots=True)
class BlockRecord:
    id: str
    path: str
    kind: str
    from_model: bool
    new: bool  # first time this block left in this session
    masked: str | None = None  # None: already stored
    replacements: list[dict[str, Any]] = field(default_factory=list)
    tool: str | None = None  # the tool whose output a tool result is, when the request said
    # The file or the web address that output came from, when its call said, as the request carried it (masked), a web
    # address by its scheme and host alone (``recorder.kept_source``); and where values are masked in it, as for
    # ``replacements``: it is shown with them from the vault, so a value forgotten there is gone from it too.
    source: str | None = None
    source_found: list[dict[str, Any]] = field(default_factory=list)


def block_ref(block: BlockRecord) -> list[Any]:
    """A block as an exchange refers to it: id, path, from_model, new, and its tool and source when known (older
    references have four or five items). The source is its text, or its text and where values are masked in it
    (``[text, replacements]``)."""
    ref: list[Any] = [block.id, block.path, int(block.from_model), int(block.new)]
    if block.source:
        return [*ref, block.tool, [block.source, block.source_found] if block.source_found else block.source]
    return [*ref, block.tool] if block.tool else ref


def ref_parts(ref: Sequence[Any]) -> tuple[str, str, bool, bool, str | None, Any]:
    """A reference as ``block_ref`` wrote it: id, path, from_model, new, tool, source (None when not kept; its text, or
    its text and where values are masked in it, which ``recorder.restored_source`` reads)."""
    tool = ref[4] if len(ref) > 4 else None
    return ref[0], ref[1], bool(ref[2]), bool(ref[3]), tool, (ref[5] if len(ref) > 5 else None)


@dataclass(slots=True)
class ImageRecord:
    """An image of a request as it was sent: masked, clean (nothing to mask), or withheld (not sent)."""

    path: str
    state: str
    id: str | None = None  # None: not kept (withheld, or too large)
    media_type: str | None = None
    data: str | None = None  # base64, when not stored yet
    block: str | None = None  # the block of the text read in it


@dataclass(slots=True)
class ExchangeRecord:
    summary: dict[str, Any]
    blocks: list[BlockRecord] = field(default_factory=list)
    images: list[ImageRecord] = field(default_factory=list)
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    response: dict[str, Any] | None = None
    leaks: list[dict[str, Any]] = field(default_factory=list)
    body_bytes: int = 0
    # FULL history only: private source fields, sealed with the existing exchange refs.
    sensitive: dict[str, Any] | None = None
    destination_name: str | None = None  # trusted matched configured-route provenance, never raw summary inference
    session_source: str | None = None
    project_id: str | None = None  # actual registered Project provenance, never arbitrary summary inference
    history_code: int = 0  # private producer/snapshot handoff; summary claims never populate it


BUILTIN_DESTINATIONS = ("anthropic", "openai", "chatgpt", "openrouter", "gemini", "bedrock", "vertex")
_DESTINATION_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DESTINATION_DOMAIN = b"exchange-destination-index:v1"


def configured_destination(name: Any) -> bool:
    return (
        type(name) is str
        and 1 <= len(name) <= 63
        and name not in (*BUILTIN_DESTINATIONS, "backend-api", "_latet")
        and _DESTINATION_NAME.fullmatch(name) is not None
    )


def destination_index(key: bytes, name: Any, builtin: Any = None) -> str | None:
    """History-private configured-route equality only; never a decision/audit authority or public identifier."""
    if configured_destination(name):
        return "dest:v1:" + hmac.new(key, name.encode("ascii"), hashlib.sha256).hexdigest()
    return builtin if type(builtin) is str and builtin in BUILTIN_DESTINATIONS else None


def destination_batches(current_names: Sequence[str] | None) -> Iterator[list[str]]:
    seen: set[str] = set()
    batch: list[str] = []
    for name in BUILTIN_DESTINATIONS if current_names is None else current_names:
        if type(name) is not str or not (name in BUILTIN_DESTINATIONS or configured_destination(name)) or name in seen:
            continue
        seen.add(name)
        batch.append(name)
        if len(batch) == 64:
            yield batch
            batch = []
    if batch:
        yield batch


_HISTORY_CHUNK = 4096
_HISTORY_VERSION = (
    "CASE WHEN json_valid(summary) THEN CASE"
    " WHEN json_type(summary, '$.history_group_version') IS NULL THEN 0"
    " WHEN json_type(summary, '$.history_group_version') = 'integer'"
    " AND json_extract(summary, '$.history_group_version') = 1 THEN 1 ELSE -1 END ELSE -1 END"
)
_EXCHANGE_GROUP = "latet_exchange_group(rowid, " + _HISTORY_VERSION + ", 1)"
_EXCHANGE_VIEW = "latet_exchange_group(rowid, " + _HISTORY_VERSION + ", 0)"
_REVIEW_GROUP = "latet_review_group(rowid, " + _HISTORY_VERSION + ", 1)"
_REVIEW_VIEW = "latet_review_group(rowid, " + _HISTORY_VERSION + ", 0)"


def history_digest(key: bytes, source: str, prefix: bytes = b"") -> bytes:
    """Hash exact UTF8 incrementally, without another unbounded encoded source."""
    if type(source) is not str:
        raise TypeError("history source must be text")
    digest = hmac.new(key, prefix, hashlib.sha256)
    for start in range(0, len(source), 1024):
        digest.update(source[start : start + 1024].encode("utf-8"))
    return digest.digest()


def history_handle(kind: str, index: bytes, key: bytes) -> str:
    return "hg:" + kind + ":" + index.hex() + ":" + hmac.new(key, index, hashlib.sha256).hexdigest()


def history_handle_index(value: Any, kind: str, key: bytes) -> bytes | None:
    if type(value) is not str or len(value) != 135 or not value.startswith("hg:" + kind + ":") or value[70] != ":":
        return None
    if any(c not in "0123456789abcdef" for c in value[6:70] + value[71:]):
        return None
    index = bytes.fromhex(value[6:70])
    expected = hmac.new(key, index, hashlib.sha256).hexdigest()
    return index if hmac.compare_digest(value[71:], expected) else None


def valid_project_id(value: Any) -> str | None:
    return value if type(value) is str and len(value) == 12 and all(c in "0123456789abcdef" for c in value) else None


def history_project(value: Any) -> dict[str, str] | None:
    pid = valid_project_id(value.get("id") if type(value) is dict else value)
    return {"id": pid, "name": pid} if pid is not None else None


def refs_payload(refs: list[list[Any]], sensitive: dict[str, Any] | None) -> Any:
    """Full private detail uses the same sealed column, AAD and lifetime as its block references."""
    return refs if sensitive is None else {"version": 1, "blocks": refs, "sensitive": sensitive}


def read_refs(value: Any) -> tuple[list[list[Any]], dict[str, Any] | None]:
    """Read historical list references or the explicit full-detail envelope; unknown versions fail closed."""
    if value is None:
        return [], None
    if type(value) is list:
        return value, None
    if (
        type(value) is dict
        and type(value.get("version")) is int
        and value["version"] == 1
        and type(value.get("blocks")) is list
        and type(value.get("sensitive")) is dict
        and set(value) == {"version", "blocks", "sensitive"}
    ):
        return value["blocks"], value["sensitive"]
    raise ValueError("unsupported history references")


# Safe history projections are deliberately independent of mode/masked/verified claims.
# A historical row and a new row without a final coverage proof both have unknown coverage.
_HISTORY_FLAGS = frozenset(
    {
        "blocked",
        "leak",
        "unknown-route",
        "auto-mode-review",
        "project-off",
        "project-claimed",
        "withheld",
        "history",
        "route-bypass",
        "error",
        "outbound:web-search",
        "outbound:web-fetch",
        "outbound:remote-tool",
        "uninspected:image",
        "uninspected:audio",
        "uninspected:video",
        "uninspected:file",
        "uninspected:other",
        "image:masked",
        "image:withheld",
        "degraded:other",
    }
)
_HISTORY_AGENTS = ("claude-code", "codex", "opencode", "gemini-cli", "tutorial-agent", "other")
# What the gateway reads a request as (mediate.protocol_for) and how it treated it (mediate.REWRITE, VERIFY,
# PASSTHROUGH): an unknown mode claims nothing was protected, never more.
_HISTORY_PROTOCOLS = ("anthropic", "chat", "responses", "embeddings", "gemini")
_HISTORY_MODES = ("rewrite", "verify", "passthrough")
# The areas a request is shown by when its path is no route template below (logline.safe_request_path).
_HISTORY_AREAS = frozenset(
    {"/health", "/admin", "/dashboard", "/applications", "/bridge", "/docs", "/gateway", "/other"}
)
# The route templates the envelope admits a request by (request_profiles: a Profile's template, every part a fixed word
# or a named placeholder, never a value of the request) and the plugin's checks: what a record's path is shown as.
_HISTORY_ROUTES = frozenset(
    {
        "/{destination}/v1/messages",
        "/{destination}/v1/messages/count_tokens",
        "/{destination}/v1/chat/completions",
        "/{destination}/v1/completions",
        "/{destination}/v1/responses",
        "/{destination}/v1/embeddings",
        "/{destination}/v1/models",
        "/{destination}/v1/models/{model}",
        "/{destination}/chat/completions",
        "/{destination}/completions",
        "/{destination}/embeddings",
        "/{destination}/responses",
        "/{destination}/model/{model}/{operation}",
        "/{destination}/v1/projects/{project}/locations/{location}/publishers/anthropic/models/{model}:{operation}",
        "/{destination}/{version}/models",
        "/{destination}/{version}/models/{model}",
        "/{destination}/{version}/models/{model}:{operation}",
        "/{destination}/{version}/cachedContents",
        "/{destination}/{version}/cachedContents/{id}",
        "/{destination}/{version}/openai/{operation}",
        "/bedrock/{region}/model/{model}/{operation}",
        "/bedrock/{region}/inference-profiles",
        "/bedrock/{region}/inference-profiles/{id}",
        "/vertex/v1/projects/{project}/locations/{location}/publishers/anthropic/models/{model}:{operation}",
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/plugin/route-check",
        "/plugin/tool-call",
        "/plugin/final-check",
    }
)
# Where a request went (routes.Resolved.host: a configured destination's host, a cloud's by its enumerated region or
# location): a host name or an address, with its port; userinfo, a path or anything else is no host.
_HISTORY_HOST = re.compile(
    r"(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?"
)
_HISTORY_TYPES = frozenset(
    {
        "PERSON",
        "EMAIL",
        "PHONE",
        "IBAN",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "CRYPTO_WALLET",
        "VAT_NUMBER",
        "IT_FISCAL_CODE",
        "IT_VAT_CODE",
        "PASSWORD",
        "SECRET",
        "TOKEN",
        "USERNAME",
        "HOSTNAME",
        "ORG",
        "LOCATION",
        "ADDRESS",
        "CUSTOM",
        "STRUCT",
        "OTHER",
    }
)
_HISTORY_CLASSES = ("STRUCT", "NER", "LLM", "PII", "IDENTITY", "CUSTOM", "SECRET", "OTHER")
_HISTORY_MODULES = (
    "secrets",
    "secret-structure",
    "personal",
    "ids",
    "identity",
    "terms",
    "recognizers",
    "names",
    "reviewer",
    "live",
    "fields",
    "data",
    "other",
)
_MAX_HISTORY_COUNT = (1 << 63) - 1


def _history_code(value: Any, choices: Collection[str], default: Any = None) -> Any:
    return value if type(value) is str and value in choices else default


def _history_count(value: Any) -> int:
    return min(value, _MAX_HISTORY_COUNT) if type(value) is int and value >= 0 else 0


def _history_counts(value: Any, choices: Collection[str], other: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if type(value) is dict:
        for key, count in value.items():
            code = _history_code(key, choices, other)
            out[code] = min(_MAX_HISTORY_COUNT, out.get(code, 0) + _history_count(count))
    return out


def safe_flags(value: Any) -> list[str]:
    """Only finite categories enter clear indexes; a tool's source name remains private."""
    out: set[str] = set()
    if type(value) is list:
        for flag in value:
            if type(flag) is not str:
                continue
            if flag in _HISTORY_FLAGS:
                out.add(flag)
            elif flag.startswith("outbound:"):
                # Only exact known builtins carry a more specific category.
                code = {
                    "outbound:web_search": "outbound:web-search",
                    "outbound:WebSearch": "outbound:web-search",
                    "outbound:web_fetch": "outbound:web-fetch",
                    "outbound:WebFetch": "outbound:web-fetch",
                }.get(flag)
                out.add(code or "outbound:remote-tool")
            elif flag.startswith("uninspected:"):
                out.add("uninspected:other")
            elif flag.startswith("degraded:"):
                out.add("degraded:other")
    return sorted(out)


def _history_time(value: Any) -> int | float:
    if type(value) is int and -_MAX_HISTORY_COUNT <= value <= _MAX_HISTORY_COUNT:
        return value
    return (
        value
        if type(value) is float and math.isfinite(value) and -_MAX_HISTORY_COUNT <= value <= _MAX_HISTORY_COUNT
        else 0
    )


def _history_path(value: Any) -> str:
    """A route template or a plugin's check as it is; any other path by its area alone (an old row's raw path)."""
    from latet.logline import safe_request_path

    if type(value) is str and (value in _HISTORY_ROUTES or value in _HISTORY_AREAS):
        return value
    return safe_request_path(value)


@cache
def _history_types() -> frozenset[str]:
    """The entity types Latet's own detectors give values (``engine.build``: the reserved ones and those its detectors
    keep free), with the fixed categories above: categories, never values. An owner's own type (a recognizer's, a
    term's) is OTHER."""
    from latet.engine.build import detected_types, reserved_types

    return frozenset(_HISTORY_TYPES | reserved_types() | detected_types())


@cache
def _public_hosts() -> frozenset[str]:
    """The providers' public endpoints a request can go to: the built-in destinations' addresses, Bedrock's in each
    region it is enumerated for and Vertex AI's in each location."""
    from urllib.parse import urlsplit

    from latet.gateway.cloud import VERTEX_EXPRESS, vertex_host
    from latet.gateway.request_profiles import BEDROCK_REGIONS, VERTEX_LOCATIONS
    from latet.gateway.routes import DEFAULT_UPSTREAMS

    hosts = {urlsplit(upstream.base_url).netloc for upstream in (*DEFAULT_UPSTREAMS, VERTEX_EXPRESS)}
    hosts |= {
        f"{service}.{region}.amazonaws.com" for service in ("bedrock-runtime", "bedrock") for region in BEDROCK_REGIONS
    }
    hosts |= {vertex_host(location) for location in VERTEX_LOCATIONS}
    return frozenset(hosts)


def _history_host(value: Any) -> str | None:
    """A provider's public endpoint as it is; any other address (a destination of one's own) is ``custom``."""
    if type(value) is not str or not value:
        return None
    return value if value in _public_hosts() else "custom"


def _keyed_host(key: bytes, host: str) -> str:
    message = b"exchange-host:v1\0" + host.encode("utf-8", "backslashreplace")
    return "host:v1:" + hmac.new(key, message, hashlib.sha256).hexdigest()


def keyed_private(sensitive: dict[str, Any] | None, key: bytes) -> dict[str, Any] | None:
    """The full history's private view as a store seals it: a destination of one's own by its key there too, its name
    as the store's index keys it (``destination_index``) and an address no provider's public endpoint likewise."""
    if sensitive is None:
        return None
    out = dict(sensitive)
    if configured_destination(out.get("upstream")):
        out["upstream"] = destination_index(key, out["upstream"])
    host = out.get("host")
    if type(host) is str and host and host not in _public_hosts():
        out["host"] = _keyed_host(key, host)
    return out


def history_written(source: Any) -> bool:
    """A row this history wrote under its current contract (its grouping version), not one an older Latet wrote."""
    return (
        type(source) is dict
        and type(source.get("history_group_version")) is int
        and source["history_group_version"] == 1
    )


def _history_ms(value: Any) -> int | None:
    """A duration in milliseconds; None when it is not known (``overhead_ms`` of a request never sent)."""
    return min(value, _MAX_HISTORY_COUNT) if type(value) is int and value >= 0 else None


def _history_signed(value: Any) -> bool:
    """Whether Latet signed it, from the producer's signing kind (``aws-profile:<name>``, ``aws-session``): never the
    profile's name."""
    return value is True or (type(value) is str and (value == "aws-session" or value.startswith("aws-profile:")))


# A release number and, at most, a pre-release word with its number: never an opaque suffix (review Minor 4).
_AGENT_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}(?:-(?:alpha|beta|rc|preview|nightly)(?:\.[0-9]{1,6})?)?")


def _history_version(value: Any) -> str | None:
    """An agent's version as its user agent gave it, by a release number's grammar alone (``2.1.312``,
    ``0.162.0-alpha.3``): what `latet doctor` names a launcher by; None for anything else."""
    return value if type(value) is str and len(value) <= 48 and _AGENT_VERSION.fullmatch(value) else None


def _history_passes(value: Any) -> list[str]:
    """What a request was let through by, by the fixed categories alone (``request_policy.PASSES``), each once."""
    from latet.gateway.request_policy import PASSES

    if type(value) is not list:
        return []
    return sorted({item for item in value if type(item) is str and item in PASSES})


def _history_profile(value: Any) -> str | None:
    """A request profile's id (``anthropic.messages.1``, ``anthropic.count_tokens.1``, a WebSocket handshake's
    ``openai.responses.1.ws-envelope-1``): one of the ids the built-in profiles produce (``profile_ids``); None
    otherwise."""
    from latet.gateway.profile_ids import admitted

    return value if admitted(value, websocket=True) else None


def _history_removed(value: Any) -> dict[str, int]:
    """How many headers and body members Latet removed as their profile declares (D4): two counts."""
    value = value if type(value) is dict else {}
    return {key: _history_count(value.get(key)) for key in ("headers", "members")}


def _history_model(value: Any) -> str | None:
    """A model id a provider's documented grammar admits (``model_ids``, closed vocabularies and numbers), OpenRouter's
    vendor ids included; None for anything else (an owner-listed resource, an old row's free text)."""
    from latet.gateway.model_ids import BEDROCK_MODEL, CLAUDE, GEMINI, OPENAI, VERTEX_CLAUDE, openrouter_model

    if type(value) is not str or len(value) > 128:
        return None
    grammars = (CLAUDE, OPENAI, GEMINI, BEDROCK_MODEL, VERTEX_CLAUDE)
    admitted = any(grammar.fullmatch(value) is not None for grammar in grammars) or openrouter_model(value)
    return value if admitted else None


def safe_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """A safe list/summary, never proof reconstructed from a historical mode or count. A stopped request's
    ``block_reason`` is its category code (``outcome.BLOCK_CODES``): its words, an old row's included, stay in the
    full history's private view."""
    from latet.gateway.launch_authority import LAUNCH_CATEGORIES
    from latet.gateway.outcome import BLOCK_CODES

    out: dict[str, Any] = {key: _history_count(summary.get(key)) for key in ("masked", "new_masked", "restored")}
    out |= {key: _history_ms(summary.get(key)) for key in ("latency_ms", "overhead_ms")}
    # IDs are assigned by the history owner. Session/project grouping is supplied separately by its index adapter.
    out.update(
        {
            "id": summary.get("id"),
            "ts": _history_time(summary.get("ts")),
            "session": None,
            "project": history_project(summary.get("project")),
            "agent": _history_code(summary.get("agent"), _HISTORY_AGENTS, "other"),
            "agent_version": _history_version(summary.get("agent_version")),
            "upstream": _history_code(summary.get("upstream"), (*BUILTIN_DESTINATIONS, "custom", "unknown"), "unknown"),
            "host": _history_host(summary.get("host")),
            "signed": _history_signed(summary.get("signed")),
            "path": _history_path(summary.get("path")),
            "protocol": _history_code(summary.get("protocol"), _HISTORY_PROTOCOLS),
            "mode": _history_code(summary.get("mode"), _HISTORY_MODES, "passthrough"),
            "transport": _history_code(summary.get("transport"), ("http", "ws", "plugin"), "other"),
            "model": _history_model(summary.get("model")),
            "block_reason": _history_code(summary.get("block_reason"), BLOCK_CODES),
            # What its launch was decided as (P2-6a): a fixed category, never a key, a folder or a project.
            "launch": _history_code(summary.get("launch"), LAUNCH_CATEGORIES),
            # What it was let through by besides a complete reading (P2-7): built-in rules and exceptions by category,
            # its envelope's profile by id, and the counts of what its profile removed. Never a value.
            "passes": _history_passes(summary.get("passes")),
            "profile": _history_profile(summary.get("profile")),
            "removed": _history_removed(summary.get("removed")),
            "coverage": "unknown",
            # The egress check ran and left nothing in clear, as the recorder found it; coverage says what was read.
            "verified": summary.get("verified") is True,
            "status": summary.get("status")
            if type(summary.get("status")) is int and 100 <= summary["status"] <= 599
            else None,
            "flags": safe_flags(summary.get("flags")),
        }
    )
    types = _history_types()
    for key, choices, other in (
        ("by_type", types, "OTHER"),
        ("new_by_type", types, "OTHER"),
        ("by_class", _HISTORY_CLASSES, "OTHER"),
        ("by_module", _HISTORY_MODULES, "other"),
    ):
        out[key] = _history_counts(summary.get(key), choices, other)
    tokens = summary.get("tokens")
    out["tokens"] = (
        {key: _history_count(tokens.get(key)) for key in ("input", "output", "cache_read", "cache_write")}
        if type(tokens) is dict
        else None
    )
    if (
        type(summary.get("upstream")) is str
        and len(summary["upstream"]) == 72
        and summary["upstream"].startswith("dest:v1:")
    ):
        out["upstream"] = "custom"
    return out


_HISTORY_COVERAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_history_coverage_id(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 64 and _HISTORY_COVERAGE_ID.fullmatch(value) is not None


def history_coverage_payload(code: Any, exchange_id: Any) -> bytes | None:
    """Closed writer input from the private producer handoff, never a summary field."""
    if type(code) is not int or not valid_history_coverage_id(exchange_id):
        return None
    if code == 21:
        return b"\x01\x01\x01\x01"
    if code == 25:
        return b"\x01\x01\x02\x01"
    if code == 26:
        return b"\x01\x01\x02\x02"
    if code == 28:
        return b"\x01\x01\x03\x00"
    return None


def history_coverage_opened(payload: bytes) -> int:
    """Only the four authenticated version/source/outcome/coverage capsules have meaning."""
    if type(payload) is not bytes or len(payload) != 4:
        return 0
    if payload == b"\x01\x01\x01\x01":
        return 21
    if payload == b"\x01\x01\x02\x01":
        return 25
    if payload == b"\x01\x01\x02\x02":
        return 26
    return 28 if payload == b"\x01\x01\x03\x00" else 0


def history_coverage_summary(summary: dict[str, Any], code: Any, exchange_id: Any) -> dict[str, Any]:
    """Project a trusted carrier separately from conservative generic summary sanitization."""
    out = safe_summary(summary)
    if (
        history_coverage_payload(code, exchange_id) is not None
        and type(summary.get("id")) is str
        and summary["id"] == exchange_id
    ):
        if code == 21 or code == 25:
            out["coverage"] = "complete-accounting"
        elif code == 26:
            out["coverage"] = "legacy-uninspected"
    return out  # verified stays the recorded fact: authenticated coverage is a separate field


def safe_leaks(leaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only finite kind/type and boolean facts; raw previews/details live in full private detail."""
    kinds = (
        "system",
        "user_text",
        "assistant_text",
        "tool_input",
        "tool_result",
        "attachment",
        "tool_description",
        "outbound_tool",
        "image_text",
        "other",
    )
    return [
        {
            "type": _history_code(leak.get("type"), _history_types(), "OTHER"),
            "kind": _history_code(leak.get("kind"), kinds, "other"),
            "new": leak.get("new") is True,
            "allowed": leak.get("allowed") is True,
        }
        for leak in leaks
        if type(leak) is dict
    ]


def detail_leaks(
    sensitive: dict[str, Any] | None, legacy: list[dict[str, Any]], has_content: bool
) -> list[dict[str, Any]]:
    """Authorized FULL detail retains its sealed locations/previews; metadata never grows a private view."""
    if sensitive is not None:
        private = sensitive.get("leaks")
        return private if type(private) is list else safe_leaks(legacy)
    return legacy if has_content else safe_leaks(legacy)


def legacy_private(summary: dict[str, Any], has_content: bool) -> dict[str, Any] | None:
    """Only authorized old full content has a private view; no row or sealed bytes are rewritten."""
    fields = ("agent_version", "upstream", "host", "path", "model", "block_reason", "session", "project")
    if not has_content or not any(
        summary.get(key) is not None
        for key in (
            "agent_version",
            "host",
            "model",
            "block_reason",
            "session",
            "project",
        )
    ):
        return None
    private = {key: summary.get(key) for key in fields}
    private["outbound"] = [
        flag[len("outbound:") :]
        for flag in summary.get("flags", [])
        if type(flag) is str and flag.startswith("outbound:")
    ]
    return private


def new_exchange_id() -> str:
    return f"ex_{int(time.time() * 1000):x}{os.urandom(3).hex()}"


class ExchangeStore:
    def __init__(self, path: Path, root_key: bytes, retention_days: int = 14) -> None:
        make_private_folder(path.parent)
        self._aead = AESGCM(hmac.new(root_key, b"exchanges", hashlib.sha256).digest())
        self._id_key = hmac.new(root_key, b"exchange-ids", hashlib.sha256).digest()
        self._destination_key = hmac.new(root_key, _DESTINATION_DOMAIN, hashlib.sha256).digest()
        self._history_group_key = hmac.new(root_key, b"history-group-index:v1", hashlib.sha256).digest()
        self._history_handle_key = hmac.new(root_key, b"history-group-handle:v1", hashlib.sha256).digest()
        # zstandard's compressors and decompressors are not for two threads at once, and the recorder's thread and the
        # dashboard's workers use one store together: the compressor is used under the lock alone (``record``), and
        # each opening makes a decompressor of its own (``image`` opens outside the lock), which costs microseconds.
        self._zc = zstandard.ZstdCompressor(level=6)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            self._db.executescript(_SCHEMA)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(exchanges)")}
            if "project" not in columns:
                self._db.execute("ALTER TABLE exchanges ADD COLUMN project TEXT")  # a history from before projects
            if "images" not in columns:
                self._db.execute("ALTER TABLE exchanges ADD COLUMN images BLOB")  # sealed JSON [image, ...]
            self._db.execute("CREATE INDEX IF NOT EXISTS exchanges_project ON exchanges(project, ts)")
            self._cut_field_names()
            self._history_utf8 = self._db.execute("PRAGMA encoding").fetchone()[0] == "UTF-8"
            self._db.create_function(
                "latet_exchange_group",
                3,
                lambda row, version, strict: self._stored_group("exchanges", row, version, strict=strict),
            )
            self._db.create_function(
                "latet_review_group",
                3,
                lambda row, version, strict: self._stored_group("reviews", row, version, strict=strict),
            )
        except BaseException:  # not a history (a file damaged, or of something else): its file is not held open
            self._db.close()
            raise
        # Pruned by its owner once the retention is known (`make_runtime`): opening it prunes nothing.
        self.retention_days = retention_days
        # What clearing and pruning do to the upgrade copies' histories too (``upgrade_copies.Follow``): this
        # computer's history alone has one, given by ``state.local.local_backends``.
        self.copies: Follow | None = None

    def _cut_field_names(self) -> None:
        """What stopped a request is kept in clear (``leaks``): a rule an earlier Latet kept there with the name of the
        field a value sat in, which can be a value, names it by its kind from now on (``modules.kept``)."""
        named = (
            'SELECT id, leaks FROM exchanges WHERE leaks LIKE \'%"rule": "strict:%\' OR leaks LIKE \'%"rule": "field%\''
        )
        for exchange_id, raw in self._db.execute(named).fetchall():
            try:
                leaks = json.loads(raw)
            except ValueError:
                continue
            cut = [
                leak | {"rule": kept(leak["rule"])} if isinstance(leak, dict) and leak.get("rule") else leak
                for leak in leaks
            ]
            if cut != leaks:
                self._db.execute("UPDATE exchanges SET leaks = ? WHERE id = ?", (json.dumps(cut), exchange_id))

    # --- sealing ----------------------------------------------------------------------------------

    def _seal(self, value: Any, aad: str) -> bytes:
        nonce = os.urandom(12)
        data = self._zc.compress(json.dumps(value, ensure_ascii=False).encode())
        return nonce + self._aead.encrypt(nonce, data, aad.encode())

    def _open(self, sealed: bytes | None, aad: str) -> Any:
        if sealed is None:
            return None
        opened = self._aead.decrypt(sealed[:12], sealed[12:], aad.encode())
        return json.loads(zstandard.ZstdDecompressor().decompress(opened))

    def block_id(self, masked: str) -> str:
        return hmac.new(self._id_key, masked.encode(), hashlib.sha256).hexdigest()[:32]

    def image_id(self, data: str) -> str:
        return "im_" + hmac.new(self._id_key, b"image\0" + data.encode(), hashlib.sha256).hexdigest()[:32]

    def history_group(self, source: str) -> str:
        """An authenticated private history equality handle, never an engine key or send authority."""
        return history_handle("l1", history_digest(self._history_group_key, source), self._history_handle_key)

    def _session_selector(self, value: str) -> str:
        return (
            value
            if history_handle_index(value, "l1", self._history_handle_key) is not None
            else self.history_group(value)
        )

    def _stored_group(self, table: str, rowid: int, version: int, *, strict: int = 1) -> str | None:
        """ONE canonical function for legacy groups/selectors/projections, using bounded readonly TEXT reads."""
        if type(strict) is not int or strict not in (0, 1):
            raise ValueError("history grouping unavailable")
        try:
            if not self._history_utf8 or version not in (0, 1):
                raise ValueError("history grouping unavailable")
            queries = {
                "exchanges": "SELECT typeof(session) FROM exchanges WHERE rowid = ?",
                "reviews": "SELECT typeof(session) FROM reviews WHERE rowid = ?",
            }
            with self._lock:
                row = self._db.execute(queries[table], (rowid,)).fetchone()
                if row is None or row[0] != "text":
                    raise ValueError("history grouping unavailable")
                with self._db.blobopen(table, "session", rowid, readonly=True) as source:
                    if version == 1:
                        if len(source) != 135:
                            raise ValueError("history grouping unavailable")
                        handle = source.read(135).decode("ascii")
                        if history_handle_index(handle, "l1", self._history_handle_key) is None:
                            raise ValueError("history grouping unavailable")
                        return handle
                    digest = hmac.new(self._history_group_key, digestmod=hashlib.sha256)
                    while chunk := source.read(_HISTORY_CHUNK):
                        digest.update(chunk)
                    return history_handle("l1", digest.digest(), self._history_handle_key)
        except (KeyError, ValueError, UnicodeError, sqlite3.Error):
            if strict:
                raise ValueError("history grouping unavailable") from None
            return None

    def _history_coverage(self, source: dict[str, Any], exchange_id: Any) -> int:
        if (
            not valid_history_coverage_id(exchange_id)
            or type(source.get("id")) is not str
            or source["id"] != exchange_id
        ):
            return 0
        capsule = source.get("_history_coverage")
        # Bound every decode/allocation before decoding or asking the AEAD to authenticate.
        if (
            type(capsule) is not str
            or len(capsule) != 70
            or not capsule.startswith("hc:l1:")
            or any(c not in "0123456789abcdef" for c in capsule[6:])
        ):
            return 0
        try:
            sealed = bytes.fromhex(capsule[6:])
            payload = self._aead.decrypt(
                sealed[:12],
                sealed[12:],
                b"latet:history-coverage:v1\0local\0" + exchange_id.encode("ascii"),
            )
        except (InvalidTag, ValueError):
            return 0
        return history_coverage_opened(payload)

    def _history_summary(
        self,
        raw: str,
        group: str | None,
        exchange_id: str | None = None,
        *,
        review: bool = False,
    ) -> dict[str, Any]:
        # The handle and physical ID belong to the SAME SELECT snapshot as raw, never a later rowid lookup.
        source = json.loads(raw)
        summary = (
            dict(source)
            if review
            else history_coverage_summary(
                source,
                self._history_coverage(source, exchange_id),
                exchange_id,
            )
        )
        if not review and not history_written(source):
            summary["verified"] = False  # an older Latet's claim, not this record's fact (coverage unknown too)
        summary["session"] = group
        project = history_project(source.get("project"))
        summary["project"] = (project["id"] if project is not None else None) if review else project
        return summary

    # --- writes -----------------------------------------------------------------------------------

    def known_blocks(self, ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(ids))
        found: set[str] = set()
        with self._lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                found.update(r[0] for r in self._db.execute(f"SELECT id FROM blocks WHERE id IN ({marks})", part))  # noqa: S608
        return found

    def last_used(self, current_names: Sequence[str] | None = None) -> dict[str, float]:
        """Last use of explicit current names (default: seven builtins), <=64 SQL aggregates per batch."""
        out: dict[str, float] = {}
        with self._lock:
            for batch in destination_batches(current_names):
                wanted = [
                    (key, slot)
                    for slot, name in enumerate(batch)
                    for key in dict.fromkeys((destination_index(self._destination_key, name, name), name))
                    if key is not None
                ]
                values = ",".join("(?,?)" for _ in wanted)
                query = (
                    "WITH wanted(key,slot) AS (VALUES " + values + ")"  # noqa: S608 - placeholders only; values are bound
                    " SELECT w.slot, MAX(e.ts) FROM exchanges e JOIN wanted w"
                    " ON json_extract(e.summary, '$.upstream') = w.key GROUP BY w.slot"
                )
                rows = self._db.execute(query, [value for pair in wanted for value in pair]).fetchall()
                for slot, timestamp in rows:
                    out[batch[slot]] = float(timestamp)
        return out

    def destinations(self, ids: Iterable[str], current_names: Sequence[str]) -> dict[str, str]:
        """Which of these exchanges went to which of the history owner's current configured destinations, by the
        keyed index the store keeps of each (the summary shows ``custom``): for the owner's own dashboard alone."""
        keys = {destination_index(self._destination_key, name): name for name in current_names}
        keys.pop(None, None)
        ids = [i for i in dict.fromkeys(ids) if type(i) is str]
        out: dict[str, str] = {}
        if not keys:
            return out
        with self._lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                query = f"SELECT id, json_extract(summary, '$.upstream') FROM exchanges WHERE id IN ({marks})"  # noqa: S608
                for exchange_id, key in self._db.execute(query, part):
                    if key in keys:
                        out[exchange_id] = keys[key]
        return out

    def block_types(self, ids: Iterable[str]) -> dict[str, list[tuple[str, str, str, int]]]:
        """Values masked in stored blocks, as (type, class, module, count)."""
        ids = list(dict.fromkeys(ids))
        out: dict[str, list[tuple[str, str, str, int]]] = {}
        with self._lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                for block_id, data in self._db.execute(f"SELECT id, data FROM blocks WHERE id IN ({marks})", part):  # noqa: S608
                    replacements = self._open(data, block_id)[1]
                    counts = Counter(
                        (r["type"], r.get("class", "?"), r.get("module") or module_of(r.get("rule"), r["type"]))
                        for r in replacements
                    )
                    out[block_id] = sorted((t, c, m, n) for (t, c, m), n in counts.items())
        return out

    def record(self, rec: ExchangeRecord) -> str:
        summary = rec.summary
        exchange_id = summary.setdefault("id", new_exchange_id())
        now = time.time()
        refs = refs_payload([block_ref(b) for b in rec.blocks], keyed_private(rec.sensitive, self._destination_key))
        stored = safe_summary(summary)
        payload = history_coverage_payload(rec.history_code, exchange_id)
        if payload is not None:
            nonce = os.urandom(12)
            aad = b"latet:history-coverage:v1\0local\0" + exchange_id.encode("ascii")
            stored["_history_coverage"] = "hc:l1:" + (nonce + self._aead.encrypt(nonce, payload, aad)).hex()
        source = rec.session_source if rec.session_source is not None else summary["session"]
        stored["session"] = self.history_group(source)
        stored["history_group_version"] = 1
        stored["project"] = history_project(rec.project_id)
        stored["upstream"] = destination_index(self._destination_key, rec.destination_name, summary.get("upstream"))
        images = [{"id": i.id, "path": i.path, "state": i.state, "block": i.block} for i in rec.images]
        flags = " " + " ".join(safe_flags(summary.get("flags"))) + " "
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for b in rec.blocks:
                    if b.masked is not None:
                        self._db.execute(
                            "INSERT OR IGNORE INTO blocks VALUES (?, ?, ?, ?)",
                            (b.id, b.kind, self._seal([b.masked, b.replacements], b.id), now),
                        )
                for image in rec.images:
                    if image.id is None:
                        continue
                    if image.data is not None:
                        sealed = self._seal({"media_type": image.media_type, "data": image.data}, image.id)
                        self._db.execute("INSERT OR IGNORE INTO images VALUES (?, ?, ?)", (image.id, sealed, now))
                    self._db.execute("UPDATE images SET last_seen = ? WHERE id = ?", (now, image.id))
                ids = list(dict.fromkeys(b.id for b in rec.blocks))
                for i in range(0, len(ids), _CHUNK):
                    part = ids[i : i + _CHUNK]
                    marks = ",".join("?" * len(part))
                    self._db.execute(f"UPDATE blocks SET last_seen = ? WHERE id IN ({marks})", [now, *part])  # noqa: S608
                self._db.execute(
                    "INSERT OR REPLACE INTO exchanges (id, ts, session, agent, flags, summary, refs, request_headers,"
                    " response, leaks, body_bytes, project, images) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        exchange_id,
                        summary["ts"],
                        stored["session"],
                        stored["agent"],
                        flags,
                        json.dumps(stored),
                        self._seal(refs, f"{exchange_id}:refs") if refs else None,
                        self._seal(rec.request_headers, f"{exchange_id}:headers") if rec.request_headers else None,
                        self._seal(rec.response, f"{exchange_id}:response") if rec.response is not None else None,
                        json.dumps(safe_leaks(rec.leaks)),
                        rec.body_bytes,
                        (stored["project"] or {}).get("id"),
                        self._seal(images, f"{exchange_id}:images") if images else None,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return exchange_id

    def record_review(
        self,
        summary: dict[str, Any],
        *,
        session_source: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Keep a history-only copy; explicit source is raw even if it looks like a generated handle."""
        source = session_source if session_source is not None else summary["session"]
        stored = dict(summary)
        stored["session"] = self.history_group(source)
        stored["project"] = valid_project_id(project_id)
        stored["history_group_version"] = 1
        review_id = f"rv_{int(time.time() * 1000):x}{os.urandom(3).hex()}"
        with self._lock:
            self._db.execute(
                "INSERT INTO reviews (id, ts, session, project, block, kind, inline, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    stored["ts"],
                    stored["session"],
                    stored["project"],
                    stored["block"],
                    stored["kind"],
                    int(bool(stored.get("inline"))),
                    json.dumps(stored),
                ),
            )
        return review_id

    def reviews(self, limit: int = 100, session: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, summary, " + _REVIEW_VIEW + " FROM reviews"  # noqa: S608 - Only module-constant canonical SQL; user values stay bound.
        args: list[Any] = []
        if session:
            query += " WHERE " + _REVIEW_GROUP + " = ?"
            args.append(self._session_selector(session))
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
            return [{"id": rid, **self._history_summary(data, group, review=True)} for rid, data, group in rows]

    def prune(self, now: float | None = None) -> int:
        """Drop exchanges older than the retention period, and blocks no newer exchange carried: here, and in every copy
        `latet upgrade` took of this history (``copies``)."""
        cutoff = (now or time.time()) - self.retention_days * 86400
        with self._lock:
            removed = {
                table: self._db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount  # noqa: S608
                for table, column in PRUNED
            }["exchanges"]
            settle(self._db)  # what was deleted, overwritten, into the file; never fails the prune (at every start)
        if self.copies is not None:
            self.copies.prune(self.retention_days, now)
        return removed

    def clear(self) -> None:
        """Every exchange, block, review and image gone: here, and in every copy `latet upgrade` took (``copies``)."""
        with self._lock:
            for table in CLEARED:
                self._db.execute(f"DELETE FROM {table}")  # noqa: S608
            settle(self._db, vacuum=True)  # the file made anew, the journal into it; never fails the Clear
        if self.copies is not None:
            self.copies.clear()

    # --- reads ------------------------------------------------------------------------------------

    def list(
        self,
        limit: int = 100,
        before: str | None = None,
        session: str | None = None,
        agent: str | None = None,
        flag: str | None = None,
        project: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        where: list[str] = []
        args: list[Any] = []
        with self._lock:
            if before:
                row = self._db.execute("SELECT ts FROM exchanges WHERE id = ?", (before,)).fetchone()
                if row:
                    where.append("(ts < ? OR (ts = ? AND id < ?))")
                    args += [row[0], row[0], before]
            if session:
                where.append(_EXCHANGE_GROUP + " = ?")
                args.append(self._session_selector(session))
            if agent:
                where.append("agent = ?")
                args.append(agent)
            if flag:
                where.append("(flags LIKE ? OR flags LIKE ?)")
                args += [f"% {flag} %", f"% {flag}:%"]
            if project:
                where.append("project = ?")
                args.append(project)
            query = "SELECT id, summary, " + _EXCHANGE_VIEW + " FROM exchanges"  # noqa: S608 - Only module-constant canonical SQL; user values stay bound.
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " ORDER BY ts DESC, id DESC LIMIT ?"
            rows = self._db.execute(query, [*args, limit + 1]).fetchall()
            items = [self._history_summary(data, group, eid) for eid, data, group in rows]
        next_id = items[limit - 1]["id"] if len(items) > limit else None
        return items[:limit], next_id

    def in_order(self, session: str) -> list[str]:
        """The ids of one conversation's exchanges, in the order they were recorded. An agent sends a conversation's
        requests one after the other, so this is the order they were sent: their start times can be equal (a tick
        of Windows' clock is 15.6 ms), and an id's random tail sorts them at random then."""
        with self._lock:
            rows = self._db.execute(
                # Only module-constant canonical SQL; user values stay bound.
                "SELECT id FROM exchanges WHERE " + _EXCHANGE_GROUP + " = ? ORDER BY rowid",  # noqa: S608
                (self._session_selector(session),),
            )
            return [row[0] for row in rows]

    def detail(self, exchange_id: str) -> dict[str, Any] | None:
        """The stored exchange: masked blocks and response, with replacement offsets in masked text."""
        with self._lock:
            row = self._db.execute(
                # Only module-constant canonical SQL; user values stay bound.
                "SELECT id, summary, refs, request_headers, response, leaks, body_bytes, images, "  # noqa: S608
                + _EXCHANGE_VIEW
                + " FROM exchanges WHERE id = ?",
                (exchange_id,),
            ).fetchone()
            if row is None:
                return None
            exchange_id, summary, refs, headers, response, leaks, body_bytes, images, group = row
            images = self._open(images, f"{exchange_id}:images") or []
            kept = self._known_images([i["id"] for i in images if i.get("id")])
            for image in images:
                image["kept"] = image.get("id") in kept
            refs, sensitive = read_refs(self._open(refs, f"{exchange_id}:refs"))
            ids = list(dict.fromkeys(r[0] for r in refs))
            stored: dict[str, tuple[str, list[dict[str, Any]], str]] = {}
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                for block_id, kind, data in self._db.execute(
                    f"SELECT id, kind, data FROM blocks WHERE id IN ({marks})",  # noqa: S608
                    part,
                ):
                    masked, replacements = self._open(data, block_id)
                    stored[block_id] = (masked, replacements, kind)
            slots = []
            for ref in refs:
                block_id, path, from_model, new, tool, source = ref_parts(ref)
                found = stored.get(block_id)
                if found is None:
                    continue  # pruned
                masked, replacements, kind = found
                slots.append(
                    {
                        "block": block_id,
                        "path": path,
                        "kind": kind,
                        "from_model": from_model,
                        "new": new,
                        "masked": masked,
                        "replacements": replacements,
                        "tool": tool,
                        "source": source,
                    }
                )
            return {
                "summary": self._history_summary(summary, group, exchange_id),
                "sensitive": sensitive
                if sensitive is not None
                else legacy_private(json.loads(summary), bool(refs or headers or response)),
                "request": {
                    "headers": self._open(headers, f"{exchange_id}:headers") or [],
                    "slots": slots,
                    "images": images,
                    "body_bytes": body_bytes,
                },
                "response": self._open(response, f"{exchange_id}:response"),
                "leaks": detail_leaks(sensitive, json.loads(leaks), bool(refs or headers or response)),
            }

    def _known_images(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0] for r in self._db.execute(f"SELECT id FROM images WHERE id IN ({marks})", ids)}  # noqa: S608

    def image(self, image_id: str) -> tuple[str, bytes] | None:
        """An image as it was sent: its media type and bytes."""
        import base64

        with self._lock:
            row = self._db.execute("SELECT data FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return None
        found = self._open(row[0], image_id)
        return str(found["media_type"]), base64.b64decode(found["data"])

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT " + _EXCHANGE_GROUP + " AS canonical, MAX(agent), MIN(ts), MAX(ts), COUNT(*),"  # noqa: S608 - Only module-constant canonical SQL; user values stay bound.
                " COALESCE(SUM(json_extract(summary, '$.new_masked')), 0), SUM(flags LIKE '% blocked %')"
                " FROM exchanges GROUP BY canonical ORDER BY MAX(ts) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"key": k, "agent": a, "first_ts": f, "last_ts": la, "exchanges": n, "masked": m, "blocked": b}
            for k, a, f, la, n, m, b in rows
        ]

    def summaries_since(self, ts: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                # Only module-constant canonical SQL; user values stay bound.
                "SELECT id, summary, " + _EXCHANGE_VIEW + " FROM exchanges WHERE ts >= ? ORDER BY ts",  # noqa: S608
                (ts,),
            ).fetchall()
            return [self._history_summary(data, group, eid) for eid, data, group in rows]

    def summaries_parts(self, ts: float, most: int) -> Iterator[list[dict[str, Any]]]:
        """Existing selected-row materialization; each returned page projects through the same canonical source."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, summary, " + _EXCHANGE_VIEW + " FROM exchanges WHERE ts >= ? ORDER BY ts DESC LIMIT ?",  # noqa: S608 - Only module-constant canonical SQL; user values stay bound.
                (ts, max(0, most)),
            ).fetchall()
        for start in range(0, len(rows), _SUMMARIES_AT_ONCE):
            # No DB access or live owner is needed after a selected snapshot yields a page.
            yield [
                self._history_summary(data, group, eid) for eid, data, group in rows[start : start + _SUMMARIES_AT_ONCE]
            ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
